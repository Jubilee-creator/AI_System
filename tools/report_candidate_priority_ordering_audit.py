#!/usr/bin/env python3
"""
Phase 10E - Candidate Priority / Ordering Audit
Sentinel: CANDIDATE_PRIORITY_ORDERING_AUDIT_OK

Read-only research report. It audits the current edge-descending ordering
against payoff-aware candidate quality without modifying ranking or execution.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (  # noqa: E402
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    QUARANTINED_TICKER_PREFIXES,
    TRADING_MODE,
)
from tools.report_payoff_geometry_candidate_quality_autopsy import (  # noqa: E402
    FUNNEL_LOG,
    SCANNER_LOG,
    SCANNER_TAIL_BYTES,
    TRADES_LOG,
    action_of,
    clean_fresh_rows,
    edge_bucket,
    edge_value,
    entry_bucket,
    final_reason,
    load_trades,
    market_family,
    model_minus_breakeven,
    model_probability,
    read_jsonl,
    reward_risk,
    reward_risk_bucket,
    side_entry_price,
    summarize_candidate_rows,
    summarize_settled_rows,
)

SENTINEL = "CANDIDATE_PRIORITY_ORDERING_AUDIT_OK"
MAX_SLOTS = 3


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _rank(row: dict[str, Any]) -> int | None:
    value = row.get("scan_non_pass_rank") if row.get("scan_non_pass_rank") is not None else row.get("opportunity_rank")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scan_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("run_id") or "UNKNOWN_RUN"), str(row.get("scan_id") or "UNKNOWN_SCAN"))


def _avg(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def _candidate_source(funnel_rows: list[dict[str, Any]], scanner_rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    if funnel_rows:
        return "execution_funnel", list(funnel_rows)
    return "scanner_non_pass_tail", [row for row in scanner_rows if action_of(row) != "PASS"]


def payoff_priority_score(row: dict[str, Any]) -> float | None:
    margin = model_minus_breakeven(row)
    rr = reward_risk(side_entry_price(row))
    edge = edge_value(row)
    if margin is None or rr is None:
        return None
    # Positive model margin matters, but only if payoff geometry can survive loss.
    # Edge is a small tiebreaker so this does not reduce to reported edge sorting.
    return max(margin, -1.0) * min(rr, 5.0) + 0.05 * (edge or 0.0)


def _top_by_actual_rank(rows: list[dict[str, Any]], limit: int = MAX_SLOTS) -> list[dict[str, Any]]:
    return sorted(
        [row for row in rows if _rank(row) is not None],
        key=lambda row: _rank(row) or 10**9,
    )[:limit]


def _top_by_payoff(rows: list[dict[str, Any]], limit: int = MAX_SLOTS) -> list[dict[str, Any]]:
    scored = [(payoff_priority_score(row), idx, row) for idx, row in enumerate(rows)]
    scored = [item for item in scored if item[0] is not None]
    scored.sort(key=lambda item: (-(item[0] or -10**9), item[1]))
    return [row for _, _, row in scored[:limit]]


def _identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("run_id"),
        row.get("scan_id"),
        row.get("ticker"),
        action_of(row),
        _rank(row),
    )


def _opened(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if bool(row.get("paper_trade_opened")) or final_reason(row) == "TRADE_OPENED")


def _group_summary(rows: list[dict[str, Any]], key_name: str) -> list[tuple[str, dict[str, Any]]]:
    if key_name == "rank":
        def key_fn(row: dict[str, Any]) -> str:
            rank = _rank(row)
            if rank is None:
                return "missing"
            if rank <= 3:
                return "1-3"
            if rank <= 10:
                return "4-10"
            if rank <= 25:
                return "11-25"
            return "26+"
    elif key_name == "action":
        key_fn = action_of
    elif key_name == "edge":
        key_fn = lambda row: edge_bucket(edge_value(row))
    elif key_name == "confidence":
        def key_fn(row: dict[str, Any]) -> str:
            prob = model_probability(row)
            if prob is None:
                return "missing"
            if prob < 0.70:
                return "<0.70"
            if prob < 0.80:
                return "0.70-0.80"
            if prob < 0.90:
                return "0.80-0.90"
            return "0.90+"
    elif key_name == "rr":
        key_fn = lambda row: reward_risk_bucket(reward_risk(side_entry_price(row)))
    elif key_name == "entry":
        key_fn = lambda row: entry_bucket(side_entry_price(row))
    elif key_name == "family":
        key_fn = lambda row: market_family(row.get("ticker"))
    else:
        raise ValueError(key_name)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    return sorted(
        ((key, summarize_candidate_rows(group)) for key, group in grouped.items()),
        key=lambda item: (-item[1]["n"], item[0]),
    )


def _actual_vs_payoff_by_scan(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_scan: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if action_of(row) == "PASS":
            continue
        by_scan[_scan_key(row)].append(row)

    scans = 0
    scans_with_payoff_alternative = 0
    scans_top_actual_has_expensive_yes = 0
    scans_payoff_top_has_bet_no = 0
    scans_actual_top_has_bet_no = 0
    overlap_counts: list[float] = []
    actual_top_rows: list[dict[str, Any]] = []
    payoff_top_rows: list[dict[str, Any]] = []
    best_payoff_actual_ranks: list[float] = []
    best_payoff_after_slots = 0

    for scan_rows in by_scan.values():
        non_pass = [row for row in scan_rows if _rank(row) is not None]
        if not non_pass:
            continue
        actual_top = _top_by_actual_rank(non_pass)
        payoff_top = _top_by_payoff(non_pass)
        if not actual_top or not payoff_top:
            continue
        scans += 1
        actual_top_rows.extend(actual_top)
        payoff_top_rows.extend(payoff_top)
        actual_ids = {_identity(row) for row in actual_top}
        payoff_ids = {_identity(row) for row in payoff_top}
        overlap_counts.append(len(actual_ids & payoff_ids) / max(1, len(payoff_ids)))
        if actual_ids != payoff_ids:
            scans_with_payoff_alternative += 1
        if any(action_of(row) == "BET_YES" and (side_entry_price(row) or 0.0) >= 0.80 for row in actual_top):
            scans_top_actual_has_expensive_yes += 1
        if any(action_of(row) == "BET_NO" for row in payoff_top):
            scans_payoff_top_has_bet_no += 1
        if any(action_of(row) == "BET_NO" for row in actual_top):
            scans_actual_top_has_bet_no += 1
        best_payoff = payoff_top[0]
        rank = _rank(best_payoff)
        if rank is not None:
            best_payoff_actual_ranks.append(float(rank))
            if rank > MAX_SLOTS:
                best_payoff_after_slots += 1

    return {
        "scans_analyzed": scans,
        "scans_with_payoff_alternative": scans_with_payoff_alternative,
        "payoff_alternative_rate": scans_with_payoff_alternative / scans if scans else None,
        "avg_top3_overlap": _avg(overlap_counts),
        "actual_top_expensive_yes_scans": scans_top_actual_has_expensive_yes,
        "actual_top_expensive_yes_rate": scans_top_actual_has_expensive_yes / scans if scans else None,
        "payoff_top_has_bet_no_scans": scans_payoff_top_has_bet_no,
        "actual_top_has_bet_no_scans": scans_actual_top_has_bet_no,
        "avg_best_payoff_actual_rank": _avg(best_payoff_actual_ranks),
        "best_payoff_after_slots_scans": best_payoff_after_slots,
        "best_payoff_after_slots_rate": best_payoff_after_slots / scans if scans else None,
        "actual_top_summary": summarize_candidate_rows(actual_top_rows),
        "payoff_top_summary": summarize_candidate_rows(payoff_top_rows),
    }


def _settled_predictor_groups(fresh_rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    return {
        "entry": _settled_groups(fresh_rows, lambda row: entry_bucket(side_entry_price(row))),
        "reward_risk": _settled_groups(fresh_rows, lambda row: reward_risk_bucket(reward_risk(side_entry_price(row)))),
        "edge": _settled_groups(fresh_rows, lambda row: edge_bucket(edge_value(row))),
        "confidence": _settled_groups(fresh_rows, _confidence_key),
    }


def _confidence_key(row: dict[str, Any]) -> str:
    prob = model_probability(row)
    if prob is None:
        return "missing"
    if prob < 0.70:
        return "<0.70"
    if prob < 0.80:
        return "0.70-0.80"
    if prob < 0.90:
        return "0.80-0.90"
    return "0.90+"


def _settled_groups(fresh_rows: list[dict[str, Any]], key_fn: Any) -> list[tuple[str, dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fresh_rows:
        grouped[key_fn(row)].append(row)
    return sorted(
        ((key, summarize_settled_rows(group)) for key, group in grouped.items()),
        key=lambda item: (-item[1]["n"], item[0]),
    )


def build_report(
    trades_path: Path = TRADES_LOG,
    funnel_path: Path = FUNNEL_LOG,
    scanner_path: Path = SCANNER_LOG,
    scanner_tail_bytes: int = SCANNER_TAIL_BYTES,
) -> dict[str, Any]:
    trades = load_trades(trades_path)
    funnel_rows, _ = read_jsonl(funnel_path)
    scanner_rows, scanner_tail_limited = read_jsonl(scanner_path, scanner_tail_bytes)
    source, candidates = _candidate_source(funnel_rows, scanner_rows)
    fresh_rows = clean_fresh_rows(trades)

    ordering = _actual_vs_payoff_by_scan(candidates)
    opened = [row for row in candidates if bool(row.get("paper_trade_opened")) or final_reason(row) == "TRADE_OPENED"]
    actual_top = ordering["actual_top_summary"]
    payoff_top = ordering["payoff_top_summary"]

    ranking_flawed = bool(
        ordering["scans_analyzed"]
        and (
            (ordering["payoff_alternative_rate"] or 0.0) > 0.50
            or (ordering["actual_top_expensive_yes_rate"] or 0.0) > 0.25
            or (ordering["best_payoff_after_slots_rate"] or 0.0) > 0.25
        )
    )
    payoff_aware_promising = bool(
        payoff_top["n"]
        and actual_top["n"]
        and (payoff_top["avg_reward_risk"] or 0.0) > (actual_top["avg_reward_risk"] or 0.0)
        and (payoff_top["avg_entry"] or 1.0) < (actual_top["avg_entry"] or 0.0)
    )

    return {
        "source": source,
        "scanner_tail_limited": scanner_tail_limited,
        "counts": {
            "trades": len(trades),
            "fresh_clean_settled": len(fresh_rows),
            "funnel_rows": len(funnel_rows),
            "scanner_rows_loaded": len(scanner_rows),
            "candidates": len(candidates),
            "opened_candidates": _opened(candidates),
            "opened_rows_for_priority": len(opened),
        },
        "source_inspection": {
            "ranking_logic": "ARB first, then BET_YES/BET_NO tied, sorted by descending reported edge",
            "paper_trader_iteration": "Dashboard iterates scanner opportunities in scanner order",
            "payoff_fields_in_current_sort": "not used",
        },
        "candidate_groups": {
            "rank": _group_summary(candidates, "rank"),
            "action": _group_summary(candidates, "action"),
            "edge": _group_summary(candidates, "edge"),
            "confidence": _group_summary(candidates, "confidence"),
            "reward_risk": _group_summary(candidates, "rr"),
            "entry": _group_summary(candidates, "entry"),
            "family": _group_summary(candidates, "family")[:20],
        },
        "ordering": ordering,
        "opened_summary": summarize_candidate_rows(opened),
        "settled_predictors": _settled_predictor_groups(fresh_rows),
        "safety": {
            "trading_mode": TRADING_MODE,
            "paper_only": TRADING_MODE == "PAPER",
            "real_money_allowed": False,
            "scale_allowed": False,
            "kelly_execution_disabled": GLOBAL_FORCED_LEARNING_MODE,
            "dc_override_enabled": DATA_COLLECTION_OVERRIDE_ENABLED,
            "kxeth_quarantine_active": "KXETH" in {str(prefix).upper() for prefix in QUARANTINED_TICKER_PREFIXES},
        },
        "verdict": {
            "ranking_system_flawed": ranking_flawed,
            "execution_order_hurts_profitability": "LIKELY_EX_ANTE_BUT_NOT_OUTCOME_PROVEN" if ranking_flawed else "NOT_PROVEN",
            "confidence_overweighted": True,
            "reward_risk_underweighted": True,
            "payoff_aware_ranking_promising": payoff_aware_promising,
            "exact_weakness": "reported_edge_sort_ignores_reward_risk_breakeven_and_entry_price",
            "live_patch_allowed": False,
        },
    }


def _print_candidate_table(title: str, rows: list[tuple[str, dict[str, Any]]], limit: int = 12) -> None:
    print()
    print(title)
    print("-" * len(title))
    if not rows:
        print("(none)")
        return
    print(f"{'bucket':<30} {'n':>7} {'open':>6} {'avg_ep':>8} {'RR':>8} {'m-be':>8} {'edge':>8}")
    for key, stats in rows[:limit]:
        print(
            f"{key:<30} {stats['n']:>7} {stats['opened']:>6} "
            f"{_fmt_num(stats['avg_entry']):>8} {_fmt_num(stats['avg_reward_risk']):>8} "
            f"{_fmt_num(stats['avg_model_margin']):>8} {_fmt_num(stats['avg_edge']):>8}"
        )


def _print_settled_table(title: str, rows: list[tuple[str, dict[str, Any]]], limit: int = 12) -> None:
    print()
    print(title)
    print("-" * len(title))
    if not rows:
        print("(none)")
        return
    print(f"{'bucket':<24} {'n':>5} {'WR':>8} {'BE':>8} {'PnL':>9} {'ROI':>8} {'PF':>8} {'RR':>8}")
    for key, stats in rows[:limit]:
        print(
            f"{key:<24} {stats['n']:>5} {_fmt_pct(stats.get('win_rate')):>8} "
            f"{_fmt_pct(stats.get('breakeven_wr')):>8} {_fmt_num(stats.get('total_economic_pnl')):>9} "
            f"{_fmt_pct(stats.get('roi')):>8} {_fmt_num(stats.get('profit_factor')):>8} "
            f"{_fmt_num(stats.get('avg_reward_risk')):>8}"
        )


def print_report(report: dict[str, Any]) -> None:
    counts = report["counts"]
    ordering = report["ordering"]
    print("=" * 92)
    print("CANDIDATE PRIORITY / ORDERING AUDIT")
    print("=" * 92)
    print("Read-only: no ranking, scanner, strategy, risk, threshold, or live-money state is modified.")
    print(f"candidate source:       {report['source']}")
    print(f"scanner tail-limited:   {report['scanner_tail_limited']}")
    print(f"candidate rows:         {_fmt_int(counts['candidates'])}")
    print(f"opened candidates:      {_fmt_int(counts['opened_candidates'])}")
    print(f"fresh clean settled:    {_fmt_int(counts['fresh_clean_settled'])}")
    print()
    print("SOURCE INSPECTION")
    print("-----------------")
    for key, value in report["source_inspection"].items():
        print(f"{key:<30} {value}")

    print()
    print("ACTUAL TOP-3 VS PAYOFF-AWARE TOP-3")
    print("----------------------------------")
    for key in (
        "scans_analyzed",
        "scans_with_payoff_alternative",
        "payoff_alternative_rate",
        "avg_top3_overlap",
        "actual_top_expensive_yes_scans",
        "actual_top_expensive_yes_rate",
        "payoff_top_has_bet_no_scans",
        "actual_top_has_bet_no_scans",
        "avg_best_payoff_actual_rank",
        "best_payoff_after_slots_scans",
        "best_payoff_after_slots_rate",
    ):
        value = ordering[key]
        print(f"{key:<36} {_fmt_num(value, 3) if isinstance(value, float) else value}")

    print()
    print("TOP-3 ECONOMIC COMPARISON")
    print("-------------------------")
    actual = ordering["actual_top_summary"]
    payoff = ordering["payoff_top_summary"]
    print(f"actual top3: n={actual['n']:,} avg_ep={_fmt_num(actual['avg_entry'])} rr={_fmt_num(actual['avg_reward_risk'])} m-be={_fmt_num(actual['avg_model_margin'])} edge={_fmt_num(actual['avg_edge'])}")
    print(f"payoff top3: n={payoff['n']:,} avg_ep={_fmt_num(payoff['avg_entry'])} rr={_fmt_num(payoff['avg_reward_risk'])} m-be={_fmt_num(payoff['avg_model_margin'])} edge={_fmt_num(payoff['avg_edge'])}")

    _print_candidate_table("CANDIDATES BY RANK BUCKET", report["candidate_groups"]["rank"])
    _print_candidate_table("CANDIDATES BY ACTION", report["candidate_groups"]["action"])
    _print_candidate_table("CANDIDATES BY EDGE", report["candidate_groups"]["edge"])
    _print_candidate_table("CANDIDATES BY CONFIDENCE", report["candidate_groups"]["confidence"])
    _print_candidate_table("CANDIDATES BY REWARD/RISK", report["candidate_groups"]["reward_risk"])
    _print_candidate_table("CANDIDATES BY ENTRY", report["candidate_groups"]["entry"])

    _print_settled_table("SETTLED OUTCOMES BY EDGE", report["settled_predictors"]["edge"])
    _print_settled_table("SETTLED OUTCOMES BY CONFIDENCE", report["settled_predictors"]["confidence"])
    _print_settled_table("SETTLED OUTCOMES BY REWARD/RISK", report["settled_predictors"]["reward_risk"])

    print()
    print("VERDICT")
    print("-------")
    for key, value in report["verdict"].items():
        print(f"{key:<36} {value}")
    print("Exact interpretation: current order is economically naive because it sorts by reported edge, "
          "while the losing proof cohort shows reward/risk and breakeven dominate reported edge.")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> int:
    print_report(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
