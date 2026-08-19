"""Tests for iteration_37: providers_health grouping (rate_limited_grouped, skipped_grouped).

Covers:
1. In-process unit test of ai_providers.health_status() grouping logic
2. Public API /api/ai/status returns new keys with backward-compatible legacy keys
"""
import os
import sys
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://daytrader-stable-1.preview.emergentagent.com").rstrip("/")
sys.path.insert(0, "/app/backend")


# ---------- Unit test: grouping ----------
def test_group_by_provider_rate_limited_and_skipped():
    from services import ai_providers as ap
    # Clean state
    ap._health.clear()
    ap._recent_failures.clear()

    ap.record_result("cerebras", "gpt-oss-120b", "rate_limited", "429", role="analyst")
    ap.record_result("cerebras", "zai-glm-4.7", "rate_limited", "429", role="trade_manager")
    ap.record_result("groq", "openai/gpt-oss-20b", "skipped_too_large",
                     "Prompt ~14215 Tokens > Budget 5000", role="research_analyst")

    st = ap.health_status()

    # Backward-compat keys still there
    for k in ("rate_limited", "skipped_too_large", "errors", "active_fallbacks",
              "rate_limited_grouped", "skipped_grouped"):
        assert k in st, f"missing key {k}"

    rlg = st["rate_limited_grouped"]
    assert len(rlg) == 1, f"expected 1 provider group, got {rlg}"
    g = rlg[0]
    assert g["provider"] == "cerebras"
    assert set(g["models"]) == {"gpt-oss-120b", "zai-glm-4.7"}
    assert set(g["roles"]) >= {"analyst", "trade_manager"}
    assert g["count"] == 2

    skg = st["skipped_grouped"]
    assert len(skg) == 1
    sg = skg[0]
    assert sg["provider"] == "groq"
    assert "research_analyst" in sg["roles"]
    assert "openai/gpt-oss-20b" in sg["models"]


# ---------- API test ----------
@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": "Admin", "password": "Dean06Greif!/Admin"},
                      timeout=15)
    if r.status_code != 200:
        pytest.skip(f"login failed: {r.status_code} {r.text[:200]}")
    return r.json().get("token") or r.json().get("access_token")


def test_ai_status_has_grouped_keys(token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(f"{BASE_URL}/api/ai/status", headers=headers, timeout=20)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    ph = data.get("providers_health") or data
    for k in ("rate_limited_grouped", "skipped_grouped",
              "rate_limited", "skipped_too_large", "errors", "active_fallbacks"):
        assert k in ph, f"providers_health missing '{k}' – keys={list(ph.keys())[:20]}"
    assert isinstance(ph["rate_limited_grouped"], list)
    assert isinstance(ph["skipped_grouped"], list)
