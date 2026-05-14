#!/usr/bin/env python3
"""Tests for Phase 10G payoff-aware shadow forward validation."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import trading_config  # noqa: E402
from logs.payoff_aware_shadow_ranking_logger import (  # noqa: E402
    build_shadow_row,
    log_payoff_aware_shadow_ranking,
    strict_payoff_allowed,
)
from tools import report_payoff_aware_shadow_forward_validation as report  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))


def _opp(**overrides: Any) -> dict[str, Any]:
    row = {
        "ticker": "KXBTCD-TEST",
        "action": "BET_YES",
        "confidence": 0.86,
        "edge": 0.04,
        "yes_bid": 0.81,
        "yes_ask": 0.82,
        "price_yes": 0.82,
        "no_bid": 0.18,
        "no_ask": 0.19,
        "price_no": 0.19,
    }
    row.update(overrides)
    return row


def test_empty_scan_safe_and_shadow_only() -> str | None:
    row = build_shadow_row([], scan_id="s1", run_id="r1")
    if row["candidate_count"] != 0:
        return "empty scan should have zero candidates"
    if row["shadow_only"] is not True:
        return "shadow_only flag missing"
    if row["execution_changed"] is not False:
        return "logger claims execution changed"
    if row["strict_starvation_count"] != 3:
        return "empty strict mode should starve all three slots"
    return None


def test_missing_fields_safe() -> str | None:
    row = build_shadow_row([{"ticker": "MISSING", "action": "BET_YES"}], scan_id="s1", run_id="r1")
    if row["candidate_count"] != 1:
        return "missing-field opportunity was not counted"
    if row["payoff_aware_top_3"]:
        return "missing-field row should not receive payoff score"
    return None


def test_strict_mode_can_starve() -> str | None:
    opportunities = [
        _opp(ticker="A", yes_ask=0.90, price_yes=0.90, confidence=0.91),
        _opp(ticker="B", yes_ask=0.88, price_yes=0.88, confidence=0.89),
    ]
    row = build_shadow_row(opportunities, scan_id="s1", run_id="r1")
    if row["strict_starvation_count"] <= 0:
        return "strict mode should starve weak payoff candidates"
    return None


def test_strict_exception_and_block_rules() -> str | None:
    below_be = _opp(yes_ask=0.82, price_yes=0.82, confidence=0.80)
    weak_rr = _opp(yes_ask=0.86, price_yes=0.86, confidence=0.91)
    exceptional = _opp(yes_ask=0.84, price_yes=0.84, confidence=0.96)
    if strict_payoff_allowed(below_be):
        return "strict allowed model probability below breakeven"
    if strict_payoff_allowed(weak_rr):
        return "strict allowed weak reward/risk without exceptional signal"
    if not strict_payoff_allowed(exceptional):
        return "strict blocked exceptional candidate"
    return None


def test_logger_writes_temp_path_only() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "shadow.jsonl"
        result = log_payoff_aware_shadow_ranking([_opp()], scan_id="s1", run_id="r1", path=path)
        if result["written"] != 1 or result["errors"] != 0:
            return "temp logger write failed"
        rows = report.read_shadow_rows(path)
        if len(rows) != 1:
            return "report could not read temp shadow row"
        if rows[0].get("execution_changed") is not False:
            return "temp shadow row changed execution"
    return None


def test_report_handles_no_outcomes() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shadow = tmp / "shadow.jsonl"
        trades = tmp / "paper.jsonl"
        log_payoff_aware_shadow_ranking([_opp()], scan_id="s1", run_id="r1", path=shadow)
        _write_jsonl(trades, [])
        built = report.build_report(shadow, trades)
        if built["rows_logged"] != 1:
            return "report did not count shadow rows"
        if built["evidence_maturity"]["mature_outcomes"] is not False:
            return "empty paper log should not be mature"
        if built["settled_matches"]["current"]["matched_rows"] != 0:
            return "empty paper log should have no matches"
    return None


def test_safety_locks() -> str | None:
    if trading_config.TRADING_MODE != "PAPER":
        return "TRADING_MODE changed"
    if not trading_config.GLOBAL_FORCED_LEARNING_MODE:
        return "Kelly/forced learning guard changed"
    if trading_config.DATA_COLLECTION_OVERRIDE_ENABLED:
        return "dc override changed"
    if "KXETH" not in {str(prefix).upper() for prefix in trading_config.QUARANTINED_TICKER_PREFIXES}:
        return "KXETH quarantine missing"
    with tempfile.TemporaryDirectory() as tmpdir:
        shadow = Path(tmpdir) / "missing_shadow.jsonl"
        trades = Path(tmpdir) / "missing_paper.jsonl"
        built = report.build_report(shadow, trades)
        safety = built["safety"]
        if safety["real_money_allowed"] is not False:
            return "report claims real money allowed"
        if safety["scale_allowed"] is not False:
            return "report claims scale allowed"
        if safety["kxeth_quarantine_active"] is not True:
            return "report lost KXETH quarantine"
    return None


def run_tests() -> int:
    tests = [
        test_empty_scan_safe_and_shadow_only,
        test_missing_fields_safe,
        test_strict_mode_can_starve,
        test_strict_exception_and_block_rules,
        test_logger_writes_temp_path_only,
        test_report_handles_no_outcomes,
        test_safety_locks,
    ]
    failures: list[str] = []
    for test in tests:
        result = test()
        if result:
            failures.append(f"{test.__name__}: {result}")
        else:
            print(f"[PASS] {test.__name__}")
    if failures:
        print("\nFAILURES")
        for failure in failures:
            print(f"[FAIL] {failure}")
        return 1
    print("\nSentinel: PAYOFF_AWARE_SHADOW_FORWARD_VALIDATION_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
