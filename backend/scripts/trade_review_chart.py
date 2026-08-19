"""Trade-Review mit echten Kerzen (read-only Prod + öffentliche Markt-APIs).
Bewertet Entry/Exit/SL der letzten KI-Trades gegen den Chart: MFE/MAE in R,
TP1 erreichbar?, SL zu eng (Stop-out + anschließende Reversal-Bewegung)?"""
import os, asyncio, sys
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from motor.motor_asyncio import AsyncIOMotorClient
import aiohttp
from services import history_sources

MIN = 60_000


def ts_ms(iso):
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


async def candles(session, symbol, start_ms, end_ms):
    try:
        blocks = await history_sources.fetch_blocks(session, symbol, start_ms, end_ms)
        if not blocks:
            return None
        m = np.vstack(blocks)
        m = m[np.argsort(m[:, 0])]
        return m[(m[:, 0] >= start_ms) & (m[:, 0] <= end_ms)]
    except Exception as e:
        print(f"    [Kerzen-Fehler {symbol}: {str(e)[:80]}]")
        return None


def analyze(t, m):
    side = str(t.get("side") or "").upper()
    entry = float(t.get("entry") or 0)
    isl = float(t.get("initial_sl") or t.get("sl") or 0)
    tp1 = float(t.get("tp1") or 0)
    open_ms = ts_ms(t.get("opened_at"))
    close_ms = ts_ms(t.get("closed_at")) or int(datetime.now(timezone.utc).timestamp() * 1000)
    if not (entry and isl and open_ms) or m is None or len(m) < 3:
        return None
    sl_dist = abs(entry - isl)
    r = {}
    r["sl_pct"] = 100 * sl_dist / entry
    r["tp1_pct"] = 100 * abs(tp1 - entry) / entry if tp1 else None
    r["crv"] = abs(tp1 - entry) / sl_dist if tp1 and sl_dist else None
    live = m[(m[:, 0] >= open_ms) & (m[:, 0] <= close_ms)]
    if len(live) >= 1:
        if side == "LONG":
            mfe = (live[:, 2].max() - entry) / sl_dist
            mae = (entry - live[:, 3].min()) / sl_dist
            tp1_hit = bool(tp1 and live[:, 2].max() >= tp1)
            sl_hit = bool(live[:, 3].min() <= isl)
        else:
            mfe = (entry - live[:, 3].min()) / sl_dist
            mae = (live[:, 2].max() - entry) / sl_dist
            tp1_hit = bool(tp1 and live[:, 3].min() <= tp1)
            sl_hit = bool(live[:, 2].max() >= isl)
        r["mfe_r"] = round(float(mfe), 2)
        r["mae_r"] = round(float(mae), 2)
        r["tp1_hit_chart"] = tp1_hit
        r["sl_hit_chart"] = sl_hit
    after = m[(m[:, 0] > close_ms) & (m[:, 0] <= close_ms + 4 * 60 * MIN)]
    if len(after) >= 3 and tp1:
        if side == "LONG":
            r["tp1_after_close"] = bool(after[:, 2].max() >= tp1)
            r["post_move_r"] = round(float((after[:, 2].max() - entry) / sl_dist), 2)
        else:
            r["tp1_after_close"] = bool(after[:, 3].min() <= tp1)
            r["post_move_r"] = round(float((entry - after[:, 3].min()) / sl_dist), 2)
    pre = m[(m[:, 0] >= open_ms - 60 * MIN) & (m[:, 0] < open_ms)]
    if len(pre) >= 10:
        hi, lo = pre[:, 2].max(), pre[:, 3].min()
        rng = hi - lo
        if rng > 0:
            pos = (entry - lo) / rng
            r["entry_range_pos_60m"] = round(float(pos), 2)
    return r


async def main():
    c = AsyncIOMotorClient(os.environ["PROD_MONGO_URL"])
    db = c[os.environ["PROD_DB_NAME"]]
    closed = await db.auto_trades.find({"strategy_id": "ai_trader", "status": {"$ne": "open"}}) \
        .sort("closed_at", -1).to_list(25)
    open_tr = await db.auto_trades.find({"strategy_id": "ai_trader", "status": "open"}).to_list(20)
    async with aiohttp.ClientSession() as session:
        for label, trades in (("GESCHLOSSEN", closed), ("OFFEN", open_tr)):
            print(f"\n{'='*100}\n{label} ({len(trades)} Trades)\n{'='*100}")
            for t in trades:
                open_ms = ts_ms(t.get("opened_at"))
                close_ms = ts_ms(t.get("closed_at")) or int(datetime.now(timezone.utc).timestamp() * 1000)
                if not open_ms:
                    continue
                m = await candles(session, t["symbol"], open_ms - 90 * MIN,
                                  min(close_ms + 4 * 60 * MIN,
                                      int(datetime.now(timezone.utc).timestamp() * 1000)))
                a = analyze(t, m)
                pnl = float(t.get("realized_pnl") or 0)
                fees = float(t.get("fees_paid") or 0)
                risk = float(t.get("risk") or 0)
                dur = (close_ms - open_ms) / 60000
                print(f"\n  {t['symbol']} {t.get('side')} conf={t.get('ai_confidence')} "
                      f"setup={t.get('setup')} dc={1 if t.get('data_collection') else 0} "
                      f"lev={t.get('leverage')} dauer={dur:.0f}min")
                print(f"    entry={t.get('entry')} isl={t.get('initial_sl')} tp1={t.get('tp1')} "
                      f"exit={t.get('exit_price')} pnl={pnl:+.2f}$ fees={fees:.2f}$ risk={risk:.2f}$ "
                      f"result={t.get('result')}")
                if a:
                    tp1d = f"{a['tp1_pct']:.2f}%" if a.get("tp1_pct") else "-"
                    crv = f"{a['crv']:.1f}" if a.get("crv") else "-"
                    print(f"    CHART: SL-Dist={a['sl_pct']:.2f}% TP1-Dist={tp1d} "
                          f"CRV={crv} | MFE={a.get('mfe_r')}R MAE={a.get('mae_r')}R "
                          f"TP1-hit={a.get('tp1_hit_chart')} SL-hit={a.get('sl_hit_chart')} "
                          f"| nach Close: TP1 doch erreicht={a.get('tp1_after_close')} "
                          f"post_move={a.get('post_move_r')}R | Entry-Pos 60m-Range={a.get('entry_range_pos_60m')}")
                reason = str(t.get("ai_reasoning") or "")[:180]
                if reason:
                    print(f"    KI: {reason}")
                lr = str(t.get("ai_levels_reason") or "")[:140]
                if lr:
                    print(f"    Levels: {lr}")
    c.close()

asyncio.run(main())
