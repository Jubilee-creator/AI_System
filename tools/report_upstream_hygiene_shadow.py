#!/usr/bin/env python3
"""
Phase 10P - Upstream Hygiene Shadow Report
Sentinel: UPSTREAM_HYGIENE_SHADOW_REPORT_OK

Read-only report for upstream hygiene shadow rows. If the runtime shadow log is
not populated yet, it creates an in-memory report-only projection from recent
execution funnel rows and does not write logs.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logs.upstream_hygiene_shadow_logger import LOG_PATH, build_shadow_row  # noqa: E402
from tools.report_candidate_to_paper_forward_blockers import FUNNEL_LOG, SHADOW_START  # noqa: E402

SENTINEL = "UPSTREAM_HYGIENE_SHADOW_REPORT_OK"


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _rows_after(rows: list[dict[str, Any]], start: datetime = SHADOW_START) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        ts = _parse_ts(row.get("timestamp_utc") or row.get("timestamp"))
        if ts is not None and ts >= start:
            selected.append(row)
    return selected


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


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    rows = sorted(rows, key=lambda row: _parse_ts(row.get("timestamp_utc")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows[-1]


def _projection_from_funnel(funnel_path: Path) -> dict[str, Any] | None:
    rows = _rows_after(_read_jsonl(funnel_path))
    if not rows:
        return None
    return build_shadow_row(rows, scan_id="REPORT_ONLY_EXECUTION_FUNNEL_PROJECTION", run_id="REPORT_ONLY")


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    variant_values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    variant_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        variants = row.get("variants") or {}
        for name, variant in variants.items():
            if not isinstance(variant, dict):
                continue
            variant_counts[name] += 1
            for key in ("candidate_count", "removed_count", "quality_score", "avg_entry", "avg_reward_risk", "avg_model_margin", "quarantine_count", "weak_reward_risk_count", "expensive_80_90_count", "market_quality_estimate"):
                value = variant.get(key)
                if isinstance(value, (int, float)):
                    variant_values[name][key].append(float(value))
    return {
        name: {
            "rows": variant_counts[name],
            **{key: _avg(values) for key, values in fields.items()},
        }
        for name, fields in variant_values.items()
    }


def build_report(log_path: Path = LOG_PATH, funnel_path: Path = FUNNEL_LOG) -> dict[str, Any]:
    runtime_rows = _read_jsonl(log_path)
    latest_runtime = _latest(runtime_rows)
    projection = None if latest_runtime else _projection_from_funnel(funnel_path)
    active_row = latest_runtime or projection
    source = "runtime_shadow_log" if latest_runtime else "report_only_execution_funnel_projection" if projection else "none"
    rows_for_aggregate = runtime_rows if runtime_rows else ([projection] if projection else [])
    return {
        "shadow_log_path": str(log_path),
        "runtime_shadow_rows": len(runtime_rows),
        "source": source,
        "latest_row": active_row,
        "aggregate": _aggregate(rows_for_aggregate),
        "wired_runtime_expected": True,
        "execution_changed": False,
        "live_strategy_mutated": False,
        "live_deployable": False,
    }


def _print_variant(name: str, variant: dict[str, Any]) -> None:
    print(
        f"  {name}: candidates={_fmt_int(variant.get('candidate_count'))} "
        f"removed={_fmt_int(variant.get('removed_count'))} "
        f"removal_rate={_fmt_pct(variant.get('removal_rate'))} "
        f"quality={_fmt_num(variant.get('quality_score'), 2)} "
        f"entry={_fmt_num(variant.get('avg_entry'))} "
        f"rr={_fmt_num(variant.get('avg_reward_risk'))} "
        f"margin={_fmt_num(variant.get('avg_model_margin'))} "
        f"quarantine={_fmt_int(variant.get('quarantine_count'))} "
        f"weak_rr={_fmt_int(variant.get('weak_reward_risk_count'))} "
        f"expensive={_fmt_int(variant.get('expensive_80_90_count'))} "
        f"mq_est={_fmt_int(variant.get('market_quality_estimate'))} "
        f"starvation={variant.get('starvation_risk')} "
        f"class={variant.get('safety_classification')} "
        f"live_deployable={variant.get('live_deployable')}"
    )


def print_report(report: dict[str, Any]) -> None:
    print("=== Upstream Hygiene Shadow Report (Phase 10P) ===")
    print(f"shadow_log_path: {report['shadow_log_path']}")
    print(f"runtime_shadow_rows: {_fmt_int(report['runtime_shadow_rows'])}")
    print(f"source: {report['source']}")
    print(f"wired_runtime_expected: {report['wired_runtime_expected']}")
    print(f"execution_changed: {report['execution_changed']}")
    print(f"live_strategy_mutated: {report['live_strategy_mutated']}")
    print(f"live_deployable: {report['live_deployable']}")
    row = report.get("latest_row")
    if not row:
        print("latest_row: NONE")
    else:
        print(f"latest_timestamp: {row.get('timestamp_utc')}")
        print(f"scan_id: {row.get('scan_id')} run_id: {row.get('run_id')}")
        print(f"note: {row.get('note')}")
        print("\nLatest variant comparison:")
        variants = row.get("variants") or {}
        for name in ("current", "stack1_quarantine_only", "research_variant_weak_rr", "research_variant_expensive_entry", "aggressive_stack"):
            variant = variants.get(name)
            if isinstance(variant, dict):
                _print_variant(name, variant)
    print("\nAggregate averages:")
    if report["aggregate"]:
        for name, stats in report["aggregate"].items():
            print(
                f"  {name}: rows={_fmt_int(stats.get('rows'))} "
                f"avg_candidates={_fmt_num(stats.get('candidate_count'))} "
                f"avg_removed={_fmt_num(stats.get('removed_count'))} "
                f"avg_quality={_fmt_num(stats.get('quality_score'), 2)}"
            )
    else:
        print("  NONE")
    print(SENTINEL)


def main() -> None:
    print_report(build_report())


if __name__ == "__main__":
    main()
