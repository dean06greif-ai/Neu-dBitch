"""Iteration 46 – Watchdog-Reste-Fix (Bug-Report POL):
Nach dem Close eines Bot-Trades verbleibende Rest-Positionen an der Börse
dürfen NICHT als 'Manuell (Bitunix)' übernommen werden. Stattdessen:
  a) Rest erkannt (Bot-Close < 30 min her) -> sofort an der Börse bereinigen
  b) Bereinigung schlägt fehl -> als 'Rest nach Bot-Close' übernehmen
  c) kein kürzlicher Bot-Close -> weiterhin normal 'Manuell (Bitunix)'
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "crypto_scanner")

SYM = "WDTESTUSDT"  # eigenes Test-Symbol, kollidiert mit nichts Echtem


class _FakeClient:
    def __init__(self, close_ok=True):
        self.close_ok = close_ok
        self.close_calls = []

    async def get_mark_price(self, symbol):
        return 1.0

    async def flash_close(self, symbol, position_id, side, qty):
        self.close_calls.append((symbol, position_id, side, qty))
        if not self.close_ok:
            raise RuntimeError("simulierter Börsen-Fehler")
        return {"code": 0}


def _pos(side="LONG", qty=0.4):
    return {"bitunix_symbol": SYM, "side": side, "qty": qty, "entry": 1.0,
            "position_id": f"pid-{uuid.uuid4().hex[:8]}", "margin": 1.0}


def _wd(db, client):
    from services.position_watchdog import PositionWatchdog
    wd = PositionWatchdog()
    wd.db = db
    wd.client = client
    return wd


async def _seed_closed_bot_trade(db, minutes_ago=5):
    doc = {"id": str(uuid.uuid4()), "symbol": SYM, "side": "LONG",
           "mode": "live", "status": "closed", "strategy_id": "ai_trader",
           "strategy_name": "KI Trader",
           "closed_at": (datetime.now(timezone.utc)
                         - timedelta(minutes=minutes_ago)).isoformat()}
    await db.auto_trades.insert_one(dict(doc))
    return doc


async def _cleanup(db):
    await db.auto_trades.delete_many({"symbol": SYM})


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


def test_leftover_cleaned_at_exchange(db):
    async def scenario():
        await _cleanup(db)
        await _seed_closed_bot_trade(db)
        client = _FakeClient(close_ok=True)
        wd = _wd(db, client)
        result = await wd._adopt(SYM, _pos())
        assert result is None, "Rest darf NICHT als Trade übernommen werden"
        assert len(client.close_calls) == 1, "Rest muss an der Börse geschlossen werden"
        adopted = await db.auto_trades.find_one({"symbol": SYM, "status": "open"})
        assert adopted is None, "kein lokaler Trade für bereinigten Rest"
        await _cleanup(db)
    _run(scenario())


def test_leftover_adopted_when_close_fails(db):
    async def scenario():
        await _cleanup(db)
        await _seed_closed_bot_trade(db)
        client = _FakeClient(close_ok=False)
        wd = _wd(db, client)
        result = await wd._adopt(SYM, _pos())
        assert result is not None
        assert result["strategy_name"] == "Rest nach Bot-Close"
        assert result["leftover"] is True
        assert result["manual_trade"] is False, \
            "Rest darf nicht als manueller Bitunix-Trade markiert werden"
        await _cleanup(db)
    _run(scenario())


def test_unknown_position_still_adopted_as_manual(db):
    async def scenario():
        await _cleanup(db)  # kein kürzlicher Bot-Close vorhanden
        client = _FakeClient(close_ok=True)
        wd = _wd(db, client)
        result = await wd._adopt(SYM, _pos())
        assert result is not None
        assert result["strategy_name"] == "Manuell (Bitunix)"
        assert result["manual_trade"] is True
        assert not client.close_calls, "manuelle Position darf NIE geschlossen werden"
        await _cleanup(db)
    _run(scenario())


def test_old_close_not_treated_as_leftover(db):
    async def scenario():
        await _cleanup(db)
        await _seed_closed_bot_trade(db, minutes_ago=90)  # > 30-min-Fenster
        client = _FakeClient(close_ok=True)
        wd = _wd(db, client)
        result = await wd._adopt(SYM, _pos())
        assert result is not None
        assert result["strategy_name"] == "Manuell (Bitunix)"
        assert not client.close_calls
        await _cleanup(db)
    _run(scenario())
