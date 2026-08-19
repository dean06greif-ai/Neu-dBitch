"""E2E tests for maker-mode API (iter 48) - toggle, wait clamp, suspend/resume, trade explain."""
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback: read from frontend .env
    with open("/app/frontend/.env") as fh:
        for line in fh:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok, f"no token in response: {r.json()}"
    return tok


@pytest.fixture(scope="module")
def h(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_cfg(h):
    r = requests.get(f"{BASE_URL}/api/ai/status", headers=h, timeout=15)
    assert r.status_code == 200, r.text
    return r.json().get("config") or r.json()


def _post_cfg(h, payload):
    r = requests.post(f"{BASE_URL}/api/ai/config", headers=h, json=payload, timeout=15)
    assert r.status_code == 200, f"POST /api/ai/config {payload} -> {r.status_code} {r.text}"
    return r.json()


def test_ai_status_has_maker_fields(h):
    cfg = _get_cfg(h)
    assert "maker_mode" in cfg
    assert "maker_wait_sec" in cfg


def test_toggle_maker_mode_on(h):
    _post_cfg(h, {"maker_mode": True, "maker_wait_sec": 60})
    cfg = _get_cfg(h)
    assert cfg.get("maker_mode") is True
    assert int(cfg.get("maker_wait_sec")) == 60


def test_maker_wait_clamped(h):
    _post_cfg(h, {"maker_wait_sec": 9999})
    cfg = _get_cfg(h)
    assert int(cfg.get("maker_wait_sec")) == 300, f"expected clamp to 300, got {cfg.get('maker_wait_sec')}"


def test_maker_suspend_hours_sets_iso(h):
    before = datetime.now(timezone.utc).timestamp()
    _post_cfg(h, {"maker_suspend_hours": 5})
    cfg = _get_cfg(h)
    until = cfg.get("maker_suspended_until")
    assert until, f"maker_suspended_until missing: {cfg}"
    # parse ISO
    ts = datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp()
    delta_h = (ts - before) / 3600.0
    assert 4.5 < delta_h < 5.5, f"expected ~5h, got {delta_h}"


def test_maker_resume_clears_suspend(h):
    _post_cfg(h, {"maker_suspended_until": None})
    cfg = _get_cfg(h)
    assert cfg.get("maker_suspended_until") in (None, ""), f"still suspended: {cfg.get('maker_suspended_until')}"


def test_reset_maker_config(h):
    _post_cfg(h, {"maker_mode": False, "maker_wait_sec": 45, "maker_suspended_until": None})
    cfg = _get_cfg(h)
    assert cfg.get("maker_mode") is False
    assert int(cfg.get("maker_wait_sec")) == 45
    assert cfg.get("maker_suspended_until") in (None, "")


# --- ai_knowledge / _tuning_guard forbidden keys ---
def test_ai_knowledge_forbids_maker_keys():
    from services import ai_knowledge
    res = ai_knowledge.validate_changes({"maker_mode": True}, scope="engine")
    # Expect rejection -> either raises, returns error dict/list, or False
    txt = str(res).lower()
    assert ("forbidden" in txt or "not allowed" in txt or "verboten" in txt or "abgelehnt" in txt
            or res is False or (isinstance(res, dict) and (res.get("ok") is False or res.get("errors")))), \
        f"maker_mode should be forbidden, got {res}"


def test_ai_knowledge_accepts_suspend_hours():
    from services import ai_knowledge
    res = ai_knowledge.validate_changes({"maker_suspend_hours": 12}, scope="engine")
    txt = str(res).lower()
    # should NOT contain forbidden
    assert "forbidden" not in txt and "verboten" not in txt, f"suspend_hours should be allowed: {res}"


def test_tuning_guard_maker():
    from services import ai_engine
    # Try to find guard func
    guard = getattr(ai_engine, "_tuning_guard", None) or getattr(ai_engine, "tuning_guard", None)
    if guard is None:
        pytest.skip("no _tuning_guard exported")
    r_ok = guard({"maker_suspend_hours": 12})
    r_bad_hi = guard({"maker_suspend_hours": 100})
    r_bad_lo = guard({"maker_suspend_hours": 0})
    assert (r_ok == "" or r_ok is None or (isinstance(r_ok, str) and len(r_ok) == 0)), f"12h should be autonomous, got {r_ok!r}"
    assert (isinstance(r_bad_hi, str) and len(r_bad_hi) > 0) or bool(r_bad_hi), f"100h should need confirmation: {r_bad_hi!r}"
    assert (isinstance(r_bad_lo, str) and len(r_bad_lo) > 0) or bool(r_bad_lo), f"0h should need confirmation: {r_bad_lo!r}"


# --- Trade Explain ---
def test_trade_explain_has_order_kind(h):
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "crypto_scanner")
    cli = MongoClient(mongo_url)
    db = cli[db_name]
    col = db["auto_trades"]

    # Find an existing trade
    existing = col.find_one({}, sort=[("_id", -1)])
    seeded_id = None
    if not existing:
        seeded_id = f"TEST_MAKER_{uuid.uuid4().hex[:8]}"
        col.insert_one({
            "id": seeded_id,
            "symbol": "BTCUSDT",
            "side": "long",
            "status": "closed",
            "order_kind": "maker",
            "entry_fee_percent": 0.02,
            "entry_price": 50000.0,
            "exit_price": 50500.0,
            "size": 0.001,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        trade_id = seeded_id
    else:
        trade_id = existing.get("id") or str(existing.get("_id"))

    try:
        # Try common explain endpoints
        candidates = [
            f"{BASE_URL}/api/autotrade/trade/{trade_id}/explain",
            f"{BASE_URL}/api/autotrade/trades/{trade_id}/explain",
        ]
        r = None
        for u in candidates:
            r = requests.get(u, headers=h, timeout=15)
            if r.status_code == 200:
                break
        assert r is not None and r.status_code == 200, f"explain endpoint not reachable: {[c for c in candidates]} last={r.status_code if r else 'N/A'} {r.text if r else ''}"
        data = r.json()
        state = data.get("state") or data.get("trade_state") or data
        assert "order_kind" in state, f"order_kind missing in state: {list(state.keys())[:20]}"
        assert "entry_fee_percent" in state, f"entry_fee_percent missing in state: {list(state.keys())[:20]}"
    finally:
        if seeded_id:
            col.delete_one({"id": seeded_id})


def test_ai_lab_trash(h):
    r = requests.get(f"{BASE_URL}/api/ai/lab/trash", headers=h, timeout=15)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
