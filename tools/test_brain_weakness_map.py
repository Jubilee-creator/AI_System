#!/usr/bin/env python3
"""Tests for Phase 10I brain weakness map."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import trading_config  # noqa: E402
from tools import report_brain_weakness_map as report  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))


def _sha(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trade(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "ticker": "KXBTCD-TEST",
        "status": "SETTLED",
        "result": "LOSS",
        "action": "BET_YES",
        "scanner_action": "BET_YES",
        "entry_price": 0.82,
        "size": 5.0,
        "model_probability": 0.86,
        "confidence": 0.86,
        "risk_edge": 0.04,
        "edge": 0.04,
        "yes_bid": 0.81,
        "yes_ask": 0.82,
        "no_bid": 0.18,
        "no_ask": 0.19,
        "economic_pnl": -4.10,
        "recorded_pnl": -4.10,
        "pnl": -4.10,
        "capital_at_risk": 4.10,
        "payout_notional": 5.0,
        "max_profit_if_win": 0.90,
        "max_loss_if_loss": 4.10,
        "accounting_version": "economic_contract_notional_v1",
        "data_collection_override": False,
        "bootstrap_provisional": False,
        "side_coverage": False,
        "side_coverage_test": False,
    }
    row.update(overrides)
    return row


def _funnel(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "run_id": "r1",
        "scan_id": "s1",
        "ticker": "KXBTCD-TEST",
        "scanner_action": "BET_YES",
        "confidence": 0.86,
        "edge": 0.04,
        "yes_bid": 0.81,
        "yes_ask": 0.82,
        "no_bid": 0.18,
        "no_ask": 0.19,
        "spread": 0.01,
        "scan_non_pass_rank": 1,
        "paper_trade_opened": True,
        "final_reason": "TRADE_OPENED",
    }
    row.update(overrides)
    return row


def _shadow_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "run_id": "r1",
        "scan_id": "s1",
        "shadow_only": True,
        "execution_changed": False,
        "current_top_3": [],
        "payoff_aware_top_3": [],
        "strict_payoff_top_3": [],
    }
    row.update(overrides)
    return row


def test_empty_and_missing_files_safe() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        built = report.build_report(
            tmp / "missing_paper.jsonl",
            tmp / "missing_funnel.jsonl",
            tmp / "missing_scanner.jsonl",
            tmp / "missing_shadow.jsonl",
        )
        if built["counts"]["candidate_rows"] != 0:
            return "missing logs should produce zero candidates"
        if built["read_only"] is not True:
            return "report should identify as read-only"
        if len(built["brains"]) != 10:
            return "expected ten brain scores"
    return None


def test_report_is_read_only() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper.jsonl"
        funnel = tmp / "funnel.jsonl"
        scanner = tmp / "scanner.jsonl"
        shadow = tmp / "shadow.jsonl"
        _write_jsonl(trades, [_trade()])
        _write_jsonl(funnel, [_funnel()])
        _write_jsonl(scanner, [])
        _write_jsonl(shadow, [_shadow_row()])
        before = {path: _sha(path) for path in (trades, funnel, scanner, shadow)}
        report.build_report(trades, funnel, scanner, shadow)
        after = {path: _sha(path) for path in (trades, funnel, scanner, shadow)}
        if before != after:
            return "report modified input files"
    return None


def test_negative_roi_flags_probability_weak() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper.jsonl"
        funnel = tmp / "funnel.jsonl"
        scanner = tmp / "scanner.jsonl"
        shadow = tmp / "shadow.jsonl"
        rows = [_trade(timestamp=f"2026-01-01T00:{i:02d}:00+00:00") for i in range(30)]
        _write_jsonl(trades, rows)
        _write_jsonl(funnel, [_funnel()])
        _write_jsonl(scanner, [])
        _write_jsonl(shadow, [_shadow_row()])
        built = report.build_report(trades, funnel, scanner, shadow)
        statuses = {item["name"]: item["status"] for item in built["brains"]}
        if statuses["Probability / confidence brain"] not in {"WEAK", "DANGEROUS"}:
            return "negative ROI did not weaken probability brain"
        if statuses["Data/proof brain"] not in {"WEAK", "DANGEROUS"}:
            return "negative ROI did not weaken data/proof brain"
    return None


def test_shadow_rows_under_30_immature() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper.jsonl"
        funnel = tmp / "funnel.jsonl"
        scanner = tmp / "scanner.jsonl"
        shadow = tmp / "shadow.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(funnel, [])
        _write_jsonl(scanner, [])
        _write_jsonl(shadow, [_shadow_row(scan_id=f"s{i}") for i in range(29)])
        built = report.build_report(trades, funnel, scanner, shadow)
        statuses = {item["name"]: item["status"] for item in built["brains"]}
        if statuses["Shadow learning brain"] != "IMMATURE":
            return "shadow rows <30 should be immature"
    return None


def test_safety_locks_preserved() -> str | None:
    if trading_config.TRADING_MODE != "PAPER":
        return "TRADING_MODE changed"
    if not trading_config.GLOBAL_FORCED_LEARNING_MODE:
        return "Kelly/forced learning guard changed"
    if trading_config.DATA_COLLECTION_OVERRIDE_ENABLED:
        return "data collection override changed"
    if "KXETH" not in {str(prefix).upper() for prefix in trading_config.QUARANTINED_TICKER_PREFIXES}:
        return "KXETH quarantine missing"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        built = report.build_report(
            tmp / "missing_paper.jsonl",
            tmp / "missing_funnel.jsonl",
            tmp / "missing_scanner.jsonl",
            tmp / "missing_shadow.jsonl",
        )
        safety = built["safety"]
        if safety["real_money_allowed"] is not False:
            return "report claims real money allowed"
        if safety["scale_allowed"] is not False:
            return "report claims scale allowed"
        if safety["paper_only"] is not True:
            return "report lost paper-only lock"
        for item in built["brains"]:
            if item["live_patch_allowed"] is not False:
                return f"{item['name']} allows live patch"
    return None


def run_tests() -> int:
    tests = [
        test_empty_and_missing_files_safe,
        test_report_is_read_only,
        test_negative_roi_flags_probability_weak,
        test_shadow_rows_under_30_immature,
        test_safety_locks_preserved,
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
    print("\nSentinel: BRAIN_WEAKNESS_MAP_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
