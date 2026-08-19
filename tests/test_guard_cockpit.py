"""Tests Guard-Cockpit-Endpoint (/api/ai/guard-stats): Kategorisierung + Regime-Schatten."""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"] + "_test_guard_cockpit"]
    from services.ai_engine import ai_engine, DEFAULT_AI_CONFIG
    from routers.ai import ai_guard_stats

    old_db, old_cfg = ai_engine.db, ai_engine.config
    ai_engine.db = db
    ai_engine.config = dict(DEFAULT_AI_CONFIG)
    try:
        ts = now_iso()
        blocked = [
            ("EURUSD", "Markt geschlossen (Wochenende): Euro/US-Dollar öffnet erst wieder Sonntag ~21:15 UTC – kein KI-Einstieg auf eingefrorenen Kursen", False),
            ("GOLD", "Markt geschlossen (Wochenende): Gold öffnet erst wieder Sonntag ~22:05 UTC – kein KI-Einstieg auf eingefrorenen Kursen", True),
            ("EURUSD", "Stale-Price-Guard: letzter Kurs von EURUSD ist 45 min alt (Limit 10 min) – kein Einstieg auf veralteten Daten", False),
            ("BTCUSDT", "Regime-Sperrfilter: BTCUSDT ist im Regime 'range_ruhig' – Neueinstiege in unklaren Marktphasen gesperrt (Sammel-Trades laufen weiter)", False),
            ("ETHUSDT", "Korrelations-Guard: BTC/ETH/SOL zählen als EIN Richtungs-Risiko – ein LONG ist bereits offen", False),
            ("SOLUSDT", "Richtungs-Guard: bereits 5 offene LONG-Risiken (GOLD, OIL) – Limit 5, kein weiterer gleichgerichteter Trade", True),
            ("SOLUSDT", "Richtungs-Guard: bereits 3 offene LONG-Risiken – Limit 3, kein weiterer gleichgerichteter Trade", False),
            ("ADAUSDT", "Cluster-Guard: offener LONG auf ADAUSDT @ 0.7 liegt nur 0.1% entfernt", False),
            ("XRPUSDT", "Playbook: Setup 'breakout' ist gesperrt (schwache Performance)", False),
            ("BNBUSDT", "MasterPrompt: bereits 3 offene KI-Trades – Limit 3, kein weiterer gleichgerichteter Trade", False),
            ("DOGEUSDT", "Irgendein unbekannter Grund", False),
        ]
        await db.ai_decisions.insert_many([
            {"ts": ts, "symbol": s, "action": "LONG", "blocked_by": why, "data_collection": dc}
            for s, why, dc in blocked])
        # nicht geblockte Decision darf nicht zählen
        await db.ai_decisions.insert_one({"ts": ts, "symbol": "LTCUSDT", "action": "LONG",
                                          "signaled": True, "blocked_by": None})
        await db.fee_guard_blocks.insert_many([
            {"id": "f1", "ts": ts, "symbol": "BTCUSDT", "side": "LONG", "collection": False,
             "sl_dist_pct": 0.2, "est_fees_usdt": 1.2, "reason": "Fee-Wächter: SL-Distanz 0.200% < Minimum 0.48%"},
            {"id": "f2", "ts": ts, "symbol": "ETHUSDT", "side": "SHORT", "collection": True,
             "sl_dist_pct": 0.3, "est_fees_usdt": 0.8, "reason": "Fee-Wächter: SL-Distanz 0.300% < ATR-Minimum 0.80%"},
        ])
        # Signale mit Regime für den Schatten-Zähler
        await db.ai_decisions.insert_many([
            {"ts": ts, "symbol": "BTCUSDT", "action": "LONG", "signaled": True,
             "entry_market_snapshot": {"features": {"regime": "range_ruhig"}}},
            {"ts": ts, "symbol": "ETHUSDT", "action": "SHORT", "signaled": True,
             "entry_market_snapshot": {"features": {"regime": "trend_up_volatil"}}},
            {"ts": ts, "symbol": "SOLUSDT", "action": "LONG", "signaled": True, "data_collection": True,
             "entry_market_snapshot": {"features": {"regime": "range_ruhig"}}},
        ])

        res = await ai_guard_stats(days=7)
        by = {g["key"]: g for g in res["guards"]}
        assert by["weekend"]["count"] == 2 and by["weekend"]["collection"] == 1
        assert by["stale"]["count"] == 1
        assert by["regime"]["count"] == 1
        assert by["correlation"]["count"] == 1
        assert by["direction"]["count"] == 2 and by["direction"]["collection"] == 1
        assert by["cluster"]["count"] == 1
        assert by["playbook"]["count"] == 1
        assert by["master"]["count"] == 1
        assert by["other"]["count"] == 1
        assert by["fee"]["count"] == 2 and by["fee"]["collection"] == 1
        assert by["fee"]["last"]["reason"].startswith("Fee-Wächter")
        assert res["total_blocked"] == 13
        assert by["weekend"]["last"]["symbol"] in ("EURUSD", "GOLD")
        print("PASS 1: Kategorisierung aller 10 Guard-Typen + Fee-Wächter + Sonstige korrekt")

        shadow = res["regime_shadow"]
        assert shadow["enabled"] is False
        assert shadow["list"] == ["range_ruhig"]
        assert shadow["would_block_live"] == 1  # nur BTC live, SOL ist Sammel-Trade
        print("PASS 2: Regime-Schatten zählt nur Live-Signale im gesperrten Regime")

        res14 = await ai_guard_stats(days=999)
        assert res14["days"] == 90  # Klemme
        print("PASS 3: days-Parameter geklemmt (1-90)")
    finally:
        ai_engine.db, ai_engine.config = old_db, old_cfg
        await db.client.drop_database(db.name)
    print("Cleanup OK – alle 3 Testblöcke grün")


asyncio.run(main())
