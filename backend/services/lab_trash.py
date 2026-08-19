"""Papierkorb fürs KI-Labor: Forschungs- und ML-Resets löschen nichts mehr
endgültig, sondern verschieben den Stand in die Kollektion `lab_trash`.
Von dort kann jeder Eintrag per Klick wiederhergestellt werden.
Pro Art (kind: 'ml' | 'research') werden die letzten Stände behalten."""
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

KEEP_PER_KIND = 10


async def put(db, kind: str, payload: Dict, label: str = "") -> str:
    tid = str(uuid.uuid4())
    await db.lab_trash.insert_one({
        "id": tid, "kind": kind, "label": label,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload})
    try:
        old = await (db.lab_trash.find({"kind": kind}, {"id": 1})
                     .sort("deleted_at", -1).skip(KEEP_PER_KIND).to_list(100))
        if old:
            await db.lab_trash.delete_many({"id": {"$in": [r["id"] for r in old]}})
    except Exception as e:
        logger.warning(f"Papierkorb-Kappung fehlgeschlagen: {e}")
    return tid


async def list_items(db) -> List[Dict]:
    return await (db.lab_trash.find({}, {"_id": 0, "payload": 0})
                  .sort("deleted_at", -1).to_list(50))


async def pop(db, tid: str) -> Optional[Dict]:
    doc = await db.lab_trash.find_one({"id": tid})
    if doc:
        await db.lab_trash.delete_one({"id": tid})
        doc.pop("_id", None)
    return doc


async def discard(db, tid: str) -> bool:
    """Eintrag endgültig löschen (ohne Wiederherstellung)."""
    res = await db.lab_trash.delete_one({"id": tid})
    return res.deleted_count > 0
