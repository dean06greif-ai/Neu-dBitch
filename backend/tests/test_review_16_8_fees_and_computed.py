"""Review 16.8: Regression checks against live preview backend.

Scope:
 - /api/settings enthält futures_taker_fee_pct (0.06) und futures_maker_fee_pct (0.02)
 - POST /api/settings (Admin) persistiert Änderungen (Reset auf 0.06/0.02 nach dem Test!)
 - /api/autotrade/trades computed-Felder: entry_fee, fees_total_est, notional_usdt,
   price_move_pct, pnl_pct_margin, exit_fee (closed) bzw. est_close_fee (open).

Nutzt REACT_APP_BACKEND_URL aus /app/frontend/.env; keine harten URLs.
"""
import math
import os
import time
from pathlib import Path

import pytest
import requests


def _load_backend_url() -> str:
    env_path = Path(__file__).resolve().parents[2] / "frontend" / ".env"
    for ln in env_path.read_text().splitlines():
        if ln.startswith("REACT_APP_BACKEND_URL="):
            return ln.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not found in frontend/.env")


BASE_URL = _load_backend_url()
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "Dean06Greif!/Admin")


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token(api):
    r = api.post(f"{BASE_URL}/api/auth/login",
                 json={"username": ADMIN_USER, "password": ADMIN_PASS}, timeout=30)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    tok = r.json().get("token")
    assert tok, "no token in response"
    return tok


@pytest.fixture(scope="session")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# --- /api/settings ---------------------------------------------------------

def test_settings_contains_default_futures_fees(api):
    r = api.get(f"{BASE_URL}/api/settings", timeout=30)
    assert r.status_code == 200, r.text[:200]
    data = r.json()
    assert "futures_taker_fee_pct" in data, "missing futures_taker_fee_pct"
    assert "futures_maker_fee_pct" in data, "missing futures_maker_fee_pct"
    # Defaults sollen 0.06/0.02 sein (aktueller Sollzustand des Systems)
    assert math.isclose(float(data["futures_taker_fee_pct"]), 0.06, abs_tol=1e-9), (
        f"expected taker=0.06 got {data['futures_taker_fee_pct']}"
    )
    assert math.isclose(float(data["futures_maker_fee_pct"]), 0.02, abs_tol=1e-9), (
        f"expected maker=0.02 got {data['futures_maker_fee_pct']}"
    )


def test_settings_post_persists_futures_fees_and_reset(api, auth_headers):
    # Snapshot originalen Wert
    r0 = api.get(f"{BASE_URL}/api/settings", timeout=30)
    assert r0.status_code == 200
    orig = r0.json()
    orig_taker = float(orig.get("futures_taker_fee_pct", 0.06))
    orig_maker = float(orig.get("futures_maker_fee_pct", 0.02))

    # Neue Werte setzen
    new_taker, new_maker = 0.055, 0.018
    r1 = api.post(f"{BASE_URL}/api/settings",
                  json={"futures_taker_fee_pct": new_taker,
                        "futures_maker_fee_pct": new_maker},
                  headers=auth_headers, timeout=30)
    assert r1.status_code in (200, 204), r1.text[:200]

    # Persistenz prüfen (GET)
    r2 = api.get(f"{BASE_URL}/api/settings", timeout=30)
    assert r2.status_code == 200
    d = r2.json()
    try:
        assert math.isclose(float(d["futures_taker_fee_pct"]), new_taker, abs_tol=1e-9)
        assert math.isclose(float(d["futures_maker_fee_pct"]), new_maker, abs_tol=1e-9)
    finally:
        # IMMER zurücksetzen (Produktivsystem!)
        api.post(f"{BASE_URL}/api/settings",
                 json={"futures_taker_fee_pct": orig_taker,
                       "futures_maker_fee_pct": orig_maker},
                 headers=auth_headers, timeout=30)

    # Verify Reset
    r3 = api.get(f"{BASE_URL}/api/settings", timeout=30)
    d3 = r3.json()
    assert math.isclose(float(d3["futures_taker_fee_pct"]), orig_taker, abs_tol=1e-9)
    assert math.isclose(float(d3["futures_maker_fee_pct"]), orig_maker, abs_tol=1e-9)


# --- /api/autotrade/trades computed-Felder --------------------------------

def _get_trades(api, limit=5):
    # Warm-up if backend cold - retry once with longer timeout
    try:
        r = api.get(f"{BASE_URL}/api/autotrade/trades", params={"limit": limit}, timeout=90)
    except requests.exceptions.ReadTimeout:
        time.sleep(3)
        r = api.get(f"{BASE_URL}/api/autotrade/trades", params={"limit": limit}, timeout=120)
    assert r.status_code == 200, r.text[:200]
    payload = r.json()
    # Response kann {"trades": [...]}, {"items": [...]} oder direkt Liste sein
    if isinstance(payload, list):
        return payload
    for key in ("trades", "items", "data", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def test_trades_endpoint_returns_data(api):
    trades = _get_trades(api, limit=5)
    if not trades:
        pytest.skip("keine Trades im System – computed-Felder nicht prüfbar")
    assert isinstance(trades, list)
    assert len(trades) >= 1


def test_trades_have_expected_computed_fields(api):
    trades = _get_trades(api, limit=5)
    if not trades:
        pytest.skip("keine Trades im System")

    required_all = {"entry_fee", "fees_total_est", "notional_usdt",
                    "price_move_pct", "pnl_pct_margin"}
    for t in trades:
        c = t.get("computed") or {}
        missing = required_all - set(c.keys())
        assert not missing, f"trade {t.get('id')} fehlt computed-Felder: {missing}; hat: {list(c.keys())}"

        status = (t.get("status") or "").lower()
        if status == "open":
            assert "est_close_fee" in c, f"open trade {t.get('id')} ohne est_close_fee"
        elif status == "closed":
            assert "exit_fee" in c, f"closed trade {t.get('id')} ohne exit_fee"


def test_entry_fee_matches_formula(api):
    trades = _get_trades(api, limit=5)
    if not trades:
        pytest.skip("keine Trades im System")
    # Prüfe an einem Trade: entry_fee == entry * qty * fee_percent / 100
    checked = 0
    for t in trades:
        c = t.get("computed") or {}
        entry = t.get("entry")
        qty = t.get("qty") or t.get("qty_remaining")
        fee_pct = t.get("fee_percent")
        if entry is None or qty is None or fee_pct is None:
            continue
        expected = float(entry) * float(qty) * float(fee_pct) / 100.0
        # kleine Toleranz für Rundung / andere Berechnungsbasis
        assert math.isclose(float(c["entry_fee"]), expected, rel_tol=0.02, abs_tol=1e-4), (
            f"entry_fee für {t.get('id')}: expected≈{expected} got {c['entry_fee']}"
        )
        checked += 1
        if checked >= 2:
            break
    if checked == 0:
        pytest.skip("Trades enthalten keine ausreichenden Felder für Formel-Check")
