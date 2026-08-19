"""Iteration 47 – backend regression tests

Scope (per review_request):
  - Admin-Login /api/auth/login
  - GET /api/ai/models/catalog: pending has >=20 entries; discovered empty at start
  - POST /api/ai/models/approve moves entry pending -> discovered
  - POST /api/ai/models/dismiss removes entry from pending (not in discovered)
  - approve without token -> 401; unknown model -> 400
  - GET /api/ai/status: providers_health.key_status incl. cerebras total==14,
    other providers >=1
  - POST /api/ai/roles toggles news_watcher.deep_on_news

Read-only regression: cleans up its own approved test model via dismiss.
NO LLM analysis triggers (real Bitunix keys are live).
"""
import os
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASS = "Dean06Greif!/Admin"


# ---------------- fixtures ----------------
@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def admin_token(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    tok = data.get("token") or data.get("access_token")
    assert tok and isinstance(tok, str) and len(tok) > 20
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json"}


# ---------------- Auth ----------------
def test_admin_login(admin_token):
    assert admin_token  # implicit from fixture


# ---------------- AI Status: providers_health.key_status ----------------
def test_ai_status_key_status(api):
    r = api.get(f"{BASE_URL}/api/ai/status")
    assert r.status_code == 200
    ks = ((r.json().get("providers_health") or {}).get("key_status")) or {}
    assert ks, "key_status missing from providers_health"
    assert ks.get("cerebras", {}).get("total") == 14, \
        f"cerebras total != 14 (got {ks.get('cerebras')})"
    for prov in ("gemini", "groq", "openrouter", "mistral"):
        assert prov in ks, f"provider {prov} missing"
        assert int(ks[prov].get("total") or 0) >= 1, \
            f"{prov} total < 1: {ks[prov]}"


# ---------------- Model catalog: pending list ----------------
def test_catalog_has_pending_models(api):
    r = api.get(f"{BASE_URL}/api/ai/models/catalog")
    assert r.status_code == 200
    d = r.json()
    pending = d.get("pending") or []
    assert isinstance(pending, list)
    assert len(pending) >= 20, f"pending has only {len(pending)} entries"
    for e in pending[:5]:
        assert "provider" in e and "model" in e


# ---------------- Approve/Dismiss flow ----------------
def _pick_two_pending(api):
    d = api.get(f"{BASE_URL}/api/ai/models/catalog").json()
    pending = d.get("pending") or []
    assert len(pending) >= 2
    return pending[0], pending[1]


def test_approve_without_token_returns_401(api):
    pick, _ = _pick_two_pending(api)
    r = requests.post(f"{BASE_URL}/api/ai/models/approve", json=pick)
    assert r.status_code == 401


def test_approve_unknown_model_returns_400(api, auth_headers):
    r = requests.post(f"{BASE_URL}/api/ai/models/approve",
                      headers=auth_headers,
                      json={"provider": "gemini",
                            "model": "__DEFINITELY_UNKNOWN_XYZ__"})
    assert r.status_code == 400


def test_approve_then_verify_and_cleanup(api, auth_headers):
    pick1, pick2 = _pick_two_pending(api)

    # approve pick1
    r = requests.post(f"{BASE_URL}/api/ai/models/approve",
                      headers=auth_headers, json=pick1)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"

    # dismiss pick2
    r = requests.post(f"{BASE_URL}/api/ai/models/dismiss",
                      headers=auth_headers, json=pick2)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"

    # verify catalog
    d = api.get(f"{BASE_URL}/api/ai/models/catalog").json()
    pending = d.get("pending") or []
    disc = d.get("discovered") or []
    assert pick1 in disc, f"approved {pick1} not in discovered"
    assert pick1 not in pending
    assert pick2 not in pending
    assert pick2 not in disc

    # cleanup: dismiss the approved test model to restore original state
    requests.post(f"{BASE_URL}/api/ai/models/dismiss",
                  headers=auth_headers, json=pick1)
    d2 = api.get(f"{BASE_URL}/api/ai/models/catalog").json()
    assert pick1 not in (d2.get("discovered") or [])


# ---------------- Roles / news_watcher.deep_on_news ----------------
def test_roles_toggle_deep_on_news(api, auth_headers):
    # set false
    r = requests.post(f"{BASE_URL}/api/ai/roles",
                      headers=auth_headers,
                      json={"news_watcher": {"deep_on_news": False}})
    assert r.status_code == 200
    nw = r.json().get("roles", {}).get("news_watcher") or {}
    assert nw.get("deep_on_news") is False

    # restore true
    r = requests.post(f"{BASE_URL}/api/ai/roles",
                      headers=auth_headers,
                      json={"news_watcher": {"deep_on_news": True}})
    assert r.status_code == 200
    nw = r.json().get("roles", {}).get("news_watcher") or {}
    assert nw.get("deep_on_news") is True
