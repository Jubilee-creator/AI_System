#!/usr/bin/env python3
"""Tests for Phase 10O upstream filter simulation."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import report_upstream_filter_simulation as sim


START = datetime.fromisoformat("2026-05-14T03:22:46.375517+00:00").astimezone(timezone.utc)
AFTER = "2026-05-14T03:22:47+00:00"
BEFORE = "2026-05-14T03:22:45+00:00"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def base_row(**overrides) -> dict:
    row = {
        "timestamp_utc": AFTER,
        "ticker": "KXBTC-26MAY",
        "scanner_action": "BET_YES",
        "final_reason": "BLOCKED_MIN_EDGE",
        "paper_trade_opened": False,
        "confidence": 0.72,
        "edge": 0.02,
        "yes_ask": 0.70,
        "yes_bid": 0.68,
        "no_ask": 0.31,
        "spread": 0.02,
        "volume": 2000,
    }
    row.update(overrides)
    return row


def run_report(rows: list[dict]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "funnel.jsonl"
        write_jsonl(path, rows)
        before = path.read_text(encoding="utf-8")
        report = sim.build_report(path, START)
        after = path.read_text(encoding="utf-8")
        assert before == after, "report must be read-only"
        return report


def test_empty_logs_do_not_crash():
    report = run_report([])
    assert report["baseline"]["n"] == 0
    assert report["deployable_live"] is False


def test_events_before_shadow_start_ignored():
    report = run_report([base_row(timestamp_utc=BEFORE), base_row(timestamp_utc=AFTER)])
    assert report["baseline"]["n"] == 1


def test_quarantine_exclusion_removes_only_quarantined_rows():
    report = run_report([
        base_row(ticker="KXETHD-26MAY", final_reason="BLOCKED_QUARANTINE"),
        base_row(ticker="KXBTC-26MAY"),
    ])
    result = report["individual_filter_results"]["A"]
    assert result["candidates_removed"] == 1
    assert result["quarantine_blocks_removed"] == 1


def test_weak_reward_risk_filter_identifies_rr_below_threshold():
    report = run_report([
        base_row(yes_ask=0.90, no_ask=0.11),
        base_row(yes_ask=0.50, no_ask=0.51),
    ])
    result = report["individual_filter_results"]["C"]
    assert result["candidates_removed"] == 1
    assert result["weak_reward_risk_rows_removed"] == 1


def test_expensive_entry_gate_identifies_80_90_entries():
    report = run_report([
        base_row(yes_ask=0.85, no_ask=0.16),
        base_row(yes_ask=0.91, no_ask=0.10),
        base_row(yes_ask=0.79, no_ask=0.22),
    ])
    result = report["individual_filter_results"]["D"]
    assert result["candidates_removed"] == 1
    assert result["expensive_entry_rows_removed"] == 1


def test_entry_conditioned_edge_floor_works():
    report = run_report([
        base_row(yes_ask=0.92, confidence=0.98, edge=0.05),
        base_row(yes_ask=0.92, confidence=0.95, edge=0.02),
        base_row(yes_ask=0.72, confidence=0.80, edge=0.05),
    ])
    result = report["individual_filter_results"]["E"]
    assert result["candidates_removed"] == 1


def test_market_quality_repeat_deprioritization_works():
    rows = [
        base_row(ticker="KXBAD-1", final_reason="BLOCKED_MARKET_QUALITY", spread=0.10)
        for _ in range(sim.REPEAT_MARKET_QUALITY_MIN_BLOCKS)
    ]
    rows.append(base_row(ticker="KXGOOD-1", final_reason="BLOCKED_MIN_EDGE"))
    report = run_report(rows)
    result = report["individual_filter_results"]["B"]
    assert result["candidates_removed"] == sim.REPEAT_MARKET_QUALITY_MIN_BLOCKS
    assert result["market_quality_blocks_removed"] == sim.REPEAT_MARKET_QUALITY_MIN_BLOCKS


def test_side_specific_profiles_separate_yes_and_no():
    report = run_report([
        base_row(scanner_action="BET_YES", yes_ask=0.70),
        base_row(scanner_action="BET_NO", no_ask=0.40, yes_ask=0.61),
    ])
    profiles = report["side_specific_profiles"]
    assert profiles["BET_YES"]["n"] == 1
    assert profiles["BET_NO"]["n"] == 1
    assert report["individual_filter_results"]["F"]["simulation_only"] is True


def test_filter_stacks_do_not_double_count_removed_rows():
    row = base_row(ticker="KXETHD-26MAY", final_reason="BLOCKED_QUARANTINE", yes_ask=0.85)
    report = run_report([row])
    stack = report["stack_results"]["Stack 6"]
    assert stack["candidates_removed"] == 1


def test_starvation_and_overfitting_reported():
    rows = [base_row(yes_ask=0.90) for _ in range(10)]
    report = run_report(rows)
    result = report["individual_filter_results"]["C"]
    assert result["starvation_risk"] in {"HIGH", "MEDIUM_HIGH"}
    assert result["overfitting_risk"] == "MEDIUM"


def test_all_recommendations_simulation_only_and_no_live_mutation():
    report = run_report([base_row()])
    all_results = list(report["individual_filter_results"].values()) + list(report["stack_results"].values())
    assert all(row["simulation_only"] for row in all_results)
    assert report["deployable_live"] is False
    assert report["safety"]["real_money_allowed"] is False
    assert report["safety"]["scale_allowed"] is False
    assert report["safety"]["live_strategy_mutated"] is False


if __name__ == "__main__":
    test_empty_logs_do_not_crash()
    test_events_before_shadow_start_ignored()
    test_quarantine_exclusion_removes_only_quarantined_rows()
    test_weak_reward_risk_filter_identifies_rr_below_threshold()
    test_expensive_entry_gate_identifies_80_90_entries()
    test_entry_conditioned_edge_floor_works()
    test_market_quality_repeat_deprioritization_works()
    test_side_specific_profiles_separate_yes_and_no()
    test_filter_stacks_do_not_double_count_removed_rows()
    test_starvation_and_overfitting_reported()
    test_all_recommendations_simulation_only_and_no_live_mutation()
    print("UPSTREAM_FILTER_SIMULATION_TESTS_OK")
