"""Zentrale Provider-Schicht für alle KI-Aufrufe.

Kapselt Modell-Katalog, API-Key-Verwaltung (inkl. Backup-Keys), Modell-Gewichte
und die eigentliche Generierung (JSON / Text / Stream) über alle Provider:
  - Google Gemini      -> GEMINI_API_KEY (google-genai SDK)
  - Groq               -> GROQ_API_KEY
  - OpenRouter         -> OPENROUTER_API_KEY + OPENROUTER_API_KEY_BACKUP
  - Mistral            -> MISTRAL_API_KEY
  - GitHub Models      -> GITHUB_MODELS_TOKEN ("Copilot-Modelle", free tier)
  - Cerebras           -> CEREBRAS_API_KEY (free tier, extrem schnell)

Backup-Keys: Für jeden OpenAI-kompatiblen Provider wird zusätzlich
`<ENV>_BACKUP` geprüft. Ist der primäre Key rate-limited (z.B. Tages-Maximum
bei OpenRouter), wird die komplette Modell-Kette mit dem Backup-Key wiederholt.
"""
import os
import re
import asyncio
import logging
from collections import deque
from typing import AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Erlaubte Modelle je Provider (alle mit kostenlosem Free-Tier).
# WICHTIG: Slugs wurden live gegen die Provider-Kataloge verifiziert (Juni 2026).
# Der Modell-Wächter (services/ai_model_watch.py) prüft sie wöchentlich erneut.
ALLOWED_MODELS = {
    "gemini": [
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
    ],
    "groq": [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.6-27b",
    ],
    "openrouter": [
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
        "openai/gpt-oss-20b:free",
        "nvidia/nemotron-nano-9b-v2:free",
        # Bezahlt, extrem günstig / stark (OpenRouter-Credits nötig, KEIN
        # neuer Key). Alle in PAID_MODELS_NO_FALLBACK -> nie Auto-Fallback.
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro-0813",
        "z-ai/glm-5.2",
        "qwen/qwen3.7-flash",
        "x-ai/grok-4.20",
    ],
    "mistral": [
        "mistral-small-latest",
        "ministral-8b-latest",
    ],
    # Cerebras – free tier, sehr schnelle Inferenz
    "cerebras": [
        "gpt-oss-120b",
        "gemma-4-31b",
    ],
}

# Vom Modell-Wächter live entdeckte, NEUE Modelle (provider -> [slugs]).
# Werden zusätzlich zu ALLOWED_MODELS erlaubt und sind im Frontend auswählbar.
DYNAMIC_MODELS: Dict[str, List[str]] = {}


def set_dynamic_models(mapping: Dict[str, List[str]]):
    """Entdeckte Modelle freischalten (aus settings/model_watch geladen)."""
    DYNAMIC_MODELS.clear()
    for prov, models in (mapping or {}).items():
        base = ALLOWED_MODELS.get(prov, [])
        extra = [m for m in (models or []) if m not in base]
        if extra:
            DYNAMIC_MODELS[prov] = extra


def allowed_models(provider: str) -> List[str]:
    """Fest konfigurierte + dynamisch entdeckte Modelle eines Providers."""
    return ALLOWED_MODELS.get(provider, []) + DYNAMIC_MODELS.get(provider, [])


# Tote/umbenannte Slugs -> funktionierender Ersatz (gleiche Leistungsklasse).
# Greift bei gespeicherten Konfigurationen (Engine-Hauptmodell, KI-Team-Rollen).
# GitHub Models wurde vom Anbieter eingestellt (HTTP 410 "retirement") –
# alle GitHub-Modelle wandern auf gleichwertige, verifizierte Alternativen.
MODEL_MIGRATIONS = {
    "qwen/qwen3-32b": ("groq", "qwen/qwen3.6-27b"),
    "deepseek/deepseek-r1:free": ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
    "qwen/qwen3-235b-a22b:free": ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    "open-mistral-nemo": ("mistral", "ministral-8b-latest"),
    "llama-3.3-70b": ("cerebras", "gpt-oss-120b"),
    "qwen-3-32b": ("cerebras", "gemma-4-31b"),
    # 17.06.2026: von Groq bzw. Cerebras entfernte Modelle -> beste Nachfolger
    "llama-3.3-70b-versatile": ("groq", "openai/gpt-oss-120b"),
    "llama-3.1-8b-instant": ("groq", "openai/gpt-oss-20b"),
    "zai-glm-4.7": ("cerebras", "gemma-4-31b"),
    "openai/gpt-4.1": ("groq", "openai/gpt-oss-120b"),
    "openai/gpt-4.1-mini": ("groq", "openai/gpt-oss-120b"),
    "openai/gpt-4o-mini": ("gemini", "gemini-3.5-flash-lite"),
}

# Fallback-Reihenfolge innerhalb eines Providers (bei 429 nächstes Modell).
# Bezahlte Modelle (PAID_MODELS_NO_FALLBACK) stehen bewusst NICHT in der
# Fallback-Kette: sie werden nur genutzt, wenn explizit ausgewählt – ein
# Rate-Limit darf nie unbemerkt auf ein kostenpflichtiges Modell ausweichen.
PAID_MODELS_NO_FALLBACK = {"deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro-0813",
                           "z-ai/glm-5.2", "qwen/qwen3.7-flash", "x-ai/grok-4.20"}

FALLBACK_ORDER = {
    "gemini": ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.1-pro-preview",
               "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
    "groq": ["openai/gpt-oss-120b", "qwen/qwen3.6-27b", "openai/gpt-oss-20b"],
    "openrouter": [
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
        "openai/gpt-oss-20b:free",
        "nvidia/nemotron-nano-9b-v2:free",
    ],
    "mistral": ["mistral-small-latest", "ministral-8b-latest"],
    "cerebras": ["gpt-oss-120b", "gemma-4-31b"],
}

# OpenAI-kompatible Backends: base_url + Env-Keys (Reihenfolge = Prio,
# Backup-Key wird automatisch als "<ENV>_BACKUP" ergänzt).
OPENAI_COMPAT_PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "env_keys": ["GROQ_API_KEY"]},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "env_keys": ["OPENROUTER_API_KEY"]},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "env_keys": ["MISTRAL_API_KEY"]},
    "cerebras": {"base_url": "https://api.cerebras.ai/v1", "env_keys": ["CEREBRAS_API_KEY"]},
}

# Modell-Stärke (1 = leicht, 2 = mittel, 3 = stark). Lektionen & Analysen
# stärkerer Modelle bekommen im System mehr Gewicht.
MODEL_WEIGHTS = {
    "gemini-3.1-pro-preview": 3,
    "gemini-3.5-flash": 2,
    "gemini-3.6-flash": 2,
    "gemini-3.5-flash-lite": 1,
    "gemini-3.1-flash-lite": 1,
    "llama-3.3-70b-versatile": 3,
    "llama-3.1-8b-instant": 1,
    "openai/gpt-oss-120b": 3,
    "openai/gpt-oss-20b": 1,
    "qwen/qwen3.6-27b": 2,
    "nvidia/nemotron-3-super-120b-a12b:free": 2,
    "nvidia/nemotron-3-ultra-550b-a55b:free": 3,
    "nvidia/nemotron-3.5-lightning:free": 3,
    "deepseek/deepseek-v4-flash": 3,
    "deepseek/deepseek-v4-pro-0813": 3,
    "z-ai/glm-5.2": 3,
    "qwen/qwen3.7-flash": 2,
    "x-ai/grok-4.20": 3,
    "google/gemma-4-31b-it:free": 2,
    "openai/gpt-oss-20b:free": 1,
    "nvidia/nemotron-nano-9b-v2:free": 1,
    "mistral-small-latest": 2,
    "ministral-8b-latest": 1,
    "gpt-oss-120b": 3,
    "zai-glm-4.7": 2,
    "gemma-4-31b": 2,
}

WEIGHT_LABELS = {1: "basis", 2: "mittel", 3: "hoch"}

# Token-Budget pro Anfrage (Free-Tier TPM-Limits): Prompts über dem Budget
# liefern beim Anbieter einen 413 ("Request too large") – solche Modelle
# werden in der Kette ÜBERSPRUNGEN statt einen Fehler zu produzieren.
# Sichtbar im KI-Status als "übersprungen – Prompt zu groß".
MODEL_INPUT_TOKEN_BUDGET = {
    "groq/llama-3.1-8b-instant": 5000,
    "groq/llama-3.3-70b-versatile": 10000,
    "groq/openai/gpt-oss-120b": 7000,
    "groq/openai/gpt-oss-20b": 7000,
    "groq/qwen/qwen3.6-27b": 7000,
}

# Aus 413-Fehlern gelernte Budgets (Laufzeit): Modell -> max. Input-Tokens.
_learned_budget: Dict[str, int] = {}


def estimate_tokens(text: str) -> int:
    """Grobe, konservative Token-Schätzung (~3 Zeichen/Token)."""
    return max(1, len(text or "") // 3)


def input_budget(provider: str, model: str) -> Optional[int]:
    key = f"{provider}/{model}"
    static = MODEL_INPUT_TOKEN_BUDGET.get(key)
    learned = _learned_budget.get(key)
    if static and learned:
        return min(static, learned)
    return learned or static


def learn_budget(provider: str, model: str, est_tokens: int):
    """Nach einem 413: Budget für dieses Modell dauerhaft absenken."""
    key = f"{provider}/{model}"
    new = max(1000, int(est_tokens * 0.85))
    cur = _learned_budget.get(key)
    _learned_budget[key] = min(cur, new) if cur else new


def is_too_large_error(err: Exception) -> bool:
    s = str(err).lower()
    return ("413" in s or "request too large" in s or "prompt is too long" in s
            or "context_length_exceeded" in s or "maximum context length" in s
            or "tokens_limit_reached" in s)


def migrate_model(provider: Optional[str], model: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Tote Slugs auf funktionierende Nachfolger mappen (sonst unverändert)."""
    if not model:
        return provider, model
    if model in MODEL_MIGRATIONS:
        return MODEL_MIGRATIONS[model]
    p = provider_for_model(model)
    if p:
        return p, model
    return provider, model


def model_weight(model: Optional[str]) -> int:
    return MODEL_WEIGHTS.get(model or "", 2)


def weight_label(model: Optional[str]) -> str:
    return WEIGHT_LABELS[model_weight(model)]


def provider_for_model(model: str) -> Optional[str]:
    for prov, models in ALLOWED_MODELS.items():
        if model in models:
            return prov
    for prov, models in DYNAMIC_MODELS.items():
        if model in models:
            return prov
    return None


def is_rate_limit_error(err: Exception) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("429", "resource_exhausted", "quota", "rate limit",
                                "ratelimit", "too many requests"))


def is_payment_error(err: Exception) -> bool:
    """402 Payment Required: Gratis-Kontingent erschöpft / Guthaben nötig
    (z.B. Cerebras). Verhalten wie Rate-Limit (nächster Key/Modell), aber
    mit klarer Log-Meldung, damit es nicht wie ein Tageslimit aussieht."""
    s = str(err).lower()
    return any(k in s for k in ("error code: 402", "payment required",
                                "insufficient credit", '"code":402',
                                "payment_required"))


def is_empty_response_error(err: Exception) -> bool:
    """Transient: Free-Modell (v.a. OpenRouter) liefert 200 mit leeren
    choices/Content – typisch Überlastung des Upstream-Providers.
    Wird mit einem Retry + Key-Wechsel behandelt statt Modell aufzugeben."""
    return "leere antwort" in str(err).lower()


def _backup_env_names(base: str) -> List[str]:
    """Alle Backup-Env-Namen eines Keys: _BACKUP, _BACKUP2, _BACKUP3, … (unbegrenzt).

    Es wird dynamisch die Umgebung gescannt – beliebig viele Keys können in der
    .env ergänzt werden (nur die Zahl hinter BACKUP hochzählen), ohne Code-Änderung."""
    pat = re.compile(rf"^{re.escape(base)}_BACKUP(\d+)$")
    nums = sorted(int(m.group(1)) for k in os.environ for m in [pat.match(k)] if m)
    # _BACKUP1 wird mitgenommen (manche Envs zählen ab 1 statt ab 2)
    return [f"{base}_BACKUP"] + [f"{base}_BACKUP{i}" for i in nums]


def provider_keys(provider: str) -> List[str]:
    """Alle verfügbaren Keys eines Providers in Prio-Reihenfolge
    (primär, backup, backup2, …)."""
    keys: List[str] = []
    if provider == "gemini":
        env_names = ["GEMINI_API_KEY", "GOOGLE_API_KEY"] + _backup_env_names("GEMINI_API_KEY")
    else:
        meta = OPENAI_COMPAT_PROVIDERS.get(provider)
        if not meta:
            return []
        env_names = []
        for e in meta["env_keys"]:
            env_names.append(e)
            env_names += _backup_env_names(e)
    for name in env_names:
        v = os.environ.get(name)
        if v and v not in keys:
            keys.append(v)
    return keys


def primary_key(provider: str) -> Optional[str]:
    keys = provider_keys(provider)
    return keys[0] if keys else None


def available_providers() -> Dict[str, bool]:
    out = {"gemini": bool(provider_keys("gemini"))}
    for p in OPENAI_COMPAT_PROVIDERS:
        out[p] = bool(provider_keys(p))
    return out


def backup_keys_info() -> Dict[str, bool]:
    """Welche Provider haben mindestens einen Backup-Key gesetzt? (fürs Frontend)"""
    out = {}
    out["gemini"] = any(os.environ.get(n) for n in _backup_env_names("GEMINI_API_KEY"))
    for p, meta in OPENAI_COMPAT_PROVIDERS.items():
        out[p] = any(os.environ.get(n) for e in meta["env_keys"]
                     for n in _backup_env_names(e))
    return out


def backup_key_counts() -> Dict[str, int]:
    """Anzahl Backup-Keys pro Provider (Primärkey zählt nicht mit)."""
    out = {}
    for p in list(OPENAI_COMPAT_PROVIDERS) + ["gemini"]:
        out[p] = max(0, len(provider_keys(p)) - 1)
    return out


def same_provider_chain(provider: str, preferred: Optional[str]) -> List[Tuple[str, str]]:
    """Bevorzugtes Modell zuerst, dann die restlichen des Providers."""
    allowed = allowed_models(provider)
    if not allowed:
        return []
    pref = preferred if preferred in allowed else allowed[0]
    order = FALLBACK_ORDER.get(provider, allowed)
    chain = [pref] + [m for m in order if m != pref]
    return [(provider, m) for m in chain if m in allowed]


# ---------------- Provider-Health (Limit-/Fallback-Anzeige) ----------------
# Sichtbar machen, WARUM die KI auf ein anderes Modell ausgewichen ist.
RATE_LIMIT_COOLDOWN_S = 30 * 60

_health: Dict[str, Dict] = {}     # "provider/model" -> {status, ts, detail, key_index}
_last_call: Dict = {}             # letzter erfolgreicher Aufruf inkl. Fallback-Info


def _now() -> float:
    import time as _t
    return _t.time()


# ---------------- Key-Level-Tracking (pro einzelnem API-Key) ----------------
# Beantwortet transparent: WELCHE Keys eines Providers sind gerade im Limit?
# Frisch limitierte Keys werden kurz übersprungen (weniger Latenz, weniger
# 429-Spam) – sind ALLE Keys im Cooldown, wird trotzdem jeder erneut probiert.
KEY_LIMIT_COOLDOWN_S = 10 * 60
_key_limited: Dict[str, Dict[int, Dict]] = {}   # provider -> key_index -> {ts, detail}
_rr_start: Dict[str, int] = {}                  # provider -> Round-Robin-Zähler
# Nach so vielen 429 in FOLGE (verschiedene Keys, gleicher Call) werden die
# restlichen Keys übersprungen: die Limits gelten bei den Providern pro
# KONTO/ORGANISATION – Keys aus demselben Konto teilen sich EIN Kontingent.
SAME_ORG_429_STREAK = 3


def mark_key_limited(provider: str, key_index: int, detail: str = ""):
    _key_limited.setdefault(provider, {})[key_index] = {
        "ts": _now(), "detail": str(detail)[:160]}


def clear_key_limited(provider: str, key_index: int):
    _key_limited.get(provider, {}).pop(key_index, None)


def usable_key_indices(provider: str, n_keys: int, now: Optional[float] = None) -> List[int]:
    """Key-Indizes in Prio-Reihenfolge; frisch rate-limitierte werden übersprungen.
    Der Startpunkt rotiert pro Aufruf (Round-Robin), damit sich die Last über
    Keys aus VERSCHIEDENEN Konten verteilt statt immer Key 1 zu verbrennen.
    Fallback: sind alle im Cooldown, werden wieder alle geliefert."""
    now = _now() if now is None else now
    limited = _key_limited.get(provider) or {}
    fresh = {i for i, info in limited.items()
             if (now - float(info.get("ts", 0))) < KEY_LIMIT_COOLDOWN_S}
    idxs = [i for i in range(n_keys) if i not in fresh]
    idxs = idxs or list(range(n_keys))
    if len(idxs) > 1:
        off = _rr_start.get(provider, 0) % len(idxs)
        _rr_start[provider] = off + 1
        idxs = idxs[off:] + idxs[:off]
    return idxs


def key_status() -> Dict[str, Dict]:
    """Pro Provider: wie viele Keys existieren und welche sind aktuell im Limit."""
    now = _now()
    out: Dict[str, Dict] = {}
    for p in list(OPENAI_COMPAT_PROVIDERS) + ["gemini"]:
        n = len(provider_keys(p))
        if not n:
            continue
        lim = []
        for idx, info in sorted((_key_limited.get(p) or {}).items()):
            age = now - float(info.get("ts", 0))
            if age < KEY_LIMIT_COOLDOWN_S and idx < n:
                lim.append({"index": idx + 1, "age_s": int(age),
                            "detail": str(info.get("detail", ""))[:120]})
        out[p] = {"total": n, "limited": len(lim), "limited_keys": lim}
    return out


def record_result(provider: str, model: str, status: str, detail: str = "",
                  key_index: int = 0, role: Optional[str] = None,
                  requested: Optional[str] = None):
    """Ergebnis eines Modell-Aufrufs festhalten (ok | rate_limited | error)."""
    role = role or _current_role.get("role")
    _health[f"{provider}/{model}"] = {
        "provider": provider, "model": model, "status": status,
        "detail": str(detail)[:200], "key_index": key_index, "ts": _now(),
        "role": role,
    }
    if status != "ok":
        # Verlauf für die UI: WAS ist ausgefallen (Rate-Limit / Budget / Fehler),
        # WELCHER Assistent (Rolle) war betroffen.
        _recent_failures.append({
            "ts": _now(), "provider": provider, "model": model, "role": role,
            "reason": status, "detail": str(detail)[:200],
        })
        # Gleiche Details zusätzlich in die Website-Glocke (Cooldown gegen Spam)
        try:
            import asyncio
            from services.notifications import notify_model_failure
            asyncio.get_running_loop().create_task(
                notify_model_failure(role, provider, model, status, str(detail)))
        except Exception:  # noqa: BLE001 – kein Loop (z.B. Tests) -> still
            pass
    if status == "ok":
        _last_call.update({
            "provider": provider, "model": model, "role": role,
            "requested_model": requested, "key_index": key_index,
            "fallback": bool((requested and requested != model) or key_index > 0),
            "ts": _now(),
        })
        # Aktive Fallbacks pro Rolle: Welcher Assistent arbeitet gerade NICHT
        # mit seinem Wunsch-Modell? (UI: Warnfenster im AI-Trading-Panel)
        if role:
            if _last_call["fallback"]:
                _role_fallbacks[role] = {
                    "role": role, "provider": provider, "model": model,
                    "requested_model": requested, "key_index": key_index, "ts": _now(),
                }
            else:
                _role_fallbacks.pop(role, None)
        if requested and requested != model:
            # Fallback wurde nötig -> festhalten, worauf ausgewichen wurde
            for f in reversed(_recent_failures):
                if f.get("role") == role and not f.get("fallback_used"):
                    f["fallback_used"] = f"{provider}/{model}"
                else:
                    break


_recent_failures: deque = deque(maxlen=40)
_current_role: Dict[str, Optional[str]] = {"role": None}
# role -> letzter erfolgreicher Call, der NICHT das Wunsch-Modell nutzte
_role_fallbacks: Dict[str, Dict] = {}


def set_current_role(role: Optional[str]):
    """Vom KI-Team gesetzt, damit Ausfälle der richtigen Rolle zugeordnet werden."""
    _current_role["role"] = role


def health_status() -> Dict:
    """Aufbereiteter Zustand für /api/ai/status und die UI."""
    now = _now()
    limited, errors, skipped, models = [], [], [], {}
    for key, h in _health.items():
        age = now - float(h.get("ts", 0))
        entry = {**h, "age_s": int(age)}
        if h.get("status") == "rate_limited":
            entry["cooldown_left_s"] = max(0, int(RATE_LIMIT_COOLDOWN_S - age))
            if entry["cooldown_left_s"] > 0:
                limited.append(entry)
        elif h.get("status") == "error" and age < RATE_LIMIT_COOLDOWN_S:
            errors.append(entry)
        elif h.get("status") == "skipped_too_large" and age < RATE_LIMIT_COOLDOWN_S:
            skipped.append(entry)
        models[key] = entry
    recent = []
    for f in list(_recent_failures)[-15:][::-1]:
        recent.append({**f, "age_s": int(now - float(f.get("ts", 0)))})
    # Aktive Fallbacks (max. 6h alt): Assistent läuft gerade auf Ersatz-Modell
    active_fb = []
    for r, fb in list(_role_fallbacks.items()):
        age = now - float(fb.get("ts", 0))
        if age > 6 * 3600:
            _role_fallbacks.pop(r, None)
            continue
        active_fb.append({**fb, "age_s": int(age)})
    active_fb.sort(key=lambda x: x["age_s"])
    last = dict(_last_call)
    if last.get("ts"):
        last["age_s"] = int(now - last["ts"])

    # Gruppierung pro Anbieter (User-Wunsch): Ursache ist fast immer das
    # Key-Kontingent des Anbieters, nicht das einzelne Modell. Statt N Zeilen
    # (eine je Modell) EINE Warnung je Anbieter inkl. betroffener Assistenten.
    def _group_by_provider(entries: List[Dict]) -> List[Dict]:
        groups: Dict[str, Dict] = {}
        for e in entries:
            g = groups.setdefault(e["provider"], {
                "provider": e["provider"], "models": [], "roles": [],
                "count": 0, "detail": e.get("detail") or "",
                "cooldown_left_s": 0, "key_index": e.get("key_index", 0),
            })
            g["count"] += 1
            if e["model"] not in g["models"]:
                g["models"].append(e["model"])
            if e.get("role") and e["role"] not in g["roles"]:
                g["roles"].append(e["role"])
            g["cooldown_left_s"] = max(g["cooldown_left_s"],
                                       int(e.get("cooldown_left_s") or 0))
            g["key_index"] = max(g["key_index"], e.get("key_index", 0))
        # Betroffene Rollen zusätzlich aus dem Ausfall-Verlauf ergänzen
        # (ein _health-Eintrag merkt sich nur die LETZTE Rolle pro Modell).
        for f in _recent_failures:
            if now - float(f.get("ts", 0)) > RATE_LIMIT_COOLDOWN_S:
                continue
            g = groups.get(f.get("provider"))
            if g and f.get("role") and f["role"] not in g["roles"]:
                g["roles"].append(f["role"])
        return list(groups.values())

    return {
        "models": models,
        "rate_limited": limited,
        "errors": errors,
        "skipped_too_large": skipped,
        "rate_limited_grouped": _group_by_provider(limited),
        "skipped_grouped": _group_by_provider(skipped),
        "last_call": last,
        "recent_failures": recent,
        "active_fallbacks": active_fb,
        "fallback_active": bool(last.get("fallback")),
        "providers": available_providers(),
        "backup_keys": backup_keys_info(),
        "key_status": key_status(),
    }


# ---------------- client caches ----------------
_gemini_clients: Dict[str, object] = {}   # key -> genai.Client
_oai_clients: Dict[Tuple[str, str], object] = {}  # (provider, key) -> AsyncOpenAI


def _gemini_client(key: str):
    cl = _gemini_clients.get(key)
    if cl is None:
        from google import genai
        cl = genai.Client(api_key=key)
        _gemini_clients[key] = cl
    return cl


def _oai_client(provider: str, key: str):
    ck = (provider, key)
    cl = _oai_clients.get(ck)
    if cl is None:
        from openai import AsyncOpenAI
        meta = OPENAI_COMPAT_PROVIDERS[provider]
        default_headers = None
        if provider == "openrouter":
            default_headers = {
                "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://krypto-alert.local"),
                "X-Title": os.environ.get("OPENROUTER_TITLE", "Krypto Alert KI Trader"),
            }
        cl = AsyncOpenAI(base_url=meta["base_url"], api_key=key, default_headers=default_headers)
        _oai_clients[ck] = cl
    return cl


# ---------------- generation ----------------
async def _gemini_generate(model: str, key: str, prompt: str, system: str,
                           temperature: float, json_mode: bool) -> str:
    from google.genai import types
    client = _gemini_client(key)
    cfg = dict(system_instruction=system, temperature=temperature)
    if json_mode:
        cfg["response_mime_type"] = "application/json"
    resp = await client.aio.models.generate_content(
        model=model, contents=prompt, config=types.GenerateContentConfig(**cfg))
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError(f"Leere Antwort von gemini/{model}")
    return text


async def _oai_generate(provider: str, model: str, key: str, prompt: str, system: str,
                        temperature: float, json_mode: bool) -> str:
    client = _oai_client(provider, key)
    kwargs = dict(model=model,
                  messages=[{"role": "system", "content": system},
                            {"role": "user", "content": prompt}],
                  temperature=temperature)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = await client.chat.completions.create(**kwargs)
    except Exception as inner:
        if json_mode and ("response_format" in str(inner).lower() or "json_object" in str(inner).lower()):
            kwargs.pop("response_format", None)
            resp = await client.chat.completions.create(**kwargs)
        else:
            raise
    # Defensive: einige OpenRouter-Free-Modelle liefern choices=None/leer
    # ('NoneType' object is not subscriptable) – als klarer Fehler behandeln.
    choices = getattr(resp, "choices", None) or []
    if not choices or getattr(choices[0], "message", None) is None:
        raise RuntimeError(f"Leere Antwort (keine choices) von {provider}/{model}")
    text = (choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError(f"Leere Antwort von {provider}/{model}")
    return text


async def generate_chain(chain: List[Tuple[str, str]], prompt: str, system: str,
                         temperature: float = 0.4, json_mode: bool = True) -> Tuple[str, str, str]:
    """Iteriert (provider, model)-Kette; pro Provider alle Keys (primär -> backup).

    Rate-Limits führen zum nächsten Key bzw. Modell; andere Fehler zum nächsten
    Ketten-Eintrag. Rückgabe: (text, provider, model). Wirft den letzten Fehler,
    wenn die gesamte Kette scheitert."""
    last_err: Optional[Exception] = None
    tried_any = False
    failed_models: List[str] = []
    failed_providers = set()
    # Detail-Analyse für die Ausfall-Meldung: WARUM ist jedes Modell gescheitert
    failure_details: List[Dict] = []

    def _fail_detail(prov: str, mdl: str, reason: str, detail: str):
        failure_details.append({"model": f"{prov}/{mdl}", "reason": reason,
                                "detail": str(detail)[:200]})

    est_tokens = estimate_tokens(system) + estimate_tokens(prompt)
    for provider, model in chain:
        keys = provider_keys(provider)
        if not keys:
            continue
        budget = input_budget(provider, model)
        if budget and est_tokens > budget:
            # Prompt passt nicht ins Free-Tier-Budget -> Modell überspringen
            # (sichtbar im KI-Status), nächstes Modell der Kette probieren.
            logger.info(f"{provider}/{model}: übersprungen – Prompt ~{est_tokens} "
                        f"Tokens > Budget {budget}")
            record_result(provider, model, "skipped_too_large",
                          f"Prompt ~{est_tokens} Tokens > Budget {budget}")
            _fail_detail(provider, model, "skipped_too_large",
                         f"Prompt ~{est_tokens} Tokens > Budget {budget}")
            continue
        idxs = usable_key_indices(provider, len(keys))
        last_idx = idxs[-1]
        streak_429 = 0
        for i in idxs:
            key = keys[i]
            tried_any = True
            try:
                async def _gen_once():
                    if provider == "gemini":
                        return await _gemini_generate(model, key, prompt, system, temperature, json_mode)
                    return await _oai_generate(provider, model, key, prompt, system, temperature, json_mode)
                try:
                    text = await _gen_once()
                except Exception as e1:
                    # Leere Antwort ist meist transient (Free-Tier überlastet):
                    # einmaliger Retry auf demselben Key, dann Key-Wechsel.
                    if not is_empty_response_error(e1):
                        raise
                    logger.info(f"{provider}/{model}: leere Antwort – einmaliger Retry in 2s…")
                    await asyncio.sleep(2)
                    text = await _gen_once()
                if i > 0:
                    logger.warning(f"AI: Backup-Key für {provider} genutzt ({model})")
                clear_key_limited(provider, i)
                record_result(provider, model, "ok", key_index=i,
                              requested=chain[0][1] if chain else None)
                # KI-Ausfall-Meldung: Primär- UND Backup-Provider gescheitert,
                # ein Notfall-Fallback musste übernehmen.
                if len(failed_providers - {provider}) >= 2:
                    try:
                        from services.notifications import notify_ai_failure
                        await notify_ai_failure(
                            _current_role.get("role") or "KI-Anfrage",
                            failed_models, f"{provider}/{model}",
                            failures=failure_details)
                    except Exception:
                        pass
                return text, provider, model
            except Exception as e:
                last_err = e
                if is_too_large_error(e):
                    # 413/Kontext-Limit: Budget lernen + Modell überspringen –
                    # zählt NICHT als Provider-Ausfall (nur Prompt zu groß).
                    learn_budget(provider, model, est_tokens)
                    logger.warning(f"{provider}/{model}: Prompt zu groß (413) – "
                                   f"Budget gelernt (~{est_tokens} Tokens), weiter…")
                    record_result(provider, model, "skipped_too_large",
                                  f"Prompt ~{est_tokens} Tokens zu groß (413)", key_index=i)
                    _fail_detail(provider, model, "skipped_too_large",
                                 f"Prompt ~{est_tokens} Tokens zu groß (413)")
                    break  # nächstes Modell – gleicher Prompt scheitert bei jedem Key
                if is_rate_limit_error(e) or is_payment_error(e):
                    reason = ("Free-Kontingent erschöpft (402) – Backup übernimmt"
                              if is_payment_error(e) else "rate-limited")
                    mark_key_limited(provider, i, str(e))
                    logger.warning(f"{provider}/{model} {reason} (Key {i + 1}/{len(keys)}), weiter…")
                    record_result(provider, model, "rate_limited", str(e), key_index=i)
                    streak_429 += 1
                    # Kontingente gelten pro KONTO/ORGANISATION: liefern mehrere
                    # Keys in Folge 429, teilen sie sich fast sicher EIN Konto –
                    # restliche Keys überspringen (spart Latenz + 429-Spam).
                    if streak_429 >= SAME_ORG_429_STREAK and i != last_idx:
                        remaining = [j for j in idxs if idxs.index(j) > idxs.index(i)]
                        for j in remaining:
                            mark_key_limited(provider, j,
                                             "übersprungen – Kontingent gilt pro "
                                             "Konto/Organisation (Keys teilen sich das Limit)")
                        failed_models.append(f"{provider}/{model}")
                        failed_providers.add(provider)
                        _fail_detail(provider, model, "rate_limited",
                                     f"{streak_429} Keys in Folge im Limit – Hinweis: "
                                     f"{provider}-Kontingente gelten pro KONTO, Backup-Keys "
                                     f"aus demselben Konto teilen sich EIN Limit; "
                                     f"{len(remaining)} weitere Keys übersprungen")
                        logger.warning(
                            f"{provider}/{model}: {streak_429} Keys in Folge im Limit – "
                            f"Kontingent gilt pro Konto/Organisation, "
                            f"{len(remaining)} restliche Keys übersprungen")
                        break
                    # Ausfall-Meldung erst, wenn ALLE Keys (primär + backup)
                    # dieses Providers erschöpft sind – nicht beim ersten Limit.
                    if i == last_idx:
                        failed_models.append(f"{provider}/{model}")
                        failed_providers.add(provider)
                        _fail_detail(provider, model, "rate_limited",
                                     f"alle {len(keys)} Key(s) im Limit: {str(e)[:140]}")
                    continue
                if is_empty_response_error(e):
                    streak_429 = 0
                    logger.warning(f"{provider}/{model}: leere Antwort trotz Retry "
                                   f"(Modell überlastet) – Key {i + 1}/{len(keys)}, weiter…")
                    record_result(provider, model, "error",
                                  "Leere Antwort trotz Retry – Free-Modell aktuell "
                                  "überlastet, Fallback übernimmt", key_index=i)
                    if i == last_idx:
                        failed_models.append(f"{provider}/{model}")
                        failed_providers.add(provider)
                        _fail_detail(provider, model, "empty_response",
                                     "Leere Antwort (Modell überlastet) trotz Retry auf allen Keys")
                    continue  # nächster Key = andere Upstream-Route, statt Modell aufgeben
                failed_models.append(f"{provider}/{model}")
                failed_providers.add(provider)
                logger.warning(f"{provider}/{model} Fehler: {str(e)[:150]} – nächstes Modell…")
                record_result(provider, model, "error", str(e), key_index=i)
                _fail_detail(provider, model, "error", str(e))
                break  # anderer Fehler -> nächstes Modell, nicht nächster Key
    if not tried_any:
        raise RuntimeError("Kein API-Key für die konfigurierten Provider gesetzt")
    try:
        from services.notifications import notify_ai_failure
        await notify_ai_failure(_current_role.get("role") or "KI-Anfrage",
                                failed_models, None, failures=failure_details)
    except Exception:
        pass
    raise last_err or RuntimeError("Alle Modelle der Kette fehlgeschlagen")


async def stream_chain(chain: List[Tuple[str, str]], prompt: str, system: str,
                       temperature: float = 0.6):
    """Streaming über die Kette. Yields ('token', str) Chunks, danach einmal
    ('meta', (provider, model)). Bei komplettem Scheitern ('error', msg)."""
    last_err: Optional[Exception] = None
    tried_any = False
    est_tokens = estimate_tokens(system) + estimate_tokens(prompt)
    for provider, model in chain:
        keys = provider_keys(provider)
        if not keys:
            continue
        budget = input_budget(provider, model)
        if budget and est_tokens > budget:
            record_result(provider, model, "skipped_too_large",
                          f"Prompt ~{est_tokens} Tokens > Budget {budget}")
            continue
        for i in usable_key_indices(provider, len(keys)):
            key = keys[i]
            tried_any = True
            streamed = False
            try:
                if provider == "gemini":
                    from google.genai import types
                    client = _gemini_client(key)
                    stream = await client.aio.models.generate_content_stream(
                        model=model, contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system, temperature=temperature))
                    async for chunk in stream:
                        part = getattr(chunk, "text", None)
                        if part:
                            streamed = True
                            yield ("token", part)
                else:
                    client = _oai_client(provider, key)
                    stream = await client.chat.completions.create(
                        model=model,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": prompt}],
                        temperature=temperature, stream=True)
                    async for chunk in stream:
                        try:
                            part = chunk.choices[0].delta.content
                        except Exception:
                            part = None
                        if part:
                            streamed = True
                            yield ("token", part)
                record_result(provider, model, "ok", key_index=i,
                              requested=chain[0][1] if chain else None)
                clear_key_limited(provider, i)
                yield ("meta", (provider, model))
                return
            except Exception as e:
                last_err = e
                if streamed:
                    yield ("error", f"KI-Fehler: {str(e)[:200]}")
                    return
                if is_too_large_error(e):
                    learn_budget(provider, model, est_tokens)
                    record_result(provider, model, "skipped_too_large",
                                  f"Prompt ~{est_tokens} Tokens zu groß (413)", key_index=i)
                    break
                if is_rate_limit_error(e) or is_payment_error(e):
                    reason = ("Free-Kontingent erschöpft (402) – Backup übernimmt"
                              if is_payment_error(e) else "rate-limited")
                    mark_key_limited(provider, i, str(e))
                    logger.warning(f"{provider}/{model} chat {reason} (Key {i + 1}), weiter…")
                    record_result(provider, model, "rate_limited", str(e), key_index=i)
                    streak_429 += 1
                    if streak_429 >= SAME_ORG_429_STREAK:
                        # Kontingent gilt pro Konto/Organisation – Rest überspringen
                        for j in idxs:
                            if idxs.index(j) > idxs.index(i):
                                mark_key_limited(provider, j,
                                                 "übersprungen – Kontingent gilt pro "
                                                 "Konto/Organisation (Keys teilen sich das Limit)")
                        break
                    continue
                logger.warning(f"{provider}/{model} chat Fehler: {str(e)[:150]}")
                record_result(provider, model, "error", str(e), key_index=i)
                break
    if not tried_any:
        yield ("error", "Kein API-Key für die konfigurierten Provider gesetzt")
        return
    yield ("error", f"Alle Modelle rate-limited/fehlgeschlagen. {str(last_err)[:150]}")


# ---------------- Modell-Katalog-Verifikation (Modell-Wächter) ----------------
# Nicht-Chat- bzw. Spezial-Modelle, die bei der Neu-Entdeckung ignoriert werden.
_NEW_MODEL_EXCLUDE = ("whisper", "tts", "audio", "embed", "moderation", "guard",
                      "ocr", "image", "imagen", "veo", "aqa", "vision", "live",
                      "transcribe", "rerank", "voxtral", "playai", "saba",
                      "allam", "compound", "safety", "-exp", "orpheus",
                      "canopylabs", "customtools", "speech", "realtime")


def _interesting_new(provider: str, live: set, configured: List[str]) -> List[str]:
    """Neue, fürs Trading brauchbare Chat-Modelle aus dem Live-Katalog filtern."""
    out = []
    for mid in live:
        m = str(mid)
        low = m.lower()
        if m in configured or not m:
            continue
        if any(x in low for x in _NEW_MODEL_EXCLUDE):
            continue
        if provider == "gemini":
            # Nur echte gemini-Chat-Modelle, keine datierten Preview-Duplikate
            if not low.startswith("gemini-") or re.search(r"-\d{2}-\d{4}$|-\d{3,}$", low):
                continue
        if provider == "openrouter" and not low.endswith(":free"):
            continue  # nur kostenlose Modelle automatisch anbieten
        if provider == "mistral" and not low.endswith("-latest"):
            continue  # Mistral: nur stabile -latest-Aliasse, keine Datums-Duplikate
        out.append(m)
    return sorted(out)


async def verify_catalog() -> Dict:
    """Prüft alle konfigurierten Slugs gegen die Live-Kataloge der Provider.

    Rückgabe: {"dead": ["provider/model", …], "unverified": [provider, …],
    "providers": {provider: {"live": n, "dead": […]}}}. Provider ohne Key oder
    mit nicht erreichbarem Katalog landen in "unverified" (keine Falschmeldung)."""
    import httpx
    out = {"providers": {}, "dead": [], "unverified": [], "new": {}}
    async with httpx.AsyncClient(timeout=25) as client:
        for provider, models in ALLOWED_MODELS.items():
            key = primary_key(provider)
            if not key:
                out["unverified"].append(provider)
                continue
            try:
                if provider == "gemini":
                    r = await client.get(
                        "https://generativelanguage.googleapis.com/v1beta/models",
                        params={"key": key, "pageSize": 200})
                    r.raise_for_status()
                    live = {str(m.get("name", "")).split("/")[-1]
                            for m in r.json().get("models", [])}
                else:
                    base = OPENAI_COMPAT_PROVIDERS[provider]["base_url"]
                    r = await client.get(f"{base}/models",
                                         headers={"Authorization": f"Bearer {key}"})
                    r.raise_for_status()
                    data = r.json()
                    rows = data.get("data") if isinstance(data, dict) else data
                    live = {str(m.get("id")) for m in (rows or []) if isinstance(m, dict)}
                dead = [m for m in models if m not in live]
                out["providers"][provider] = {"live": len(live), "dead": dead}
                out["dead"] += [f"{provider}/{m}" for m in dead]
                out["new"][provider] = _interesting_new(provider, live, models)
            except Exception as e:
                logger.warning(f"verify_catalog({provider}) fehlgeschlagen: {str(e)[:120]}")
                out["unverified"].append(provider)
                out["providers"][provider] = {"error": str(e)[:120]}
    return out
