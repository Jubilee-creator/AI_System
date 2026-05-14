#!/usr/bin/env python3
"""
Phase 10E - BET_NO + Payoff-Asymmetry Research
Sentinel: BET_NO_ASYMMETRY_RESEARCH_OK

Read-only research report. It does not change scanner ranking, thresholds,
strategy, paper trading, risk rules, logs, or live-money state.
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
    spread_value,
    summarize_candidate_rows,
    summarize_settled_rows,
)

SENTINEL = "BET_NO_ASYMMETRY_RESEARCH_OK"
MIN_RESEARCH_SAMPLE = 30


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def _rank(row: dict[str, Any]) -> int | None:
    value = row.get("scan_non_pass_rank") if row.get("scan_non_pass_rank") is not None else row.get("opportunity_rank")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def _candidate_source(funnel_rows: list[dict[str, Any]], scanner_rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    if funnel_rows:
        return "execution_funnel", list(funnel_rows)
    return "scanner_non_pass_tail", [row for row in scanner_rows if action_of(row) != "PASS"]


def _side_rows(rows: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [row for row in rows if action_of(row) == action]


def _opened(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if bool(row.get("paper_trade_opened")) or final_reason(row) == "TRADE_OPENED")


def _rank_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ranks = [_rank(row) for row in rows]
    ranks = [rank for rank in ranks if rank is not None]
    return {
        "avg_rank": sum(ranks) / len(ranks) if ranks else None,
        "top3": sum(1 for rank in ranks if rank <= 3),
        "rank_gt_slots": sum(1 for rank in ranks if rank > 3),
        "ranked": len(ranks),
    }


def _scan_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("run_id") or "UNKNOWN_RUN"), str(row.get("scan_id") or "UNKNOWN_SCAN"))


def _scan_bet_no_timing(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_scan: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_scan[_scan_key(row)].append(row)

    scans_with_no = 0
    no_after_yes = 0
    no_after_three_yes = 0
    no_cap_full = 0
    no_after_slots_or_cap = 0
    first_no_ranks: list[int] = []
    yes_before_no_values: list[int] = []

    for scan_rows in by_scan.values():
        non_pass = sorted(
            [row for row in scan_rows if action_of(row) != "PASS" and _rank(row) is not None],
            key=lambda row: _rank(row) or 10**9,
        )
        no_rows = [row for row in non_pass if action_of(row) == "BET_NO"]
        if not no_rows:
            continue
        scans_with_no += 1
        first_no = no_rows[0]
        first_no_rank = _rank(first_no)
        if first_no_rank is not None:
            first_no_ranks.append(first_no_rank)
            yes_before = sum(1 for row in non_pass if action_of(row) == "BET_YES" and (_rank(row) or 0) < first_no_rank)
            yes_before_no_values.append(yes_before)
            if yes_before > 0:
                no_after_yes += 1
            if yes_before >= 3:
                no_after_three_yes += 1
        if first_no.get("cap_already_full") is True or final_reason(first_no) == "BLOCKED_MAX_OPEN_TRADES":
            no_cap_full += 1
        if (yes_before_no_values and yes_before_no_values[-1] >= 3) or first_no.get("cap_already_full") is True or final_reason(first_no) == "BLOCKED_MAX_OPEN_TRADES":
            no_after_slots_or_cap += 1

    return {
        "scans_with_bet_no": scans_with_no,
        "avg_first_no_rank": _avg([float(rank) for rank in first_no_ranks]),
        "avg_yes_before_first_no": _avg([float(value) for value in yes_before_no_values]),
        "first_no_after_any_yes_scans": no_after_yes,
        "first_no_after_three_yes_scans": no_after_three_yes,
        "first_no_cap_full_or_max_open_scans": no_cap_full,
        "first_no_after_slots_or_cap_scans": no_after_slots_or_cap,
    }


def _group_summaries(rows: list[dict[str, Any]], key_name: str) -> list[tuple[str, dict[str, Any]]]:
    if key_name == "blocker":
        key_fn = final_reason
    elif key_name == "family":
        key_fn = lambda row: market_family(row.get("ticker"))
    elif key_name == "entry":
        key_fn = lambda row: entry_bucket(side_entry_price(row))
    elif key_name == "rr":
        key_fn = lambda row: reward_risk_bucket(reward_risk(side_entry_price(row)))
    elif key_name == "edge":
        key_fn = lambda row: edge_bucket(edge_value(row))
    else:
        raise ValueError(key_name)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    return sorted(
        ((key, summarize_candidate_rows(group)) for key, group in grouped.items()),
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

    bet_no = _side_rows(candidates, "BET_NO")
    bet_yes = _side_rows(candidates, "BET_YES")
    arb = _side_rows(candidates, "ARB")
    settled_no = _side_rows(fresh_rows, "BET_NO")
    settled_yes = _side_rows(fresh_rows, "BET_YES")

    no_summary = summarize_candidate_rows(bet_no)
    yes_summary = summarize_candidate_rows(bet_yes)
    no_rank = _rank_stats(bet_no)
    yes_rank = _rank_stats(bet_yes)
    timing = _scan_bet_no_timing(candidates)

    no_opened = _opened(bet_no)
    yes_opened = _opened(bet_yes)
    no_frequency = len(bet_no) / len(candidates) if candidates else None
    yes_frequency = len(bet_yes) / len(candidates) if candidates else None

    if not bet_no:
        bet_no_verdict = "NO_BET_NO_EVIDENCE"
    elif no_opened == 0:
        bet_no_verdict = "UNDER_TESTED_BLOCKED_BEFORE_OPEN"
    elif len(settled_no) < MIN_RESEARCH_SAMPLE:
        bet_no_verdict = "TOO_SMALL_FOR_OUTCOME_CLAIM"
    else:
        no_settled = summarize_settled_rows(settled_no)
        bet_no_verdict = "ECONOMICALLY_STRONG" if (no_settled.get("roi") or 0.0) > 0 else "ECONOMICALLY_WEAK"

    if not settled_no:
        historical_no_verdict = "NO_SETTLED_BET_NO_PROOF"
    elif len(settled_no) < MIN_RESEARCH_SAMPLE:
        historical_no_verdict = "BET_NO_SETTLED_SAMPLE_TOO_SMALL"
    else:
        historical_no_verdict = "BET_NO_SETTLED_SAMPLE_AVAILABLE"

    no_geometry_better = (
        no_summary["n"] > 0
        and yes_summary["n"] > 0
        and (no_summary["avg_reward_risk"] or -1.0) > (yes_summary["avg_reward_risk"] or 10**9)
        and (no_summary["avg_entry"] or 10**9) < (yes_summary["avg_entry"] or -1.0)
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
            "bet_no": len(bet_no),
            "bet_yes": len(bet_yes),
            "arb": len(arb),
            "bet_no_opened": no_opened,
            "bet_yes_opened": yes_opened,
            "settled_bet_no": len(settled_no),
            "settled_bet_yes": len(settled_yes),
        },
        "frequency": {
            "bet_no": no_frequency,
            "bet_yes": yes_frequency,
            "underrepresented": bool(no_frequency is not None and yes_frequency is not None and no_frequency < yes_frequency * 0.50),
        },
        "bet_no": {
            "summary": no_summary,
            "rank": no_rank,
            "blockers": dict(Counter(final_reason(row) for row in bet_no).most_common()),
            "by_entry": _group_summaries(bet_no, "entry"),
            "by_reward_risk": _group_summaries(bet_no, "rr"),
            "by_edge": _group_summaries(bet_no, "edge"),
            "by_family": _group_summaries(bet_no, "family")[:20],
            "settled_summary": summarize_settled_rows(settled_no),
            "verdict": bet_no_verdict,
            "historical_verdict": historical_no_verdict,
        },
        "bet_yes": {
            "summary": yes_summary,
            "rank": yes_rank,
            "blockers": dict(Counter(final_reason(row) for row in bet_yes).most_common()),
            "settled_summary": summarize_settled_rows(settled_yes),
        },
        "arb": {
            "rows": len(arb),
            "opened": _opened(arb),
            "blockers": dict(Counter(final_reason(row) for row in arb).most_common()),
        },
        "timing": timing,
        "comparison": {
            "bet_no_geometry_better_ex_ante": no_geometry_better,
            "avg_reward_risk_gap_no_minus_yes": (
                (no_summary["avg_reward_risk"] or 0.0) - (yes_summary["avg_reward_risk"] or 0.0)
                if no_summary["avg_reward_risk"] is not None and yes_summary["avg_reward_risk"] is not None
                else None
            ),
            "avg_entry_gap_no_minus_yes": (
                (no_summary["avg_entry"] or 0.0) - (yes_summary["avg_entry"] or 0.0)
                if no_summary["avg_entry"] is not None and yes_summary["avg_entry"] is not None
                else None
            ),
            "avg_model_margin_gap_no_minus_yes": (
                (no_summary["avg_model_margin"] or 0.0) - (yes_summary["avg_model_margin"] or 0.0)
                if no_summary["avg_model_margin"] is not None and yes_summary["avg_model_margin"] is not None
                else None
            ),
        },
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
            "is_bet_no_economically_stronger": "UNPROVEN_NO_SETTLED_SAMPLE" if not settled_no else bet_no_verdict,
            "is_bet_no_too_rare": bool(no_frequency is not None and no_frequency < 0.10),
            "is_bet_no_blocked_correctly": "LIKELY_YES_BUT_UNPROVEN" if no_opened == 0 else "NEEDS_OUTCOME_AUDIT",
            "does_bet_no_deserve_research": bool(bet_no and no_opened == 0),
            "live_patch_allowed": False,
        },
    }


def _print_side(label: str, stats: dict[str, Any], rank: dict[str, Any]) -> None:
    print(f"{label:<10} n={stats['n']:,} opened={stats['opened']:,} blocked={stats['blocked']:,} "
          f"avg_ep={_fmt_num(stats['avg_entry'])} rr={_fmt_num(stats['avg_reward_risk'])} "
          f"m-be={_fmt_num(stats['avg_model_margin'])} edge={_fmt_num(stats['avg_edge'])} "
          f"avg_rank={_fmt_num(rank['avg_rank'], 2)} top3={rank['top3']:,}")


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


def print_report(report: dict[str, Any]) -> None:
    counts = report["counts"]
    print("=" * 92)
    print("BET_NO + PAYOFF-ASYMMETRY RESEARCH")
    print("=" * 92)
    print("Read-only: no thresholds, strategy, risk, execution, logs, or live-money state are modified.")
    print(f"candidate source:      {report['source']}")
    print(f"scanner tail-limited:  {report['scanner_tail_limited']}")
    print(f"candidate rows:        {_fmt_int(counts['candidates'])}")
    print(f"fresh clean settled:   {_fmt_int(counts['fresh_clean_settled'])}")
    print()
    print("SIDE COMPARISON")
    print("---------------")
    _print_side("BET_NO", report["bet_no"]["summary"], report["bet_no"]["rank"])
    _print_side("BET_YES", report["bet_yes"]["summary"], report["bet_yes"]["rank"])
    print(f"BET_NO frequency:      {_fmt_pct(report['frequency']['bet_no'])}")
    print(f"BET_YES frequency:     {_fmt_pct(report['frequency']['bet_yes'])}")
    print(f"BET_NO underrepresented: {report['frequency']['underrepresented']}")
    print(f"reward/risk gap NO-YES: {_fmt_num(report['comparison']['avg_reward_risk_gap_no_minus_yes'])}")
    print(f"entry gap NO-YES:       {_fmt_num(report['comparison']['avg_entry_gap_no_minus_yes'])}")
    print(f"model margin gap NO-YES:{_fmt_num(report['comparison']['avg_model_margin_gap_no_minus_yes'])}")

    print()
    print("BET_NO BLOCKERS")
    print("---------------")
    for key, value in report["bet_no"]["blockers"].items():
        print(f"{key:<32} {value:>8,}")

    print()
    print("BET_NO TIMING / SLOT PRESSURE")
    print("-----------------------------")
    for key, value in report["timing"].items():
        print(f"{key:<36} {_fmt_num(value, 2) if isinstance(value, float) else value}")

    _print_candidate_table("BET_NO BY ENTRY PRICE", report["bet_no"]["by_entry"])
    _print_candidate_table("BET_NO BY REWARD/RISK", report["bet_no"]["by_reward_risk"])
    _print_candidate_table("BET_NO BY EDGE", report["bet_no"]["by_edge"])
    _print_candidate_table("BET_NO BY MARKET FAMILY", report["bet_no"]["by_family"])

    print()
    print("HISTORICAL OUTCOME TRUTH")
    print("------------------------")
    no_settled = report["bet_no"]["settled_summary"]
    yes_settled = report["bet_yes"]["settled_summary"]
    print(f"settled BET_NO rows:  {counts['settled_bet_no']:,} verdict={report['bet_no']['historical_verdict']}")
    print(f"settled BET_YES rows: {counts['settled_bet_yes']:,} ROI={_fmt_pct(yes_settled.get('roi'))} PF={_fmt_num(yes_settled.get('profit_factor'))}")
    print(f"BET_NO outcome ROI:   {_fmt_pct(no_settled.get('roi'))} PF={_fmt_num(no_settled.get('profit_factor'))}")

    print()
    print("ARB TRUTH")
    print("---------")
    print(f"ARB rows={report['arb']['rows']:,} opened={report['arb']['opened']:,} blockers={report['arb']['blockers']}")

    print()
    print("VERDICT")
    print("-------")
    for key, value in report["verdict"].items():
        print(f"{key:<36} {value}")
    print("Exact interpretation: BET_NO has better-looking payoff asymmetry in candidate logs, "
          "but no settled BET_NO proof exists, so it is research evidence only.")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> int:
    print_report(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
