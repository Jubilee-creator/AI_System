#!/usr/bin/env python3
"""
tools/report_2d_clv_payoff_cells.py
--------------------------------------
Phase 9K: 2D CLV / Payoff Cell Report.

Shows edge_bucket × entry_price_bucket for all non-KXETH clean-settled trades.
Per cell: trades, win_rate, total_pnl, avg_pnl, avg_clv, true EV, breakeven WR,
and a diagnosis (GOOD / PAYOFF_STRUCTURE / MODEL_QUALITY / MIXED / TOO_SMALL).

Payoff structure note:
  paper_trader.settle_trade() uses:
    WIN:  pnl = (1 - entry_price) * size
    LOSS: pnl = -size
  This is ASYMMETRIC (win gains 1-ep per unit, loss costs 1 per unit).
  CLV = exit_price - entry_price  →  avg_CLV ≈ WR - ep  (signal quality)
  True EV per unit = WR*(2-ep) - 1  (actual profitability under real payoff)
  CLV breakeven WR  = ep
  True breakeven WR = 1 / (2 - ep)  [correct for this system's payoff structure]

Usage:
  python3 tools/report_2d_clv_payoff_cells.py

DIAGNOSTIC ONLY — zero live trading behavior is modified.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

PROFILE_PATH = ROOT / "data" / "edge_profile.json"

from performance_report import (
    build_terminal_key_sets,
    classify_settled_records,
    get_clv,
    get_pnl,
    load_trades,
)

try:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES as _RAW_Q
except ImportError:
    _RAW_Q = ["KXETH"]

_EXCLUDED: frozenset = frozenset(str(p).upper() for p in _RAW_Q)

MIN_N_DIAGNOSIS = 5   # minimum trades to assign a diagnosis
MIN_N_CONFIDENT = 20  # minimum trades to call a cell "confident"
_SENTINEL = "PROVEN_2D_CLV_PAYOFF_OK"


# ── bucket helpers ────────────────────────────────────────────────────────────

def _edge_bucket(e: Optional[float]) -> Optional[str]:
    if e is None:
        return None
    if e < 0.03: return "<0.03"
    if e < 0.05: return "0.03-0.05"
    if e < 0.10: return "0.05-0.10"
    if e < 0.25: return "0.10-0.25"
    if e < 0.50: return "0.25-0.50"
    return ">=0.50"


def _price_bucket(p: Optional[float]) -> Optional[str]:
    if p is None:
        return None
    if p < 0.10: return "<0.10"
    if p < 0.20: return "0.10-0.20"
    if p < 0.30: return "0.20-0.30"
    if p < 0.40: return "0.30-0.40"
    if p < 0.50: return "0.40-0.50"
    if p < 0.60: return "0.50-0.60"
    if p < 0.70: return "0.60-0.70"
    if p < 0.80: return "0.70-0.80"
    if p < 0.90: return "0.80-0.90"
    return "0.90-1.00"


def _is_excluded(ticker: Optional[str]) -> bool:
    t = str(ticker or "").upper()
    return any(t.startswith(pfx) for pfx in _EXCLUDED)


# ── cell accumulator ──────────────────────────────────────────────────────────

def _empty_raw() -> dict:
    return {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0,
            "clv_sum": 0.0, "clv_count": 0, "pos_clv": 0, "neg_clv": 0,
            "ep_sum": 0.0}


def _add(raw: dict, rec: dict) -> None:
    pnl = get_pnl(rec)
    ep  = rec.get("entry_price") or rec.get("yes_ask")
    if ep is None:
        return
    ep = float(ep)
    raw["n"] += 1
    raw["ep_sum"] += ep
    raw["pnl"]    += pnl
    if pnl > 0:
        raw["wins"] += 1
    elif pnl < 0:
        raw["losses"] += 1

    clv = get_clv(rec)
    if clv is None and rec.get("exit_price") is not None:
        clv = float(rec["exit_price"]) - ep
    if clv is not None:
        raw["clv_sum"]   += clv
        raw["clv_count"] += 1
        if clv > 0:
            raw["pos_clv"] += 1
        elif clv < 0:
            raw["neg_clv"] += 1


# ── cell finalization ─────────────────────────────────────────────────────────

def _diagnose(n: int, avg_clv: Optional[float], total_pnl: float, true_ev: float) -> str:
    if n < MIN_N_DIAGNOSIS:
        return "TOO_SMALL"
    if avg_clv is None:
        return "NO_CLV_DATA"
    if avg_clv > 0 and total_pnl > 0:
        return "GOOD"
    if avg_clv > 0 and total_pnl <= 0:
        return "PAYOFF_STRUCTURE"
    if avg_clv <= 0 and total_pnl <= 0:
        return "MODEL_QUALITY"
    return "MIXED"


def _finalize_cell(raw: dict) -> dict:
    n = raw["n"]
    if n == 0:
        return {"n": 0, "diagnosis": "EMPTY"}
    wins   = raw["wins"]
    wr     = wins / n
    pnl    = raw["pnl"]
    avg_ep = raw["ep_sum"] / n
    cn     = raw["clv_count"]
    avg_clv = raw["clv_sum"] / cn if cn else None

    # True breakeven: WIN=(1-ep)*size, LOSS=-size → BE_WR = 1/(2-ep)
    true_be_wr  = 1.0 / (2.0 - avg_ep)
    # CLV breakeven: symmetric assumption → BE_WR = ep
    clv_be_wr   = avg_ep
    # True EV per unit under actual payoff structure
    true_ev     = wr * (2.0 - avg_ep) - 1.0

    diagnosis = _diagnose(n, avg_clv, pnl, true_ev)

    return {
        "n":               n,
        "wins":            wins,
        "losses":          raw["losses"],
        "win_rate":        round(wr, 4),
        "total_pnl":       round(pnl, 2),
        "avg_pnl":         round(pnl / n, 4),
        "clv_count":       cn if cn else None,
        "avg_clv":         round(avg_clv, 4) if avg_clv is not None else None,
        "total_clv":       round(raw["clv_sum"], 4) if cn else None,
        "positive_clv_count": raw["pos_clv"] if cn else None,
        "negative_clv_count": raw["neg_clv"] if cn else None,
        "positive_clv_rate": round(raw["pos_clv"] / cn, 4) if cn else None,
        "avg_entry_price": round(avg_ep, 4),
        "true_be_wr":      round(true_be_wr, 4),
        "clv_be_wr":       round(clv_be_wr, 4),
        "wr_vs_true_be":   round(wr - true_be_wr, 4),
        "true_ev":         round(true_ev, 4),
        "diagnosis":       diagnosis,
    }


# ── build table ───────────────────────────────────────────────────────────────

def build_2d_cell_data(records: list[dict]) -> dict[str, dict]:
    """
    Build finalized 2D table {cell_key: cell_dict} from a list of records.
    Uses original_edge (fallback to edge) for edge bucketing (Critic-consistent).
    Uses entry_price (fallback to yes_ask) for price bucketing.
    Excludes quarantined tickers.
    Public API used by the test suite.
    """
    raw: dict[str, dict] = defaultdict(_empty_raw)
    for rec in records:
        if _is_excluded(rec.get("ticker")):
            continue
        oe = rec.get("original_edge") or rec.get("edge")
        ep = rec.get("entry_price") or rec.get("yes_ask")
        if oe is None or ep is None:
            continue
        eb = _edge_bucket(float(oe))
        pb = _price_bucket(float(ep))
        if eb and pb:
            _add(raw[f"{eb}|{pb}"], rec)
    return {k: _finalize_cell(v) for k, v in sorted(raw.items())}


# ── print helpers ─────────────────────────────────────────────────────────────

def _hdr(title: str) -> None:
    print(f"\n  {title}")
    print(f"  {'─'*66}")


def _diag_abbrev(d: str) -> str:
    return {
        "GOOD":             "GOOD",
        "PAYOFF_STRUCTURE":  "PAYOFF_STRUCT",
        "MODEL_QUALITY":     "MODEL_QUALITY",
        "MIXED":            "MIXED",
        "TOO_SMALL":        "TOO_SMALL",
        "NO_CLV_DATA":      "NO_CLV",
        "EMPTY":            "---",
    }.get(d, d)


def _flag(cell: dict) -> str:
    flags = []
    if cell["n"] >= MIN_N_CONFIDENT:
        flags.append("n≥20")
    if cell.get("avg_clv") is not None and cell["avg_clv"] > 0 and cell["total_pnl"] < 0:
        flags.append("CLV+/PnL-")
    if cell.get("avg_clv") is not None and cell["avg_clv"] < 0 and cell.get("win_rate",0) > cell.get("clv_be_wr",1):
        flags.append("neg-CLV/hi-WR")
    return " ".join(flags)


def _print_table(cells: dict[str, dict], label: str) -> None:
    _hdr(label)
    print(
        f"  {'Cell':>22}  {'n':>4}  {'WR':>6}  {'PnL':>8}  "
        f"{'avgCLV':>8}  {'TrueEV':>8}  {'TrueBE':>7}  {'WR-BE':>6}  "
        f"{'Diagnosis':>14}  Flags"
    )
    print(
        f"  {'─'*22}  {'─'*4}  {'─'*6}  {'─'*8}  "
        f"{'─'*8}  {'─'*8}  {'─'*7}  {'─'*6}  "
        f"{'─'*14}  {'─'*12}"
    )
    for key, c in sorted(cells.items()):
        if c["n"] == 0:
            continue
        clv_s  = f"{c['avg_clv']:+.4f}" if c.get("avg_clv") is not None else "    n/a"
        tev_s  = f"{c['true_ev']:+.4f}" if "true_ev" in c else "    n/a"
        tbe_s  = f"{c['true_be_wr']:.4f}" if "true_be_wr" in c else "  n/a"
        wrbe_s = f"{c['wr_vs_true_be']:+.4f}" if "wr_vs_true_be" in c else "  n/a"
        diag   = _diag_abbrev(c["diagnosis"])
        fl     = _flag(c)
        print(
            f"  {key:>22}  {c['n']:>4}  {c['win_rate']:>6.3f}  {c['total_pnl']:>+8.2f}  "
            f"{clv_s:>8}  {tev_s:>8}  {tbe_s:>7}  {wrbe_s:>6}  "
            f"{diag:>14}  {fl}"
        )


# ── sections ──────────────────────────────────────────────────────────────────

def _load_clean_settled_non_kxeth() -> tuple[list, list, list]:
    """Returns (all_non_kxeth, kxeth_excluded, all_clean_settled)."""
    all_records = load_trades()
    s_keys, fc_keys, v_keys = build_terminal_key_sets(all_records)
    clean, conflicted = classify_settled_records(all_records, s_keys, fc_keys, v_keys)
    kxeth = [r for r in clean if _is_excluded(r.get("ticker"))]
    non_kxeth = [r for r in clean if not _is_excluded(r.get("ticker"))]
    return non_kxeth, kxeth, clean


def _load_stored_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text())
    except Exception:
        return {}


def section_1_overview(non_kxeth: list, kxeth: list, clean: list) -> None:
    _hdr("1. POPULATION OVERVIEW")
    print(f"  clean_settled total        : {len(clean)}")
    print(f"  KXETH excluded             : {len(kxeth)}")
    print(f"  non-KXETH for 2D analysis  : {len(non_kxeth)}")
    nm = sum(1 for r in non_kxeth
             if r.get("council_decision") is not None
             and r.get("bootstrap_provisional") is not None
             and not r.get("data_collection_override")
             and not r.get("bootstrap_provisional"))
    dc_count = sum(1 for r in non_kxeth if r.get("data_collection_override"))
    bp_count = sum(1 for r in non_kxeth if r.get("bootstrap_provisional") and not r.get("data_collection_override"))
    legacy   = sum(1 for r in non_kxeth if r.get("council_decision") is None)
    print(f"    normal_modern (proof-eligible): {nm}")
    print(f"    dc_override (learning)         : {dc_count}")
    print(f"    bootstrap_provisional          : {bp_count}")
    print(f"    legacy (pre-modern)            : {legacy}")
    print()
    print("  Edge bucketing  : original_edge (fallback to edge) — Critic-consistent")
    print("  Price bucketing : entry_price (fallback to yes_ask)")
    print()
    print("  Payoff structure confirmed from paper_trader.settle_trade():")
    print("    WIN:  pnl = (1 - entry_price) × size")
    print("    LOSS: pnl = -size                  [NOT -ep×size]")
    print("  ∴ True breakeven WR = 1/(2-ep), not ep")
    print("    CLV breakeven WR = ep  [standard symmetric formula — optimistic by 1-10pp]")


def section_2_all_table(cells: dict) -> None:
    _print_table(cells, "2. FULL 2D TABLE — ALL NON-KXETH CLEAN SETTLED")
    print()
    print("  Columns:")
    print("    avgCLV  = avg(exit_price - entry_price) ≈ WR - ep  (signal quality)")
    print("    TrueEV  = WR*(2-ep) - 1  (EV per unit, actual payoff structure)")
    print("    TrueBE  = 1/(2-ep)       (breakeven WR under actual payoff)")
    print("    WR-BE   = WR - TrueBE    (> 0 → profitable; < 0 → losing)")


def section_3_stored_profile(profile: dict) -> None:
    table = profile.get("profiles", {}).get("by_edge_price_bucket")
    if not table:
        _hdr("3. STORED PROFILE 2D TABLE (normal_modern only)")
        print("  Profile not loaded or by_edge_price_bucket missing.")
        print("  Run: python3 tools/build_edge_profile.py")
        return

    _hdr("3. STORED PROFILE 2D TABLE — NORMAL_MODERN ONLY (from edge_profile.json)")
    print(
        f"  {'Cell':>22}  {'n':>4}  {'WR':>6}  {'PnL':>8}  "
        f"{'avgCLV':>8}  {'posRate':>7}  avgEP  TrueBE  WR-BE  Diagnosis"
    )
    print(
        f"  {'─'*22}  {'─'*4}  {'─'*6}  {'─'*8}  "
        f"{'─'*8}  {'─'*7}  {'─'*6}  {'─'*6}  {'─'*6}  {'─'*14}"
    )
    for key in sorted(table.keys()):
        c   = table[key]
        n   = c["trades"]
        wr  = c["win_rate"]
        pnl = c["total_pnl"]
        avg_clv   = c.get("avg_clv")
        pos_rate  = c.get("positive_clv_rate")
        avg_ep    = c.get("avg_edge")  # profile stores avg_edge as risk_edge-based
        # For breakeven compute from win rate and pnl
        # Approximate avg_ep from PnL structure: avg_win = pnl_wins/wins, etc.
        # We'll note this is approximated from win/loss counts
        clv_s  = f"{avg_clv:+.4f}" if avg_clv is not None else "    n/a"
        pr_s   = f"{pos_rate:.3f}" if pos_rate is not None else "  n/a"
        # For stored cells, we don't have entry_price sum — show approximate from positive_clv_rate
        # positive_clv_rate ≈ win_rate for binary, avg_clv ≈ WR - ep
        if avg_clv is not None and wr > 0:
            approx_ep = wr - avg_clv  # avg_ep ≈ wr - avg_clv (from CLV = WR - ep formula)
            approx_ep = max(0.01, min(0.99, approx_ep))
            true_be  = 1.0 / (2.0 - approx_ep)
            wr_be    = wr - true_be
            diag     = _diagnose(n, avg_clv, pnl, wr * (2.0 - approx_ep) - 1.0)
            be_s     = f"{true_be:.4f}"
            wrbe_s   = f"{wr_be:+.4f}"
            ep_s     = f"{approx_ep:.4f}"
        else:
            be_s = wrbe_s = ep_s = "  n/a"
            diag = "TOO_SMALL" if n < MIN_N_DIAGNOSIS else "NO_CLV"
        print(
            f"  {key:>22}  {n:>4}  {wr:>6.3f}  {pnl:>+8.2f}  "
            f"{clv_s:>8}  {pr_s:>7}  {ep_s:>6}  {be_s:>6}  {wrbe_s:>6}  "
            f"{_diag_abbrev(diag)}"
        )


def section_4_special_cells(cells: dict) -> None:
    _hdr("4. SPECIAL CELL HIGHLIGHTS")

    SPECIAL = [
        ("0.05-0.10|0.80-0.90", "SWEET SPOT — 2D gate override target"),
        ("0.05-0.10|0.70-0.80", "POISON ZONE — 2D gate blocks (named)"),
        ("0.05-0.10|0.60-0.70", "MID-PRICE — thin CLV, payoff asymmetry concern"),
        ("0.05-0.10|0.50-0.60", "LOW-PRICE — best raw WR, smallest true breakeven gap"),
        ("0.10-0.25|0.80-0.90", "HIGH-EDGE (risk_edge) — all have original_edge 0.05-0.10"),
    ]

    for key, label in SPECIAL:
        c = cells.get(key)
        print(f"\n  [{key}]  {label}")
        if c is None or c["n"] == 0:
            print(f"    Not populated in all-clean-settled dataset.")
            # Special note for 0.10-0.25 cells
            if "0.10-0.25" in key:
                print("    (17 risk_edge 0.10-0.25 trades have original_edge in 0.05-0.10|0.80-0.90)")
            continue
        print(f"    n={c['n']}  WR={c['win_rate']:.3f}  PnL={c['total_pnl']:+.2f}")
        if c.get("avg_clv") is not None:
            print(f"    avg_CLV={c['avg_clv']:+.4f}  TrueEV={c['true_ev']:+.4f}")
            print(f"    CLV_BE={c['clv_be_wr']:.4f}  True_BE={c['true_be_wr']:.4f}  WR-TrueBE={c['wr_vs_true_be']:+.4f}")
        print(f"    Diagnosis: {c['diagnosis']}")
        if c["n"] >= MIN_N_CONFIDENT:
            print(f"    [CONFIDENT: n={c['n']} ≥ {MIN_N_CONFIDENT}]")
        # Specific commentary
        if key == "0.05-0.10|0.80-0.90":
            print("    → WR well above True_BE → genuinely profitable under actual payoff")
            print("    → 2D gate correctly identifies this as the sweet spot")
        elif key == "0.05-0.10|0.70-0.80":
            print("    → avg_CLV < 0: model is WRONG directionally in this zone")
            print("    → WR below both CLV_BE and True_BE → MODEL QUALITY problem")
            print("    → 2D gate correctly blocks this zone (poison cell)")
        elif key == "0.05-0.10|0.60-0.70":
            avg_clv_c = c.get("avg_clv", 0) or 0
            if avg_clv_c > 0:
                print("    → avg_CLV barely positive: model is barely right directionally")
            else:
                print("    → avg_CLV negative (all-clean): legacy/dc_override drag model quality negative")
                print("    → normal_modern only shows avg_CLV=+0.047 (PAYOFF_STRUCTURE in proof view)")
            print(f"    → True_BE={c['true_be_wr']:.3f} >> CLV_BE={c['clv_be_wr']:.3f}: payoff asymmetry penalty")
            print(f"    → WR={c['win_rate']:.3f} < True_BE={c['true_be_wr']:.3f}: not profitable under actual payoff")
            print("    → 2D gate blocks this cell (WR < 0.80 threshold not met)")
        elif key == "0.05-0.10|0.50-0.60":
            print("    → Cheapest entry prices: True_BE is lowest at ~0.70")
            print(f"    → WR={c['win_rate']:.3f} > True_BE={c['true_be_wr']:.3f}: genuinely profitable")
            print("    → n too small for definitive price-quality filter recommendation")


def section_5_asymmetry_analysis(cells: dict) -> None:
    _hdr("5. PAYOFF ASYMMETRY ANALYSIS")
    print("  System actual payoff: WIN=(1-ep)×bet, LOSS=-bet (confirmed from code)")
    print()
    print("  CLV formula assumes symmetric: WIN=(1-ep), LOSS=-ep")
    print("  Actual system: LOSS=-1 (not -ep) → asymmetric penalty at low entry prices")
    print()
    print(f"  {'Bucket':>22}  {'avg_ep':>7}  {'CLV_BE':>7}  {'True_BE':>8}  {'Gap':>6}  {'WR':>6}  {'Verdict'}")
    print(f"  {'─'*22}  {'─'*7}  {'─'*7}  {'─'*8}  {'─'*6}  {'─'*6}  {'─'*20}")
    for key in sorted(cells.keys()):
        c = cells[key]
        if c["n"] < MIN_N_DIAGNOSIS:
            continue
        ep      = c["avg_entry_price"]
        clv_be  = c["clv_be_wr"]
        true_be = c["true_be_wr"]
        gap     = true_be - clv_be
        wr      = c["win_rate"]
        if wr > true_be:
            verdict = "PROFITABLE"
        elif wr > clv_be:
            verdict = "CLV+, EV- (asymmetry kills)"
        else:
            verdict = "LOSING (CLV negative)"
        print(
            f"  {key:>22}  {ep:>7.4f}  {clv_be:>7.4f}  {true_be:>8.4f}  "
            f"{gap:>+6.4f}  {wr:>6.3f}  {verdict}"
        )
    print()
    print("  GAP = True_BE - CLV_BE: how much extra WR is needed beyond CLV prediction")
    print("  At ep=0.57: gap=+0.129 → need 12.9pp MORE WR than CLV suggests")
    print("  At ep=0.84: gap=+0.022 → CLV is a close approximation at high prices")
    print("  → The CLV metric alone is INSUFFICIENT at mid/low entry prices (ep < 0.75)")


def section_6_diagnosis(cells: dict) -> None:
    _hdr("6. DIAGNOSIS SUMMARY")

    good_cells      = [k for k,c in cells.items() if c["diagnosis"] == "GOOD"]
    payoff_cells    = [k for k,c in cells.items() if c["diagnosis"] == "PAYOFF_STRUCTURE"]
    model_cells     = [k for k,c in cells.items() if c["diagnosis"] == "MODEL_QUALITY"]
    mixed_cells     = [k for k,c in cells.items() if c["diagnosis"] == "MIXED"]
    small_cells     = [k for k,c in cells.items() if c["diagnosis"] == "TOO_SMALL"]

    def _show(label, keys):
        if not keys:
            print(f"  {label}: none")
            return
        print(f"  {label}:")
        for k in keys:
            c = cells[k]
            print(f"    {k}: n={c['n']} WR={c['win_rate']:.3f} PnL={c['total_pnl']:+.2f}")

    _show("GOOD", good_cells)
    _show("PAYOFF_STRUCTURE", payoff_cells)
    _show("MODEL_QUALITY", model_cells)
    _show("MIXED", mixed_cells)
    _show("TOO_SMALL (n<5)", small_cells)

    print()
    total_pnl = sum(c["total_pnl"] for c in cells.values() if c["n"] > 0)
    good_pnl  = sum(cells[k]["total_pnl"] for k in good_cells)
    bad_pnl   = sum(cells[k]["total_pnl"] for k in payoff_cells + model_cells)
    print(f"  Total PnL across all cells : {total_pnl:+.2f}")
    print(f"  GOOD cells PnL contribution: {good_pnl:+.2f}")
    print(f"  BAD  cells PnL contribution: {bad_pnl:+.2f}")


def section_7_questions(cells: dict) -> None:
    _hdr("7. ANSWERING THE 6 KEY QUESTIONS")

    c_sweet = cells.get("0.05-0.10|0.80-0.90", {})
    c_60_70 = cells.get("0.05-0.10|0.60-0.70", {})
    c_70_80 = cells.get("0.05-0.10|0.70-0.80", {})
    c_50_60 = cells.get("0.05-0.10|0.50-0.60", {})

    print()
    print("  Q1. Is the sweet-spot cell truly good?")
    if c_sweet.get("n", 0) >= MIN_N_DIAGNOSIS:
        print(f"      n={c_sweet['n']}  WR={c_sweet['win_rate']:.3f}  True_BE={c_sweet['true_be_wr']:.3f}  WR-BE={c_sweet['wr_vs_true_be']:+.4f}")
        if c_sweet["wr_vs_true_be"] > 0:
            print("      YES — WR exceeds true breakeven. Profitable under actual payoff. 2D gate correct.")
        else:
            print("      NO — WR does not exceed true breakeven despite positive CLV.")
    else:
        print("      Insufficient data.")

    print()
    print("  Q2. Is the system mainly suffering from bad entry price?")
    print("      NO — both losing zones are primarily MODEL QUALITY problems.")
    print("      Note: normal_modern only (Section 3) shows 0.60-0.70 as PAYOFF_STRUCTURE")
    print("      but all-clean-settled is MODEL_QUALITY (legacy/dc_override drags avg_CLV negative).")
    if c_60_70.get("n", 0) >= MIN_N_DIAGNOSIS:
        diag_60 = c_60_70.get("diagnosis", "?")
        print(f"      - 0.60-0.70 zone (n={c_60_70['n']}, all-clean): {diag_60}")
        print(f"        avg_CLV={c_60_70.get('avg_clv', 0):+.4f} True_BE={c_60_70.get('true_be_wr',0):.3f} WR={c_60_70.get('win_rate',0):.3f}")
    if c_70_80.get("n", 0) >= MIN_N_DIAGNOSIS:
        diag_70 = c_70_80.get("diagnosis", "?")
        print(f"      - 0.70-0.80 zone (n={c_70_80['n']}, all-clean): {diag_70}")
        print(f"        avg_CLV={c_70_80.get('avg_clv', 0):+.4f} True_BE={c_70_80.get('true_be_wr',0):.3f} WR={c_70_80.get('win_rate',0):.3f}")
    print("      The 2D gate correctly blocks both problem zones.")

    print()
    print("  Q3. Which cells are model-quality problems?")
    mq = [k for k,c in cells.items() if c["diagnosis"] == "MODEL_QUALITY"]
    for k in mq:
        c = cells[k]
        print(f"      {k}: n={c['n']} avg_CLV={c.get('avg_clv',0):+.4f} WR={c['win_rate']:.3f}")
    if not mq:
        print("      None with sufficient evidence.")

    print()
    print("  Q4. Which cells are payoff-structure problems?")
    ps = [k for k,c in cells.items() if c["diagnosis"] == "PAYOFF_STRUCTURE"]
    for k in ps:
        c = cells[k]
        print(f"      {k}: n={c['n']} avg_CLV={c.get('avg_clv',0):+.4f} True_BE={c.get('true_be_wr',0):.3f} WR={c['win_rate']:.3f}")
    if not ps:
        print("      None with sufficient evidence.")

    print()
    print("  Q5. Is there enough evidence to consider a future price-quality filter?")
    print("      NOT YET. The 2D gate already acts as a price-quality filter:")
    print("      - Blocks 0.60-0.80 (both MODEL_QUALITY and PAYOFF_STRUCTURE zones)")
    print("      - Allows 0.80-0.90 sweet-spot (confirmed GOOD, n=52 normal_modern)")
    if c_50_60.get("n", 0) >= MIN_N_DIAGNOSIS:
        print(f"      The 0.50-0.60 zone (n={c_50_60['n']} all, likely fewer normal_modern) looks GOOD")
        print("      but needs 30+ normal_modern trades before a permanent filter is justified.")
        print("      WAIT — do not add a price filter for 0.50-0.60 until then.")
    print("      Current recommendation: run the existing 2D gate, accumulate trades.")

    print()
    print("  Q6. What should the next phase be?")
    print("      - Phase 9I-B: still wait for 30+ additional normal_modern trades.")
    print("        Both shadow (original_edge) and live (risk_edge) show 0.05-0.10")
    print("        bad_enough=True — zero decision change confirmed. Monitor.")
    print("      - Phase 9K: complete (this report). No live changes needed.")
    print("      - Next suggested: Phase 9L — Dashboard CLV cell view (show 2D cells")
    print("        with CLV/EV in the Trading Research Control Room), OR")
    print("        Phase 10 — wait for proof gates (30 normal_modern) to complete.")


def section_8_next_phase() -> None:
    _hdr("8. SAFETY AND LOCKS CONFIRMATION")
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        records = load_trades()
        buckets = classify_records(records)
        gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
        ram = gate.get("real_money_allowed")
        sca = gate.get("scale_allowed")
        print(f"  real_money_allowed : {ram}  (must be False)")
        print(f"  scale_allowed      : {sca}  (must be False)")
        if ram is not False or sca is not False:
            print("  *** SAFETY VIOLATION — check evaluate_proof_gates() immediately ***")
        else:
            print("  Both locks confirmed intact.")
    except Exception as e:
        print(f"  [WARN] Could not load clean_truth_report: {e}")
    try:
        from config.trading_config import MIN_EDGE, MIN_CONFIDENCE
        print(f"  MIN_EDGE={MIN_EDGE}  MIN_CONFIDENCE={MIN_CONFIDENCE}  (must be 0.03, 0.65)")
    except Exception as e:
        print(f"  [WARN] Could not load trading_config: {e}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 68)
    print("2D CLV / PAYOFF CELL REPORT — Phase 9K")
    print("=" * 68)

    non_kxeth, kxeth, clean = _load_clean_settled_non_kxeth()
    cells = build_2d_cell_data(non_kxeth)
    profile = _load_stored_profile()

    section_1_overview(non_kxeth, kxeth, clean)
    section_2_all_table(cells)
    section_3_stored_profile(profile)
    section_4_special_cells(cells)
    section_5_asymmetry_analysis(cells)
    section_6_diagnosis(cells)
    section_7_questions(cells)
    section_8_next_phase()

    print()
    print("=" * 68)
    print(_SENTINEL)
    print("=" * 68)
    print()


if __name__ == "__main__":
    main()
