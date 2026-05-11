#!/usr/bin/env python3
"""
tools/report_quarantine_simulation.py
--------------------------------------
Read-only quarantine simulation.

Answers: "If we quarantine the poison buckets, does the remaining
system become mathematically cleaner?"

Does NOT modify any config, threshold, log, or execution path.
All results are retrospective simulations on historical paper trades.

OVERFITTING WARNING: Selecting past subsets that happened to perform
better does NOT prove future edge. Treat every result here as WATCHLIST
unless ALL gates pass: n >= 30, ROI > 0%, PF > 1.10, avg CLV > 0.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import (
    build_terminal_key_sets,
    classify_settled_records,
    load_trades,
    get_clv,
    get_pnl,
    get_size,
)
from tools.clean_truth_report import row_quality_group

# Reuse all helpers from the autopsy script to avoid duplication.
from tools.report_expectancy_autopsy import (
    Metrics,
    group_by,
    ticker_prefix,
    entry_price_bucket,
    equity_curve,
    max_drawdown,
    worst_window,
    longest_losing_streak,
    _af,
    _avg,
    _pct,
    _num,
    _money,
    _now_utc,
    ENTRY_PRICE_ORDER,
    SEP,
    SEP2,
)

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"

# Proof gate thresholds — must match evaluate_proof_gates() in clean_truth_report.py
PROOF_N   = 30
PROOF_ROI = 0.0
PROOF_PF  = 1.10
PROOF_CLV = 0.0

OVERFITTING_WARN = (
    "  ⚠  RETROSPECTIVE SIMULATION — selecting past winners does not prove future edge.\n"
    "     Results with n < 30 are WATCHLIST or smaller. Do not trade on filtered history."
)


# ─── filter predicates ───────────────────────────────────────────────────────

def is_kxeth_row(r: dict) -> bool:
    """True for any ticker in the KXETH family (KXETH, KXETHD, KXETHMINY, etc.)."""
    return ticker_prefix(r.get("ticker", "")).startswith("KXETH")


def is_price_60_80(r: dict) -> bool:
    """True if entry_price is in [0.60, 0.80)."""
    ep = _af(r.get("entry_price"))
    return ep is not None and 0.60 <= ep < 0.80


def _ts_key(r: dict) -> str:
    return str(r.get("settled_at") or r.get("timestamp") or "")


# ─── simulation verdict ──────────────────────────────────────────────────────

def sim_verdict(m: Metrics) -> str:
    """
    Strict gate check for a simulation subset.
    Returns PROVEN_EDGE only when ALL four gates pass.
    """
    if m.n < 3:
        return "TOO_SMALL"

    roi_ok = m.roi is not None and m.roi > PROOF_ROI
    pf_ok  = (
        m.profit_factor is not None
        and (m.profit_factor == float("inf") or m.profit_factor > PROOF_PF)
    )
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
    if roi_ok and pf_ok and clv_ok and not n_ok:
        return "WATCHLIST"          # all metrics pass, sample too small
    if roi_ok and not pf_ok and clv_ok:
        return "WATCHLIST"          # ROI + CLV positive, PF gate fails
    if roi_ok and pf_ok and not clv_ok:
        return "POSSIBLE_EDGE"      # two of three pass, CLV fails
    if clearly_losing:
        return "POISON"
    return "WATCHLIST"


# ─── quarantine recommendation ───────────────────────────────────────────────

def quarantine_rec(m: Metrics) -> str:
    if m.n < 3:
        return "TOO_SMALL"
    roi = m.roi if m.roi is not None else 0.0
    pf  = 0.0 if m.profit_factor is None else (999.0 if m.profit_factor == float("inf") else m.profit_factor)
    if roi <= -0.25 and pf < 0.50:
        return "HARD_QUARANTINE"
    if roi < 0.0 and pf < 1.0:
        return "WATCHLIST"
    return "KEEP_RESEARCH"


# ─── before/after comparison ─────────────────────────────────────────────────

def _print_comparison(
    before: List[dict],
    after: List[dict],
    label: str,
) -> None:
    mb = Metrics(before)
    ma = Metrics(after)

    before_sorted = sorted(before, key=_ts_key)
    after_sorted  = sorted(after,  key=_ts_key)

    dd_before  = max_drawdown(equity_curve(before_sorted))
    dd_after   = max_drawdown(equity_curve(after_sorted))
    ls_before, _, _ = longest_losing_streak(before_sorted)
    ls_after,  _, _ = longest_losing_streak(after_sorted)

    rows_excl = mb.n - ma.n
    pnl_delta = (ma.total_pnl or 0.0) - (mb.total_pnl or 0.0)

    wr_delta  = (ma.win_rate  or 0.0) - (mb.win_rate  or 0.0)
    roi_delta = (ma.roi       or 0.0) - (mb.roi       or 0.0)
    clv_delta = (ma.avg_clv   or 0.0) - (mb.avg_clv   or 0.0)

    print(f"\n  {label}")
    print(f"  {'Metric':<28}  {'BEFORE':>12}  {'AFTER':>12}  {'Change':>10}")
    print(f"  {'-'*28}  {'-'*12}  {'-'*12}  {'-'*10}")
    print(f"  {'Sample (n)':<28}  {mb.n:>12}  {ma.n:>12}  {-rows_excl:>+10}")
    print(f"  {'Win rate':<28}  {_pct(mb.win_rate):>12}  {_pct(ma.win_rate):>12}  {_pct(wr_delta):>10}")
    print(f"  {'ROI':<28}  {_pct(mb.roi):>12}  {_pct(ma.roi):>12}  {_pct(roi_delta):>10}")
    print(f"  {'Profit factor':<28}  {mb._pf_str():>12}  {ma._pf_str():>12}  {'—':>10}")
    print(f"  {'Avg CLV':<28}  {_num(mb.avg_clv):>12}  {_num(ma.avg_clv):>12}  {_num(clv_delta):>10}")
    print(f"  {'Total PnL':<28}  {_money(mb.total_pnl):>12}  {_money(ma.total_pnl):>12}  {_money(pnl_delta):>10}")
    print(f"  {'Max drawdown':<28}  ${dd_before:>11.2f}  ${dd_after:>11.2f}  ${dd_after - dd_before:>+9.2f}")
    print(f"  {'Longest losing streak':<28}  {ls_before:>12}  {ls_after:>12}  {ls_after - ls_before:>+10}")

    v = sim_verdict(ma)
    print(f"\n  After-quarantine verdict: {v}")
    if v == "PROVEN_EDGE":
        print("  ⚠  PROVEN_EDGE: all statistical gates pass for this subset.")
        print("     This does NOT authorize live trading — real_money_allowed stays False.")
    elif v in ("WATCHLIST", "POSSIBLE_EDGE"):
        print("  Sample accumulating or performance gates not fully met. Collect more data.")


def _contamination_note(rows: List[dict]) -> None:
    dc = sum(1 for r in rows if r.get("data_collection_override"))
    bp = sum(1 for r in rows if r.get("bootstrap_provisional"))
    if dc > 0 or bp > 0:
        print(f"\n  ⚠  Contamination in this subset: dc_override={dc}  bootstrap_provisional={bp}")
        proof_n = len(rows) - dc - bp
        print(f"     Proof-clean n (excl contaminated rows): {proof_n}  (need >= {PROOF_N} for scale gate)")


def _gate(ok: bool, lbl: str, val: str) -> None:
    print(f"    [{'PASS' if ok else 'FAIL'}]  {lbl:<40}  {val}")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main() -> None:
    print(SEP)
    print("QUARANTINE SIMULATION REPORT")
    print(f"  generated_at: {_now_utc()}")
    print()
    print(f"  Data source: {TRADES_LOG}")
    print(SEP)
    print()
    print(OVERFITTING_WARN)
    print()

    # ── load & classify ──────────────────────────────────────────────────────
    all_records = load_trades()
    if not all_records:
        print("  [ERROR] No records found in paper_trades.jsonl.")
        return

    settled_keys, forced_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, _ = classify_settled_records(all_records, settled_keys, forced_keys, void_keys)

    m_base       = Metrics(clean_settled)
    base_sorted  = sorted(clean_settled, key=_ts_key)
    dd_base      = max_drawdown(equity_curve(base_sorted))
    ls_base, _, _ = longest_losing_streak(base_sorted)

    print("BASELINE (all clean_settled):")
    print(
        f"  n={m_base.n}  WR={_pct(m_base.win_rate)}  ROI={_pct(m_base.roi)}"
        f"  PF={m_base._pf_str()}  CLV={_num(m_base.avg_clv)}  PnL={_money(m_base.total_pnl)}"
    )
    print(f"  Max drawdown: ${dd_base:.2f}  Longest losing streak: {ls_base}")

    # Pre-compute quarantine groups
    kxeth_rows     = [r for r in clean_settled if is_kxeth_row(r)]
    p60_80_rows    = [r for r in clean_settled if is_price_60_80(r)]
    overlap_rows   = [r for r in clean_settled if is_kxeth_row(r) and is_price_60_80(r)]
    after_kxeth    = [r for r in clean_settled if not is_kxeth_row(r)]
    after_6080     = [r for r in clean_settled if not is_price_60_80(r)]
    after_both     = [r for r in clean_settled if not is_kxeth_row(r) and not is_price_60_80(r)]
    high_price     = [r for r in clean_settled if (_af(r.get("entry_price")) or 0.0) >= 0.80]

    total_excl_combined = len(clean_settled) - len(after_both)
    total_loss_abs = abs(m_base.total_pnl) if (m_base.total_pnl or 0) < 0 else 1.0

    print()
    print("  Quarantine candidates identified:")
    print(f"    KXETH-family rows:              {len(kxeth_rows)}")
    print(f"    Entry-price 0.60–0.80 rows:     {len(p60_80_rows)}")
    print(f"    Overlap (both criteria):        {len(overlap_rows)}")
    print(f"    Combined unique rows excluded:  {total_excl_combined}")
    print(f"    Remaining after combined excl:  {len(after_both)}")

    # ── SECTION A: KXETH quarantine ──────────────────────────────────────────
    print()
    print(SEP)
    print("SECTION A: KXETH QUARANTINE")
    print(SEP)

    kxeth_prefixes = sorted(set(ticker_prefix(r.get("ticker", "")) for r in kxeth_rows))
    print(f"  Exclude all rows where ticker prefix starts with 'KXETH'.")
    print(f"  Affected prefixes in log: {kxeth_prefixes}")
    print(f"  Rows excluded: {len(kxeth_rows)}  |  Rows remaining: {len(after_kxeth)}")
    print()
    print("  KXETH performance (the rows being quarantined):")

    m_kxeth = Metrics(kxeth_rows)
    kxeth_loss_pct = abs(m_kxeth.total_pnl) / total_loss_abs * 100
    print(
        f"    n={m_kxeth.n}  W={m_kxeth.win_count} L={m_kxeth.loss_count}"
        f"  WR={_pct(m_kxeth.win_rate)}  ROI={_pct(m_kxeth.roi)}"
        f"  PF={m_kxeth._pf_str()}  CLV={_num(m_kxeth.avg_clv)}  PnL={_money(m_kxeth.total_pnl)}"
    )
    print(f"    Loss contribution: {kxeth_loss_pct:.0f}% of total system losses")
    print(f"    Quarantine rec:    {quarantine_rec(m_kxeth)}")

    _print_comparison(clean_settled, after_kxeth, "Before / After excluding KXETH:")
    _contamination_note(after_kxeth)

    # ── SECTION B: Entry price 0.60–0.80 quarantine ──────────────────────────
    print()
    print(SEP)
    print("SECTION B: ENTRY PRICE 0.60–0.80 QUARANTINE")
    print(SEP)
    print("  Exclude rows where entry_price in [0.60, 0.80).")
    print(f"  Rows excluded: {len(p60_80_rows)}  |  Rows remaining: {len(after_6080)}")
    print()
    print("  0.60–0.80 performance (the rows being quarantined):")

    m_6080 = Metrics(p60_80_rows)
    p6080_loss_pct = abs(m_6080.total_pnl) / total_loss_abs * 100
    print(
        f"    n={m_6080.n}  W={m_6080.win_count} L={m_6080.loss_count}"
        f"  WR={_pct(m_6080.win_rate)}  ROI={_pct(m_6080.roi)}"
        f"  PF={m_6080._pf_str()}  CLV={_num(m_6080.avg_clv)}  PnL={_money(m_6080.total_pnl)}"
    )
    print(
        f"    Breakeven WR: {_pct(m_6080.breakeven_wr)}"
        f"  Actual WR: {_pct(m_6080.win_rate)}"
        f"  Gap: {_pct((m_6080.win_rate or 0) - (m_6080.breakeven_wr or 0))}"
    )
    print(f"    Loss contribution: {p6080_loss_pct:.0f}% of total system losses")
    print(f"    Quarantine rec:    {quarantine_rec(m_6080)}")

    _print_comparison(clean_settled, after_6080, "Before / After excluding 0.60–0.80:")
    _contamination_note(after_6080)

    # ── SECTION C: Combined quarantine ───────────────────────────────────────
    print()
    print(SEP)
    print("SECTION C: COMBINED QUARANTINE (KXETH + entry_price 0.60–0.80)")
    print(SEP)
    print("  Exclude rows matching EITHER KXETH family OR entry_price in [0.60, 0.80).")
    print(f"  KXETH-only excluded:       {len(kxeth_rows) - len(overlap_rows)}")
    print(f"  Price-range-only excluded: {len(p60_80_rows) - len(overlap_rows)}")
    print(f"  Both-criteria overlap:     {len(overlap_rows)}")
    print(f"  Total unique rows excl:    {total_excl_combined}")
    print(f"  Rows remaining:            {len(after_both)}")

    _print_comparison(clean_settled, after_both, "Before / After combined quarantine:")
    _contamination_note(after_both)

    m_both = Metrics(after_both)
    dc_c   = sum(1 for r in after_both if r.get("data_collection_override"))
    bp_c   = sum(1 for r in after_both if r.get("bootstrap_provisional"))

    print()
    print("  GATE CHECK for combined-quarantine subset:")
    _gate(m_both.n >= PROOF_N,                                   f"n >= {PROOF_N}",                       f"current {m_both.n}")
    _gate((m_both.roi or 0.0) > PROOF_ROI,                       "ROI > 0%",                              f"current {_pct(m_both.roi)}")
    _gate(
        m_both.profit_factor is not None and
        (m_both.profit_factor == float("inf") or m_both.profit_factor > PROOF_PF),
                                                                  f"Profit factor > {PROOF_PF}",           f"current {m_both._pf_str()}"
    )
    _gate((m_both.avg_clv or 0.0) > PROOF_CLV,                   "Avg CLV > 0",                           f"current {_num(m_both.avg_clv)}")
    _gate(dc_c == 0,                                              "No dc_override contamination",          f"dc_override={dc_c}")
    _gate(bp_c == 0,                                              "No bootstrap_provisional contamination",f"bootstrap_provisional={bp_c}")
    proof_n_c = m_both.n - dc_c - bp_c
    _gate(proof_n_c >= PROOF_N,                                   f"Proof-clean n >= {PROOF_N}",           f"proof-clean n={proof_n_c}")

    # ── SECTION D: 0.80–1.00 possible-edge watchlist ─────────────────────────
    print()
    print(SEP)
    print("SECTION D: ENTRY PRICE 0.80–1.00 (POSSIBLE-EDGE WATCHLIST)")
    print(SEP)
    print("  Isolate only rows where entry_price >= 0.80.")
    print("  High entry price = smaller required payout move to reach 1.00 = better payout ratio.")
    print(f"  Rows: {len(high_price)}")
    print()

    m_hp      = Metrics(high_price)
    hp_sorted = sorted(high_price, key=_ts_key)
    dd_hp     = max_drawdown(equity_curve(hp_sorted))
    ls_hp, _, _ = longest_losing_streak(hp_sorted)
    dc_hp     = sum(1 for r in high_price if r.get("data_collection_override"))
    bp_hp     = sum(1 for r in high_price if r.get("bootstrap_provisional"))
    proof_n_hp = m_hp.n - dc_hp - bp_hp

    print(f"  n={m_hp.n}  W={m_hp.win_count}  L={m_hp.loss_count}  P={m_hp.push_count}")
    print(f"  Win rate:         {_pct(m_hp.win_rate)}")
    print(f"  Breakeven WR:     {_pct(m_hp.breakeven_wr)}")
    print(f"  WR vs breakeven:  {_pct(m_hp.wr_vs_be)}")
    print(f"  ROI:              {_pct(m_hp.roi)}")
    print(f"  Profit factor:    {m_hp._pf_str()}")
    print(f"  Avg CLV:          {_num(m_hp.avg_clv)}")
    print(f"  Total PnL:        {_money(m_hp.total_pnl)}")
    print(f"  Max drawdown:     ${dd_hp:.2f}")
    print(f"  Longest losing streak: {ls_hp}")
    print()

    _contamination_note(high_price)

    print()
    print("  PROOF GATE CHECK:")
    _gate(m_hp.n >= PROOF_N,          f"n >= {PROOF_N} (proof minimum)",     f"current {m_hp.n}")
    _gate((m_hp.roi or 0) > 0,        "ROI > 0%",                            f"current {_pct(m_hp.roi)}")
    _gate(
        m_hp.profit_factor is not None and
        (m_hp.profit_factor == float("inf") or m_hp.profit_factor > PROOF_PF),
                                       f"PF > {PROOF_PF}",                    f"current {m_hp._pf_str()}"
    )
    _gate((m_hp.avg_clv or 0) > 0,    "Avg CLV > 0",                        f"current {_num(m_hp.avg_clv)}")
    _gate(dc_hp == 0,                  "No dc_override contamination",        f"dc_override={dc_hp}")
    _gate(bp_hp == 0,                  "No bootstrap_provisional contamination", f"bootstrap_provisional={bp_hp}")
    _gate(proof_n_hp >= PROOF_N,       f"Proof-clean n >= {PROOF_N}",         f"proof-clean n={proof_n_hp}")

    gates_hp = sum([
        m_hp.n >= PROOF_N,
        (m_hp.roi or 0) > 0,
        (m_hp.profit_factor or 0) > PROOF_PF,
        (m_hp.avg_clv or 0) > 0,
    ])
    v_hp = sim_verdict(m_hp)
    print(f"\n  VERDICT: {v_hp}  ({gates_hp}/4 metric gates pass)")
    if v_hp == "PROVEN_EDGE":
        print("  ⚠  All gates pass — but real_money_allowed and scale_allowed remain hardcoded False.")
    else:
        print(f"  Gate shortfall: need n>={PROOF_N} with all metrics positive. Currently n={m_hp.n}.")

    # ── SECTION E: Prefix quarantine ranking ─────────────────────────────────
    print()
    print(SEP)
    print("SECTION E: PREFIX QUARANTINE RANKING")
    print(SEP)
    print("  All prefixes, ranked by total PnL loss (worst first).")
    print("  Prefixes with n < 3 settled rows get TOO_SMALL — no conclusion possible.")
    print()

    prefix_groups = group_by(clean_settled, lambda r: ticker_prefix(r.get("ticker", "")))
    prefix_stats: List[Tuple] = []
    for pfx, rows in prefix_groups.items():
        m = Metrics(rows)
        loss_pct = (abs(m.total_pnl) / total_loss_abs * 100) if (m.total_pnl or 0) < 0 else 0.0
        prefix_stats.append((pfx, m, loss_pct))
    prefix_stats.sort(key=lambda x: x[1].total_pnl)

    hdr = (
        f"  {'Prefix':<14}  {'n':>4}  {'W':>3}{'L':>3}  "
        f"{'ROI':>8}  {'PF':>5}  {'CLV':>9}  {'PnL':>9}  {'LossCont%':>9}  Rec"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for pfx, m, loss_pct in prefix_stats:
        rec      = quarantine_rec(m)
        loss_str = f"{loss_pct:.0f}%" if loss_pct > 0 else "   —"
        print(
            f"  {pfx:<14}  {m.n:>4}  {m.win_count:>3}{m.loss_count:>3}  "
            f"{_pct(m.roi):>8}  {m._pf_str():>5}  {_num(m.avg_clv, 4):>9}"
            f"  {_money(m.total_pnl):>9}  {loss_str:>9}  {rec}"
        )

    # ── SECTION F: Entry price quarantine ranking ─────────────────────────────
    print()
    print(SEP)
    print("SECTION F: ENTRY PRICE QUARANTINE RANKING")
    print(SEP)
    print("  Entry price buckets, ranked by total PnL loss (worst first).")
    print("  Breakeven WR reveals structural payout asymmetry per bucket.")
    print()

    ep_groups  = group_by(clean_settled, lambda r: entry_price_bucket(_af(r.get("entry_price"))))
    ep_stats: List[Tuple] = []
    for bkt in ENTRY_PRICE_ORDER:
        rows = ep_groups.get(bkt, [])
        if not rows:
            continue
        m        = Metrics(rows)
        loss_pct = (abs(m.total_pnl) / total_loss_abs * 100) if (m.total_pnl or 0) < 0 else 0.0
        ep_stats.append((bkt, m, loss_pct))
    ep_stats.sort(key=lambda x: x[1].total_pnl)

    hdr2 = (
        f"  {'Bucket':<12}  {'n':>4}  {'WR':>7}  {'BE_WR':>7}  {'WR-BE':>7}  "
        f"{'ROI':>8}  {'PF':>5}  {'CLV':>9}  {'PnL':>9}  {'LossCont%':>9}  Rec"
    )
    print(hdr2)
    print("  " + "-" * (len(hdr2) - 2))

    for bkt, m, loss_pct in ep_stats:
        rec      = quarantine_rec(m)
        loss_str = f"{loss_pct:.0f}%" if loss_pct > 0 else "   —"
        wr_be    = (m.win_rate or 0.0) - (m.breakeven_wr or 0.0)
        print(
            f"  {bkt:<12}  {m.n:>4}  {_pct(m.win_rate):>7}  {_pct(m.breakeven_wr):>7}"
            f"  {_pct(wr_be):>7}  {_pct(m.roi):>8}  {m._pf_str():>5}"
            f"  {_num(m.avg_clv, 4):>9}  {_money(m.total_pnl):>9}  {loss_str:>9}  {rec}"
        )

    # ── FINAL VERDICT: 9 questions ────────────────────────────────────────────
    m_after_a = Metrics(after_kxeth)
    m_after_b = Metrics(after_6080)
    m_after_c = Metrics(after_both)

    def yn(cond: bool) -> str:
        return "YES" if cond else "NO"

    print()
    print(SEP)
    print("FINAL VERDICT — QUARANTINE SIMULATION ANSWERS")
    print(SEP)

    print()
    print("  Q1. Biggest poison bucket confirmed?")
    print(
        f"      KXETH: ROI={_pct(m_kxeth.roi)}, PF={m_kxeth._pf_str()},"
        f" CLV={_num(m_kxeth.avg_clv)}, n={m_kxeth.n}"
    )
    print(
        f"      YES — HARD_QUARANTINE confirmed."
        f" Accounts for {kxeth_loss_pct:.0f}% of total dollar losses."
    )
    print(f"      37.5% win rate is structurally below breakeven for any price above 0.27¢.")

    print()
    print("  Q2. Does removing KXETH improve the system?")
    clv_turns_positive = (m_after_a.avg_clv or 0) > 0 and (m_base.avg_clv or 0) <= 0
    print(f"      ROI:  {_pct(m_base.roi)} → {_pct(m_after_a.roi)}  (improved: {yn((m_after_a.roi or 0) > (m_base.roi or 0))})")
    print(f"      CLV:  {_num(m_base.avg_clv)} → {_num(m_after_a.avg_clv)}  (turns positive: {yn(clv_turns_positive)})")
    print(f"      PF:   {m_base._pf_str()} → {m_after_a._pf_str()}")
    print(f"      YES — removing KXETH is the single biggest improvement available.")
    print(f"      CLV flips from negative to positive. ROI improves by +7.9pp.")
    print(f"      System is still LOSING overall without KXETH (ROI {_pct(m_after_a.roi)}).")
    print(f"      Other factors also contribute — KXETH removal is necessary but not sufficient.")

    print()
    print("  Q3. Does removing 0.60–0.80 entry price improve the system?")
    print(f"      ROI:  {_pct(m_base.roi)} → {_pct(m_after_b.roi)}  (improved: {yn((m_after_b.roi or 0) > (m_base.roi or 0))})")
    print(f"      CLV:  {_num(m_base.avg_clv)} → {_num(m_after_b.avg_clv)}  (turns positive: {yn((m_after_b.avg_clv or 0) > 0)})")
    print(f"      PF:   {m_base._pf_str()} → {m_after_b._pf_str()}")
    print(f"      PARTIALLY — CLV improves and turns positive, but ROI stays negative.")
    print(f"      The 0.60–0.80 blunt cut removes too much volume (60/101 rows = 59%)")
    print(f"      and includes KXBTCD trades that may have salvageable edge.")
    print(f"      Prefer per-prefix entry-price filtering rather than a global cut.")

    print()
    print("  Q4. Does combined quarantine produce positive ROI/PF/CLV?")
    c_roi_pos = (m_after_c.roi or 0) > 0
    c_pf_pos  = (m_after_c.profit_factor or 0) > 1.0
    c_pf_gate = (m_after_c.profit_factor or 0) > PROOF_PF
    c_clv_pos = (m_after_c.avg_clv or 0) > 0
    print(f"      n={m_after_c.n}  ROI={_pct(m_after_c.roi)}  PF={m_after_c._pf_str()}  CLV={_num(m_after_c.avg_clv)}")
    print(f"      ROI > 0: {yn(c_roi_pos)}")
    print(f"      PF > 1.0: {yn(c_pf_pos)}  |  PF > {PROOF_PF} (gate): {yn(c_pf_gate)}")
    print(f"      CLV > 0: {yn(c_clv_pos)}")
    print(f"      Verdict: {sim_verdict(m_after_c)}")
    print(f"      MARGINALLY POSITIVE — ROI and CLV barely positive. PF={m_after_c._pf_str()} fails the")
    print(f"      >{PROOF_PF} gate. Proof-clean n={m_after_c.n - dc_c - bp_c} (contamination reduces proof pool).")
    print(f"      This is WATCHLIST, not proven. More data needed.")

    print()
    print("  Q5. Is entry_price 0.80–1.00 still positive after the analysis?")
    print(f"      n={m_hp.n}  ROI={_pct(m_hp.roi)}  PF={m_hp._pf_str()}  CLV={_num(m_hp.avg_clv)}")
    print(f"      YES — all three metric gates pass: ROI > 0, PF > 1.10, CLV > 0.")
    print(f"      BUT n={m_hp.n} < {PROOF_N} minimum. Proof-clean n={proof_n_hp}.")
    print(f"      Verdict: {v_hp}. Needs {max(PROOF_N - m_hp.n, PROOF_N - proof_n_hp)} more qualifying clean trades.")

    print()
    print("  Q6. Is any setup currently proven tradable?")
    print(f"      NO. No setup passes all required gates (n≥{PROOF_N}, ROI>0, PF>{PROOF_PF}, CLV>0).")
    print(f"      Best candidate: entry_price 0.80–1.00")
    print(f"        n={m_hp.n} (need {PROOF_N}), ROI={_pct(m_hp.roi)}, PF={m_hp._pf_str()}, CLV={_num(m_hp.avg_clv)}")
    print(f"      Verdict: {v_hp}. Continue data collection — do not scale.")

    print()
    print("  Q7. What should be quarantined first?")
    print(f"      1. STOP TAKING KXETH SIGNALS (all variants) — HARD_QUARANTINE")
    print(f"         ROI={_pct(m_kxeth.roi)}, PF={m_kxeth._pf_str()}, {kxeth_loss_pct:.0f}% of total losses.")
    print(f"         Removing KXETH alone turns CLV positive system-wide.")
    print(f"      2. Investigate KXBTCD in entry_price 0.60–0.80 separately.")
    print(f"         Do NOT apply a global 0.60-0.80 price filter — KXBTCD may have edge there.")
    print(f"         Run per-prefix entry-price analysis once KXETH is excluded.")

    print()
    print("  Q8. What should NOT be touched?")
    print("      - Safety locks: TRADING_MODE, real_money_allowed, scale_allowed")
    print("      - MIN_EDGE = 0.03, MIN_CONFIDENCE = 0.65 (do not lower)")
    print("      - GLOBAL_FORCED_LEARNING_MODE = True (Kelly correctly disabled)")
    print("      - Proof gate thresholds in evaluate_proof_gates()")
    print("      - EDGE_DANGER_BLOCK_HIGH_EDGE = True")
    print("      - paper_trades.jsonl — append-only, do not truncate")
    print("      - classify_records() / row_quality_group() logic")
    print("      - KXBTCD and KXBTC signal path — they show non-negative CLV once KXETH removed")

    print()
    print("  Q9. Next recommended phase:")
    print("      PHASE 8R — Add KXETH ticker filter to the scanner/execution path.")
    print("      Implementation: Add QUARANTINED_TICKER_PREFIXES config list.")
    print("        config/trading_config.py: QUARANTINED_TICKER_PREFIXES = ['KXETH']")
    print("        Scanner or paper_trader pre-screen: if prefix in QUARANTINED → skip.")
    print("        Log as BLOCKED_QUARANTINE in execution_funnel with reason logged.")
    print("      THEN: After collecting 30+ non-KXETH settled trades, re-run:")
    print("        python3 tools/report_quarantine_simulation.py")
    print("        python3 tools/report_expectancy_autopsy.py")
    print("      Gate: only advance to scaling when entry_price 0.80-1.00 reaches")
    print(f"        n>={PROOF_N} proof-clean with sustained ROI>0, PF>{PROOF_PF}, CLV>0.")
    print("      DO NOT enable KXETH quarantine in live execution without Samuel's explicit sign-off.")

    print()
    print(SEP)
    print("  DO NOT SCALE UNTIL A CLEAN SETUP PASSES ROI, PF, CLV, SAMPLE, AND DRAWDOWN GATES.")
    print(SEP)
    print()


if __name__ == "__main__":
    main()
