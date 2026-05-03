#!/usr/bin/env python3
"""
Phase 8P: Normal-modern proof flow audit.

Read-only. Traces the full path:
  scanner → signal → council → execution funnel →
  paper trade row → settlement → proof_class → normal_modern count

No trading, no log mutation, no config changes, no proof gate modifications.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"

SEP = "=" * 72
SUBSEP = "-" * 72


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _as_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _unique_trades(rows: List[dict]) -> List[dict]:
    """Deduplicate by (ticker, timestamp): last-write wins (SETTLED overwrites OPEN)."""
    seen: Dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("ticker"), r.get("timestamp"))
        seen[key] = r
    return list(seen.values())


def _proof_class(row: dict) -> str:
    bea = bool(row.get("bootstrap_era_council_allow") or row.get("bootstrap_era_allow"))
    bprov = bool(row.get("bootstrap_provisional"))
    dco = bool(row.get("data_collection_override"))
    has_modern = row.get("council_decision") is not None and row.get("risk_edge") is not None

    if not has_modern:
        return "LEGACY_EDGE_ONLY"
    if dco:
        return "DC_OVERRIDE_EXCLUDED"
    if bprov:
        return "BOOTSTRAP_PROVISIONAL_EXCLUDED"
    if bea:
        return "BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL"
    return "NORMAL_MODERN_CANDIDATE"


def _outcome_source(row: dict) -> str:
    status = str(row.get("status") or "")
    if status == "SETTLED":
        return "MARKET_RESOLVED"
    if status == "FORCED_CLOSE":
        result = str(row.get("result") or "").upper()
        if result == "TIME_EXIT":
            return "FORCED_TIME_EXIT"
        return "FORCED_VOID"
    if status == "OPEN":
        return "OPEN_NO_OUTCOME"
    return f"OTHER_{status}"


# ── section A: scanner volume ─────────────────────────────────────────────────

def section_a_scanner(funnel: List[dict]) -> None:
    print(SEP)
    print("A. SCANNER / FUNNEL VOLUME")
    print(SUBSEP)
    total = len(funnel)
    opened = sum(1 for r in funnel if r.get("final_status") == "TRADE_OPENED")
    print(f"  Total funnel rows (all time):     {total:>7}")
    print(f"  TRADE_OPENED:                     {opened:>7}")
    conv = opened / total * 100 if total else 0
    print(f"  Open rate:                        {conv:>6.2f}%")

    reason_counts = Counter(r.get("final_reason") for r in funnel)
    print()
    print("  Final-reason breakdown:")
    for reason, count in reason_counts.most_common(15):
        pct = count / total * 100 if total else 0
        print(f"    {str(reason):<40s}  {count:>6}  ({pct:>5.1f}%)")


# ── section B: blocker detail ─────────────────────────────────────────────────

def section_b_blockers(funnel: List[dict]) -> None:
    print()
    print(SEP)
    print("B. BLOCKER DEEP DIVE")
    print(SUBSEP)

    # BLOCKED_MAX_OPEN_TRADES detail
    max_open = [r for r in funnel if r.get("final_reason") == "BLOCKED_MAX_OPEN_TRADES"]
    print(f"  BLOCKED_MAX_OPEN_TRADES: {len(max_open)}")
    open_counts = Counter(r.get("open_count_before") for r in max_open)
    for k in sorted(open_counts, key=lambda x: (x is None, x)):
        print(f"    open_count_before={k}: {open_counts[k]}")

    # BLOCKED_MARKET_QUALITY detail
    mq = [r for r in funnel if r.get("final_reason") == "BLOCKED_MARKET_QUALITY"]
    spread_too_high = sum(1 for r in mq if (r.get("spread") or 0) > 0.05)
    vol_too_low = sum(1 for r in mq if (r.get("spread") or 0) <= 0.05)
    print()
    print(f"  BLOCKED_MARKET_QUALITY: {len(mq)}")
    print(f"    spread > 0.05:  {spread_too_high}")
    print(f"    volume too low: {vol_too_low}")

    # BLOCKED_MIN_EDGE detail
    min_edge = [r for r in funnel if r.get("final_reason") == "BLOCKED_MIN_EDGE"]
    edges_me = [_as_float(r.get("edge")) for r in min_edge if _as_float(r.get("edge")) is not None]
    print()
    print(f"  BLOCKED_MIN_EDGE: {len(min_edge)}")
    if edges_me:
        import statistics
        print(f"    edge range: [{min(edges_me):.4f}, {max(edges_me):.4f}]  median={statistics.median(edges_me):.4f}")

    # BLOCKED_EDGE_DANGER_GUARD detail
    danger = [r for r in funnel if r.get("final_reason") == "BLOCKED_EDGE_DANGER_GUARD"]
    print()
    print(f"  BLOCKED_EDGE_DANGER_GUARD: {len(danger)}")
    if danger:
        edges_d = [_as_float(r.get("edge")) for r in danger if _as_float(r.get("edge")) is not None]
        if edges_d:
            import statistics
            print(f"    edge range: [{min(edges_d):.4f}, {max(edges_d):.4f}]  median={statistics.median(edges_d):.4f}")


# ── section C: council decision breakdown ────────────────────────────────────

def section_c_council(funnel: List[dict]) -> None:
    print()
    print(SEP)
    print("C. COUNCIL DECISION BREAKDOWN")
    print(SUBSEP)

    cd_counts = Counter(r.get("council_decision") for r in funnel)
    total = len(funnel)
    for k, v in cd_counts.most_common():
        pct = v / total * 100 if total else 0
        print(f"  {str(k):<35s}  {v:>6}  ({pct:>5.1f}%)")

    print()
    allow_rows = [r for r in funnel if r.get("council_decision") == "ALLOW"]
    allow_opened = [r for r in allow_rows if r.get("paper_trade_opened")]
    allow_blocked = [r for r in allow_rows if not r.get("paper_trade_opened")]
    print(f"  ALLOW council decisions: {len(allow_rows)}")
    print(f"    → TRADE_OPENED:     {len(allow_opened)}")
    print(f"    → BLOCKED_RISK:     {len(allow_blocked)}")
    if allow_blocked:
        risk_reasons = Counter(r.get("final_reason") for r in allow_blocked)
        for k, v in risk_reasons.most_common(5):
            print(f"        final_reason={k}: {v}")


# ── section D: bootstrap catch-22 ────────────────────────────────────────────

def section_d_bootstrap_catchtwentytwo(funnel: List[dict]) -> None:
    print()
    print(SEP)
    print("D. THE BOOTSTRAP CATCH-22")
    print(SUBSEP)

    # Live values from config (import here to always reflect current state)
    from config.trading_config import (
        BOOTSTRAP_MIN_EDGE,
        BOOTSTRAP_CONFIDENCE_ADJUSTMENT as BOOTSTRAP_CONF_ADJ,
        EDGE_DANGER_HIGH_EDGE_MIN as EDGE_DANGER_MIN,
        MIN_EDGE,
    )

    print("  Config constants:")
    print(f"    BOOTSTRAP_MIN_EDGE             = {BOOTSTRAP_MIN_EDGE}")
    print(f"    BOOTSTRAP_CONFIDENCE_ADJUSTMENT = {BOOTSTRAP_CONF_ADJ}")
    print(f"    EDGE_DANGER_HIGH_EDGE_MIN       = {EDGE_DANGER_MIN}")
    print(f"    MIN_EDGE                        = {MIN_EDGE}")

    print()
    print("  The structural gap:")
    print(f"    Bootstrap path requires:  pre_council_edge >= {BOOTSTRAP_MIN_EDGE}")
    print(f"    Council subtracts:        {BOOTSTRAP_CONF_ADJ} from confidence")
    print(f"      → post_council_edge = pre_council_edge - {abs(BOOTSTRAP_CONF_ADJ)}")
    print(f"    Risk manager requires:    post_council_edge >= {MIN_EDGE}")
    print(f"      → pre_council_edge >= {MIN_EDGE} + {abs(BOOTSTRAP_CONF_ADJ)} = {MIN_EDGE + abs(BOOTSTRAP_CONF_ADJ)}")
    print(f"    Danger guard blocks:      pre_council_edge >= {EDGE_DANGER_MIN}")
    print()
    required = MIN_EDGE + abs(BOOTSTRAP_CONF_ADJ)
    viable_window = EDGE_DANGER_MIN - required
    print(f"  RESULT: pre_council_edge must be >= {required:.2f} to survive risk manager")
    print(f"          Danger guard blocks         >= {EDGE_DANGER_MIN}")
    if viable_window > 0:
        print(f"  VIABLE WINDOW: [{required:.2f}, {EDGE_DANGER_MIN}) = width {viable_window:.3f}")
        print(f"  → CATCH-22 RESOLVED (Phase 8P-C patch applied)")
    else:
        print(f"  CATCH-22 ACTIVE: required={required:.2f} >= blocks={EDGE_DANGER_MIN} → zero viable window")
        print()
        print("  The only escape: floating-point arithmetic")
        print(f"    Python: 0.690 - 0.600 - 0.01 = {0.690 - 0.600 - 0.01}")
        print(f"    This evaluates to 0.07999... which is < 0.08 (passes danger guard)")
        post_adj = 0.640 - 0.600 - 0.01
        print(f"    After council {BOOTSTRAP_CONF_ADJ}: 0.640 - 0.600 - 0.01 = {post_adj}")
        print(f"    This is {'PASSES' if post_adj >= MIN_EDGE else 'FAILS'} MIN_EDGE check")

    # Count signals in bootstrap window
    bootstrap_window = [
        r for r in funnel
        if _as_float(r.get("edge")) is not None
        and 0.05 <= _as_float(r.get("edge")) < 0.08
    ]
    caught = [r for r in bootstrap_window if r.get("final_reason") == "BLOCKED_RISK"]
    escaped = [r for r in bootstrap_window if r.get("final_status") == "TRADE_OPENED"]

    # Count ALLOW decisions for signals in the bootstrap window
    allow_in_window = [r for r in funnel if r.get("council_decision") == "ALLOW"]
    allow_blocked_all = [r for r in allow_in_window if not r.get("paper_trade_opened")]
    allow_opened_all = [r for r in allow_in_window if r.get("paper_trade_opened")]

    print()
    print("  Evidence from execution_funnel.jsonl:")
    print(f"    Signals in bootstrap window [0.05, 0.08): {len(bootstrap_window)}")
    print(f"      (many blocked earlier — market quality, max open, etc.)")
    print(f"    Council ALLOW decisions (all edges):       {len(allow_in_window)}")
    print(f"      → BLOCKED_RISK after ALLOW:              {len(allow_blocked_all)}")
    print(f"      → TRADE_OPENED after ALLOW:              {len(allow_opened_all)}")
    if allow_in_window:
        pct_blocked = len(allow_blocked_all) / len(allow_in_window) * 100
        print(f"    Survival rate for ALLOW signals: {100-pct_blocked:.1f}%  (failure rate {pct_blocked:.1f}%)")

    # True catch-22 escapes: ALLOW council + trade opened (bootstrap_era_allow path)
    allow_escaped = [r for r in escaped if r.get("council_decision") == "ALLOW"]
    provisional_opened = [r for r in escaped if r.get("council_decision") == "PROVISIONAL"]

    if allow_escaped:
        print()
        print(f"  Floating-point lucky trades (ALLOW path, {len(allow_escaped)}):")
        for r in allow_escaped:
            edge = _as_float(r.get("edge"))
            conf = _as_float(r.get("confidence"))
            ticker = r.get("ticker", "")[:35]
            ts = str(r.get("timestamp_utc", ""))[:10]
            print(f"    {ts}  edge={edge:.6f}  conf={conf:.3f}  ticker={ticker}")

    if provisional_opened:
        print()
        print(f"  Opened via PROVISIONAL path (bootstrap_provisional, NOT proof): {len(provisional_opened)}")

    # Show BLOCKED_RISK sample traces to confirm the pattern
    if caught:
        print()
        print(f"  Sample BLOCKED_RISK traces (3 of {len(caught)}):")
        for r in caught[:3]:
            edge = _as_float(r.get("edge"))
            trace = str(r.get("trace_excerpt") or "")
            post_edge_hint = ""
            for segment in trace.split("["):
                if "edge" in segment.lower() and ("below" in segment.lower() or "minimum" in segment.lower()):
                    post_edge_hint = segment[:80]
                    break
            print(f"    edge={edge:.4f}  trace hint: {post_edge_hint[:80]}")


# ── section E: execution funnel → paper trade mapping ────────────────────────

def section_e_funnel_to_trades(funnel: List[dict], trades: List[dict]) -> None:
    print()
    print(SEP)
    print("E. EXECUTION FUNNEL → PAPER TRADE MAPPING")
    print(SUBSEP)

    funnel_opened = [r for r in funnel if r.get("final_status") == "TRADE_OPENED"]
    unique_trades = _unique_trades(trades)

    print(f"  Funnel TRADE_OPENED entries: {len(funnel_opened)}")
    print(f"  Unique paper trades in log:  {len(unique_trades)}")
    print()

    # Classify by council decision
    cd_counts = Counter(r.get("council_decision") for r in funnel_opened)
    print("  Council decision at trade open:")
    for k, v in cd_counts.most_common():
        print(f"    {str(k):<20s}  {v}")

    print()
    # Proof class breakdown
    proof_counts = Counter(_proof_class(r) for r in unique_trades)
    print("  Proof class breakdown (unique trades):")
    for k, v in proof_counts.most_common():
        print(f"    {k:<45s}  {v}")

    print()
    print("  Breakdown by proof class × outcome:")
    by_class: Dict[str, List[dict]] = defaultdict(list)
    for r in unique_trades:
        by_class[_proof_class(r)].append(r)
    for cls in sorted(by_class):
        class_rows = by_class[cls]
        outcomes = Counter(_outcome_source(r) for r in class_rows)
        print(f"    {cls}")
        for out, cnt in outcomes.most_common():
            print(f"      {out:<25s}  {cnt}")


# ── section F: settlement status ─────────────────────────────────────────────

def section_f_settlement(trades: List[dict]) -> None:
    print()
    print(SEP)
    print("F. SETTLEMENT STATUS")
    print(SUBSEP)

    unique = _unique_trades(trades)
    status_counts = Counter(r.get("status") for r in unique)
    print("  Status distribution (unique trades):")
    for k, v in status_counts.most_common():
        print(f"    {str(k):<30s}  {v}")

    # OPEN ghost check
    open_rows = [r for r in unique if r.get("status") == "OPEN"]
    if open_rows:
        print()
        print(f"  WARNING: {len(open_rows)} OPEN records still in log:")
        for r in open_rows:
            print(f"    {r.get('ticker')}  entered={r.get('timestamp','')[:16]}")
    else:
        print()
        print("  No ghost OPEN records detected.")


# ── section G: metadata completeness ─────────────────────────────────────────

def section_g_metadata(trades: List[dict]) -> None:
    print()
    print(SEP)
    print("G. METADATA COMPLETENESS (modern rows)")
    print(SUBSEP)

    unique = _unique_trades(trades)
    modern_fields = [
        "council_decision",
        "bootstrap_provisional",
        "data_collection_override",
        "risk_edge",
        "bootstrap_era_council_allow",
        "model_probability",
    ]

    modern_rows = [r for r in unique if r.get("council_decision") is not None]
    print(f"  Rows with council_decision (modern era): {len(modern_rows)}")
    print(f"  Rows without council_decision (legacy):  {len(unique) - len(modern_rows)}")
    print()
    print("  Field presence in modern rows:")
    for field in modern_fields:
        present = sum(1 for r in modern_rows if r.get(field) is not None)
        pct = present / len(modern_rows) * 100 if modern_rows else 0
        print(f"    {field:<35s}  {present:>3}/{len(modern_rows)}  ({pct:>5.1f}%)")


# ── section H: normal_modern count and path ──────────────────────────────────

def section_h_normal_modern(trades: List[dict]) -> None:
    print()
    print(SEP)
    print("H. NORMAL_MODERN PROOF COUNT")
    print(SUBSEP)

    unique = _unique_trades(trades)
    proof_classes = Counter(_proof_class(r) for r in unique)

    all_normal = [
        r for r in unique
        if _proof_class(r) in ("BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL", "NORMAL_MODERN_CANDIDATE")
    ]
    # clean_truth_report.py evaluate_proof_gates() only passes SETTLED rows as
    # 'evaluated'.  TIME_EXIT rows go to a separate time_exits bucket and are
    # NOT counted in formal proof gate normal_modern_count.
    normal_settled = [r for r in all_normal if r.get("status") == "SETTLED"]
    normal_time_exit = [
        r for r in all_normal
        if r.get("status") == "FORCED_CLOSE" and str(r.get("result") or "").upper() == "TIME_EXIT"
    ]
    # formal_normal_modern mirrors what evaluate_proof_gates() counts
    formal_normal_modern = normal_settled

    print("  Proof class counts (all unique trades):")
    for k, v in proof_classes.most_common():
        symbol = "✓" if k in ("BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL", "NORMAL_MODERN_CANDIDATE") else "✗"
        print(f"    {symbol} {k:<45s}  {v}")

    print()
    print(f"  All normal_modern-eligible trades:  {len(all_normal)}")
    print(f"    SETTLED (counted in proof gate):   {len(normal_settled)}")
    print(f"    TIME_EXIT (not in evaluated set):  {len(normal_time_exit)}")
    print()
    print(f"  Formal normal_modern count (matches report_health.py): {len(formal_normal_modern)}")
    if normal_time_exit:
        print(f"  NOTE: {len(normal_time_exit)} TIME_EXIT row(s) have known outcomes but are")
        print(f"        excluded from evaluate_proof_gates() by design (outcome_known=clean_settled).")
        print(f"        This is a known classification gap — TIME_EXIT PnL is tracked but not")
        print(f"        counted toward proof. No action needed; just document.")

    print()
    print("  Proof gate progress (formal count):")
    nm = len(formal_normal_modern)
    print(f"    Trust gate:   {nm:>2} / 10  normal_modern  {'✓ MET' if nm >= 10 else '✗ NOT MET'}")
    print(f"    Scale gate:   {nm:>2} / 30  normal_modern  {'✓ MET' if nm >= 30 else '✗ NOT MET'}")

    if formal_normal_modern:
        wins = sum(1 for r in formal_normal_modern if (r.get("outcome") or "").upper() in ("WIN", "YES_WIN", "NO_WIN"))
        losses = sum(1 for r in formal_normal_modern if (r.get("outcome") or "").upper() in ("LOSS", "YES_LOSS", "NO_LOSS"))
        settled_with_outcome = wins + losses
        wr = wins / settled_with_outcome if settled_with_outcome else None
        pnls = [_as_float(r.get("pnl")) for r in formal_normal_modern if _as_float(r.get("pnl")) is not None]
        total_pnl = sum(pnls) if pnls else None
        print()
        print(f"  Normal_modern performance (settled={len(formal_normal_modern)}, with outcome={settled_with_outcome}):")
        if wr is not None:
            print(f"    Win rate:   {wr:.3f}  (wins={wins} losses={losses})")
        if total_pnl is not None:
            print(f"    Total PnL:  ${total_pnl:+.2f}")

    print()
    print("  All normal_modern-eligible trades:")
    for r in all_normal:
        cls = _proof_class(r)
        status = r.get("status", "")
        result = r.get("result", "")
        outcome = r.get("outcome", "")
        ticker = str(r.get("ticker", ""))[:35]
        ts = str(r.get("timestamp", ""))[:16]
        pnl = r.get("pnl")
        pnl_str = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "n/a"
        in_proof = "COUNTED" if r.get("status") == "SETTLED" else "TIME_EXIT_EXCLUDED"
        print(f"    [{in_proof}] {ts}  {ticker}  {status}/{result}/{outcome}  pnl={pnl_str}")


# ── section I: fix analysis ───────────────────────────────────────────────────

def section_i_fix_analysis() -> None:
    print()
    print(SEP)
    print("I. FIX ANALYSIS (SAFE OPTIONS ONLY)")
    print(SUBSEP)

    print("  Root cause: BOOTSTRAP_CONFIDENCE_ADJUSTMENT(-0.05) + MIN_EDGE(0.03)")
    print("  creates a requirement that pre_council_edge >= 0.08 to survive,")
    print("  but EDGE_DANGER_HIGH_EDGE_MIN=0.08 blocks exactly that range.")
    print()
    print("  Three candidate fixes:")
    print()

    print("  OPTION 1: Reduce BOOTSTRAP_CONFIDENCE_ADJUSTMENT magnitude")
    print("    Change: BOOTSTRAP_CONFIDENCE_ADJUSTMENT = -0.05  →  -0.02")
    print("    Effect: post_council_edge = pre_council_edge - 0.02")
    print("            For edge=0.05: post=0.03 → passes MIN_EDGE ✓")
    print("            For edge=0.07: post=0.05 → passes MIN_EDGE ✓")
    print("    Implication: Bootstrap skepticism reduced from -5% to -2%")
    print("    Safety: edge_profile still UNTRUSTED; council still flags it;")
    print("            $5 flat bets unchanged; proof classification unchanged")
    print("    Risk: Slightly less penalty for bootstrap signals — but the")
    print("          point of bootstrap IS to collect data, not prove edge")
    print("    File: config/trading_config.py  BOOTSTRAP_CONFIDENCE_ADJUSTMENT")
    print()

    print("  OPTION 2: Raise EDGE_DANGER_HIGH_EDGE_MIN slightly")
    print("    Change: EDGE_DANGER_HIGH_EDGE_MIN = 0.08  →  0.12")
    print("            (opens edges in [0.08, 0.12) to bootstrap trades)")
    print("    Effect: signals with edge in [0.08, 0.12) no longer danger-blocked;")
    print("            post_council_edge for edge=0.08 → 0.03 (passes MIN_EDGE)")
    print("    Risk: The danger guard exists because edge >= 0.08 is historically")
    print("          inverted in this system (Phase 6D finding). Raising it admits")
    print("          the very signals flagged as unreliable.")
    print("    VERDICT: NOT RECOMMENDED — weakens a safety guard for the wrong reason")
    print()

    print("  OPTION 3: Add risk override for bootstrap_era_allow signals")
    print("    Change: In paper_trader.py risk check, if post_council_edge >= MIN_EDGE/2")
    print("            and bootstrap_era_allow=True, allow through at $5 flat")
    print("    Risk: Complex conditional; hard to audit; mixes risk and council logic")
    print("    VERDICT: NOT RECOMMENDED — over-engineered for what is a config fix")
    print()

    print("  RECOMMENDATION:")
    print("    Option 1 is the minimal, auditable, safe fix.")
    print("    It changes ONE config constant, does not weaken the danger guard,")
    print("    does not touch proof thresholds, and does not bypass risk checks.")
    print("    Bootstrap trades will still be classified as bootstrap_era_allow,")
    print("    still cost $5 flat, and still require normal_modern=30 for proof.")
    print()
    print("    DO NOT IMPLEMENT without Samuel's explicit approval.")
    print("    This section is analysis only.")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    funnel = _load_jsonl(FUNNEL_LOG)
    trades = _load_jsonl(TRADES_LOG)

    print(SEP)
    print("NORMAL-MODERN PROOF FLOW AUDIT  —  Phase 8P")
    print(SEP)
    print("Read-only. No trading, no log changes, no proof gate modifications.")
    print(f"funnel rows:  {len(funnel)}")
    print(f"trade rows:   {len(trades)}")
    print()

    section_a_scanner(funnel)
    section_b_blockers(funnel)
    section_c_council(funnel)
    section_d_bootstrap_catchtwentytwo(funnel)
    section_e_funnel_to_trades(funnel, trades)
    section_f_settlement(trades)
    section_g_metadata(trades)
    section_h_normal_modern(trades)
    section_i_fix_analysis()

    print()
    print(SEP)
    print("AUDIT COMPLETE — RESULT: CATCH22_IDENTIFIED")
    print(SEP)


if __name__ == "__main__":
    main()
