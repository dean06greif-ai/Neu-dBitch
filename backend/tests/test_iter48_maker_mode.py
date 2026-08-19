"""Iteration 48 – Maker-Order-Modus:
Unkritische KI-Entries als Post-Only-Limit (Maker-Fee ~0,02% statt Taker
~0,06%). Trader schaltet den Modus im Setup an/aus; der KI-Trader darf ihn
bei schlechter Performance bis 72h aussetzen (Wieder-Aktivieren nur durch
den Trader). Nicht gefüllte Limits fallen automatisch auf Market zurück.
"""
import asyncio
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")


# ---------------- parse_order_fill (rein) ----------------
class TestParseOrderFill:
    def test_filled(self):
        from services.bitunix_trade import parse_order_fill
        r = parse_order_fill({"code": 0, "data": {"status": "FILLED",
                                                  "dealAmount": "0.5",
                                                  "avgPrice": "101.5"}})
        assert r == {"status": "FILLED", "filled_qty": 0.5, "avg_price": 101.5}

    def test_partial_and_alt_keys(self):
        from services.bitunix_trade import parse_order_fill
        r = parse_order_fill({"code": 0, "data": {"orderStatus": "part_filled",
                                                  "tradeQty": 0.2, "price": 99}})
        assert r["status"] == "PART_FILLED"
        assert r["filled_qty"] == 0.2 and r["avg_price"] == 99

    def test_garbage_safe(self):
        from services.bitunix_trade import parse_order_fill
        assert parse_order_fill(None)["filled_qty"] == 0.0
        assert parse_order_fill({"code": 1})["status"] == ""


# ---------------- _maker_entry mit Fake-Börse ----------------
class _FakeMakerClient:
    """Simuliert Bitunix für den Maker-Flow."""
    def __init__(self, mode="fill", place_code=0):
        self.mode = mode          # fill | never_fill | partial | reject_postonly
        self.place_code = place_code
        self.cancel_calls = []
        self.place_calls = []
        self._polls = 0

    async def place_order(self, symbol, side, qty, order_type="MARKET",
                          price=None, tp_price=None, sl_price=None,
                          reduce_only=False, effect=None):
        self.place_calls.append({"order_type": order_type, "price": price,
                                 "effect": effect, "qty": qty})
        if self.place_code != 0:
            return {"code": self.place_code, "msg": "rejected"}
        return {"code": 0, "data": {"orderId": "ord-1"}}

    async def get_order_detail(self, order_id):
        self._polls += 1
        if self.mode == "fill":
            return {"code": 0, "data": {"status": "FILLED", "dealAmount": 0.4,
                                        "avgPrice": 100.1}}
        if self.mode == "partial":
            return {"code": 0, "data": {"status": "PART_FILLED",
                                        "dealAmount": 0.15, "avgPrice": 100.0}}
        if self.mode == "reject_postonly":
            return {"code": 0, "data": {"status": "CANCELED", "dealAmount": 0}}
        return {"code": 0, "data": {"status": "NEW", "dealAmount": 0}}

    async def cancel_orders(self, symbol, order_ids):
        self.cancel_calls.append(list(order_ids))
        return {"code": 0}

    async def get_pending_orders(self, symbol):
        return {"code": 0, "data": []}


def _mgr(client):
    from services.bitunix_trade import AutoTradeManager
    return AutoTradeManager(client)


def test_maker_entry_filled():
    client = _FakeMakerClient("fill")
    r = asyncio.run(_mgr(client)._maker_entry(
        "BTCUSDT", "LONG", 0.4, 100.0, 10, tpf=102.0, sl=99.0))
    assert r["kind"] == "maker"
    assert r["qty"] == 0.4 and r["entry"] == 100.1
    p = client.place_calls[0]
    assert p["order_type"] == "LIMIT" and p["effect"] == "POST_ONLY"
    assert p["price"] < 100.0, "LONG-Limit muss passiv UNTER dem Mark liegen"


def test_maker_entry_short_price_above_mark():
    client = _FakeMakerClient("fill")
    asyncio.run(_mgr(client)._maker_entry(
        "BTCUSDT", "SHORT", 0.4, 100.0, 10, tpf=98.0, sl=101.0))
    assert client.place_calls[0]["price"] > 100.0


def test_maker_entry_timeout_falls_back_and_cancels():
    client = _FakeMakerClient("never_fill")
    r = asyncio.run(_mgr(client)._maker_entry(
        "BTCUSDT", "LONG", 0.4, 100.0, 10, tpf=102.0, sl=99.0))
    assert r["kind"] == "fallback"
    assert client.cancel_calls == [["ord-1"]], "Timeout muss die Limit-Order stornieren"


def test_maker_entry_partial_keeps_fill():
    client = _FakeMakerClient("partial")
    r = asyncio.run(_mgr(client)._maker_entry(
        "BTCUSDT", "LONG", 0.4, 100.0, 10, tpf=102.0, sl=99.0))
    assert r["kind"] == "maker_partial"
    assert r["qty"] == 0.15
    assert client.cancel_calls, "Rest der Teil-Fill-Order muss storniert werden"


def test_maker_entry_postonly_rejected_falls_back():
    client = _FakeMakerClient("reject_postonly")
    r = asyncio.run(_mgr(client)._maker_entry(
        "BTCUSDT", "LONG", 0.4, 100.0, 10, tpf=102.0, sl=99.0))
    assert r["kind"] == "fallback"


def test_maker_entry_place_rejected_falls_back():
    client = _FakeMakerClient("fill", place_code=30001)
    r = asyncio.run(_mgr(client)._maker_entry(
        "BTCUSDT", "LONG", 0.4, 100.0, 10, tpf=102.0, sl=99.0))
    assert r["kind"] == "fallback"


# ---------------- Engine: Aussetzung + Autonomie-Leitplanken ----------------
class TestEngineMakerRules:
    def test_suspend_hours_sets_and_clears(self):
        from services.ai_engine import ai_engine
        from datetime import datetime, timezone, timedelta
        eng = ai_engine
        eng.config["maker_suspended_until"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert eng._maker_suspended() is True
        eng.config["maker_suspended_until"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        assert eng._maker_suspended() is False, "abgelaufene Aussetzung muss auto-enden"
        assert eng.config["maker_suspended_until"] is None

    def test_tuning_guard_maker(self):
        from services.ai_engine import ai_engine
        assert ai_engine._tuning_guard({"maker_suspend_hours": 12}) == ""
        assert "Trader" in ai_engine._tuning_guard({"maker_suspend_hours": 0})
        assert "72" in ai_engine._tuning_guard({"maker_suspend_hours": 100})

    def test_forbidden_keys_for_ki(self):
        from services.ai_knowledge import validate_changes
        valid, rejected = validate_changes(
            {"maker_mode": True, "maker_wait_sec": 60,
             "maker_suspended_until": None, "maker_suspend_hours": 6},
            scope="engine")
        assert {"maker_mode", "maker_wait_sec", "maker_suspended_until"} <= set(rejected)
        assert valid.get("maker_suspend_hours") == 6


# ---------------- API: Setup-Toggle + Wieder-Aktivieren ----------------
@pytest.fixture(scope="module")
def auth_headers():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"),
                            "password": os.environ.get("ADMIN_PASSWORD", "admin")},
                      timeout=15)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


class TestMakerApi:
    def test_maker_stats_math(self, auth_headers):
        from pymongo import MongoClient
        db = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))[
            os.environ.get("DB_NAME", "crypto_scanner")]
        db.auto_trades.delete_many({"symbol": "MKSTATUSDT"})
        db.auto_trades.insert_many([
            {"id": "mks-1", "symbol": "MKSTATUSDT", "mode": "live", "status": "closed",
             "order_kind": "maker", "entry": 100, "qty": 0.5,
             "fee_percent": 0.06, "entry_fee_percent": 0.02},
            {"id": "mks-2", "symbol": "MKSTATUSDT", "mode": "live", "status": "closed",
             "order_kind": "taker_fallback", "entry": 50, "qty": 1,
             "fee_percent": 0.06, "entry_fee_percent": 0.06}])
        try:
            r = requests.get(f"{BASE_URL}/api/ai/maker-stats", timeout=15)
            assert r.status_code == 200
            d = r.json()
            # 100 * 0.5 * (0.06-0.02)/100 = 0.02 $ Ersparnis
            assert d["live"]["saved_usdt"] >= 0.02
            assert d["live"]["trades"] >= 1
            assert d["fallback_trades"] >= 1
        finally:
            db.auto_trades.delete_many({"symbol": "MKSTATUSDT"})

    def test_toggle_wait_and_resume(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"maker_mode": True, "maker_wait_sec": 60}, timeout=15)
        assert r.status_code == 200
        cfg = r.json().get("config", {})
        assert cfg.get("maker_mode") is True and cfg.get("maker_wait_sec") == 60
        # KI setzt aus (virtueller Key) -> suspended_until gesetzt
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"maker_suspend_hours": 5}, timeout=15)
        assert r.json()["config"].get("maker_suspended_until")
        # Trader aktiviert wieder
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"maker_suspended_until": None}, timeout=15)
        assert r.json()["config"].get("maker_suspended_until") is None
        # Wartezeit wird geklemmt (10..300)
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"maker_wait_sec": 9999}, timeout=15)
        assert r.json()["config"].get("maker_wait_sec") == 300
        # zurück auf Default (aus)
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"maker_mode": False, "maker_wait_sec": 45}, timeout=15)
        assert r.json()["config"].get("maker_mode") is False
