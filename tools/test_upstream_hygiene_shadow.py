#!/usr/bin/env python3
"""Tests for Phase 10P upstream hygiene shadow logger/report."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import DATA_COLLECTION_OVERRIDE_ENABLED, MIN_EDGE, MIN_VOLUME, QUARANTINED_TICKER_PREFIXES
from logs import upstream_hygiene_shadow_logger as logger
from tools import report_upstream_hygiene_shadow as report


def candidate(**overrides):
    row = {
        "ticker": "KXBTC-26MAY",
        "action": "BET_YES",
        "confidence": 0.72,
        "edge": 0.02,
        "yes_ask": 0.70,
        "yes_bid": 0.68,
        "no_ask": 0.31,
        "volume": 2000,
        "spread": 0.02,
    }
    row.update(overrides)
    return row


def test_logger_is_shadow_only_and_non_mutating():
    candidates = [
        candidate(),
        candidate(ticker="KXETHD-26MAY", yes_ask=0.85, confidence=0.90),
        candidate(yes_ask=0.90, no_ask=0.11),
    ]
    before = copy.deepcopy(candidates)
    row = logger.build_shadow_row(candidates, scan_id="scan1", run_id="run1")
    assert candidates == before
    assert row["shadow_only"] is True
    assert row["execution_changed"] is False
    assert row["live_strategy_mutated"] is False
    assert row["live_deployable"] is False
    assert row["note"] == "SHADOW_ONLY_NOT_EXECUTION"


def test_logger_never_changes_execution_candidates():
    candidates = [candidate(), candidate(action="PASS")]
    before_ids = [id(row) for row in candidates]
    logger.build_shadow_row(candidates)
    assert [id(row) for row in candidates] == before_ids
    assert candidates[1]["action"] == "PASS"


def test_logger_writes_valid_jsonl_and_empty_safe():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shadow.jsonl"
        stats = logger.log_upstream_hygiene_shadow([], path=path)
        assert stats["written"] == 1
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["current_candidate_count"] == 0
        assert rows[0]["shadow_only"] is True


def test_quarantine_exclusion_only_removes_quarantined_rows():
    row = logger.build_shadow_row([
        candidate(ticker="KXETHD-26MAY", yes_ask=0.85),
        candidate(ticker="KXBTC-26MAY", yes_ask=0.85),
    ])
    assert row["quarantine_removed_count"] == 1
    assert row["variants"]["stack1_quarantine_only"]["candidate_count"] == 1


def test_weak_rr_and_expensive_variants_detect_conditions():
    row = logger.build_shadow_row([
        candidate(yes_ask=0.90, no_ask=0.11),
        candidate(yes_ask=0.85, no_ask=0.16),
        candidate(yes_ask=0.50, no_ask=0.51),
    ])
    assert row["weak_reward_risk_removed_count"] == 2
    assert row["expensive_entry_removed_count"] == 1


def test_aggressive_stack_marked_research_only():
    row = logger.build_shadow_row([candidate(ticker="KXETHD-26MAY", yes_ask=0.85)])
    aggressive = row["variants"]["aggressive_stack"]
    assert aggressive["safety_classification"] == "RESEARCH_ONLY_AGGRESSIVE"
    assert aggressive["shadow_only"] is True
    assert aggressive["live_deployable"] is False


def test_report_reads_log_and_preserves_deploy_lock():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shadow.jsonl"
        logger.log_upstream_hygiene_shadow([candidate(ticker="KXETHD-26MAY")], path=path)
        built = report.build_report(log_path=path, funnel_path=Path(tmp) / "missing.jsonl")
        assert built["runtime_shadow_rows"] == 1
        assert built["source"] == "runtime_shadow_log"
        assert built["execution_changed"] is False
        assert built["live_strategy_mutated"] is False
        assert built["live_deployable"] is False


def test_safety_locks_and_thresholds_unchanged():
    assert DATA_COLLECTION_OVERRIDE_ENABLED is False
    assert MIN_EDGE == 0.03
    assert MIN_VOLUME == 1000
    assert any(str(prefix).startswith("KXETH") for prefix in QUARANTINED_TICKER_PREFIXES)


def test_no_paper_trader_behavior_in_logger():
    source = Path("logs/upstream_hygiene_shadow_logger.py").read_text(encoding="utf-8")
    assert "PaperTrader(" not in source
    assert "process_signal" not in source


if __name__ == "__main__":
    test_logger_is_shadow_only_and_non_mutating()
    test_logger_never_changes_execution_candidates()
    test_logger_writes_valid_jsonl_and_empty_safe()
    test_quarantine_exclusion_only_removes_quarantined_rows()
    test_weak_rr_and_expensive_variants_detect_conditions()
    test_aggressive_stack_marked_research_only()
    test_report_reads_log_and_preserves_deploy_lock()
    test_safety_locks_and_thresholds_unchanged()
    test_no_paper_trader_behavior_in_logger()
    print("UPSTREAM_HYGIENE_SHADOW_TESTS_OK")
