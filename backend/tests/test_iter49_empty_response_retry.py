"""Iteration 49 – Leere-Antwort-Fix (Bug-Report):
OpenRouter-Free-Modelle (z.B. openai/gpt-oss-20b:free) liefern gelegentlich
HTTP 200 mit leeren choices ("Leere Antwort"). Das ist transient (Upstream
überlastet). Neues Verhalten in generate_chain:
  1) einmaliger Retry (2s) auf demselben Key
  2) danach nächster Key (andere Route) statt das Modell sofort aufzugeben
  3) erst wenn alle Keys leer bleiben -> nächstes Modell der Kette
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import ai_providers  # noqa: E402


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def _fast(_secs):
        await real_sleep(0)
    monkeypatch.setattr(ai_providers.asyncio, "sleep", _fast)


def _patch(monkeypatch, gen, keys=("k1", "k2")):
    monkeypatch.setattr(ai_providers, "_oai_generate", gen)
    monkeypatch.setattr(ai_providers, "provider_keys", lambda p: list(keys))
    monkeypatch.setattr(ai_providers, "input_budget", lambda p, m: None)


def test_empty_response_single_retry_succeeds(monkeypatch):
    calls = []

    async def gen(provider, model, key, prompt, system, temperature, json_mode):
        calls.append(key)
        if len(calls) == 1:
            raise RuntimeError(f"Leere Antwort (keine choices) von {provider}/{model}")
        return '{"ok": true}'

    _patch(monkeypatch, gen)
    text, prov, model = asyncio.run(ai_providers.generate_chain(
        [("openrouter", "openai/gpt-oss-20b:free")], "p", "s"))
    assert text == '{"ok": true}'
    assert model == "openai/gpt-oss-20b:free"
    assert calls == ["k1", "k1"], "Retry muss auf DEMSELBEN Key passieren"


def test_empty_response_rotates_keys_then_next_model(monkeypatch):
    calls = []

    async def gen(provider, model, key, prompt, system, temperature, json_mode):
        calls.append((model, key))
        if model == "openai/gpt-oss-20b:free":
            raise RuntimeError(f"Leere Antwort (keine choices) von {provider}/{model}")
        return "antwort"

    _patch(monkeypatch, gen)
    text, prov, model = asyncio.run(ai_providers.generate_chain(
        [("openrouter", "openai/gpt-oss-20b:free"),
         ("openrouter", "google/gemma-4-27b-it:free")], "p", "s"))
    assert text == "antwort"
    assert model == "google/gemma-4-27b-it:free"
    # Modell A: Key1 (+Retry) und Key2 (+Retry) versucht = 4 Calls, dann Modell B
    a_calls = [c for c in calls if c[0] == "openai/gpt-oss-20b:free"]
    assert len(a_calls) == 4
    assert {k for _, k in a_calls} == {"k1", "k2"}, "beide Keys müssen probiert werden"


def test_other_errors_still_skip_model_immediately(monkeypatch):
    calls = []

    async def gen(provider, model, key, prompt, system, temperature, json_mode):
        calls.append((model, key))
        if model == "kaputt":
            raise RuntimeError("Error code: 500 - internal")
        return "ok"

    _patch(monkeypatch, gen)
    text, prov, model = asyncio.run(ai_providers.generate_chain(
        [("openrouter", "kaputt"), ("openrouter", "heil")], "p", "s"))
    assert model == "heil"
    assert [c for c in calls if c[0] == "kaputt"] == [("kaputt", "k1")], \
        "harte Fehler dürfen weiterhin sofort zum nächsten Modell springen"


def test_all_empty_raises_last_error(monkeypatch):
    async def gen(provider, model, key, prompt, system, temperature, json_mode):
        raise RuntimeError(f"Leere Antwort (keine choices) von {provider}/{model}")

    _patch(monkeypatch, gen, keys=("k1",))
    with pytest.raises(RuntimeError, match="Leere Antwort"):
        asyncio.run(ai_providers.generate_chain(
            [("openrouter", "openai/gpt-oss-20b:free")], "p", "s"))


def test_health_detail_explains_overload(monkeypatch):
    async def gen(provider, model, key, prompt, system, temperature, json_mode):
        if model == "openai/gpt-oss-20b:free":
            raise RuntimeError(f"Leere Antwort (keine choices) von {provider}/{model}")
        return "ok"

    _patch(monkeypatch, gen, keys=("k1",))
    asyncio.run(ai_providers.generate_chain(
        [("openrouter", "openai/gpt-oss-20b:free"),
         ("openrouter", "heil")], "p", "s"))
    h = ai_providers._health.get("openrouter/openai/gpt-oss-20b:free") or {}
    assert "überlastet" in (h.get("detail") or ""), \
        "KI-Team-Status muss die Überlastung verständlich erklären"
