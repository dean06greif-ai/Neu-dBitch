"""Tests Stale-Price-Guard + Regime-Sperrfilter (Default AUS) im KI-Trader."""
import asyncio
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient

MIN_MS = 60_000


def fresh_buf(sym_price=1.16):
    now = time.time() * 1000
    return [{"timestamp": now - 30_000, "open": sym_price, "close": sym_price,
             "high": sym_price, "low": sym_price, "volume": 5}]


def stale_buf(age_min=45, sym_price=1.16):
    now = time.time() * 1000
    return [{"timestamp": now - age_min * MIN_MS, "open": sym_price, "close": sym_price,
             "high": sym_price, "low": sym_price, "volume": 5}]


def make_dec(action="LONG", conf=75):
    return {"symbol": "EURUSD", "action": action, "confidence": conf, "price": 1.16,
            "sl_pct": 0.6, "tp1_pct": 1.2, "tpf_pct": 2.4, "reasoning": "t"}


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"] + "_test_stale_regime"]
    import services.ai_engine as em
    from services.ai_engine import ai_engine, DEFAULT_AI_CONFIG
    from services.ai_market_observer import market_observer

    old_db, old_cfg, old_scanner = ai_engine.db, ai_engine.config, ai_engine.scanner
    orig_check_trade = em.master_prompt.check_trade
    orig_snap = market_observer.snapshots.pop("EURUSD", None)
    ai_engine.db = db
    ai_engine.config = dict(DEFAULT_AI_CONFIG)
    # MasterPrompt als kontrollierter Endpunkt: wer hier ankommt, hat alle
    # neuen Guards passiert -> blocked_by == "TESTSTOP".
    em.master_prompt.check_trade = lambda *a, **k: (False, "TESTSTOP")

    try:
        # ---------- 1) Defaults ----------
        assert ai_engine.config["stale_price_max_min"] == 10
        assert ai_engine.config["regime_block_enabled"] is False
        assert ai_engine.config["regime_block_list"] == ["range_ruhig"]
        print("PASS 1: Defaults (Stale 10 min an, Regime-Sperre AUS, Liste range_ruhig)")

        # ---------- 2) update_config: Klemmen + Validierung ----------
        await ai_engine.update_config({"stale_price_max_min": 999})
        assert ai_engine.config["stale_price_max_min"] == 120.0
        await ai_engine.update_config({"stale_price_max_min": -5})
        assert ai_engine.config["stale_price_max_min"] == 0.0
        await ai_engine.update_config({"stale_price_max_min": 10,
                                       "regime_block_enabled": True,
                                       "regime_block_list": ["range_ruhig", "  drift_ruhig  ", "", 42]})
        assert ai_engine.config["regime_block_enabled"] is True
        assert ai_engine.config["regime_block_list"] == ["range_ruhig", "drift_ruhig", "42"]
        await ai_engine.update_config({"regime_block_enabled": False,
                                       "regime_block_list": ["range_ruhig"]})
        print("PASS 2: update_config (Klemme 0-120, Bool, Listen-Bereinigung)")

        # ---------- 3) Stale-Price-Guard blockt alten Kurs ----------
        ai_engine.scanner = SimpleNamespace(candle_buffer={"EURUSD": stale_buf(45)})
        dec = make_dec()
        ok = await ai_engine._emit_signal(dec)
        assert ok is False and "Stale-Price-Guard" in str(dec.get("blocked_by")), dec.get("blocked_by")
        assert "45" in str(dec.get("blocked_by"))
        # Sammel-Trades ebenfalls geblockt (veralteter Preis ist für Labels wertlos)
        dec_c = make_dec(conf=65)
        ok_c = await ai_engine._emit_signal(dec_c, collection=True)
        assert ok_c is False and "Stale-Price-Guard" in str(dec_c.get("blocked_by"))
        print("PASS 3: Stale-Price-Guard blockt 45-min-alten Kurs (live + Sammel)")

        # ---------- 4) Frischer Kurs passiert; 0 = aus ----------
        ai_engine.scanner = SimpleNamespace(candle_buffer={"EURUSD": fresh_buf()})
        dec = make_dec()
        ok = await ai_engine._emit_signal(dec)
        assert ok is False and str(dec.get("blocked_by")) == "TESTSTOP", dec.get("blocked_by")
        ai_engine.scanner = SimpleNamespace(candle_buffer={"EURUSD": stale_buf(45)})
        await ai_engine.update_config({"stale_price_max_min": 0})
        dec = make_dec()
        ok = await ai_engine._emit_signal(dec)
        assert str(dec.get("blocked_by")) == "TESTSTOP"  # Guard aus -> durchgereicht
        await ai_engine.update_config({"stale_price_max_min": 10})
        # Leerer Buffer (Boot) blockt nicht fälschlich
        ai_engine.scanner = SimpleNamespace(candle_buffer={})
        dec = make_dec()
        await ai_engine._emit_signal(dec)
        assert str(dec.get("blocked_by")) == "TESTSTOP"
        print("PASS 4: frischer Kurs passiert, 0=aus, leerer Buffer blockt nicht")

        # ---------- 5) Regime-Sperrfilter: Default AUS, aktiv blockt nur live ----------
        ai_engine.scanner = SimpleNamespace(candle_buffer={"EURUSD": fresh_buf()})
        market_observer.snapshots["EURUSD"] = {"features": {"regime": "range_ruhig"}}
        dec = make_dec()
        await ai_engine._emit_signal(dec)
        assert str(dec.get("blocked_by")) == "TESTSTOP"  # Default AUS -> kein Regime-Block
        await ai_engine.update_config({"regime_block_enabled": True})
        await db.ai_chat.delete_many({})
        dec = make_dec()
        ok = await ai_engine._emit_signal(dec)
        assert ok is False and "Regime-Sperrfilter" in str(dec.get("blocked_by")), dec.get("blocked_by")
        gov = await db.ai_chat.find_one({"role": "governance"})
        assert gov and "Regime-Sperrfilter" in gov["text"]
        # Sammel-Trades laufen weiter (Statistik-Beweis)
        dec_c = make_dec(conf=65)
        await ai_engine._emit_signal(dec_c, collection=True)
        assert str(dec_c.get("blocked_by")) == "TESTSTOP"
        # Anderes Regime -> kein Block
        market_observer.snapshots["EURUSD"] = {"features": {"regime": "trend_volatil"}}
        dec = make_dec()
        await ai_engine._emit_signal(dec)
        assert str(dec.get("blocked_by")) == "TESTSTOP"
        await ai_engine.update_config({"regime_block_enabled": False})
        print("PASS 5: Regime-Sperre Default AUS; aktiv: blockt nur live + Governance-Eintrag, Sammel/andere Regime frei")
    finally:
        em.master_prompt.check_trade = orig_check_trade
        ai_engine.db, ai_engine.config, ai_engine.scanner = old_db, old_cfg, old_scanner
        if orig_snap is not None:
            market_observer.snapshots["EURUSD"] = orig_snap
        else:
            market_observer.snapshots.pop("EURUSD", None)
        await db.client.drop_database(db.name)
    print("Cleanup OK – alle 5 Testblöcke grün")


asyncio.run(main())
