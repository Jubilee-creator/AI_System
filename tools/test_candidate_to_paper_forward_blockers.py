#!/usr/bin/env python3
"""Tests for Phase 10M candidate-to-paper forward blocker audit."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import report_candidate_to_paper_forward_blockers as audit


START = datetime.fromisoformat("2026-05-14T03:22:46.375517+00:00").astimezone(timezone.utc)
AFTER = "2026-05-14T03:22:47+00:00"
BEFORE = "2026-05-14T03:22:45+00:00"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def base_row(reason: str, **overrides) -> dict:
    row = {
        "timestamp_utc": AFTER,
        "ticker": "KXTEST-26MAY",
        "scanner_action": "BET_YES",
        "final_reason": reason,
        "dashboard_seen": True,
        "passed_to_paper_trader": True,
        "paper_trader_received": True,
        "paper_trade_opened": False,
        "trace_excerpt": reason,
    }
    row.update(overrides)
    return row


def run_report(shadow_rows: list[dict], funnel_rows: list[dict], trade_rows: list[dict]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shadow = root / "shadow.jsonl"
        funnel = root / "funnel.jsonl"
        trades = root / "trades.jsonl"
        write_jsonl(shadow, shadow_rows)
        write_jsonl(funnel, funnel_rows)
        write_jsonl(trades, trade_rows)
        before = {path.name: path.read_text(encoding="utf-8") for path in (shadow, funnel, trades)}
        report = audit.build_report(shadow, funnel, trades, START)
        after = {path.name: path.read_text(encoding="utf-8") for path in (shadow, funnel, trades)}
        assert before == after, "report must be read-only"
        return report


def test_empty_logs_safe():
    report = run_report([], [], [])
    assert report["candidate_count_after_start"] == 0
    assert report["main_blocker_label"] == "NO_CANDIDATES_AFTER_SHADOW_START"


def test_events_before_shadow_start_are_ignored():
    before_row = base_row("TRADE_OPENED", timestamp_utc=BEFORE, paper_trade_opened=True)
    after_row = base_row("BLOCKED_MIN_EDGE")
    report = run_report([{"timestamp_utc": AFTER, "candidate_count": 1}], [before_row, after_row], [])
    assert report["execution_funnel_events_after_start"] == 1
    assert report["trade_opened_events_after_start"] == 0
    assert report["blocked_candidates_by_reason"]["BLOCKED_MIN_EDGE"] == 1


def test_trade_opened_and_paper_trade_rows_counted():
    opened = base_row("TRADE_OPENED", paper_trade_opened=True)
    trade = {"timestamp": AFTER, "ticker": "KXTEST-26MAY", "status": "OPEN"}
    report = run_report([{"timestamp_utc": AFTER, "candidate_count": 1}], [opened], [trade])
    assert report["trade_opened_events_after_start"] == 1
    assert report["paper_trades_rows_after_start"] == 1
    assert report["paper_trades_write_healthy"] is True


def test_blocker_reason_grouping_and_stage_counts():
    rows = [
        base_row("BLOCKED_COUNCIL", council_decision="BLOCK"),
        base_row("BLOCKED_RISK", risk_block_reason="daily cap"),
        base_row("BLOCKED_MIN_EDGE"),
        base_row("BLOCKED_MARKET_QUALITY", trace_excerpt="market quality filter"),
        base_row("BLOCKED_MAX_OPEN_TRADES"),
        base_row("BLOCKED_DUPLICATE_TICKER"),
    ]
    report = run_report([{"timestamp_utc": AFTER, "candidate_count": 6}], rows, [])
    assert report["blocked_candidates_by_reason"]["BLOCKED_COUNCIL"] == 1
    assert report["blocker_counts_by_stage"]["council"] == 1
    assert report["blocker_counts_by_stage"]["risk"] == 1
    assert report["blocker_counts_by_stage"]["edge_threshold"] == 1
    assert report["blocker_counts_by_stage"]["market_quality"] == 1
    assert report["blocker_counts_by_stage"]["max_open_exposure"] == 1
    assert report["blocker_counts_by_stage"]["duplicate_protection"] == 1


def test_single_stage_main_blockers():
    cases = [
        ("BLOCKED_COUNCIL", "COUNCIL_BLOCKING_ALL", {"council_decision": "BLOCK"}),
        ("BLOCKED_RISK", "RISK_BLOCKING_ALL", {"risk_block_reason": "blocked"}),
        ("BLOCKED_MIN_EDGE", "EDGE_FILTER_BLOCKING_ALL", {}),
        ("BLOCKED_MARKET_QUALITY", "MARKET_QUALITY_BLOCKING_ALL", {}),
        ("BLOCKED_MAX_OPEN_TRADES", "MAX_OPEN_OR_EXPOSURE_BLOCKING", {}),
        ("BLOCKED_DUPLICATE_TICKER", "DUPLICATE_PROTECTION_BLOCKING", {}),
    ]
    for reason, label, overrides in cases:
        report = run_report([{"timestamp_utc": AFTER, "candidate_count": 1}], [base_row(reason, **overrides)], [])
        assert report["main_blocker_label"] == label


def test_paper_trader_not_called_classification():
    row = base_row("BLOCKED_OR_SKIPPED_UNKNOWN", passed_to_paper_trader=True, paper_trader_received=False)
    report = run_report([{"timestamp_utc": AFTER, "candidate_count": 1}], [row], [])
    assert report["main_blocker_label"] == "PAPER_TRADER_NOT_CALLED"
    assert report["paper_trader_is_being_called"] is False


def test_log_write_failure_classification():
    opened = base_row("TRADE_OPENED", paper_trade_opened=True)
    report = run_report([{"timestamp_utc": AFTER, "candidate_count": 1}], [opened], [])
    assert report["main_blocker_label"] == "PAPER_TRADE_LOG_WRITE_FAILURE"
    assert report["paper_trades_write_healthy"] is False


def test_unknown_blocker_handled_honestly():
    report = run_report([{"timestamp_utc": AFTER, "candidate_count": 1}], [base_row("SOMETHING_NEW")], [])
    assert report["main_blocker_label"] == "UNKNOWN_FORWARD_BLOCKER"
    assert report["blocker_counts_by_stage"]["unknown"] == 1


def test_mixed_valid_safety_blocks_are_expected_not_bug():
    rows = [
        base_row("BLOCKED_MIN_EDGE"),
        base_row("BLOCKED_MARKET_QUALITY"),
        base_row("BLOCKED_QUARANTINE", trace_excerpt="quarantined prefix"),
    ]
    report = run_report([{"timestamp_utc": AFTER, "candidate_count": 3}], rows, [])
    assert report["main_blocker_label"] == "EXPECTED_SAFETY_BLOCK_NOT_A_BUG"
    assert report["paper_trader_is_being_called"] is True


def test_safety_locks_preserved():
    assert audit.TRADING_MODE in {"watchlist", "paper", "paper_only", "research_only", "WATCHLIST", "PAPER"}
    assert audit.DATA_COLLECTION_OVERRIDE_ENABLED is False
    assert audit.GLOBAL_FORCED_LEARNING_MODE is True
    assert any(str(prefix).startswith("KXETH") for prefix in audit.QUARANTINED_TICKER_PREFIXES)


if __name__ == "__main__":
    test_empty_logs_safe()
    test_events_before_shadow_start_are_ignored()
    test_trade_opened_and_paper_trade_rows_counted()
    test_blocker_reason_grouping_and_stage_counts()
    test_single_stage_main_blockers()
    test_paper_trader_not_called_classification()
    test_log_write_failure_classification()
    test_unknown_blocker_handled_honestly()
    test_mixed_valid_safety_blocks_are_expected_not_bug()
    test_safety_locks_preserved()
    print("CANDIDATE_TO_PAPER_FORWARD_BLOCKERS_TESTS_OK")
