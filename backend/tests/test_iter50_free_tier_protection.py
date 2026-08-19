"""Iteration 50 – Guthaben-Schutz (Free-Tier-Garantie):
Der Trader nutzt bewusst nur Free-Tiers; Guthaben (OpenRouter/Gemini) darf
NUR für explizit ausgewählte Paid-Only-Modelle (z.B. DeepSeek) fließen.
Diese Tests schreiben die Garantien dauerhaft fest:
  1) Kein Paid-Modell in irgendeiner automatischen Fallback-Kette
  2) Rollen-Ketten (alle Presets) enthalten nie ein Paid-Modell von selbst
  3) Modell-Wächter bietet neue OpenRouter-Modelle nur mit :free-Suffix an
  4) Explizit gewählte Paid-Modelle bleiben nutzbar (bewusste Entscheidung)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import ai_providers as ap  # noqa: E402
from services.ai_roles import AIRoleManager, ROLE_PRESETS  # noqa: E402

ENGINE_CFG = {"provider": "gemini", "model": "gemini-3.5-flash"}


def test_no_paid_model_in_fallback_orders():
    leak = [m for ms in ap.FALLBACK_ORDER.values() for m in ms
            if m in ap.PAID_MODELS_NO_FALLBACK]
    assert leak == [], f"Paid-Modell in automatischer Fallback-Kette: {leak}"


def test_role_preset_chains_never_contain_paid_models():
    rm = AIRoleManager()
    leak = []
    for role in ROLE_PRESETS:
        for p, m in rm.chain(role, ENGINE_CFG):
            if m in ap.PAID_MODELS_NO_FALLBACK:
                leak.append((role, p, m))
    assert leak == [], f"Rollen-Kette würde ungefragt Guthaben verbrauchen: {leak}"


def test_discovery_only_offers_free_openrouter_models():
    live = {"deepseek/deepseek-v4-flash", "x-ai/grok-4.20",
            "foo/neu:free", "bar/teuer"}
    out = ap._interesting_new("openrouter", live, [])
    assert out == ["foo/neu:free"], \
        "Modell-Wächter darf nur :free-Modelle automatisch freischalten"


def test_explicit_paid_selection_still_works():
    """Bewusste Trader-Entscheidung: Paid-Modell als Haupt-Modell einer Rolle
    bleibt in der Kette – nur automatisches Ausweichen darauf ist verboten."""
    rm = AIRoleManager()
    rm.config["deep_analyst"].update({
        "provider": "openrouter", "model": "deepseek/deepseek-v4-flash"})
    chain = rm.chain("deep_analyst", ENGINE_CFG)
    assert chain[0] == ("openrouter", "deepseek/deepseek-v4-flash")
    # und dahinter ausschließlich Free-Fallbacks
    rest_paid = [m for _, m in chain[1:] if m in ap.PAID_MODELS_NO_FALLBACK]
    assert rest_paid == []


def test_paid_models_are_openrouter_only_and_in_catalog():
    """Paid-Set = nur OpenRouter-Slugs (Gemini/Groq/Cerebras/Mistral bleiben
    komplett Free-Tier) und alle sind explizit auswählbar (im Katalog)."""
    for m in ap.PAID_MODELS_NO_FALLBACK:
        assert m in ap.ALLOWED_MODELS["openrouter"], f"{m} fehlt im Katalog"
    for prov in ("gemini", "groq", "cerebras", "mistral"):
        assert not any(m in ap.PAID_MODELS_NO_FALLBACK
                       for m in ap.ALLOWED_MODELS.get(prov, []))
