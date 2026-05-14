#!/usr/bin/env python3
"""Tests for Phase 10E read-only BET_NO and ordering research reports."""
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
from tools import report_bet_no_asymmetry_research as bet_no_report  # noqa: E402
from tools import report_candidate_priority_ordering_audit as ordering_report  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"


def _funnel_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "run_id": "r1",
        "scan_id": "s1",
        "ticker": "KXBTCD-TEST",
        "scanner_action": "BET_YES",
        "confidence": 0.88,
        "edge": 0.05,
        "yes_bid": 0.83,
        "yes_ask": 0.84,
        "no_bid": 0.16,
        "no_ask": 0.17,
        "spread": 0.01,
        "scan_non_pass_rank": 1,
        "paper_trade_opened": False,
        "final_reason": "BLOCKED_MIN_EDGE",
    }
    row.update(overrides)
    return row


def test_empty_logs_safe() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(funnel, [])
        _write_jsonl(scanner, [])
        a = bet_no_report.build_report(trades, funnel, scanner)
        b = ordering_report.build_report(trades, funnel, scanner)
        if a["counts"]["candidates"] != 0:
            return "BET_NO report did not handle empty candidates"
        if b["ordering"]["scans_analyzed"] != 0:
            return "ordering report did not handle empty candidates"
    return None


def test_reports_are_read_only() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(funnel, [_funnel_row(), _funnel_row(scanner_action="BET_NO", no_ask=0.35, confidence=0.70)])
        _write_jsonl(scanner, [])
        before = {path: _sha(path) for path in (trades, funnel, scanner)}
        bet_no_report.build_report(trades, funnel, scanner)
        ordering_report.build_report(trades, funnel, scanner)
        after = {path: _sha(path) for path in (trades, funnel, scanner)}
        if before != after:
            return "reports modified temp logs"
    return None


def test_bet_no_classification_and_blockers() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(
            funnel,
            [
                _funnel_row(scanner_action="BET_NO", no_ask=0.35, confidence=0.70, edge=0.34, final_reason="BLOCKED_COUNCIL"),
                _funnel_row(scanner_action="BET_YES", yes_ask=0.85, confidence=0.90, edge=0.04, final_reason="BLOCKED_MIN_EDGE"),
            ],
        )
        _write_jsonl(scanner, [])
        built = bet_no_report.build_report(trades, funnel, scanner)
        if built["counts"]["bet_no"] != 1:
            return "BET_NO count wrong"
        if built["bet_no"]["blockers"].get("BLOCKED_COUNCIL") != 1:
            return "BET_NO blocker count wrong"
        if built["verdict"]["does_bet_no_deserve_research"] is not True:
            return "BET_NO research verdict wrong"
    return None


def test_ordering_analysis_detects_payoff_alternative() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        rows = [
            _funnel_row(scan_non_pass_rank=1, scanner_action="BET_YES", yes_ask=0.90, confidence=0.92, edge=0.01),
            _funnel_row(scan_non_pass_rank=2, scanner_action="BET_YES", yes_ask=0.88, confidence=0.91, edge=0.02),
            _funnel_row(scan_non_pass_rank=3, scanner_action="BET_YES", yes_ask=0.86, confidence=0.90, edge=0.03),
            _funnel_row(scan_non_pass_rank=4, scanner_action="BET_NO", no_ask=0.35, confidence=0.72, edge=0.36),
        ]
        _write_jsonl(trades, [])
        _write_jsonl(funnel, rows)
        _write_jsonl(scanner, [])
        built = ordering_report.build_report(trades, funnel, scanner)
        if built["ordering"]["scans_analyzed"] != 1:
            return "scan count wrong"
        if built["ordering"]["best_payoff_after_slots_scans"] != 1:
            return "did not detect best payoff after slots"
        if built["verdict"]["reward_risk_underweighted"] is not True:
            return "reward/risk verdict wrong"
    return None


def test_safety_locks_unchanged() -> str | None:
    if trading_config.TRADING_MODE != "PAPER":
        return "TRADING_MODE is not PAPER"
    if not trading_config.GLOBAL_FORCED_LEARNING_MODE:
        return "GLOBAL_FORCED_LEARNING_MODE is not active"
    if trading_config.DATA_COLLECTION_OVERRIDE_ENABLED:
        return "DATA_COLLECTION_OVERRIDE_ENABLED changed"
    if "KXETH" not in {str(prefix).upper() for prefix in trading_config.QUARANTINED_TICKER_PREFIXES}:
        return "KXETH quarantine missing"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(funnel, [])
        _write_jsonl(scanner, [])
        for built in (
            bet_no_report.build_report(trades, funnel, scanner),
            ordering_report.build_report(trades, funnel, scanner),
        ):
            safety = built["safety"]
            if safety["real_money_allowed"] is not False:
                return "report claims real money allowed"
            if safety["scale_allowed"] is not False:
                return "report claims scale allowed"
            if safety["kxeth_quarantine_active"] is not True:
                return "report did not preserve KXETH quarantine"
    return None


def run_tests() -> int:
    tests = [
        test_empty_logs_safe,
        test_reports_are_read_only,
        test_bet_no_classification_and_blockers,
        test_ordering_analysis_detects_payoff_alternative,
        test_safety_locks_unchanged,
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
    print("\nSentinel: PHASE10E_BET_NO_ORDERING_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
