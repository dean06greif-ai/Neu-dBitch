"""API-level pytest for the new /api/ai/guard-stats endpoint (Guard-Cockpit)."""
import os
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")

EXPECTED_KEYS = ["weekend", "stale", "regime", "correlation", "direction",
                 "cluster", "playbook", "master", "fee", "other"]


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def test_health(api):
    r = api.get(f"{BASE_URL}/api/health", timeout=15)
    assert r.status_code == 200
    assert r.json().get("status") == "alive"


def test_guard_stats_public_no_auth(api):
    # Kein Authorization-Header -> muss trotzdem 200 liefern
    r = api.get(f"{BASE_URL}/api/ai/guard-stats?days=7", timeout=15)
    assert r.status_code == 200


def test_guard_stats_default_shape(api):
    r = api.get(f"{BASE_URL}/api/ai/guard-stats?days=7", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["days"] == 7
    assert isinstance(d["total_blocked"], int)
    guards = d["guards"]
    assert isinstance(guards, list) and len(guards) == 10
    keys = [g["key"] for g in guards]
    assert keys == EXPECTED_KEYS  # exact order per router.ai
    for g in guards:
        assert set(g.keys()) >= {"key", "label", "count", "collection", "last"}
        assert isinstance(g["count"], int)
        assert isinstance(g["collection"], int)
    rs = d["regime_shadow"]
    assert rs["enabled"] is False
    assert rs["list"] == ["range_ruhig"]
    assert isinstance(rs["would_block_live"], int)


def test_guard_stats_days_clamp_high(api):
    r = api.get(f"{BASE_URL}/api/ai/guard-stats?days=999", timeout=15)
    assert r.status_code == 200
    assert r.json()["days"] == 90


def test_guard_stats_days_clamp_low(api):
    r = api.get(f"{BASE_URL}/api/ai/guard-stats?days=0", timeout=15)
    assert r.status_code == 200
    assert r.json()["days"] == 1


def test_fee_guard_stats_regression(api):
    """Bestehender fee-guard-Endpoint muss weiter funktionieren."""
    r = api.get(f"{BASE_URL}/api/ai/fee-guard/stats?days=7", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["days"] == 7
    assert "blocked_total" in d and "blocked_collection" in d
    assert "est_fees_saved_usdt" in d and "recent" in d
