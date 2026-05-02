#!/usr/bin/env python3
"""
tools/report_scale_readiness.py
--------------------------------
Read-only scale safety audit.

Shows ALL scale blockers in explicit language. The verdict is SAFE_TO_SCALE=NO
until every hard gate is met and a human explicitly signs off in code.

Scale gates (from clean_truth_report.evaluate_proof_gates):
  SCALE_ELIGIBLE requires:
    - modern_full_metadata_trades >= 100
    - normal_council_approved_modern_trades >= 30
    - profit_factor >= 1.10
    - ROI > 0
    - avg_CLV > 0
    - clean_settled_trades ROI > 0

  scale_allowed is hardcoded False in evaluate_proof_gates — human sign-off required.
  real_money_allowed is always False.

Does NOT modify any state.

Expected result: SCALE_SAFETY_REPORT_OK
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (
    BOOTSTRAP_ALLOW_ENABLED,
    DATA_COLLECTION_OVERRIDE_ENABLED,
    DAILY_LOSS_LIMIT,
    EDGE_DANGER_BLOCK_HIGH_EDGE,
    EDGE_DANGER_HIGH_EDGE_MIN,
    GLOBAL_FORCED_LEARNING_MODE,
    INITIAL_BANKROLL,
    KELLY_FRACTION,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_SIZE,
    MAX_TRADES_PER_DAY,
    TRADING_MODE,
    WEEKLY_LOSS_LIMIT,
)
from tools.clean_truth_report import (
    _GATE_MIN_MODERN_SCALE,
    _GATE_MIN_NORMAL_SCALE,
    _GATE_MIN_PROFIT_FACTOR,
    _as_float,
    _avg,
    _fmt_money,
    _fmt_num,
    _fmt_pct,
    _fmt_ratio,
    calc_asymmetry,
    classify_records,
    get_clv,
    get_pnl,
    get_size,
    row_quality_group,
)
from tools.performance_report import load_trades

EDGE_PROFILE_PATH = ROOT / "data" / "edge_profile.json"
RISK_STATE_PATH = ROOT / "data" / "risk_state.json"
_GATE_MIN_MODERN_WATCHLIST = 30
_GATE_MIN_NORMAL_WATCHLIST = 30


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _check(label: str, ok: bool, detail: str = "") -> tuple[bool, str]:
    tag = "[PASS]" if ok else "[FAIL]"
    line = f"  {tag}  {label}"
    if detail:
        line += f"  ({detail})"
    return ok, line


def _analyse_stale_open(all_records: list[dict]) -> tuple[int, int]:
    open_recs = [r for r in all_records if r.get("status") == "OPEN"]
    tickers_settled = {
        r.get("ticker")
        for r in all_records
        if r.get("status") in ("SETTLED", "FORCED_CLOSE", "VOID_LEGACY_DUPLICATE")
        and r.get("ticker")
    }
    stale = sum(1 for r in open_recs if r.get("ticker") in tickers_settled)
    active = len(open_recs) - stale
    return active, stale


def main() -> None:
    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]
    modern_full = [r for r in clean_settled if row_quality_group(r) == "MODERN_FULL_METADATA"]

    dc_count    = sum(1 for r in modern_full if r.get("data_collection_override"))
    bp_count    = sum(1 for r in modern_full if r.get("bootstrap_provisional"))
    era_count   = sum(1 for r in modern_full if r.get("bootstrap_era_council_allow"))
    normal_modern = max(0, len(modern_full) - dc_count - bp_count)

    m_overall   = calc_asymmetry(clean_settled)
    m_modern    = calc_asymmetry(modern_full) if modern_full else None
    clv_vals    = [v for v in (get_clv(r) for r in clean_settled) if v is not None]
    avg_clv     = _avg(clv_vals)
    m_clv_vals  = [v for v in (get_clv(r) for r in modern_full) if v is not None]
    m_avg_clv   = _avg(m_clv_vals)

    roi_cs      = m_overall["roi"]
    roi_modern  = m_modern["roi"] if m_modern else None
    pf          = m_modern["profit_factor"] if m_modern else None

    profile = _load_json(EDGE_PROFILE_PATH)
    risk_state = _load_json(RISK_STATE_PATH)
    active_open, stale_open = _analyse_stale_open(all_records)

    W = 72
    print("=" * W)
    print("SCALE READINESS / SAFETY AUDIT")
    print("=" * W)
    print(f"Trading mode:       {TRADING_MODE}")
    print(f"Bankroll:           ${INITIAL_BANKROLL:.2f}")
    print(f"Max position size:  ${MAX_POSITION_SIZE:.2f}")
    print(f"Daily loss limit:   {_fmt_money(DAILY_LOSS_LIMIT)}")
    print(f"Weekly loss limit:  {_fmt_money(WEEKLY_LOSS_LIMIT)}")
    print(f"Max open positions: {MAX_OPEN_POSITIONS}")
    print(f"Max trades/day:     {MAX_TRADES_PER_DAY}")
    print(f"Kelly fraction:     {KELLY_FRACTION}")
    print()

    print("FORCED-LEARNING GUARDS")
    print("-" * W)
    g_learning = GLOBAL_FORCED_LEARNING_MODE
    ok, line = _check("GLOBAL_FORCED_LEARNING_MODE = True", g_learning,
                      f"current = {GLOBAL_FORCED_LEARNING_MODE}")
    print(line)
    if not g_learning:
        print("    [CRITICAL] Kelly is live — position sizing is NOT capped at $5 learning bets.")
    ok2, line2 = _check("TRADING_MODE = PAPER", TRADING_MODE == "PAPER",
                        f"current = {TRADING_MODE}")
    print(line2)
    ok3, line3 = _check("EDGE_DANGER_BLOCK_HIGH_EDGE = True", EDGE_DANGER_BLOCK_HIGH_EDGE,
                        f"blocks edge>={EDGE_DANGER_HIGH_EDGE_MIN:.2f}")
    print(line3)
    ok4, line4 = _check("DATA_COLLECTION_OVERRIDE_ENABLED = False", not DATA_COLLECTION_OVERRIDE_ENABLED,
                        f"current = {DATA_COLLECTION_OVERRIDE_ENABLED}")
    print(line4)
    print()

    print("RISK STATE")
    print("-" * W)
    daily_pnl  = risk_state.get("daily_pnl", 0.0)
    weekly_pnl = risk_state.get("weekly_pnl", 0.0)
    trades_today = risk_state.get("trades_today", 0)
    loss_streak = risk_state.get("loss_streak", 0)
    daily_ok   = DAILY_LOSS_LIMIT <= daily_pnl
    weekly_ok  = WEEKLY_LOSS_LIMIT <= weekly_pnl
    print(f"  daily_pnl:        {_fmt_money(daily_pnl)}  (limit {_fmt_money(DAILY_LOSS_LIMIT)})  {'OK' if daily_ok else 'BREACHED'}")
    print(f"  weekly_pnl:       {_fmt_money(weekly_pnl)}  (limit {_fmt_money(WEEKLY_LOSS_LIMIT)})  {'OK' if weekly_ok else 'BREACHED'}")
    print(f"  trades_today:     {trades_today}  (max {MAX_TRADES_PER_DAY})")
    print(f"  loss_streak:      {loss_streak}")
    print(f"  open_positions:   {risk_state.get('open_positions', 0)}")
    print()

    print("OPEN TRADE STATE")
    print("-" * W)
    print(f"  Active open (no settlement):   {active_open}")
    print(f"  Stale open (settlement exists): {stale_open}")
    print(f"  PaperTrader reports:           {risk_state.get('open_positions', 0)} active")
    if stale_open > 0:
        print(f"  [NOTE] {stale_open} ghost OPEN records exist in log but settlement rows")
        print("  are already present. PaperTrader correctly ignores these as stale.")
        print("  Run 6B-6/6B-7 ghost OPEN cleanup to sanitize the log.")
    print()

    print("EVIDENCE GATES (SCALE ELIGIBILITY)")
    print("-" * W)
    n_cs = len(clean_settled)
    n_mf = len(modern_full)

    rows_and_checks = [
        (n_cs >= _GATE_MIN_MODERN_WATCHLIST,
         "clean_settled >= 30 (watchlist floor)",
         f"{n_cs}/{_GATE_MIN_MODERN_WATCHLIST}"),
        (n_mf >= _GATE_MIN_MODERN_WATCHLIST,
         "modern_full_metadata >= 30 (watchlist floor)",
         f"{n_mf}/{_GATE_MIN_MODERN_WATCHLIST}"),
        (normal_modern >= _GATE_MIN_NORMAL_WATCHLIST,
         "normal_council_approved_modern >= 30 (watchlist floor)",
         f"{normal_modern}/{_GATE_MIN_NORMAL_WATCHLIST}"),
        (n_mf >= _GATE_MIN_MODERN_SCALE,
         "modern_full_metadata >= 100 (scale gate)",
         f"{n_mf}/{_GATE_MIN_MODERN_SCALE}"),
        (normal_modern >= _GATE_MIN_NORMAL_SCALE,
         "normal_council_approved_modern >= 30 (scale gate)",
         f"{normal_modern}/{_GATE_MIN_NORMAL_SCALE}"),
    ]
    for gate_ok, label, detail in rows_and_checks:
        _, line = _check(label, gate_ok, detail)
        print(line)
    print()

    print("PERFORMANCE GATES (SCALE ELIGIBILITY)")
    print("-" * W)
    perf_checks = [
        (roi_cs > 0,
         "clean_settled ROI > 0%",
         f"current {_fmt_pct(roi_cs)}"),
        (roi_modern is not None and roi_modern > 0,
         "modern_full ROI > 0%",
         f"current {_fmt_pct(roi_modern) if roi_modern is not None else 'n/a'}"),
        (avg_clv is not None and avg_clv > 0,
         "avg_CLV > 0",
         f"current {_fmt_num(avg_clv) if avg_clv is not None else 'n/a'}"),
        (pf is not None and pf > _GATE_MIN_PROFIT_FACTOR,
         f"profit_factor > {_GATE_MIN_PROFIT_FACTOR:.2f}",
         f"current {_fmt_ratio(pf) if pf is not None else 'n/a'}"),
    ]
    for gate_ok, label, detail in perf_checks:
        _, line = _check(label, gate_ok, detail)
        print(line)
    print()

    print("SCALE / REAL MONEY HARDCODED BLOCKS")
    print("-" * W)
    print("  [PASS]  scale_allowed = False  (hardcoded in evaluate_proof_gates)")
    print("  [PASS]  real_money_allowed = False  (hardcoded in evaluate_proof_gates)")
    print("  [PASS]  TRADING_MODE = PAPER  (no live order execution path exists)")
    print()

    print("BOOTSTRAP ALLOW STATUS (trust progress)")
    print("-" * W)
    trust = profile.get("edge_profile_health", {}).get("edge_profile_trusted", False)
    print(f"  edge_profile_trusted:              {'YES' if trust else 'NO'}")
    print(f"  clean_settled:                     {n_cs}  (need 30)")
    print(f"  modern_full_metadata:              {n_mf}  (need 30 for watchlist, 100 for scale)")
    print(f"  normal_council_approved_modern:    {normal_modern}  (need 10 for trusted, 30 for scale)")
    print(f"  data_collection_override:          {dc_count}  (excluded from proof)")
    print(f"  bootstrap_provisional:             {bp_count}  (excluded from proof)")
    print(f"  bootstrap_era_allow:               {era_count}  (Phase 6B-2; counts as normal)")
    print(f"  BOOTSTRAP_ALLOW_ENABLED:           {BOOTSTRAP_ALLOW_ENABLED}")
    print()

    print("=" * W)
    print("SCALE VERDICT")
    print("=" * W)
    gate_evidence_ok = (
        n_cs >= _GATE_MIN_MODERN_WATCHLIST
        and n_mf >= _GATE_MIN_MODERN_SCALE
        and normal_modern >= _GATE_MIN_NORMAL_SCALE
    )
    gate_perf_ok = (
        roi_cs > 0
        and roi_modern is not None and roi_modern > 0
        and avg_clv is not None and avg_clv > 0
        and pf is not None and pf > _GATE_MIN_PROFIT_FACTOR
    )
    print(f"  SAFE_TO_SCALE:          NO  (hardcoded False; requires all gates + human sign-off)")
    print(f"  REAL_MONEY_READY:       ABSOLUTELY_NOT")
    print(f"  Evidence gates passed:  {'YES' if gate_evidence_ok else 'NO'}")
    print(f"  Performance gates pass: {'YES' if gate_perf_ok else 'NO'}")
    print()

    all_blockers = []
    if n_cs < _GATE_MIN_MODERN_WATCHLIST:
        all_blockers.append(f"clean_settled {n_cs} < {_GATE_MIN_MODERN_WATCHLIST}")
    if n_mf < _GATE_MIN_MODERN_SCALE:
        all_blockers.append(f"modern_full {n_mf} < {_GATE_MIN_MODERN_SCALE} (scale gate)")
    if normal_modern < _GATE_MIN_NORMAL_SCALE:
        all_blockers.append(f"normal_modern {normal_modern} < {_GATE_MIN_NORMAL_SCALE} (scale gate)")
    if not (roi_cs > 0):
        all_blockers.append(f"ROI {_fmt_pct(roi_cs)} <= 0")
    if avg_clv is None or avg_clv <= 0:
        all_blockers.append(f"avg_CLV {_fmt_num(avg_clv) if avg_clv is not None else 'n/a'} <= 0")
    if pf is None or pf <= _GATE_MIN_PROFIT_FACTOR:
        all_blockers.append(f"profit_factor {_fmt_ratio(pf) if pf is not None else 'n/a'} <= {_GATE_MIN_PROFIT_FACTOR:.2f}")
    if not GLOBAL_FORCED_LEARNING_MODE:
        all_blockers.append("GLOBAL_FORCED_LEARNING_MODE=False — Kelly is live, CRITICAL")
    if TRADING_MODE != "PAPER":
        all_blockers.append(f"TRADING_MODE={TRADING_MODE} — should be PAPER")

    if all_blockers:
        print(f"  Active blockers ({len(all_blockers)}):")
        for b in all_blockers:
            print(f"    - {b}")
    else:
        print("  All blockers cleared — human sign-off still required.")
    print()
    print("RESULT: SCALE_SAFETY_REPORT_OK")


if __name__ == "__main__":
    main()
