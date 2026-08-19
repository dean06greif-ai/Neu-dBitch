"""Modell-Berater: neutraler Chat im KI-Team-Reiter.

Kennt den kompletten Modell-Katalog (inkl. vom Modell-Wächter neu entdeckter
Modelle), die Free-Tier-Limits und Token-Budgets, die Modell-Gewichte, die
aktuelle Team-Konfiguration und den Live-Zustand (Rate-Limits, aktive
Fallbacks). Er berät neutral, welche Modelle für welche Rolle bzw. als
Fallback 1/2 sinnvoll sind – und hinterfragt ausdrücklich auch das aktuelle
Haupt-Modell (also sein eigenes)."""
import logging
from typing import AsyncIterator, Dict, List

from services import ai_providers

logger = logging.getLogger(__name__)

# Ungefähre Free-Tier-Limits der Anbieter (Stand regelmäßig geprüft; als
# Beratungs-Wissen bewusst konservativ formuliert).
FREE_TIER_LIMITS = (
    "- Gemini Free: Flash-Modelle ~250-1000 Anfragen/Tag, Pro ~50-100/Tag, "
    "~250k Tokens/min. Sehr großer Kontext (1M).\n"
    "- Groq Free: je nach Modell ~1.000-14.400 Anfragen/Tag, nur 6k-30k "
    "Tokens/min (TPM) – große Prompts scheitern schnell mit 413, siehe "
    "Token-Budgets unten. Extrem schnelle Antworten.\n"
    "- Cerebras Free: ~14.400 Anfragen/Tag, ~60k Tokens/min, ~1M Tokens/Tag "
    "pro Key – extrem schnell, ideal als Arbeitspferd/Fallback.\n"
    "- OpenRouter :free-Modelle: ~50 Anfragen/Tag ohne Guthaben, ~1000/Tag "
    "ab 10$ Guthaben auf dem Account. Bezahlte Modelle (DeepSeek, GLM, Grok…) "
    "kosten pro Token und werden NIE als Auto-Fallback genutzt.\n"
    "- Mistral Free: ~1 Anfrage/s, ~500k Tokens/min, ~1 Mrd. Tokens/Monat."
)

SYSTEM_TEMPLATE = (
    "Du bist der neutrale MODELL-BERATER dieser Trading-Website. Deine einzige "
    "Aufgabe: den Betreiber bei der Wahl der KI-Modelle für die Team-Rollen "
    "(Haupt-Modell, Rollen-Modelle, Fallback 1/2) zu beraten.\n"
    "WICHTIG:\n"
    "- Sei strikt NEUTRAL. Du läufst selbst auf einem dieser Modelle – "
    "hinterfrage und kritisiere auch das aktuelle Haupt-Modell (dein eigenes), "
    "wenn es objektiv nicht die beste Wahl ist.\n"
    "- Begründe Empfehlungen mit Fakten: Tages-/Minuten-Limits (Caps), "
    "Token-Budgets, Geschwindigkeit, Qualität (Gewicht 1-3), Backup-Keys, "
    "Kosten (bezahlte Modelle klar kennzeichnen).\n"
    "- Denke in Ketten: Haupt-Modell und Fallbacks sollten möglichst "
    "verschiedene Anbieter nutzen, damit ein Rate-Limit nicht alles lahmlegt.\n"
    "- Antworte kurz, konkret und auf Deutsch. Nenne Modelle immer als "
    "provider/modell.\n"
    "- EMPFEHLUNG ZUM ÜBERNEHMEN: Wenn der Nutzer eine konkrete Empfehlung "
    "möchte (oder du eine klar bessere Konfiguration siehst), hänge ans ENDE "
    "deiner Antwort GENAU EINEN maschinenlesbaren Block an:\n"
    "<<<APPLY\n"
    '{{"roles": {{"<rollen_key>": {{"provider": "...", "model": "...", '
    '"fallback_provider": "...", "fallback_model": "...", '
    '"fallback2_provider": "...", "fallback2_model": "..."}}}}, '
    '"main": {{"provider": "...", "model": "..."}}}}\n'
    "APPLY>>>\n"
    "Nur Rollen/Felder aufnehmen, die du wirklich ändern willst ('main' nur bei "
    "Haupt-Modell-Wechsel). Gültige Rollen-Keys: {role_keys}. Nur exakte "
    "Modell-Slugs aus dem Katalog. Der Trader bekommt dann einen "
    "„Empfehlung übernehmen“-Button.\n\n"
    "=== FREE-TIER-LIMITS (CAPS) DER ANBIETER ===\n{limits}\n\n"
    "=== VERFÜGBARE MODELLE (Gewicht 1=leicht, 3=stark · Keys: primär+Backups) ===\n{catalog}\n\n"
    "=== TOKEN-BUDGETS PRO ANFRAGE (413-Schutz, gelernt aus echten Fehlern) ===\n{budgets}\n\n"
    "=== AKTUELLE TEAM-KONFIGURATION ===\n{team}\n\n"
    "=== LIVE-ZUSTAND (Rate-Limits / aktive Fallbacks, letzte 30 min) ===\n{health}\n\n"
    "=== BISHERIGER CHAT ===\n{history}"
)


def _catalog_block() -> str:
    lines = []
    counts = ai_providers.backup_key_counts()
    avail = ai_providers.available_providers()
    for prov in list(ai_providers.ALLOWED_MODELS):
        key_info = f"{1 + counts.get(prov, 0)} Key(s)" if avail.get(prov) else "KEIN KEY!"
        lines.append(f"[{prov} · {key_info}]")
        for m in ai_providers.allowed_models(prov):
            w = ai_providers.model_weight(m)
            tags = []
            if m in ai_providers.PAID_MODELS_NO_FALLBACK:
                tags.append("BEZAHLT")
            if m in ai_providers.DYNAMIC_MODELS.get(prov, []):
                tags.append("NEU entdeckt")
            tag = f" ({', '.join(tags)})" if tags else ""
            lines.append(f"  - {prov}/{m} · Gewicht {w}{tag}")
    return "\n".join(lines)


def _budget_block() -> str:
    rows = dict(ai_providers.MODEL_INPUT_TOKEN_BUDGET)
    for k, v in ai_providers._learned_budget.items():
        rows[k] = min(rows.get(k, v), v)
    if not rows:
        return "(keine Budget-Einschränkungen bekannt)"
    return "\n".join(f"- {k}: max. ~{v} Input-Tokens" for k, v in sorted(rows.items()))


def _team_block() -> str:
    from services.ai_engine import ai_engine
    from services.ai_roles import role_manager, ROLE_LABELS
    cfg = ai_engine.config or {}
    lines = [f"Haupt-Modell (Engine): {cfg.get('provider')}/{cfg.get('model')}"]
    for role, rc in (role_manager.snapshot() or {}).items():
        label = ROLE_LABELS.get(role, role)
        model = f"{rc.get('provider')}/{rc.get('model')}" if rc.get("model") else "erbt Haupt-Modell"
        fb1 = f"{rc.get('fallback_provider')}/{rc.get('fallback_model')}" if rc.get("fallback_model") else "–"
        fb2 = f"{rc.get('fallback2_provider')}/{rc.get('fallback2_model')}" if rc.get("fallback2_model") else "–"
        state = "AUS" if rc.get("enabled") is False else "aktiv"
        lines.append(f"- {label} ({role}, {state}): {model} · Fallback1 {fb1} · Fallback2 {fb2}")
    return "\n".join(lines)


def _health_block() -> str:
    h = ai_providers.health_status()
    lines = []
    for g in h.get("rate_limited_grouped") or []:
        lines.append(f"- RATE-LIMIT {g['provider']}: {', '.join(g['models'][:4])} "
                     f"(Cooldown noch ~{g.get('cooldown_left_s', 0) // 60} min)")
    for fb in h.get("active_fallbacks") or []:
        lines.append(f"- FALLBACK aktiv: Rolle {fb.get('role')} läuft auf "
                     f"{fb.get('provider')}/{fb.get('model')} statt {fb.get('requested_model')}")
    return "\n".join(lines) or "(alles im grünen Bereich – keine Limits, keine Fallbacks)"


def build_system() -> str:
    from services.ai_roles import ROLE_LABELS
    return SYSTEM_TEMPLATE.format(
        role_keys=", ".join(ROLE_LABELS),
        limits=FREE_TIER_LIMITS,
        catalog=_catalog_block(),
        budgets=_budget_block(),
        team=_team_block(),
        health=_health_block(),
        history="{history}",
    )


async def advisor_stream(text: str, history: List[Dict]) -> AsyncIterator[str]:
    """Streamt die Berater-Antwort über die Haupt-Modell-Kette (inkl. Fallbacks)."""
    from services.ai_engine import ai_engine
    from services.ai_roles import role_manager
    ai_providers.set_current_role("model_advisor")
    chain = role_manager.chain("chat", ai_engine.config or {})
    hist = "\n".join(
        f"{'Nutzer' if m.get('role') == 'user' else 'Berater'}: {str(m.get('text', ''))[:500]}"
        for m in (history or [])[-10:]
    ) or "(erste Nachricht)"
    system = build_system().replace("{history}", hist)
    async for kind, payload in ai_providers.stream_chain(chain, text, system, temperature=0.5):
        if kind == "token":
            yield payload
        elif kind == "error":
            yield f"\n⚠️ {payload}"
    ai_providers.set_current_role(None)
