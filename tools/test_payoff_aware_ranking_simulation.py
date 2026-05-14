#!/usr/bin/env python3
"""Tests for Phase 10F payoff-aware ranking simulation."""
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
from tools import report_payoff_aware_ranking_simulation as report  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"


def _row(**overrides: Any) -> dict[str, Any]:
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
        "scan_non_pass_rank": 1,
        "paper_trade_opened": False,
        "final_reason": "BLOCKED_MIN_EDGE",
    }
    row.update(overrides)
    return row


def test_empty_and_missing_logs_safe() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(funnel, [{"ticker": "MISSING_FIELDS"}])
        _write_jsonl(scanner, [])
        built = report.build_report(trades, funnel, scanner)
        if built["counts"]["candidate_rows"] != 1:
            return "missing-field candidate row was not retained safely"
        _write_jsonl(funnel, [])
        built = report.build_report(trades, funnel, scanner)
        if built["counts"]["candidate_rows"] != 0:
            return "empty logs should produce zero candidates"
    return None


def test_report_is_read_only() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(funnel, [_row(), _row(scan_non_pass_rank=2, yes_ask=0.65, confidence=0.75)])
        _write_jsonl(scanner, [])
        before = {path: _sha(path) for path in (trades, funnel, scanner)}
        report.build_report(trades, funnel, scanner)
        after = {path: _sha(path) for path in (trades, funnel, scanner)}
        if before != after:
            return "report mutated input logs"
    return None


def test_current_ranking_uses_scan_rank() -> str | None:
    rows = [
        _row(scan_non_pass_rank=2, ticker="SECOND"),
        _row(scan_non_pass_rank=1, ticker="FIRST"),
    ]
    picked = report._pick_current(rows, 1)
    if not picked or picked[0]["ticker"] != "FIRST":
        return "current ranking did not use scan_non_pass_rank"
    return None


def test_payoff_score_penalizes_expensive_and_rewards_rr() -> str | None:
    expensive = _row(yes_ask=0.90, confidence=0.93, edge=0.02)
    cheap = _row(yes_ask=0.55, confidence=0.70, edge=0.02)
    if (report.payoff_score(cheap) or -999) <= (report.payoff_score(expensive) or 999):
        return "payoff score did not reward better reward/risk / cheaper entry"
    return None


def test_strict_blocks_bad_geometry() -> str | None:
    below_be = _row(yes_ask=0.82, confidence=0.80, edge=0.20)
    weak_rr = _row(yes_ask=0.86, confidence=0.91, edge=0.20)
    exceptional = _row(yes_ask=0.84, confidence=0.96, edge=0.20)
    if report.strict_payoff_allowed(below_be):
        return "strict mode allowed model probability <= breakeven"
    if report.strict_payoff_allowed(weak_rr):
        return "strict mode allowed weak reward/risk without exceptional strength"
    if not report.strict_payoff_allowed(exceptional):
        return "strict mode blocked exceptional high-confidence/high-margin weak-RR row"
    return None


def test_strict_simulation_can_starve() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(
            funnel,
            [
                _row(scan_non_pass_rank=1, yes_ask=0.90, confidence=0.91),
                _row(scan_non_pass_rank=2, yes_ask=0.88, confidence=0.89),
                _row(scan_non_pass_rank=3, yes_ask=0.86, confidence=0.87),
            ],
        )
        _write_jsonl(scanner, [])
        built = report.build_report(trades, funnel, scanner)
        if built["top3"]["strict"]["starved_slots"] <= 0:
            return "strict simulation should report starvation when all candidates fail"
    return None


def test_safety_locks_unchanged() -> str | None:
    if trading_config.TRADING_MODE != "PAPER":
        return "TRADING_MODE changed"
    if not trading_config.GLOBAL_FORCED_LEARNING_MODE:
        return "Kelly/forced learning guard changed"
    if trading_config.DATA_COLLECTION_OVERRIDE_ENABLED:
        return "dc override changed"
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
        built = report.build_report(trades, funnel, scanner)
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
        test_empty_and_missing_logs_safe,
        test_report_is_read_only,
        test_current_ranking_uses_scan_rank,
        test_payoff_score_penalizes_expensive_and_rewards_rr,
        test_strict_blocks_bad_geometry,
        test_strict_simulation_can_starve,
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
    print("\nSentinel: PAYOFF_AWARE_RANKING_SIMULATION_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
