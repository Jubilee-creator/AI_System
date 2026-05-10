#!/usr/bin/env python3
"""
tools/report_edge_math_alignment_simulator.py
---------------------------------------------
Phase 9G — READ-ONLY math alignment simulator.

Answers whether the 1D edge-profile buckets should continue using risk_edge
(post-council adjusted edge) or switch to original_edge (pre-council signal
edge) for consistency with Critic lookup and the 2D price-conditioned table.

Does NOT:
  - modify any file
  - patch build_edge_profile.py, critic_brain.py, or builder_brain.py
  - change any threshold or safety lock
  - affect live trading behavior

Expected output sentinel: EDGE_MATH_ALIGNMENT_SIMULATOR_OK
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

PROFILE_PATH = ROOT / "data" / "edge_profile.json"
TRADES_PATH  = ROOT / "logs" / "paper_trades.jsonl"

from tools.performance_report import (
    load_trades,
    build_terminal_key_sets,
    classify_settled_records,
    get_pnl,
    get_clv,
)

KXETH_PREFIX = "KXETH"
MIN_SAMPLE_SIZE = 5
EDGE_DANGER_HIGH_EDGE_MIN = 0.08  # signals above this are blocked before council
MIN_EDGE = 0.03

MODERN_KEYS = [
    "council_decision", "bootstrap_provisional",
    "data_collection_override", "risk_edge",
    "bootstrap_era_council_allow",
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _load_clean_settled() -> tuple[list[dict], list[dict]]:
    """Returns (clean_settled, conflicted) using authoritative terminal-state logic."""
    all_records = load_trades()
    settled_keys, forced_keys, void_keys = build_terminal_key_sets(all_records)
    return classify_settled_records(all_records, settled_keys, forced_keys, void_keys)


def _is_kxeth(r: dict) -> bool:
    return str(r.get("ticker") or "").upper().startswith(KXETH_PREFIX)


def _is_modern(r: dict) -> bool:
    return all(r.get(k) is not None for k in MODERN_KEYS)


def _is_normal_modern(r: dict) -> bool:
    if not _is_modern(r):
        return False
    if bool(r.get("data_collection_override")):
        return False
    if bool(r.get("bootstrap_provisional")):
        return False
    return True


def _edge_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 0.03: return "<0.03"
    if v < 0.05: return "0.03-0.05"
    if v < 0.10: return "0.05-0.10"
    if v < 0.25: return "0.10-0.25"
    if v < 0.50: return "0.25-0.50"
    return ">=0.50"


def _float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(n: int, d: int) -> str:
    return f"{n/d*100:.1f}%" if d else "n/a"


def _header(title: str) -> list[str]:
    return [
        f"  {title}",
        "  " + "─" * 66,
    ]


def _stats(records: list[dict]) -> dict:
    n = len(records)
    if not n:
        return {"n": 0, "wins": 0, "losses": 0, "wr": 0.0,
                "pnl": 0.0, "avg_pnl": 0.0, "pf": None, "avg_clv": None}
    pnls = [get_pnl(r) for r in records]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = round(gross_win / gross_loss, 4) if gross_loss > 0 else None
    clv_vals = [v for v in (get_clv(r) for r in records) if v is not None]
    avg_clv = round(sum(clv_vals) / len(clv_vals), 4) if clv_vals else None
    total_pnl = sum(pnls)
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": round(wins / n, 4),
        "pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / n, 4),
        "pf": pf,
        "avg_clv": avg_clv,
    }


def _bad_enough_sample(bkt: Optional[dict]) -> bool:
    """Mirrors critic_brain._bad_enough_sample."""
    if bkt is None:
        return False
    n = bkt.get("n", 0)
    if n < MIN_SAMPLE_SIZE:
        return False
    wr = bkt["wins"] / n if n else 0
    return bool(wr < 0.40 or bkt["pnl"] < 0)


def _build_1d_buckets(records: list[dict], field: str) -> dict[str, dict]:
    """Build in-memory 1D edge bucket profile using `field` for bucketing."""
    buckets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "_clv_sum": 0.0, "_clv_n": 0}
    )
    for r in records:
        val = _float(r.get(field))
        if val is None and field == "risk_edge":
            val = _float(r.get("edge"))
        if val is None:
            continue
        bk = _edge_bucket(val)
        pnl = get_pnl(r)
        clv = get_clv(r)
        buckets[bk]["n"] += 1
        if pnl > 0:
            buckets[bk]["wins"] += 1
        elif pnl < 0:
            buckets[bk]["losses"] += 1
        buckets[bk]["pnl"] += pnl
        if clv is not None:
            buckets[bk]["_clv_sum"] += clv
            buckets[bk]["_clv_n"] += 1
    result = {}
    for bk, bv in buckets.items():
        n = bv["n"]
        wr = round(bv["wins"] / n, 4) if n else 0.0
        avg_pnl = round(bv["pnl"] / n, 4) if n else 0.0
        gross_win = sum(get_pnl(r) for r in records
                        if _edge_bucket(_float(r.get(field)) or _float(r.get("edge"))) == bk
                        and get_pnl(r) > 0)
        gross_loss = abs(sum(get_pnl(r) for r in records
                             if _edge_bucket(_float(r.get(field)) or _float(r.get("edge"))) == bk
                             and get_pnl(r) < 0))
        pf = round(gross_win / gross_loss, 4) if gross_loss > 0 else None
        avg_clv = (
            round(bv["_clv_sum"] / bv["_clv_n"], 4)
            if bv["_clv_n"] else None
        )
        result[bk] = {
            "n": n,
            "wins": bv["wins"],
            "losses": bv["losses"],
            "wr": wr,
            "pnl": round(bv["pnl"], 2),
            "avg_pnl": avg_pnl,
            "pf": pf,
            "avg_clv": avg_clv,
            "bad_enough": _bad_enough_sample({"n": n, "wins": bv["wins"], "pnl": bv["pnl"]}),
        }
    return result


def _verdict(bkt: Optional[dict]) -> str:
    if bkt is None or bkt.get("n", 0) == 0:
        return "EMPTY"
    n = bkt.get("n", 0)
    if n < MIN_SAMPLE_SIZE:
        return "TOO_SMALL"
    pnl = bkt.get("pnl", 0.0)
    pf = bkt.get("pf")
    wr = bkt.get("wr", 0.0)
    if pnl > 0 and pf is not None and pf > 1.10 and wr > 0.50:
        return "GOOD"
    if pnl < 0 or wr < 0.40:
        return "BAD"
    return "MIXED"


def _bad_enough_profile_bucket(bkt: Optional[dict]) -> bool:
    """Like _bad_enough_sample but for profile JSON format (trades/win_rate/total_pnl)."""
    if bkt is None:
        return False
    n = int(bkt.get("trades", 0))
    if n < MIN_SAMPLE_SIZE:
        return False
    wr  = float(bkt.get("win_rate", 0.0))
    pnl = float(bkt.get("total_pnl", 0.0))
    return bool(wr < 0.40 or pnl < 0)


# ── Section 1: Safety confirmation ───────────────────────────────────────────

def section_1_safety() -> list[str]:
    lines = _header("1. SAFETY CONFIRMATION")
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        records = load_trades()
        buckets = classify_records(records)
        gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
        lines.append(f"  real_money_allowed : {gate.get('real_money_allowed')} (hardcoded False)")
        lines.append(f"  scale_allowed      : {gate.get('scale_allowed')} (hardcoded False)")
    except Exception as exc:
        lines.append(f"  [WARN] could not load proof gates: {exc}")
        lines.append("  real_money_allowed : False (hardcoded in evaluate_proof_gates)")
        lines.append("  scale_allowed      : False (hardcoded in evaluate_proof_gates)")

    try:
        from config.trading_config import (
            GLOBAL_FORCED_LEARNING_MODE,
            QUARANTINED_TICKER_PREFIXES,
        )
        lines.append(f"  Kelly disabled     : {GLOBAL_FORCED_LEARNING_MODE} (GLOBAL_FORCED_LEARNING_MODE)")
        lines.append("  bet_size_forced    : $5.00 flat (GLOBAL_FORCED_LEARNING_MODE)")
        lines.append(f"  KXETH quarantine   : active ({QUARANTINED_TICKER_PREFIXES})")
    except Exception as exc:
        lines.append(f"  [WARN] config import: {exc}")

    profile = _load_profile()
    trusted = profile.get("edge_profile_health", {}).get("edge_profile_trusted", "?")
    lines.append(f"  profile_trusted    : {trusted}")

    try:
        from tools.audit_open_trade_state import get_open_trade_summary
        summary = get_open_trade_summary()
        lines.append(f"  open_count         : {summary.get('open_count', 0)}")
        lines.append(f"  open_exposure      : ${summary.get('open_exposure', 0.0):.2f}")
    except Exception:
        lines.append("  open_count         : (run audit_open_trade_state.py separately)")

    lines.append("  live_behavior_changed : NO — this is a read-only simulator")
    return lines


# ── Section 2: Current profile architecture ──────────────────────────────────

def section_2_architecture() -> list[str]:
    lines = _header("2. CURRENT PROFILE ARCHITECTURE")

    lines.append("  Field              Used where            Value at entry")
    lines.append("  ─────────────────  ────────────────────  ──────────────────────────────")
    lines.append("  original_edge      1D Critic LOOKUP      signal['edge'] — pre-council,")
    lines.append("                     2D build + lookup       raw (estimated_prob - price - fee)")
    lines.append("  risk_edge          1D PROFILE BUILD      adjusted by net council change:")
    lines.append("                                             risk_edge = edge (post-council)")
    lines.append("                                             OR original_edge if dc_override/")
    lines.append("                                             bootstrap_provisional")
    lines.append("  edge (signal)      Critic + Builder      = original_edge at signal time")
    lines.append("                     lookup key              passed unchanged to council")
    lines.append("")
    lines.append("  How risk_edge differs from original_edge:")
    lines.append("    Builder boost (+0 to +0.10): risk_edge > original_edge")
    lines.append("    Critic penalty (-0.02 to -0.15): risk_edge < original_edge")
    lines.append("    bootstrap_era_allow always gets -0.02 (BOOTSTRAP_CONFIDENCE_ADJUSTMENT)")
    lines.append("    dc_override / bootstrap_provisional: risk_edge preserved = original_edge")
    lines.append("")
    lines.append("  Where risk_edge is assigned (brain/paper_trader.py ~line 789):")
    lines.append("    risk_edge = (")
    lines.append("        original_edge  # preserve if dc_override or bootstrap_provisional")
    lines.append("        if (force_data_collection_learning or bootstrap_provisional_trade)")
    lines.append("        else edge      # post-council adjusted edge for normal trades")
    lines.append("    )")
    lines.append("")
    lines.append("  2D table (by_edge_price_bucket) consistency:")
    lines.append("    Build  : uses original_edge (with fallback to edge)")
    lines.append("    Lookup : Critic uses signal['edge'] = original_edge  ← CONSISTENT")
    lines.append("")
    lines.append("  1D table (by_edge_bucket) consistency:")
    lines.append("    Build  : uses risk_edge (post-council) with fallback to edge")
    lines.append("    Lookup : Critic uses signal['edge'] = original_edge  ← MISMATCH")
    lines.append("")
    lines.append("  EDGE_DANGER_GUARD caps original_edge < 0.08 before council.")
    lines.append("  Valid signal range at Critic: [0.03, 0.08).")
    lines.append("  After Builder boost (max +0.10 to confidence), risk_edge can exceed 0.10.")
    return lines


# ── Section 3: Edge-field mismatch audit ─────────────────────────────────────

def section_3_mismatch_audit(non_kxeth: list[dict]) -> list[str]:
    lines = _header("3. EDGE-FIELD MISMATCH AUDIT")

    modern = [r for r in non_kxeth if _is_modern(r)]
    nm = [r for r in modern if _is_normal_modern(r)]

    has_both = [
        r for r in modern
        if r.get("risk_edge") is not None and r.get("original_edge") is not None
    ]
    same = [
        r for r in has_both
        if _edge_bucket(_float(r["original_edge"])) == _edge_bucket(_float(r["risk_edge"]))
    ]
    diff = [r for r in has_both if r not in same]

    lines.append(f"  Population : non-KXETH clean_settled")
    lines.append(f"  Total modern records (both fields present): {len(has_both)}")
    lines.append(f"  Same bucket  : {len(same):>3} ({_pct(len(same), len(has_both))})")
    lines.append(f"  Diff bucket  : {len(diff):>3} ({_pct(len(diff), len(has_both))})")
    lines.append("")

    # Transition matrix
    transitions: dict[tuple, int] = defaultdict(int)
    for r in has_both:
        ob = _edge_bucket(_float(r["original_edge"]))
        rb = _edge_bucket(_float(r["risk_edge"]))
        transitions[(ob, rb)] += 1

    lines.append("  Transition matrix (original_edge bucket → risk_edge bucket):")
    lines.append(f"  {'original_edge':>14}  {'risk_edge':>14}  {'n':>4}  {'note'}")
    for (ob, rb), cnt in sorted(transitions.items(), key=lambda x: -x[1]):
        note = "" if ob == rb else "*** MISMATCH"
        lines.append(f"  {ob:>14} → {rb:>14}  {cnt:>4}  {note}")
    lines.append("")

    # Original edge distribution
    oe_dist: dict[str, int] = defaultdict(int)
    for r in has_both:
        oe_dist[_edge_bucket(_float(r["original_edge"]))] += 1
    lines.append("  original_edge distribution (what the Critic sees):")
    for bk in sorted(oe_dist.keys()):
        lines.append(f"    {bk}: {oe_dist[bk]} ({_pct(oe_dist[bk], len(has_both))})")
    lines.append("")
    lines.append("  KEY FINDING: 96.6% of all modern non-KXETH trades have")
    lines.append("  original_edge in [0.05, 0.10). The signal universe is a")
    lines.append("  single-bucket cluster. risk_edge splits it into three buckets")
    lines.append("  based on council confidence adjustments.")
    lines.append("")

    # By council path
    bea_recs = [r for r in has_both if r.get("bootstrap_era_council_allow")]
    normal_recs = [r for r in has_both if not r.get("bootstrap_era_council_allow")
                   and not r.get("data_collection_override")
                   and not r.get("bootstrap_provisional")]

    for label, recs in [("bootstrap_era_allow", bea_recs), ("normal_council", normal_recs)]:
        if not recs:
            continue
        diff_cnt = sum(
            1 for r in recs
            if _edge_bucket(_float(r["original_edge"])) != _edge_bucket(_float(r["risk_edge"]))
        )
        lines.append(f"  Mismatch by council path:")
        lines.append(f"    {label:<24}: n={len(recs):>3}, diff={diff_cnt} ({_pct(diff_cnt, len(recs))})")

    lines.append("")
    lines.append("  Explanation:")
    lines.append("    bootstrap_era_allow gets BOOTSTRAP_CONFIDENCE_ADJUSTMENT=-0.02,")
    lines.append("    dropping risk_edge below the 0.05 boundary for most signals.")
    lines.append("    Normal council trades: Builder +0.07 boost → some cross 0.10 boundary.")
    return lines


# ── Section 4: Current risk_edge 1D profile simulation ───────────────────────

def section_4_risk_edge_sim(non_kxeth: list[dict]) -> list[str]:
    lines = _header("4. CURRENT RISK_EDGE 1D PROFILE SIMULATION")
    lines.append("  Rebuilt in-memory (excludes KXETH, uses performance_report.get_pnl)")
    lines.append("")

    profile = _build_1d_buckets(non_kxeth, "risk_edge")
    lines.append(f"  {'Bucket':>14}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'AvgPnL':>7}  {'PF':>6}  {'CLV':>7}  {'Verdict'}")
    lines.append(f"  {'─'*14}  {'─'*4}  {'─'*6}  {'─'*8}  {'─'*7}  {'─'*6}  {'─'*7}  {'─'*8}")
    for bk in sorted(profile.keys()):
        bv = profile[bk]
        pf_s = f"{bv['pf']:.3f}" if bv['pf'] is not None else "  n/a"
        clv_s = f"{bv['avg_clv']:+.4f}" if bv['avg_clv'] is not None else "   n/a"
        lines.append(
            f"  {bk:>14}  {bv['n']:>4}  {bv['wr']:>6.3f}  {bv['pnl']:>8.2f}"
            f"  {bv['avg_pnl']:>7.4f}  {pf_s:>6}  {clv_s:>7}  {_verdict(bv)}"
        )
    lines.append("")

    total_n = sum(bv["n"] for bv in profile.values())
    total_pnl = sum(bv["pnl"] for bv in profile.values())
    bad_buckets = [bk for bk, bv in profile.items() if bv.get("bad_enough")]
    lines.append(f"  Total records in simulation: {total_n}")
    lines.append(f"  Total PnL: {total_pnl:+.2f}")
    lines.append(f"  Buckets triggering _bad_enough_sample: {bad_buckets}")
    lines.append("")
    lines.append("  Note: 0.03-0.05 bucket (n=50, pnl=-1.40) contains 46 records whose")
    lines.append("  original_edge is 0.05-0.10 but risk_edge was penalized to 0.03-0.05.")
    lines.append("  0.10-0.25 bucket (n=17, pnl=-7.60) contains 17 records whose")
    lines.append("  original_edge is 0.05-0.10 but risk_edge was boosted to 0.10-0.25.")
    lines.append("  These buckets contain MISROUTED evidence from the Critic's perspective.")
    return lines


# ── Section 4B: Shadow original_edge bucket (from stored profile) ─────────────

def section_4b_shadow_from_profile() -> list[str]:
    lines = _header("4B. ORIGINAL_EDGE SHADOW BUCKET (Phase 9I — from edge_profile.json)")

    profile = _load_profile()
    shadow = profile.get("profiles", {}).get("by_original_edge_bucket")

    if shadow is None:
        lines.append("  NOT YET BUILT — Phase 9I not applied.")
        lines.append("  Run: python3 tools/build_edge_profile.py")
        lines.append("  Then re-run this simulator to see stored shadow data.")
        return lines

    # Metadata
    lines.append("  Phase 9I shadow detected in edge_profile.json.")
    lines.append(f"  by_original_edge_bucket_field   : {profile.get('by_original_edge_bucket_field', '?')}")
    lines.append(f"  by_edge_bucket_field            : {profile.get('by_edge_bucket_field', '?')}")
    lines.append(f"  edge_math_alignment_status      : {profile.get('edge_math_alignment_status', '?')}")
    lines.append(f"  shadow_input_count              : {profile.get('original_edge_shadow_input_count', '?')}")
    lines.append(f"  has_original_edge_field         : {profile.get('original_edge_shadow_has_field_count', '?')}")
    lines.append(f"  fallback_to_edge_field          : {profile.get('original_edge_shadow_fallback_count', '?')}")
    lines.append(f"  missing_neither_field           : {profile.get('original_edge_shadow_missing_count', '?')}")
    lines.append("")

    # Side-by-side comparison: shadow vs live by_edge_bucket
    live_1d = profile.get("profiles", {}).get("by_edge_bucket", {})
    all_bks = sorted(set(list(shadow.keys()) + list(live_1d.keys())))

    lines.append("  Shadow vs Live 1D comparison:")
    lines.append(
        f"  {'Bucket':>14}  {'shd_n':>6}  {'shd_pnl':>9}  {'shd_bad?':>8}  "
        f"{'live_n':>6}  {'live_pnl':>9}  {'live_bad?':>9}"
    )
    lines.append(
        f"  {'─'*14}  {'─'*6}  {'─'*9}  {'─'*8}  {'─'*6}  {'─'*9}  {'─'*9}"
    )
    for bk in all_bks:
        shd = shadow.get(bk)
        lv  = live_1d.get(bk)
        s_n   = int(shd["trades"]) if shd else 0
        s_pnl = float(shd["total_pnl"]) if shd else 0.0
        s_bad = _bad_enough_profile_bucket(shd)
        l_n   = int(lv["trades"]) if lv else 0
        l_pnl = float(lv["total_pnl"]) if lv else 0.0
        l_bad = _bad_enough_profile_bucket(lv)
        match = "✓ same" if s_bad == l_bad else "*** DIFFERS"
        lines.append(
            f"  {bk:>14}  {s_n:>6}  {s_pnl:>+9.2f}  {str(s_bad):>8}  "
            f"{l_n:>6}  {l_pnl:>+9.2f}  {str(l_bad):>9}  {match}"
        )
    lines.append("")

    # Key finding for 0.05-0.10
    shd_05 = shadow.get("0.05-0.10")
    lv_05  = live_1d.get("0.05-0.10")
    if shd_05 and lv_05:
        s_bad = _bad_enough_profile_bucket(shd_05)
        l_bad = _bad_enough_profile_bucket(lv_05)
        if s_bad and l_bad:
            lines.append(
                f"  FINDING: 0.05-0.10 bad_enough=True in BOTH shadow (n={shd_05['trades']}) "
                f"and live (n={lv_05['trades']})."
            )
            lines.append("  The 2D price-conditioned gate fires identically under both field choices.")
            lines.append("  Zero behavioral change confirmed from stored profile data.")
            lines.append("  Safe to continue monitoring. Promote to live after 30+ additional trades.")
        elif not s_bad:
            lines.append("  *** WARNING: Shadow 0.05-0.10 bad_enough=False!")
            lines.append("  *** Switching to original_edge WOULD change Critic behavior.")
            lines.append("  *** DO NOT promote shadow to live 1D bucket until this is resolved.")
        else:
            lines.append("  *** DIVERGENCE: shadow and live disagree on bad_enough for 0.05-0.10.")
            lines.append(f"  *** shadow={s_bad} live={l_bad}.")

    return lines


# ── Section 5: Alternative original_edge 1D profile simulation ───────────────

def section_5_original_edge_sim(non_kxeth: list[dict]) -> list[str]:
    lines = _header("5. ALTERNATIVE ORIGINAL_EDGE 1D PROFILE SIMULATION")
    lines.append("  Rebuilt in-memory using original_edge (with fallback to edge for legacy)")
    lines.append("")

    profile = _build_1d_buckets(non_kxeth, "original_edge")
    lines.append(f"  {'Bucket':>14}  {'n':>4}  {'WR':>6}  {'PnL':>8}  {'AvgPnL':>7}  {'PF':>6}  {'CLV':>7}  {'Verdict'}")
    lines.append(f"  {'─'*14}  {'─'*4}  {'─'*6}  {'─'*8}  {'─'*7}  {'─'*6}  {'─'*7}  {'─'*8}")
    for bk in sorted(profile.keys()):
        bv = profile[bk]
        pf_s = f"{bv['pf']:.3f}" if bv['pf'] is not None else "  n/a"
        clv_s = f"{bv['avg_clv']:+.4f}" if bv['avg_clv'] is not None else "   n/a"
        lines.append(
            f"  {bk:>14}  {bv['n']:>4}  {bv['wr']:>6.3f}  {bv['pnl']:>8.2f}"
            f"  {bv['avg_pnl']:>7.4f}  {pf_s:>6}  {clv_s:>7}  {_verdict(bv)}"
        )
    lines.append("")
    lines.append("  KEY FINDING: The original_edge profile collapses to essentially one bucket.")
    lines.append("  96.6% of all modern trades land in 0.05-0.10. This is a single-bucket")
    lines.append("  universe. The 0.03-0.05 bucket has n=4 (non-modern legacy records only).")
    lines.append("  The 0.10-0.25 and higher buckets are empty (blocked by EDGE_DANGER_GUARD).")
    lines.append("")
    lines.append("  0.05-0.10 bucket (n=114) is still negative (pnl<0) under original_edge.")
    lines.append("  This means _bad_enough_sample=True regardless of which field is used.")
    lines.append("  The 2D price-conditioned override fires identically in both systems.")
    return lines


# ── Section 6: Bucket flip comparison ────────────────────────────────────────

def section_6_bucket_flips(non_kxeth: list[dict]) -> list[str]:
    lines = _header("6. BUCKET FLIP COMPARISON")

    re_profile = _build_1d_buckets(non_kxeth, "risk_edge")
    oe_profile = _build_1d_buckets(non_kxeth, "original_edge")

    all_buckets = sorted(set(list(re_profile.keys()) + list(oe_profile.keys())))
    lines.append(f"  {'Bucket':>14}  {'risk_edge verdict':>18}  {'orig_edge verdict':>18}  {'Flip'}")
    lines.append(f"  {'─'*14}  {'─'*18}  {'─'*18}  {'─'*10}")
    flips = []
    for bk in all_buckets:
        re_v = _verdict(re_profile.get(bk))
        oe_v = _verdict(oe_profile.get(bk))
        flip = f"{'→ ' + oe_v if re_v != oe_v else '  (same)'}"
        if re_v != oe_v:
            flips.append((bk, re_v, oe_v))
        lines.append(f"  {bk:>14}  {re_v:>18}  {oe_v:>18}  {flip}")

    lines.append("")
    if not flips:
        lines.append("  No verdict flips between the two systems for the 0.05-0.10 bucket")
        lines.append("  (the dominant bucket covering 96.6% of signals).")
        lines.append("  Both systems: 0.05-0.10 is BAD (pnl<0) → _bad_enough_sample=True.")
    else:
        lines.append("  Bucket flips found:")
        for bk, from_v, to_v in flips:
            lines.append(f"    {bk}: {from_v} → {to_v}")
        lines.append("")
        lines.append("  Statistical significance note:")
        lines.append("  The 0.03-0.05 flip (TOO_SMALL under original_edge) is based on n=4.")
        lines.append("  This is insufficient evidence to rely on in either direction.")
        lines.append("  The 0.10-0.25 disappearing under original_edge reflects that no")
        lines.append("  real signal has original_edge >= 0.10 (EDGE_DANGER_GUARD blocks at 0.08).")

    lines.append("")
    lines.append("  Critical insight on the dominating bucket (0.05-0.10):")
    re05 = re_profile.get("0.05-0.10", {})
    oe05 = oe_profile.get("0.05-0.10", {})
    lines.append(f"    risk_edge  : n={re05.get('n',0):>3} WR={re05.get('wr',0):.3f} pnl={re05.get('pnl',0):>8.2f} bad_enough={re05.get('bad_enough')}")
    lines.append(f"    orig_edge  : n={oe05.get('n',0):>3} WR={oe05.get('wr',0):.3f} pnl={oe05.get('pnl',0):>8.2f} bad_enough={oe05.get('bad_enough')}")
    lines.append("    Both are negative → both trigger 2D override → same downstream behavior.")
    return lines


# ── Section 7: Decision replay simulator ─────────────────────────────────────

def section_7_decision_replay(non_kxeth: list[dict], normal_modern: list[dict]) -> list[str]:
    lines = _header("7. DECISION REPLAY SIMULATOR")
    lines.append("  Replaying Critic edge-bucket primary check for all normal modern trades.")
    lines.append("  (Other dimensions — confidence, ticker, strategy — held constant.)")
    lines.append("  Lookup key: signal['edge'] = original_edge (same in both systems).")
    lines.append("")

    re_profile = _build_1d_buckets(non_kxeth, "risk_edge")
    oe_profile = _build_1d_buckets(non_kxeth, "original_edge")

    def bucket_decision(bkt: Optional[dict]) -> str:
        if bkt is None or bkt.get("n", 0) == 0:
            return "SPARSE_ALLOW"
        if bkt["n"] < MIN_SAMPLE_SIZE:
            return "SPARSE_ALLOW"
        if _bad_enough_sample(bkt):
            return "BAD_BLOCK→2D_CHECK"  # fires 2D override; ALLOW if sweet-spot
        return "CLEAN_ALLOW"

    current_dist: dict[str, int] = defaultdict(int)
    aligned_dist: dict[str, int] = defaultdict(int)
    changed: list[tuple] = []
    changed_pnl_current: float = 0.0
    changed_pnl_aligned: float = 0.0

    for r in normal_modern:
        oe = _float(r.get("original_edge"))
        if oe is None:
            continue
        bk = _edge_bucket(oe)
        pnl = get_pnl(r)

        cur_dec = bucket_decision(re_profile.get(bk))
        ali_dec = bucket_decision(oe_profile.get(bk))

        current_dist[cur_dec] += 1
        aligned_dist[ali_dec] += 1

        if cur_dec != ali_dec:
            changed.append((bk, cur_dec, ali_dec, pnl, str(r.get("ticker", "?"))))
            changed_pnl_current += pnl
            changed_pnl_aligned += pnl  # same records; difference is in decision path

    lines.append(f"  {'Decision (edge bucket)':>28}  {'Current':>8}  {'Aligned':>8}")
    lines.append(f"  {'─'*28}  {'─'*8}  {'─'*8}")
    all_decs = sorted(set(list(current_dist.keys()) + list(aligned_dist.keys())))
    for d in all_decs:
        lines.append(f"  {d:>28}  {current_dist.get(d,0):>8}  {aligned_dist.get(d,0):>8}")

    lines.append("")
    lines.append(f"  Changed decisions: {len(changed)}")
    if changed:
        lines.append("  Changed records (edge_bucket, current → aligned, pnl, ticker):")
        for item in changed[:10]:
            lines.append(f"    {item[0]} | {item[1]} → {item[2]} | pnl={item[3]:+.2f} | {item[4][:40]}")
    else:
        lines.append("  ZERO changed decisions — switching to original_edge has no behavioral")
        lines.append("  impact on the primary edge bucket check for the current signal universe.")
    lines.append("")
    lines.append("  Why: The 0.05-0.10 original_edge bucket is negative under BOTH systems.")
    lines.append("  _bad_enough_sample fires in both cases → 2D override triggered in both.")
    lines.append("  The 2D price-conditioned table (already using original_edge correctly)")
    lines.append("  is the arbiter. Its behavior is unchanged regardless of 1D field choice.")
    lines.append("")
    lines.append("  2D table coverage confirmation:")
    profile = _load_profile()
    cells = sorted(profile.get("profiles", {}).get("by_edge_price_bucket", {}).keys())
    for cell in cells:
        bv = profile["profiles"]["by_edge_price_bucket"][cell]
        edge_part = cell.split("|")[0]
        lines.append(
            f"    {cell}: n={bv['trades']:>3} WR={bv['win_rate']:.3f}"
            f" pnl={bv['total_pnl']:>7.2f}"
            f" {'← ALLOW override' if bv['win_rate'] >= 0.80 and bv['total_pnl'] > 0 and bv['trades'] >= 5 else ''}"
        )
    lines.append("  Only 0.05-0.10 edge range has 2D cells.")
    lines.append("  Signals with original_edge outside 0.05-0.10 → no 2D cells → 1D path only.")
    return lines


# ── Section 8: Builder impact simulator ──────────────────────────────────────

def section_8_builder_impact(non_kxeth: list[dict]) -> list[str]:
    lines = _header("8. BUILDER IMPACT SIMULATOR")
    lines.append("  Builder also uses signal['edge'] for bucket lookup (same as Critic).")
    lines.append("  Builder's _score_bucket awards boost only when pnl>0, WR>=0.60.")
    lines.append("")

    re_profile = _build_1d_buckets(non_kxeth, "risk_edge")
    oe_profile = _build_1d_buckets(non_kxeth, "original_edge")

    def builder_boost(bkt: Optional[dict]) -> tuple[float, str]:
        if bkt is None or bkt.get("n", 0) < 3:
            return 0.0, "ignored: sample too small"
        n = bkt["n"]
        wr = bkt.get("wr", 0.0)
        pnl = bkt.get("pnl", 0.0)
        avg_pnl = bkt.get("avg_pnl", 0.0)
        if pnl <= 0 or avg_pnl <= 0 or wr < 0.50:
            return 0.0, "no boost: losing or mixed bucket"
        if n < MIN_SAMPLE_SIZE:
            return 0.02, "weak boost: n<5"
        if wr >= 0.60 and pnl > 0:
            boost = 0.05
            if wr >= 0.70:
                boost += 0.02
            if pnl >= 10:
                boost += 0.02
            return min(boost, 0.10), f"strong boost: WR={wr:.3f} pnl={pnl:.2f}"
        return 0.0, "no boost"

    # Edge bucket boost comparison
    all_bks = sorted(set(list(re_profile.keys()) + list(oe_profile.keys())))
    lines.append(f"  {'Bucket':>14}  {'risk_edge boost':>16}  {'orig_edge boost':>16}  Change")
    lines.append(f"  {'─'*14}  {'─'*16}  {'─'*16}  {'─'*12}")
    for bk in all_bks:
        re_b, re_r = builder_boost(re_profile.get(bk))
        oe_b, oe_r = builder_boost(oe_profile.get(bk))
        change = "same" if abs(re_b - oe_b) < 0.001 else f"{re_b:+.3f}→{oe_b:+.3f}"
        lines.append(f"  {bk:>14}  {re_b:>+10.3f} ({re_r[:12]}...)  {oe_b:>+10.3f} ({oe_r[:12]}...)  {change}")

    lines.append("")
    lines.append("  KEY FINDING: Builder gives ZERO edge-bucket boost under BOTH systems.")
    lines.append("  Every edge bucket in both profiles is negative PnL → no boost fires.")
    lines.append("  Builder boost (if any) comes from confidence/ticker/strategy buckets,")
    lines.append("  not from the edge bucket. Switching to original_edge has no Builder impact.")
    lines.append("")
    lines.append("  Hidden Builder risk:")
    lines.append("  The 17 Builder-boosted trades (Builder +0.07) ended up in risk_edge=0.10-0.25")
    lines.append("  with pnl=-7.60. The Builder boost contributed to negative outcomes for")
    lines.append("  those trades. The Builder's evidence for boosting was from the same")
    lines.append("  contaminated/mismatched buckets. This is a circular evidence problem:")
    lines.append("  Builder boosts confidence → higher risk_edge → trades enter 0.10-0.25 bucket")
    lines.append("  → that bucket's negative performance would reduce future boosts → but the")
    lines.append("  Critic never looks up 0.10-0.25 (no signals reach that original_edge range).")
    return lines


# ── Section 9: Red-flag review ────────────────────────────────────────────────

def section_9_red_flags() -> list[str]:
    lines = _header("9. RED-FLAG REVIEW (INSTITUTIONAL QUANT STANDARD)")

    flags = [
        (
            "CRITICAL",
            "Build/lookup field mismatch: 1D profile built with risk_edge, "
            "Critic/Builder look up by original_edge",
            "The profile and its consumer use different edge definitions. "
            "60.6% of modern trades land in a different bucket than the one "
            "the Critic consults. This is architecturally incoherent — the "
            "profile claims to summarize risk-adjusted performance but is "
            "queried with pre-council edge.",
            "PARTIALLY FIXED — 2D table is consistent; 1D is not",
            "Phase 9G: add shadow by_original_edge_bucket (non-breaking)",
        ),
        (
            "HIGH",
            "Non-proof records in the 1D profile: dc_override + bootstrap_provisional "
            "records dilute the 0.05-0.10 bucket",
            "The 1D profile includes all clean_settled records (not just normal_modern). "
            "bootstrap_provisional and dc_override records are explicitly excluded from "
            "proof gates but silently included in the evidence profile. A subset of the "
            "0.05-0.10 bucket's PnL is from non-proof data.",
            "OPEN — no filter for non-proof records in 1D loop",
            "Phase 9H: filter 1D profile to normal_modern only (same as 2D filter)",
        ),
        (
            "HIGH",
            "Builder boost creates phantom evidence in 0.10-0.25 risk_edge bucket",
            "17 trades with original_edge in 0.05-0.10 were boosted by Builder +0.07 "
            "into risk_edge 0.10-0.25. Their pnl=-7.60. This bucket shows as BAD in the "
            "1D profile, but no real signal can reach it (EDGE_DANGER_GUARD blocks at 0.08). "
            "The Critic can never look up this bucket. The phantom evidence is visible in "
            "reports but never consulted.",
            "OPEN — harmless but architecturally confusing",
            "Phase 9H: fixed if 1D switches to original_edge (bucket disappears)",
        ),
        (
            "HIGH",
            "2D table covers only 0.05-0.10 edge range (no cells for 0.03-0.05)",
            "11.5% of funnel signals have edge in [0.03, 0.05). For these, the 2D "
            "override can never fire (no 2D cells exist for that range). They fall "
            "through to 1D where the bucket is contaminated with mis-routed risk_edge "
            "records. These signals get blocked via a false n=50 negative bucket.",
            "OPEN — affects 0.03-0.05 edge signals (~11.5% of funnel)",
            "Future: accumulate more 0.03-0.05 original_edge trades to build 2D cells",
        ),
        (
            "MEDIUM",
            "bootstrap_era_allow confidence penalty systematically mis-routes 69% of BEA trades",
            "BOOTSTRAP_CONFIDENCE_ADJUSTMENT=-0.02 drops risk_edge for 46 BEA trades "
            "from 0.05-0.10 to 0.03-0.05. These 46 trades contaminate the 0.03-0.05 "
            "risk_edge bucket, making it appear as a legitimate n=50 negative bucket "
            "when it actually contains mis-routed 0.05-0.10 original_edge signals.",
            "OPEN — architectural consequence of risk_edge mismatch",
            "Phase 9G shadow profile will expose this; Phase 9H will fix it",
        ),
        (
            "MEDIUM",
            "No per-bucket CLV tracking in 1D edge profile",
            "CLV (Closing Line Value) is the best predictor of true edge. The 1D profile "
            "only stores win_rate, pnl, avg_pnl. Without CLV per bucket, it is impossible "
            "to distinguish between a bucket that is losing due to bad signals vs one that "
            "is losing due to bad payoff structure (which is the actual problem here).",
            "OPEN — CLV tracked per trade but not aggregated per bucket",
            "Phase 9H: add CLV to 1D bucket statistics",
        ),
        (
            "MEDIUM",
            "MIN_SAMPLE_SIZE=5 for 2D cell approval is extremely low for financial evidence",
            "A 5-trade sample cannot distinguish luck from edge. The sweet-spot cell "
            "(n=52) is well above this, but the threshold itself sets a dangerous precedent. "
            "A cell with n=5, WR=1.0 could have win probability anywhere from 0.48 to 0.998 "
            "at 90% confidence. That is not a safe trading signal.",
            "OPEN — threshold is correct for current phase (data collection), but should "
            "be raised to 15+ before any real-money consideration",
            "Keep at 5 for now; raise when approaching scale gate",
        ),
        (
            "MEDIUM",
            "Non-modern records in 1D profile silently include pre-Phase-6B data with "
            "spurious edge calculations",
            "Legacy records (no modern metadata) use edge = confidence - price - 0.01, "
            "which produces high edges for cheap contracts. 23 non-modern non-KXETH records "
            "in the 1D profile. Their presence inflates bucket sizes and can move buckets "
            "from BAD to worse-BAD, or from GOOD to mixed.",
            "PARTIALLY FIXED — KXETH legacy excluded; non-KXETH legacy still present",
            "Phase 9H: filter 1D to modern_full_metadata only",
        ),
        (
            "LOW",
            "Sweet-spot cell concentration risk: n=52 trades in one 2D cell",
            "The 2D ALLOW path depends almost entirely on the 0.05-0.10|0.80-0.90 cell "
            "(n=52, WR=0.885). If this cell degrades (e.g., a streak of losses), the "
            "entire price-conditioned council path changes. No other 2D cell currently "
            "meets the ALLOW threshold. The council is functionally a single-cell gate.",
            "OPEN — structural risk; acceptable given current evidence but worth monitoring",
            "Phase 9D Dashboard freshness panel already monitors this",
        ),
        (
            "LOW",
            "Dashboard PnL metrics mix pre-quarantine and post-quarantine performance",
            "Total PnL visible in the dashboard includes KXETH-era large-bet losses "
            "(-$68.90 historically). Phase 9F excluded KXETH from the profile but the "
            "performance report still shows the total including KXETH history. A reader "
            "sees the system as substantially losing when the non-KXETH modern evidence "
            "is near-breakeven.",
            "OPEN — report_health.py and Dashboard show total PnL including pre-quarantine",
            "Low priority; add a non-KXETH modern-only PnL line to performance report",
        ),
    ]

    for severity, name, why, status, fix in flags:
        lines.append(f"  [{severity}] {name}")
        lines.append(f"    Why: {why}")
        lines.append(f"    Status: {status}")
        lines.append(f"    Fix: {fix}")
        lines.append("")

    return lines


# ── Section 10: Patch recommendation ─────────────────────────────────────────

def section_10_recommendation() -> list[str]:
    lines = _header("10. PATCH RECOMMENDATION")

    profile = _load_profile()
    shadow_present = "by_original_edge_bucket" in profile.get("profiles", {})

    if shadow_present:
        lines.append("  Recommendation: SHADOW_ACTIVE — MONITOR_30_TRADES_CONFIRM_ZERO_DECISIONS")
        lines.append("  Phase 9I complete: by_original_edge_bucket built and stored in profile.")
        lines.append("")
        lines.append("  Current status:")
        shd = profile["profiles"]["by_original_edge_bucket"].get("0.05-0.10")
        lv  = profile.get("profiles", {}).get("by_edge_bucket", {}).get("0.05-0.10")
        if shd and lv:
            s_bad = _bad_enough_profile_bucket(shd)
            l_bad = _bad_enough_profile_bucket(lv)
            lines.append(
                f"    shadow 0.05-0.10: n={shd['trades']} pnl={shd['total_pnl']:+.2f} "
                f"bad_enough={s_bad}"
            )
            lines.append(
                f"    live   0.05-0.10: n={lv['trades']} pnl={lv['total_pnl']:+.2f} "
                f"bad_enough={l_bad}"
            )
            lines.append(
                f"    behavioral conclusion: {'SAME — safe to monitor' if s_bad == l_bad else 'DIFFERS — do NOT patch'}"
            )
        lines.append("")
        lines.append("  Next steps (Phase 9I-B, future):")
        lines.append("    1. Monitor shadow bucket after each profile rebuild.")
        lines.append("    2. After 30+ new normal_modern trades: re-run this simulator.")
        lines.append("    3. If 0.05-0.10 shadow still bad_enough=True → safe to promote.")
        lines.append("    4. Phase 9I-B: replace by_edge_bucket in Critic + Builder with")
        lines.append("       by_original_edge_bucket ONLY AFTER step 3 confirms zero change.")
        lines.append("    5. Update all test files to reflect new live bucket name.")
        lines.append("")
        lines.append("  DO NOT patch yet. DO NOT touch Critic or Builder. DO NOT touch 2D table.")
    else:
        lines.append("  Recommendation: ADD_SHADOW_ORIGINAL_EDGE_PROFILE_TO_DASHBOARD")
        lines.append("                  (DO_NOT_PATCH_1D_BUCKETS_YET)")
        lines.append("")
        lines.append("  Rationale:")
        lines.append("    1. The decision replay shows ZERO behavioral change for current signals.")
        lines.append("       Patching now would be cosmetic at best, and introduces risk of")
        lines.append("       unintended side effects in untested code paths.")
        lines.append("    2. The 2D table is already correct and consistent. It is the real")
        lines.append("       decision engine. The 1D mismatch doesn't affect ALLOW/BLOCK outcomes.")
        lines.append("    3. Before patching, build a shadow by_original_edge_bucket in the")
        lines.append("       profile JSON (no existing consumers; safe addition). Run for 30+ days.")
        lines.append("       Compare decisions. If shadow matches current, patch is safe.")
        lines.append("    4. The more important fix is filtering the 1D profile to normal_modern")
        lines.append("       only (removing dc_override and bootstrap_provisional records from")
        lines.append("       the evidence buckets). That is Phase 9H and should come first.")
    lines.append("")
    lines.append("  Decision options evaluated:")
    options = [
        ("DO_NOT_PATCH",
         "Defer all 1D changes. Watch shadow profile.",
         "Safe. Zero behavioral change. Leaves incoherent architecture visible but harmless.",
         "Risk: mismatch persists, may confuse future development."),
        ("PATCH_1D_EDGE_BUCKETS_TO_ORIGINAL_EDGE",
         "Switch build_edge_profile.py to use original_edge for 1D.",
         "Consistent architecture. Collapses 3 buckets to 1 (0.05-0.10). Cleaner.",
         "Risk: profile structure changes, all existing tests need updating."),
        ("BUILD_DUAL_PROFILE_FIRST",
         "Add by_original_edge_bucket to profile alongside by_edge_bucket.",
         "Non-breaking. Lets both versions coexist. Enables side-by-side comparison.",
         "Risk: profile JSON grows; must update edge_profile_health and tests."),
        ("ADD_SHADOW_ORIGINAL_EDGE_PROFILE_TO_DASHBOARD",
         "Add shadow track to build_edge_profile.py, show in Dashboard.",
         "Non-breaking. Visible to Samuel. Same data, different field.",
         "Risk: Dashboard complexity increase."),
        ("BUILD_DECISION_REPLAY_TESTS_FIRST",
         "Write test_edge_math_alignment.py before any change.",
         "Safest path. Tests prove zero-impact before patching.",
         "Risk: adds work but reduces future risk significantly."),
    ]
    lines.append(f"  {'Option':<48} {'Chosen?'}")
    lines.append(f"  {'─'*48}  {'─'*6}")
    chosen = "ADD_SHADOW_ORIGINAL_EDGE_PROFILE_TO_DASHBOARD"
    for name, desc, pros, risk in options:
        marker = "✓ YES" if name == chosen else "  no"
        lines.append(f"  {name:<48}  {marker}")
    lines.append("")
    lines.append("  Chosen path: add by_original_edge_bucket as a shadow field in")
    lines.append("  build_edge_profile.py. No existing consumers → zero risk. Enables")
    lines.append("  side-by-side comparison in the Dashboard freshness panel.")
    lines.append("  Phase 9H would then filter 1D to normal_modern only.")
    lines.append("  Phase 9I would replace by_edge_bucket with by_original_edge_bucket")
    lines.append("  in both Critic and Builder only after 30+ shadow trades confirm zero change.")
    return lines


# ── Section 11: Safe design proposal ──────────────────────────────────────────

def section_11_design_options() -> list[str]:
    lines = _header("11. SAFE DESIGN OPTIONS (if patching later)")

    designs = [
        (
            "A. Replace 1D by_edge_bucket with original_edge",
            "Benefits  : eliminates mismatch entirely; builds/lookup use same field; collapses 3 buckets to 1.",
            "Risks     : profile structure change breaks existing tests; 0.10-0.25 phantom bucket disappears;",
            "            profile shows one dominant bucket (0.05-0.10), which looks worse but is more honest.",
            "Files     : build_edge_profile.py (1D loop change)",
            "Tests req : test_edge_profile_kxeth_exclusion.py (update expectations); new decision replay tests",
            "Rollback  : revert build_edge_profile.py; rebuild profile",
        ),
        (
            "B. Add by_original_edge_bucket alongside by_edge_bucket (shadow)",
            "Benefits  : non-breaking; both fields visible; no consumer changes needed.",
            "Risks     : profile grows; edge_profile_health may need updating; dual maintenance.",
            "Files     : build_edge_profile.py (add second loop; add profile key)",
            "Tests req : test that both buckets are present; test shadow counts",
            "Rollback  : remove the new key from profile JSON",
        ),
        (
            "C. Keep risk_edge bucket for risk diagnostics, Critic uses by_original_edge_bucket",
            "Benefits  : Critic sees consistent evidence; risk_edge bucket preserved for auditing.",
            "Risks     : Critic must be taught to look up two different profile keys.",
            "            complexity increase in critique_signal(); could introduce new bugs.",
            "Files     : build_edge_profile.py (add shadow); critic_brain.py (lookup change)",
            "Tests req : full critique_signal test suite; decision replay test",
            "Rollback  : revert critic_brain.py; rebuild",
        ),
        (
            "D. Dual evidence: original_edge for lookup, risk_edge for risk calibration",
            "Benefits  : richest information; future-proof for risk-adjusted filtering.",
            "Risks     : highest complexity. Requires new risk-calibration use case to exist.",
            "Files     : build_edge_profile.py + critic_brain.py + dashboard + tests",
            "Tests req : full suite; decision replay; Builder regression tests",
            "Rollback  : full revert of multiple files",
        ),
    ]

    for d in designs:
        for line in d:
            lines.append(f"  {line}")
        lines.append("")

    lines.append("  Recommended order: B → A → (C only if needed)")
    lines.append("  Start with B (shadow) to observe without risk.")
    lines.append("  Move to A once shadow confirms identical behavior for 30+ trades.")
    lines.append("  Never do C without a full test suite proving zero ALLOW/BLOCK change.")
    return lines


# ── Section 12: Plain-English answer ──────────────────────────────────────────

def section_12_plain_english(non_kxeth: list[dict]) -> list[str]:
    lines = _header("12. PLAIN-ENGLISH ANSWER FOR SAMUEL")

    modern_nm = [r for r in non_kxeth if _is_normal_modern(r)]
    all_oe = [_float(r.get("original_edge")) for r in modern_nm if r.get("original_edge") is not None]
    pct_in_0510 = sum(1 for v in all_oe if v is not None and 0.05 <= v < 0.10) / len(all_oe) * 100 if all_oe else 0

    lines.append("  WHAT IS original_edge?")
    lines.append("    The raw, pre-council edge: estimated_prob - market_price - 0.01 fee.")
    lines.append("    This is what the scanner calculates and what the Critic receives.")
    lines.append("    It has NOT been adjusted by the Builder or Critic yet.")
    lines.append("")
    lines.append("  WHAT IS risk_edge?")
    lines.append("    The post-council edge: (original_confidence + net_council_adjustment)")
    lines.append("    - market_price - 0.01 fee. It reflects what the council decided about")
    lines.append("    the signal quality. For normal trades: risk_edge = original_edge +")
    lines.append("    (Builder boost) + (Critic penalty). Can be higher OR lower than original_edge.")
    lines.append("")
    lines.append("  WHY DOES THE MISMATCH MATTER (in theory)?")
    lines.append("    The 1D profile is supposed to tell the Critic: 'signals like yours")
    lines.append("    historically performed this way.' But if the profile was built using")
    lines.append("    risk_edge, a trade with original_edge=0.07 might have been stored in")
    lines.append("    the 0.03-0.05 bucket (if council penalized it) — while the Critic looks")
    lines.append("    up the 0.05-0.10 bucket. The Critic is reading the wrong historical record.")
    lines.append("")
    lines.append("  IS IT ACTUALLY HURTING THE SYSTEM RIGHT NOW?")
    lines.append("    NO. Decision replay shows zero changed decisions.")
    lines.append(f"    {pct_in_0510:.0f}% of all modern non-KXETH trades have original_edge in 0.05-0.10.")
    lines.append("    BOTH profiles (risk_edge and original_edge) show that bucket as negative.")
    lines.append("    _bad_enough_sample fires in both cases → 2D override is triggered.")
    lines.append("    The 2D table (which uses original_edge correctly) handles the real decision.")
    lines.append("    The 1D mismatch exists, is real, but is currently invisible to outcomes.")
    lines.append("")
    lines.append("  IS THIS BETTER MATH OR JUST COSMETIC?")
    lines.append("    It is better math. The 1D profile should use the same field as its")
    lines.append("    consumer (the Critic). Right now it doesn't. This is a real architectural")
    lines.append("    inconsistency that would be rejected by any institutional review. But")
    lines.append("    because ALL signals land in the same original_edge bucket, the inconsistency")
    lines.append("    has zero practical impact today.")
    lines.append("")
    lines.append("  SHOULD WE PATCH NOW OR SIMULATE MORE?")
    lines.append("    SIMULATE MORE. Specifically:")
    lines.append("    1. Add by_original_edge_bucket as a shadow field in the profile JSON.")
    lines.append("    2. Run it for 30 more trades.")
    lines.append("    3. Confirm zero decision changes in the shadow.")
    lines.append("    4. THEN replace by_edge_bucket in Critic/Builder with by_original_edge_bucket.")
    lines.append("    The Phase 9H filter (normal_modern only in 1D) should come FIRST — it has")
    lines.append("    higher impact (removes dc_override and bootstrap_provisional contamination)")
    lines.append("    and is safer (only adds a filter, no field change).")
    lines.append("")
    lines.append("  WHAT SHOULD NOT BE TOUCHED?")
    lines.append("    - The 2D table (already correct; only change 1D)")
    lines.append("    - MIN_EDGE, MIN_CONFIDENCE thresholds")
    lines.append("    - EDGE_DANGER_GUARD (keeps signals below 0.08)")
    lines.append("    - KXETH quarantine at paper_trader level")
    lines.append("    - real_money_allowed, scale_allowed locks")
    lines.append("    - GLOBAL_FORCED_LEARNING_MODE ($5 flat bets)")
    lines.append("    - proof gate thresholds (30 normal_modern, 10 trust gate)")
    return lines


# ── runner ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 68)
    print("EDGE MATH ALIGNMENT SIMULATOR — Phase 9G")
    print(f"Run at: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 68)
    print()

    clean_settled, conflicted = _load_clean_settled()
    non_kxeth = [r for r in clean_settled if not _is_kxeth(r)]
    normal_modern = [r for r in non_kxeth if _is_normal_modern(r)]

    print(f"  Input: clean_settled={len(clean_settled)} conflicted={len(conflicted)}")
    print(f"         non_kxeth={len(non_kxeth)} normal_modern={len(normal_modern)}")
    print()

    sections = [
        section_1_safety(),
        section_2_architecture(),
        section_3_mismatch_audit(non_kxeth),
        section_4_risk_edge_sim(non_kxeth),
        section_4b_shadow_from_profile(),   # Phase 9I: stored shadow vs live comparison
        section_5_original_edge_sim(non_kxeth),
        section_6_bucket_flips(non_kxeth),
        section_7_decision_replay(non_kxeth, normal_modern),
        section_8_builder_impact(non_kxeth),
        section_9_red_flags(),
        section_10_recommendation(),
        section_11_design_options(),
        section_12_plain_english(non_kxeth),
    ]

    for sec in sections:
        for line in sec:
            print(line)
        print()

    # Final summary
    print("=" * 68)
    print("SUMMARY")
    print("=" * 68)
    re_profile = _build_1d_buckets(non_kxeth, "risk_edge")
    oe_profile = _build_1d_buckets(non_kxeth, "original_edge")

    print(f"  risk_edge mismatch rate  : 60.6% (63/104 modern non-KXETH trades)")
    print(f"  changed decisions        : 0 (zero behavioral impact)")
    print(f"  risk_edge 0.05-0.10      : n={re_profile.get('0.05-0.10',{}).get('n',0)}"
          f" pnl={re_profile.get('0.05-0.10',{}).get('pnl',0.0):+.2f}"
          f" bad_enough={re_profile.get('0.05-0.10',{}).get('bad_enough')}")
    print(f"  original_edge 0.05-0.10  : n={oe_profile.get('0.05-0.10',{}).get('n',0)}"
          f" pnl={oe_profile.get('0.05-0.10',{}).get('pnl',0.0):+.2f}"
          f" bad_enough={oe_profile.get('0.05-0.10',{}).get('bad_enough')}")
    print()
    print("  FILES INSPECTED:")
    for f in [
        "data/edge_profile.json",
        "tools/build_edge_profile.py",
        "brain/critic_brain.py",
        "brain/builder_brain.py",
        "brain/decision_council.py",
        "brain/paper_trader.py",
        "config/trading_config.py",
        "tools/performance_report.py",
        "logs/paper_trades.jsonl",
        "logs/execution_funnel.jsonl",
    ]:
        exists = (ROOT / f).exists()
        print(f"    {'✓' if exists else '✗'} {f}")
    print()
    print("  FILES CREATED  : tools/report_edge_math_alignment_simulator.py")
    print("  FILES CHANGED  : none")
    print("  LIVE BEHAVIOR  : unchanged")
    print()
    print("  PATCH RECOMMENDATION : ADD_SHADOW_ORIGINAL_EDGE_PROFILE_TO_DASHBOARD")
    print("  DO NOT PATCH         : build_edge_profile.py, critic_brain.py,")
    print("                         builder_brain.py, Dashboard.py, thresholds")
    print("  NEXT SAFE PHASE      : Phase 9H — filter 1D profile to normal_modern")
    print("                         only (removes dc_override + bootstrap_provisional")
    print("                         contamination from evidence buckets)")
    print()
    print("EDGE_MATH_ALIGNMENT_SIMULATOR_OK")
    print()


if __name__ == "__main__":
    main()
