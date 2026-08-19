"""Großer Read-only-Check gegen die Prod-DB (PROD_MONGO_URL). NIEMALS schreiben."""
import os, asyncio, json
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from motor.motor_asyncio import AsyncIOMotorClient

NOW = datetime.now(timezone.utc)
D7 = (NOW - timedelta(days=7)).isoformat()
D14 = (NOW - timedelta(days=14)).isoformat()


def sec(title):
    print(f"\n{'='*70}\n## {title}\n{'='*70}")


async def main():
    c = AsyncIOMotorClient(os.environ["PROD_MONGO_URL"], serverSelectionTimeoutMS=10000)
    db = c[os.environ["PROD_DB_NAME"]]

    sec("A) ENGINE-CONFIG (Prod, live)")
    cfg = await db.settings.find_one({"_id": "ai_trader_config"}) or {}
    keys = ["enabled", "min_confidence", "cooldown_min", "tune_conf_min", "tune_conf_max",
            "tune_cooldown_max", "collection_enabled", "collection_min_confidence",
            "collection_cooldown_min", "collection_max_same_direction", "collection_max_per_coin",
            "max_same_direction", "correlation_guard", "min_entry_distance_pct",
            "max_trades_per_coin", "fee_guard_enabled", "fee_guard_mult", "crv_max",
            "autonomy", "provider", "model", "interval_min", "use_liquidation_data",
            "use_heatmap_data", "lev_mode", "smart_skip_move_pct", "use_ai_levels"]
    for k in keys:
        if k in cfg:
            print(f"  {k} = {cfg[k]}")
    mp = await db.settings.find_one({"_id": "master_prompt"}) or {}
    print("  master_prompt.rules =", json.dumps(mp.get("rules") or {}, ensure_ascii=False)[:400])

    sec("B) DECISIONS 7d: Aktionen / Konfidenz / signaled / blocked_by")
    rows = await db.ai_decisions.find({"ts": {"$gte": D7}},
        {"action": 1, "confidence": 1, "signaled": 1, "blocked_by": 1, "symbol": 1,
         "data_collection": 1, "gate_shadow": 1, "ts": 1}).to_list(50000)
    print(f"  Decisions 7d: {len(rows)}")
    print("  Aktionen:", dict(Counter(r.get("action") for r in rows)))
    ls = [r for r in rows if r.get("action") in ("LONG", "SHORT")]
    print(f"  LONG/SHORT: {len(ls)}, davon signaled: {sum(1 for r in ls if r.get('signaled'))}, "
          f"davon collection: {sum(1 for r in ls if r.get('data_collection'))}")
    conf_hist = Counter()
    for r in ls:
        cf = int(r.get("confidence") or 0)
        conf_hist[f"{cf//5*5}-{cf//5*5+4}"] += 1
    print("  Konfidenz-Histogramm LONG/SHORT:", dict(sorted(conf_hist.items())))
    blocked = Counter()
    for r in ls:
        b = r.get("blocked_by")
        if b:
            blocked[str(b)[:80]] += 1
    print("  blocked_by (Top 15):")
    for reason, n in blocked.most_common(15):
        print(f"    {n:4d}x {reason}")
    nb = [r for r in ls if not r.get("signaled") and not r.get("blocked_by")]
    print(f"  LONG/SHORT weder signaled noch blocked_by (Cooldown/Schwelle/Session): {len(nb)}")
    ge = [r for r in ls if isinstance(r.get("gate_shadow"), dict)]
    wb = [r for r in ge if r["gate_shadow"].get("would_block")]
    print(f"  gate_shadow vorhanden: {len(ge)}, would_block: {len(wb)} "
          f"({100*len(wb)/max(1,len(ge)):.0f}%)")

    sec("C) SIGNALS 7d: _reject_reason der KI-Signale")
    sigs = await db.signals.find({"ts": {"$gte": D7}, "strategy_id": "ai_trader"},
        {"_reject_reason": 1, "symbol": 1, "data_collection": 1, "result": 1}).to_list(5000)
    print(f"  KI-Signale 7d: {len(sigs)}")
    rej = Counter(str(s.get("_reject_reason"))[:80] for s in sigs if s.get("_reject_reason"))
    print("  _reject_reason (alle):")
    for reason, n in rej.most_common(20):
        print(f"    {n:4d}x {reason}")

    sec("D) GOVERNANCE-Feed 7d (blockierte Trades)")
    gov = await db.ai_chat.find({"role": "governance", "ts": {"$gte": D7}},
        {"text": 1, "ts": 1}).sort("ts", -1).to_list(500)
    print(f"  Governance-Einträge 7d: {len(gov)}")
    gtxt = Counter()
    for g in gov:
        t = str(g.get("text") or "")
        key = t.split("–")[-1].strip()[:80] if "–" in t else t[:80]
        gtxt[key] += 1
    for reason, n in gtxt.most_common(15):
        print(f"    {n:4d}x {reason}")

    sec("E) TRADES: letzte 30 geschlossene + offene KI-Trades")
    open_tr = await db.auto_trades.find({"strategy_id": "ai_trader", "status": "open"}).to_list(50)
    closed = await db.auto_trades.find({"strategy_id": "ai_trader", "status": {"$ne": "open"}}) \
        .sort("closed_at", -1).to_list(30)
    print(f"  Offene KI-Trades: {len(open_tr)}")
    for t in open_tr:
        print(f"    OPEN {t.get('symbol')} {t.get('side')} entry={t.get('entry')} sl={t.get('sl')} "
              f"tp1={t.get('tp1')} tpf={t.get('tp')} lev={t.get('leverage')} qty={t.get('qty')} "
              f"mode={t.get('mode')} dc={t.get('data_collection')} conf={t.get('ai_confidence')} "
              f"setup={t.get('setup')} opened={str(t.get('opened_at'))[:16]}")
    print(f"\n  Letzte {len(closed)} geschlossene KI-Trades:")
    wins = 0
    for t in closed:
        pnl = float(t.get("realized_pnl") or 0)
        wins += 1 if pnl > 0 else 0
        fees = float(t.get("fees_paid") or 0)
        print(f"    {str(t.get('closed_at'))[:16]} {t.get('symbol'):10s} {t.get('side'):5s} "
              f"pnl={pnl:+8.3f} fees={fees:6.3f} lev={t.get('leverage')} "
              f"conf={t.get('ai_confidence')} setup={str(t.get('setup'))[:18]:18s} "
              f"dc={1 if t.get('data_collection') else 0} exit={str(t.get('close_reason') or t.get('exit_reason'))[:28]}")
    if closed:
        print(f"  Winrate letzte {len(closed)}: {wins}/{len(closed)} = {100*wins/len(closed):.0f}%")

    sec("F) WINRATE nach Konfidenz-Bucket (alle geschlossenen KI-Trades)")
    allc = await db.auto_trades.find({"strategy_id": "ai_trader", "status": {"$ne": "open"}},
        {"realized_pnl": 1, "ai_confidence": 1, "data_collection": 1, "fees_paid": 1,
         "closed_at": 1, "setup": 1}).to_list(2000)
    print(f"  Geschlossene KI-Trades gesamt: {len(allc)}")
    buckets = defaultdict(lambda: [0, 0, 0.0])
    for t in allc:
        cf = t.get("ai_confidence")
        if cf is None:
            b = "unbekannt"
        else:
            cf = int(cf)
            b = "<65" if cf < 65 else "65-69" if cf < 70 else "70-74" if cf < 75 else \
                "75-79" if cf < 80 else ">=80"
        pnl = float(t.get("realized_pnl") or 0)
        buckets[b][0] += 1
        buckets[b][1] += 1 if pnl > 0 else 0
        buckets[b][2] += pnl
    for b in ["<65", "65-69", "70-74", "75-79", ">=80", "unbekannt"]:
        if b in buckets:
            n, w, p = buckets[b]
            print(f"    conf {b:9s}: n={n:3d} wins={w:3d} winrate={100*w/max(1,n):3.0f}% sumPnL={p:+8.2f}")
    fee_dom = sum(1 for t in allc if float(t.get("realized_pnl") or 0) < 0
                  and float(t.get("fees_paid") or 0) >= 0.5 * abs(float(t.get("realized_pnl") or 1e-9)))
    losses = sum(1 for t in allc if float(t.get("realized_pnl") or 0) < 0)
    print(f"  Fee-dominierte Verluste (Fees>=50% des Verlusts): {fee_dom}/{losses}")
    setups = defaultdict(lambda: [0, 0, 0.0])
    for t in allc:
        s = str(t.get("setup") or "?")
        pnl = float(t.get("realized_pnl") or 0)
        setups[s][0] += 1; setups[s][1] += 1 if pnl > 0 else 0; setups[s][2] += pnl
    print("  Nach Setup:")
    for s, (n, w, p) in sorted(setups.items(), key=lambda x: -x[1][0])[:10]:
        print(f"    {s:22s}: n={n:3d} winrate={100*w/max(1,n):3.0f}% sumPnL={p:+8.2f}")

    sec("G) REGIME v2: Verteilung Snapshots 7d")
    snaps = await db.ai_market_snapshots.find({"ts": {"$gte": D7}},
        {"symbol": 1, "features.regime": 1, "features.regime_v": 1,
         "features.vol_basis": 1}).to_list(60000)
    print(f"  Snapshots 7d: {len(snaps)}")
    v2 = [s for s in snaps if (s.get("features") or {}).get("regime_v") == 2]
    print(f"  davon regime_v=2: {len(v2)}")
    reg = Counter((s.get("features") or {}).get("regime") for s in v2)
    tot = max(1, len(v2))
    print("  Regime-Verteilung (v2):")
    for r, n in reg.most_common(20):
        print(f"    {100*n/tot:5.1f}% {n:6d}x {r}")
    vb = Counter((s.get("features") or {}).get("vol_basis") for s in v2)
    print("  vol_basis:", dict(vb))
    vol_suffix = Counter(str((s.get("features") or {}).get("regime") or "").split("_")[-1] for s in v2)
    print("  Vol-Anteil:", {k: f"{100*v/tot:.0f}%" for k, v in vol_suffix.most_common()})

    sec("H) TOKEN-USAGE letzte 7 Tage (pro Rolle)")
    tok = await db.ai_token_usage.find().sort("date", -1).to_list(200)
    bydate = defaultdict(list)
    for t in tok:
        bydate[t.get("date")].append(t)
    for d in sorted(bydate.keys(), reverse=True)[:7]:
        rows_ = bydate[d]
        tot_t = sum(int(r.get("tokens") or 0) for r in rows_)
        print(f"  {d}: gesamt {tot_t:,} Tokens")
        for r in sorted(rows_, key=lambda x: -int(x.get("tokens") or 0)):
            print(f"      {r.get('role'):18s} {int(r.get('tokens') or 0):>10,} tok "
                  f"{int(r.get('calls') or 0):>5} calls  model={str(r.get('model'))[:40]}")

    sec("I) ML-GATE: Modelle + Shadow-Report-Basis")
    models = await db.ml_gate_models.find({}, {"version": 1, "trained_at": 1, "samples": 1,
        "metrics": 1, "trigger": 1}).sort("version", -1).to_list(10)
    for m in models:
        met = m.get("metrics") or {}
        print(f"    v{m.get('version')} {str(m.get('trained_at'))[:16]} samples={m.get('samples')} "
              f"AUC={met.get('auc')} brier={met.get('brier_calibrated')} vs base={met.get('brier_baseline')} "
              f"trigger={m.get('trigger')}")
    gs = await db.settings.find_one({"_id": "ml_gate_settings"}) or {}
    print("  gate settings:", {k: v for k, v in gs.items() if k != "_id"})

    sec("J) REWARDS: letzte Lern-Einträge")
    rew = await db.ai_rewards.find().sort("ts", -1).to_list(500)
    print(f"  ai_rewards gesamt: {len(rew)}")
    if rew:
        w = sum(1 for r in rew if float(r.get("reward") or 0) > 0)
        print(f"  Reward>0: {w}/{len(rew)}")
        fs = [float(r.get("fee_share_pct")) for r in rew if r.get("fee_share_pct") is not None]
        if fs:
            print(f"  Ø fee_share_pct bei Verlusten: {sum(fs)/len(fs):.0f}% (n={len(fs)})")
        byreg = defaultdict(lambda: [0, 0])
        for r in rew:
            rg = str(r.get("regime") or "?")
            byreg[rg][0] += 1
            byreg[rg][1] += 1 if float(r.get("reward") or 0) > 0 else 0
        print("  Nach Regime:")
        for rg, (n, w2) in sorted(byreg.items(), key=lambda x: -x[1][0])[:12]:
            print(f"    {rg:24s} n={n:3d} win={100*w2/max(1,n):3.0f}%")

    sec("K) AI-PROPOSALS (Self-Tuning) letzte 14d")
    props = await db.ai_proposals.find({"ts": {"$gte": D14}}).sort("ts", -1).to_list(50)
    for p in props[:20]:
        print(f"    {str(p.get('ts'))[:16]} {p.get('symbol')} {json.dumps(p.get('changes') or {}, ensure_ascii=False)[:80]} "
              f"status={p.get('status')} {str(p.get('guard_reason') or '')[:60]}")

    c.close()

asyncio.run(main())
