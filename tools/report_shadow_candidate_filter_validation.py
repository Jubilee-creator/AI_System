#!/usr/bin/env python3
"""
Phase 9T — Shadow Candidate Filter Validation
Sentinel: SHADOW_CANDIDATE_FILTER_VALIDATION_REPORT_OK

Read-only validation layer for frozen candidate filters discovered from the
fresh Phase 9N economic proof cohort. This report separates discovery from
validation so we do not accidentally re-optimize on the same rows.
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.report_accounting_version_proof_cohorts import (
    clean_settled_rows,
    economic_pnl_value,
    entry_price,
    is_clean_proof_row,
    is_kxeth_or_quarantined,
    load_trades as load_raw_trades,
    recorded_pnl_value,
    risk_edge,
    stored_pnl_value,
    capital_at_risk_value,
    payout_notional_value,
)
from tools.report_fresh_economic_proof_autopsy import (
    cell_key,
    council_path,
    edge_bucket,
    fresh_proof_rows,
    price_bucket,
    summarize_cells,
    summarize_rows,
)

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "SHADOW_CANDIDATE_FILTER_VALIDATION_REPORT_OK"
DISCOVERY_FRACTION = 0.60
MIN_SHADOW_SAMPLE = 15
HIGH_ENTRY_BUCKET = "0.80-0.90"
VALID_PRICE_BUCKETS = ("0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00")
VALID_EDGE_BUCKETS = ("0.03-0.05", "0.05-0.10", "0.10+")


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "MISSING"
    if not math.isfinite(value):
        return "INF"
    return f"${value:+.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "MISSING"
    if not math.isfinite(value):
        return "INF"
    return f"{value * 100:.1f}%"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "MISSING"
    if not math.isfinite(value):
        return "INF"
    return f"{value:.{digits}f}"


def _timestamp_key(rec: dict[str, Any], idx: int) -> tuple[str, int]:
    return (str(rec.get("timestamp") or ""), idx)


def _clv_value(rec: dict[str, Any]) -> float | None:
    value = _as_float(rec.get("clv"))
    if value is not None:
        return value
    ep = entry_price(rec)
    exit_price = _as_float(rec.get("exit_price"))
    if ep is not None and exit_price is not None:
        return exit_price - ep
    return None


def _max_drawdown(rows: list[dict[str, Any]]) -> float | None:
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    seen = False
    for rec in rows:
        pnl = economic_pnl_value(rec)
        if pnl is None:
            continue
        seen = True
        cumulative += pnl
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst if seen else None


def _high_entry_bucket(rec: dict[str, Any]) -> bool:
    return price_bucket(entry_price(rec)) == HIGH_ENTRY_BUCKET


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = dict(summarize_rows(rows))
    clv_vals = [v for v in (_clv_value(r) for r in rows) if v is not None]
    summary["avg_clv"] = sum(clv_vals) / len(clv_vals) if clv_vals else None
    summary["total_clv"] = sum(clv_vals) if clv_vals else None
    summary["max_drawdown"] = _max_drawdown(rows)
    summary["high_entry_rows"] = sum(1 for r in rows if _high_entry_bucket(r))
    summary["high_entry_losses"] = sum(
        1 for r in rows if _high_entry_bucket(r) and (_as_float(economic_pnl_value(r)) or 0.0) < 0
    )
    summary["high_entry_pnl"] = sum(
        _as_float(economic_pnl_value(r)) or 0.0 for r in rows if _high_entry_bucket(r)
    )
    summary["high_entry_capital"] = sum(
        _as_float(capital_at_risk_value(r)) or 0.0 for r in rows if _high_entry_bucket(r)
    )
    summary["builder_boost_rows"] = sum(1 for r in rows if council_path(r) == "builder_boost")
    summary["critic_caution_rows"] = sum(1 for r in rows if council_path(r) == "critic_caution")
    summary["bootstrap_era_allow_rows"] = sum(1 for r in rows if council_path(r) == "bootstrap_era_allow")
    summary["edge_bucket_counts"] = Counter(edge_bucket(risk_edge(r)) for r in rows)
    summary["price_bucket_counts"] = Counter(price_bucket(entry_price(r)) for r in rows)
    summary["cell_map"] = summarize_cells(rows)
    return summary


def _sort_metric(value: Any) -> float:
    if value is None:
        return float("-inf")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float("-inf")
    if not math.isfinite(value):
        return float("-inf")
    return value


def _row_identity(rec: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(rec.get("timestamp") or ""),
        str(rec.get("ticker") or ""),
        str(rec.get("action") or ""),
        _as_float(rec.get("size")),
        _as_float(rec.get("entry_price")),
    )


def split_discovery_validation(rows: list[dict[str, Any]], discovery_fraction: float = DISCOVERY_FRACTION) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    ordered = sorted(enumerate(rows), key=lambda item: _timestamp_key(item[1], item[0]))
    if not ordered:
        return [], [], None
    cutoff_index = max(1, int(round(len(ordered) * discovery_fraction)))
    cutoff_index = min(cutoff_index, len(ordered))
    discovery = [rec for _, rec in ordered[:cutoff_index]]
    validation = [rec for _, rec in ordered[cutoff_index:]]
    cutoff_ts = str(ordered[cutoff_index - 1][1].get("timestamp") or "") if discovery else None
    return discovery, validation, cutoff_ts


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    description: str
    apply: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    frozen_cells: frozenset[str] = frozenset()


@dataclass
class CandidateResult:
    spec: CandidateSpec
    discovery_pass: list[dict[str, Any]]
    validation_pass: list[dict[str, Any]]
    discovery_summary: dict[str, Any]
    validation_summary: dict[str, Any]
    status: str
    retained_total_frequency: float | None
    retained_validation_frequency: float | None
    discovery_retained_frequency: float | None
    blocked_rows: int
    blocked_high_entry_rows: int
    blocked_high_entry_pnl: float
    avoided_high_entry_loss: float
    blocked_price_80_90_rows: int
    blocked_builder_boost_rows: int
    blocked_edge_0_10_plus_rows: int
    blocked_council_path_rows: dict[str, int]


def _row_filter_spec(name: str, description: str, predicate: Callable[[dict[str, Any]], bool]) -> CandidateSpec:
    def apply(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in rows if predicate(r)]

    return CandidateSpec(name=name, description=description, apply=apply)


def _cell_allowlist_spec(
    name: str,
    description: str,
    discovery_rows: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> CandidateSpec:
    cells = summarize_cells(discovery_rows)
    allowed = frozenset(key for key, stats in cells.items() if predicate(stats))

    def apply(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in rows if cell_key(r) in allowed]

    return CandidateSpec(name=name, description=description, apply=apply, frozen_cells=allowed)


def build_candidate_specs(discovery_rows: list[dict[str, Any]]) -> list[CandidateSpec]:
    specs: list[CandidateSpec] = [
        _row_filter_spec(
            "block_0.80-0.90",
            "Block the broad 0.80-0.90 price bucket",
            lambda r: price_bucket(entry_price(r)) != "0.80-0.90",
        ),
        _row_filter_spec(
            "block_0.70-0.80",
            "Block the 0.70-0.80 price bucket",
            lambda r: price_bucket(entry_price(r)) != "0.70-0.80",
        ),
        _row_filter_spec(
            "block_builder_boost",
            "Block builder_boost council path",
            lambda r: council_path(r) != "builder_boost",
        ),
        _row_filter_spec(
            "only_critic_caution",
            "Keep only critic_caution council path",
            lambda r: council_path(r) == "critic_caution",
        ),
        _row_filter_spec(
            "block_edge_0.10_plus",
            "Block rows with reported edge bucket 0.10+",
            lambda r: edge_bucket(risk_edge(r)) != "0.10+",
        ),
    ]

    specs.extend(
        [
            _cell_allowlist_spec(
                "discovery_cells_positive_pnl",
                "Freeze discovery cells with n>=5 and positive economic PnL",
                discovery_rows,
                lambda s: s.get("sample_ge_5") and (s.get("total_economic_pnl") or 0.0) > 0,
            ),
            _cell_allowlist_spec(
                "discovery_cells_be_margin_2pp",
                "Freeze discovery cells with n>=5 and win rate at least 2pp above breakeven",
                discovery_rows,
                lambda s: s.get("sample_ge_5") and (s.get("wr_margin") or 0.0) >= 0.02,
            ),
            _cell_allowlist_spec(
                "discovery_cells_roi_positive",
                "Freeze discovery cells with n>=5 and positive ROI",
                discovery_rows,
                lambda s: s.get("sample_ge_5") and (s.get("roi") or 0.0) > 0,
            ),
            _cell_allowlist_spec(
                "discovery_cells_pf_gt_1",
                "Freeze discovery cells with n>=5 and PF > 1.0",
                discovery_rows,
                lambda s: s.get("sample_ge_5") and (s.get("profit_factor") or 0.0) > 1.0,
            ),
            _cell_allowlist_spec(
                "discovery_cells_positive_clv",
                "Freeze discovery cells with n>=5 and positive CLV",
                discovery_rows,
                lambda s: s.get("sample_ge_5") and s.get("avg_clv") is not None and (s.get("avg_clv") or 0.0) > 0,
            ),
            _cell_allowlist_spec(
                "discovery_cells_strict_combined",
                "Freeze discovery cells that clear all strict quality checks",
                discovery_rows,
                lambda s: (
                    s.get("sample_ge_5")
                    and (s.get("total_economic_pnl") or 0.0) > 0
                    and (s.get("roi") or 0.0) > 0
                    and (s.get("profit_factor") or 0.0) > 1.0
                    and (s.get("wr_margin") or 0.0) >= 0.02
                    and s.get("avg_clv") is not None
                    and (s.get("avg_clv") or 0.0) > 0
                ),
            ),
        ]
    )
    return specs


def _candidate_status(result: CandidateResult, baseline_validation: dict[str, Any]) -> str:
    val = result.validation_summary
    if val["n"] == 0 or baseline_validation["n"] == 0:
        return "DISCOVERY_ONLY"
    if baseline_validation["n"] < MIN_SHADOW_SAMPLE or val["n"] < MIN_SHADOW_SAMPLE:
        return "SHADOW_VALIDATION_TOO_SMALL"

    roi = val.get("roi")
    base_roi = baseline_validation.get("roi")
    pf = val.get("profit_factor")
    base_pf = baseline_validation.get("profit_factor")
    pnl = val.get("total_economic_pnl")
    base_pnl = baseline_validation.get("total_economic_pnl")
    wr_margin = val.get("wr_margin")
    base_wr_margin = baseline_validation.get("wr_margin")

    improved = (
        roi is not None
        and base_roi is not None
        and roi > base_roi
        and pf is not None
        and base_pf is not None
        and pf > base_pf
        and pnl is not None
        and base_pnl is not None
        and pnl > base_pnl
        and wr_margin is not None
        and base_wr_margin is not None
        and wr_margin > base_wr_margin
    )

    if improved and val["n"] >= 30 and result.blocked_high_entry_rows > 0:
        return "SHADOW_VALIDATION_READY"
    if improved:
        return "PROMISING_BUT_UNPROVEN"
    return "FAILED_SHADOW_VALIDATION"


def _evaluate_candidate(
    spec: CandidateSpec,
    discovery_rows: list[dict[str, Any]],
    validation_rows: list[dict[str, Any]],
    baseline_validation: dict[str, Any],
) -> CandidateResult:
    discovery_pass = spec.apply(discovery_rows)
    validation_pass = spec.apply(validation_rows)
    discovery_summary = _summary(discovery_pass)
    validation_summary = _summary(validation_pass)

    passed_ids = {_row_identity(r) for r in validation_pass}
    blocked_validation = [r for r in validation_rows if _row_identity(r) not in passed_ids]

    blocked_high_entry = [r for r in blocked_validation if _high_entry_bucket(r)]
    blocked_80_90 = [r for r in blocked_validation if price_bucket(entry_price(r)) == HIGH_ENTRY_BUCKET]
    blocked_builder = [r for r in blocked_validation if council_path(r) == "builder_boost"]
    blocked_edge = [r for r in blocked_validation if edge_bucket(risk_edge(r)) == "0.10+"]
    blocked_paths = Counter(council_path(r) for r in blocked_validation)
    avoided_high_entry_loss = sum(
        -(_as_float(economic_pnl_value(r)) or 0.0)
        for r in blocked_high_entry
        if (_as_float(economic_pnl_value(r)) or 0.0) < 0
    )

    result = CandidateResult(
        spec=spec,
        discovery_pass=discovery_pass,
        validation_pass=validation_pass,
        discovery_summary=discovery_summary,
        validation_summary=validation_summary,
        status="DO_NOT_PATCH_LIVE_YET",
        retained_total_frequency=validation_summary["n"] / baseline_validation["n"] if baseline_validation["n"] else None,
        retained_validation_frequency=validation_summary["n"] / baseline_validation["n"] if baseline_validation["n"] else None,
        discovery_retained_frequency=discovery_summary["n"] / len(discovery_rows) if discovery_rows else None,
        blocked_rows=len(blocked_validation),
        blocked_high_entry_rows=len(blocked_high_entry),
        blocked_high_entry_pnl=sum(_as_float(economic_pnl_value(r)) or 0.0 for r in blocked_high_entry),
        avoided_high_entry_loss=avoided_high_entry_loss,
        blocked_price_80_90_rows=len(blocked_80_90),
        blocked_builder_boost_rows=len(blocked_builder),
        blocked_edge_0_10_plus_rows=len(blocked_edge),
        blocked_council_path_rows=dict(blocked_paths),
    )
    object.__setattr__(result, "status", _candidate_status(result, baseline_validation))
    return result


def build_report_state(records: list[dict[str, Any]]) -> dict[str, Any]:
    fresh = fresh_proof_rows(records)
    discovery_rows, validation_rows, cutoff_ts = split_discovery_validation(fresh)
    baseline_all = _summary(fresh)
    baseline_discovery = _summary(discovery_rows)
    baseline_validation = _summary(validation_rows)
    specs = build_candidate_specs(discovery_rows)
    candidate_results = [
        _evaluate_candidate(spec, discovery_rows, validation_rows, baseline_validation)
        for spec in specs
    ]

    excluded_total = len(records) - len(fresh)
    exclusion_counts = {
        "kxeth_or_quarantined": sum(1 for r in records if is_kxeth_or_quarantined(r)),
        "data_collection_override": sum(1 for r in records if bool(r.get("data_collection_override"))),
        "bootstrap_provisional": sum(1 for r in records if bool(r.get("bootstrap_provisional"))),
        "side_coverage": sum(1 for r in records if bool(r.get("side_coverage_test"))),
        "open_rows": sum(1 for r in records if str(r.get("status") or "").upper() == "OPEN"),
        "legacy_or_unversioned": sum(1 for r in records if not r.get("accounting_version")),
        "missing_entry_price": sum(1 for r in records if entry_price(r) is None),
        "missing_economic_pnl": sum(1 for r in records if economic_pnl_value(r) is None),
    }

    top_candidate = max(
        candidate_results,
        key=lambda r: (
            _sort_metric(r.discovery_summary.get("roi")),
            _sort_metric(r.discovery_summary.get("profit_factor")),
            _sort_metric(r.discovery_summary.get("total_economic_pnl")),
        ),
        default=None,
    )
    if baseline_validation["n"] == 0:
        overall_status = "DISCOVERY_ONLY"
    elif baseline_validation["n"] < MIN_SHADOW_SAMPLE:
        overall_status = "SHADOW_VALIDATION_TOO_SMALL"
    elif any(r.status == "SHADOW_VALIDATION_READY" for r in candidate_results):
        overall_status = "SHADOW_VALIDATION_READY"
    elif any(r.status == "PROMISING_BUT_UNPROVEN" for r in candidate_results):
        overall_status = "PROMISING_BUT_UNPROVEN"
    elif all(r.status == "FAILED_SHADOW_VALIDATION" for r in candidate_results):
        overall_status = "FAILED_SHADOW_VALIDATION"
    else:
        overall_status = "DO_NOT_PATCH_LIVE_YET"

    return {
        "raw_records": records,
        "fresh_rows": fresh,
        "discovery_rows": discovery_rows,
        "validation_rows": validation_rows,
        "cutoff_ts": cutoff_ts,
        "baseline_all": baseline_all,
        "baseline_discovery": baseline_discovery,
        "baseline_validation": baseline_validation,
        "candidate_results": candidate_results,
        "top_candidate": top_candidate,
        "overall_status": overall_status,
        "excluded_total": excluded_total,
        "exclusion_counts": exclusion_counts,
    }


def _print_summary(label: str, summary: dict[str, Any]) -> None:
    print(f"\n{label}")
    print("-" * 78)
    print(f"  rows:                 {summary['n']}")
    print(f"  wins / losses:        {summary['wins']} / {summary['losses']}")
    print(f"  win rate:             {_fmt_pct(summary['win_rate'])}")
    print(f"  breakeven wr:         {_fmt_pct(summary['breakeven_wr'])}")
    print(f"  wr margin:            {_fmt_pct(summary['wr_margin'])}")
    print(f"  economic pnl:         {_fmt_money(summary['total_economic_pnl'])}")
    print(f"  ROI:                  {_fmt_pct(summary['roi'])}")
    print(f"  profit factor:        {_fmt_num(summary['profit_factor'])}")
    print(f"  avg entry price:      {_fmt_num(summary['avg_entry_price'])}")
    print(f"  avg win / avg loss:   {_fmt_money(summary['avg_win'])} / {_fmt_money(summary['avg_loss'])}")
    print(f"  reward / risk:        {_fmt_num(summary['reward_risk'])}")
    print(f"  avg CLV:              {_fmt_num(summary.get('avg_clv'))}")
    print(f"  max drawdown:         {_fmt_money(summary.get('max_drawdown'))}")
    print(f"  high-entry rows:      {summary.get('high_entry_rows', 0)}")
    print(f"  high-entry capital:   {_fmt_money(summary.get('high_entry_capital'))}")
    print(f"  high-entry pnl:       {_fmt_money(summary.get('high_entry_pnl'))}")
    print(f"  high-entry losses:    {summary.get('high_entry_losses', 0)}")


def _print_bucket_table(title: str, buckets: dict[str, Any], order: Iterable[str]) -> None:
    print(f"\n{title}")
    print("-" * 118)
    print("bucket              n      WR      BE     mrg        PnL      ROI      PF      RR   avg_ep          tag")
    print("-" * 118)
    for key in order:
        stats = buckets.get(key)
        if not stats or stats.get("n", 0) == 0:
            continue
        tag = "POS" if (stats.get("total_economic_pnl") or 0) > 0 else "POISON" if (stats.get("total_economic_pnl") or 0) < 0 else "NEUTRAL"
        print(
            f"{key:<18} {stats['n']:>4}  "
            f"{_fmt_pct(stats['win_rate']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_pct(stats['breakeven_wr']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_pct(stats['wr_margin']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_money(stats['total_economic_pnl']):>9}  "
            f"{_fmt_pct(stats['roi']).replace('MISSING', 'n/a'):>7}  "
            f"{_fmt_num(stats['profit_factor']):>6}  "
            f"{_fmt_num(stats['reward_risk']):>6}  "
            f"{_fmt_num(stats['avg_entry_price'], 4):>8}  {tag:>12}"
        )


def _print_edge_bucket_counts(summary: dict[str, Any]) -> None:
    print("\nEDGE BUCKET PERFORMANCE")
    print("-" * 90)
    print("bucket      n      WR      BE     mrg        PnL      ROI      PF      RR")
    print("-" * 90)
    for key in VALID_EDGE_BUCKETS:
        stats = summary["edge_bucket_counts"].get(key)
        if not stats:
            continue
        cell_rows = [r for r in summary.get("rows", []) if edge_bucket(risk_edge(r)) == key]
        cell_sum = _summary(cell_rows)
        print(
            f"{key:<10} {cell_sum['n']:>4}  {_fmt_pct(cell_sum['win_rate']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_pct(cell_sum['breakeven_wr']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_pct(cell_sum['wr_margin']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_money(cell_sum['total_economic_pnl']):>9}  "
            f"{_fmt_pct(cell_sum['roi']).replace('MISSING', 'n/a'):>7}  "
            f"{_fmt_num(cell_sum['profit_factor']):>6}  {_fmt_num(cell_sum['reward_risk']):>6}"
        )


def _print_cell_table(rows: list[dict[str, Any]], title: str) -> None:
    cells = summarize_cells(rows)
    print(f"\n{title}")
    print("-" * 132)
    print("cell                     n      WR      BE     mrg        PnL      ROI      PF      RR   avg_ep          tag")
    print("-" * 132)
    for key, stats in sorted(cells.items(), key=lambda item: (-item[1]["n"], item[0])):
        if stats["n"] == 0:
            continue
        tag = "POS" if (stats.get("total_economic_pnl") or 0) > 0 else "POISON" if (stats.get("total_economic_pnl") or 0) < 0 else "NEUTRAL"
        print(
            f"{key:<24} {stats['n']:>4}  {_fmt_pct(stats['win_rate']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_pct(stats['breakeven_wr']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_pct(stats['wr_margin']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_money(stats['total_economic_pnl']):>9}  "
            f"{_fmt_pct(stats['roi']).replace('MISSING', 'n/a'):>7}  "
            f"{_fmt_num(stats['profit_factor']):>6}  "
            f"{_fmt_num(stats['reward_risk']):>6}  "
            f"{_fmt_num(stats['avg_entry_price'], 4):>8}  {tag:>12}"
        )


def _print_candidate_table(results: list[CandidateResult]) -> None:
    print("\nCANDIDATE FILTER SHADOW VALIDATION")
    print("-" * 180)
    print(
        "filter                              disc_n  disc_roi  disc_pf   val_n   val_roi   val_pf   keep%  block90  blockB  avoidH  status"
    )
    print("-" * 180)
    for result in sorted(
        results,
        key=lambda r: (
            _sort_metric(r.discovery_summary.get("roi")),
            _sort_metric(r.validation_summary.get("roi")),
            _sort_metric(r.validation_summary.get("total_economic_pnl")),
        ),
        reverse=True,
    ):
        disc = result.discovery_summary
        val = result.validation_summary
        keep_pct = result.retained_validation_frequency if result.retained_validation_frequency is not None else None
        print(
            f"{result.spec.name:<34} "
            f"{disc['n']:>6}  {_fmt_pct(disc['roi']):>8}  {_fmt_num(disc['profit_factor']):>7}  "
            f"{val['n']:>6}  {_fmt_pct(val['roi']):>8}  {_fmt_num(val['profit_factor']):>7}  "
            f"{_fmt_pct(keep_pct):>6}  "
            f"{result.blocked_price_80_90_rows:>7}  {result.blocked_builder_boost_rows:>6}  "
            f"{_fmt_money(result.avoided_high_entry_loss):>6}  "
            f"{result.status}"
        )


def render_report(state: dict[str, Any]) -> None:
    fresh = state["fresh_rows"]
    discovery = state["discovery_rows"]
    validation = state["validation_rows"]
    print("=" * 78)
    print("SHADOW CANDIDATE FILTER VALIDATION")
    print("=" * 78)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print("Purpose: validate candidate filters on a frozen discovery slice and a newer shadow slice.")
    print(f"Raw records loaded: {len(state['raw_records'])}")
    print(f"Fresh proof rows:    {len(fresh)}")
    print(f"Discovery rows:      {len(discovery)}")
    print(f"Validation rows:     {len(validation)}")
    print(f"Discovery cutoff:    {state['cutoff_ts'] or 'MISSING'}")
    print(f"Overall status:      {state['overall_status']}")

    print("\nEXCLUSIONS FROM FRESH PROOF")
    print("-" * 78)
    print(f"  excluded total (all reasons overlap): {state['excluded_total']}")
    for key, value in state["exclusion_counts"].items():
        print(f"  {key:<28}: {value}")

    state["baseline_all"]["rows"] = fresh
    state["baseline_discovery"]["rows"] = discovery
    state["baseline_validation"]["rows"] = validation

    _print_summary("BASELINE (ALL FRESH ROWS)", state["baseline_all"])
    _print_summary("DISCOVERY WINDOW", state["baseline_discovery"])
    _print_summary("SHADOW VALIDATION WINDOW", state["baseline_validation"])

    _print_bucket_table(
        "PRICE BUCKETS — SHADOW VALIDATION WINDOW",
        {
            bucket: _summary([r for r in validation if price_bucket(entry_price(r)) == bucket])
            for bucket in VALID_PRICE_BUCKETS
        },
        VALID_PRICE_BUCKETS,
    )
    _print_edge_bucket_counts(state["baseline_validation"])
    _print_cell_table(validation, "2D EDGE × PRICE — SHADOW VALIDATION WINDOW")

    council_groups = {name: _summary([r for r in validation if council_path(r) == name]) for name in ("builder_boost", "critic_caution", "bootstrap_era_allow", "other")}
    print("\nCOUNCIL PATH PERFORMANCE — SHADOW VALIDATION WINDOW")
    print("-" * 118)
    print("path              n      WR      BE     mrg        PnL      ROI      PF      RR   avg_ep          tag")
    print("-" * 118)
    for key in ("builder_boost", "critic_caution", "bootstrap_era_allow", "other"):
        stats = council_groups[key]
        if stats["n"] == 0:
            continue
        tag = "POS" if (stats.get("total_economic_pnl") or 0) > 0 else "POISON" if (stats.get("total_economic_pnl") or 0) < 0 else "NEUTRAL"
        print(
            f"{key:<16} {stats['n']:>4}  {_fmt_pct(stats['win_rate']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_pct(stats['breakeven_wr']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_pct(stats['wr_margin']).replace('MISSING', 'n/a'):>6}  "
            f"{_fmt_money(stats['total_economic_pnl']):>9}  "
            f"{_fmt_pct(stats['roi']).replace('MISSING', 'n/a'):>7}  "
            f"{_fmt_num(stats['profit_factor']):>6}  "
            f"{_fmt_num(stats['reward_risk']):>6}  "
            f"{_fmt_num(stats['avg_entry_price'], 4):>8}  {tag:>12}"
        )

    _print_candidate_table(state["candidate_results"])

    print("\nDISCOVERY-FROZEN CELL ALLOWLISTS")
    print("-" * 118)
    for result in state["candidate_results"]:
        if not result.spec.frozen_cells:
            continue
        allowed = ", ".join(sorted(result.spec.frozen_cells)[:6]) if result.spec.frozen_cells else "none"
        suffix = "" if len(result.spec.frozen_cells) <= 6 else f" ... (+{len(result.spec.frozen_cells) - 6} more)"
        print(
            f"  {result.spec.name:<32} allowed_cells={len(result.spec.frozen_cells):>3}  "
            f"discovery_n={result.discovery_summary['n']:>3}  validation_n={result.validation_summary['n']:>3}  "
            f"status={result.status}"
        )
        print(f"    {allowed}{suffix}")

    top = state["top_candidate"]
    if top is not None:
        print("\nTOP DISCOVERY CANDIDATE")
        print("-" * 78)
        print(f"  name:             {top.spec.name}")
        print(f"  discovery ROI:    {_fmt_pct(top.discovery_summary.get('roi'))}")
        print(f"  validation ROI:   {_fmt_pct(top.validation_summary.get('roi'))}")
        print(f"  validation PF:    {_fmt_num(top.validation_summary.get('profit_factor'))}")
        print(f"  validation PnL:   {_fmt_money(top.validation_summary.get('total_economic_pnl'))}")
        print(f"  status:           {top.status}")
        print(f"  keep frequency:   {_fmt_pct(top.retained_validation_frequency)}")

    print("\nRECOMMENDATION")
    print("-" * 78)
    if state["overall_status"] == "SHADOW_VALIDATION_TOO_SMALL":
        print("  validation slice is too small to trust; do not patch live strategy.")
    elif state["overall_status"] == "FAILED_SHADOW_VALIDATION":
        print("  candidate filters failed shadow validation; do not patch live strategy.")
    elif state["overall_status"] == "PROMISING_BUT_UNPROVEN":
        print("  some filters look directionally better, but the shadow sample is not strong enough to patch live.")
    elif state["overall_status"] == "SHADOW_VALIDATION_READY":
        print("  a candidate looks strong in shadow, but live patching is still deferred until a dedicated promotion phase.")
    else:
        print("  discovery-only signal remains. Do not patch live strategy yet.")
    print("  what not to touch: thresholds, gates, KXETH quarantine, Kelly, scale, or real money.")
    print(
        "  overfitting warning: any discovery-frozen cell list must survive validation on new rows before it is treated as a live candidate."
    )
    print(
        "  why high WR can still fail: if validation breakeven stays above WR, the payoff asymmetry is still wrong."
    )
    print()
    print(f"Sentinel: {SENTINEL}")
    print("=" * 78)


def main() -> None:
    records = load_raw_trades(TRADES_LOG)
    state = build_report_state(records)
    render_report(state)


if __name__ == "__main__":
    main()
