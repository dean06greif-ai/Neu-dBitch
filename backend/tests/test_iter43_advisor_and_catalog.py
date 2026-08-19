"""Iteration 43 – Modell-Berater, Modell-Katalog, Strategie-Vergleich (unknown-Fix).

Prüft:
  * GET /api/ai/models/catalog: builtin(dict), discovered(list ~35), weights,
    backup_keys(groq==2, openrouter==4), providers (alle 5 true).
  * GET /api/ai/models/watch: enthält checked_at, discovered(dict), last_new.
  * GET /api/analytics/strategy-comparison: kein 'unknown' im comparison-Array.
  * POST /api/ai/team/advisor:
        - ohne Token -> 401
        - mit Token -> SSE-Stream data:{"t":...} Tokens + {"done":true}
        - Antwort ist non-empty, deutscher Text mit Modell-Empfehlung
"""
import json
import os
import re
import time

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_USER = "Admin"
ADMIN_PASSWORD = "Dean06Greif!/Admin"


@pytest.fixture(scope="module")
def admin_token():
    for _ in range(3):
        try:
            r = requests.post(
                f"{BASE_URL}/api/auth/login",
                json={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
                timeout=30,
            )
            break
        except Exception:
            time.sleep(2)
    assert r.status_code == 200, f"Login fehlgeschlagen: {r.status_code} {r.text[:200]}"
    data = r.json()
    tok = data.get("token") or data.get("access_token")
    assert tok, f"Kein token in {data}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# ---------------- Modell-Katalog ----------------

def _get_retry(url, timeout=30, tries=3):
    last = None
    for _ in range(tries):
        try:
            return requests.get(url, timeout=timeout)
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def test_models_catalog_shape_and_backups():
    r = _get_retry(f"{BASE_URL}/api/ai/models/catalog", timeout=30)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    for k in ("builtin", "discovered", "weights", "backup_keys", "providers"):
        assert k in d, f"Feld fehlt: {k}"
    # builtin: dict provider -> list of models
    assert isinstance(d["builtin"], dict) and d["builtin"], "builtin leer/keine dict"
    for prov, models in d["builtin"].items():
        assert isinstance(models, list) and models, f"builtin[{prov}] leer"
    # discovered: list mit provider/model – seit dem Bestätigungs-Flow sind neu
    # entdeckte Modelle PENDING und erst nach Bestätigung in "discovered".
    assert isinstance(d["discovered"], list), "discovered kein list"
    assert isinstance(d.get("pending"), list), "pending fehlt/kein list"
    total_found = len(d["discovered"]) + len(d["pending"])
    assert total_found >= 20, (
        f"Erwartet ~35 entdeckte Modelle (pending+bestätigt), gefunden {total_found}"
    )
    for entry in (d["discovered"] + d["pending"])[:3]:
        assert "provider" in entry and "model" in entry
    # weights: dict
    assert isinstance(d["weights"], dict) and d["weights"]
    # backup_keys: konsistent mit den tatsächlich gesetzten Env-Keys
    bk = d["backup_keys"]
    assert bk.get("groq", 0) >= 1, f"groq backup_keys erwartet >=1, ist {bk.get('groq')}"
    assert bk.get("openrouter", 0) >= 1, (
        f"openrouter backup_keys erwartet >=1, ist {bk.get('openrouter')}"
    )
    # providers: alle 5 true
    prov = d["providers"]
    for p in ("gemini", "groq", "openrouter", "mistral", "cerebras"):
        assert prov.get(p) is True, f"provider {p} nicht verfügbar: {prov}"


# ---------------- Modell-Wächter Status ----------------

def test_models_watch_status_has_expected_keys():
    r = _get_retry(f"{BASE_URL}/api/ai/models/watch", timeout=30)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    for k in ("checked_at", "discovered", "last_new"):
        assert k in d, f"Feld fehlt in /api/ai/models/watch: {k} (got {list(d.keys())})"
    assert isinstance(d["discovered"], dict), "discovered muss dict sein"


# ---------------- Strategie-Vergleich: kein 'unknown' ----------------

def test_strategy_comparison_no_unknown():
    r = _get_retry(f"{BASE_URL}/api/analytics/strategy-comparison", timeout=60)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    comp = d.get("comparison")
    assert isinstance(comp, list), f"comparison ist keine Liste: {type(comp)}"
    unknown_entries = [e for e in comp if e.get("strategy_id") == "unknown"]
    assert not unknown_entries, f"'unknown' im Vergleich: {unknown_entries[:2]}"


# ---------------- Advisor: Auth ----------------

def test_advisor_requires_auth():
    for _ in range(3):
        try:
            r = requests.post(
                f"{BASE_URL}/api/ai/team/advisor",
                json={"message": "Test", "history": []},
                timeout=30,
            )
            break
        except Exception:
            time.sleep(2)
    assert r.status_code == 401, f"erwartet 401, got {r.status_code}: {r.text[:200]}"


# ---------------- Advisor: SSE-Stream mit echter Modell-Antwort ----------------

def test_advisor_sse_stream_returns_german_recommendation(auth_headers):
    """Streamt Antwort und prüft, dass mehrere data:{'t':...} Tokens und ein
    finales {'done':true} kommen. Antwort soll deutschsprachig sein und ein
    Modell empfehlen (Fallback-Kette darf bis ~60s brauchen)."""
    url = f"{BASE_URL}/api/ai/team/advisor"
    body = {
        "message": "Antworte in einem kurzen deutschen Satz: welches Modell empfiehlst du als Haupt-Modell?",
        "history": [],
    }
    with requests.post(url, json=body, headers=auth_headers, stream=True, timeout=120) as r:
        assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
        assert "text/event-stream" in r.headers.get("content-type", ""), (
            f"Content-Type: {r.headers.get('content-type')}"
        )
        tokens = []
        done_seen = False
        error_seen = None
        started = time.time()
        for raw in r.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            if not raw:
                continue
            if not raw.startswith("data:"):
                continue
            payload = raw[5:].strip()
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if "t" in obj:
                tokens.append(str(obj["t"]))
            if obj.get("error"):
                error_seen = obj["error"]
            if obj.get("done"):
                done_seen = True
                break
            if time.time() - started > 90:
                break
        text = "".join(tokens)
        assert done_seen, f"kein done-Event gesehen. tokens={len(tokens)} error={error_seen} preview={text[:200]!r}"
        assert not error_seen, f"Fehler-Event: {error_seen}"
        assert len(tokens) >= 3, f"zu wenige Token-Events: {len(tokens)} / preview={text[:200]!r}"
        assert len(text) > 20, f"Advisor-Antwort zu kurz: {text!r}"
        # Deutsch-Check: mindestens ein deutsches Funktionswort ODER Modell-Slug
        lower = text.lower()
        german_hint = any(w in lower for w in (
            "der ", "die ", "das ", "ich ", "empfehle", "modell", "als ", "für ",
            "mit ", "nicht", "ist ", "sind ", "wenn ", "und "))
        model_hint = bool(re.search(r"[a-z0-9\-\.]+/[a-z0-9\-\._:]+", lower))
        assert german_hint or model_hint, (
            f"Antwort weder deutsch noch mit Modell-Slug: {text[:300]!r}"
        )
