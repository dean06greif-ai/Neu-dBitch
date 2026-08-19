"""Tests Equity-Kurven-Endpoint (/api/ai/equity-curve) für den Verlauf-Reiter."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def iso(dt):
    return dt.isoformat()


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"] + "_test_equity"]
    from services.ai_engine import ai_engine
    from routers.ai import ai_equity_curve

    old_db = ai_engine.db
    ai_engine.db = db
    now = datetime.now(timezone.utc)
    try:
        # Reset-Fall: leere DB -> leere Kurve, keine Fehler
        res = await ai_equity_curve(days=0, mode="all")
        assert res["points"] == [] and res["summary"]["trades"] == 0
        print("PASS 1: leere DB (nach Reset) -> leere Kurve, summary=0")

        trades = [
            # (closed_at-offset_tage, pnl, dc, status)
            (10, +5.0, False, "closed"),
            (8, -2.0, True, "closed"),
            (5, +3.0, False, "closed"),
            (2, -4.0, True, "closed"),
            (1, +1.0, False, "closed"),
        ]
        docs = []
        for i, (off, pnl, dc, status) in enumerate(trades):
            docs.append({"id": f"t{i}", "strategy_id": "ai_trader", "status": status,
                         "symbol": "BTCUSDT", "side": "LONG",
                         "closed_at": iso(now - timedelta(days=off)),
                         "realized_pnl": pnl, "fees_paid": 0.5,
                         "data_collection": dc})
        # offener Trade + fremde Strategie dürfen NICHT zählen
        docs.append({"id": "open1", "strategy_id": "ai_trader", "status": "open",
                     "symbol": "ETHUSDT", "side": "LONG", "realized_pnl": 99})
        docs.append({"id": "x1", "strategy_id": "momentum", "status": "closed",
                     "symbol": "ETHUSDT", "side": "LONG",
                     "closed_at": iso(now - timedelta(days=1)), "realized_pnl": 99})
        await db.auto_trades.insert_many(docs)

        res = await ai_equity_curve(days=0, mode="all")
        assert res["summary"]["trades"] == 5
        eq = [p["equity"] for p in res["points"]]
        assert eq == [5.0, 3.0, 6.0, 2.0, 3.0], eq  # kumuliert, zeitlich sortiert
        s = res["summary"]
        assert s["total_pnl"] == 3.0 and s["wins"] == 3 and s["winrate"] == 60.0
        assert s["peak_equity"] == 6.0 and s["max_drawdown"] == 4.0
        assert s["fees"] == 2.5
        print("PASS 2: Kumulation, Sortierung, Winrate, Peak/Drawdown, Fees korrekt")

        res_live = await ai_equity_curve(days=0, mode="live")
        assert res_live["summary"]["trades"] == 3
        assert res_live["summary"]["total_pnl"] == 9.0
        res_dc = await ai_equity_curve(days=0, mode="collection")
        assert res_dc["summary"]["trades"] == 2
        assert res_dc["summary"]["total_pnl"] == -6.0
        print("PASS 3: mode=live/collection filtert nach data_collection")

        res_7d = await ai_equity_curve(days=7, mode="all")
        assert res_7d["summary"]["trades"] == 3  # nur Trades der letzten 7 Tage
        res_clamp = await ai_equity_curve(days=9999, mode="quatsch")
        assert res_clamp["days"] == 365 and res_clamp["mode"] == "all"
        print("PASS 4: days-Filter + Klemme (0-365) + mode-Fallback auf all")
    finally:
        ai_engine.db = old_db
        await db.client.drop_database(db.name)
    print("Cleanup OK – alle 4 Testblöcke grün")


asyncio.run(main())
