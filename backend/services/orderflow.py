"""Echter Orderflow aus dem öffentlichen Bitunix-Trade-Channel (WebSocket).

Ersetzt den bisherigen Kerzen-Volumen-Proxy durch echte Tick-Daten:
  * Delta (Käufer- vs. Verkäufer-Volumen) über 1/5/15 Minuten
  * CVD-Trend (kumulatives Volumen-Delta, letzte 15 min vs. davor)
  * Großaufträge (Prints >= 95. Perzentil des Fensters, dominante Seite)
  * Liquidity-Sweep-Heuristik: Volumen-Spike auf einer Seite, den der Kurs
    sofort wieder absorbiert (klassischer Stop-Hunt / Sweep)

Fällt der Stream aus (Reconnect läuft im Hintergrund), liefert snapshot_text
None und die KI nutzt automatisch weiter den alten Kerzen-Proxy.
RAM-schonend: pro Symbol ein begrenztes Deque (~30 min Ticks).
"""
import asyncio
import json
import logging
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

WS_URL = "wss://fapi.bitunix.com/public/"
WINDOW_SEC = 30 * 60          # Ticks der letzten 30 Minuten behalten
MAX_TICKS = 9000              # harte RAM-Grenze pro Symbol
MIN_TRADES_FOR_TEXT = 30      # unter dieser Datenlage: Fallback auf Kerzen-Proxy


class OrderflowCollector:
    def __init__(self):
        # symbol -> deque[(ts_sec, price, vol, is_buy)]
        self._ticks: Dict[str, Deque[Tuple[float, float, float, bool]]] = {}
        self.running = False
        self.status = {"connected": False, "last_error": None,
                       "messages": 0, "reconnects": 0, "symbols": []}

    # ---------------- Datenaufnahme ----------------
    def add_trade(self, symbol: str, ts_sec: float, price: float,
                  vol: float, is_buy: bool):
        dq = self._ticks.get(symbol)
        if dq is None:
            dq = self._ticks[symbol] = deque(maxlen=MAX_TICKS)
        dq.append((ts_sec, price, vol, is_buy))

    def _window(self, symbol: str, seconds: float) -> List[Tuple[float, float, float, bool]]:
        dq = self._ticks.get(symbol)
        if not dq:
            return []
        cutoff = time.time() - seconds
        return [t for t in dq if t[0] >= cutoff]

    # ---------------- Analyse ----------------
    @staticmethod
    def _delta(ticks) -> float:
        buy = sum(v for _, _, v, b in ticks if b)
        sell = sum(v for _, _, v, b in ticks if not b)
        tot = buy + sell
        return (buy - sell) / tot if tot else 0.0

    def stats(self, symbol: str) -> Dict:
        t15 = self._window(symbol, 900)
        t5 = [t for t in t15 if t[0] >= time.time() - 300]
        t1 = [t for t in t5 if t[0] >= time.time() - 60]
        prev15 = [t for t in self._window(symbol, 1800) if t[0] < time.time() - 900]
        big = self._large_prints(t15)
        sweep = self._sweep(symbol)
        return {
            "trades_15m": len(t15),
            "delta_1m": round(self._delta(t1), 3),
            "delta_5m": round(self._delta(t5), 3),
            "delta_15m": round(self._delta(t15), 3),
            "cvd_prev_15m": round(self._delta(prev15), 3),
            "buy_vol_15m": round(sum(v for _, _, v, b in t15 if b), 4),
            "sell_vol_15m": round(sum(v for _, _, v, b in t15 if not b), 4),
            "large_prints": big,
            "sweep": sweep,
            "connected": self.status["connected"],
        }

    @staticmethod
    def _large_prints(ticks) -> Optional[Dict]:
        """Prints >= 95. Perzentil (Notional) – wer setzt die großen Orders?"""
        if len(ticks) < 40:
            return None
        notionals = sorted(p * v for _, p, v, _ in ticks)
        p95 = notionals[int(len(notionals) * 0.95)]
        big = [(p * v, b) for _, p, v, b in ticks if p * v >= p95 and p95 > 0]
        if not big:
            return None
        buy_n = sum(n for n, b in big if b)
        sell_n = sum(n for n, b in big if not b)
        tot = buy_n + sell_n
        if tot <= 0:
            return None
        side = "Käufer" if buy_n > sell_n * 1.3 else (
            "Verkäufer" if sell_n > buy_n * 1.3 else "gemischt")
        return {"count": len(big), "notional_usdt": round(tot),
                "side": side, "buy_share": round(buy_n / tot, 2)}

    def _sweep(self, symbol: str) -> Optional[Dict]:
        """Sweep-Heuristik (letzte 5 min, 30s-Buckets): einseitiger
        Volumen-Spike (>=3x Median), den der Kurs sofort zurückerobert."""
        ticks = self._window(symbol, 300)
        if len(ticks) < 40:
            return None
        buckets: Dict[int, Dict] = {}
        for ts, p, v, b in ticks:
            key = int(ts // 30)
            bu = buckets.setdefault(key, {"buy": 0.0, "sell": 0.0,
                                          "lo": p, "hi": p, "last": p})
            bu["buy" if b else "sell"] += v
            bu["lo"] = min(bu["lo"], p)
            bu["hi"] = max(bu["hi"], p)
            bu["last"] = p
        if len(buckets) < 4:
            return None
        keys = sorted(buckets)
        vols = sorted(b["buy"] + b["sell"] for b in buckets.values())
        median = vols[len(vols) // 2] or 0.0
        if median <= 0:
            return None
        now_price = buckets[keys[-1]]["last"]
        for key in keys[:-1]:            # abgeschlossene Buckets
            bu = buckets[key]
            tot = bu["buy"] + bu["sell"]
            if tot < 3 * median:
                continue
            sell_heavy = bu["sell"] > bu["buy"] * 2
            buy_heavy = bu["buy"] > bu["sell"] * 2
            age = int(time.time() - key * 30)
            if sell_heavy and bu["lo"] > 0 and now_price >= bu["lo"] * 1.0005:
                return {"type": "sell_sweep_absorbed", "age_sec": age,
                        "level": bu["lo"],
                        "text": (f"Sell-Sweep vor {age}s @ {bu['lo']:g} absorbiert "
                                 "(Stop-Hunt unten, Käufer übernehmen)")}
            if buy_heavy and now_price <= bu["hi"] * 0.9995:
                return {"type": "buy_sweep_absorbed", "age_sec": age,
                        "level": bu["hi"],
                        "text": (f"Buy-Sweep vor {age}s @ {bu['hi']:g} abverkauft "
                                 "(Stop-Hunt oben, Verkäufer übernehmen)")}
        return None

    def snapshot_text(self, symbol: str) -> Optional[str]:
        """Eine kompakte Prompt-Zeile für die KI – None, wenn zu wenig Daten."""
        t15 = self._window(symbol, 900)
        if len(t15) < MIN_TRADES_FOR_TEXT:
            return None
        s = self.stats(symbol)
        side = ("Käufer" if s["delta_5m"] > 0.05
                else "Verkäufer" if s["delta_5m"] < -0.05 else "ausgeglichen")
        cvd = ("steigend" if s["delta_15m"] > s["cvd_prev_15m"] + 0.05
               else "fallend" if s["delta_15m"] < s["cvd_prev_15m"] - 0.05
               else "neutral")
        parts = [f"Orderflow (echte Bitunix-Trades, {s['trades_15m']} Ticks/15m): "
                 f"Delta 1m {s['delta_1m']:+.2f} / 5m {s['delta_5m']:+.2f} ({side}) "
                 f"/ 15m {s['delta_15m']:+.2f}, CVD-Trend {cvd}"]
        big = s.get("large_prints")
        if big:
            parts.append(f"Großaufträge: {big['count']} Prints "
                         f"~{big['notional_usdt']:,} USDT, dominant {big['side']}")
        sweep = s.get("sweep")
        if sweep:
            parts.append(f"⚠ {sweep['text']}")
        return " | ".join(parts)

    # ---------------- WebSocket-Loop ----------------
    async def run_loop(self, symbols: List[str]):
        import websockets
        self.running = True
        self.status["symbols"] = list(symbols)
        backoff = 5
        while self.running:
            try:
                async with websockets.connect(WS_URL, ping_interval=20,
                                              ping_timeout=20,
                                              open_timeout=15) as ws:
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": [{"ch": "trade", "symbol": s} for s in symbols]}))
                    self.status["connected"] = True
                    self.status["last_error"] = None
                    backoff = 5
                    logger.info(f"Orderflow: Trade-Channel abonniert ({len(symbols)} Symbole)")
                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            try:
                                await ws.send(json.dumps({"op": "ping"}))
                                continue
                            except Exception:
                                break
                        try:
                            data = json.loads(msg)
                        except ValueError:
                            continue
                        if data.get("ch") != "trade":
                            continue
                        sym = data.get("symbol")
                        rows = data.get("data") or []
                        if not sym or not isinstance(rows, list):
                            continue
                        now = time.time()
                        for r in rows:
                            try:
                                self.add_trade(sym, now, float(r["p"]),
                                               float(r["v"]),
                                               str(r.get("s", "")).lower() == "buy")
                            except (KeyError, TypeError, ValueError):
                                continue
                        self.status["messages"] += 1
            except Exception as e:
                self.status["connected"] = False
                self.status["last_error"] = str(e)[:200]
                logger.warning(f"Orderflow-WS: {e}")
            if self.running:
                self.status["reconnects"] += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def stop(self):
        self.running = False


orderflow = OrderflowCollector()
