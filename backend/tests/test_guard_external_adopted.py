"""Guard-Regression + HTTP-Semantik für POST /api/autotrade/trade/{id}/action.

1) 404 wenn trade_id nicht existiert
2) 409 wenn Trade external_adopted=True (direkt in Mongo eingefügt, danach aufgeräumt)
"""
import os
import uuid
import asyncio
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASSWORD = "Dean06Greif!/Admin"


@pytest.fixture(scope="module")
def hdr():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
                      timeout=30)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("token")
    assert tok
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _mongo_db():
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "crypto_scanner")
    return AsyncIOMotorClient(mongo_url)[db_name]


def test_404_when_trade_id_not_found(hdr):
    """Nicht-existente trade_id -> 404 (nicht 502/500)."""
    fake_id = f"nonexistent-{uuid.uuid4()}"
    r = requests.post(f"{BASE_URL}/api/autotrade/trade/{fake_id}/action",
                      json={"action": "partial_close", "value": 30,
                            "reason": "test 404"},
                      headers=hdr, timeout=30)
    print(f"[404-CHECK] status={r.status_code} body={r.text[:200]}")
    assert r.status_code == 404, f"Erwartet 404, bekam {r.status_code}: {r.text[:300]}"


def test_409_when_external_adopted():
    """Trade mit external_adopted=True -> 409 blocked (nicht 502, nicht 200)."""
    # Login
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
                      timeout=30)
    assert r.status_code == 200
    tok = r.json()["token"]
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    trade_id = f"TEST_EXTERNAL_{uuid.uuid4()}"

    async def _insert():
        db = _mongo_db()
        await db.auto_trades.insert_one({
            "id": trade_id,
            "symbol": "TESTUSDT",
            "side": "LONG",
            "mode": "paper",
            "status": "open",
            "external_adopted": True,
            "manual_trade": False,
            "qty": 1.0,
            "qty_remaining": 1.0,
            "entry_price": 100.0,
            "opened_at": "2026-01-01T00:00:00Z",
            "strategy_id": "external",
        })

    async def _cleanup():
        db = _mongo_db()
        await db.auto_trades.delete_one({"id": trade_id})

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_insert())
        r = requests.post(f"{BASE_URL}/api/autotrade/trade/{trade_id}/action",
                          json={"action": "partial_close", "value": 30,
                                "reason": "guard-test external"},
                          headers=headers, timeout=30)
        print(f"[EXT-CHECK] status={r.status_code} body={r.text[:300]}")
        assert r.status_code == 409, \
            f"Erwartet 409 (blocked), bekam {r.status_code}: {r.text[:400]}"
        detail = (r.json().get("detail") or "").lower()
        assert "extern" in detail or "bitunix" in detail, \
            f"Detail sollte auf external/bitunix hinweisen: {detail}"
    finally:
        loop.run_until_complete(_cleanup())
        loop.close()
