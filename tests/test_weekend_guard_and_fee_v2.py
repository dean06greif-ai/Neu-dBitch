"""Tests Wochenend-Guard (Markt-Kalender) + Fee-Wächter V2 (ATR-Floor) + be_mode=crv-Default."""
import asyncio
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def make_candles(n=120, price=100.0):
    random.seed(7)
    out, ts = [], int(time.time() * 1000) - n * 60_000
    for i in range(n):
        o = price
        price = max(1.0, price * (1 + random.uniform(-0.002, 0.002)))
        out.append({"timestamp": ts + i * 60_000, "open": o, "close": price,
                    "high": max(o, price) * 1.001, "low": min(o, price) * 0.999,
                    "volume": random.uniform(10, 100)})
    return out


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"] + "_test_wk_fee2"]
    from core import market_hours
    from services.bitunix_trade import fee_guard_min_sl_pct, fee_guard_check
    from services.ai_engine import ai_engine, DEFAULT_AI_CONFIG

    # ---------- 1) market_hours: reine Kalender-Logik ----------
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fri = (base + timedelta(days=(4 - base.weekday()) % 7)).replace(hour=0, minute=0)
    sat, sun, wed = fri + timedelta(days=1), fri + timedelta(days=2), fri + timedelta(days=5)
    assert (fri.weekday(), sat.weekday(), sun.weekday()) == (4, 5, 6)
    # Krypto immer offen
    assert market_hours.is_weekend_closed("BTCUSDT", sat.replace(hour=12))[0] is False
    assert market_hours.is_weekend_closed("UNBEKANNT", sat.replace(hour=12))[0] is False
    # Forex: Fr ab 21:00 zu, Sa komplett, So bis 21:15
    assert market_hours.is_weekend_closed("EURUSD", fri.replace(hour=20, minute=59))[0] is False
    assert market_hours.is_weekend_closed("EURUSD", fri.replace(hour=21))[0] is True
    assert market_hours.is_weekend_closed("EURUSD", sat.replace(hour=12))[0] is True
    assert market_hours.is_weekend_closed("EURUSD", sun.replace(hour=21, minute=14))[0] is True
    assert market_hours.is_weekend_closed("EURUSD", sun.replace(hour=21, minute=16))[0] is False
    # Rohstoffe/Indizes: So bis 22:05
    assert market_hours.is_weekend_closed("GOLD", sun.replace(hour=22, minute=4))[0] is True
    assert market_hours.is_weekend_closed("GOLD", sun.replace(hour=22, minute=6))[0] is False
    closed, why = market_hours.is_weekend_closed("QQQUSDT", sat.replace(hour=3))
    assert closed and "Markt geschlossen" in why and "Wochenende" in why
    # Wochentag: alles offen
    assert market_hours.is_weekend_closed("EURUSD", wed.replace(hour=12))[0] is False
    assert market_hours.is_weekend_closed("OIL", wed.replace(hour=12))[0] is False
    print("PASS 1: market_hours-Kalender (Krypto 24/7, Forex/CME-Wochenendfenster)")

    # ---------- 2) Fee-Wächter V2: ATR-Floor-Mathematik ----------
    assert abs(fee_guard_min_sl_pct(0.06, 4.0) - 0.48) < 1e-9                     # nur Fees
    assert abs(fee_guard_min_sl_pct(0.06, 4.0, atr_pct=0.2, atr_mult=4.0) - 0.8) < 1e-9  # ATR bindet
    assert abs(fee_guard_min_sl_pct(0.06, 4.0, atr_pct=0.05, atr_mult=4.0) - 0.48) < 1e-9  # Fees binden
    cfg = {"fee_percent": 0.06}
    # ATR 0.2% vom Kurs, 4x -> Minimum 0.8%; SL 0.6% wird geblockt mit ATR-Grund
    ok, why = fee_guard_check({"fee_guard_enabled": True, "fee_guard_mult": 4.0,
                               "fee_guard_atr_mult": 4.0}, cfg, 100.0, 99.4, atr=0.2)
    assert not ok and "ATR-Minimum" in why, why
    # gleicher SL ohne ATR (Rückwärtskompatibilität) -> ok (0.6% >= 0.48%)
    ok, _ = fee_guard_check({"fee_guard_enabled": True, "fee_guard_mult": 4.0,
                             "fee_guard_atr_mult": 4.0}, cfg, 100.0, 99.4)
    assert ok
    # ATR-Faktor 0 = aus -> nur Fee-Floor gilt
    ok, _ = fee_guard_check({"fee_guard_enabled": True, "fee_guard_mult": 4.0,
                             "fee_guard_atr_mult": 0}, cfg, 100.0, 99.4, atr=0.2)
    assert ok
    # Fee-Floor blockt weiterhin mit Fee-Grund (SL 0.2% < 0.48%)
    ok, why = fee_guard_check({"fee_guard_enabled": True, "fee_guard_mult": 4.0,
                               "fee_guard_atr_mult": 4.0}, cfg, 100.0, 99.8, atr=0.05)
    assert not ok and "Roundtrip-Fees" in why
    print("PASS 2: fee_guard V2 (ATR-Floor bindet/aus, Fee-Floor unverändert, rückwärtskompatibel)")

    # ---------- 3) update_config: fee_guard_atr_mult Klemme 0-30 ----------
    old_db, old_cfg = ai_engine.db, ai_engine.config
    ai_engine.db = db
    ai_engine.config = dict(DEFAULT_AI_CONFIG)
    assert ai_engine.config["fee_guard_atr_mult"] == 4.0
    await ai_engine.update_config({"fee_guard_atr_mult": 99})
    assert ai_engine.config["fee_guard_atr_mult"] == 30.0
    await ai_engine.update_config({"fee_guard_atr_mult": 0})
    assert ai_engine.config["fee_guard_atr_mult"] == 0.0
    ai_engine.db, ai_engine.config = old_db, old_cfg
    print("PASS 3: update_config übernimmt fee_guard_atr_mult (geklemmt 0-30)")

    # ---------- 4) _emit_signal: Wochenend-Guard blockt vor allem anderen ----------
    import services.ai_engine as ai_engine_module
    orig_fn = market_hours.is_weekend_closed
    try:
        market_hours.is_weekend_closed = lambda sym, now=None: (
            (True, "Markt geschlossen (Wochenende): Test") if sym == "EURUSD" else (False, ""))
        dec = {"symbol": "EURUSD", "action": "LONG", "confidence": 75, "price": 1.16,
               "sl_pct": 0.5, "tp1_pct": 1.0, "tpf_pct": 2.0, "reasoning": "t"}
        ok = await ai_engine._emit_signal(dec)
        assert ok is False and "Markt geschlossen" in str(dec.get("blocked_by"))
        dec2 = {"symbol": "EURUSD", "action": "SHORT", "confidence": 65, "price": 1.16,
                "sl_pct": 0.5, "tp1_pct": 1.0, "tpf_pct": 2.0, "reasoning": "t"}
        ok2 = await ai_engine._emit_signal(dec2, collection=True)
        assert ok2 is False and "Markt geschlossen" in str(dec2.get("blocked_by"))
    finally:
        market_hours.is_weekend_closed = orig_fn
    print("PASS 4: _emit_signal blockt Live- UND Sammel-Einstiege bei geschlossenem Markt")

    # ---------- 5) be_mode=crv-Default für KI-Trader ----------
    from core.state import autotrader
    old_at_db = autotrader.db
    autotrader.db = db
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}},
                           "strategy_coin_configs": {"ai_trader_TESTUSDT": {"mode": "paper"}}})
    await db.auto_trades.delete_many({})
    await db.settings.update_one(
        {"_id": "ai_trader_config"},
        {"$set": {"fee_guard_enabled": True, "fee_guard_mult": 4.0, "fee_guard_atr_mult": 0.0,
                  "max_trades_per_coin": 1, "collection_max_per_coin": 2}}, upsert=True)
    candles = make_candles()
    entry = candles[-1]["close"]
    base_sig = {"symbol": "TESTUSDT", "type": "LONG", "entry_price": entry,
                "strategy_id": "ai_trader", "strategy_name": "KI Trader",
                "timeframe": "1m", "use_ai_levels": True,
                "trade_date": datetime.now(timezone.utc).date().isoformat()}
    s1 = {**base_sig, "id": f"be1_{int(time.time())}",
          "stop_loss": entry * 0.99, "take_profit_1": entry * 1.02,
          "take_profit_full": entry * 1.04}
    t1 = await autotrader.on_signal(s1, candles)
    assert t1, f"Trade nicht eröffnet: {s1.get('_reject_reason')}"
    assert t1["be_mode"] == "crv" and float(t1["be_trigger_crv"]) == 1.0, t1["be_mode"]
    # Explizite Nutzer-Einstellung hat Vorrang
    await db.auto_trades.delete_many({})
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}},
                           "strategy_coin_configs": {"ai_trader_TESTUSDT": {"mode": "paper",
                                                                            "be_mode": "tp1"}}})
    s2 = {**base_sig, "id": f"be2_{int(time.time())}",
          "stop_loss": entry * 0.99, "take_profit_1": entry * 1.02,
          "take_profit_full": entry * 1.04}
    t2 = await autotrader.on_signal(s2, candles)
    assert t2 and t2["be_mode"] == "tp1", (t2 or {}).get("be_mode")
    # Andere Strategien: Default bleibt tp1
    await db.auto_trades.delete_many({})
    autotrader.set_config({"mode": "paper", "coins": {"TESTUSDT": {"enabled": True}},
                           "sl_mode": "fixed", "sl_fixed_percent": 1.0})
    s3 = {"symbol": "TESTUSDT", "type": "LONG", "entry_price": entry,
          "strategy_id": "momentum", "strategy_name": "Momentum", "timeframe": "1m",
          "id": f"be3_{int(time.time())}",
          "trade_date": datetime.now(timezone.utc).date().isoformat()}
    t3 = await autotrader.on_signal(s3, candles)
    assert t3 and t3["be_mode"] == "tp1", (t3 or {}).get("be_mode")
    autotrader.db = old_at_db
    print("PASS 5: be_mode=crv-Default nur für KI-Trader; explizite Settings + andere Strategien unberührt")

    await db.client.drop_database(db.name)
    print("Cleanup OK – alle 5 Testblöcke grün")


asyncio.run(main())
