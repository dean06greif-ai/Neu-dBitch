# KI-Trader Settings-Check (Live-Produktions-Daten, Juni 2026)

Analysiert wurden deine ECHTEN Produktions-Einstellungen (Atlas-DB, read-only)
plus die Ergebnisse aller 48 abgeschlossenen KI-Trades.

## Ist-Zustand (Ergebnisse)
| Kategorie | Trades | Winrate | PnL |
|---|---|---|---|
| Paper (echte KI-Entscheidungen) | 12 | **8 %** (1/12) | −85 USDT |
| Datensammel-Trades (Paper) | 35 | 43 % (15/35) | −115 USDT |
| Live | 1 | 0 % | −0,02 USDT |
| Ø Hebel | **44,9x** (max. 87x!) | | Ø −4,18 USDT/Trade |

**Diagnose:** Die Verluste kommen nicht primär von schlechten Analysen
(Datensammel-Trades mit lockereren Schwellen treffen 43 %), sondern von
**zu hohem Hebel** (Ø 45x → SL-Rauschen + Fees fressen alles) und **zwei
invertierten Daten-Schaltern** (siehe Punkt 1).

---

## Empfohlene Änderungen (Prio-Reihenfolge, alle im UI einstellbar)

### 1. KRITISCH: Liquiditäts-Schalter sind genau FALSCH herum
Aktuell: `use_heatmap_data: AN`, `use_liquidation_data: AUS`.
Der Code enthält dazu sogar eine dokumentierte RCA: Die Heatmap-Cluster sind
**reine Formelwerte (Preis ± 1/Hebel), keine gemessenen Daten** – sie haben
die KI nachweislich in Fade-Trades an erfundenen Levels gelockt. Die ECHTEN
Daten (Long/Short-Ratio, OI, Orderbook-Wände, Live-Liquidationen) sind
ausgeschaltet.
→ **use_liquidation_data = AN, use_heatmap_data = AUS** (KI-Setup → Liquidität).

### 2. KRITISCH: Hebel begrenzen (größter PnL-Hebel überhaupt)
- Ø 45x Hebel ist für langfristigen Profit tödlich (0,2 % Gegenbewegung = −9 %
  auf die Margin, dazu Fees × Hebel).
- Aktuell: `lev_mode: coin` (Coin-Settings erlauben offenbar 50–90x),
  Trade-Manager `max_leverage: 125`, `swing_max_leverage: 20`.
→ Empfehlung:
  - **lev_mode = auto, lev_auto_max = 12** (KI wählt pro Trade 1–12x)
  - **Trade-Manager max_leverage = 25** (statt 125)
  - **swing_max_leverage = 5–8** (Swing lebt von weiten Zielen, nicht Hebel)
  - profit_lock_max_leverage 100 → **50**

### 3. Lern-Setup (gut, mit 2 Schwächen)
Gut eingestellt: learning_enabled, learn_on_trade_close, Lookback 60 Tage,
max_lessons 50, Datensammel-Modus AN (min_conf 55, 3/Coin) → liefert Labels.
Schwächen:
- **Learner-Rolle läuft auf `openai/gpt-oss-20b:free`** – das schwächste
  Modell im Katalog (Gewicht 1) für die WICHTIGSTE Lern-Rolle. Lektionen
  wirken dauerhaft auf alle Trades!
  → **groq / openai/gpt-oss-120b** (gratis) oder Premium `deepseek-v4-pro`.
- **Forschungs-Analyst auf `gemini-3.1-flash-lite`** (Gewicht 1) – muss
  Backtests/Optimizer auswerten, dafür zu schwach.
  → **cerebras / gpt-oss-120b** (gratis, kein Groq-Token-Limit → keine
  „Prompt zu groß"-Skips mehr bei dieser Rolle).

### 4. Sinnvoll nachjustieren (mittel)
- **max_trades_per_coin 3 → 2**: weniger Stacking-Klumpenrisiko, Lern-Daten
  bleiben sauberer (Diversifikations-Guards greifen zwar, aber 3 parallele
  Trades auf einem Coin verdreifachen denselben Fehler).
- **smart_skip_move_pct 0.02 → 0.10–0.15**: Bei 0,02 % skippt Smart-Skip
  praktisch nie → unnötige LLM-Läufe (Kosten/Rate-Limits) ohne Mehrwert.
- **autonomy: auto** ist ok (Guard-Spanne 55–75 schützt), aber solange die
  Winrate < 40 % liegt, wäre **suggest** sicherer – du siehst dann, WAS die
  KI an sich selbst ändern will, bevor es gilt.

### 5. Bereits richtig eingestellt (nicht anfassen)
- min_confidence 70 (in der erlaubten Tune-Spanne 55–75)
- Fee-Wächter AN mit mult 4.0 (blockt garantierte Fee-Verlierer)
- CRV-Rahmen 1.2–4.0, correlation_guard AN, max_same_direction 3
- use_ai_levels AN (ok, weil Fee-Wächter + Clamps greifen)
- group_analysis, lean_prompt AN (Token-Effizienz)
- Live-Trading faktisch pausiert (nur 1 Live-Trade) → richtig: **erst live
  skalieren, wenn Paper-Winrate über mehrere Wochen > 45–50 % bei CRV ≥ 1.5**

## Erwartung nach Umstellung
Punkt 1 + 2 zusammen adressieren die beiden Hauptverlustquellen. Mit Hebel ≤ 12
wäre der Ø-Verlust pro Trade grob um Faktor 3–4 kleiner gewesen, und die KI
handelt nicht mehr auf erfundene Heatmap-Levels. Danach 2–3 Wochen Paper
laufen lassen und im Analyse-Tab Winrate + Ø-PnL/Trade neu bewerten.
