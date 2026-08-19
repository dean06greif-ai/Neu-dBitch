"""Tests: echter Orderflow (Bitunix Trade-Channel) + Lektionen-Neubewertung."""
import time

from services.orderflow import OrderflowCollector
from services.ai_learning import AILearning


# ---------------- Orderflow ----------------
def _fill(c, symbol="BTCUSDT", n=120, buy_ratio=0.7, price=100.0, vol=1.0,
          age_start=800):
    now = time.time()
    for i in range(n):
        is_buy = (i % 10) < int(buy_ratio * 10)
        c.add_trade(symbol, now - age_start + i * (age_start / max(n, 1)),
                    price, vol, is_buy)


def test_orderflow_delta_direction():
    c = OrderflowCollector()
    _fill(c, buy_ratio=0.8)
    s = c.stats("BTCUSDT")
    assert s["trades_15m"] >= 100
    assert s["delta_15m"] > 0.4          # klar käufergetrieben
    txt = c.snapshot_text("BTCUSDT")
    assert txt and "echte Bitunix-Trades" in txt and "Delta" in txt


def test_orderflow_needs_min_trades():
    c = OrderflowCollector()
    _fill(c, n=10)
    assert c.snapshot_text("BTCUSDT") is None   # Fallback auf Kerzen-Proxy


def test_orderflow_sell_sweep_absorbed():
    c = OrderflowCollector()
    now = time.time()
    # 4 min normales Grundrauschen (kleine Buckets)
    for i in range(48):
        c.add_trade("X", now - 280 + i * 5, 100.0, 0.5, i % 2 == 0)
    # Spike vor ~60s: massives SELL-Volumen drückt auf 99.0 …
    for i in range(10):
        c.add_trade("X", now - 65 + i * 0.5, 99.0, 5.0, False)
    # … Kurs erholt sich sofort wieder über das Sweep-Tief
    for i in range(10):
        c.add_trade("X", now - 20 + i, 100.2, 0.5, True)
    sweep = c.stats("X")["sweep"]
    assert sweep and sweep["type"] == "sell_sweep_absorbed"
    assert "absorbiert" in sweep["text"]


def test_orderflow_large_prints_side():
    c = OrderflowCollector()
    now = time.time()
    for i in range(100):
        c.add_trade("Y", now - 500 + i * 4, 100.0, 0.1, i % 2 == 0)
    for i in range(5):   # große Käufer-Prints
        c.add_trade("Y", now - 100 + i, 100.0, 50.0, True)
    big = c.stats("Y")["large_prints"]
    assert big and big["side"] == "Käufer" and big["buy_share"] >= 0.9


# ---------------- Lektionen-Neubewertung (reine Anwenden-Logik) ----------------
def _lessons():
    return [
        {"id": "les_a", "title": "Alte 15min-Latenz", "detail": "x", "locked": False},
        {"id": "les_b", "title": "Trader-Regel", "detail": "y", "locked": True},
        {"id": "les_c", "title": "SL-Politik alt", "detail": "z", "locked": False,
         "origin": "ai"},
        {"id": "les_d", "title": "Bleibt gültig", "detail": "w", "locked": False},
    ]


def _apply(verdicts):
    learn = AILearning.__new__(AILearning)
    return learn._reeval_apply(_lessons(), verdicts)


def test_reeval_removes_outdated_only_unlocked():
    res = _apply([
        {"id": "les_a", "verdict": "veraltet", "reason": "Intervall geändert"},
        {"id": "les_b", "verdict": "veraltet", "reason": "darf nicht"},
    ])
    removed_ids = [r["id"] for r in res["removed"]]
    assert removed_ids == ["les_a"]
    kept_ids = [l["id"] for l in res["kept"]]
    assert "les_b" in kept_ids, "LOCKED-Lektion darf NIE entfernt werden"
    assert res["protected"] and res["protected"][0]["id"] == "les_b"


def test_reeval_adjust_updates_detail():
    res = _apply([{"id": "les_c", "verdict": "anpassen",
                   "reason": "neue SL-Politik", "new_detail": "Mind. 1.5 ATR SL"}])
    lc = next(l for l in res["kept"] if l["id"] == "les_c")
    assert lc["detail"] == "Mind. 1.5 ATR SL"
    assert lc.get("revalidated_at")
    assert res["adjusted"] and res["adjusted"][0]["id"] == "les_c"


def test_reeval_valid_gets_fresh_stamp():
    res = _apply([{"id": "les_d", "verdict": "gueltig"}])
    ld = next(l for l in res["kept"] if l["id"] == "les_d")
    assert ld.get("revalidated_at")
    assert not res["removed"] and not res["adjusted"]
