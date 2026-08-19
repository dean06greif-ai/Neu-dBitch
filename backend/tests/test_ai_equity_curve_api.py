"""API regression tests for /api/ai/equity-curve (iter42).

Public URL via REACT_APP_BACKEND_URL. No auth needed. Dev DB is empty ->
all responses return empty points and zeroed summary but valid shape.
"""
import os
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
URL = f"{BASE_URL}/api/ai/equity-curve"

EXPECTED_SUMMARY_KEYS = {
    "trades", "wins", "winrate", "total_pnl",
    "peak_equity", "max_drawdown", "fees",
}


def _get(params):
    r = requests.get(URL, params=params, timeout=15)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def test_shape_default_empty_db():
    data = _get({"days": 0, "mode": "all"})
    assert set(data.keys()) >= {"days", "mode", "points", "summary"}
    assert data["days"] == 0
    assert data["mode"] == "all"
    assert data["points"] == []
    assert set(data["summary"].keys()) == EXPECTED_SUMMARY_KEYS
    assert data["summary"]["trades"] == 0
    assert data["summary"]["total_pnl"] == 0.0


@pytest.mark.parametrize("mode", ["all", "live", "collection"])
def test_valid_modes(mode):
    data = _get({"days": 0, "mode": mode})
    assert data["mode"] == mode
    assert data["points"] == []


def test_mode_fallback_invalid_to_all():
    data = _get({"days": 0, "mode": "quatsch"})
    assert data["mode"] == "all"


def test_days_clamp_high():
    data = _get({"days": 9999, "mode": "all"})
    assert data["days"] == 365


def test_days_clamp_low_negative():
    data = _get({"days": -5, "mode": "all"})
    # clamp min 0
    assert data["days"] == 0


def test_days_arbitrary_within_range():
    data = _get({"days": 30, "mode": "all"})
    assert data["days"] == 30
