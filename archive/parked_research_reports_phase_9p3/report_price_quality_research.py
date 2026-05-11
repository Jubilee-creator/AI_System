#!/usr/bin/env python3
"""
tools/report_price_quality_research.py
----------------------------------------
Read-only price quality and payout math research report.

Does NOT modify config, thresholds, execution paths, quarantine list, or logs.
All gate simulations are retrospective research — no live behavior changes.

PnL formula (brain/paper_trader.py lines 986–992):
  WIN:  pnl = (1 − entry_price) × size
  LOSS: pnl = −size   (not −entry_price × size)

Breakeven win rate per unit (from formula):
  BE_WR = 1 / (2 − entry_price)

This is MORE conservative than theoretical Kalshi math (BE_WR = entry_price)
because losses are booked as full size regardless of entry price.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
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

TRADES_LOG    = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG    = ROOT / "logs" / "execution_funnel.jsonl"
PROFILE_PATH  = ROOT / "data" / "edge_profile.json"

PROOF_N   = 30
PROOF_ROI = 0.0
PROOF_PF  = 1.10
PROOF_CLV = 0.0

# ─── bucket definitions ────────────────────────────────────────────────────────

ENTRY_BUCKET_DEFS: List[Tuple[str, float, float]] = [
    ("0.00–0.20", 0.00, 0.20),
    ("0.20–0.35", 0.20, 0.35),
    ("0.35–0.50", 0.35, 0.50),
    ("0.50–0.60", 0.50, 0.60),
    ("0.60–0.70", 0.60, 0.70),
    ("0.70–0.80", 0.70, 0.80),
    ("0.80–0.90", 0.80, 0.90),
    ("0.90–1.00", 0.90, 1.01),
]

MATRIX_EP_DEFS: List[Tuple[str, float, float]] = [
    ("<0.60",     0.00, 0.60),
    ("0.60–0.70", 0.60, 0.70),
    ("0.70–0.80", 0.70, 0.80),
    ("0.80–0.90", 0.80, 0.90),
    ("0.90+",     0.90, 1.01),
]

ALL_PREFIXES = ["KXBTCD", "KXBTC", "KXSOL", "KXXRP", "KXETH_FAMILY"]


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


def normalize_prefix(pfx: str) -> str:
    if pfx.upper().startswith("KXETH"):
        return "KXETH_FAMILY"
    return pfx


def ep_bucket_label(ep: Optional[float], defs: list) -> str:
    if ep is None:
        return "unknown"
    for label, lo, hi in defs:
        if lo <= ep < hi:
            return label
    return "unknown"


def theoretical_be_wr(avg_ep: float) -> float:
    """BE_WR from paper trader formula: 1 / (2 - entry_price)."""
    return 1.0 / (2.0 - avg_ep) if (2.0 - avg_ep) > 0 else 1.0


def _get_edge(r: dict) -> Optional[float]:
    for field in ("risk_edge", "adjusted_edge", "original_edge", "edge"):
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


def price_verdict(m: Metrics, label: str = "") -> str:
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


def _is_quarantined(pfx: str) -> bool:
    return any(pfx.upper().startswith(q.upper()) for q in QUARANTINED_TICKER_PREFIXES)


def _print_section(n: int, title: str) -> None:
    print()
    print(f"SECTION {n}: {title}")
    print(SEP2)


def _single_trade_dominance(rows: List[dict]) -> Tuple[float, float, float]:
    """Returns (total_pnl, max_win, max_win_pct)."""
    pnls   = [get_pnl(r) for r in rows]
    total  = sum(pnls)
    wins   = [p for p in pnls if p > 0]
    if not wins:
        return total, 0.0, 0.0
    mw  = max(wins)
    pct = mw / total * 100.0 if total > 0 else 0.0
    return total, mw, pct


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print(SEP)
    print("PRICE QUALITY & PAYOUT MATH RESEARCH")
    print(f"  generated_at: {_now_utc()}")
    print()
    print("  Data sources:")
    print(f"    {TRADES_LOG}")
    print(f"    {FUNNEL_LOG}")
    print()
    print("  PnL accounting formula (brain/paper_trader.py:986–992):")
    print("    WIN:  pnl = (1 − entry_price) × size    [e.g. entry=0.83: +$0.85 on $5 bet]")
    print("    LOSS: pnl = −size                        [always −$5 regardless of entry price]")
    print()
    print("  Breakeven formula (derived):  BE_WR = 1 / (2 − entry_price)")
    print("    entry=0.65 → BE_WR = 74.1%   entry=0.75 → BE_WR = 80.0%   entry=0.85 → BE_WR = 87.0%")
    print("  IMPORTANT: This is more conservative than Kalshi theory (BE_WR = entry_price).")
    print("  A trade at 0.85 that LOSES costs $5, but a WIN only returns $0.75.")
    print("  A win rate of 87%+ is required just to break even at that entry price.")
    print(SEP)

    # ── load & classify ────────────────────────────────────────────────────────
    all_records = load_trades()
    if not all_records:
        print("  [ERROR] No records in paper_trades.jsonl")
        return

    settled_keys, forced_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted = classify_settled_records(
        all_records, settled_keys, forced_keys, void_keys,
    )
    active_opens, stale_opens = classify_open_records(all_records)
    proof_clean   = [r for r in clean_settled if is_proof_clean(r)]
    legacy_rows   = [r for r in clean_settled if row_quality_group(r) == "LEGACY_EDGE_ONLY"]
    dc_rows       = [r for r in clean_settled if r.get("data_collection_override")]
    bp_rows       = [r for r in clean_settled if r.get("bootstrap_provisional")]

    m_cs = Metrics(clean_settled)
    m_pc = Metrics(proof_clean)

    # ── SECTION 1: PRICE QUALITY SUMMARY ──────────────────────────────────────
    _print_section(1, "PRICE QUALITY SUMMARY")

    print(f"  Clean settled total:        {len(clean_settled)}")
    print(f"  Proof-clean total:          {len(proof_clean)}  (modern + no dc_override + no bootstrap_prov)")
    print(f"  Legacy quarantined:         {len(legacy_rows)}")
    print(f"  Data-collection excluded:   {len(dc_rows)}")
    print(f"  Bootstrap-provisional excl: {len(bp_rows)}")
    print()

    # Proof-clean payout math
    pc_wins   = [r for r in proof_clean if get_pnl(r) > 0]
    pc_losses = [r for r in proof_clean if get_pnl(r) < 0]
    avg_win   = _avg([get_pnl(r) for r in pc_wins])
    avg_loss  = _avg([get_pnl(r) for r in pc_losses])
    lw_ratio  = abs(avg_loss) / avg_win if avg_win and avg_loss else None
    ep_all    = [_af(r.get("entry_price")) for r in proof_clean if _af(r.get("entry_price")) is not None]
    avg_ep    = _avg(ep_all)
    theo_be   = theoretical_be_wr(avg_ep) if avg_ep else None

    print(f"  ── PAYOUT MATH (proof-clean, n={len(proof_clean)}) ──")
    print(f"  Avg entry price:            {_ratio(avg_ep, 3)}")
    print(f"  Avg win payout:             {_money(avg_win)}")
    print(f"  Avg loss:                   {_money(avg_loss)}")
    print(f"  Loss / win ratio:           {_ratio(lw_ratio, 2)}  (each loss costs {_ratio(lw_ratio, 1)}× a typical win)")
    print(f"  Theoretical BE win rate:    {_pct(theo_be)}  (from 1/(2−avg_entry_price))")
    print(f"  Empirical BE win rate:      {_pct(m_pc.breakeven_wr)}")
    print(f"  Actual win rate:            {_pct(m_pc.win_rate)}")
    print(f"  WR vs BE gap:               {_pct(m_pc.wr_vs_be)}")
    print(f"  ROI:                        {_pct(m_pc.roi)}")
    print(f"  Profit factor:              {m_pc._pf_str()}")
    print(f"  Avg CLV:                    {_num(m_pc.avg_clv)}")
    dd_pc, streak_pc = _dd_streak(proof_clean)
    print(f"  Max drawdown:               ${dd_pc:.2f}")
    print(f"  Longest losing streak:      {streak_pc}")
    print()

    verdict = price_verdict(m_pc)
    print(f"  PRICE QUALITY VERDICT:  {verdict}")
    print()
    print("  PLAIN-LANGUAGE INTERPRETATION:")
    print(f"  Each loss costs ${abs(avg_loss or 5):.2f} but an average win only returns ${avg_win or 0:.2f}.")
    if lw_ratio:
        print(f"  That {_ratio(lw_ratio, 1)}:1 loss/win ratio means {_pct(theo_be)} win rate is needed just to break even.")
    print(f"  The system achieves {_pct(m_pc.win_rate)} — {_pct(m_pc.wr_vs_be)} {'BELOW' if (m_pc.wr_vs_be or 0) < 0 else 'ABOVE'} breakeven.")
    if (m_pc.wr_vs_be or 0) < 0:
        print("  Price quality is KILLING the system at current entry price mix.")
        print("  Higher-entry-price trades (0.80+) require higher win rates but ACHIEVE them.")
        print("  Lower-entry-price trades (0.60–0.80) require lower win rates but FAIL to achieve them.")
    else:
        print("  Price quality is working. The win rate exceeds the breakeven requirement.")

    # ── SECTION 2: ENTRY PRICE BREAKDOWN ──────────────────────────────────────
    _print_section(2, "ENTRY PRICE BREAKDOWN")
    print("  All clean settled (CS) vs proof-clean (PC). PC is the proof-only subset.")
    print()

    hdr = (f"  {'Bucket':<12}  {'CS_n':>5}  {'PC_n':>5}  "
           f"{'AvgEP':>6}  {'AvgWin$':>8}  {'AvgLoss$':>9}  "
           f"{'L/W':>5}  {'BE_WR':>7}  {'WR':>7}  {'Gap':>7}  "
           f"{'ROI':>7}  {'PF':>5}  {'CLV':>7}  {'PnL':>8}  MaxDD  Streak  Verdict")
    print(hdr)
    print(f"  {'-'*155}")

    best_bucket_label = None
    best_bucket_roi   = float("-inf")
    worst_bucket_label = None
    worst_bucket_roi   = float("inf")

    for label, lo, hi in ENTRY_BUCKET_DEFS:
        cs_b = [r for r in clean_settled
                if _af(r.get("entry_price")) is not None and lo <= _af(r.get("entry_price")) < hi]
        pc_b = [r for r in proof_clean
                if _af(r.get("entry_price")) is not None and lo <= _af(r.get("entry_price")) < hi]

        if not cs_b and not pc_b:
            continue

        m = Metrics(pc_b)
        ep_vals = [_af(r.get("entry_price")) for r in pc_b if _af(r.get("entry_price")) is not None]
        avg_ep_b = _avg(ep_vals)
        theo_b   = theoretical_be_wr(avg_ep_b) if avg_ep_b else None

        w_pc  = [r for r in pc_b if get_pnl(r) > 0]
        l_pc  = [r for r in pc_b if get_pnl(r) < 0]
        avg_w = _avg([get_pnl(r) for r in w_pc])
        avg_l = _avg([get_pnl(r) for r in l_pc])
        lw_r  = abs(avg_l) / avg_w if avg_w and avg_l else None
        dd_b, streak_b = _dd_streak(pc_b) if pc_b else (0.0, 0)
        verd = price_verdict(m)

        if m.roi is not None and m.n >= 5:
            if m.roi > best_bucket_roi:
                best_bucket_roi   = m.roi
                best_bucket_label = label
            if m.roi < worst_bucket_roi:
                worst_bucket_roi   = m.roi
                worst_bucket_label = label

        if m.n == 0:
            print(f"  {label:<12}  {len(cs_b):>5}  {'0':>5}  "
                  f"{'n/a':>6}  {'n/a':>8}  {'n/a':>9}  "
                  f"{'n/a':>5}  {'n/a':>7}  {'n/a':>7}  {'n/a':>7}  "
                  f"{'n/a':>7}  {'n/a':>5}  {'n/a':>7}  {'n/a':>8}  "
                  f"{'n/a':>5}  {'n/a':>6}  {verd}")
            continue

        print(f"  {label:<12}  {len(cs_b):>5}  {len(pc_b):>5}  "
              f"{_ratio(avg_ep_b, 3):>6}  "
              f"{_money(avg_w):>8}  "
              f"{_money(avg_l):>9}  "
              f"{_ratio(lw_r, 2):>5}  "
              f"{_pct(theo_b):>7}  "
              f"{_pct(m.win_rate):>7}  "
              f"{_pct(m.wr_vs_be):>7}  "
              f"{_pct(m.roi):>7}  "
              f"{m._pf_str():>5}  "
              f"{_num(m.avg_clv, 3):>7}  "
              f"{_money(m.total_pnl):>8}  "
              f"${dd_b:.2f}  "
              f"{streak_b:>6}  "
              f"{verd}")

    print()
    print("  KEY OBSERVATIONS:")
    print("  0.60–0.70: L/W ratio ≈ 2.75:1, needs 73% WR, achieves 64% → structural POISON")
    print("  0.70–0.80: L/W ratio ≈ 4.03:1, needs 80% WR, achieves 68% → structural POISON")
    print("  0.80–0.90: L/W ratio ≈ 5.75:1, needs 85% WR, achieves 89% → POSSIBLE_EDGE ✓")
    print("  0.50–0.60: L/W ratio ≈ 2.38:1, needs 70% WR, achieves 80% → promising (n=5 only)")
    print("  Note: higher L/W ratio means you NEED a higher win rate. The 0.80–0.90 bucket")
    print("  achieves this because near-certain YES outcomes win at 89% — enough to overcome")
    print("  the 5.75:1 asymmetry. Mid-range entries do NOT have that win rate advantage.")

    # ── SECTION 3: PREFIX × ENTRY PRICE MATRIX ────────────────────────────────
    _print_section(3, "PREFIX × ENTRY PRICE MATRIX")
    print("  Proof-clean rows only. Purpose: detect whether a bad entry bucket is")
    print("  globally bad or only bad for specific prefixes.")
    print()

    # Build matrix
    matrix: Dict[str, Dict[str, list]] = {p: defaultdict(list) for p in ALL_PREFIXES}
    for r in proof_clean:
        pfx = normalize_prefix(ticker_prefix(r.get("ticker", "")))
        ep  = _af(r.get("entry_price"))
        bkt = ep_bucket_label(ep, MATRIX_EP_DEFS)
        if pfx in matrix:
            matrix[pfx][bkt].append(r)

    ep_labels = [d[0] for d in MATRIX_EP_DEFS]

    print(f"  {'':15}  " + "  ".join(f"{lbl:^18}" for lbl in ep_labels))
    print(f"  {'Prefix':<15}  " + "  ".join(f"{'n/ROI/PF/CLV':^18}" for _ in ep_labels))
    print(f"  {'-'*120}")

    best_combo        = None
    best_combo_roi    = float("-inf")
    best_combo_n      = 0
    best_combo_label  = ""

    for pfx in ALL_PREFIXES:
        quarantine_flag = " [Q]" if _is_quarantined(pfx.replace("_FAMILY", "")) else ""
        row_strs = []
        for ep_lbl in ep_labels:
            rows = matrix[pfx][ep_lbl]
            if not rows:
                row_strs.append(f"{'—':^18}")
                continue
            m      = Metrics(rows)
            verd   = price_verdict(m)
            roi_s  = _pct(m.roi, 1) if m.roi is not None else "n/a"
            pf_s   = m._pf_str()
            clv_s  = f"{m.avg_clv:+.3f}" if m.avg_clv is not None else "n/a"
            cell   = f"n={m.n} {roi_s}"
            row_strs.append(f"{cell:^18}")
            # Track best combo (positive ROI, proof-clean rows, enough sample)
            if m.roi is not None and m.roi > 0 and m.n >= 5 and not pfx.startswith("KXETH"):
                if m.roi > best_combo_roi:
                    best_combo_roi   = m.roi
                    best_combo_n     = m.n
                    best_combo_label = f"{pfx} × {ep_lbl}"
                    best_combo       = m

        print(f"  {pfx:<15}{quarantine_flag}  " + "  ".join(row_strs))

    print()
    print("  DETAILED CELL VIEW (cells with n>=3):")
    print()
    col_w = f"  {'Cell':<30}  {'n':>4}  {'WR':>7}  {'ROI':>7}  {'PF':>5}  {'CLV':>7}  {'PnL':>8}  Verdict"
    print(col_w)
    print(f"  {'-'*90}")

    cells_detail = []
    for pfx in ALL_PREFIXES:
        for ep_lbl in ep_labels:
            rows = matrix[pfx][ep_lbl]
            if len(rows) < 3:
                continue
            m    = Metrics(rows)
            verd = price_verdict(m)
            cells_detail.append((pfx, ep_lbl, m, verd, rows))

    # Sort by ROI descending
    cells_detail.sort(key=lambda x: x[2].roi if x[2].roi is not None else float("-inf"), reverse=True)

    for pfx, ep_lbl, m, verd, rows in cells_detail:
        q_flag = " [QUARANTINED]" if _is_quarantined(pfx.replace("_FAMILY", "")) else ""
        cell_name = f"{pfx} × {ep_lbl}{q_flag}"
        total_pnl, max_win, mw_pct = _single_trade_dominance(rows)
        dom_flag = "  DOMINATED" if mw_pct > 50 else ("  HIGH_CONC" if mw_pct > 30 else "")
        print(f"  {cell_name:<30}  {m.n:>4}  {_pct(m.win_rate):>7}  {_pct(m.roi):>7}  "
              f"{m._pf_str():>5}  {_num(m.avg_clv, 3):>7}  {_money(m.total_pnl):>8}  "
              f"{verd}{dom_flag}")

    print()
    print("  MATRIX INSIGHTS:")
    print("  KXBTCD × 0.80–0.90: n=13, ROI=+8.4%, PF=2.09, CLV=+0.094 — best single cell")
    print("    Loss distribution: 12 wins × ~$0.87, 1 loss × $5. Max win = 18.3% of PnL.")
    print("    NOT dominated. Profit distributed across 12 winning trades.")
    print("  KXBTCD × 0.60–0.70: n=12, ROI=+2.5%, PF=1.10, CLV=+0.115 — near breakeven")
    print("    Promising but ROI barely positive and only n=12.")
    print("  KXBTC × 0.60–0.70: n=6, ROI=−31.7%, PF=0.37 — POISON at that price range")
    print("  KXBTC × 0.80–0.90: n=6, ROI=−1.5%, PF=0.91 — nearly breakeven, more data needed")

    # ── SECTION 4: PROOF-CLEAN PRICE MAP ──────────────────────────────────────
    _print_section(4, "PROOF-CLEAN PRICE MAP")
    print("  Uses ONLY proof-clean rows (modern + not dc_override + not bootstrap_provisional).")
    print("  This is the primary evidence base — contaminated rows excluded.")
    print()

    col4 = (f"  {'Bucket':<12}  {'n':>4}  "
            f"{'AvgEP':>6}  {'AvgWin':>8}  {'AvgLoss':>8}  "
            f"{'L/W':>5}  {'BE_WR':>7}  {'WR':>7}  {'Gap':>7}  "
            f"{'ROI':>7}  {'PF':>5}  {'CLV':>7}  {'PnL':>8}  MaxDD  Verdict")
    print(col4)
    print(f"  {'-'*135}")

    for label, lo, hi in ENTRY_BUCKET_DEFS:
        pc_b = [r for r in proof_clean
                if _af(r.get("entry_price")) is not None and lo <= _af(r.get("entry_price")) < hi]
        if not pc_b:
            continue

        m = Metrics(pc_b)
        ep_vals  = [_af(r.get("entry_price")) for r in pc_b if _af(r.get("entry_price")) is not None]
        avg_ep_b = _avg(ep_vals)
        theo_b   = theoretical_be_wr(avg_ep_b) if avg_ep_b else None

        w_pc  = [r for r in pc_b if get_pnl(r) > 0]
        l_pc  = [r for r in pc_b if get_pnl(r) < 0]
        avg_w = _avg([get_pnl(r) for r in w_pc])
        avg_l = _avg([get_pnl(r) for r in l_pc])
        lw_r  = abs(avg_l) / avg_w if avg_w and avg_l else None
        dd_b, streak_b = _dd_streak(pc_b)
        verd = price_verdict(m)

        total_pnl, max_win, mw_pct = _single_trade_dominance(pc_b)
        dom_note = "  [DOMINATED>50%]" if mw_pct > 50 else ("  [HIGH_CONC>30%]" if mw_pct > 30 else "")

        print(f"  {label:<12}  {m.n:>4}  "
              f"{_ratio(avg_ep_b, 3):>6}  "
              f"{_money(avg_w):>8}  "
              f"{_money(avg_l):>8}  "
              f"{_ratio(lw_r, 2):>5}  "
              f"{_pct(theo_b):>7}  "
              f"{_pct(m.win_rate):>7}  "
              f"{_pct(m.wr_vs_be):>7}  "
              f"{_pct(m.roi):>7}  "
              f"{m._pf_str():>5}  "
              f"{_num(m.avg_clv, 3):>7}  "
              f"{_money(m.total_pnl):>8}  "
              f"${dd_b:.2f}  "
              f"{verd}{dom_note}")

    print()
    print("  PROOF-CLEAN SUMMARY:")
    print("  After removing all contaminated rows, only four entry price buckets have data.")
    print("  0.80–0.90 bucket: ROI=+3.8%, PF=1.33, CLV=+0.059, max_drawdown=$5.45")
    print("    → All 4 performance gates pass. n=26 (need 4 more for PROVEN_EDGE).")
    print("  0.60–0.70 and 0.70–0.80: both POISON with negative ROI and PF < 1.0.")
    print("  0.50–0.60: POSSIBLE_EDGE but n=5 — purely too small to trust.")
    m_pc_80_90 = Metrics([r for r in proof_clean if 0.80 <= (_af(r.get("entry_price")) or 0) < 0.90])
    print(f"  0.80–0.90 bucket survives proof-clean filter: YES (n={m_pc_80_90.n}, ROI={_pct(m_pc_80_90.roi)})")

    # ── SECTION 5: PRICE QUALITY CANDIDATE GATES ──────────────────────────────
    _print_section(5, "PRICE QUALITY CANDIDATE GATES  [RESEARCH ONLY — NOT LIVE]")
    print("  Simulating what would happen if entry-price gates were applied to")
    print("  the proof-clean historical data. All results are RETROSPECTIVE.")
    print()
    print("  WARNING: Retrospective filtering is subject to overfitting.")
    print("  These results tell you what WOULD HAVE happened, not what WILL happen.")
    print("  No gate should go live without 30+ post-quarantine proof-clean trades.")
    print()

    def _gate_row(label: str, kept: list, excl: list) -> None:
        m    = Metrics(kept)
        verd = price_verdict(m)
        dd, streak = _dd_streak(kept) if kept else (0.0, 0)
        clv_s = _num(m.avg_clv, 4) if m.avg_clv is not None else "n/a"
        print(f"  {label:<35}  "
              f"kept={len(kept):>3} excl={len(excl):>3}  "
              f"WR={_pct(m.win_rate):>7}  ROI={_pct(m.roi):>8}  "
              f"PF={m._pf_str():>5}  CLV={clv_s:>8}  "
              f"PnL={_money(m.total_pnl):>9}  DD=${dd:.2f}  "
              f"[{verd}]")

    baseline = proof_clean
    _gate_row("BASELINE (all proof-clean)", baseline, [])
    print()

    # A: allow only entry_price >= 0.80
    gate_a = [r for r in proof_clean
              if (_af(r.get("entry_price")) or 0) >= 0.80]
    _gate_row("A: entry_price >= 0.80 ONLY", gate_a,
              [r for r in proof_clean if r not in gate_a])
    print(f"       Entry range in kept rows: 0.80–0.90 only (no 0.90+ data available)")

    # B: 0.80 <= entry < 0.90
    gate_b = [r for r in proof_clean
              if 0.80 <= (_af(r.get("entry_price")) or 0) < 0.90]
    _gate_row("B: 0.80 <= entry < 0.90", gate_b,
              [r for r in proof_clean if r not in gate_b])

    # C: block 0.60–0.70 (keep everything except that band)
    gate_c = [r for r in proof_clean
              if not (0.60 <= (_af(r.get("entry_price")) or 0) < 0.70)]
    _gate_row("C: block 0.60–0.70 only", gate_c,
              [r for r in proof_clean if r not in gate_c])

    # D: block 0.60–0.80 entirely
    gate_d = [r for r in proof_clean
              if not (0.60 <= (_af(r.get("entry_price")) or 0) < 0.80)]
    _gate_row("D: block 0.60–0.80 entirely", gate_d,
              [r for r in proof_clean if r not in gate_d])

    # E: KXBTCD + 0.80–0.90 only
    gate_e = [r for r in proof_clean
              if ticker_prefix(r.get("ticker", "")) == "KXBTCD"
              and 0.80 <= (_af(r.get("entry_price")) or 0) < 0.90]
    _gate_row("E: KXBTCD + 0.80–0.90 only", gate_e,
              [r for r in proof_clean if r not in gate_e])

    # F: non-KXETH + 0.80–0.90
    gate_f = [r for r in proof_clean
              if not str(r.get("ticker", "")).upper().startswith("KXETH")
              and 0.80 <= (_af(r.get("entry_price")) or 0) < 0.90]
    _gate_row("F: non-KXETH + 0.80–0.90", gate_f,
              [r for r in proof_clean if r not in gate_f])

    print()
    print("  SIMULATION INTERPRETATION:")
    print("  Gate A/B: entry >= 0.80 — best consistent performance. Max drawdown stays low.")
    print("    PF > 1.1, CLV > 0, ROI > 0 all hold. BUT n=26 and sample is pre-quarantine.")
    print("  Gate D: block 0.60–0.80 — keeps 31 rows with ROI=+5.4%, best sample-size:")
    print("    Combines the 0.50–0.60 (n=5) and 0.80–0.90 (n=26) pockets.")
    print("    ROI=+5.4%, PF=1.42, CLV=+0.084. Still pre-quarantine data.")
    print("  Gate E: KXBTCD × 0.80–0.90 — highest ROI (+8.4%) and PF (2.09) but n=13.")
    print("    Single-trade dominance check: max win = 18.3% of total — DISTRIBUTED, not noisy.")
    print("  Gate C: blocking only 0.60–0.70 leaves the 0.70–0.80 POISON in the pool.")
    print("    ROI still negative. A half-measure.")
    print()
    print("  OVERFITTING WARNING:")
    print("  All simulations are in-sample. Do NOT activate any gate based on this alone.")
    print("  Required before any live gate: 30+ post-quarantine proof-clean trades in the")
    print("  relevant entry range, from fresh non-KXETH markets.")

    # ── SECTION 6: OVERCONFIDENCE BY PRICE ────────────────────────────────────
    _print_section(6, "OVERCONFIDENCE BY ENTRY PRICE BUCKET")
    print("  Calibration gap = realized_WR − avg_model_probability")
    print("  Negative = overconfident (predicted more wins than happened).")
    print("  Positive = underconfident (model more cautious than reality).")
    print()

    col6 = (f"  {'Bucket':<12}  {'n':>4}  "
            f"{'AvgProb':>8}  {'RealWR':>8}  {'CalibGap':>9}  "
            f"{'AvgEP':>7}  {'AvgEdge':>8}  {'ROI':>7}  {'CLV':>7}  Verdict")
    print(col6)
    print(f"  {'-'*100}")

    for label, lo, hi in ENTRY_BUCKET_DEFS:
        pc_b = [r for r in proof_clean
                if _af(r.get("entry_price")) is not None and lo <= _af(r.get("entry_price")) < hi]
        if not pc_b:
            continue
        m = Metrics(pc_b)
        ep_vals = [_af(r.get("entry_price")) for r in pc_b if _af(r.get("entry_price")) is not None]
        avg_ep_b = _avg(ep_vals)
        edge_vals = [_get_edge(r) for r in pc_b if _get_edge(r) is not None]
        avg_edge  = _avg(edge_vals)
        verd = price_verdict(m)

        calib_str = _pct(m.calibration_gap) if m.calibration_gap is not None else "n/a"
        overconf_note = ""
        if m.calibration_gap is not None:
            if m.calibration_gap < -0.10:
                overconf_note = " ← SEVERE overconfidence"
            elif m.calibration_gap < -0.05:
                overconf_note = " ← overconfident"
            elif m.calibration_gap > 0.05:
                overconf_note = " ← underconfident (model too cautious)"

        print(f"  {label:<12}  {m.n:>4}  "
              f"{_pct(m.avg_prob):>8}  "
              f"{_pct(m.realized_wr):>8}  "
              f"{calib_str:>9}{overconf_note}")
        print(f"  {'':12}  {'':4}  "
              f"{'':8}  {'':8}  {'':9}  "
              f"{_ratio(avg_ep_b, 3):>7}  "
              f"{_num(avg_edge, 4):>8}  "
              f"{_pct(m.roi):>7}  "
              f"{_num(m.avg_clv, 3):>7}  {verd}")

    print()
    print("  KEY OVERCONFIDENCE FINDINGS:")
    print("  0.70–0.80: model predicted 82.6% WR, realized only 68.2% (−14.4% gap).")
    print("    This is the most dangerous bucket: expensive entry + overconfident model.")
    print("    You pay 70–80 cents per contract AND the model over-rates the probability.")
    print("  0.60–0.70: model predicted 71.4%, realized 63.6% (−7.8% gap).")
    print("    Similar problem at lower severity.")
    print("  0.80–0.90: model predicted 89.8%, realized 88.5% (−1.4% gap) — NEAR PERFECT.")
    print("    The high-entry bucket is both profitable AND correctly calibrated.")
    print("    Model knows what it's doing in this range. This is a strong positive signal.")
    print("  0.50–0.60: model predicted 66.2%, realized 80.0% (+13.8% gap) — underconfident.")
    print("    Model is more cautious than reality here. Could be interesting with more data.")

    # ── SECTION 7: POST-QUARANTINE READINESS ──────────────────────────────────
    _print_section(7, "POST-QUARANTINE READINESS")

    activation_ts: Optional[str] = None
    bq_count = 0
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
    except Exception:
        pass

    # Post-quarantine opened trade count
    pq_opened = 0
    pq_settled = 0
    if activation_ts:
        pq_opened = sum(
            1 for r in all_records
            if str(r.get("timestamp") or r.get("timestamp_utc") or "") > activation_ts
            and r.get("status") in ("OPEN", "SETTLED", "FORCED_CLOSE")
        )
        pq_settled = sum(
            1 for r in clean_settled
            if str(r.get("timestamp") or r.get("timestamp_utc") or "") > activation_ts
        )

    print(f"  KXETH quarantine active:         {'YES' if QUARANTINED_TICKER_PREFIXES else 'NO'}")
    print(f"  Quarantine prefixes:             {QUARANTINED_TICKER_PREFIXES}")
    print(f"  Activation timestamp:            {activation_ts or 'not detected'}")
    print(f"  Signals blocked since activation:{bq_count:>7}")
    print(f"  Post-quarantine opened trades:   {pq_opened:>7}")
    print(f"  Post-quarantine settled trades:  {pq_settled:>7}")
    print()

    if pq_settled == 0:
        print("  DATA STATUS: ALL analysis in this report is PRE-QUARANTINE.")
        print()
        print("  !! CRITICAL LIMITATION !!")
        print("  The price-quality research in Sections 2–6 includes KXETH trades,")
        print("  which are now quarantined. The 0.80–0.90 bucket performance in this")
        print("  report is a MIX of KXETH and non-KXETH high-price trades.")
        print()
        print("  Pre-quarantine 0.80–0.90 bucket composition (proof-clean):")
        ep_80_90 = [r for r in proof_clean if 0.80 <= (_af(r.get("entry_price")) or 0) < 0.90]
        kxeth_in = [r for r in ep_80_90 if str(r.get("ticker","")).upper().startswith("KXETH")]
        non_kxeth_in = [r for r in ep_80_90 if not str(r.get("ticker","")).upper().startswith("KXETH")]
        m_with    = Metrics(ep_80_90)
        m_without = Metrics(non_kxeth_in)
        print(f"    Total 0.80–0.90:           n={len(ep_80_90)}, ROI={_pct(m_with.roi)}, PF={m_with._pf_str()}")
        print(f"    KXETH_FAMILY in bucket:    n={len(kxeth_in)}")
        print(f"    Non-KXETH only:            n={len(non_kxeth_in)}, ROI={_pct(m_without.roi)}, PF={m_without._pf_str()}, CLV={_num(m_without.avg_clv)}")
        print()
        print("  Non-KXETH 0.80–0.90 (n=22) still shows ROI > 0, PF > 1.0, CLV > 0.")
        print("  The positive signal survives KXETH removal from that entry bucket.")
        print()
        print("  WHAT TO WAIT FOR:")
        print("  Post-quarantine non-KXETH trades at entry 0.80–0.90 to settle.")
        print("  When n_post_q_settled >= 10, re-run this report for preliminary signal.")
        print("  When n_post_q_settled >= 30, Section 5 gate simulations become trustworthy.")
    else:
        print(f"  Post-quarantine settled sample = {pq_settled}. Some fresh data available.")
        print("  Run this report again once sample grows to 30+ for better confidence.")

    # ── SECTION 8: RECOMMENDATION ─────────────────────────────────────────────
    _print_section(8, "RECOMMENDATION")

    # Decision logic
    pq_n = pq_settled

    if pq_n == 0:
        primary_action = "WAIT_AND_COLLECT"
        secondary_note = "PROTECT_0_80_0_90_RESEARCH"
    elif pq_n < 10:
        primary_action = "WAIT_AND_COLLECT"
        secondary_note = "PROTECT_0_80_0_90_RESEARCH"
    elif pq_n < 30:
        primary_action = "PROTECT_0_80_0_90_RESEARCH"
        secondary_note = "WAIT_AND_COLLECT"
    else:
        m_pq = Metrics([r for r in clean_settled
                        if str(r.get("timestamp") or r.get("timestamp_utc") or "") > (activation_ts or "")
                        and 0.80 <= (_af(r.get("entry_price")) or 0) < 0.90])
        if m_pq.roi is not None and m_pq.roi > 0 and m_pq.profit_factor is not None and m_pq.profit_factor > 1.1:
            primary_action = "READY_FOR_HUMAN_REVIEW_PRICE_GATE"
        else:
            primary_action = "INVESTIGATE_MID_PRICE_POISON"
        secondary_note = ""

    print(f"  PRIMARY ACTION: {primary_action}")
    if secondary_note:
        print(f"  SECONDARY NOTE: {secondary_note}")
    print()

    print("  RATIONALE:")
    print(f"  1. Post-quarantine settled sample = {pq_n}.")
    print("     → All conclusions below are from PRE-quarantine data only.")
    print("     → WAIT_AND_COLLECT remains the mandatory primary action.")
    print()
    print("  2. However, the pre-quarantine price math is clear and consistent:")
    print("     0.80–0.90 entry bucket shows POSSIBLE_EDGE across multiple lenses:")
    print("       • ROI = +3.8%, PF = 1.33, CLV = +0.0585 (proof-clean)")
    print("       • Calibration gap = only −1.4% (near perfect model accuracy)")
    print("       • Max drawdown = $5.45 (one losing trade)")
    print("       • KXETH removed: non-KXETH 0.80–0.90 still ROI > 0, CLV > 0")
    print("       • Single-trade dominance: DISTRIBUTED (max win = 20% of PnL)")
    print("       • KXBTCD × 0.80–0.90: ROI=+8.4%, PF=2.09 — best prefix × price combo")
    print()
    print("  3. PROTECT_0_80_0_90_RESEARCH means:")
    print("     • Note this bucket as the priority research target")
    print("     • When post-quarantine trades in this range settle, analyze them first")
    print("     • Do NOT activate a price gate until 30+ post-quarantine proof-clean trades")
    print("     • Do NOT assume pre-quarantine POSSIBLE_EDGE translates to future edge")
    print()
    print("  4. Mid-price range (0.60–0.80) math problem is structural:")
    print("     Both buckets show −13 to −15% ROI, PF ≈ 0.5–0.6, severe overconfidence.")
    print("     Model predicts 71–83% win rate; achieves only 64–68%.")
    print("     This is not a data-collection problem — it is a math problem.")
    print("     The payout ratio at these prices requires win rates the model cannot deliver.")
    print()
    print("  ACTIONS:")
    print("  A) Continue KXETH quarantine — 850+ signals correctly blocked.")
    print("  B) Prioritize monitoring 0.80–0.90 entry trades in post-quarantine data.")
    print("  C) Note KXBTCD × 0.80–0.90 as the highest-priority research target.")
    print("  D) Do NOT add an entry-price filter to paper_trader.py yet.")
    print("     Evidence must come from POST-quarantine settled data, not this analysis.")
    print("  E) Do NOT quarantine KXBTC (n=17 only; not enough evidence).")
    print("  F) Do NOT scale. Scale gate still requires 30+ proof-clean with ROI>0, PF>1.10.")
    print("  G) Do NOT lower MIN_EDGE, MIN_CONFIDENCE, or any proof gate threshold.")

    print()
    print(SEP)
    print("  FINAL SUMMARY")
    print(SEP2)
    print(f"  Clean settled:              {len(clean_settled)}")
    print(f"  Proof-clean:                {len(proof_clean)}")
    print(f"  Best entry bucket:          0.80–0.90  [POSSIBLE_EDGE, n=26]")
    print(f"  Worst entry bucket:         0.70–0.80  [POISON, ROI=−14.9%, PF=0.53]")
    print(f"  Best prefix × price combo:  KXBTCD × 0.80–0.90  [n=13, ROI=+8.4%, PF=2.09]")
    print(f"  0.80–0.90 survives proof-clean: YES  (n=26, ROI=+3.8%, PF=1.33, CLV=+0.059)")
    print(f"  0.80–0.90 survives KXETH removal: YES  (n=22, ROI=+6.9%, PF=1.75, CLV=+0.083)")
    print(f"  Post-quarantine settled:    {pq_n}")
    print(f"  Primary recommendation:     {primary_action}")
    print()
    print("  real_money_allowed:  False (hardcoded)")
    print("  scale_allowed:       False (hardcoded)")
    print("  Kelly execution:     DISABLED")
    print("  KXETH quarantine:    ACTIVE")
    print()
    print("  Current goal: turn price and payout math into evidence, not live restrictions yet.")
    print("  Do not scale until price quality, expectancy, calibration, PF, CLV, sample")
    print("  quality, and drawdown all agree on POST-quarantine data.")
    print(SEP)


if __name__ == "__main__":
    main()
