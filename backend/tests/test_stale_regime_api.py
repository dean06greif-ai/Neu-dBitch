"""API-level test for stale-price + regime-block config knobs (iteration_40).

Runs end-to-end against REACT_APP_BACKEND_URL:
  - login as Admin -> Bearer token
  - GET /api/ai/status  (defaults)
  - POST /api/ai/config with clamping (999 -> 120, -5 -> 0, back to 10)
  - regime_block_enabled true/false round-trip
  - regime_block_list mutate + reset
Final state MUST be: stale=10, regime_enabled=false, list=['range_ruhig'], fee_guard_atr_mult=4.0
"""
import os
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    tok = data.get("token") or data.get("access_token")
    assert tok, f"no token in response: {data}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _cfg():
    r = requests.get(f"{BASE}/api/ai/status", timeout=15)
    assert r.status_code == 200
    return r.json().get("config", {})


def test_health():
    r = requests.get(f"{BASE}/api/health", timeout=30)
    assert r.status_code == 200
    assert r.json().get("status") == "alive"


def test_defaults_present():
    c = _cfg()
    assert c["stale_price_max_min"] == 10
    assert c["regime_block_enabled"] is False
    assert c["regime_block_list"] == ["range_ruhig"]
    assert float(c["fee_guard_atr_mult"]) == 4.0


def test_stale_price_clamping(auth_headers):
    # 999 -> 120
    r = requests.post(f"{BASE}/api/ai/config", headers=auth_headers,
                      json={"stale_price_max_min": 999}, timeout=15)
    assert r.status_code == 200, r.text
    assert float(_cfg()["stale_price_max_min"]) == 120.0
    # -5 -> 0
    r = requests.post(f"{BASE}/api/ai/config", headers=auth_headers,
                      json={"stale_price_max_min": -5}, timeout=15)
    assert r.status_code == 200, r.text
    assert float(_cfg()["stale_price_max_min"]) == 0.0
    # back to 10
    r = requests.post(f"{BASE}/api/ai/config", headers=auth_headers,
                      json={"stale_price_max_min": 10}, timeout=15)
    assert r.status_code == 200, r.text
    assert float(_cfg()["stale_price_max_min"]) == 10.0


def test_regime_block_roundtrip(auth_headers):
    # enable
    r = requests.post(f"{BASE}/api/ai/config", headers=auth_headers,
                      json={"regime_block_enabled": True}, timeout=15)
    assert r.status_code == 200, r.text
    assert _cfg()["regime_block_enabled"] is True
    # list mutate
    r = requests.post(f"{BASE}/api/ai/config", headers=auth_headers,
                      json={"regime_block_list": ["range_ruhig", "drift_ruhig"]}, timeout=15)
    assert r.status_code == 200, r.text
    assert _cfg()["regime_block_list"] == ["range_ruhig", "drift_ruhig"]
    # reset
    r = requests.post(f"{BASE}/api/ai/config", headers=auth_headers,
                      json={"regime_block_enabled": False,
                            "regime_block_list": ["range_ruhig"]}, timeout=15)
    assert r.status_code == 200, r.text
    c = _cfg()
    assert c["regime_block_enabled"] is False
    assert c["regime_block_list"] == ["range_ruhig"]


def test_final_state():
    c = _cfg()
    assert float(c["stale_price_max_min"]) == 10.0
    assert c["regime_block_enabled"] is False
    assert c["regime_block_list"] == ["range_ruhig"]
    assert float(c["fee_guard_atr_mult"]) == 4.0
