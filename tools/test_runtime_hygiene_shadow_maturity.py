#!/usr/bin/env python3
"""
tools/test_runtime_hygiene_shadow_maturity.py
----------------------------------------------
Phase 10R — Test suite for report_runtime_hygiene_shadow_maturity.py

Uses synthetic in-memory rows and temporary files only.
Never reads or modifies the real shadow log or paper_trades.jsonl.

Sentinel: PROVEN_RUNTIME_HYGIENE_SHADOW_MATURITY_OK
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.report_runtime_hygiene_shadow_maturity import (
    THRESHOLD_DEVELOPING,
    THRESHOLD_EARLY,
    THRESHOLD_IMMATURE,
    aggregate_variants,
    attempt_outcome_matching,
    build_report,
    check_safety_violations,
    freshness_label,
    is_projection_row,
    maturity_label,
    recommendation_labels,
    safety_class_distribution,
    split_rows,
    starvation_distribution,
    verdict_label,
)

SENTINEL = "PROVEN_RUNTIME_HYGIENE_SHADOW_MATURITY_OK"
NOW_UTC = datetime.now(timezone.utc)


# ── Synthetic row builder ─────────────────────────────────────────────────────

def _ts(offset_minutes: float = 0.0) -> str:
    return (NOW_UTC - timedelta(minutes=offset_minutes)).isoformat()


def _make_variant(
    name: str,
    candidate_count: int = 6,
    removed_count: int = 0,
    quality_score: float = 70.0,
    avg_entry: float = 0.82,
    avg_rr: float = 0.22,
    avg_margin: float = 0.05,
    quarantine_count: int = 1,
    weak_rr_count: int = 3,
    expensive_count: int = 2,
    mq_estimate: int = 0,
    min_edge_estimate: int = 1,
    starvation_risk: str = "LOW",
    safety_classification: str = "BASELINE",
    live_deployable: bool = False,
    shadow_only: bool = True,
) -> dict[str, Any]:
    return {
        "name":                  name,
        "candidate_count":       candidate_count,
        "removed_count":         removed_count,
        "removal_rate":          removed_count / max(candidate_count + removed_count, 1),
        "avg_entry":             avg_entry,
        "avg_reward_risk":       avg_rr,
        "avg_model_margin":      avg_margin,
        "quality_score":         quality_score,
        "quarantine_count":      quarantine_count,
        "weak_reward_risk_count":weak_rr_count,
        "expensive_80_90_count": expensive_count,
        "market_quality_estimate": mq_estimate,
        "min_edge_estimate":     min_edge_estimate,
        "starvation_risk":       starvation_risk,
        "safety_classification": safety_classification,
        "live_deployable":       live_deployable,
        "shadow_only":           shadow_only,
    }


def _make_runtime_row(
    scan_num: int = 1,
    run_id: str = "dashboard_run_20260514_070854",
    timestamp_offset_min: float = 0.0,
    execution_changed: bool = False,
    live_strategy_mutated: bool = False,
    live_deployable: bool = False,
    shadow_only: bool = True,
    note: str = "SHADOW_ONLY_NOT_EXECUTION",
    include_variants: bool = True,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp_utc":       _ts(timestamp_offset_min),
        "run_id":              run_id,
        "scan_id":             f"dashboard_scan_{scan_num}",
        "shadow_only":         shadow_only,
        "execution_changed":   execution_changed,
        "live_strategy_mutated": live_strategy_mutated,
        "live_deployable":     live_deployable,
        "note":                note,
        "starvation_risk": {
            "current":                       "LOW",
            "stack1_quarantine_only":        "LOW",
            "research_variant_weak_rr":      "MEDIUM",
            "research_variant_expensive_entry": "MEDIUM",
            "aggressive_stack":              "MEDIUM_HIGH",
        },
        "safety_classification": {
            "current":                       "BASELINE",
            "stack1_quarantine_only":        "SAFE_TO_SHADOW_TEST",
            "research_variant_weak_rr":      "PROMISING_BUT_NEEDS_MORE_DATA",
            "research_variant_expensive_entry": "PROMISING_BUT_NEEDS_MORE_DATA",
            "aggressive_stack":              "RESEARCH_ONLY_AGGRESSIVE",
        },
    }
    if include_variants:
        row["variants"] = {
            "current": _make_variant(
                "current",
                candidate_count=7, quality_score=72.0,
                quarantine_count=2, weak_rr_count=4, expensive_count=3,
                safety_classification="BASELINE",
            ),
            "stack1_quarantine_only": _make_variant(
                "stack1_quarantine_only",
                candidate_count=5, removed_count=2, quality_score=81.0,
                quarantine_count=0, weak_rr_count=3, expensive_count=2,
                safety_classification="SAFE_TO_SHADOW_TEST",
            ),
            "research_variant_weak_rr": _make_variant(
                "research_variant_weak_rr",
                candidate_count=3, removed_count=4, quality_score=93.0,
                quarantine_count=1, weak_rr_count=0, expensive_count=0,
                starvation_risk="MEDIUM",
                safety_classification="PROMISING_BUT_NEEDS_MORE_DATA",
            ),
            "research_variant_expensive_entry": _make_variant(
                "research_variant_expensive_entry",
                candidate_count=4, removed_count=3, quality_score=88.0,
                quarantine_count=1, weak_rr_count=1, expensive_count=0,
                starvation_risk="MEDIUM",
                safety_classification="PROMISING_BUT_NEEDS_MORE_DATA",
            ),
            "aggressive_stack": _make_variant(
                "aggressive_stack",
                candidate_count=1, removed_count=6, quality_score=100.0,
                quarantine_count=0, weak_rr_count=0, expensive_count=0,
                starvation_risk="MEDIUM_HIGH",
                safety_classification="RESEARCH_ONLY_AGGRESSIVE",
            ),
        }
    return row


def _make_projection_row() -> dict[str, Any]:
    row = _make_runtime_row()
    row["run_id"]  = "REPORT_ONLY"
    row["scan_id"] = "REPORT_ONLY_EXECUTION_FUNNEL_PROJECTION"
    return row


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# ── Test functions ─────────────────────────────────────────────────────────────

def test_missing_log_no_crash() -> Optional[str]:
    """Missing shadow log must not crash build_report."""
    ghost = Path("/tmp/nonexistent_shadow_9r2.jsonl")
    assert not ghost.exists(), "ghost path unexpectedly exists"
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        trades_path = Path(f.name)
    try:
        report = build_report(log_path=ghost, trades_path=trades_path)
        if report["runtime_rows"] != 0:
            return f"expected 0 runtime_rows for missing log, got {report['runtime_rows']}"
        if report["log_exists"] is not False:
            return "expected log_exists=False for missing log"
    finally:
        trades_path.unlink(missing_ok=True)
    return None


def test_empty_log_no_crash() -> Optional[str]:
    """Empty shadow log must not crash and must return 0 runtime rows."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "shadow.jsonl"
        log_path.write_text("")
        trades_path = Path(tmp) / "trades.jsonl"
        trades_path.write_text("")
        report = build_report(log_path=log_path, trades_path=trades_path)
        if report["runtime_rows"] != 0:
            return f"expected 0 runtime_rows for empty log, got {report['runtime_rows']}"
        if report["maturity"] != "NO_RUNTIME_ROWS":
            return f"expected NO_RUNTIME_ROWS, got {report['maturity']}"
    return None


def test_projection_rows_excluded() -> Optional[str]:
    """Projection rows must not count toward runtime_rows."""
    rows = [_make_projection_row() for _ in range(5)]
    runtime, projection = split_rows(rows)
    if len(runtime) != 0:
        return f"expected 0 runtime rows from 5 projection rows, got {len(runtime)}"
    if len(projection) != 5:
        return f"expected 5 projection rows, got {len(projection)}"
    return None


def test_projection_marker() -> Optional[str]:
    """is_projection_row must correctly identify projection vs runtime rows."""
    if not is_projection_row(_make_projection_row()):
        return "projection row not detected as projection"
    if is_projection_row(_make_runtime_row()):
        return "runtime row falsely detected as projection"
    if not is_projection_row({"run_id": "REPORT_ONLY", "scan_id": "x"}):
        return "REPORT_ONLY run_id not detected"
    if not is_projection_row({"run_id": "x", "scan_id": "REPORT_ONLY_EXECUTION_FUNNEL_PROJECTION"}):
        return "REPORT_ONLY scan_id not detected"
    return None


def test_runtime_rows_counted() -> Optional[str]:
    """Runtime rows must be counted correctly and projected rows excluded."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "shadow.jsonl"
        rows = [_make_runtime_row(i + 1, timestamp_offset_min=float(i)) for i in range(10)]
        rows.append(_make_projection_row())
        _write_jsonl(log_path, rows)
        trades_path = Path(tmp) / "trades.jsonl"
        trades_path.write_text("")
        report = build_report(log_path=log_path, trades_path=trades_path)
        if report["runtime_rows"] != 10:
            return f"expected 10 runtime_rows, got {report['runtime_rows']}"
        if report["projection_rows"] != 1:
            return f"expected 1 projection_row, got {report['projection_rows']}"
    return None


def test_maturity_labels() -> Optional[str]:
    """maturity_label must return the correct label for each threshold."""
    cases = [
        (0,                    "NO_RUNTIME_ROWS"),
        (1,                    "IMMATURE_UNDER_50_ROWS"),
        (THRESHOLD_IMMATURE - 1, "IMMATURE_UNDER_50_ROWS"),
        (THRESHOLD_IMMATURE,   "EARLY_50_TO_100_ROWS"),
        (THRESHOLD_EARLY - 1,  "EARLY_50_TO_100_ROWS"),
        (THRESHOLD_EARLY,      "DEVELOPING_100_TO_300_ROWS"),
        (THRESHOLD_DEVELOPING - 1, "DEVELOPING_100_TO_300_ROWS"),
        (THRESHOLD_DEVELOPING, "MATURE_300_PLUS_ROWS"),
        (9999,                 "MATURE_300_PLUS_ROWS"),
    ]
    for n, expected in cases:
        got = maturity_label(n)
        if got != expected:
            return f"maturity_label({n}): expected {expected!r}, got {got!r}"
    return None


def test_freshness_labels() -> Optional[str]:
    """freshness_label must classify ages correctly."""
    cases = [
        (0.0,    "FRESH"),
        (300.0,  "FRESH"),
        (599.9,  "FRESH"),
        (600.0,  "AGING"),
        (1799.9, "AGING"),
        (1800.0, "STALE"),
        (9999.0, "STALE"),
        (None,   "UNKNOWN"),
    ]
    for age, expected in cases:
        got = freshness_label(age)
        if got != expected:
            return f"freshness_label({age}): expected {expected!r}, got {got!r}"
    return None


def test_stale_logger_flagged() -> Optional[str]:
    """Logger with rows older than STALE_MIN_S must produce LOGGER_STALE verdict."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "shadow.jsonl"
        # 3 hours ago
        old_ts = (NOW_UTC - timedelta(hours=3)).isoformat()
        row = _make_runtime_row(1)
        row["timestamp_utc"] = old_ts
        _write_jsonl(log_path, [row])
        trades_path = Path(tmp) / "trades.jsonl"
        trades_path.write_text("")
        report = build_report(log_path=log_path, trades_path=trades_path)
        if report["verdict"] != "LOGGER_STALE":
            return f"expected LOGGER_STALE for old rows, got {report['verdict']!r}"
        if report["freshness"] != "STALE":
            return f"expected freshness=STALE, got {report['freshness']!r}"
    return None


def test_violation_live_deployable_true() -> Optional[str]:
    """Row-level live_deployable=True must be detected as a violation."""
    row = _make_runtime_row(1, live_deployable=True)
    viol = check_safety_violations([row])
    if viol["live_deployable_violations"] != 1:
        return f"expected 1 live_deployable violation, got {viol['live_deployable_violations']}"
    if viol["all_clear"]:
        return "all_clear should be False when live_deployable=True"
    return None


def test_violation_execution_changed_true() -> Optional[str]:
    """execution_changed=True must be detected as a violation."""
    row = _make_runtime_row(1, execution_changed=True)
    viol = check_safety_violations([row])
    if viol["execution_changed_violations"] != 1:
        return f"expected 1 ec violation, got {viol['execution_changed_violations']}"
    if viol["all_clear"]:
        return "all_clear should be False"
    return None


def test_violation_live_strategy_mutated_true() -> Optional[str]:
    """live_strategy_mutated=True must be detected as a violation."""
    row = _make_runtime_row(1, live_strategy_mutated=True)
    viol = check_safety_violations([row])
    if viol["live_strategy_mutated_violations"] != 1:
        return f"expected 1 lsm violation, got {viol['live_strategy_mutated_violations']}"
    if viol["all_clear"]:
        return "all_clear should be False"
    return None


def test_violation_variant_live_deployable_true() -> Optional[str]:
    """Variant-level live_deployable=True must be detected."""
    row = _make_runtime_row(1)
    row["variants"]["current"]["live_deployable"] = True
    viol = check_safety_violations([row])
    if viol["variant_live_deployable_violations"] < 1:
        return f"expected >=1 variant_live_deployable_violation, got {viol['variant_live_deployable_violations']}"
    if viol["all_clear"]:
        return "all_clear should be False"
    return None


def test_clean_rows_no_violations() -> Optional[str]:
    """Clean rows must produce zero violations."""
    rows = [_make_runtime_row(i + 1, timestamp_offset_min=float(i)) for i in range(10)]
    viol = check_safety_violations(rows)
    if not viol["all_clear"]:
        return f"expected all_clear=True for clean rows, got violations={viol}"
    if viol["total_violations"] != 0:
        return f"expected 0 total_violations, got {viol['total_violations']}"
    return None


def test_variant_aggregation() -> Optional[str]:
    """Aggregate must compute per-variant averages correctly."""
    rows = [_make_runtime_row(i + 1, timestamp_offset_min=float(i)) for i in range(5)]
    agg = aggregate_variants(rows)
    for name in ("current", "stack1_quarantine_only", "research_variant_weak_rr",
                 "research_variant_expensive_entry", "aggressive_stack"):
        vstats = agg.get(name)
        if vstats is None:
            return f"variant {name!r} missing from aggregate"
        if vstats["row_count"] != 5:
            return f"{name} row_count: expected 5, got {vstats['row_count']}"
        if vstats.get("quality_score") is None:
            return f"{name} quality_score is None"
    return None


def test_starvation_distribution_counts() -> Optional[str]:
    """Starvation distribution must count risk labels per variant."""
    rows = [_make_runtime_row(i + 1, timestamp_offset_min=float(i)) for i in range(3)]
    dist = starvation_distribution(rows)
    if "current" not in dist:
        return "current missing from starvation_distribution"
    if dist["current"].get("LOW", 0) != 3:
        return f"expected current LOW=3, got {dist['current']}"
    if dist["aggressive_stack"].get("MEDIUM_HIGH", 0) != 3:
        return f"expected aggressive_stack MEDIUM_HIGH=3, got {dist['aggressive_stack']}"
    return None


def test_no_fake_outcome_matching() -> Optional[str]:
    """Outcome matching must not fabricate matches when no settled trades exist."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "shadow.jsonl"
        _write_jsonl(log_path, [_make_runtime_row(1)])
        trades_path = Path(tmp) / "trades.jsonl"
        trades_path.write_text("")
        report = build_report(log_path=log_path, trades_path=trades_path)
        om = report["outcome_matching"]
        if om.get("settled_after_shadow_start", 0) != 0:
            return f"expected 0 settled, got {om.get('settled_after_shadow_start')}"
        if "NO_OUTCOME_MATCHES_YET" not in om["verdict"]:
            return f"expected NO_OUTCOME_MATCHES_YET, got {om['verdict']!r}"
    return None


def test_settled_outcomes_reported() -> Optional[str]:
    """When settled trades exist after shadow start, outcome matching reports them."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "shadow.jsonl"
        # Shadow row: 2 hours ago
        row = _make_runtime_row(1)
        row["timestamp_utc"] = (NOW_UTC - timedelta(hours=2)).isoformat()
        _write_jsonl(log_path, [row])

        # Trade settled 1 hour ago (after shadow start)
        trades_path = Path(tmp) / "trades.jsonl"
        trade_ts = (NOW_UTC - timedelta(hours=1)).isoformat()
        with trades_path.open("w") as fh:
            fh.write(json.dumps({"timestamp": trade_ts, "status": "SETTLED", "ticker": "FAKE"}) + "\n")

        result = attempt_outcome_matching([row], trades_path)
        if result.get("settled_after_shadow_start", 0) != 1:
            return f"expected 1 settled, got {result.get('settled_after_shadow_start')}"
        if "NO_OUTCOME_MATCHES_YET" in result["verdict"]:
            return f"expected non-NO_OUTCOME verdict when settled trade exists, got {result['verdict']!r}"
    return None


def test_report_is_readonly() -> Optional[str]:
    """build_report must not create or modify any files."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path    = Path(tmp) / "shadow.jsonl"
        trades_path = Path(tmp) / "trades.jsonl"
        rows = [_make_runtime_row(i + 1, timestamp_offset_min=float(i)) for i in range(5)]
        _write_jsonl(log_path, rows)
        trades_path.write_text("")

        before_files = set(Path(tmp).iterdir())
        build_report(log_path=log_path, trades_path=trades_path)
        after_files  = set(Path(tmp).iterdir())

        if after_files != before_files:
            new_files = after_files - before_files
            return f"build_report created unexpected files: {new_files}"
    return None


def test_verdict_labels() -> Optional[str]:
    """verdict_label must produce the correct label for each scenario."""
    cases = [
        # (n, freshness, has_violations, expected)
        (0,   "FRESH",  False, "COLLECTION_NOT_STARTED"),
        (10,  "FRESH",  False, "COLLECTION_ACTIVE_BUT_IMMATURE"),
        (50,  "FRESH",  False, "COLLECTION_ACTIVE_EARLY_SIGNAL_ONLY"),
        (100, "FRESH",  False, "COLLECTION_ACTIVE_DEVELOPING"),
        (300, "FRESH",  False, "COLLECTION_MATURE_NO_OUTCOMES"),
        (50,  "STALE",  False, "LOGGER_STALE"),
        (50,  "FRESH",  True,  "LOGGER_SAFETY_VIOLATION"),
    ]
    for n, freshness, has_viol, expected in cases:
        got = verdict_label(n, freshness, has_viol)
        if got != expected:
            return f"verdict_label({n},{freshness},{has_viol}): expected {expected!r}, got {got!r}"
    return None


def test_no_paptrader_changes() -> Optional[str]:
    """Report must not import or call PaperTrader or any broker module."""
    src  = Path(__file__).resolve().parent / "report_runtime_hygiene_shadow_maturity.py"
    text = src.read_text()
    # Only flag actual import statements or call patterns, not docstring mentions
    forbidden_patterns = [
        "import paper_trader",
        "from brain import",
        "import brain",
        "from brokers import",
        "import brokers",
        ".place_order(",
        ".execute(",
        "requests.post(",
        "requests.put(",
        "requests.delete(",
    ]
    for pattern in forbidden_patterns:
        if pattern in text:
            return f"forbidden import/call pattern found in report source: {pattern!r}"
    return None


def test_live_deployable_always_false() -> Optional[str]:
    """Report-level live_deployable must always be False."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path    = Path(tmp) / "shadow.jsonl"
        trades_path = Path(tmp) / "trades.jsonl"
        _write_jsonl(log_path, [_make_runtime_row(1)])
        trades_path.write_text("")
        report = build_report(log_path=log_path, trades_path=trades_path)
        if report.get("live_deployable") is not False:
            return f"live_deployable must be False, got {report.get('live_deployable')}"
        if report.get("execution_changed") is not False:
            return f"execution_changed must be False, got {report.get('execution_changed')}"
        if report.get("live_strategy_mutated") is not False:
            return f"live_strategy_mutated must be False, got {report.get('live_strategy_mutated')}"
    return None


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("missing log does not crash",               test_missing_log_no_crash),
        ("empty log does not crash",                 test_empty_log_no_crash),
        ("projection rows are excluded from count",  test_projection_rows_excluded),
        ("is_projection_row correctly classifies",   test_projection_marker),
        ("runtime rows are counted correctly",       test_runtime_rows_counted),
        ("maturity_label thresholds are correct",    test_maturity_labels),
        ("freshness_label thresholds are correct",   test_freshness_labels),
        ("stale logger is flagged as LOGGER_STALE",  test_stale_logger_flagged),
        ("live_deployable=True detected",            test_violation_live_deployable_true),
        ("execution_changed=True detected",          test_violation_execution_changed_true),
        ("live_strategy_mutated=True detected",      test_violation_live_strategy_mutated_true),
        ("variant live_deployable=True detected",    test_violation_variant_live_deployable_true),
        ("clean rows produce 0 violations",          test_clean_rows_no_violations),
        ("variant aggregation computes averages",    test_variant_aggregation),
        ("starvation risk distribution correct",     test_starvation_distribution_counts),
        ("no fake outcome matching on empty trades", test_no_fake_outcome_matching),
        ("settled trades reported when present",     test_settled_outcomes_reported),
        ("report is read-only (no file creation)",   test_report_is_readonly),
        ("verdict_label produces correct labels",    test_verdict_labels),
        ("no PaperTrader imports or references",     test_no_paptrader_changes),
        ("live_deployable always False at report level", test_live_deployable_always_false),
    ]

    passed = failed = 0
    print("=" * 68)
    print("RUNTIME HYGIENE SHADOW MATURITY TEST SUITE — Phase 10R")
    print("=" * 68)
    print()
    for name, fn in tests:
        try:
            err = fn()
        except Exception as exc:
            err = f"EXCEPTION: {exc}"
        if err is None:
            print(f"  [PASS]  {name}")
            passed += 1
        else:
            print(f"  [FAIL]  {name}")
            print(f"          {err}")
            failed += 1

    print()
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    print()
    if failed == 0:
        print(SENTINEL)
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
