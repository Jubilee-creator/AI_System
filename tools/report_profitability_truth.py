#!/usr/bin/env python3
"""
tools/report_profitability_truth.py
------------------------------------
Read-only deep profitability audit.

PROOF STANDARD (strict):
  PROVEN_PROFITABLE requires ALL of:
    - n >= 30 clean settled trades
    - ROI > 0
    - avg CLV > 0
    - profit_factor > 1.10
    - modern_full sample size >= 30

  WATCHLIST: gates met but performance not yet positive.
  NOT_PROVEN: sample too small.
  LOSING: negative expectancy confirmed.

Breakdowns:
  - Market family (ticker prefix: KXBTCD, KXBTC15M, KXETHD, KXETH15M, KXXRP15M, etc.)
  - Confidence bucket
  - Edge bucket
  - Action side (BET_YES / BET_NO)
  - Entry price bucket
  - Strategy
  - Row quality (MODERN_FULL_METADATA / LEGACY_EDGE_ONLY / …)

CLV and payoff asymmetry analysis shown for each breakout.

Does NOT modify any state.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import (
    build_terminal_key_sets,
    classify_settled_records,
    get_clv,
    get_pnl,
    get_size,
    load_trades,
)
from tools.clean_truth_report import (
    CALIBRATION_BUCKET_ORDER,
    EDGE_ORDER,
    ENTRY_PRICE_ORDER,
    HORIZON_ORDER,
    ROW_QUALITY_ORDER,
    _as_float,
    _avg,
    _fmt_money,
    _fmt_num,
    _fmt_pct,
    _fmt_ratio,
    calc_asymmetry,
    calibration_bucket,
    classify_open_records,
    classify_records,
    edge_bucket,
    entry_price_bucket,
    market_horizon,
    row_quality_group,
    strategy_key,
)

_PROOF_MIN_N        = 30
_PROOF_MIN_MODERN_N = 30
_PROOF_MIN_ROI      = 0.0
_PROOF_MIN_CLV      = 0.0
_PROOF_MIN_PF       = 1.10


def _ticker_family(rec: dict) -> str:
    ticker = str(rec.get("ticker") or "").upper()
    if ticker.startswith("KXBTCD"):
        return "KXBTCD (BTC daily)"
    if "BTC15M" in ticker or "KXBTC15M" in ticker:
        return "KXBTC15M (BTC 15-min)"
    if ticker.startswith("KXETHD"):
        return "KXETHD (ETH daily)"
    if "ETH15M" in ticker or "KXETH15M" in ticker:
        return "KXETH15M (ETH 15-min)"
    if ticker.startswith("KXXRP") or "XRP" in ticker:
        return "KXXRP (XRP)"
    if "SOL" in ticker:
        return "SOL"
    if "DOGE" in ticker:
        return "DOGE"
    return "OTHER"


def _action_side(rec: dict) -> str:
    return str(rec.get("action") or "UNKNOWN").upper()


def _compute_clv_stats(rows: list[dict]) -> tuple[float | None, int]:
    vals = [v for v in (get_clv(r) for r in rows) if v is not None]
    return (_avg(vals), len(vals))


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _row(label: str, rows: list[dict]) -> None:
    m = calc_asymmetry(rows)
    avg_clv, clv_n = _compute_clv_stats(rows)
    wins, losses = m["wins"], m["losses"]
    wl = wins + losses
    wr = wins / wl if wl else 0.0
    pf_str = _fmt_ratio(m["profit_factor"])
    clv_str = _fmt_num(avg_clv) if avg_clv is not None else "  n/a  "
    size_note = "  [tiny]" if 0 < m["count"] < 5 else ""
    print(
        f"  {label:<38} n={m['count']:>3}  W={wins:>3} L={losses:>3}  "
        f"WR={_fmt_pct(wr):>6}  PnL={_fmt_money(m['total_pnl']):>9}  "
        f"ROI={_fmt_pct(m['roi']):>8}  "
        f"avgW={_fmt_money(m['avg_win']) if m['avg_win'] is not None else 'n/a':>8}  "
        f"avgL={_fmt_money(m['avg_loss']) if m['avg_loss'] is not None else 'n/a':>8}  "
        f"PF={pf_str:>4}  "
        f"CLV={clv_str}"
        f"{size_note}"
    )


def _group_rows(rows: list[dict], key_func, order: list[str] | None = None) -> None:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[key_func(r)].append(r)
    keys = order or sorted(groups)
    for k in keys:
        g = groups.get(k)
        if g:
            _row(k, g)


def _verdict(n: int, modern_n: int, roi: float, avg_clv: float | None, pf: float | None) -> str:
    if n < _PROOF_MIN_N or modern_n < _PROOF_MIN_MODERN_N:
        return "NOT_PROVEN"
    if roi > _PROOF_MIN_ROI and (avg_clv or 0.0) > _PROOF_MIN_CLV and (pf or 0.0) > _PROOF_MIN_PF:
        return "PROVEN_PROFITABLE"
    if roi < 0 or (avg_clv is not None and avg_clv < 0):
        return "LOSING"
    return "WATCHLIST"


def main() -> None:
    all_records = load_trades()
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted = classify_settled_records(
        all_records, settled_keys, forced_close_keys, void_keys
    )

    modern_full = [r for r in clean_settled if row_quality_group(r) == "MODERN_FULL_METADATA"]

    m_overall = calc_asymmetry(clean_settled)
    avg_clv, clv_n = _compute_clv_stats(clean_settled)

    W = 72
    print("=" * W)
    print("PROFITABILITY TRUTH REPORT")
    print("=" * W)
    print(f"Source:            {ROOT / 'logs' / 'paper_trades.jsonl'}")
    print(f"Clean settled:     {len(clean_settled)}")
    print(f"Conflicted excl:   {len(conflicted)}")
    print(f"Modern full-meta:  {len(modern_full)}")
    print()

    print("PROOF STANDARD")
    print("-" * W)
    print(f"  Minimum clean settled:       {_PROOF_MIN_N}  (current: {len(clean_settled)})")
    print(f"  Minimum modern full-meta:    {_PROOF_MIN_MODERN_N}  (current: {len(modern_full)})")
    print(f"  Minimum ROI:                 > {_PROOF_MIN_ROI*100:.0f}%")
    print(f"  Minimum avg CLV:             > {_PROOF_MIN_CLV:.2f}")
    print(f"  Minimum profit factor:       > {_PROOF_MIN_PF:.2f}")
    print()

    print("OVERALL CLEAN SETTLED")
    print("-" * W)
    _row("OVERALL", clean_settled)
    print()
    wins_list = [get_pnl(r) for r in clean_settled if get_pnl(r) > 0]
    losses_list = [get_pnl(r) for r in clean_settled if get_pnl(r) < 0]
    avg_win = sum(wins_list) / len(wins_list) if wins_list else None
    avg_loss = sum(losses_list) / len(losses_list) if losses_list else None
    print(f"  avg_win:       {_fmt_money(avg_win) if avg_win is not None else 'n/a'}")
    print(f"  avg_loss:      {_fmt_money(avg_loss) if avg_loss is not None else 'n/a'}")
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        payoff = avg_win / abs(avg_loss)
        print(f"  payoff_ratio:  {payoff:.3f}  (avg_win / |avg_loss|)")
        be_wr = abs(avg_loss) / (avg_win + abs(avg_loss))
        actual_wr = m_overall["win_rate"]
        print(f"  breakeven_WR:  {_fmt_pct(be_wr)}")
        print(f"  actual_WR:     {_fmt_pct(actual_wr)}")
        print(f"  WR – BE_WR:    {_fmt_pct(actual_wr - be_wr)}  "
              f"({'POSITIVE' if actual_wr >= be_wr else 'NEGATIVE'})")
    print(f"  avg_CLV:       {_fmt_num(avg_clv) if avg_clv is not None else 'n/a'}  (n={clv_n})")
    pf = m_overall["profit_factor"]
    print(f"  profit_factor: {_fmt_ratio(pf) if pf else 'n/a'}")
    roi = m_overall["roi"]
    print(f"  ROI:           {_fmt_pct(roi)}")
    print()

    _section("BY MARKET FAMILY (ticker prefix)")
    _group_rows(clean_settled, _ticker_family)

    _section("BY MARKET HORIZON")
    _group_rows(clean_settled, market_horizon, HORIZON_ORDER)

    _section("BY ROW QUALITY (evidence generation era)")
    _group_rows(clean_settled, row_quality_group, ROW_QUALITY_ORDER)

    _section("BY CONFIDENCE BUCKET")
    _group_rows(clean_settled, calibration_bucket, CALIBRATION_BUCKET_ORDER)

    _section("BY EDGE BUCKET")
    _group_rows(clean_settled, edge_bucket, EDGE_ORDER)

    _section("BY ENTRY PRICE BUCKET")
    _group_rows(clean_settled, entry_price_bucket, ENTRY_PRICE_ORDER)

    _section("BY ACTION SIDE")
    _group_rows(clean_settled, _action_side)

    _section("BY STRATEGY")
    _group_rows(clean_settled, strategy_key)

    _section("MODERN FULL-METADATA ONLY")
    if not modern_full:
        print("  (no modern full-metadata rows yet)")
    else:
        _row("MODERN_FULL_METADATA", modern_full)
        m_clv, m_clv_n = _compute_clv_stats(modern_full)
        m_m = calc_asymmetry(modern_full)
        dc_n = sum(1 for r in modern_full if r.get("data_collection_override"))
        bp_n = sum(1 for r in modern_full if r.get("bootstrap_provisional"))
        era_n = sum(1 for r in modern_full if r.get("bootstrap_era_council_allow"))
        normal_n = max(0, len(modern_full) - dc_n - bp_n)
        print()
        print(f"  Split:")
        print(f"    dc_override:       {dc_n}  (excluded from normal proof)")
        print(f"    bootstrap_prov:    {bp_n}  (excluded from normal proof)")
        print(f"    era_allow:         {era_n}  (Phase 6B-2; counts as normal proof)")
        print(f"    normal_modern:     {normal_n}")
        print()
        _section("MODERN FULL-META BY CONFIDENCE BUCKET")
        _group_rows(modern_full, calibration_bucket, CALIBRATION_BUCKET_ORDER)
        _section("MODERN FULL-META BY EDGE BUCKET")
        _group_rows(modern_full, edge_bucket, EDGE_ORDER)
        _section("MODERN FULL-META BY ENTRY PRICE")
        _group_rows(modern_full, entry_price_bucket, ENTRY_PRICE_ORDER)

    print()
    print("=" * W)
    print("PROFITABILITY VERDICT")
    print("=" * W)

    m_pf = m_overall.get("profit_factor")
    verdict = _verdict(len(clean_settled), len(modern_full), roi, avg_clv, m_pf)

    missing = []
    if len(clean_settled) < _PROOF_MIN_N:
        missing.append(f"clean_settled {len(clean_settled)}/{_PROOF_MIN_N}")
    if len(modern_full) < _PROOF_MIN_MODERN_N:
        missing.append(f"modern_full {len(modern_full)}/{_PROOF_MIN_MODERN_N}")
    if roi <= _PROOF_MIN_ROI:
        missing.append(f"ROI {_fmt_pct(roi)} (need > 0%)")
    if avg_clv is None or avg_clv <= _PROOF_MIN_CLV:
        missing.append(f"avg_CLV {_fmt_num(avg_clv) if avg_clv is not None else 'n/a'} (need > 0)")
    if m_pf is None or m_pf <= _PROOF_MIN_PF:
        missing.append(f"profit_factor {_fmt_ratio(m_pf) if m_pf else 'n/a'} (need > {_PROOF_MIN_PF:.2f})")

    print(f"VERDICT:  {verdict}")
    if verdict != "PROVEN_PROFITABLE":
        print()
        print("Failing gates:")
        for item in missing:
            print(f"  - {item}")

    if avg_clv is not None and avg_clv < 0:
        print()
        print("[CLV WARNING] avg_CLV is negative — model is systematically mispricing signals.")
        print("Signals are entered at prices where the closing line is unfavorable. This is")
        print("the primary driver of losses. Negative CLV persists regardless of sample size.")

    if m_pf is not None and m_pf < 1.0:
        print()
        print("[ASYMMETRY WARNING] profit_factor < 1.0 — losses exceed wins in dollar terms.")
        if avg_win is not None and avg_loss is not None:
            print(f"avg_win={_fmt_money(avg_win)}  avg_loss={_fmt_money(avg_loss)}  "
                  f"ratio={avg_win / abs(avg_loss):.3f}  (need >= 1.0 for break-even)")
        print("Even at 50% win rate this asymmetry produces a structural loss.")

    print()
    print("RESULT: PROFITABILITY_TRUTH_REPORT_OK")


if __name__ == "__main__":
    main()
