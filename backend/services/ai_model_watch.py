"""Modell-Wächter: prüft wöchentlich alle konfigurierten Modell-Slugs.

1) Meldet tote Slugs (Modell beim Anbieter entfernt/umbenannt) per Website-
   Benachrichtigung + Telegram – die Fallback-Ketten übernehmen automatisch.
2) Entdeckt NEUE Chat-Modelle in den Live-Katalogen der Provider, schaltet sie
   sofort zur Auswahl frei (ai_providers.DYNAMIC_MODELS, sichtbar im KI-Team &
   AI-Panel) und meldet sie per Website-Glocke + Telegram.

Ergebnis wird in `settings/model_watch` abgelegt und ist über
/api/ai/models/watch abrufbar (manueller Lauf: POST .../run). Der Lauf ist
bewusst leichtgewichtig: 1 Katalog-Request pro Provider, 1x pro Woche.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

from services import ai_providers

logger = logging.getLogger(__name__)

DOC_ID = "model_watch"
CHECK_EVERY_S = 12 * 3600      # Loop prüft 2x täglich, ob ein Lauf fällig ist
INTERVAL_DAYS = 7              # wöchentlicher Voll-Check (schont die Free-Tiers)


class ModelWatch:
    def __init__(self):
        self.running = False

    async def status(self, db) -> Dict:
        doc = await db.settings.find_one({"_id": DOC_ID}) or {}
        doc.pop("_id", None)
        return {"running": self.running, "interval_days": INTERVAL_DAYS, **doc}

    async def load_discovered(self, db):
        """Beim Boot: nur vom Trader BESTÄTIGTE Modelle wieder freischalten.
        Entdeckte, aber unbestätigte Modelle bleiben gesperrt (Pending)."""
        try:
            doc = await db.settings.find_one({"_id": DOC_ID}) or {}
            approved = doc.get("approved") or {}
            ai_providers.set_dynamic_models(approved)
            n = sum(len(v) for v in ai_providers.DYNAMIC_MODELS.values())
            if n:
                logger.info(f"Modell-Wächter: {n} bestätigte Modelle wieder aktiv")
            pending = sum(len(v) for v in (doc.get("discovered") or {}).values())
            if pending > n:
                logger.info(f"Modell-Wächter: {pending - n} entdeckte Modelle "
                            "warten auf Bestätigung im KI-Team-Panel")
        except Exception as e:
            logger.warning(f"Modell-Wächter load_discovered: {e}")

    async def run_check(self, db, manual: bool = False) -> Dict:
        if self.running:
            return {"status": "busy", "detail": "Modell-Check läuft bereits"}
        self.running = True
        try:
            prev = await db.settings.find_one({"_id": DOC_ID}) or {}
            first_baseline = "discovered" not in prev  # 1. Lauf: nur Basis speichern, nicht spammen
            known = {f"{p}/{m}" for p, ms in (prev.get("discovered") or {}).items()
                     for m in (ms or [])}
            result = await ai_providers.verify_catalog()
            dead = result.get("dead") or []
            dismissed = set(prev.get("dismissed") or [])
            discovered = {}
            for p, ms in (result.get("new") or {}).items():
                keep = [m for m in (ms or []) if f"{p}/{m}" not in dismissed]
                if keep:
                    discovered[p] = keep
            brand_new = [f"{p}/{m}" for p, ms in discovered.items() for m in ms
                         if f"{p}/{m}" not in known]
            # Bestätigte Modelle behalten, solange der Anbieter sie noch führt.
            # Bei nicht prüfbaren Providern (kein Key/Katalog down) nichts verwerfen.
            unverified = set(result.get("unverified") or [])
            approved = {}
            for p, ms in (prev.get("approved") or {}).items():
                keep = list(ms or []) if p in unverified else \
                    [m for m in (ms or []) if m in (discovered.get(p) or [])]
                if keep:
                    approved[p] = keep
            payload = {"checked_at": datetime.now(timezone.utc).isoformat(),
                       "dead": dead,
                       "unverified": sorted(unverified),
                       "providers": result.get("providers") or {},
                       "discovered": discovered,
                       "approved": approved,
                       "dismissed": sorted(dismissed),
                       "last_new": brand_new,
                       "manual": bool(manual)}
            await db.settings.update_one({"_id": DOC_ID}, {"$set": payload}, upsert=True)
            # Nur BESTÄTIGTE Modelle werden freigeschaltet – neu entdeckte warten
            # auf den Bestätigungs-Button im KI-Team-Panel.
            ai_providers.set_dynamic_models(approved)
            from core import state
            from services import notifications
            if dead:
                lst = ", ".join(dead[:8])
                await notifications.website_notify(
                    db, "model_watch", "Modell-Wächter: tote Modell-Slugs erkannt",
                    f"Diese konfigurierten KI-Modelle existieren beim Anbieter nicht mehr: {lst}. "
                    "Die Fallback-Ketten übernehmen automatisch – bitte im KI-Team ein anderes "
                    "Modell wählen.", cooldown_min=60)
                await notifications.telegram_notify(
                    db, state.telegram, "model_watch",
                    f"🛰️ *MODELL-WÄCHTER*\nTote Modell-Slugs erkannt: {lst}\n"
                    "Fallbacks übernehmen – bitte Modelle im KI-Team aktualisieren.",
                    cooldown_min=60)
                logger.warning(f"Modell-Wächter: tote Slugs -> {dead}")
            if brand_new and not first_baseline:
                lst = ", ".join(brand_new[:10])
                more = f" (+{len(brand_new) - 10} weitere)" if len(brand_new) > 10 else ""
                await notifications.website_notify(
                    db, "model_watch_new", "Neue KI-Modelle entdeckt",
                    f"Der Modell-Wächter hat neue Modelle entdeckt: {lst}{more}. "
                    "Sie werden erst nach Bestätigung im KI-Team-Panel auswählbar "
                    "(Button 'Bestätigen').",
                    cooldown_min=60)
                await notifications.telegram_notify(
                    db, state.telegram, "model_watch_new",
                    f"🆕 *NEUE KI-MODELLE ENTDECKT*\n{lst}{more}\n"
                    "Zur Freischaltung im KI-Team-Panel bestätigen.",
                    cooldown_min=60)
                logger.info(f"Modell-Wächter: neue Modelle entdeckt -> {brand_new}")
            if not dead and not brand_new:
                logger.info("Modell-Wächter: alle Modelle verfügbar, nichts Neues")
            return {"status": "ok", **payload}
        except Exception as e:
            logger.error(f"Modell-Wächter fehlgeschlagen: {e}")
            return {"status": "error", "detail": str(e)[:200]}
        finally:
            self.running = False

    async def approve(self, db, provider: str, model: str) -> Dict:
        """Bestätigungs-Button: entdecktes Modell zur Auswahl freischalten."""
        doc = await db.settings.find_one({"_id": DOC_ID}) or {}
        if model not in ((doc.get("discovered") or {}).get(provider) or []):
            return {"status": "error", "detail": "Modell nicht in den entdeckten Modellen"}
        approved = doc.get("approved") or {}
        cur = list(approved.get(provider) or [])
        if model not in cur:
            cur.append(model)
        approved[provider] = cur
        await db.settings.update_one(
            {"_id": DOC_ID}, {"$set": {"approved": approved}}, upsert=True)
        ai_providers.set_dynamic_models(approved)
        logger.info(f"Modell-Wächter: {provider}/{model} vom Trader bestätigt")
        return {"status": "ok", "approved": approved}

    async def dismiss(self, db, provider: str, model: str) -> Dict:
        """Entdecktes Modell verwerfen (taucht nicht erneut als 'neu' auf)."""
        doc = await db.settings.find_one({"_id": DOC_ID}) or {}
        discovered = doc.get("discovered") or {}
        if model in (discovered.get(provider) or []):
            discovered[provider] = [m for m in discovered[provider] if m != model]
            if not discovered[provider]:
                discovered.pop(provider)
        approved = doc.get("approved") or {}
        if model in (approved.get(provider) or []):
            approved[provider] = [m for m in approved[provider] if m != model]
            if not approved[provider]:
                approved.pop(provider)
        dismissed = sorted(set(doc.get("dismissed") or []) | {f"{provider}/{model}"})
        await db.settings.update_one(
            {"_id": DOC_ID},
            {"$set": {"discovered": discovered, "approved": approved,
                      "dismissed": dismissed}}, upsert=True)
        ai_providers.set_dynamic_models(approved)
        logger.info(f"Modell-Wächter: {provider}/{model} verworfen")
        return {"status": "ok"}

    async def run_loop(self):
        """Hintergrund-Loop: wöchentlicher Check (2 Min nach Boot erstmals geprüft)."""
        from core import state
        await asyncio.sleep(120)
        if state.db is not None:
            await self.load_discovered(state.db)
        while True:
            try:
                db = state.db
                if db is not None:
                    doc = await db.settings.find_one({"_id": DOC_ID}) or {}
                    due = True
                    last = doc.get("checked_at")
                    if last:
                        try:
                            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
                            due = age.days >= INTERVAL_DAYS
                        except ValueError:
                            due = True
                    if due:
                        await self.run_check(db)
            except Exception as e:
                logger.warning(f"Modell-Wächter-Loop: {e}")
            await asyncio.sleep(CHECK_EVERY_S)


model_watch = ModelWatch()
