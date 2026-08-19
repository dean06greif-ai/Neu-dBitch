"""Iteration 46 – Regressionstests:
1) Papierkorb fürs KI-Labor: Forschungs-/ML-Reset verschiebt in lab_trash,
   Wiederherstellen stellt Modell/Report zurück (statt endgültig löschen).
2) KI-Spanne Richtungs-Guard (tune_guard_min/max): KI darf max_same_direction
   nur innerhalb der Trader-Leitplanken selbst setzen; Spanne selbst ist tabu.
"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "crypto_scanner")


@pytest.fixture(scope="module")
def auth_headers():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"username": os.environ.get("ADMIN_USER", "Admin"),
                            "password": os.environ.get("ADMIN_PASSWORD", "admin")}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.text[:200]}"
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def db():
    from pymongo import MongoClient
    return MongoClient(MONGO_URL)[DB_NAME]


# ---------------- Unit: Autonomie-Spanne Richtungs-Guard ----------------
class TestGuardSpanUnit:
    def _engine(self, lo, hi):
        from services.ai_engine import ai_engine
        ai_engine.config["tune_guard_min"] = lo
        ai_engine.config["tune_guard_max"] = hi
        return ai_engine

    def test_inside_span_allowed(self):
        eng = self._engine(2, 5)
        assert eng._tuning_guard({"max_same_direction": 3}) == ""
        assert eng._tuning_guard({"max_same_direction": 2}) == ""
        assert eng._tuning_guard({"max_same_direction": 5}) == ""

    def test_outside_span_needs_confirmation(self):
        eng = self._engine(2, 5)
        assert "Autonomie-Spanne 2–5" in eng._tuning_guard({"max_same_direction": 6})
        assert "Autonomie-Spanne 2–5" in eng._tuning_guard({"max_same_direction": 1})

    def test_zero_always_needs_confirmation(self):
        eng = self._engine(1, 10)
        assert "aus" in eng._tuning_guard({"max_same_direction": 0})

    def test_defaults_1_to_6(self):
        from services.ai_engine import ai_engine
        ai_engine.config.pop("tune_guard_min", None)
        ai_engine.config.pop("tune_guard_max", None)
        assert ai_engine._tuning_guard({"max_same_direction": 6}) == ""
        assert "Autonomie-Spanne" in ai_engine._tuning_guard({"max_same_direction": 7})

    def test_ki_darf_spanne_selbst_nie_aendern(self):
        from services.ai_knowledge import validate_changes
        valid, rejected = validate_changes(
            {"tune_guard_min": 1, "tune_guard_max": 10, "min_confidence": 60},
            scope="engine")
        assert "tune_guard_min" in rejected and "tune_guard_max" in rejected
        assert "min_confidence" in valid


# ---------------- API: Spanne setzen & clampen ----------------
class TestGuardSpanApi:
    def test_set_and_clamp(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"tune_guard_min": 2, "tune_guard_max": 5}, timeout=15)
        assert r.status_code == 200
        cfg = r.json().get("config", {})
        assert cfg.get("tune_guard_min") == 2 and cfg.get("tune_guard_max") == 5
        # min > max -> min wird auf max gezogen
        r = requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                          json={"tune_guard_min": 8, "tune_guard_max": 4}, timeout=15)
        cfg = r.json().get("config", {})
        assert cfg.get("tune_guard_min") <= cfg.get("tune_guard_max")
        # zurück auf Default
        requests.post(f"{BASE_URL}/api/ai/config", headers=auth_headers,
                      json={"tune_guard_min": 1, "tune_guard_max": 6}, timeout=15)


# ---------------- API: Papierkorb Forschung + ML ----------------
class TestLabTrash:
    def test_trash_list_public(self):
        r = requests.get(f"{BASE_URL}/api/ai/lab/trash", timeout=15)
        assert r.status_code == 200
        assert isinstance(r.json().get("items"), list)

    def test_restore_requires_auth(self):
        r = requests.post(f"{BASE_URL}/api/ai/lab/trash/restore",
                          json={"id": "egal"}, timeout=15)
        assert r.status_code in (401, 403)

    def test_discard_forever(self, auth_headers, db):
        db.settings.replace_one(
            {"_id": "ai_ml_model"},
            {"trained_at": "2026-02-02T00:00:00", "cv_auc": 0.5}, upsert=True)
        requests.post(f"{BASE_URL}/api/ai/ml/reset", headers=auth_headers, timeout=20)
        items = requests.get(f"{BASE_URL}/api/ai/lab/trash", timeout=15).json()["items"]
        tid = [i for i in items if i["kind"] == "ml"][0]["id"]
        # ohne Auth verboten
        r = requests.post(f"{BASE_URL}/api/ai/lab/trash/discard",
                          json={"id": tid}, timeout=15)
        assert r.status_code in (401, 403)
        r = requests.post(f"{BASE_URL}/api/ai/lab/trash/discard", headers=auth_headers,
                          json={"id": tid}, timeout=15)
        assert r.status_code == 200
        ids = [i["id"] for i in requests.get(
            f"{BASE_URL}/api/ai/lab/trash", timeout=15).json()["items"]]
        assert tid not in ids, "Eintrag muss endgültig weg sein"
        # danach weder Restore noch zweites Discard möglich
        r = requests.post(f"{BASE_URL}/api/ai/lab/trash/restore", headers=auth_headers,
                          json={"id": tid}, timeout=15)
        assert r.status_code == 404
        r = requests.post(f"{BASE_URL}/api/ai/lab/trash/discard", headers=auth_headers,
                          json={"id": tid}, timeout=15)
        assert r.status_code == 404

    def test_ml_reset_restore_roundtrip(self, auth_headers, db):
        db.settings.replace_one(
            {"_id": "ai_ml_model"},
            {"trained_at": "2026-01-01T00:00:00", "cv_auc": 0.55, "samples": 42},
            upsert=True)
        r = requests.post(f"{BASE_URL}/api/ai/ml/reset", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        assert db.settings.find_one({"_id": "ai_ml_model"}) is None
        items = requests.get(f"{BASE_URL}/api/ai/lab/trash", timeout=15).json()["items"]
        ml_items = [i for i in items if i["kind"] == "ml"]
        assert ml_items, "ML-Eintrag fehlt im Papierkorb"
        assert "payload" not in ml_items[0], "Payload darf nicht in der Liste stehen"
        r = requests.post(f"{BASE_URL}/api/ai/lab/trash/restore", headers=auth_headers,
                          json={"id": ml_items[0]["id"]}, timeout=20)
        assert r.status_code == 200, r.text[:200]
        doc = db.settings.find_one({"_id": "ai_ml_model"})
        assert doc and doc.get("cv_auc") == 0.55
        # zweites Restore desselben Eintrags -> 404 (Eintrag verbraucht)
        r = requests.post(f"{BASE_URL}/api/ai/lab/trash/restore", headers=auth_headers,
                          json={"id": ml_items[0]["id"]}, timeout=20)
        assert r.status_code == 404

    def test_research_reset_restore_roundtrip(self, auth_headers, db):
        db.settings.replace_one(
            {"_id": "ai_research_report"},
            {"ts": "2026-01-02T00:00:00",
             "insights": [{"title": "t1", "detail": "Test-Erkenntnis"}]}, upsert=True)
        db.settings.replace_one(
            {"_id": "ai_research_state"}, {"counts": {"backtests": 7}}, upsert=True)
        r = requests.post(f"{BASE_URL}/api/ai/research/reset", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        assert db.settings.find_one({"_id": "ai_research_report"}) is None
        items = requests.get(f"{BASE_URL}/api/ai/lab/trash", timeout=15).json()["items"]
        re_items = [i for i in items if i["kind"] == "research"]
        assert re_items, "Forschungs-Eintrag fehlt im Papierkorb"
        r = requests.post(f"{BASE_URL}/api/ai/lab/trash/restore", headers=auth_headers,
                          json={"id": re_items[0]["id"]}, timeout=20)
        assert r.status_code == 200, r.text[:200]
        rep = db.settings.find_one({"_id": "ai_research_report"})
        st = db.settings.find_one({"_id": "ai_research_state"})
        assert rep and rep.get("ts") == "2026-01-02T00:00:00"
        assert st and st.get("counts", {}).get("backtests") == 7

    def test_empty_reset_creates_no_trash_entry(self, auth_headers, db):
        db.settings.delete_one({"_id": "ai_ml_model"})
        before = len([i for i in requests.get(
            f"{BASE_URL}/api/ai/lab/trash", timeout=15).json()["items"] if i["kind"] == "ml"])
        requests.post(f"{BASE_URL}/api/ai/ml/reset", headers=auth_headers, timeout=20)
        after = len([i for i in requests.get(
            f"{BASE_URL}/api/ai/lab/trash", timeout=15).json()["items"] if i["kind"] == "ml"])
        assert after == before, "leerer Reset darf keinen Papierkorb-Eintrag erzeugen"
