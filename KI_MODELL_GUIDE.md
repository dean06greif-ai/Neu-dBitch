# KI-Modell-Guide – Preis/Leistung pro Auswahlstelle (Stand: Juni 2026)

Dieser Guide deckt JEDE Stelle ab, an der du im UI eine KI auswählen kannst:
das **Haupt-Modell** (KI-Trader-Engine) und die **9 KI-Team-Rollen**
(je Primär + Fallback 1 + Fallback 2). Alle Empfehlungen basieren auf dem
verifizierten Modell-Katalog der App (`backend/services/ai_providers.py`)
und deinen vorhandenen Keys.

---

## 1. Deine Key-Situation (was du wirklich hast)

| Provider | Keys | Kosten | Rate-Limit-Puffer |
|---|---|---|---|
| Cerebras | 5 (1 + 4 Backups) | komplett gratis | sehr gut – 5 Keys rotieren automatisch |
| OpenRouter | 5 (1 + 4 Backups) | :free-Modelle gratis; bezahlte via Credits | gut – :free hat Tageslimit pro Key |
| Groq | 2 (1 + 1 Backup) | gratis (Free Tier) | mittel – TPM-Limits (App überspringt zu große Prompts automatisch) |
| Mistral | 2 (1 + 1 Backup) | gratis (Free Tier) | mittel |
| Gemini | 1 | **dein Key ist ein bezahlter Key** (Format `AQ.` = Vertex/Cloud-Billing, kein Gratis-AI-Studio-Key) | – |

### Zum Gemini-Problem („muss immer Guthaben aufladen")
Dein aktueller Key läuft über Google-Cloud-Abrechnung. Zwei Optionen:
1. **Empfohlen (gratis):** Auf https://aistudio.google.com einen **AI-Studio-Key**
   erstellen (beginnt mit `AIza…`). Der hat einen echten Free Tier
   (Flash/Flash-Lite-Modelle) und funktioniert 1:1 mit der App
   (`GEMINI_API_KEY` in Render ersetzen). Damit sind die Gemini-Fallbacks gratis.
2. **Wenn du zahlst:** Gemini nur als **seltenen Qualitäts-Fallback** lassen
   (so sind die Presets gebaut – Fallbacks feuern nur bei Ausfall/Limit,
   das sind Cent-Beträge pro Monat, kein Dauerbetrieb).

### „Kosten sind ok, wenn der Mehrwert stimmt" – die Bezahl-Option ohne neuen Key
Du brauchst **keinen neuen Anbieter**: Lade einfach **OpenRouter-Credits** auf
(openrouter.ai → Credits). Damit schaltest du die bereits eingebauten
Premium-Modelle frei (sie machen NIE Auto-Fallback, kosten also nur, wenn du
sie bewusst auswählst):

| Modell (OpenRouter) | Stärke | Preis (ca., pro 1M Tokens) | Wofür lohnt es sich |
|---|---|---|---|
| `deepseek/deepseek-v4-pro-0813` | Reasoning-Spitze | ~0,43 $ | **bester Kauf**: Trade-Manager, Learner, Deep-Analyst |
| `z-ai/glm-5.2` | Top-Allrounder | ~0,46 $ | Deep-Analyst / Research / Trade-Manager |
| `x-ai/grok-4.20` | stark, aktuelles Weltwissen, 2M Kontext | ~1,25 $ (teuerster) | News-/Makro-Deep-Dives, sonst Overkill |
| `deepseek/deepseek-v4-flash` | schnell + stark | ~0,06 $ | günstigstes Bezahl-Upgrade fürs Haupt-Modell/Analyst |
| `qwen/qwen3.7-flash` | schnell, mittel | ~0,03 $ | kein Pflichtkauf (Gratis-Modelle gleichwertig) |

**Faustregel:** 5–10 $ OpenRouter-Credits pro Monat reichen locker, wenn du
Premium nur für Trade-Manager + Learner + Deep-Analyst einsetzt.

### Fehlt ein Modell, das echten Mehrwert bringt? (deine Frage)
- **Anthropic Claude (Sonnet-Klasse)** – das einzige relevante Top-Modell, das
  gerade nicht im Katalog ist. Es ist über **OpenRouter** verfügbar (kein neuer
  Key nötig, läuft über dieselben Credits) und wäre ein Premium-Kandidat für
  **Chat-Assistent** und **Learner** (sehr gutes Instruction-Following, wenig
  Halluzination). Mehrwert vs. DeepSeek-v4-pro ist aber **klein und teurer** –
  erst holen, wenn dich DeepSeek irgendwo enttäuscht.
- **Perplexity Sonar (Online-Modell)** – einziges Modell mit eingebauter
  Websuche; wäre theoretisch ideal als News-Wächter. Aber: die App hat bereits
  eigene News-Feeds + Wirtschaftskalender, das LLM muss nur zusammenfassen →
  **kein echter Mehrwert**, Geld sparen.
- **OpenAI (GPT-Klasse)**: über OpenRouter verfügbar, aber Preis/Leistung für
  diesen Use-Case schlechter als DeepSeek/GLM. Nicht nötig.

**Fazit:** Kein neuer Key nötig. Der größte Hebel ist OpenRouter-Guthaben für
DeepSeek-v4-pro auf den 3 kritischen Rollen.

---

## 2. Haupt-Modell (KI Trader Engine, „Aufsicht")

Läuft oft (Analyse-Loop, Team-Aufsicht) → braucht starkes Gratis-Reasoning mit
robusten Fallbacks.

| Stufe | Auswahl | Warum |
|---|---|---|
| **Primär (gratis)** | `groq / openai/gpt-oss-120b` | stärkstes Gratis-Reasoning, sehr schnell |
| **Premium-Alternative** | `openrouter / deepseek/deepseek-v4-flash` | minimal besser + kein Groq-TPM-Limit, Centbeträge |
| Fallback 1 | `cerebras / gpt-oss-120b` | identisches Modell, aber deine 5 Cerebras-Keys = quasi nie rate-limited |
| Fallback 2 | `gemini / gemini-3.5-flash` | anderer Anbieter-Stack (wenn Groq UND Cerebras down sind) |

---

## 3. KI-Team – Rolle für Rolle

### Trade-Manager (KRITISCH – steuert echtes Geld: SL/TP, Teil-Exits, Hebel)
| Stufe | Gratis-Setup | Premium-Setup (empfohlen, wenn du zahlst) |
|---|---|---|
| Primär | `groq / openai/gpt-oss-120b` | `openrouter / deepseek/deepseek-v4-pro-0813` |
| Fallback 1 | `cerebras / gpt-oss-120b` | `groq / openai/gpt-oss-120b` |
| Fallback 2 | `gemini / gemini-3.5-flash` | `cerebras / gpt-oss-120b` |

**Hier lohnt Premium am meisten**: bessere Zahlen-Disziplin bei gestaffelten
Teil-Exits (TP1/TP2/TP3) und SL-Anpassungen.

### Learner (KRITISCH – Lektionen wirken dauerhaft auf alle Trades)
| Stufe | Gratis | Premium |
|---|---|---|
| Primär | `groq / openai/gpt-oss-120b` | `openrouter / deepseek/deepseek-v4-pro-0813` |
| Fallback 1 | `gemini / gemini-3.1-pro-preview` (selten → Centbeträge) | gleich |
| Fallback 2 | `openrouter / nvidia/nemotron-3-ultra-550b-a55b:free` | gleich |

### Deep-Analyst (2× täglich – wenige, aber tiefe Läufe)
| Stufe | Gratis | Premium |
|---|---|---|
| Primär | `openrouter / nvidia/nemotron-3-ultra-550b-a55b:free` | `openrouter / z-ai/glm-5.2` oder `deepseek-v4-pro` |
| Fallback 1 | `groq / openai/gpt-oss-120b` | gleich |
| Fallback 2 | `cerebras / gpt-oss-120b` | gleich |

Wenige Läufe/Tag → selbst Premium kostet hier fast nichts. Guter Platz zum Zahlen.

### Research-Analyst (wertet Backtests/Optimizer aus – große Datenmengen)
| Stufe | Empfehlung |
|---|---|
| Primär | `groq / openai/gpt-oss-120b` |
| Fallback 1 | `openrouter / nvidia/nemotron-3-super-120b-a12b:free` |
| Fallback 2 | `cerebras / gpt-oss-120b` |

Groq-TPM kann bei sehr großen Prompts zuschlagen → die App überspringt dann
automatisch zum Fallback (kein Handlungsbedarf). Premium unnötig.

### News-Wächter (läuft 24/7 alle 15 min → Volumen-Fresser!)
| Stufe | Empfehlung | Warum |
|---|---|---|
| Primär | `groq / llama-3.1-8b-instant` | schnellstes Gratis-Modell, reicht fürs News-Triage völlig |
| Fallback 1 | **`mistral / ministral-8b-latest`** | dein Wunsch – Mistral ist hier gut: eigener Anbieter-Stack, solide Kurz-Zusammenfassungen, 2 Keys |
| Fallback 2 | `gemini / gemini-3.1-flash-lite` | dritter unabhängiger Stack |

**Wichtig:** Hier NIE ein Bezahl-Modell einstellen – 96 Läufe/Tag × 30 Tage
summieren sich. Die eigentliche Tiefen-Analyse wichtiger News macht ohnehin
der Deep-/Haupt-Analyst.

### Analyst (regelmäßiger Analyse-Loop)
| Stufe | Empfehlung |
|---|---|
| Primär | `groq / openai/gpt-oss-120b` |
| Fallback 1 | `cerebras / gpt-oss-120b` |
| Fallback 2 | `gemini / gemini-3.5-flash-lite` |

Läuft häufig → gratis lassen. Cerebras-Fallback fängt Groq-Limits perfekt ab.

### Chat-Assistent (deine Fragen im KI-Chat)
| Stufe | Gratis | Premium (Komfort) |
|---|---|---|
| Primär | `groq / openai/gpt-oss-120b` | `openrouter / z-ai/glm-5.2` |
| Fallback 1 | `gemini / gemini-3.5-flash` | gleich |
| Fallback 2 | `cerebras / zai-glm-4.7` | gleich |

Nur du erzeugst hier Volumen → Premium ist bezahlbar, aber Gratis-Qualität ist
schon hoch.

### Markt-Beobachter (sammelt Trainingsdaten, LLM optional)
| Stufe | Empfehlung |
|---|---|
| Primär | `groq / llama-3.1-8b-instant` |
| Fallback 1 | `cerebras / gemma-4-31b` |
| Fallback 2 | `gemini / gemini-3.1-flash-lite` |

Reine Datensammlung – das billigste Modell ist hier das richtige. Niemals zahlen.

### Tages-Reporter / Summarizer (1× um Mitternacht)
| Stufe | Empfehlung |
|---|---|
| Primär | `gemini / gemini-3.1-flash-lite` (mit AI-Studio-Key gratis) |
| Fallback 1 | `mistral / mistral-small-latest` |
| Fallback 2 | `cerebras / gemma-4-31b` |

1 Lauf/Tag → egal welches, Hauptsache gratis.

---

## 4. Rate-Limit-Strategie (so läuft alles durch)

1. **Cerebras als Fallback-Rückgrat**: 5 Keys, App rotiert bei 429 automatisch
   auf den nächsten Key → praktisch nie komplett limitiert. Deshalb steht
   Cerebras bei allen Vielläufern als Fallback 1 oder 2.
2. **Anbieter-Streuung pro Rolle**: Primär/Fallback 1/Fallback 2 immer auf
   3 VERSCHIEDENE Anbieter legen (Groq → Cerebras → Gemini/Mistral). Fällt ein
   ganzer Anbieter aus, läuft die Rolle weiter.
3. **Groq-TPM**: Die App kennt die Token-Budgets pro Groq-Modell und überspringt
   zu große Prompts automatisch (sichtbar im Modell-Status als „übersprungen").
4. **OpenRouter :free**: Tageslimit pro Key; mit deinen 5 Keys + Rotation
   unkritisch. Tipp: Mit einmalig 10 $ Credits auf dem Haupt-Key erhöht
   OpenRouter das :free-Tageslimit deutlich – auch ohne die Credits auszugeben.
5. **Bezahl-Modelle machen nie Auto-Fallback** (fest im Code verankert) – ein
   Rate-Limit kann also niemals heimlich Geld kosten.

## 5. TL;DR – Was du konkret einstellen solltest

- **Gratis bleiben?** → Die aktuellen Voreinstellungen sind bereits genau
  dieses Optimum. Nichts zu tun, außer optional den Gemini-Key gegen einen
  gratis AI-Studio-Key zu tauschen.
- **5–10 $/Monat investieren (empfohlen)** → OpenRouter-Credits laden und nur
  bei 3 Rollen Premium einstellen:
  - Trade-Manager → `deepseek/deepseek-v4-pro-0813`
  - Learner → `deepseek/deepseek-v4-pro-0813`
  - Deep-Analyst → `z-ai/glm-5.2`
  - News-Wächter, Markt-Beobachter, Analyst, Summarizer: gratis lassen!
