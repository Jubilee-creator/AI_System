#!/usr/bin/env python3
"""
Phase 10O - Upstream Filter Simulation / Candidate Hygiene Test
Sentinel: UPSTREAM_FILTER_SIMULATION_OK

Read-only simulation of candidate hygiene filters before PaperTrader. This
does not change scanner order, ranking, thresholds, PaperTrader, risk, logs, or
live-money settings. All recommendations remain simulation-only.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (  # noqa: E402
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    MIN_EDGE,
    QUARANTINED_TICKER_PREFIXES,
    TRADING_MODE,
)
from tools import report_upstream_candidate_quality_autopsy as upstream  # noqa: E402
from tools.report_candidate_to_paper_forward_blockers import (  # noqa: E402
    FUNNEL_LOG,
    SHADOW_START,
    SHADOW_START_TEXT,
    classify_stage,
)

SENTINEL = "UPSTREAM_FILTER_SIMULATION_OK"

EXPENSIVE_LOW = 0.80
EXPENSIVE_HIGH = 0.90
WEAK_REWARD_RISK = 0.25
REPEAT_MARKET_QUALITY_MIN_BLOCKS = 20

Recommendation = str
Predicate = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class HygieneFilter:
    name: str
    condition: str
    predicate: Predicate
    overfitting_risk: str
    safety_preservation: bool = True
    simulation_only: bool = True


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _rate(count: int, total: int) -> float | None:
    return count / total if total else None


def _avg(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def _reason(row: dict[str, Any]) -> str:
    return str(row.get("final_reason") or row.get("reason") or "UNKNOWN")


def _is_expensive_80_90(row: dict[str, Any]) -> bool:
    price = upstream.side_entry_price(row)
    return price is not None and EXPENSIVE_LOW <= price < EXPENSIVE_HIGH


def _is_weak_reward_risk(row: dict[str, Any]) -> bool:
    rr = upstream.reward_risk(upstream.side_entry_price(row))
    return rr is not None and rr < WEAK_REWARD_RISK


def _entry_conditioned_edge_floor(row: dict[str, Any]) -> bool:
    price = upstream.side_entry_price(row)
    margin = upstream.model_margin(row)
    edge = upstream.edge_value(row)
    if price is None or margin is None:
        return False
    if price >= 0.90:
        return margin < 0.055
    if price >= 0.80:
        return margin < 0.08
    if price >= 0.70:
        return margin < 0.05
    return bool(edge is not None and edge < MIN_EDGE and price >= 0.60)


def repeated_market_quality_prefixes(
    rows: list[dict[str, Any]],
    min_blocks: int = REPEAT_MARKET_QUALITY_MIN_BLOCKS,
) -> set[str]:
    counts = Counter(
        upstream.ticker_prefix(upstream.ticker_of(row))
        for row in rows
        if classify_stage(row) == "market_quality"
    )
    return {prefix for prefix, count in counts.items() if count >= min_blocks}


def _market_quality_repeat_predicate(rows: list[dict[str, Any]]) -> Predicate:
    repeated = repeated_market_quality_prefixes(rows)
    return lambda row: upstream.ticker_prefix(upstream.ticker_of(row)) in repeated


def build_filters(rows: list[dict[str, Any]]) -> dict[str, HygieneFilter]:
    return {
        "A": HygieneFilter(
            name="pre_rank_quarantine_exclusion",
            condition="ticker starts with any configured quarantined prefix before ranking",
            predicate=upstream.is_quarantined,
            overfitting_risk="LOW",
        ),
        "B": HygieneFilter(
            name="market_quality_repeat_deprioritization",
            condition=f"ticker prefix has >= {REPEAT_MARKET_QUALITY_MIN_BLOCKS} post-shadow market-quality blocks",
            predicate=_market_quality_repeat_predicate(rows),
            overfitting_risk="MEDIUM",
        ),
        "C": HygieneFilter(
            name="weak_reward_risk_pre_rank_penalty",
            condition="reward_risk < 0.25 is removed/ranked behind cleaner candidates in simulation",
            predicate=_is_weak_reward_risk,
            overfitting_risk="MEDIUM",
        ),
        "D": HygieneFilter(
            name="expensive_entry_research_gate",
            condition="entry_price 0.80-0.90 requires extra model_margin and clean bucket proof",
            predicate=_is_expensive_80_90,
            overfitting_risk="MEDIUM",
        ),
        "E": HygieneFilter(
            name="entry_price_conditioned_edge_floor",
            condition="higher entry_price requires proportionally stronger model_margin",
            predicate=_entry_conditioned_edge_floor,
            overfitting_risk="MEDIUM_HIGH",
        ),
        "F": HygieneFilter(
            name="side_specific_quality_profiles",
            condition="BET_YES and BET_NO are analyzed separately, not removed",
            predicate=lambda row: False,
            overfitting_risk="LOW",
        ),
    }


STACKS = {
    "Stack 1": ("A",),
    "Stack 2": ("A", "B"),
    "Stack 3": ("A", "C"),
    "Stack 4": ("A", "B", "C"),
    "Stack 5": ("A", "B", "C", "D"),
    "Stack 6": ("A", "B", "C", "D", "E"),
}


def _quality_score(rows: list[dict[str, Any]]) -> float | None:
    return upstream._candidate_quality_score(rows)


def _side_profiles(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    profiles: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[upstream.action_of(row)].append(row)
    for action, items in sorted(grouped.items()):
        profiles[action] = {
            "n": len(items),
            "avg_entry": _avg([upstream.side_entry_price(row) for row in items]),
            "avg_reward_risk": _avg([upstream.reward_risk(upstream.side_entry_price(row)) for row in items]),
            "avg_model_margin": _avg([upstream.model_margin(row) for row in items]),
            "market_quality_blocks": sum(1 for row in items if classify_stage(row) == "market_quality"),
            "quarantine_blocks": sum(1 for row in items if upstream.is_quarantined(row)),
            "weak_reward_risk_rows": sum(1 for row in items if _is_weak_reward_risk(row)),
            "expensive_80_90_rows": sum(1 for row in items if _is_expensive_80_90(row)),
            "top_reasons": dict(Counter(_reason(row) for row in items).most_common(5)),
        }
    return profiles


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows),
        "market_quality_blocks": sum(1 for row in rows if classify_stage(row) == "market_quality"),
        "quarantine_blocks": sum(1 for row in rows if upstream.is_quarantined(row)),
        "min_edge_blocks": sum(1 for row in rows if _reason(row) == "BLOCKED_MIN_EDGE"),
        "council_blocks": sum(1 for row in rows if classify_stage(row) == "council"),
        "edge_danger_blocks": sum(1 for row in rows if _reason(row) == "BLOCKED_EDGE_DANGER_GUARD"),
        "expensive_80_90_rows": sum(1 for row in rows if _is_expensive_80_90(row)),
        "weak_reward_risk_rows": sum(1 for row in rows if _is_weak_reward_risk(row)),
        "avg_entry": _avg([upstream.side_entry_price(row) for row in rows]),
        "avg_reward_risk": _avg([upstream.reward_risk(upstream.side_entry_price(row)) for row in rows]),
        "avg_model_margin": _avg([upstream.model_margin(row) for row in rows]),
        "candidate_stream_quality_score": _quality_score(rows),
    }


def _estimate_proof_throughput_improvement(before: dict[str, Any], after: dict[str, Any]) -> float | None:
    if not before["n"]:
        return None
    before_bad = (
        before["market_quality_blocks"]
        + before["quarantine_blocks"]
        + before["weak_reward_risk_rows"]
        + before["expensive_80_90_rows"]
    )
    after_bad = (
        after["market_quality_blocks"]
        + after["quarantine_blocks"]
        + after["weak_reward_risk_rows"]
        + after["expensive_80_90_rows"]
    )
    if before_bad <= 0:
        return 0.0
    return max(0.0, (before_bad - after_bad) / before_bad)


def _starvation_risk(removal_rate: float | None, after_n: int) -> str:
    if after_n == 0 or (removal_rate is not None and removal_rate >= 0.85):
        return "HIGH"
    if removal_rate is not None and removal_rate >= 0.60:
        return "MEDIUM_HIGH"
    if removal_rate is not None and removal_rate >= 0.40:
        return "MEDIUM"
    return "LOW"


def classify_filter_result(result: dict[str, Any]) -> Recommendation:
    removal_rate = result["removal_rate"] or 0.0
    improvement = result["estimated_proof_throughput_improvement"] or 0.0
    overfit = result["overfitting_risk"]
    if result["candidates_removed"] == 0 and result["filter_name"] != "side_specific_quality_profiles":
        return "COSMETIC_ONLY"
    if result["starvation_risk"] == "HIGH":
        return "TOO_AGGRESSIVE_STARVES_SYSTEM"
    if overfit == "HIGH":
        return "OVERFIT_RISK_HIGH"
    if result["filter_name"] == "side_specific_quality_profiles":
        return "SAFE_TO_SHADOW_TEST"
    if improvement >= 0.25 and removal_rate <= 0.65 and overfit in {"LOW", "MEDIUM"}:
        return "SAFE_TO_SHADOW_TEST"
    if improvement > 0.05:
        return "PROMISING_BUT_NEEDS_MORE_DATA"
    return "COSMETIC_ONLY"


def simulate_filter(rows: list[dict[str, Any]], filt: HygieneFilter) -> dict[str, Any]:
    before = _summary(rows)
    removed = [row for row in rows if filt.predicate(row)]
    kept = [row for row in rows if not filt.predicate(row)]
    after = _summary(kept)
    removal_rate = _rate(len(removed), len(rows))
    result = {
        "filter_name": filt.name,
        "condition": filt.condition,
        "candidates_before": len(rows),
        "candidates_after": len(kept),
        "candidates_removed": len(removed),
        "removal_rate": removal_rate,
        "market_quality_blocks_removed": sum(1 for row in removed if classify_stage(row) == "market_quality"),
        "quarantine_blocks_removed": sum(1 for row in removed if upstream.is_quarantined(row)),
        "min_edge_blocks_removed": sum(1 for row in removed if _reason(row) == "BLOCKED_MIN_EDGE"),
        "council_blocks_removed": sum(1 for row in removed if classify_stage(row) == "council"),
        "edge_danger_blocks_removed": sum(1 for row in removed if _reason(row) == "BLOCKED_EDGE_DANGER_GUARD"),
        "expensive_entry_rows_removed": sum(1 for row in removed if _is_expensive_80_90(row)),
        "weak_reward_risk_rows_removed": sum(1 for row in removed if _is_weak_reward_risk(row)),
        "avg_entry_before": before["avg_entry"],
        "avg_entry_after": after["avg_entry"],
        "avg_reward_risk_before": before["avg_reward_risk"],
        "avg_reward_risk_after": after["avg_reward_risk"],
        "avg_model_margin_before": before["avg_model_margin"],
        "avg_model_margin_after": after["avg_model_margin"],
        "quality_score_before": before["candidate_stream_quality_score"],
        "quality_score_after": after["candidate_stream_quality_score"],
        "estimated_proof_throughput_improvement": _estimate_proof_throughput_improvement(before, after),
        "starvation_risk": _starvation_risk(removal_rate, len(kept)),
        "overfitting_risk": filt.overfitting_risk,
        "safety_preservation": filt.safety_preservation,
        "simulation_only": filt.simulation_only,
        "safe_for_future_shadow_only_testing": False,
    }
    result["classification"] = classify_filter_result(result)
    result["safe_for_future_shadow_only_testing"] = result["classification"] in {
        "SAFE_TO_SHADOW_TEST",
        "PROMISING_BUT_NEEDS_MORE_DATA",
    }
    return result


def simulate_stack(rows: list[dict[str, Any]], filters: dict[str, HygieneFilter], stack_name: str, codes: tuple[str, ...]) -> dict[str, Any]:
    before = _summary(rows)
    removed_keys: set[int] = set()
    for code in codes:
        filt = filters[code]
        for idx, row in enumerate(rows):
            if idx not in removed_keys and filt.predicate(row):
                removed_keys.add(idx)
    removed = [row for idx, row in enumerate(rows) if idx in removed_keys]
    kept = [row for idx, row in enumerate(rows) if idx not in removed_keys]
    after = _summary(kept)
    removal_rate = _rate(len(removed), len(rows))
    overfit_order = {"LOW": 0, "MEDIUM": 1, "MEDIUM_HIGH": 2, "HIGH": 3}
    overfit = max((filters[code].overfitting_risk for code in codes), key=lambda item: overfit_order.get(item, 0))
    result = {
        "filter_name": stack_name,
        "filter_codes": list(codes),
        "condition": " + ".join(filters[code].name for code in codes),
        "candidates_before": len(rows),
        "candidates_after": len(kept),
        "candidates_removed": len(removed),
        "removal_rate": removal_rate,
        "market_quality_blocks_removed": sum(1 for row in removed if classify_stage(row) == "market_quality"),
        "quarantine_blocks_removed": sum(1 for row in removed if upstream.is_quarantined(row)),
        "min_edge_blocks_removed": sum(1 for row in removed if _reason(row) == "BLOCKED_MIN_EDGE"),
        "council_blocks_removed": sum(1 for row in removed if classify_stage(row) == "council"),
        "edge_danger_blocks_removed": sum(1 for row in removed if _reason(row) == "BLOCKED_EDGE_DANGER_GUARD"),
        "expensive_entry_rows_removed": sum(1 for row in removed if _is_expensive_80_90(row)),
        "weak_reward_risk_rows_removed": sum(1 for row in removed if _is_weak_reward_risk(row)),
        "avg_entry_before": before["avg_entry"],
        "avg_entry_after": after["avg_entry"],
        "avg_reward_risk_before": before["avg_reward_risk"],
        "avg_reward_risk_after": after["avg_reward_risk"],
        "avg_model_margin_before": before["avg_model_margin"],
        "avg_model_margin_after": after["avg_model_margin"],
        "quality_score_before": before["candidate_stream_quality_score"],
        "quality_score_after": after["candidate_stream_quality_score"],
        "estimated_proof_throughput_improvement": _estimate_proof_throughput_improvement(before, after),
        "starvation_risk": _starvation_risk(removal_rate, len(kept)),
        "overfitting_risk": overfit,
        "safety_preservation": all(filters[code].safety_preservation for code in codes),
        "simulation_only": True,
        "safe_for_future_shadow_only_testing": False,
    }
    result["classification"] = classify_filter_result(result)
    result["safe_for_future_shadow_only_testing"] = result["classification"] in {
        "SAFE_TO_SHADOW_TEST",
        "PROMISING_BUT_NEEDS_MORE_DATA",
    }
    return result


def _safest_stack(stack_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        row for row in stack_results
        if row["safe_for_future_shadow_only_testing"]
        and row["starvation_risk"] in {"LOW", "MEDIUM"}
        and row["overfitting_risk"] in {"LOW", "MEDIUM"}
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda row: (
            row["classification"] == "SAFE_TO_SHADOW_TEST",
            row["estimated_proof_throughput_improvement"] or 0.0,
            -(row["removal_rate"] or 0.0),
        ),
        reverse=True,
    )
    return candidates[0]


def build_report(
    funnel_path: Path = FUNNEL_LOG,
    shadow_start: datetime = SHADOW_START,
) -> dict[str, Any]:
    rows = upstream.rows_after(upstream.read_jsonl(funnel_path), shadow_start)
    filters = build_filters(rows)
    baseline = _summary(rows)
    individual = {code: simulate_filter(rows, filt) for code, filt in filters.items()}
    stacks = {name: simulate_stack(rows, filters, name, codes) for name, codes in STACKS.items()}
    safest = _safest_stack(list(stacks.values()))
    return {
        "shadow_start": shadow_start.isoformat(),
        "baseline": baseline,
        "individual_filter_results": individual,
        "stack_results": stacks,
        "side_specific_profiles": _side_profiles(rows),
        "safest_filter_stack_for_shadow_testing": safest["filter_name"] if safest else None,
        "safest_filter_stack_details": safest,
        "deployable_live": False,
        "safety": {
            "trading_mode": TRADING_MODE,
            "real_money_allowed": False,
            "scale_allowed": False,
            "kelly_disabled": bool(GLOBAL_FORCED_LEARNING_MODE),
            "dc_override_enabled": bool(DATA_COLLECTION_OVERRIDE_ENABLED),
            "kxeth_quarantine_active": bool(QUARANTINED_TICKER_PREFIXES),
            "live_strategy_mutated": False,
        },
    }


def _print_result(row: dict[str, Any], label: str) -> None:
    print(
        f"  {label}: class={row['classification']} before={_fmt_int(row['candidates_before'])} "
        f"after={_fmt_int(row['candidates_after'])} removed={_fmt_int(row['candidates_removed'])} "
        f"remove_rate={_fmt_pct(row['removal_rate'])} mq_removed={_fmt_int(row['market_quality_blocks_removed'])} "
        f"quarantine_removed={_fmt_int(row['quarantine_blocks_removed'])} min_edge_removed={_fmt_int(row['min_edge_blocks_removed'])} "
        f"council_removed={_fmt_int(row['council_blocks_removed'])} edge_danger_removed={_fmt_int(row['edge_danger_blocks_removed'])}"
    )
    print(
        f"     expensive_removed={_fmt_int(row['expensive_entry_rows_removed'])} weak_rr_removed={_fmt_int(row['weak_reward_risk_rows_removed'])} "
        f"entry={_fmt_num(row['avg_entry_before'])}->{_fmt_num(row['avg_entry_after'])} "
        f"rr={_fmt_num(row['avg_reward_risk_before'])}->{_fmt_num(row['avg_reward_risk_after'])} "
        f"margin={_fmt_num(row['avg_model_margin_before'])}->{_fmt_num(row['avg_model_margin_after'])} "
        f"quality={_fmt_num(row['quality_score_before'], 2)}->{_fmt_num(row['quality_score_after'], 2)} "
        f"throughput_est={_fmt_pct(row['estimated_proof_throughput_improvement'])} "
        f"starvation={row['starvation_risk']} overfit={row['overfitting_risk']} "
        f"simulation_only={row['simulation_only']}"
    )


def print_report(report: dict[str, Any]) -> None:
    baseline = report["baseline"]
    print("=== Upstream Filter Simulation (Phase 10O) ===")
    print(f"shadow_start: {report['shadow_start']}")
    print(f"baseline_candidates: {_fmt_int(baseline['n'])}")
    print(f"baseline_quality_score: {_fmt_num(baseline['candidate_stream_quality_score'], 2)} / 100")
    print(f"baseline_market_quality_blocks: {_fmt_int(baseline['market_quality_blocks'])}")
    print(f"baseline_quarantine_blocks: {_fmt_int(baseline['quarantine_blocks'])}")
    print(f"baseline_expensive_80_90_rows: {_fmt_int(baseline['expensive_80_90_rows'])}")
    print(f"baseline_weak_reward_risk_rows: {_fmt_int(baseline['weak_reward_risk_rows'])}")

    print("\nIndividual filter simulations:")
    for code, row in report["individual_filter_results"].items():
        _print_result(row, f"{code} {row['filter_name']}")

    print("\nStack simulations:")
    for name, row in report["stack_results"].items():
        _print_result(row, name)

    print("\nSide-specific quality profiles:")
    for action, row in report["side_specific_profiles"].items():
        print(
            f"  {action}: n={_fmt_int(row['n'])} entry={_fmt_num(row['avg_entry'])} "
            f"rr={_fmt_num(row['avg_reward_risk'])} margin={_fmt_num(row['avg_model_margin'])} "
            f"mq={_fmt_int(row['market_quality_blocks'])} quarantine={_fmt_int(row['quarantine_blocks'])} "
            f"weak_rr={_fmt_int(row['weak_reward_risk_rows'])} expensive={_fmt_int(row['expensive_80_90_rows'])} "
            f"reasons={row['top_reasons']}"
        )

    print("\nSafest future shadow-only stack:")
    print(f"  {report['safest_filter_stack_for_shadow_testing'] or 'NONE'}")
    print(f"deployable_live: {report['deployable_live']}")

    print("\nSafety locks:")
    for key, value in report["safety"].items():
        print(f"  {key}: {value}")
    print(SENTINEL)


def main() -> None:
    print_report(build_report())


if __name__ == "__main__":
    main()
