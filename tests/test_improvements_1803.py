"""Regressionstests für die Verbesserungen vom 18.03-Branch:
1) Analyse-Intervalle am Voll-Stunden-Raster (ai_schedule.next_aligned_ts)
2) Event-getriggerte Tiefenanalyse bei medium/high News (should_trigger_deep)
3) Key-Level-Rate-Limit-Tracking (usable_key_indices / key_status)
4) Modell-Wächter Bestätigungs-Flow (approve/dismiss, nur bestätigte aktiv)
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

BERLIN = ZoneInfo("Europe/Berlin")


# ---------------- 1) Voll-Stunden-Raster ----------------
def _ts(h, m, s=0):
    return datetime(2026, 6, 15, h, m, s, tzinfo=BERLIN).timestamp()


def test_aligned_15min_grid():
    from services.ai_schedule import next_aligned_ts
    nxt = next_aligned_ts(_ts(14, 7), 15, BERLIN)
    assert datetime.fromtimestamp(nxt, BERLIN).strftime("%H:%M") == "14:15"


def test_aligned_exact_slot_moves_to_next():
    from services.ai_schedule import next_aligned_ts
    nxt = next_aligned_ts(_ts(14, 0), 15, BERLIN)
    assert datetime.fromtimestamp(nxt, BERLIN).strftime("%H:%M") == "14:15"


def test_aligned_30min_before_full_hour():
    from services.ai_schedule import next_aligned_ts
    nxt = next_aligned_ts(_ts(13, 59, 30), 30, BERLIN)
    assert datetime.fromtimestamp(nxt, BERLIN).strftime("%H:%M") == "14:00"


def test_aligned_hourly():
    from services.ai_schedule import next_aligned_ts
    nxt = next_aligned_ts(_ts(9, 12), 60, BERLIN)
    assert datetime.fromtimestamp(nxt, BERLIN).strftime("%H:%M") == "10:00"


def test_aligned_5min_no_odd_minutes():
    from services.ai_schedule import next_aligned_ts
    nxt = next_aligned_ts(_ts(22, 3, 44), 5, BERLIN)
    dt = datetime.fromtimestamp(nxt, BERLIN)
    assert dt.strftime("%H:%M") == "22:05" and dt.second == 0


# ---------------- 2) News -> Tiefenanalyse ----------------
def test_deep_trigger_medium_and_high():
    from services.ai_news_watcher import should_trigger_deep
    assert should_trigger_deep("medium", True, 0, 10_000)
    assert should_trigger_deep("high", True, 0, 10_000)


def test_deep_trigger_low_never():
    from services.ai_news_watcher import should_trigger_deep
    assert not should_trigger_deep("low", True, 0, 10_000)


def test_deep_trigger_respects_cooldown_and_toggle():
    from services.ai_news_watcher import should_trigger_deep, DEEP_TRIGGER_COOLDOWN_S
    now = 100_000.0
    assert not should_trigger_deep("high", True, now - 60, now)          # Cooldown aktiv
    assert should_trigger_deep("high", True, now - DEEP_TRIGGER_COOLDOWN_S, now)
    assert not should_trigger_deep("high", False, 0, now)                # Toggle aus


# ---------------- 3) Key-Level-Tracking ----------------
def test_usable_keys_skip_fresh_limited():
    from services import ai_providers as ap
    prov = f"testprov_{uuid.uuid4().hex[:6]}"
    now = ap._now()
    ap.mark_key_limited(prov, 1, "429")
    idxs = ap.usable_key_indices(prov, 4, now=now)
    assert idxs == [0, 2, 3]


def test_usable_keys_all_limited_falls_back_to_all():
    from services import ai_providers as ap
    prov = f"testprov_{uuid.uuid4().hex[:6]}"
    for i in range(3):
        ap.mark_key_limited(prov, i, "429")
    assert ap.usable_key_indices(prov, 3) == [0, 1, 2]


def test_usable_keys_cooldown_expires():
    from services import ai_providers as ap
    prov = f"testprov_{uuid.uuid4().hex[:6]}"
    ap.mark_key_limited(prov, 0, "429")
    later = ap._now() + ap.KEY_LIMIT_COOLDOWN_S + 1
    assert ap.usable_key_indices(prov, 2, now=later) == [0, 1]


def test_clear_key_limited_on_success():
    from services import ai_providers as ap
    prov = f"testprov_{uuid.uuid4().hex[:6]}"
    ap.mark_key_limited(prov, 0, "429")
    ap.clear_key_limited(prov, 0)
    assert ap.usable_key_indices(prov, 2) == [0, 1]


def test_key_status_reports_cerebras_key_count():
    from services import ai_providers as ap
    st = ap.key_status()
    if "cerebras" in st:  # nur wenn Keys in der Env gesetzt sind
        assert st["cerebras"]["total"] == len(ap.provider_keys("cerebras"))
        assert st["cerebras"]["total"] >= 1


# ---------------- 4) Modell-Wächter Bestätigungs-Flow ----------------
def _test_db():
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[f"test_model_watch_{uuid.uuid4().hex[:8]}"]


def test_approve_flow_only_confirmed_models_active():
    from services import ai_providers
    from services.ai_model_watch import ModelWatch, DOC_ID

    async def run():
        client, db = _test_db()
        try:
            mw = ModelWatch()
            await db.settings.update_one(
                {"_id": DOC_ID},
                {"$set": {"discovered": {"groq": ["new/test-model-x"]}}}, upsert=True)
            # Boot: unbestätigt -> NICHT freigeschaltet
            await mw.load_discovered(db)
            assert "new/test-model-x" not in ai_providers.allowed_models("groq")
            # Bestätigen -> freigeschaltet
            res = await mw.approve(db, "groq", "new/test-model-x")
            assert res["status"] == "ok"
            assert "new/test-model-x" in ai_providers.allowed_models("groq")
            # Unbekanntes Modell kann nicht bestätigt werden
            bad = await mw.approve(db, "groq", "does/not-exist")
            assert bad["status"] == "error"
        finally:
            ai_providers.set_dynamic_models({})
            await client.drop_database(db.name)
            client.close()
    asyncio.run(run())


def test_dismiss_removes_and_blocks_rediscovery():
    from services import ai_providers
    from services.ai_model_watch import ModelWatch, DOC_ID

    async def run():
        client, db = _test_db()
        try:
            mw = ModelWatch()
            await db.settings.update_one(
                {"_id": DOC_ID},
                {"$set": {"discovered": {"groq": ["new/test-model-y"]},
                          "approved": {"groq": ["new/test-model-y"]}}}, upsert=True)
            await mw.load_discovered(db)
            assert "new/test-model-y" in ai_providers.allowed_models("groq")
            res = await mw.dismiss(db, "groq", "new/test-model-y")
            assert res["status"] == "ok"
            assert "new/test-model-y" not in ai_providers.allowed_models("groq")
            doc = await db.settings.find_one({"_id": DOC_ID})
            assert "groq/new/test-model-y" in (doc.get("dismissed") or [])
        finally:
            ai_providers.set_dynamic_models({})
            await client.drop_database(db.name)
            client.close()
    asyncio.run(run())
