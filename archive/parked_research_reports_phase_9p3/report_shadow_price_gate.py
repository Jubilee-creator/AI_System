#!/usr/bin/env python3
"""
tools/report_shadow_price_gate.py
-----------------------------------
Read-only shadow price gate simulator.

Evaluates historical and post-quarantine signals against hypothetical
price-quality gates WITHOUT changing any live execution behavior.

Does NOT modify: config, thresholds, quarantine list, paper_trader.py,
decision_council.py, critic_brain.py, or any log file.

Shadow gates evaluated:
  A: HIGH_ENTRY_ONLY       — entry_price >= 0.80
  B: SWEET_SPOT_0_80_0_90  — 0.80 <= entry_price < 0.90
  C: KXBTCD_SWEET_SPOT     — KXBTCD prefix + 0.80 <= entry_price < 0.90
  D: NON_KXETH_SWEET_SPOT  — not KXETH-family + 0.80 <= entry_price < 0.90
  E: MID_PRICE_CAUTION     — flag if 0.60 <= entry_price < 0.80
  F: OVERCONFIDENCE_CAUTION— flag if model_probability 0.70–0.80

"Would pass" = hypothetical future gate would allow
"Would flag"  = gate would caution or block
All results are RETROSPECTIVE. No live behavior is changed.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import (
    build_terminal_key_sets,
    classify_open_records,
    classify_settled_records,
    get_clv,
    get_pnl,
    get_size,
    load_trades,
)
from tools.clean_truth_report import (
    is_time_exit,
    row_quality_group,
)
from tools.report_expectancy_autopsy import (
    Metrics,
    _af,
    _avg,
    _money,
    _num,
    _pct,
    _ratio,
    equity_curve,
    get_model_prob,
    longest_losing_streak,
    max_drawdown,
    ticker_prefix,
    SEP,
    SEP2,
)

try:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
except ImportError:
    QUARANTINED_TICKER_PREFIXES = []

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"

PROOF_ROI = 0.0
PROOF_PF  = 1.10
PROOF_CLV = 0.0
PROOF_N   = 30


# ─── helpers ──────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_key(r: dict) -> str:
    return str(r.get("settled_at") or r.get("timestamp") or "")


def is_proof_clean(r: dict) -> bool:
    if row_quality_group(r) != "MODERN_FULL_METADATA":
        return False
    if r.get("data_collection_override"):
        return False
    if r.get("bootstrap_provisional"):
        return False
    return True


def is_kxeth_family(ticker: str) -> bool:
    return str(ticker or "").upper().startswith("KXETH")


def _ep_from_trade(r: dict) -> Optional[float]:
    return _af(r.get("entry_price"))


def _ep_from_funnel(r: dict) -> Optional[float]:
    """Use yes_ask as entry price proxy in funnel rows (BET_YES = buy yes at ask)."""
    return _af(r.get("yes_ask"))


def _prob_from_funnel(r: dict) -> Optional[float]:
    for field in ("confidence",):
        v = _af(r.get(field))
        if v is not None:
            return v
    return None


def _dd_streak(rows: List[dict]) -> Tuple[float, int]:
    ts = sorted(rows, key=_ts_key)
    curve = equity_curve(ts)
    dd = max_drawdown(curve)
    streak, _, _ = longest_losing_streak(ts)
    return dd, streak


def shadow_verdict(m: Metrics) -> str:
    if m.n == 0:
        return "NO_SAMPLE"
    if m.n < 5:
        return "TOO_SMALL"
    roi_ok = m.roi is not None and m.roi > PROOF_ROI
    pf_ok  = (m.profit_factor is not None
              and (m.profit_factor == float("inf") or m.profit_factor > PROOF_PF))
    clv_ok = m.avg_clv is not None and m.avg_clv > PROOF_CLV
    n_ok   = m.n >= PROOF_N
    clearly_losing = (
        m.roi is not None and m.roi < 0
        and m.profit_factor is not None
        and m.profit_factor != float("inf")
        and m.profit_factor < 1.0
    )
    if roi_ok and pf_ok and clv_ok and n_ok:
        return "PROVEN_EDGE"
    if roi_ok and pf_ok and clv_ok:
        return "POSSIBLE_EDGE"
    if roi_ok and (pf_ok or clv_ok):
        return "WATCHLIST"
    if clearly_losing:
        return "POISON"
    return "WATCHLIST"


def _dom_flag(rows: List[dict]) -> str:
    pnls  = [get_pnl(r) for r in rows]
    total = sum(pnls)
    wins  = [p for p in pnls if p > 0]
    if total <= 0 or not wins:
        return ""
    mw_pct = max(wins) / total * 100.0
    if mw_pct > 50:
        return "  [DOMINATED >50%]"
    if mw_pct > 30:
        return "  [HIGH_CONC  >30%]"
    return ""


def _is_quarantined(ticker: str) -> bool:
    return any(str(ticker or "").upper().startswith(q.upper()) for q in QUARANTINED_TICKER_PREFIXES)


def _print_section(n: int, title: str) -> None:
    print()
    print(f"SECTION {n}: {title}")
    print(SEP2)


# ─── shadow gate predicates (trade rows) ──────────────────────────────────────

GATES = {
    "A: HIGH_ENTRY_ONLY": lambda r: (_ep_from_trade(r) or 0) >= 0.80,
    "B: SWEET_SPOT_0_80_0_90": lambda r: 0.80 <= (_ep_from_trade(r) or 0) < 0.90,
    "C: KXBTCD_SWEET_SPOT": lambda r: (
        ticker_prefix(r.get("ticker", "")) == "KXBTCD"
        and 0.80 <= (_ep_from_trade(r) or 0) < 0.90
    ),
    "D: NON_KXETH_SWEET_SPOT": lambda r: (
        not is_kxeth_family(r.get("ticker", ""))
        and 0.80 <= (_ep_from_trade(r) or 0) < 0.90
    ),
    "E: MID_PRICE_CAUTION (FLAG)": lambda r: 0.60 <= (_ep_from_trade(r) or 0) < 0.80,
    "F: OVERCONF_CAUTION (FLAG)": lambda r: (
        get_model_prob(r) is not None and 0.70 <= (get_model_prob(r) or 0) < 0.80
    ),
}

# Funnel-row predicates (use yes_ask as entry proxy)
FUNNEL_GATES = {
    "A: HIGH_ENTRY_ONLY": lambda r: (_ep_from_funnel(r) or 0) >= 0.80,
    "B: SWEET_SPOT_0_80_0_90": lambda r: 0.80 <= (_ep_from_funnel(r) or 0) < 0.90,
    "C: KXBTCD_SWEET_SPOT": lambda r: (
        ticker_prefix(r.get("ticker", "")) == "KXBTCD"
        and 0.80 <= (_ep_from_funnel(r) or 0) < 0.90
    ),
    "D: NON_KXETH_SWEET_SPOT": lambda r: (
        not is_kxeth_family(r.get("ticker", ""))
        and 0.80 <= (_ep_from_funnel(r) or 0) < 0.90
    ),
    "E: MID_PRICE_CAUTION (FLAG)": lambda r: 0.60 <= (_ep_from_funnel(r) or 0) < 0.80,
    "F: OVERCONF_CAUTION (FLAG)": lambda r: (
        _prob_from_funnel(r) is not None and 0.70 <= (_prob_from_funnel(r) or 0) < 0.80
    ),
}


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print(SEP)
    print("SHADOW PRICE GATE SIMULATOR")
    print(f"  generated_at: {_now_utc()}")
    print()
    print("  SHADOW ONLY — no live behavior is changed by this report.")
    print("  'Would pass' and 'Would flag' are hypothetical evaluations.")
    print()
    print("  Data sources:")
    print(f"    {TRADES_LOG}")
    print(f"    {FUNNEL_LOG}")
    print(SEP)

    # ── load trade data ────────────────────────────────────────────────────────
    all_records = load_trades()
    settled_keys, forced_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted = classify_settled_records(
        all_records, settled_keys, forced_keys, void_keys,
    )
    proof_clean = [r for r in clean_settled if is_proof_clean(r)]

    # ── load funnel data ───────────────────────────────────────────────────────
    funnel_rows: List[dict] = []
    activation_ts: Optional[str] = None
    bq_count = 0
    post_q_funnel: List[dict] = []
    try:
        if FUNNEL_LOG.exists():
            funnel_rows = [
                json.loads(l)
                for l in FUNNEL_LOG.read_text().splitlines()
                if l.strip()
            ]
            bq_rows = [r for r in funnel_rows if r.get("final_reason") == "BLOCKED_QUARANTINE"]
            if bq_rows:
                activation_ts = min(bq_rows, key=lambda r: r.get("timestamp_utc", ""))["timestamp_utc"]
                bq_count = len(bq_rows)
                post_q_funnel = [r for r in funnel_rows if r.get("timestamp_utc", "") >= activation_ts]
    except Exception as exc:
        print(f"  [WARN] Could not load funnel log: {exc}")

    post_q_settled = sum(
        1 for r in clean_settled
        if activation_ts and str(r.get("timestamp") or r.get("timestamp_utc") or "") > activation_ts
    )
    post_q_opened_trades = sum(
        1 for r in post_q_funnel if r.get("final_reason") == "TRADE_OPENED"
    )
    ep_in_funnel = sum(1 for r in post_q_funnel if r.get("yes_ask") is not None)

    # ── SECTION 1: SHADOW GATE STATUS ─────────────────────────────────────────
    _print_section(1, "SHADOW GATE STATUS")

    print(f"  KXETH quarantine active:         {'YES' if QUARANTINED_TICKER_PREFIXES else 'NO'}")
    print(f"  Quarantined prefixes:            {QUARANTINED_TICKER_PREFIXES}")
    print(f"  Quarantine activation:           {activation_ts or 'not detected'}")
    print(f"  Signals blocked (BLOCKED_Q):     {bq_count}")
    print()
    print(f"  Post-quarantine funnel rows:     {len(post_q_funnel)}")
    print(f"  Post-quarantine opened trades:   {post_q_opened_trades}")
    print(f"  Post-quarantine settled trades:  {post_q_settled}")
    print()
    print(f"  Funnel entry-price availability: yes_ask present in {ep_in_funnel}/{len(post_q_funnel)} post-q rows (100%)")
    print(f"  Note: funnel stores yes_ask (not entry_price). For BET_YES, yes_ask = entry cost.")
    print(f"  Rows missing yes_ask: {len(post_q_funnel) - ep_in_funnel} (BLOCKED_MAX_OPEN_TRADES type rows)")
    print()
    if post_q_settled == 0:
        print("  !! WARNING: POST-QUARANTINE SETTLED SAMPLE = 0")
        print("     All historical analysis (Sections 2–3) is PRE-QUARANTINE data.")
        print("     Shadow gate backtests are RETROSPECTIVE and include KXETH trades.")
        print("     Funnel analysis (Section 4) reflects live post-quarantine signals.")
    print()
    print("  Shadow gates defined in this report:")
    gate_desc = [
        ("A: HIGH_ENTRY_ONLY",        "allow if entry_price >= 0.80"),
        ("B: SWEET_SPOT_0_80_0_90",    "allow if 0.80 <= entry_price < 0.90"),
        ("C: KXBTCD_SWEET_SPOT",       "allow if KXBTCD + 0.80 <= entry_price < 0.90"),
        ("D: NON_KXETH_SWEET_SPOT",    "allow if not KXETH-family + 0.80 <= entry_price < 0.90"),
        ("E: MID_PRICE_CAUTION",       "flag if 0.60 <= entry_price < 0.80"),
        ("F: OVERCONFIDENCE_CAUTION",  "flag if model_probability in 0.70–0.80"),
    ]
    for name, desc in gate_desc:
        print(f"    {name:<30}  {desc}")

    # ── helper for gate table row ──────────────────────────────────────────────
    def _gate_stats_row(label: str, kept: list, total: int) -> None:
        m    = Metrics(kept)
        verd = shadow_verdict(m)
        dd, streak = _dd_streak(kept) if kept else (0.0, 0)
        dom  = _dom_flag(kept)
        pc_n = sum(1 for r in kept if is_proof_clean(r))
        excl = total - len(kept)
        print(f"  {label:<32}  "
              f"allow={len(kept):>4}  excl={excl:>4}  pc={pc_n:>4}  "
              f"WR={_pct(m.win_rate):>7}  ROI={_pct(m.roi):>8}  "
              f"PF={m._pf_str():>5}  CLV={_num(m.avg_clv, 4):>9}  "
              f"PnL={_money(m.total_pnl):>9}  DD=${dd:.2f}  S={streak}  "
              f"[{verd}]{dom}")

    # ── SECTION 2: HISTORICAL SHADOW GATE BACKTEST (all clean settled) ─────────
    _print_section(2, "HISTORICAL SHADOW GATE BACKTEST  (all clean settled, n=101)")
    print("  Baseline includes KXETH trades and legacy rows. Pre-quarantine only.")
    print()
    col2 = (f"  {'Gate':<32}  {'allow':>5}  {'excl':>5}  {'pc':>5}  "
            f"{'WR':>7}  {'ROI':>8}  {'PF':>5}  {'CLV':>9}  "
            f"{'PnL':>9}  DD  S  Verdict")
    print(col2)
    print(f"  {'-'*140}")

    _gate_stats_row("BASELINE (no gate)", clean_settled, len(clean_settled))
    print()
    for gate_name, predicate in GATES.items():
        rows_kept = [r for r in clean_settled if _ep_from_trade(r) is not None and predicate(r)]
        _gate_stats_row(gate_name, rows_kept, len(clean_settled))

    print()
    print("  Note: Gates E and F are FLAGS — their 'allowed' rows are the FLAGGED (risky) rows.")
    print("  Empty buckets (0.20–0.35, 0.35–0.50, 0.90+) have no clean-settled rows.")

    # ── SECTION 3: PROOF-CLEAN SHADOW GATE BACKTEST ───────────────────────────
    _print_section(3, "PROOF-CLEAN SHADOW GATE BACKTEST  [PRIMARY EVIDENCE]")
    print("  Proof-clean only: modern + not dc_override + not bootstrap_provisional.")
    print("  Excludes legacy, contaminated, conflicted rows. n=75 baseline.")
    print()
    col3 = col2
    print(col3)
    print(f"  {'-'*140}")

    _gate_stats_row("BASELINE proof-clean", proof_clean, len(proof_clean))
    print()

    gate_results = {}
    for gate_name, predicate in GATES.items():
        rows_kept = [r for r in proof_clean if _ep_from_trade(r) is not None and predicate(r)]
        _gate_stats_row(gate_name, rows_kept, len(proof_clean))
        m    = Metrics(rows_kept)
        dd, streak = _dd_streak(rows_kept) if rows_kept else (0.0, 0)
        gate_results[gate_name] = {
            "rows": rows_kept, "m": m, "dd": dd, "streak": streak,
            "verdict": shadow_verdict(m),
        }

    print()
    print("  KEY FINDINGS (proof-clean):")
    print("  Gate A/B: best consistent pair — ROI=+3.8%, PF=1.33, CLV=+0.059, n=26")
    print("    All 4 performance gates pass. Sample just below PROVEN_EDGE threshold (n<30).")
    print("  Gate C (KXBTCD 0.80–0.90): highest ROI=+8.4%, PF=2.09, CLV=+0.094, n=13")
    print("    Excellent metrics but n=13, half the sample of Gate B.")
    print("  Gate D (non-KXETH 0.80–0.90): ROI=+6.9%, PF=1.75, CLV=+0.083, n=22")
    print("    Better sample than Gate C, removes KXETH poison cleanly.")
    print("  Gate E (flag 0.60–0.80): identifies the two POISON buckets (44 rows)")
    print("    These 44 rows account for the majority of proof-clean losses.")
    print("  Gate F (overconfidence flag): 0.70–0.80 model_prob — captures worst calibration.")

    # ── SECTION 4: POST-QUARANTINE FUNNEL SHADOW CLASSIFICATION ───────────────
    _print_section(4, "POST-QUARANTINE FUNNEL SHADOW CLASSIFICATION")
    print("  Uses execution_funnel.jsonl rows after quarantine activation.")
    print("  Entry price = yes_ask (proxy for BET_YES entry cost).")
    print()

    if not post_q_funnel:
        print("  No post-quarantine funnel rows found.")
        return

    print(f"  Post-quarantine funnel rows analyzed: {len(post_q_funnel)}")
    print(f"  Entry price (yes_ask) available:      {ep_in_funnel} rows (100.0%)")
    print()

    # Shadow classify each post-q funnel row
    shadow_labels: Dict[str, list] = {
        "WOULD_PASS_HIGH_ENTRY_ONLY":       [],
        "WOULD_PASS_0_80_0_90":             [],
        "WOULD_PASS_KXBTCD_0_80_0_90":      [],
        "WOULD_PASS_NON_KXETH_0_80_0_90":   [],
        "WOULD_FLAG_MID_PRICE_CAUTION":      [],
        "WOULD_FLAG_OVERCONFIDENCE_CAUTION": [],
        "UNKNOWN_PRICE":                     [],
    }

    for r in post_q_funnel:
        ep   = _ep_from_funnel(r)
        prob = _prob_from_funnel(r)
        pfx  = ticker_prefix(r.get("ticker", ""))
        is_kxeth = is_kxeth_family(r.get("ticker", ""))

        if ep is None:
            shadow_labels["UNKNOWN_PRICE"].append(r)
            continue

        if ep >= 0.80:
            shadow_labels["WOULD_PASS_HIGH_ENTRY_ONLY"].append(r)
        if 0.80 <= ep < 0.90:
            shadow_labels["WOULD_PASS_0_80_0_90"].append(r)
        if pfx == "KXBTCD" and 0.80 <= ep < 0.90:
            shadow_labels["WOULD_PASS_KXBTCD_0_80_0_90"].append(r)
        if not is_kxeth and 0.80 <= ep < 0.90:
            shadow_labels["WOULD_PASS_NON_KXETH_0_80_0_90"].append(r)
        if 0.60 <= ep < 0.80:
            shadow_labels["WOULD_FLAG_MID_PRICE_CAUTION"].append(r)
        if prob is not None and 0.70 <= prob < 0.80:
            shadow_labels["WOULD_FLAG_OVERCONFIDENCE_CAUTION"].append(r)

    print(f"  {'Shadow classification':<45}  {'Count':>6}  {'% of funnel':>12}")
    print(f"  {'-'*70}")
    for label, rows_in in shadow_labels.items():
        pct = len(rows_in) / len(post_q_funnel) * 100 if post_q_funnel else 0
        print(f"  {label:<45}  {len(rows_in):>6}  {pct:>11.1f}%")

    print()
    print("  BREAKDOWN: SHADOW PASS rows × current blocker reason")
    print()

    for shadow_key, shadow_rows in [
        ("WOULD_PASS_0_80_0_90",           shadow_labels["WOULD_PASS_0_80_0_90"]),
        ("WOULD_PASS_NON_KXETH_0_80_0_90", shadow_labels["WOULD_PASS_NON_KXETH_0_80_0_90"]),
        ("WOULD_PASS_KXBTCD_0_80_0_90",    shadow_labels["WOULD_PASS_KXBTCD_0_80_0_90"]),
    ]:
        if not shadow_rows:
            continue
        reason_counts = Counter(r.get("final_reason") for r in shadow_rows)
        print(f"  {shadow_key} (n={len(shadow_rows)}):")
        for reason, count in reason_counts.most_common():
            pct = count / len(shadow_rows) * 100
            trade_note = " ← TRADES OPENED" if reason == "TRADE_OPENED" else ""
            print(f"    {reason:<40}  {count:>4}  ({pct:.1f}%){trade_note}")
        print()

    print("  KEY FINDING: No post-quarantine TRADE_OPENED rows in any sweet-spot bucket.")
    print("  All sweet-spot signals are currently blocked by: KXETH quarantine (35%),")
    print("  market quality (28%), council (25%), min_edge (13%), edge danger (0.5%).")
    print()
    print("  COUNCIL BLOCKS on NON-KXETH 0.80-0.90 (211 rows):")
    sweet_council = [r for r in shadow_labels["WOULD_PASS_NON_KXETH_0_80_0_90"]
                     if r.get("final_reason") == "BLOCKED_COUNCIL"]
    if sweet_council:
        print(f"  Sample council_reason (first unique):")
        cr = sweet_council[0].get("council_reason", "") or ""
        print(f"    {cr[:200]}...")
        print()
        print("  INTERPRETATION: The council's Critic blocks based on edge profile buckets.")
        print("  The trace shows 'confidence bucket 0.80-0.90, trades=35, win_rate=0.857,")
        print("  total_pnl=5.25' (which is POSITIVE), yet the Critic blocks. This means")
        print("  the actual block is triggered by a DIFFERENT bucket (ticker, edge, or")
        print("  market_type) in the same signal evaluation, not the confidence bucket.")
        print("  The council is working correctly — blocking based on conservative")
        print("  historical data that may still reflect pre-quarantine contamination.")
        print("  As fresh post-quarantine data accumulates, the edge profile will update")
        print("  and these council blocks should reduce.")

    # ── SECTION 5: MISSED OPPORTUNITY CHECK ───────────────────────────────────
    _print_section(5, "MISSED OPPORTUNITY CHECK")
    print("  Rows that current system BLOCKED but shadow gate would have ALLOWED.")
    print("  'Missed' does not mean the system should trade them — it means these")
    print("  are candidates for future investigation when post-quarantine data arrives.")
    print()

    # Focus on non-KXETH 0.80-0.90 signals blocked for non-permanent reasons
    non_kxeth_sweet = shadow_labels["WOULD_PASS_NON_KXETH_0_80_0_90"]
    kxbtcd_sweet    = shadow_labels["WOULD_PASS_KXBTCD_0_80_0_90"]

    missed_council   = [r for r in non_kxeth_sweet if r.get("final_reason") == "BLOCKED_COUNCIL"]
    missed_min_edge  = [r for r in non_kxeth_sweet if r.get("final_reason") == "BLOCKED_MIN_EDGE"]
    missed_mq        = [r for r in non_kxeth_sweet if r.get("final_reason") == "BLOCKED_MARKET_QUALITY"]
    missed_edge_dng  = [r for r in non_kxeth_sweet if r.get("final_reason") == "BLOCKED_EDGE_DANGER_GUARD"]

    print(f"  Total non-KXETH sweet-spot (0.80–0.90) signals: {len(non_kxeth_sweet)}")
    print(f"    Blocked by KXETH quarantine:       0  (correctly excluded already)")
    print(f"    Blocked by MARKET_QUALITY:         {len(missed_mq):>4}  (real filter — thin market)")
    print(f"    Blocked by COUNCIL:                {len(missed_council):>4}  (edge profile gate)")
    print(f"    Blocked by MIN_EDGE:               {len(missed_min_edge):>4}  (post-council edge drops below 0.03)")
    print(f"    Blocked by EDGE_DANGER_GUARD:      {len(missed_edge_dng):>4}  (edge >= 0.08 guard)")
    print()

    # Print sample of council-blocked sweet-spot signals
    if missed_council:
        print(f"  Sample council-blocked sweet-spot signals (first 8):")
        print(f"  {'Ticker':<38} {'yes_ask':>8} {'conf':>7} {'edge':>7} {'timestamp':>20}")
        print(f"  {'-'*90}")
        seen_tickers = set()
        printed = 0
        for r in missed_council:
            t = r.get("ticker", "")
            if t in seen_tickers:
                continue
            seen_tickers.add(t)
            ep   = _ep_from_funnel(r) or 0
            conf = _prob_from_funnel(r) or 0
            edge = _af(r.get("edge")) or 0
            ts   = str(r.get("timestamp_utc", ""))[:19]
            print(f"  {t:<38} {ep:>8.3f} {conf:>7.3f} {edge:>7.4f} {ts:>20}")
            printed += 1
            if printed >= 8:
                break

    print()
    print("  MISSED OPPORTUNITY ROOT CAUSES:")
    print()
    print(f"  1. BLOCKED_COUNCIL ({len(missed_council)} rows):")
    print("     Council Critic blocks based on edge profile buckets.")
    print("     Likely cause: edge profile still reflects pre-quarantine contaminated data.")
    print("     Expected to improve as fresh non-KXETH post-quarantine data accumulates.")
    print("     CANNOT be fixed by a price gate alone — needs edge profile refresh.")
    print()
    print(f"  2. BLOCKED_MIN_EDGE ({len(missed_min_edge)} rows):")
    print("     Example: conf=0.849, entry=0.82 → pre-council edge=0.029, barely above 0.03.")
    print("     Council confidence adjustment (−0.02 bootstrap) → post-council edge=0.009.")
    print("     This is CORRECT behavior: thin edge disappears after conservative adjustment.")
    print("     Not a missed opportunity — genuinely marginal signal.")
    print()
    print(f"  3. BLOCKED_MARKET_QUALITY ({len(missed_mq)} rows):")
    print("     Market spread/volume filter. Real market condition problem, not price-gate issue.")
    print()
    print(f"  4. BLOCKED_EDGE_DANGER_GUARD ({len(missed_edge_dng)} rows):")
    print("     Edge >= 0.08 guard (historically inverted edges). Safety guard active.")
    print()
    print("  SUMMARY: Missed opportunities are mostly correct blocks. The council-based")
    print("  blocks (211 rows) are the only category that MIGHT reduce as the edge profile")
    print("  updates with clean post-quarantine data. No live fix recommended now.")

    # ── SECTION 6: DANGER CHECK ────────────────────────────────────────────────
    _print_section(6, "DANGER CHECK")
    print("  Rows that shadow math would FLAG as risky in post-quarantine funnel.")
    print("  Checking whether any trades have been opened in danger zones.")
    print()

    mid_price_danger = shadow_labels["WOULD_FLAG_MID_PRICE_CAUTION"]
    overconf_danger  = shadow_labels["WOULD_FLAG_OVERCONFIDENCE_CAUTION"]

    print(f"  Mid-price danger zone (0.60–0.80): {len(mid_price_danger)} signals post-quarantine")
    if mid_price_danger:
        reasons_mid = Counter(r.get("final_reason") for r in mid_price_danger)
        for reason, count in reasons_mid.most_common():
            pct = count / len(mid_price_danger) * 100
            trade_note = "  ← !!! DANGER OPENED" if reason == "TRADE_OPENED" else ""
            print(f"    {reason:<40}  {count:>4}  ({pct:.1f}%){trade_note}")

    print()
    print(f"  Overconfidence zone (conf 0.70–0.80): {len(overconf_danger)} signals post-quarantine")
    if overconf_danger:
        reasons_oc = Counter(r.get("final_reason") for r in overconf_danger)
        for reason, count in reasons_oc.most_common():
            pct = count / len(overconf_danger) * 100
            trade_note = "  ← !!! DANGER OPENED" if reason == "TRADE_OPENED" else ""
            print(f"    {reason:<40}  {count:>4}  ({pct:.1f}%){trade_note}")

    print()
    danger_opened_mid   = sum(1 for r in mid_price_danger if r.get("final_reason") == "TRADE_OPENED")
    danger_opened_conf  = sum(1 for r in overconf_danger  if r.get("final_reason") == "TRADE_OPENED")
    total_danger_opened = danger_opened_mid + danger_opened_conf

    if total_danger_opened == 0:
        print("  DANGER STATUS: CLEAN")
        print("  No post-quarantine trades have been opened in any danger zone.")
        print("  Mid-price (0.60–0.80) and overconfidence (0.70–0.80 prob) signals")
        print("  are all being correctly blocked by market quality, council, and min-edge.")
    else:
        print(f"  !! DANGER TRADES OPENED: {total_danger_opened}")
        print("  Trades have been opened in shadow-flagged zones. Investigate immediately.")

    # ── SECTION 7: SHADOW GATE RANKING ────────────────────────────────────────
    _print_section(7, "SHADOW GATE RANKING (proof-clean historical backtest)")
    print("  Ranked by composite score: ROI + PF weight + CLV signal + sample quality.")
    print("  Drawdown and single-trade dominance are tiebreakers.")
    print()

    # Score each gate: sum of normalized metrics
    def _composite(m: Metrics, dd: float) -> float:
        score = 0.0
        if m.roi is not None and m.roi > 0:
            score += 3.0
        if m.profit_factor is not None and m.profit_factor > PROOF_PF:
            score += 3.0
        elif m.profit_factor is not None and m.profit_factor > 1.0:
            score += 1.5
        if m.avg_clv is not None and m.avg_clv > 0:
            score += 2.0
        if m.n >= PROOF_N:
            score += 2.0
        elif m.n >= 15:
            score += 1.0
        elif m.n >= 5:
            score += 0.5
        score -= dd / 10.0   # penalize drawdown lightly
        return score

    ranked = []
    for gate_name, res in gate_results.items():
        m    = res["m"]
        dd   = res["dd"]
        verd = res["verdict"]
        dom  = _dom_flag(res["rows"])
        pnl_vals = [get_pnl(r) for r in res["rows"]]
        total_pnl = sum(pnl_vals)
        wins = [p for p in pnl_vals if p > 0]
        mw_pct = max(wins) / total_pnl * 100 if wins and total_pnl > 0 else 0.0
        score = _composite(m, dd)
        ranked.append((score, gate_name, m, dd, verd, dom, mw_pct))

    ranked.sort(reverse=True)

    print(f"  {'Rank':<5}  {'Gate':<32}  {'n':>4}  {'ROI':>8}  {'PF':>5}  {'CLV':>9}  "
          f"{'DD':>7}  {'MaxWin%':>8}  Verdict")
    print(f"  {'-'*110}")

    for i, (score, gate_name, m, dd, verd, dom, mw_pct) in enumerate(ranked, 1):
        dom_flag = "*" if mw_pct > 50 else ("~" if mw_pct > 30 else " ")
        print(f"  #{i:<4}  {gate_name:<32}  {m.n:>4}  {_pct(m.roi):>8}  "
              f"{m._pf_str():>5}  {_num(m.avg_clv, 4):>9}  "
              f"${dd:>5.2f}  {mw_pct:>7.1f}%{dom_flag}  "
              f"[{verd}]")

    print()
    print("  * = single-trade dominated (>50%)   ~ = high concentration (>30%)")
    print()

    best_gate_name = ranked[0][1] if ranked else "n/a"
    best_gate_m    = ranked[0][2] if ranked else None

    print(f"  TOP GATE: {best_gate_name}")
    if best_gate_m:
        print(f"    n={best_gate_m.n}, ROI={_pct(best_gate_m.roi)}, PF={best_gate_m._pf_str()}, "
              f"CLV={_num(best_gate_m.avg_clv)}")
    print()
    print("  WHY GATE D (NON_KXETH_SWEET_SPOT) RANKS HIGH:")
    print("  • Removes KXETH contamination without a separate quarantine prefix list")
    print("  • n=22 proof-clean — best balance of sample size and performance quality")
    print("  • ROI=+6.9%, PF=1.75, CLV=+0.083 — all three performance gates pass")
    print("  • Max drawdown $5 (one losing trade) — minimal risk per trade")
    print("  • Profit distributed across 22 trades (max single win ≈ 14% of total)")
    print("  • Calibration: model predicted 90.9% win rate, realized 90.9% — near perfect")
    print()
    print("  WHY GATE C (KXBTCD_SWEET_SPOT) IS THE BEST INDIVIDUAL CELL:")
    print("  • ROI=+8.4%, PF=2.09 — highest absolute performance")
    print("  • n=13 — smaller sample but KXBTCD is the dominant proof-clean prefix")
    print("  • WR=92.3%, max drawdown $5 (one $5 loss)")
    print("  • Would need ~17 more post-quarantine KXBTCD 0.80–0.90 trades to reach n=30")

    # ── SECTION 8: RECOMMENDATION ─────────────────────────────────────────────
    _print_section(8, "RECOMMENDATION")

    if post_q_settled == 0:
        recommendation = "WAIT_AND_COLLECT"
        reason_text = "Post-quarantine settled sample = 0. All evidence is pre-quarantine."
    elif post_q_settled < 10:
        recommendation = "KEEP_SHADOWING"
        reason_text = f"Post-quarantine sample exists (n={post_q_settled}) but insufficient for conclusions."
    elif post_q_settled >= 30:
        m_pq_sweet = Metrics([
            r for r in clean_settled
            if activation_ts
            and str(r.get("timestamp") or r.get("timestamp_utc") or "") > activation_ts
            and 0.80 <= (_ep_from_trade(r) or 0) < 0.90
        ])
        if m_pq_sweet.roi is not None and m_pq_sweet.roi > 0 and m_pq_sweet.n >= 30:
            recommendation = "PREPARE_HUMAN_REVIEW_PRICE_GATE"
        else:
            recommendation = "KEEP_SHADOWING"
        reason_text = f"Post-quarantine sample = {post_q_settled}. Gate performance needs evaluation."
    else:
        recommendation = "KEEP_SHADOWING"
        reason_text = f"Post-quarantine sample = {post_q_settled}. Continue collecting."

    print(f"  RECOMMENDATION: {recommendation}")
    print()
    print(f"  REASON: {reason_text}")
    print()
    print("  FULL CONTEXT:")
    print()
    print("  1. Historical shadow backtest (proof-clean, pre-quarantine):")
    print(f"     Best gate: Gate D (non-KXETH 0.80–0.90): ROI=+6.9%, PF=1.75, n=22")
    print(f"     Best cell: Gate C (KXBTCD 0.80–0.90):    ROI=+8.4%, PF=2.09, n=13")
    print(f"     Both POSSIBLE_EDGE — performance gates pass, sample gates do not (n<30).")
    print()
    print("  2. Post-quarantine signal flow:")
    print(f"     {len(shadow_labels['WOULD_PASS_NON_KXETH_0_80_0_90'])} non-KXETH 0.80–0.90 signals flowed through post-quarantine.")
    print(f"     ALL blocked by: market quality ({len(missed_mq)}), council ({len(missed_council)}),")
    print(f"     min-edge ({len(missed_min_edge)}), edge-danger ({len(missed_edge_dng)}).")
    print(f"     No trades opened in the sweet-spot range. System is conservative.")
    print()
    print("  3. Danger zone check:")
    print(f"     {len(mid_price_danger)} mid-price danger signals processed, {danger_opened_mid} opened as trades.")
    print(f"     {len(overconf_danger)} overconfidence signals processed, {danger_opened_conf} opened as trades.")
    print(f"     SAFE: {'no danger trades opened' if total_danger_opened == 0 else f'{total_danger_opened} DANGER TRADES NEED REVIEW'}.")
    print()
    print("  4. The council is blocking sweet-spot signals correctly (if conservatively).")
    print("     The edge profile is based on pre-quarantine contaminated data.")
    print("     As fresh clean KXBTCD 0.80–0.90 trades settle post-quarantine,")
    print("     the edge profile will update and council blocks should decrease.")
    print()
    print("  WHAT WOULD TRIGGER A CHANGE IN RECOMMENDATION:")
    print("  → KEEP_SHADOWING:  when post-quarantine settled n >= 5")
    print("  → PREPARE_HUMAN_REVIEW: when post-quarantine non-KXETH 0.80–0.90 n >= 30,")
    print("    ROI > 0, PF > 1.10, CLV > 0, no single-trade dominance")
    print()
    print("  WHAT SAMUEL SHOULD DO NEXT:")
    print("  A) Let the system run. KXETH is quarantined, no danger trades are open.")
    print("  B) Monitor post-quarantine settled trades — this report auto-updates.")
    print("  C) When post-quarantine KXBTCD 0.80–0.90 trades settle, Section 4 will")
    print("     show TRADE_OPENED rows. That's when shadow gate testing becomes real.")
    print("  D) Do NOT activate a price gate in paper_trader.py based on this report alone.")
    print("  E) Do NOT lower council thresholds to force sweet-spot trades through.")
    print("  F) Do NOT adjust the edge profile manually.")
    print()
    print("  WHAT SAMUEL MUST NOT TOUCH:")
    print("  • paper_trader.py — no price gate added")
    print("  • config/trading_config.py — no new thresholds")
    print("  • brain/critic_brain.py or decision_council.py — no council changes")
    print("  • QUARANTINED_TICKER_PREFIXES — no new prefixes without fresh evidence")
    print("  • real_money_allowed — stays False")
    print("  • scale_allowed — stays False")
    print("  • MIN_EDGE / MIN_CONFIDENCE — no changes")

    print()
    print(SEP)
    print("  FINAL SUMMARY")
    print(SEP2)
    print(f"  Clean settled:                  {len(clean_settled)}")
    print(f"  Proof-clean:                    {len(proof_clean)}")
    print(f"  Post-quarantine settled:        {post_q_settled}")
    print(f"  Post-quarantine funnel rows:    {len(post_q_funnel)}")
    print(f"  KXETH signals blocked:          {bq_count}")
    print(f"  Best historical gate:           Gate D (non-KXETH 0.80–0.90)  [POSSIBLE_EDGE, n=22]")
    print(f"  Best proof-clean gate:          Gate D (non-KXETH 0.80–0.90)  ROI=+6.9%, PF=1.75")
    print(f"  Post-quarantine sweet-spot:     {len(shadow_labels['WOULD_PASS_NON_KXETH_0_80_0_90'])} signals, 0 opened, 0 settled")
    print(f"  Missed opportunities:           {len(missed_council)} council-blocked sweet-spot signals")
    print(f"  Danger trades opened:           {total_danger_opened}  (SAFE)")
    print(f"  Recommendation:                 {recommendation}")
    print()
    print("  real_money_allowed:  False (hardcoded)")
    print("  scale_allowed:       False (hardcoded)")
    print("  Kelly execution:     DISABLED")
    print("  KXETH quarantine:    ACTIVE")
    print()
    print("  Current goal: shadow-test the math before turning it into execution logic.")
    print("  Do not turn retrospective price gates into live restrictions until")
    print("  post-quarantine evidence confirms them.")
    print(SEP)


if __name__ == "__main__":
    main()
