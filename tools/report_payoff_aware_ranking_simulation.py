#!/usr/bin/env python3
"""
Phase 10F - Payoff-Aware Ranking Simulation
Sentinel: PAYOFF_AWARE_RANKING_SIMULATION_OK

Read-only shadow simulation. It does not modify scanner ordering, thresholds,
strategy, PaperTrader, risk rules, logs, or live-money state.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

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
    edge_value,
    final_reason,
    load_trades,
    model_minus_breakeven,
    model_probability,
    read_jsonl,
    reward_risk,
    side_entry_price,
    summarize_candidate_rows,
    summarize_settled_rows,
)

SENTINEL = "PAYOFF_AWARE_RANKING_SIMULATION_OK"
MAX_SLOTS = 3
EXPENSIVE_ENTRY = 0.80
TOXIC_ENTRY_LOW = 0.80
TOXIC_ENTRY_HIGH = 0.90
WEAK_REWARD_RISK = 0.25
EXTREME_MARGIN = 0.10
EXTREME_CONFIDENCE = 0.90
STRICT_WEAK_RR_MARGIN = 0.12
STRICT_WEAK_RR_CONFIDENCE = 0.92


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


def _scan_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("run_id") or "UNKNOWN_RUN"), str(row.get("scan_id") or "UNKNOWN_SCAN"))


def _candidate_source(funnel_rows: list[dict[str, Any]], scanner_rows: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    if funnel_rows:
        return "execution_funnel", list(funnel_rows)
    return "scanner_non_pass_tail", [row for row in scanner_rows if action_of(row) != "PASS"]


def _identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("run_id"),
        row.get("scan_id"),
        row.get("ticker"),
        action_of(row),
        _rank(row),
    )


def _opened(row: dict[str, Any]) -> bool:
    return bool(row.get("paper_trade_opened")) or final_reason(row) == "TRADE_OPENED"


def _current_sort_key(row: dict[str, Any]) -> tuple[int, int]:
    rank = _rank(row)
    return (rank if rank is not None else 10**9, 0)


def payoff_score(row: dict[str, Any]) -> float | None:
    """Payoff-aware score: margin and reward/risk dominate; edge is a tiebreaker."""
    margin = model_minus_breakeven(row)
    rr = reward_risk(side_entry_price(row))
    price = side_entry_price(row)
    edge = edge_value(row) or 0.0
    if margin is None or rr is None or price is None:
        return None
    score = margin * min(rr, 5.0)
    score += 0.03 * edge
    if price >= EXPENSIVE_ENTRY:
        score -= 0.25
    if rr < WEAK_REWARD_RISK:
        score -= 0.20
    if margin <= 0:
        score -= 0.50
    return score


def strict_payoff_allowed(row: dict[str, Any]) -> bool:
    price = side_entry_price(row)
    rr = reward_risk(price)
    margin = model_minus_breakeven(row)
    prob = model_probability(row)
    if price is None or rr is None or margin is None or prob is None:
        return False
    if margin <= 0:
        return False
    if price >= EXPENSIVE_ENTRY and not (margin >= EXTREME_MARGIN and prob >= EXTREME_CONFIDENCE):
        return False
    if rr < WEAK_REWARD_RISK and not (margin >= STRICT_WEAK_RR_MARGIN and prob >= STRICT_WEAK_RR_CONFIDENCE):
        return False
    return True


def _pick_current(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = [row for row in rows if action_of(row) != "PASS"]
    ranked.sort(key=_current_sort_key)
    return ranked[:limit]


def _pick_payoff(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    scored = [(payoff_score(row), _rank(row) or 10**9, idx, row) for idx, row in enumerate(rows) if action_of(row) != "PASS"]
    scored = [item for item in scored if item[0] is not None]
    scored.sort(key=lambda item: (-(item[0] or -10**9), item[1], item[2]))
    return [row for _, _, _, row in scored[:limit]]


def _pick_strict(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    allowed = [row for row in rows if action_of(row) != "PASS" and strict_payoff_allowed(row)]
    return _pick_payoff(allowed, limit)


def _is_expensive(row: dict[str, Any]) -> bool:
    price = side_entry_price(row)
    return price is not None and price >= EXPENSIVE_ENTRY


def _is_weak_rr(row: dict[str, Any]) -> bool:
    rr = reward_risk(side_entry_price(row))
    return rr is not None and rr < WEAK_REWARD_RISK


def _is_toxic_entry(row: dict[str, Any]) -> bool:
    price = side_entry_price(row)
    return price is not None and TOXIC_ENTRY_LOW <= price < TOXIC_ENTRY_HIGH


def _is_model_edge_bad_geometry(row: dict[str, Any]) -> bool:
    edge = edge_value(row)
    return bool(edge is not None and edge >= 0.05 and (_is_expensive(row) or _is_weak_rr(row) or (model_minus_breakeven(row) or 0.0) <= 0))


def _rate(count: int, total: int) -> float | None:
    return count / total if total else None


def _summarize_selection(rows: list[dict[str, Any]], scans_total: int, limit: int) -> dict[str, Any]:
    base = summarize_candidate_rows(rows)
    n = base["n"]
    action_mix = Counter(action_of(row) for row in rows)
    blocker_mix = Counter(final_reason(row) for row in rows)
    return {
        **base,
        "scans_total": scans_total,
        "target_picks": scans_total * limit,
        "starved_slots": max(0, scans_total * limit - n),
        "starved_slot_rate": _rate(max(0, scans_total * limit - n), scans_total * limit),
        "expensive_entry_rate": _rate(sum(1 for row in rows if _is_expensive(row)), n),
        "weak_reward_risk_rate": _rate(sum(1 for row in rows if _is_weak_rr(row)), n),
        "toxic_80_90_rate": _rate(sum(1 for row in rows if _is_toxic_entry(row)), n),
        "model_edge_bad_geometry_rate": _rate(sum(1 for row in rows if _is_model_edge_bad_geometry(row)), n),
        "actual_opened_overlap": sum(1 for row in rows if _opened(row)),
        "action_mix": dict(action_mix.most_common()),
        "final_blocker_mix": dict(blocker_mix.most_common(10)),
    }


def _trade_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    price = side_entry_price(row)
    return (
        str(row.get("ticker") or ""),
        action_of(row),
        round(price, 6) if price is not None else None,
    )


def _settled_match_summary(selection_rows: list[dict[str, Any]], fresh_rows: list[dict[str, Any]]) -> dict[str, Any]:
    selected_keys = {_trade_identity(row) for row in selection_rows if _opened(row)}
    matched = [row for row in fresh_rows if _trade_identity(row) in selected_keys]
    summary = summarize_settled_rows(matched)
    return {
        "matched_rows": len(matched),
        "summary": summary,
        "evidence_type": "settled_overlap_only" if matched else "candidate_quality_only",
    }


def _simulate_by_scan(
    candidates: list[dict[str, Any]],
    fresh_rows: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    by_scan: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        if action_of(row) == "PASS":
            continue
        by_scan[_scan_key(row)].append(row)

    current_rows: list[dict[str, Any]] = []
    payoff_rows: list[dict[str, Any]] = []
    strict_rows: list[dict[str, Any]] = []
    overlap_payoff = 0
    overlap_strict = 0
    scans_with_payoff_change = 0
    scans_with_strict_change = 0
    scans_with_strict_no_pick = 0

    scans = 0
    for rows in by_scan.values():
        if not rows:
            continue
        current = _pick_current(rows, limit)
        payoff = _pick_payoff(rows, limit)
        strict = _pick_strict(rows, limit)
        if not current:
            continue
        scans += 1
        current_rows.extend(current)
        payoff_rows.extend(payoff)
        strict_rows.extend(strict)
        current_ids = {_identity(row) for row in current}
        payoff_ids = {_identity(row) for row in payoff}
        strict_ids = {_identity(row) for row in strict}
        overlap_payoff += len(current_ids & payoff_ids)
        overlap_strict += len(current_ids & strict_ids)
        if current_ids != payoff_ids:
            scans_with_payoff_change += 1
        if current_ids != strict_ids:
            scans_with_strict_change += 1
        if not strict:
            scans_with_strict_no_pick += 1

    current_summary = _summarize_selection(current_rows, scans, limit)
    payoff_summary = _summarize_selection(payoff_rows, scans, limit)
    strict_summary = _summarize_selection(strict_rows, scans, limit)
    total_current_slots = max(1, len(current_rows))

    return {
        "limit": limit,
        "scans": scans,
        "current": current_summary,
        "payoff": payoff_summary,
        "strict": strict_summary,
        "overlap": {
            "current_vs_payoff_count": overlap_payoff,
            "current_vs_payoff_rate": overlap_payoff / total_current_slots,
            "current_vs_strict_count": overlap_strict,
            "current_vs_strict_rate": overlap_strict / total_current_slots,
            "scans_with_payoff_change": scans_with_payoff_change,
            "scans_with_payoff_change_rate": _rate(scans_with_payoff_change, scans),
            "scans_with_strict_change": scans_with_strict_change,
            "scans_with_strict_change_rate": _rate(scans_with_strict_change, scans),
            "scans_with_strict_no_pick": scans_with_strict_no_pick,
            "scans_with_strict_no_pick_rate": _rate(scans_with_strict_no_pick, scans),
        },
        "settled_matches": {
            "current": _settled_match_summary(current_rows, fresh_rows),
            "payoff": _settled_match_summary(payoff_rows, fresh_rows),
            "strict": _settled_match_summary(strict_rows, fresh_rows),
        },
    }


def _improves(lower_is_better_a: float | None, lower_is_better_b: float | None) -> bool | None:
    if lower_is_better_a is None or lower_is_better_b is None:
        return None
    return lower_is_better_b < lower_is_better_a


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

    top1 = _simulate_by_scan(candidates, fresh_rows, 1)
    top3 = _simulate_by_scan(candidates, fresh_rows, 3)

    live_patch_allowed = False
    evidence_type = "candidate_quality_only"
    for mode in ("payoff", "strict"):
        if top3["settled_matches"][mode]["matched_rows"]:
            evidence_type = "partial_settled_overlap_not_full_counterfactual"

    return {
        "source": source,
        "scanner_tail_limited": scanner_tail_limited,
        "counts": {
            "trades": len(trades),
            "fresh_clean_settled": len(fresh_rows),
            "funnel_rows": len(funnel_rows),
            "scanner_rows_loaded": len(scanner_rows),
            "candidate_rows": len(candidates),
        },
        "top1": top1,
        "top3": top3,
        "questions": {
            "expensive_entries_decrease_top3_payoff": _improves(top3["current"]["expensive_entry_rate"], top3["payoff"]["expensive_entry_rate"]),
            "expensive_entries_decrease_top3_strict": _improves(top3["current"]["expensive_entry_rate"], top3["strict"]["expensive_entry_rate"]),
            "reward_risk_improves_top3_payoff": (
                top3["payoff"]["avg_reward_risk"] > top3["current"]["avg_reward_risk"]
                if top3["payoff"]["avg_reward_risk"] is not None and top3["current"]["avg_reward_risk"] is not None
                else None
            ),
            "reward_risk_improves_top3_strict": (
                top3["strict"]["avg_reward_risk"] > top3["current"]["avg_reward_risk"]
                if top3["strict"]["avg_reward_risk"] is not None and top3["current"]["avg_reward_risk"] is not None
                else None
            ),
            "toxic_80_90_decrease_top3_payoff": _improves(top3["current"]["toxic_80_90_rate"], top3["payoff"]["toxic_80_90_rate"]),
            "toxic_80_90_decrease_top3_strict": _improves(top3["current"]["toxic_80_90_rate"], top3["strict"]["toxic_80_90_rate"]),
            "model_edge_bad_geometry_decrease_top3_payoff": _improves(top3["current"]["model_edge_bad_geometry_rate"], top3["payoff"]["model_edge_bad_geometry_rate"]),
            "model_edge_bad_geometry_decrease_top3_strict": _improves(top3["current"]["model_edge_bad_geometry_rate"], top3["strict"]["model_edge_bad_geometry_rate"]),
            "strict_no_trade_starvation_rate_top3": top3["strict"]["starved_slot_rate"],
            "evidence_type": evidence_type,
            "live_patch_allowed": live_patch_allowed,
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
    }


def _print_summary_line(label: str, stats: dict[str, Any]) -> None:
    print(
        f"{label:<12} picks={stats['n']:>7,} open_overlap={stats['actual_opened_overlap']:>4,} "
        f"avg_ep={_fmt_num(stats['avg_entry'])} rr={_fmt_num(stats['avg_reward_risk'])} "
        f"m-be={_fmt_num(stats['avg_model_margin'])} edge={_fmt_num(stats['avg_edge'])} "
        f"exp={_fmt_pct(stats['expensive_entry_rate'])} weakRR={_fmt_pct(stats['weak_reward_risk_rate'])} "
        f"toxic80={_fmt_pct(stats['toxic_80_90_rate'])} badGeom={_fmt_pct(stats['model_edge_bad_geometry_rate'])} "
        f"starved={_fmt_pct(stats['starved_slot_rate'])}"
    )


def _print_mode_block(title: str, sim: dict[str, Any]) -> None:
    print()
    print(title)
    print("-" * len(title))
    print(f"scans analyzed: {sim['scans']:,}  limit: top {sim['limit']}")
    _print_summary_line("current", sim["current"])
    _print_summary_line("payoff", sim["payoff"])
    _print_summary_line("strict", sim["strict"])
    print("overlap:")
    for key, value in sim["overlap"].items():
        print(f"  {key:<36} {_fmt_pct(value) if isinstance(value, float) and key.endswith('rate') else value}")
    print("action mix:")
    for mode in ("current", "payoff", "strict"):
        print(f"  {mode:<8} {sim[mode]['action_mix']}")
    print("final blocker mix:")
    for mode in ("current", "payoff", "strict"):
        print(f"  {mode:<8} {sim[mode]['final_blocker_mix']}")


def _print_settled_matches(sim: dict[str, Any]) -> None:
    print()
    print("SETTLED OVERLAP CHECK")
    print("---------------------")
    for mode in ("current", "payoff", "strict"):
        match = sim["settled_matches"][mode]
        summary = match["summary"]
        print(
            f"{mode:<8} matched={match['matched_rows']:>4,} evidence={match['evidence_type']:<32} "
            f"WR={_fmt_pct(summary.get('win_rate'))} ROI={_fmt_pct(summary.get('roi'))} "
            f"PF={_fmt_num(summary.get('profit_factor'))} PnL={_fmt_money(summary.get('total_economic_pnl'))}"
        )


def print_report(report: dict[str, Any]) -> None:
    print("=" * 98)
    print("PAYOFF-AWARE RANKING SIMULATION")
    print("=" * 98)
    print("Read-only: no scanner order, thresholds, PaperTrader, strategy, risk, logs, or live-money state are modified.")
    print(f"candidate source:      {report['source']}")
    print(f"scanner tail-limited:  {report['scanner_tail_limited']}")
    for key, value in report["counts"].items():
        print(f"{key:<24} {_fmt_int(value)}")

    _print_mode_block("TOP 1 SELECTION SIMULATION", report["top1"])
    _print_mode_block("TOP 3 SELECTION SIMULATION", report["top3"])
    _print_settled_matches(report["top3"])

    print()
    print("QUESTIONS")
    print("---------")
    for key, value in report["questions"].items():
        print(f"{key:<48} {value}")

    print()
    print("SAFETY LOCKS")
    print("------------")
    for key, value in report["safety"].items():
        print(f"{key:<30} {value}")

    print()
    print("VERDICT")
    print("-------")
    print("Payoff-aware ranking improves candidate geometry if it lowers expensive-entry / toxic-zone rates or raises reward/risk.")
    print("This report is not profit proof unless simulated picks overlap settled paper outcomes; most counterfactual picks remain candidate-quality evidence only.")
    print("Live patch allowed: False")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> int:
    print_report(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
