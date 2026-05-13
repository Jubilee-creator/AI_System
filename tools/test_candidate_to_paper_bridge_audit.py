#!/usr/bin/env python3
"""
Focused tests for tools/report_candidate_to_paper_bridge_audit.py.

These tests use temporary JSONL files only. They never touch production logs.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import GLOBAL_FORCED_LEARNING_MODE, QUARANTINED_TICKER_PREFIXES, TRADING_MODE
from tools import report_candidate_to_paper_bridge_audit as audit

BASELINE = {
    "clean_row_count": 0,
    "last_timestamp": "2026-01-01T00:00:00+00:00",
    "cohort_hash": "test",
}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def file_hash(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_state(tmp: Path) -> dict[str, Any]:
    return audit.build_bridge_state(
        funnel_path=tmp / "execution_funnel.jsonl",
        trades_path=tmp / "paper_trades.jsonl",
        scanner_path=tmp / "scanner_opportunities.jsonl",
        baseline_snapshot=BASELINE,
    )


def funnel_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp_utc": "2026-01-01T00:01:00+00:00",
        "ticker": "TEST-1",
        "scanner_action": "BET_YES",
        "intended_action": "BET_YES",
        "executed_action": None,
        "passed_to_paper_trader": True,
        "paper_trader_received": True,
        "paper_trade_opened": False,
        "final_status": "NO_TRADE",
        "final_reason": "BLOCKED_MIN_EDGE",
        "open_count_before": 0,
        "max_open_trades": 3,
        "cap_already_full": False,
    }
    row.update(overrides)
    return row


def paper_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp": "2026-01-01T00:01:00+00:00",
        "ticker": "TEST-1",
        "action": "BET_YES",
        "status": "OPEN",
        "result": None,
        "entry_price": 0.60,
        "size": 5.0,
        "risk_edge": 0.05,
        "model_probability": 0.70,
        "economic_pnl": 0.0,
        "yes_bid": 0.59,
        "yes_ask": 0.60,
    }
    row.update(overrides)
    return row


def run_in_temp(fn: Callable[[Path], None]) -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        write_jsonl(tmp / "execution_funnel.jsonl", [])
        write_jsonl(tmp / "paper_trades.jsonl", [])
        write_jsonl(tmp / "scanner_opportunities.jsonl", [])
        fn(tmp)


def test_report_is_read_only() -> str | None:
    def _run(tmp: Path) -> None:
        write_jsonl(tmp / "execution_funnel.jsonl", [funnel_row()])
        write_jsonl(tmp / "paper_trades.jsonl", [paper_row(status="OPEN")])
        before = {
            name: file_hash(tmp / name)
            for name in ("execution_funnel.jsonl", "paper_trades.jsonl", "scanner_opportunities.jsonl")
        }
        state = build_state(tmp)
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            audit.render_report(state)
        after = {
            name: file_hash(tmp / name)
            for name in ("execution_funnel.jsonl", "paper_trades.jsonl", "scanner_opportunities.jsonl")
        }
        if before != after:
            raise AssertionError(f"log hash changed: before={before} after={after}")
    run_in_temp(_run)
    return None


def test_funnel_active_paper_stale_candidates_blocked_validly() -> str | None:
    def _run(tmp: Path) -> None:
        write_jsonl(tmp / "execution_funnel.jsonl", [
            funnel_row(final_reason="BLOCKED_MIN_EDGE"),
            funnel_row(timestamp_utc="2026-01-01T00:02:00+00:00", final_reason="BLOCKED_MARKET_QUALITY"),
        ])
        state = build_state(tmp)
        if state["bridge"]["status"] != "CANDIDATES_BLOCKED_VALIDLY":
            raise AssertionError(state["bridge"])
        if state["bridge"]["paper_stale_after_funnel"] is not True:
            raise AssertionError(state["bridge"])
    run_in_temp(_run)
    return None


def test_trade_opened_not_written_detected() -> str | None:
    def _run(tmp: Path) -> None:
        write_jsonl(tmp / "execution_funnel.jsonl", [
            funnel_row(
                paper_trade_opened=True,
                final_status="TRADE_OPENED",
                final_reason="TRADE_OPENED",
                executed_action="BET_YES",
            )
        ])
        state = build_state(tmp)
        if state["bridge"]["status"] != "TRADE_OPENED_NOT_WRITTEN":
            raise AssertionError(state["bridge"])
        if state["bridge"]["unmatched_trade_opened_after_baseline"] != 1:
            raise AssertionError(state["bridge"])
    run_in_temp(_run)
    return None


def test_trade_opened_written_is_matched() -> str | None:
    def _run(tmp: Path) -> None:
        write_jsonl(tmp / "execution_funnel.jsonl", [
            funnel_row(
                timestamp_utc="2026-01-01T00:01:10+00:00",
                paper_trade_opened=True,
                final_status="TRADE_OPENED",
                final_reason="TRADE_OPENED",
                executed_action="BET_YES",
            )
        ])
        write_jsonl(tmp / "paper_trades.jsonl", [
            paper_row(timestamp="2026-01-01T00:01:05+00:00", status="OPEN"),
        ])
        state = build_state(tmp)
        if state["bridge"]["status"] != "BRIDGE_HEALTHY":
            raise AssertionError(state["bridge"])
        if state["bridge"]["matched_trade_opened_after_baseline"] != 1:
            raise AssertionError(state["bridge"])
    run_in_temp(_run)
    return None


def test_max_open_false_block_with_active_zero() -> str | None:
    def _run(tmp: Path) -> None:
        stale_open = paper_row(timestamp="2025-12-31T23:00:00+00:00", status="OPEN")
        stale_settled = paper_row(
            timestamp="2025-12-31T23:00:00+00:00",
            status="SETTLED",
            result="WIN",
            economic_pnl=1.0,
        )
        write_jsonl(tmp / "paper_trades.jsonl", [stale_open, stale_settled])
        write_jsonl(tmp / "execution_funnel.jsonl", [
            funnel_row(final_reason="BLOCKED_MAX_OPEN_TRADES", open_count_before=0, cap_already_full=False)
        ])
        state = build_state(tmp)
        if state["ghost"]["status"] != "MAX_OPEN_FALSE_BLOCK":
            raise AssertionError(state["ghost"])
        if state["ghost"]["active_open_count"] != 0 or state["ghost"]["stale_open_count"] != 1:
            raise AssertionError(state["ghost"])
        if state["ghost"]["suspicious_max_open_blocks_after_baseline"] != 1:
            raise AssertionError(state["ghost"])
    run_in_temp(_run)
    return None


def test_raw_open_distinguished_from_active_open() -> str | None:
    def _run(tmp: Path) -> None:
        rows = []
        for idx in range(3):
            ts = f"2025-12-31T23:0{idx}:00+00:00"
            rows.append(paper_row(timestamp=ts, ticker=f"TEST-{idx}", status="OPEN"))
            rows.append(paper_row(timestamp=ts, ticker=f"TEST-{idx}", status="SETTLED", result="WIN", economic_pnl=1.0))
        write_jsonl(tmp / "paper_trades.jsonl", rows)
        state = build_state(tmp)
        if state["ghost"]["raw_open_count"] != 3:
            raise AssertionError(state["ghost"])
        if state["ghost"]["active_open_count"] != 0:
            raise AssertionError(state["ghost"])
        if state["ghost"]["stale_open_count"] != 3:
            raise AssertionError(state["ghost"])
        if state["ghost"]["status"] != "GHOST_OPEN_PRESENT_NOT_BLOCKING":
            raise AssertionError(state["ghost"])
    run_in_temp(_run)
    return None


def test_paper_rows_and_clean_rows_after_baseline_detected() -> str | None:
    def _run(tmp: Path) -> None:
        write_jsonl(tmp / "paper_trades.jsonl", [
            paper_row(
                status="SETTLED",
                result="WIN",
                economic_pnl=1.0,
                pnl=1.0,
                exit_price=1.0,
                settled_at="2026-01-01T00:10:00+00:00",
            )
        ])
        state = build_state(tmp)
        if state["paper"]["after_baseline_rows"] != 1:
            raise AssertionError(state["paper"])
        if state["paper"]["clean_rows_after_baseline"] != 1:
            raise AssertionError(state["paper"])
    run_in_temp(_run)
    return None


def test_missing_timestamps_and_empty_logs_safe() -> str | None:
    def _run(tmp: Path) -> None:
        write_jsonl(tmp / "execution_funnel.jsonl", [{"ticker": "NO_TS", "final_reason": "BLOCKED_MIN_EDGE"}])
        write_jsonl(tmp / "paper_trades.jsonl", [{"ticker": "NO_TS", "status": "OPEN"}])
        state = build_state(tmp)
        if state["funnel"]["after_baseline_rows"] != 0:
            raise AssertionError(state["funnel"])
        if state["paper"]["after_baseline_rows"] != 0:
            raise AssertionError(state["paper"])
        write_jsonl(tmp / "execution_funnel.jsonl", [])
        write_jsonl(tmp / "paper_trades.jsonl", [])
        state = build_state(tmp)
        if state["bridge"]["status"] != "UNKNOWN_BRIDGE_FAILURE":
            raise AssertionError(state["bridge"])
    run_in_temp(_run)
    return None


def test_arb_handling_detects_blocked_before_open() -> str | None:
    def _run(tmp: Path) -> None:
        write_jsonl(tmp / "scanner_opportunities.jsonl", [
            {"timestamp_utc": "2026-01-01T00:00:30+00:00", "ticker": "ARB-1", "scanner_action": "ARB"}
        ])
        write_jsonl(tmp / "execution_funnel.jsonl", [
            funnel_row(
                timestamp_utc="2026-01-01T00:01:00+00:00",
                ticker="ARB-1",
                scanner_action="ARB",
                intended_action="ARB",
                final_reason="BLOCKED_MIN_EDGE",
            )
        ])
        state = build_state(tmp)
        if state["arb"]["status"] != "ARB_BLOCKED_BEFORE_OPEN":
            raise AssertionError(state["arb"])
        if state["arb"]["arb_passed_to_paper_trader_after_baseline"] != 1:
            raise AssertionError(state["arb"])
    run_in_temp(_run)
    return None


def test_report_prints_statuses_and_sentinel() -> str | None:
    def _run(tmp: Path) -> None:
        write_jsonl(tmp / "execution_funnel.jsonl", [funnel_row()])
        state = build_state(tmp)
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            audit.render_report(state)
        text = capture.getvalue()
        required = [
            "bridge_status:",
            "ghost_status:",
            "ARB HANDLING",
            audit.SENTINEL,
        ]
        missing = [item for item in required if item not in text]
        if missing:
            raise AssertionError(f"missing report text: {missing}")
    run_in_temp(_run)
    return None


def test_safety_locks_remain_disabled() -> str | None:
    state = audit.build_bridge_state(
        funnel_path=Path("/tmp/nonexistent-funnel.jsonl"),
        trades_path=Path("/tmp/nonexistent-paper.jsonl"),
        scanner_path=Path("/tmp/nonexistent-scanner.jsonl"),
        baseline_snapshot=BASELINE,
    )
    safety = state["safety"]
    if TRADING_MODE != "PAPER" or safety["paper_only"] is not True:
        return f"paper_only failed: {safety}"
    if safety["real_money_allowed"] is not False:
        return f"real_money_allowed changed: {safety}"
    if safety["scale_allowed"] is not False:
        return f"scale_allowed changed: {safety}"
    if GLOBAL_FORCED_LEARNING_MODE is not True or safety["kelly_execution_disabled"] is not True:
        return f"Kelly execution not disabled: {safety}"
    if not any(str(prefix).upper() == "KXETH" for prefix in QUARANTINED_TICKER_PREFIXES):
        return f"KXETH quarantine missing: {QUARANTINED_TICKER_PREFIXES}"
    if safety["kxeth_quarantine_active"] is not True:
        return f"KXETH quarantine inactive in report: {safety}"
    return None


def main() -> int:
    tests: list[tuple[str, Callable[[], str | None]]] = [
        ("report is read-only and does not modify logs", test_report_is_read_only),
        ("detects funnel active but paper stale with valid blockers", test_funnel_active_paper_stale_candidates_blocked_validly),
        ("detects TRADE_OPENED not written", test_trade_opened_not_written_detected),
        ("matches TRADE_OPENED to paper OPEN row", test_trade_opened_written_is_matched),
        ("detects max-open false block while active open is zero", test_max_open_false_block_with_active_zero),
        ("distinguishes raw OPEN from active OPEN and stale OPEN", test_raw_open_distinguished_from_active_open),
        ("detects paper rows and clean rows after baseline", test_paper_rows_and_clean_rows_after_baseline_detected),
        ("handles missing timestamps and empty logs safely", test_missing_timestamps_and_empty_logs_safe),
        ("detects ARB blocked before open", test_arb_handling_detects_blocked_before_open),
        ("prints clear bridge/ghost status and sentinel", test_report_prints_statuses_and_sentinel),
        ("safety locks remain disabled", test_safety_locks_remain_disabled),
    ]
    failures: list[str] = []
    for name, fn in tests:
        try:
            error = fn()
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
        if error:
            failures.append(f"{name}: {error}")
            print(f"[FAIL] {name}: {error}")
        else:
            print(f"[PASS] {name}")
    if failures:
        print()
        print("FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print()
    print("CANDIDATE_TO_PAPER_BRIDGE_AUDIT_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
