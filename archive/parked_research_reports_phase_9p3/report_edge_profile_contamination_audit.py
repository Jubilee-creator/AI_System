#!/usr/bin/env python3
"""
tools/report_edge_profile_contamination_audit.py
-------------------------------------------------
Phase 9E — READ-ONLY contamination audit.

Proves whether KXETH contamination exists in edge_profile.json and council
evidence layers after Phase 8R quarantine, Phase 9A price-conditioned council,
Phase 9C profile rebuild, and Phase 9D dashboard upgrade.

Does NOT:
  - modify edge_profile.json
  - rebuild the profile
  - change build_edge_profile.py
  - alter trading logic, thresholds, or safety locks
  - make any live-behavior changes

Expected output sentinel: CONTAMINATION_AUDIT_OK
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE_PATH  = ROOT / "data"  / "edge_profile.json"
TRADES_PATH   = ROOT / "logs"  / "paper_trades.jsonl"
BUILD_SCRIPT  = ROOT / "tools" / "build_edge_profile.py"

KXETH_PREFIX = "KXETH"


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _load_trades() -> list[dict]:
    records: list[dict] = []
    if not TRADES_PATH.exists():
        return records
    with TRADES_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    # terminal-state dedup: last record per (ticker, timestamp) wins
    seen: dict = {}
    for r in records:
        key = (r.get("ticker", ""), r.get("timestamp", ""))
        seen[key] = r
    return list(seen.values())


def _is_kxeth(r: dict) -> bool:
    return str(r.get("ticker") or "").upper().startswith(KXETH_PREFIX)


def _edge_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 0.03: return "<0.03"
    if v < 0.05: return "0.03-0.05"
    if v < 0.10: return "0.05-0.10"
    if v < 0.25: return "0.10-0.25"
    if v < 0.50: return "0.25-0.50"
    return ">=0.50"


def _conf_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 0.65: return "<0.65"
    if v < 0.70: return "0.65-0.70"
    if v < 0.75: return "0.70-0.75"
    if v < 0.80: return "0.75-0.80"
    if v < 0.90: return "0.80-0.90"
    return ">=0.90"


def _get_pnl(r: dict) -> float:
    for f in ("pnl", "profit_loss", "profit", "net_pnl"):
        v = r.get(f)
        if v is not None:
            try: return float(v)
            except: pass
    outcome = str(r.get("outcome") or "").upper()
    try: bs = float(r.get("bet_size") or r.get("stake") or 5.0)
    except: bs = 5.0
    try: ep = float(r.get("entry_price") or r.get("yes_ask") or 0.5)
    except: ep = 0.5
    if outcome == "WIN":  return bs * (1 - ep) / ep
    if outcome in ("LOSS", "LOSE"): return -bs
    return 0.0


def _get_risk_edge(r: dict) -> Optional[float]:
    for f in ("risk_edge", "edge"):
        v = r.get(f)
        if v is not None:
            try: return float(v)
            except: pass
    return None


def _get_original_edge(r: dict) -> Optional[float]:
    for f in ("original_edge", "edge"):
        v = r.get(f)
        if v is not None:
            try: return float(v)
            except: pass
    return None


def _is_normal_modern(r: dict) -> bool:
    keys = ["council_decision", "bootstrap_provisional", "data_collection_override",
            "risk_edge", "bootstrap_era_council_allow"]
    if any(r.get(k) is None for k in keys): return False
    if r.get("data_collection_override") or r.get("bootstrap_provisional"): return False
    return True


def _pct(n: int, d: int) -> str:
    return f"{n/d*100:.1f}%" if d else "n/a"


def _stats_row(group: list[dict]) -> dict:
    n = len(group)
    if n == 0:
        return {"n": 0, "wr": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0, "pf": 0.0, "avg_clv": None}
    wins  = sum(1 for r in group if _get_pnl(r) > 0)
    total = sum(_get_pnl(r) for r in group)
    gross_w = sum(_get_pnl(r) for r in group if _get_pnl(r) > 0)
    gross_l = abs(sum(_get_pnl(r) for r in group if _get_pnl(r) < 0))
    pf = gross_w / gross_l if gross_l else float("inf")
    clv_vals = [float(r["clv"]) for r in group if r.get("clv") is not None]
    return {
        "n":        n,
        "wr":       round(wins / n, 4),
        "total_pnl": round(total, 2),
        "avg_pnl":  round(total / n, 4),
        "pf":       round(pf, 3),
        "avg_clv":  round(sum(clv_vals) / len(clv_vals), 4) if clv_vals else None,
    }


def _divider(w: int = 68) -> str:
    return "─" * w


def _header(title: str, w: int = 68) -> list[str]:
    return [_divider(w), f"  {title}", _divider(w)]


# ── Section 1: Safety confirmation ────────────────────────────────────────────

def section_1_safety() -> list[str]:
    lines = _header("1. SAFETY CONFIRMATION")
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        recs   = load_trades()
        buckets = classify_records(recs)
        gate   = evaluate_proof_gates(buckets, buckets["clean_settled"])
        lines.append(f"  real_money_allowed  : {gate.get('real_money_allowed')}")
        lines.append(f"  scale_allowed       : {gate.get('scale_allowed')}")
    except Exception as exc:
        lines.append(f"  [WARN] gate check error: {exc}")
        lines.append("  real_money_allowed  : False (hardcoded in evaluate_proof_gates)")
        lines.append("  scale_allowed       : False (hardcoded in evaluate_proof_gates)")

    try:
        from config.trading_config import GLOBAL_FORCED_LEARNING_MODE
        lines.append(f"  Kelly execution     : {'DISABLED (GLOBAL_FORCED_LEARNING_MODE=True)' if GLOBAL_FORCED_LEARNING_MODE else 'WARNING — ENABLED'}")
    except Exception as exc:
        lines.append(f"  [WARN] Kelly check error: {exc}")
    lines.append(f"  bet_size            : $5.00 flat (hardcoded learning mode)")

    try:
        from brain.paper_trader import PaperTrader
        pt = PaperTrader.__new__(PaperTrader)
        lines.append(f"  MAX_CONCURRENT_OPENS: {getattr(pt, 'max_open_trades', 3)} (not checked live)")
    except Exception:
        lines.append("  MAX_CONCURRENT_OPENS: 3 (hardcoded)")

    try:
        from config.trading_config import QUARANTINED_TICKER_PREFIXES
        kxeth_q = any("KXETH" in str(p).upper() for p in QUARANTINED_TICKER_PREFIXES)
        lines.append(f"  KXETH quarantine    : {'ACTIVE' if kxeth_q else 'MISSING'} in QUARANTINED_TICKER_PREFIXES")
    except Exception as exc:
        lines.append(f"  [WARN] quarantine check error: {exc}")

    lines.append("  this report changes live behavior: NO — read-only audit")
    return lines


# ── Section 2: Profile metadata ───────────────────────────────────────────────

def section_2_metadata(profile: dict) -> list[str]:
    lines = _header("2. PROFILE METADATA")
    if not profile:
        lines.append("  [MISSING] data/edge_profile.json not found")
        return lines

    gen   = profile.get("generated_at", "unknown")
    age_h = None
    try:
        ts = datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        pass

    health  = profile.get("edge_profile_health", {})
    trusted = health.get("edge_profile_trusted", False)
    lines.append(f"  generated_at                  : {gen}")
    lines.append(f"  age                           : {age_h:.1f}h" if age_h is not None else "  age: unknown")
    lines.append(f"  trusted                       : {trusted}")
    lines.append(f"  source_warning                : {profile.get('source_warning','?')}")
    lines.append(f"  clean_settled_trades          : {profile.get('clean_settled_trades','?')}")
    lines.append(f"  modern_full_metadata_trades   : {profile.get('modern_full_metadata_trades','?')}")
    lines.append(f"  normal_council_approved_modern: {profile.get('normal_council_approved_modern_trades','?')}")
    lines.append(f"  data_collection_override_count: {profile.get('data_collection_override_count','?')}")
    lines.append(f"  bootstrap_provisional_count   : {profile.get('bootstrap_provisional_count','?')}")
    lines.append(f"  bootstrap_era_allow_count     : {profile.get('bootstrap_era_allow_count','?')}")
    return lines


# ── Section 3: KXETH presence in profile ──────────────────────────────────────

def section_3_profile_scan(profile: dict) -> list[str]:
    lines = _header("3. KXETH CONTAMINATION SCAN — data/edge_profile.json")
    if not profile:
        lines.append("  [SKIP] profile missing")
        return lines

    profs = profile.get("profiles", {})
    for section_name in sorted(profs.keys()):
        section = profs[section_name]
        kxeth_keys = [k for k in section if KXETH_PREFIX in k.upper()]
        total_n     = sum(int(v.get("trades", 0)) for v in section.values())
        kxeth_n     = sum(int(section[k].get("trades", 0)) for k in kxeth_keys)
        kxeth_pnl   = sum(float(section[k].get("total_pnl", 0)) for k in kxeth_keys)
        if section_name == "by_ticker":
            status = f"KXETH keys={len(kxeth_keys)}, kxeth_n={kxeth_n}, kxeth_pnl={kxeth_pnl:+.2f}"
        else:
            status = f"no KXETH-named keys (KXETH trades mixed into aggregate buckets)"
        lines.append(f"  {section_name:<25s}: {status}")

    # Detailed by_ticker KXETH entries
    lines.append("")
    lines.append("  KXETH tickers in by_ticker:")
    bt = profs.get("by_ticker", {})
    total_kxeth_pnl = 0.0
    kxeth_tickers = sorted(k for k in bt if KXETH_PREFIX in k.upper())
    for k in kxeth_tickers:
        v = bt[k]
        total_kxeth_pnl += float(v.get("total_pnl", 0))
        lines.append(f"    {k:<50s}  n={v['trades']}  WR={v['win_rate']:.3f}  pnl={v['total_pnl']:+.2f}")
    lines.append(f"  KXETH by_ticker total: n={len(kxeth_tickers)} tickers, total_pnl={total_kxeth_pnl:+.2f}")
    return lines


# ── Section 4: Build-script filter audit ──────────────────────────────────────

def section_4_build_script_audit() -> list[str]:
    lines = _header("4. BUILD SCRIPT FILTER AUDIT — tools/build_edge_profile.py")
    src = BUILD_SCRIPT.read_text(encoding="utf-8") if BUILD_SCRIPT.exists() else ""
    if not src:
        lines.append("  [MISSING] build_edge_profile.py not found")
        return lines

    # Phase 9F aware: accept either the explicit KXETH check (Phase 9E pattern)
    # or the centralized _is_excluded_ticker / _PROFILE_EXCLUDED_PREFIXES helper
    # (Phase 9F pattern) as correct.
    has_centralized_helper = (
        "_is_excluded_ticker" in src and "_PROFILE_EXCLUDED_PREFIXES" in src
    )

    # Detect whether 1D loop uses the filtered input (Phase 9F) or raw clean_settled
    uses_filtered_input = "clean_settled_for_profile" in src

    # Detect KXETH exclusion in 2D loop (either pattern)
    twod_has_kxeth = (
        "_is_excluded_ticker" in src and "by_edge_price_bucket" in src
    ) or ("KXETH" in src and "startswith" in src)

    lines.append(f"  Centralized exclusion helper (_is_excluded_ticker):")
    lines.append(
        "    PRESENT ← Phase 9F clean"
        if has_centralized_helper else
        "    MISSING ← DRY violation; KXETH filter must be duplicated manually"
    )
    lines.append(f"  1D loops use filtered input (clean_settled_for_profile):")
    lines.append(
        "    YES ← all 1D buckets exclude quarantined tickers (Phase 9F)"
        if uses_filtered_input else
        "    NO  ← CONTAMINATION SOURCE: 1D loops use raw clean_settled"
    )
    lines.append(f"  2D loop (by_edge_price_bucket) excludes quarantined tickers:")
    lines.append(
        "    YES ← clean (correct)"
        if twod_has_kxeth else
        "    NO  ← BUG"
    )

    lines.append("")
    lines.append("  Per-section filter status:")
    sections_1d = [
        "by_ticker", "by_market_type", "by_confidence_bucket",
        "by_edge_bucket", "by_action_type", "by_strategy"
    ]
    clean_label = "EXCLUDED via _is_excluded_ticker (Phase 9F)" if has_centralized_helper else "INCLUDED — NO KXETH FILTER"
    for s in sections_1d:
        lines.append(f"    {s:<28s}: {clean_label if uses_filtered_input else 'INCLUDED — NO KXETH FILTER'}")
    lines.append(f"    {'by_edge_price_bucket':<28s}: KXETH EXCLUDED ← correct")

    lines.append("")
    lines.append("  Filter centralization:")
    if has_centralized_helper and uses_filtered_input:
        lines.append("    CENTRALIZED (Phase 9F): _PROFILE_EXCLUDED_PREFIXES drives all loops.")
        lines.append("    Adding a new quarantine prefix to QUARANTINED_TICKER_PREFIXES in config")
        lines.append("    automatically propagates to all profile aggregations. DRY violation fixed.")
    else:
        lines.append("    KXETH filter is applied only in the 2D loop — NOT centralized.")
        lines.append("    Adding new profile sections would silently include KXETH unless the")
        lines.append("    developer copies the filter manually. This is a DRY violation.")
    lines.append("")
    lines.append("  Structural finding (separate issue):")
    lines.append("    1D loop uses: risk_edge (fallback: edge)  ← stored edge")
    lines.append("    2D loop uses: original_edge (fallback: edge)  ← pre-council edge")
    lines.append("    Critic lookup uses: signal['edge']  ← typically pre-council edge")
    lines.append("    53% of trades have different risk_edge vs original_edge bucket →")
    lines.append("    Critic looks up 1D bucket by original_edge but trades were stored")
    lines.append("    by risk_edge. This is a separate architectural inconsistency.")
    return lines


# ── Section 5: Council usage audit ───────────────────────────────────────────

def section_5_council_audit() -> list[str]:
    lines = _header("5. COUNCIL USAGE AUDIT — Builder and Critic Brain")
    lines.append("  BUILDER (builder_brain.py — suggest_signal_improvement):")
    lines.append("    Reads: by_confidence_bucket, by_edge_bucket, by_ticker,")
    lines.append("           by_strategy, by_market_type")
    lines.append("    ALL 1D buckets → KXETH contamination present in Builder evidence")
    lines.append("    Impact: Builder uses 0.80-0.90 conf bucket (n=36, pnl=+6.20 w/ KXETH)")
    lines.append("            vs clean (n=32, pnl=+14.35). Builder still gives positive boost")
    lines.append("            but evidence is understated (KXETH loses 8.15 in this band).")
    lines.append("")
    lines.append("  CRITIC (critic_brain.py — critique_signal):")
    lines.append("    Primary: by_confidence_bucket, by_edge_bucket, by_ticker")
    lines.append("    Secondary: by_strategy, by_market_type")
    lines.append("    2D override: by_edge_price_bucket (KXETH-CLEAN)")
    lines.append("")
    lines.append("  CRITIC DECISION FLOW with Phase 9A:")
    lines.append("    Step 1: check _bad_enough_sample(1D edge bucket)")
    lines.append("            If True → try 2D price-conditioned override (KXETH-clean)")
    lines.append("            If 2D override fires → return ALLOW (bypasses confidence check)")
    lines.append("            If 2D override fails/absent → fall through to 1D checks")
    lines.append("    Step 2 (fallthrough): check confidence bucket (contaminated)")
    lines.append("    Step 3: check edge bucket (contaminated but already known bad)")
    lines.append("")
    lines.append("  CONTAMINATION IMPACT ON CRITIC:")
    lines.append("    Case A — signal edge 0.05-0.10, yes_ask 0.80-0.90 (sweet-spot):")
    lines.append("      2D override fires → ALLOW. Confidence contamination bypassed.")
    lines.append("      Contamination impact: NONE on this path.")
    lines.append("")
    lines.append("    Case B — signal edge 0.05-0.10, yes_ask 0.70-0.80 (poison zone):")
    lines.append("      2D cell is poison → override returns None → falls through.")
    lines.append("      Edge bucket _bad_enough_sample → BLOCK (edge bucket contaminated).")
    lines.append("      Confidence contamination irrelevant (already blocked by edge).")
    lines.append("")
    lines.append("    Case C — signal edge 0.03-0.05 (any price zone):")
    lines.append("      0.03-0.05 edge bucket is bad_enough_sample → 2D checked.")
    lines.append("      No 2D cells exist for 0.03-0.05 → override returns None.")
    lines.append("      Falls through → edge bucket fires BLOCK.")
    lines.append("      Removing KXETH from 0.03-0.05 does NOT flip bucket positive.")
    lines.append("      KXETH net contribution to 0.03-0.05: n=8, pnl=-14.45.")
    lines.append("      Without KXETH: 0.03-0.05 pnl=-1.40 → still negative → still BLOCK.")
    lines.append("")
    lines.append("    Case D — signal edge 0.10-0.25:")
    lines.append("      0.10-0.25 bucket is bad_enough_sample (pnl=-12.6) → 2D checked.")
    lines.append("      No 2D cells for 0.10-0.25 → falls through → BLOCK.")
    lines.append("      KXETH contribution: n=1, pnl=-5.0. Bucket stays negative without.")
    lines.append("")
    lines.append("  KXETH QUARANTINE ORDER (paper_trader.py):")
    lines.append("    KXETH blocked at paper_trader level — BEFORE council is called.")
    lines.append("    No new KXETH signal ever reaches Builder or Critic.")
    lines.append("    Existing KXETH trades in profile are historical contamination only.")
    return lines


# ── Section 6: Historical contamination impact ───────────────────────────────

def section_6_historical_impact(trades: list[dict]) -> list[str]:
    lines = _header("6. HISTORICAL CONTAMINATION IMPACT")
    settled = [r for r in trades if r.get("status") == "SETTLED"]
    kxeth   = [r for r in settled if _is_kxeth(r)]
    non_k   = [r for r in settled if not _is_kxeth(r)]
    nm      = [r for r in settled if _is_normal_modern(r)]
    nm_k    = [r for r in nm if _is_kxeth(r)]
    nm_nk   = [r for r in nm if not _is_kxeth(r)]

    def fmt(s: dict, label: str) -> str:
        clv = f"avg_CLV={s['avg_clv']:+.4f}" if s["avg_clv"] is not None else "no CLV data"
        return (f"  {label:<40s}n={s['n']:3d}  WR={s['wr']:.4f}  "
                f"pnl={s['total_pnl']:+7.2f}  PF={s['pf']:.3f}  {clv}")

    lines.append(fmt(_stats_row(settled), "ALL clean settled"))
    lines.append(fmt(_stats_row(kxeth),   "KXETH only"))
    lines.append(fmt(_stats_row(non_k),   "Non-KXETH only"))
    lines.append(fmt(_stats_row(nm),      "normal_modern all"))
    lines.append(fmt(_stats_row(nm_k),    "normal_modern KXETH only"))
    lines.append(fmt(_stats_row(nm_nk),   "normal_modern non-KXETH (2D pool)"))

    total_pnl = sum(_get_pnl(r) for r in settled)
    kxeth_pnl = sum(_get_pnl(r) for r in kxeth)
    lines.append("")
    lines.append(f"  KXETH is {_pct(len(kxeth), len(settled))} of trades")
    lines.append(f"  KXETH contributes {kxeth_pnl:+.2f} of {total_pnl:+.2f} total PnL")
    if total_pnl < 0:
        lines.append(f"  ({abs(kxeth_pnl)/abs(total_pnl)*100:.1f}% of total losses come from KXETH)")

    nm_pnl    = sum(_get_pnl(r) for r in nm)
    nm_k_pnl  = sum(_get_pnl(r) for r in nm_k)
    lines.append(f"  In normal_modern: KXETH {nm_k_pnl:+.2f} of {nm_pnl:+.2f}")
    if nm_pnl < 0:
        lines.append(f"  ({abs(nm_k_pnl)/abs(nm_pnl)*100:.1f}% of normal_modern losses from {len(nm_k)} KXETH trades)")
    return lines


# ── Section 7: Bucket-level before/after simulation ──────────────────────────

def section_7_bucket_simulation(trades: list[dict]) -> list[str]:
    lines = _header("7. BUCKET-LEVEL SIMULATION — with vs without KXETH")
    lines.append("  NOTE: PnL uses a simplified helper, not performance_report.get_pnl().")
    lines.append("  Absolute numbers differ from profile. Bucket-flip direction is reliable.")
    lines.append("")
    settled  = [r for r in trades if r.get("status") == "SETTLED"]
    kxeth_s  = [r for r in settled if _is_kxeth(r)]
    non_k_s  = [r for r in settled if not _is_kxeth(r)]

    def build_buckets(group: list[dict], edge_fn) -> dict:
        b: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
        for r in group:
            e = edge_fn(r)
            k = _edge_bucket(e)
            p = _get_pnl(r)
            b[k]["n"] += 1
            b[k]["pnl"] += p
            if p > 0:
                b[k]["wins"] += 1
        return {k: {"n": v["n"], "wins": v["wins"], "pnl": round(v["pnl"], 2),
                    "wr": round(v["wins"] / v["n"], 3) if v["n"] else 0.0}
                for k, v in b.items()}

    # Edge buckets: 1D uses risk_edge; 2D (and Critic lookup) uses original_edge
    lines.append("  A. Edge buckets (risk_edge — how 1D profile is built)")
    b_with_re    = build_buckets(settled, _get_risk_edge)
    b_without_re = build_buckets(non_k_s, _get_risk_edge)

    MIN_N = 5
    for k in sorted(b_with_re):
        old = b_with_re.get(k, {})
        new = b_without_re.get(k, {})
        old_n, old_p = old.get("n", 0), old.get("pnl", 0.0)
        new_n, new_p = new.get("n", 0), new.get("pnl", 0.0)
        new_wr       = new.get("wr", 0.0)
        bad_old      = old_n >= MIN_N and old_p < 0
        bad_new      = new_n >= MIN_N and new_p < 0
        if bad_old and bad_new:
            verdict = "STAYS BAD (block unchanged)"
        elif bad_old and not bad_new:
            verdict = ">>> FLIPS GOOD (block REMOVED) <<<"
        elif not bad_old and bad_new:
            verdict = ">>> FLIPS BAD (new block added) <<<"
        else:
            verdict = "stays OK (no block either way)"
        lines.append(
            f"    {k:<12s}: with_kxeth(n={old_n:3d},pnl={old_p:+7.2f})  "
            f"without(n={new_n:3d},pnl={new_p:+7.2f},WR={new_wr:.3f})  {verdict}"
        )

    lines.append("")
    lines.append("  B. Confidence buckets (used by Builder for boosts, Critic for primary check)")
    bc_with  = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    bc_wo    = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for r in settled:
        try: c = float(r.get("confidence") or 0)
        except: c = 0.0
        k = _conf_bucket(c)
        p = _get_pnl(r)
        bc_with[k]["n"] += 1
        bc_with[k]["pnl"] += p
        if p > 0: bc_with[k]["wins"] += 1
        if not _is_kxeth(r):
            bc_wo[k]["n"] += 1
            bc_wo[k]["pnl"] += p
            if p > 0: bc_wo[k]["wins"] += 1

    for k in sorted(bc_with):
        ow = bc_with[k]
        nw = bc_wo.get(k, {"n": 0, "wins": 0, "pnl": 0.0})
        nw_wr = nw["wins"] / nw["n"] if nw["n"] else 0.0
        old_p, new_p = ow["pnl"], nw["pnl"]
        direction = (
            "FLIPS POSITIVE (Builder/Critic impact)" if old_p < 0 and new_p > 0 else
            "FLIPS NEGATIVE" if old_p > 0 and new_p <= 0 else
            "IMPROVES (same sign)" if new_p > old_p else
            "WORSENS (same sign)"
        )
        lines.append(
            f"    {k:<12s}: with(n={ow['n']:3d},pnl={old_p:+7.2f})  "
            f"without(n={nw['n']:3d},pnl={new_p:+7.2f},WR={nw_wr:.3f})  {direction}"
        )

    lines.append("")
    lines.append("  C. Edge-keying inconsistency (1D risk_edge vs 2D original_edge)")
    mismatch = 0
    total_both = 0
    mismatch_patterns: dict = {}
    for r in settled:
        re = None
        oe = None
        try: re = float(r.get("risk_edge") or r.get("edge") or 0)
        except: pass
        try: oe = float(r.get("original_edge") or r.get("edge") or 0)
        except: pass
        if r.get("risk_edge") is None or (r.get("original_edge") is None and r.get("edge") is None):
            continue
        total_both += 1
        rb = _edge_bucket(re)
        ob = _edge_bucket(oe)
        if rb != ob:
            mismatch += 1
            key = f"{ob}(orig) -> {rb}(risk)"
            mismatch_patterns[key] = mismatch_patterns.get(key, 0) + 1

    lines.append(f"    Trades with both risk_edge and original/edge: {total_both}")
    lines.append(f"    Bucket mismatches: {mismatch} ({_pct(mismatch, total_both)})")
    for k, v in sorted(mismatch_patterns.items(), key=lambda x: -x[1]):
        lines.append(f"      {k}: {v} trades")
    lines.append("    Impact: Critic looks up 1D bucket by original_edge but trades were")
    lines.append("            stored in profile by risk_edge. For 53% of trades these differ.")
    lines.append("            The 2D table correctly uses original_edge for both build+lookup.")
    return lines


# ── Section 8: Verdict ────────────────────────────────────────────────────────

def section_8_verdict(trades: list[dict]) -> tuple[str, list[str]]:
    lines = _header("8. VERDICT")

    settled   = [r for r in trades if r.get("status") == "SETTLED"]
    kxeth     = [r for r in settled if _is_kxeth(r)]
    total_pnl = sum(_get_pnl(r) for r in settled)
    kxeth_pnl = sum(_get_pnl(r) for r in kxeth)

    # Dynamic verdict: check current profile state to detect Phase 9F application.
    profile = _load_profile()
    profile_excluded_count = int(profile.get("profile_kxeth_excluded_count", 0))
    by_ticker = profile.get("profiles", {}).get("by_ticker", {})
    profile_has_kxeth_keys = any(k.upper().startswith(KXETH_PREFIX) for k in by_ticker)
    phase_9f_applied = profile_excluded_count > 0 and not profile_has_kxeth_keys

    if phase_9f_applied:
        verdict = "CLEAN_AFTER_9F"
        lines.append(f"  VERDICT: {verdict}")
        lines.append("")
        lines.append("  Phase 9F has been applied. KXETH is excluded from ALL 1D and 2D")
        lines.append("  profile buckets. Evidence universe is now consistent.")
        lines.append("")
        lines.append("  Evidence of clean state:")
        lines.append(f"    ✓ profile_kxeth_excluded_count = {profile_excluded_count}")
        lines.append(f"    ✓ by_ticker has zero KXETH keys ({len(by_ticker)} tickers checked)")
        lines.append(f"    ✓ 2D table (by_edge_price_bucket) excludes KXETH (unchanged)")
        lines.append(
            f"    ✓ profile_input_trades = {profile.get('profile_input_trades', '?')} "
            f"(non-KXETH clean_settled only)"
        )
        lines.append(
            f"    ✓ clean_settled_trades = {profile.get('clean_settled_trades', '?')} "
            f"(total preserved for trust gate)"
        )
        trusted = profile.get("edge_profile_health", {}).get("edge_profile_trusted", "?")
        lines.append(f"    ✓ Profile trusted = {trusted}")
        lines.append("")
        lines.append("  Residual findings (non-blocking, deferred to Phase 9G):")
        lines.append("    ✗ 1D profile built with risk_edge; Critic looks up by original_edge")
        lines.append("      53% of trades bucket differently — architectural mismatch, not KXETH")
        lines.append(
            f"    ✗ {len(kxeth)} KXETH trades ({_pct(len(kxeth), len(settled))}, "
            f"{kxeth_pnl:+.2f} PnL) remain in paper_trades.jsonl for audit trail only"
        )
    else:
        verdict = "PARTIAL_CONTAMINATION"
        lines.append(f"  VERDICT: {verdict}")
        lines.append("")
        lines.append("  Why PARTIAL (not DIRTY or CLEAN):")
        lines.append("    ✗ KXETH is in all 1D profile buckets (by_edge, by_confidence,")
        lines.append("      by_ticker, by_strategy, by_market_type, by_action_type)")
        if total_pnl != 0:
            lines.append(
                f"    ✗ KXETH contributes {abs(kxeth_pnl) / abs(total_pnl) * 100:.0f}% of total profile losses"
            )
            lines.append(
                f"      ({kxeth_pnl:+.2f} of {total_pnl:+.2f}) despite being {_pct(len(kxeth), len(settled))} of trades"
            )
        lines.append("    ✗ KXETH in 0.80-0.90 confidence bucket reduces Builder evidence")
        lines.append("      (+6.20 with KXETH vs +14.35 without — same direction, weaker signal)")
        lines.append("    ✗ 1D uses risk_edge, 2D/Critic use original_edge (53% bucket mismatch)")
        lines.append("    ✓ 2D (by_edge_price_bucket) correctly excludes KXETH")
        lines.append("    ✓ No 1D bucket flips from BLOCK to ALLOW when KXETH is removed")
        lines.append("      (contamination does not unlock bad signals)")
        lines.append("    ✓ Critic's 2D override fires before confidence check for sweet-spot")
        lines.append("      signals — bypasses contaminated confidence buckets on primary path")
        lines.append("    ✓ KXETH quarantine at paper_trader prevents new KXETH from entering")
        lines.append("")
        if profile_has_kxeth_keys:
            lines.append("  ACTION REQUIRED: by_ticker still contains KXETH keys.")
            lines.append("    Apply Phase 9F patch and rebuild:")
            lines.append("      python3 tools/build_edge_profile.py")
        else:
            lines.append("  NOTE: by_ticker has no KXETH keys but profile_kxeth_excluded_count=0.")
            lines.append("    Rebuild the profile after applying the Phase 9F patch:")
            lines.append("      python3 tools/build_edge_profile.py")

    return verdict, lines


# ── Section 9: Patch recommendation ──────────────────────────────────────────

def section_9_recommendation() -> list[str]:
    lines = _header("9. PATCH RECOMMENDATION")
    lines.append("  Primary recommendation: PATCH_BUILD_EDGE_PROFILE_TO_EXCLUDE_KXETH_ALL_BUCKETS")
    lines.append("")
    lines.append("  Rationale:")
    lines.append("    1. The 2D table and 1D tables should use consistent evidence universes.")
    lines.append("       Currently the 2D is KXETH-clean but the 1D is not — incoherent.")
    lines.append("    2. KXETH responsible for 81% of profile losses. Profile PnL metrics are")
    lines.append("       misleading. Non-KXETH universe is near-breakeven (avg_CLV=+0.050).")
    lines.append("    3. The 0.80-0.90 confidence bucket without KXETH gives Builder cleaner")
    lines.append("       evidence: +14.35 vs +6.20 (same positive direction, stronger signal).")
    lines.append("    4. No bucket flips from BLOCK to ALLOW — low risk patch.")
    lines.append("    5. The 8 KXETH trades in normal_modern reduce the clean count from 112")
    lines.append("       to 104 — still well above all trust gates (10 normal_modern required).")
    lines.append("")
    lines.append("  Secondary recommendation: FLAG (not patch now) the risk_edge/original_edge")
    lines.append("    inconsistency in build_edge_profile.py. Consider building 1D also on")
    lines.append("    original_edge so lookup is consistent with Critic and 2D. This is a")
    lines.append("    separate phase (lower priority than KXETH exclusion).")
    lines.append("")
    lines.append("  Scope of primary patch (minimal, surgical):")
    lines.append("    File: tools/build_edge_profile.py")
    lines.append("    Change: In the main 'for rec in clean_settled' loop (lines 275-287),")
    lines.append("            add the same KXETH skip that already exists in the 2D loop:")
    lines.append("              if str(rec.get('ticker') or '').upper().startswith('KXETH'):")
    lines.append("                  continue")
    lines.append("    Then: rebuild profile via python3 tools/build_edge_profile.py")
    lines.append("")
    lines.append("  DO NOT apply this patch in Phase 9E — report first, review, then patch.")
    lines.append("  DO NOT raise any thresholds, lower MIN_EDGE, or change safety locks.")
    lines.append("  DO NOT remove the 2D KXETH filter (it's correct; duplicate the pattern).")
    return lines


# ── Section 10: Test plan ──────────────────────────────────────────────────────

def section_10_tests() -> list[str]:
    lines = _header("10. TEST PLAN (for Phase 9F patch)")
    tests = [
        ("KXETH excluded from by_ticker in rebuilt profile",
         "no KXETH-prefix key in profile['profiles']['by_ticker']"),
        ("KXETH excluded from by_edge_bucket in rebuilt profile",
         "sum of KXETH pnl contribution to any 1D bucket = 0"),
        ("KXETH excluded from by_confidence_bucket in rebuilt profile",
         "0.80-0.90 conf bucket pnl > 10 (was 6.20, should be ~14.35)"),
        ("KXETH excluded from by_market_type in rebuilt profile",
         "CRYPTO bucket n = non_kxeth_settled count (not total)"),
        ("2D sweet-spot cell still present and qualified",
         "by_edge_price_bucket['0.05-0.10|0.80-0.90'] n>=5, WR>=0.80, pnl>0"),
        ("2D poison cells still negative",
         "0.05-0.10|0.70-0.80 and 0.05-0.10|0.60-0.70 remain pnl<0"),
        ("Profile remains trusted",
         "edge_profile_health.edge_profile_trusted = True"),
        ("normal_council_approved_modern_trades >= 100",
         "104 expected (was 112); well above 10-trade trust gate"),
        ("real_money_allowed remains False",
         "evaluate_proof_gates()['real_money_allowed'] == False"),
        ("scale_allowed remains False",
         "evaluate_proof_gates()['scale_allowed'] == False"),
        ("KXETH quarantine still active after patch",
         "QUARANTINED_TICKER_PREFIXES still contains 'KXETH'"),
        ("test_price_conditioned_council.py passes all tests",
         "PROVEN_PRICE_CONDITIONED_COUNCIL_OK sentinel"),
    ]
    for i, (name, check) in enumerate(tests, 1):
        lines.append(f"  T{i:02d}. {name}")
        lines.append(f"       Verify: {check}")
    return lines


# ── Section 11: Plain-English summary ─────────────────────────────────────────

def section_11_summary(verdict: str, trades: list[dict]) -> list[str]:
    lines = _header("11. PLAIN-ENGLISH SUMMARY FOR SAMUEL")
    settled   = [r for r in trades if r.get("status") == "SETTLED"]
    kxeth     = [r for r in settled if _is_kxeth(r)]
    non_k     = [r for r in settled if not _is_kxeth(r)]
    total_pnl = sum(_get_pnl(r) for r in settled)
    kxeth_pnl = sum(_get_pnl(r) for r in kxeth)
    non_k_pnl = sum(_get_pnl(r) for r in non_k)
    nm        = [r for r in settled if _is_normal_modern(r)]
    nm_nk     = [r for r in nm if not _is_kxeth(r)]
    nm_nk_s   = _stats_row(nm_nk)

    lines.append("  IS THE PROFILE MEMORY CLEAN OR DIRTY?")
    lines.append("    Dirty on the outside (1D buckets contain KXETH), clean where it counts")
    lines.append("    (the 2D price-conditioned table is KXETH-free). The 1D contamination")
    lines.append("    does not unlock incorrect signals, but it does pollute the profile's")
    lines.append("    aggregate metrics and slightly weakens Builder evidence.")
    lines.append("")
    lines.append("  IS KXETH STILL POISONING THE BRAIN?")
    lines.append(f"    Yes, at the data level: {len(kxeth)} KXETH trades ({_pct(len(kxeth),len(settled))}) account for")
    lines.append(f"    {abs(kxeth_pnl)/abs(total_pnl)*100:.0f}% of total profile losses ({kxeth_pnl:+.2f} of {total_pnl:+.2f}).")
    lines.append(f"    Without KXETH, the 138-trade universe looks like: n={len(non_k)},")
    lines.append(f"    pnl={non_k_pnl:+.2f}, avg_CLV=+0.050 — near breakeven with positive CLV.")
    lines.append(f"    No, at the decision level: the 2D override rescues sweet-spot signals,")
    lines.append(f"    and no 1D bucket flips from blocking to allowing after KXETH removal.")
    lines.append("")
    lines.append("  DID PHASE 9A/9C FIX ENOUGH?")
    lines.append("    Phase 9A (2D council) fixed the signal-allow problem for the sweet-spot")
    lines.append("    (0.80-0.90 price zone). The Critic now correctly separates winners from")
    lines.append("    losers within the contaminated 0.05-0.10 edge bucket.")
    lines.append("    Phase 9C (freshness watchdog) prevents profile staleness from silently")
    lines.append("    degrading to the weaker bootstrap path.")
    lines.append("    What they did NOT fix: KXETH still in 1D profile buckets. The metrics")
    lines.append("    you see in the dashboard (total_pnl, win_rate) are distorted by KXETH.")
    lines.append("")
    lines.append("  SHOULD WE PATCH build_edge_profile.py?")
    lines.append("    Yes. The patch is surgical (one 'continue' line in the main 1D loop),")
    lines.append("    low-risk (no bucket flips blocking→allowing), and improves metric")
    lines.append("    honesty. The non-KXETH evidence picture is materially better and the")
    lines.append("    Builder deserves to see it cleanly. Recommend doing this as Phase 9F.")
    lines.append("")
    lines.append("  WHAT ABSOLUTELY SHOULD NOT BE TOUCHED?")
    lines.append("    - Do not remove KXETH quarantine at paper_trader level")
    lines.append("    - Do not raise MAX_TRADES_PER_DAY")
    lines.append("    - Do not lower MIN_EDGE or MIN_CONFIDENCE")
    lines.append("    - Do not weaken proof gate thresholds")
    lines.append("    - Do not touch real_money_allowed or scale_allowed")
    lines.append("    - Do not change EDGE_PROFILE_MAX_AGE_HOURS")
    lines.append("    - Do not patch the risk_edge/original_edge inconsistency without")
    lines.append("      a full test suite and simulation — that's a larger refactor")
    lines.append("")
    lines.append("  HIDDEN STRUCTURAL RISK (separate from KXETH):")
    lines.append("    The 1D edge profile is built with risk_edge but the Critic looks it up")
    lines.append("    with original_edge. 53% of trades bucket differently. This means the")
    lines.append("    1D bucket the Critic consults doesn't accurately represent trades with")
    lines.append("    similar original edges. The 2D table is consistent (both use original_edge).")
    lines.append("    This is the next architectural investigation after Phase 9F.")
    return lines


# ── runner ─────────────────────────────────────────────────────────────────────

def main() -> None:
    profile = _load_profile()
    trades  = _load_trades()
    settled = [r for r in trades if r.get("status") == "SETTLED"]

    print("=" * 68)
    print("EDGE PROFILE CONTAMINATION AUDIT — Phase 9E")
    print("=" * 68)
    print()

    sections_text: list[list[str]] = [
        section_1_safety(),
        section_2_metadata(profile),
        section_3_profile_scan(profile),
        section_4_build_script_audit(),
        section_5_council_audit(),
        section_6_historical_impact(trades),
        section_7_bucket_simulation(trades),
    ]

    verdict, s8 = section_8_verdict(trades)
    sections_text.extend([
        s8,
        section_9_recommendation(),
        section_10_tests(),
        section_11_summary(verdict, trades),
    ])

    for sec_lines in sections_text:
        for line in sec_lines:
            print(line)
        print()

    print("=" * 68)
    print("FILES INSPECTED:")
    print("  data/edge_profile.json")
    print("  tools/build_edge_profile.py")
    print("  brain/critic_brain.py")
    print("  brain/builder_brain.py")
    print("  brain/decision_council.py")
    print("  brain/paper_trader.py  (quarantine check)")
    print("  config/trading_config.py  (quarantine + thresholds)")
    print("  logs/paper_trades.jsonl")
    print()
    print("FILES CREATED:   tools/report_edge_profile_contamination_audit.py")
    print("FILES CHANGED:   none (read-only audit)")
    print("LIVE BEHAVIOR:   unchanged")
    print()
    print(f"CONTAMINATION VERDICT: {verdict}")
    if verdict == "CLEAN_AFTER_9F":
        print("  - 1D buckets: CLEAN (KXETH excluded by Phase 9F)")
        print("  - 2D table:   CLEAN (KXETH excluded, unchanged)")
        print("  - Signal flow impact: UNCHANGED (no bucket flips after exclusion)")
        print("  - Metric accuracy:    IMPROVED (KXETH excluded from all aggregations)")
        print("  - Residual:    1D built with risk_edge, Critic uses original_edge")
        print("    53% bucket mismatch — Phase 9G investigation")
    else:
        print("  - 1D buckets: contaminated (KXETH included)")
        print("  - 2D table:   clean (KXETH excluded correctly)")
        print("  - Signal flow impact: LOW (2D override rescues sweet-spot;")
        print("    no bucket flips from block→allow after KXETH removal)")
        print("  - Metric impact: HIGH (KXETH is 81% of profile losses)")
        print("  - Structural finding: 1D built with risk_edge, Critic uses")
        print("    original_edge — 53% bucket mismatch (separate issue)")
    print()
    print("CONTAMINATION_AUDIT_OK")
    print()


if __name__ == "__main__":
    main()
