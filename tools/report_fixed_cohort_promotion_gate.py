#!/usr/bin/env python3
"""
Phase 9W — Fixed-Cohort Promotion Gate + Council/Entry Damage Monitor
Sentinel: FIXED_COHORT_PROMOTION_GATE_REPORT_OK

Read-only promotion gate for candidate pockets discovered from the fresh
economic proof cohort. The report defines the evidence required before any
future live-patch discussion and highlights the council/entry pockets causing
the most damage.
"""
from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_probability_calibration_payoff_truth as calib
from tools.report_accounting_version_proof_cohorts import (
    classify_accounting_version,
    economic_pnl_value,
    entry_price,
    is_kxeth_or_quarantined,
    load_trades,
    risk_edge,
)
from tools.report_fresh_economic_proof_autopsy import council_path, edge_bucket, price_bucket

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "FIXED_COHORT_PROMOTION_GATE_REPORT_OK"

WINDOW_SIZES = (10, 20, 30, 50)
COUNCIL_PATHS = ("builder_boost", "critic_caution", "bootstrap_era_allow", "other")
ENTRY_BUCKETS = ("0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00")
MIN_POCKET_SAMPLE = 5
MIN_WINDOW_SAMPLE = 10
MIN_PROMOTION_ROWS = 50
MIN_PASS_WINDOWS = 3
MIN_PROMOTION_ROI = 0.0
MIN_PROMOTION_PF = 1.20
MIN_PROMOTION_WR_MARGIN = 0.03
MAX_CALIBRATION_GAP = 0.10
SEVERE_DRAWDOWN_LIMIT = -10.0


@dataclass(frozen=True)
class PocketSpec:
    name: str
    description: str
    predicate: Callable[[dict[str, Any]], bool]


@dataclass
class PocketResult:
    spec: PocketSpec
    rows: list[dict[str, Any]]
    summary: dict[str, Any]
    window_statuses: list[tuple[int, str, dict[str, Any]]]
    windows_passed: int
    windows_total: int
    latest_window_status: str
    worst_window_status: str
    status: str
    reasons: list[str]


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _sort_key(rec: dict[str, Any], idx: int) -> tuple[str, int]:
    return (str(rec.get("timestamp") or ""), idx)


def _sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(enumerate(rows), key=lambda item: _sort_key(item[1], item[0]))
    return [rec for _, rec in ordered]


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    phat = wins / n
    denom = 1.0 + (z * z) / n
    center = (phat + (z * z) / (2.0 * n)) / denom
    margin = z * math.sqrt((phat * (1.0 - phat) + (z * z) / (4.0 * n)) / n) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def _clean_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(calib.clean_proof_rows(records))


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = dict(calib._bucket_summary(rows))
    summary["avg_edge"] = summary.get("avg_risk_edge")
    summary["model_edge_vs_price"] = (
        summary["avg_model_probability"] - summary["avg_entry_price"]
        if summary.get("avg_model_probability") is not None and summary.get("avg_entry_price") is not None
        else None
    )
    summary["actual_edge_vs_price"] = (
        summary["win_rate"] - summary["avg_entry_price"]
        if summary.get("win_rate") is not None and summary.get("avg_entry_price") is not None
        else None
    )
    summary["breakeven_gap"] = (
        summary["win_rate"] - summary["breakeven_wr"]
        if summary.get("win_rate") is not None and summary.get("breakeven_wr") is not None
        else None
    )
    summary["calibration_abs"] = abs(summary["calibration_gap"]) if summary.get("calibration_gap") is not None else None
    summary["ev_gap"] = (
        summary["total_economic_pnl"] - summary["total_expected_ev"]
        if summary.get("total_economic_pnl") is not None and summary.get("total_expected_ev") is not None
        else None
    )
    summary["win_rate_ci_low"], summary["win_rate_ci_high"] = _wilson_interval(summary["wins"], summary["n"])
    summary["sample_label"] = (
        "TOO_SMALL"
        if summary["n"] < 10
        else "INSPECTION_ONLY"
        if summary["n"] < 30
        else "BETTER_BUT_STILL_PAPER_ONLY"
        if summary["n"] < MIN_PROMOTION_ROWS
        else "MEETS_PROMOTION_FLOOR"
    )
    summary["high_entry_rows"] = sum(1 for r in rows if price_bucket(entry_price(r)) == "0.80-0.90")
    summary["high_entry_pnl"] = sum((_as_float(economic_pnl_value(r)) or 0.0) for r in rows if price_bucket(entry_price(r)) == "0.80-0.90")
    summary["builder_boost_rows"] = sum(1 for r in rows if council_path(r) == "builder_boost")
    summary["critic_caution_rows"] = sum(1 for r in rows if council_path(r) == "critic_caution")
    summary["bootstrap_era_allow_rows"] = sum(1 for r in rows if council_path(r) == "bootstrap_era_allow")
    summary["edge_0_10_plus_rows"] = sum(1 for r in rows if risk_edge(r) is not None and risk_edge(r) >= 0.10)
    summary["max_drawdown_flag"] = summary.get("max_drawdown") is not None and summary["max_drawdown"] <= SEVERE_DRAWDOWN_LIMIT
    return summary


def _gate_flags(name: str, summary: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if summary.get("roi") is not None and summary["roi"] < 0:
        flags.append("NEGATIVE_ROI")
    if summary.get("profit_factor") is not None and summary["profit_factor"] < 1.0:
        flags.append("PF_BELOW_1")
    if summary.get("win_rate") is not None and summary.get("breakeven_wr") is not None and summary["win_rate"] < summary["breakeven_wr"]:
        flags.append("BELOW_BREAKEVEN")
    if summary.get("total_expected_ev") is not None and summary.get("total_economic_pnl") is not None:
        if summary["total_expected_ev"] > 0 and summary["total_economic_pnl"] < 0:
            flags.append("MODEL_EV_FAKE")
    if summary.get("calibration_abs") is not None and summary["calibration_abs"] > MAX_CALIBRATION_GAP:
        flags.append("CALIBRATION_DAMAGE")
    if summary.get("max_drawdown_flag"):
        flags.append("DRAWDOWN_WARNING")
    if summary.get("avg_win") is not None and summary.get("avg_loss") is not None:
        if abs(summary["avg_loss"]) > abs(summary["avg_win"]) * 1.5:
            flags.append("PAYOFF_ASYMMETRY")
    if "0.80-0.90" in name and summary.get("total_economic_pnl") is not None and summary["total_economic_pnl"] < 0:
        flags.append("HIGH_ENTRY_POISON")
    if "builder_boost" in name and "0.80-0.90" in name and summary.get("total_economic_pnl") is not None and summary["total_economic_pnl"] < 0:
        flags.append("BUILDER_HIGH_ENTRY_OVERLAP")
    if summary.get("n", 0) < MIN_POCKET_SAMPLE:
        flags.append("TINY_SAMPLE")
    if summary.get("n", 0) < MIN_PROMOTION_ROWS:
        flags.append("PROMOTION_SAMPLE_BELOW_50")
    return list(dict.fromkeys(flags))


def _window_gate_status(summary: dict[str, Any]) -> str:
    if summary["n"] < MIN_WINDOW_SAMPLE:
        return "TOO_SMALL"
    if (
        summary.get("roi") is not None
        and summary["roi"] > MIN_PROMOTION_ROI
        and summary.get("profit_factor") is not None
        and summary["profit_factor"] > MIN_PROMOTION_PF
        and summary.get("win_rate") is not None
        and summary.get("breakeven_wr") is not None
        and summary["win_rate"] > summary["breakeven_wr"] + MIN_PROMOTION_WR_MARGIN
        and summary.get("calibration_abs") is not None
        and summary["calibration_abs"] <= MAX_CALIBRATION_GAP
        and summary.get("total_expected_ev") is not None
        and summary.get("total_economic_pnl") is not None
        and not (summary["total_expected_ev"] > 0 and summary["total_economic_pnl"] < 0)
        and not summary.get("max_drawdown_flag")
    ):
        return "PASS"
    if summary.get("roi") is not None and summary["roi"] > 0:
        return "WATCHLIST"
    if summary.get("profit_factor") is not None and summary["profit_factor"] > 1.0:
        return "WATCHLIST"
    if summary.get("win_rate") is not None and summary.get("breakeven_wr") is not None and summary["win_rate"] > summary["breakeven_wr"]:
        return "WATCHLIST"
    return "FAIL"


def promotion_gate_status(name: str, summary: dict[str, Any], windows_passed: int, windows_total: int) -> str:
    flags = _gate_flags(name, summary)
    positive = (
        summary.get("roi") is not None
        and summary["roi"] > 0
        and summary.get("profit_factor") is not None
        and summary["profit_factor"] > 1.0
        and summary.get("win_rate") is not None
        and summary.get("breakeven_wr") is not None
        and summary["win_rate"] > summary["breakeven_wr"]
    )
    if summary["n"] < MIN_POCKET_SAMPLE:
        return "TINY_SAMPLE"
    if "NEGATIVE_ROI" in flags or "PF_BELOW_1" in flags or "BELOW_BREAKEVEN" in flags or "MODEL_EV_FAKE" in flags:
        return "REJECTED_POISON"
    if summary["n"] >= MIN_PROMOTION_ROWS and windows_passed >= MIN_PASS_WINDOWS and positive:
        if (
            summary.get("profit_factor") is not None
            and summary["profit_factor"] >= MIN_PROMOTION_PF
            and summary.get("breakeven_gap") is not None
            and summary["breakeven_gap"] >= MIN_PROMOTION_WR_MARGIN
            and summary.get("calibration_abs") is not None
            and summary["calibration_abs"] <= MAX_CALIBRATION_GAP
            and summary.get("total_expected_ev") is not None
            and summary.get("total_economic_pnl") is not None
            and not (summary["total_expected_ev"] > 0 and summary["total_economic_pnl"] < 0)
            and not summary.get("max_drawdown_flag")
        ):
            return "PROMOTION_ELIGIBLE_PAPER_ONLY"
    if positive:
        if summary["n"] < MIN_PROMOTION_ROWS:
            return "WATCHLIST_ONLY" if windows_passed <= 1 else "PROMISING_BUT_UNPROVEN"
        if windows_passed <= 1:
            return "WATCHLIST_ONLY"
        return "PROMISING_BUT_UNPROVEN"
    return "DO_NOT_PATCH_LIVE_YET"


def _window_slice(rows: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    if len(rows) < size:
        return []
    return rows[-size:]


def _evaluate_pocket(name: str, rows: list[dict[str, Any]], spec: PocketSpec) -> PocketResult:
    pocket_rows = [r for r in rows if spec.predicate(r)]
    summary = _summary(pocket_rows)
    window_statuses: list[tuple[int, str, dict[str, Any]]] = []
    for size in WINDOW_SIZES:
        window_rows = _window_slice(rows, size)
        if not window_rows:
            continue
        pocket_window_rows = [r for r in window_rows if spec.predicate(r)]
        window_summary = _summary(pocket_window_rows)
        window_status = _window_gate_status(window_summary)
        window_statuses.append((size, window_status, window_summary))

    windows_passed = sum(1 for _, status, _ in window_statuses if status == "PASS")
    windows_total = len(window_statuses)
    latest_window_status = window_statuses[-1][1] if window_statuses else "NO_WINDOW"
    worst_order = {"PASS": 0, "WATCHLIST": 1, "FAIL": 2, "TOO_SMALL": 3, "NO_WINDOW": 4}
    worst_window_status = max(window_statuses, key=lambda item: worst_order.get(item[1], 99))[1] if window_statuses else "NO_WINDOW"
    status = promotion_gate_status(name, summary, windows_passed, windows_total)
    reasons = _gate_flags(name, summary)
    if windows_passed <= 1 and summary["n"] >= MIN_POCKET_SAMPLE and status in {"WATCHLIST_ONLY", "PROMISING_BUT_UNPROVEN"}:
        reasons.append("SINGLE_WINDOW_LUCK")
    if summary["n"] < MIN_PROMOTION_ROWS and status in {"WATCHLIST_ONLY", "PROMISING_BUT_UNPROVEN"}:
        reasons.append("PROMOTION_SAMPLE_BELOW_50")
    return PocketResult(
        spec=spec,
        rows=pocket_rows,
        summary=summary,
        window_statuses=window_statuses,
        windows_passed=windows_passed,
        windows_total=windows_total,
        latest_window_status=latest_window_status,
        worst_window_status=worst_window_status,
        status=status,
        reasons=list(dict.fromkeys(reasons)),
    )


def _cell_key(rec: dict[str, Any]) -> str:
    return f"{council_path(rec)}|{price_bucket(entry_price(rec))}"


def _build_cell_results(rows: list[dict[str, Any]]) -> list[PocketResult]:
    results: list[PocketResult] = []
    for path in COUNCIL_PATHS:
        for bucket in ENTRY_BUCKETS:
            name = f"{path}|{bucket}"
            spec = PocketSpec(
                name=name,
                description=f"Council path {path} with entry bucket {bucket}",
                predicate=lambda r, p=path, b=bucket: council_path(r) == p and price_bucket(entry_price(r)) == b,
            )
            result = _evaluate_pocket(name, rows, spec)
            if result.summary["n"] > 0:
                results.append(result)
    return results


def _build_candidate_specs(rows: list[dict[str, Any]], cell_results: list[PocketResult]) -> list[PocketSpec]:
    specs: list[PocketSpec] = [
        PocketSpec(
            name="builder_boost|0.80-0.90",
            description="Builder boost council path in the high-entry poison band",
            predicate=lambda r: council_path(r) == "builder_boost" and price_bucket(entry_price(r)) == "0.80-0.90",
        ),
        PocketSpec(
            name="critic_caution|0.80-0.90",
            description="Critic caution path in the high-entry band",
            predicate=lambda r: council_path(r) == "critic_caution" and price_bucket(entry_price(r)) == "0.80-0.90",
        ),
        PocketSpec(
            name="critic_caution|0.90-1.00",
            description="Critic caution path in the 0.90+ entry band",
            predicate=lambda r: council_path(r) == "critic_caution" and price_bucket(entry_price(r)) == "0.90-1.00",
        ),
        PocketSpec(
            name="probability|0.90+",
            description="All clean rows with model_probability >= 0.90",
            predicate=lambda r: calib.model_probability_value(r) is not None and calib.model_probability_value(r) >= 0.90,
        ),
        PocketSpec(
            name="entry|0.80-0.90",
            description="All clean rows with entry_price in the 0.80-0.90 band",
            predicate=lambda r: price_bucket(entry_price(r)) == "0.80-0.90",
        ),
        PocketSpec(
            name="edge|0.10+",
            description="All clean rows with risk_edge >= 0.10",
            predicate=lambda r: risk_edge(r) is not None and risk_edge(r) >= 0.10,
        ),
        PocketSpec(
            name="cell|0.05-0.10|0.80-0.90",
            description="2D cell edge 0.05-0.10 and entry 0.80-0.90",
            predicate=lambda r: edge_bucket(risk_edge(r)) == "0.05-0.10" and price_bucket(entry_price(r)) == "0.80-0.90",
        ),
        PocketSpec(
            name="cell|0.90+|0.90-1.00",
            description="2D cell probability 0.90+ and entry 0.90-1.00",
            predicate=lambda r: calib.model_probability_bucket(calib.model_probability_value(r)) == "0.90+" and price_bucket(entry_price(r)) == "0.90-1.00",
        ),
        PocketSpec(
            name="cell|0.03-0.05|0.80-0.90",
            description="2D cell edge 0.03-0.05 and entry 0.80-0.90",
            predicate=lambda r: edge_bucket(risk_edge(r)) == "0.03-0.05" and price_bucket(entry_price(r)) == "0.80-0.90",
        ),
    ]
    seen = {spec.name for spec in specs}
    for result in cell_results:
        if result.summary["n"] >= MIN_POCKET_SAMPLE and result.summary.get("roi") is not None and result.summary["roi"] > 0 and result.summary["n"] < MIN_PROMOTION_ROWS:
            if result.spec.name not in seen:
                specs.append(result.spec)
                seen.add(result.spec.name)
    return specs


def _promotion_standards() -> dict[str, Any]:
    return {
        "min_clean_rows": MIN_PROMOTION_ROWS,
        "min_passing_windows": MIN_PASS_WINDOWS,
        "min_window_sample": MIN_WINDOW_SAMPLE,
        "min_roi": MIN_PROMOTION_ROI,
        "min_profit_factor": MIN_PROMOTION_PF,
        "min_win_rate_margin": MIN_PROMOTION_WR_MARGIN,
        "max_calibration_gap": MAX_CALIBRATION_GAP,
        "severe_drawdown_limit": SEVERE_DRAWDOWN_LIMIT,
    }


def build_report_state(records: list[dict[str, Any]]) -> dict[str, Any]:
    clean_rows = _clean_rows(records)
    baseline = _summary(clean_rows)
    counts = {
        "raw_records": len(records),
        "clean_rows": len(clean_rows),
        "excluded_total": len(records) - len(clean_rows),
        "excluded_kxeth_or_quarantined": sum(1 for r in records if is_kxeth_or_quarantined(r)),
        "excluded_data_collection_override": sum(1 for r in records if bool(r.get("data_collection_override"))),
        "excluded_bootstrap_provisional": sum(1 for r in records if bool(r.get("bootstrap_provisional"))),
        "excluded_side_coverage": sum(1 for r in records if bool(r.get("side_coverage_test")) or bool(r.get("side_coverage"))),
        "excluded_open_rows": sum(1 for r in records if str(r.get("status") or "").upper() == "OPEN"),
        "excluded_legacy_or_unversioned": sum(1 for r in records if classify_accounting_version(r) == "legacy_hybrid_or_unversioned"),
        "excluded_unknown_other": sum(1 for r in records if classify_accounting_version(r) == "unknown_other"),
        "excluded_missing_model_probability": sum(1 for r in records if calib.model_probability_value(r) is None),
        "excluded_missing_entry_price": sum(1 for r in records if entry_price(r) is None),
        "excluded_missing_economic_pnl": sum(1 for r in records if economic_pnl_value(r) is None),
    }
    cell_results = _build_cell_results(clean_rows)
    candidate_specs = _build_candidate_specs(clean_rows, cell_results)
    candidate_results = [_evaluate_pocket(spec.name, clean_rows, spec) for spec in candidate_specs]
    small_positive_cells = [result for result in cell_results if result.summary["n"] < MIN_PROMOTION_ROWS and result.summary.get("roi") is not None and result.summary["roi"] > 0]
    rejected = [r for r in candidate_results if r.status == "REJECTED_POISON"]
    watchlist = [r for r in candidate_results if r.status == "WATCHLIST_ONLY"]
    promising = [r for r in candidate_results if r.status == "PROMISING_BUT_UNPROVEN"]
    eligible = [r for r in candidate_results if r.status == "PROMOTION_ELIGIBLE_PAPER_ONLY"]
    overall_status = "DO_NOT_PATCH_LIVE_YET"
    return {
        "records": records,
        "clean_rows": clean_rows,
        "counts": counts,
        "baseline": baseline,
        "cell_results": cell_results,
        "candidate_results": candidate_results,
        "small_positive_cells": small_positive_cells,
        "rejected": rejected,
        "watchlist": watchlist,
        "promising": promising,
        "eligible": eligible,
        "promotion_standards": _promotion_standards(),
        "overall_status": overall_status,
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print()
    print("BASELINE CLEAN PROOF SUMMARY")
    print("-" * 90)
    print(f"  clean rows:                {summary['n']}")
    print(f"  wins / losses:             {summary['wins']} / {summary['losses']}")
    print(f"  win rate:                  {_fmt_pct(summary['win_rate'])}")
    ci = f"[{_fmt_pct(summary['win_rate_ci_low'])}, {_fmt_pct(summary['win_rate_ci_high'])}]"
    print(f"  win rate CI95:             {ci}")
    print(f"  avg model_probability:     {_fmt_num(summary['avg_model_probability'])}")
    print(f"  avg entry_price:           {_fmt_num(summary['avg_entry_price'])}")
    print(f"  avg risk_edge:             {_fmt_num(summary['avg_risk_edge'])}")
    print(f"  breakeven wr:              {_fmt_pct(summary['breakeven_wr'])}")
    print(f"  win-rate margin:           {_fmt_pct(summary['breakeven_gap'])}")
    print(f"  calibration gap:           {_fmt_num(summary['calibration_gap'])}")
    print(f"  model edge vs price:       {_fmt_num(summary['model_edge_vs_price'])}")
    print(f"  actual edge vs price:      {_fmt_num(summary['actual_edge_vs_price'])}")
    print(f"  economic pnl:              {_fmt_money(summary['total_economic_pnl'])}")
    print(f"  expected EV sum:           {_fmt_money(summary['total_expected_ev'])}")
    print(f"  EV gap:                    {_fmt_money(summary['ev_gap'])}")
    print(f"  ROI on capital at risk:    {_fmt_pct(summary['roi'])}")
    print(f"  profit factor:             {_fmt_num(summary['profit_factor'])}")
    print(f"  avg win / avg loss:        {_fmt_money(summary['avg_win'])} / {_fmt_money(summary['avg_loss'])}")
    print(f"  reward / risk:             {_fmt_num(summary['reward_risk'])}")
    print(f"  max drawdown:              {_fmt_money(summary['max_drawdown'])}")
    print(f"  sample label:              {summary['sample_label']}")


def _print_cell_table(cell_results: list[PocketResult]) -> None:
    print()
    print("COUNCIL PATH × ENTRY BUCKET DAMAGE MONITOR")
    print("-" * 150)
    print(
        f"{'cell':<26} {'n':>4} {'WR':>7} {'CI95':>16} {'BE':>7} {'mrg':>7} {'PnL':>10} "
        f"{'ROI':>8} {'PF':>7} {'RR':>7} {'status':>26}"
    )
    print("-" * 150)
    for result in sorted(cell_results, key=lambda r: (_as_float(r.summary.get("total_economic_pnl")) or 0.0, r.spec.name)):
        s = result.summary
        if s["n"] == 0:
            continue
        ci = f"[{_fmt_pct(s['win_rate_ci_low'])}, {_fmt_pct(s['win_rate_ci_high'])}]"
        print(
            f"{result.spec.name:<26} {s['n']:>4} {_fmt_pct(s['win_rate']):>7} {ci:>16} {_fmt_pct(s['breakeven_wr']):>7} "
            f"{_fmt_pct(s['breakeven_gap']):>7} {_fmt_money(s['total_economic_pnl']):>10} {_fmt_pct(s['roi']):>8} "
            f"{_fmt_num(s['profit_factor']):>7} {_fmt_num(s['reward_risk']):>7} {result.status:>26}"
        )


def _print_candidate_table(results: list[PocketResult], title: str) -> None:
    print()
    print(title)
    print("-" * 160)
    print(
        f"{'candidate':<34} {'n':>4} {'WR':>7} {'CI95':>16} {'BE':>7} {'mrg':>7} {'PnL':>10} "
        f"{'ROI':>8} {'PF':>7} {'pass':>4} {'win/total':>10} {'latest':>12} {'worst':>12} {'status':>28}"
    )
    print("-" * 160)
    for result in results:
        s = result.summary
        if s["n"] == 0:
            continue
        ci = f"[{_fmt_pct(s['win_rate_ci_low'])}, {_fmt_pct(s['win_rate_ci_high'])}]"
        print(
            f"{result.spec.name:<34} {s['n']:>4} {_fmt_pct(s['win_rate']):>7} {ci:>16} {_fmt_pct(s['breakeven_wr']):>7} "
            f"{_fmt_pct(s['breakeven_gap']):>7} {_fmt_money(s['total_economic_pnl']):>10} {_fmt_pct(s['roi']):>8} "
            f"{_fmt_num(s['profit_factor']):>7} {result.windows_passed:>4} {f'{result.windows_passed}/{result.windows_total}':>10} "
            f"{result.latest_window_status:>12} {result.worst_window_status:>12} {result.status:>28}"
        )


def _print_damage_rank(results: list[PocketResult], title: str) -> None:
    ranked = [r for r in results if r.summary["n"] > 0]
    ranked.sort(key=lambda r: (_as_float(r.summary.get("total_economic_pnl")) or 0.0, -r.summary["n"]))
    print()
    print(title)
    print("-" * 110)
    for result in ranked[:8]:
        s = result.summary
        flags = ", ".join(result.reasons[:4]) if result.reasons else "none"
        print(
            f"  {result.spec.name:<34} pnl={_fmt_money(s['total_economic_pnl']):>10} roi={_fmt_pct(s['roi']):>8} "
            f"pf={_fmt_num(s['profit_factor']):>7} wr={_fmt_pct(s['win_rate']):>7} flags={flags}"
        )


def _print_standards(stds: dict[str, Any]) -> None:
    print()
    print("PROMOTION STANDARDS")
    print("-" * 90)
    print(f"  minimum clean rows:        {stds['min_clean_rows']}")
    print(f"  minimum passing windows:   {stds['min_passing_windows']}")
    print(f"  minimum window sample:     {stds['min_window_sample']}")
    print(f"  minimum ROI:               {stds['min_roi']:+.2f}")
    print(f"  minimum PF:                {stds['min_profit_factor']:.2f}")
    print(f"  minimum WR margin:         {stds['min_win_rate_margin'] * 100:.1f}pp")
    print(f"  maximum calibration gap:   {stds['max_calibration_gap'] * 100:.1f}pp")
    print(f"  severe drawdown limit:     {stds['severe_drawdown_limit']:+.2f}")


def render_report(state: dict[str, Any]) -> None:
    counts = state["counts"]
    print("=" * 90)
    print("FIXED-COHORT PROMOTION GATE + COUNCIL/ENTRY DAMAGE MONITOR")
    print("=" * 90)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print("Population: settled, economic_contract_notional_v1, normal_modern, non-KXETH clean proof rows only.")
    print(f"Raw records loaded: {counts['raw_records']}")
    print(f"Clean rows used:    {counts['clean_rows']}")
    print()
    print("Exclusions:")
    print(f"  KXETH/quarantined:       {counts['excluded_kxeth_or_quarantined']}")
    print(f"  data_collection_override:{counts['excluded_data_collection_override']}")
    print(f"  bootstrap_provisional:   {counts['excluded_bootstrap_provisional']}")
    print(f"  side coverage:           {counts['excluded_side_coverage']}")
    print(f"  open rows:               {counts['excluded_open_rows']}")
    print(f"  legacy/unversioned:      {counts['excluded_legacy_or_unversioned']}")
    print(f"  unknown_other:           {counts['excluded_unknown_other']}")
    print(f"  missing model_probability:{counts['excluded_missing_model_probability']}")
    print(f"  missing entry_price:     {counts['excluded_missing_entry_price']}")
    print(f"  missing economic_pnl:    {counts['excluded_missing_economic_pnl']}")

    _print_standards(state["promotion_standards"])
    _print_summary(state["baseline"])
    _print_cell_table(state["cell_results"])
    _print_candidate_table(state["candidate_results"], "FIXED-COHORT PROMOTION CHECKS")
    _print_damage_rank(state["cell_results"], "WORST COUNCIL × ENTRY DAMAGE CELLS")

    if state["small_positive_cells"]:
        print()
        print("SMALL POSITIVE CELLS (WATCHLIST ONLY BY SAMPLE FLOOR)")
        print("-" * 90)
        for result in sorted(state["small_positive_cells"], key=lambda r: (_as_float(r.summary.get("total_economic_pnl")) or 0.0, r.spec.name)):
            s = result.summary
            print(
                f"  {result.spec.name:<26} n={s['n']:>3} pnl={_fmt_money(s['total_economic_pnl']):>10} "
                f"roi={_fmt_pct(s['roi']):>8} pf={_fmt_num(s['profit_factor']):>7} status={result.status}"
            )
    else:
        print()
        print("SMALL POSITIVE CELLS (WATCHLIST ONLY BY SAMPLE FLOOR)")
        print("-" * 90)
        print("  none")

    print()
    print("PROMOTION VERDICT")
    print("-" * 90)
    print("  DO_NOT_PATCH_LIVE_YET")
    if state["eligible"]:
        print("  promotion-eligible paper-only pockets:")
        for result in state["eligible"]:
            print(f"    - {result.spec.name} (paper-only; do not patch live in this phase)")
    else:
        print("  no candidate pocket clears the paper-only promotion floor yet.")
    print("  live patching remains forbidden in this phase.")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> None:
    records = load_trades()
    state = build_report_state(records)
    render_report(state)


if __name__ == "__main__":
    main()
