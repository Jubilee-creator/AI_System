#!/usr/bin/env python3
"""
Tests for Phase 10D payoff geometry candidate quality autopsy.

These tests use temporary logs only. They do not import PaperTrader, execute
signals, or mutate production logs.
"""
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
from tools import report_payoff_geometry_candidate_quality_autopsy as report  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))


def _sha(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trade_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "ticker": "KXBTCD-TEST",
        "status": "SETTLED",
        "result": "WIN",
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
        "economic_pnl": 0.90,
        "recorded_pnl": 0.90,
        "pnl": 0.90,
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


def _funnel_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "ticker": "KXBTCD-TEST",
        "scanner_action": "BET_YES",
        "confidence": 0.86,
        "edge": 0.04,
        "yes_bid": 0.81,
        "yes_ask": 0.82,
        "no_bid": 0.18,
        "no_ask": 0.19,
        "spread": 0.01,
        "overround": 0.01,
        "scan_non_pass_rank": 2,
        "paper_trade_opened": False,
        "final_reason": "BLOCKED_MARKET_QUALITY",
    }
    row.update(overrides)
    return row


def test_math_helpers() -> str | None:
    price = 0.80
    if report.breakeven_wr(price) != 0.80:
        return "breakeven WR should equal binary entry price"
    if round(report.reward_risk(price) or 0.0, 4) != 0.25:
        return "reward/risk calculation wrong"
    row = _funnel_row(confidence=0.75, yes_ask=0.82)
    if round(report.model_minus_breakeven(row) or 0.0, 4) != -0.07:
        return "model probability minus breakeven calculation wrong"
    return None


def test_detects_expensive_and_bad_geometry() -> str | None:
    row = _funnel_row(confidence=0.86, edge=0.06, yes_ask=0.84)
    flags = report.candidate_quality_flags(row)
    for expected in ("expensive_entry", "weak_reward_risk", "model_edge_bad_geometry"):
        if expected not in flags:
            return f"missing quality flag {expected}"
    bad_prob = _funnel_row(confidence=0.70, edge=0.01, yes_ask=0.82)
    if "model_below_breakeven" not in report.candidate_quality_flags(bad_prob):
        return "did not detect model probability below breakeven"
    return None


def test_empty_logs_and_missing_fields() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(funnel, [{"ticker": "NO_FIELDS"}])
        _write_jsonl(scanner, [])
        built = report.build_report(trades, funnel, scanner)
        if built["counts"]["candidate_rows"] != 1:
            return "missing-field funnel row was not handled"
        if built["candidate_summary"]["avg_entry"] is not None:
            return "missing entry should stay missing"
        _write_jsonl(funnel, [])
        built = report.build_report(trades, funnel, scanner)
        if built["counts"]["candidate_rows"] != 0:
            return "empty logs should produce zero candidate rows"
    return None


def test_read_only_and_separates_blocked_from_opened() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [_trade_row()])
        _write_jsonl(
            funnel,
            [
                _funnel_row(final_reason="BLOCKED_MIN_EDGE", paper_trade_opened=False),
                _funnel_row(final_reason="TRADE_OPENED", paper_trade_opened=True),
            ],
        )
        _write_jsonl(scanner, [])
        before = {path: _sha(path) for path in (trades, funnel, scanner)}
        built = report.build_report(trades, funnel, scanner)
        after = {path: _sha(path) for path in (trades, funnel, scanner)}
        if before != after:
            return "report modified input logs"
        if built["counts"]["blocked_candidates"] != 1 or built["counts"]["opened_candidates"] != 1:
            return "blocked/opened candidate separation failed"
    return None


def test_clean_rows_exist_and_reported() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(
            trades,
            [
                _trade_row(result="WIN", economic_pnl=0.90, recorded_pnl=0.90, pnl=0.90),
                _trade_row(
                    timestamp="2026-01-01T00:01:00+00:00",
                    result="LOSS",
                    economic_pnl=-4.10,
                    recorded_pnl=-4.10,
                    pnl=-4.10,
                ),
            ],
        )
        _write_jsonl(funnel, [])
        _write_jsonl(scanner, [])
        built = report.build_report(trades, funnel, scanner)
        if built["counts"]["fresh_clean_settled"] != 2:
            return "clean settled rows not detected"
        if built["settled_summary"]["wins"] != 1 or built["settled_summary"]["losses"] != 1:
            return "settled win/loss summary wrong"
    return None


def test_safety_locks() -> str | None:
    if trading_config.TRADING_MODE != "PAPER":
        return "TRADING_MODE is not PAPER"
    if not trading_config.GLOBAL_FORCED_LEARNING_MODE:
        return "Kelly/forced learning guard is not active"
    if trading_config.DATA_COLLECTION_OVERRIDE_ENABLED:
        return "data collection override should remain disabled"
    if "KXETH" not in {str(prefix).upper() for prefix in trading_config.QUARANTINED_TICKER_PREFIXES}:
        return "KXETH quarantine is not active"
    built = report.build_report(
        Path("/tmp/nonexistent_phase10d_paper.jsonl"),
        Path("/tmp/nonexistent_phase10d_funnel.jsonl"),
        Path("/tmp/nonexistent_phase10d_scanner.jsonl"),
    )
    safety = built["safety"]
    if safety["real_money_allowed"] is not False:
        return "report claims real money allowed"
    if safety["scale_allowed"] is not False:
        return "report claims scale allowed"
    if safety["kxeth_quarantine_active"] is not True:
        return "report did not preserve KXETH quarantine"
    return None


def test_bet_no_and_arb_classification() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper_trades.jsonl"
        funnel = tmp / "execution_funnel.jsonl"
        scanner = tmp / "scanner_opportunities.jsonl"
        _write_jsonl(trades, [])
        _write_jsonl(
            funnel,
            [
                _funnel_row(scanner_action="BET_NO", confidence=0.74, no_ask=0.76, final_reason="BLOCKED_MIN_EDGE"),
                _funnel_row(scanner_action="ARB", confidence=0.50, yes_ask=0.48, no_ask=0.49, final_reason="BLOCKED_COUNCIL"),
            ],
        )
        _write_jsonl(scanner, [])
        built = report.build_report(trades, funnel, scanner)
        if built["bet_no"]["rows"] != 1 or built["bet_no"]["opened"] != 0:
            return "BET_NO classification failed"
        if built["arb"]["rows"] != 1 or built["arb"]["verdict"] != "UNPROVEN_BLOCKED_EDGE":
            return "ARB blocked classification failed"
    return None


def run_tests() -> int:
    tests = [
        test_math_helpers,
        test_detects_expensive_and_bad_geometry,
        test_empty_logs_and_missing_fields,
        test_read_only_and_separates_blocked_from_opened,
        test_clean_rows_exist_and_reported,
        test_safety_locks,
        test_bet_no_and_arb_classification,
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
    print("\nSentinel: PAYOFF_GEOMETRY_CANDIDATE_QUALITY_AUTOPSY_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
