#!/usr/bin/env python3
"""Tests for Phase 10N upstream candidate quality autopsy."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import report_upstream_candidate_quality_autopsy as audit


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
        "ticker": "KXTEST-26MAY1412-T1",
        "scanner_action": "BET_YES",
        "final_reason": reason,
        "paper_trade_opened": False,
        "passed_to_paper_trader": True,
        "paper_trader_received": True,
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


def run_report(funnel_rows: list[dict], shadow_rows: list[dict] | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        funnel = root / "funnel.jsonl"
        shadow = root / "shadow.jsonl"
        write_jsonl(funnel, funnel_rows)
        write_jsonl(shadow, shadow_rows or [])
        before = {path.name: path.read_text(encoding="utf-8") for path in (funnel, shadow)}
        report = audit.build_report(funnel, shadow, START)
        after = {path.name: path.read_text(encoding="utf-8") for path in (funnel, shadow)}
        assert before == after, "report must be read-only"
        return report


def test_empty_logs_do_not_crash():
    report = run_report([])
    assert report["total_funnel_events_after_start"] == 0
    assert report["main_upstream_bottleneck_label"] == "UNKNOWN_UPSTREAM_QUALITY_PROBLEM"


def test_events_before_shadow_start_ignored():
    report = run_report([
        base_row("BLOCKED_MARKET_QUALITY", timestamp_utc=BEFORE),
        base_row("BLOCKED_MIN_EDGE", timestamp_utc=AFTER),
    ])
    assert report["total_funnel_events_after_start"] == 1
    assert report["blocker_counts_by_reason"]["BLOCKED_MIN_EDGE"] == 1


def test_grouping_by_reason_prefix_and_action():
    report = run_report([
        base_row("BLOCKED_MARKET_QUALITY", ticker="KXBTC-ONE", scanner_action="BET_YES"),
        base_row("BLOCKED_MARKET_QUALITY", ticker="KXBTC-TWO", scanner_action="BET_NO"),
        base_row("BLOCKED_COUNCIL", ticker="KXSOL-ONE", scanner_action="BET_YES", council_decision="BLOCK"),
    ])
    assert report["blocker_counts_by_reason"]["BLOCKED_MARKET_QUALITY"] == 2
    assert report["blocker_counts_by_ticker_prefix"]["KXBTC"] == 2
    assert report["blocker_counts_by_action"]["BET_YES"] == 2
    assert report["blocker_counts_by_action"]["BET_NO"] == 1


def test_price_confidence_edge_reward_risk_buckets_work():
    report = run_report([
        base_row("BLOCKED_MIN_EDGE", yes_ask=0.85, confidence=0.91, edge=0.01),
        base_row("BLOCKED_EDGE_DANGER_GUARD", yes_ask=0.40, confidence=0.66, edge=0.09),
    ])
    assert report["blocker_counts_by_entry_price_bucket"]["0.80-0.90"] == 1
    assert report["blocker_counts_by_confidence_bucket"]["0.90+"] == 1
    assert report["blocker_counts_by_edge_bucket"]["0.00-0.03"] == 1
    assert report["blocker_counts_by_edge_bucket"]["0.08+"] == 1
    assert report["blocker_counts_by_reward_risk_bucket"]["0.15-0.25"] == 1
    assert report["blocker_counts_by_reward_risk_bucket"]["1.00+"] == 1


def test_quarantined_candidates_counted_but_filter_idea_simulation_only():
    report = run_report([
        base_row("BLOCKED_QUARANTINE", ticker="KXETHD-26MAY", trace_excerpt="quarantined prefix"),
        base_row("BLOCKED_MARKET_QUALITY", ticker="KXBTC-26MAY"),
    ])
    assert report["kxeth_quarantined_candidate_count"] == 1
    quarantine_idea = next(idea for idea in report["candidate_filter_ideas"] if idea["filter_name"] == "pre_rank_quarantine_exclusion")
    assert quarantine_idea["candidates_removed"] == 1
    assert quarantine_idea["simulation_only"] is True


def test_main_bottleneck_market_quality_dominance():
    rows = [base_row("BLOCKED_MARKET_QUALITY", spread=0.10) for _ in range(6)]
    rows += [base_row("BLOCKED_MIN_EDGE") for _ in range(2)]
    assert run_report(rows)["main_upstream_bottleneck_label"] == "MARKET_QUALITY_UNIVERSE_TOO_WEAK"


def test_main_bottleneck_edge_weakness_dominance():
    rows = [base_row("BLOCKED_MIN_EDGE", edge=0.01) for _ in range(6)]
    rows += [base_row("BLOCKED_MARKET_QUALITY", spread=0.10) for _ in range(2)]
    assert run_report(rows)["main_upstream_bottleneck_label"] == "EDGE_TOO_WEAK_BEFORE_PAPERTRADER"


def test_main_bottleneck_quarantine_dominance():
    rows = [base_row("BLOCKED_QUARANTINE", ticker="KXETHD-26MAY", trace_excerpt="quarantined prefix") for _ in range(6)]
    rows += [base_row("BLOCKED_MIN_EDGE", ticker="KXBTC-26MAY") for _ in range(2)]
    assert run_report(rows)["main_upstream_bottleneck_label"] == "QUARANTINED_MARKETS_STILL_ENTERING_STREAM"


def test_main_bottleneck_mixed_problem():
    rows = [
        base_row("BLOCKED_MARKET_QUALITY", ticker="KXBTC-1", spread=0.10),
        base_row("BLOCKED_MIN_EDGE", ticker="KXSOL-1", edge=0.01),
        base_row("BLOCKED_COUNCIL", ticker="KXXRP-1", council_decision="BLOCK", edge=0.05),
        base_row("BLOCKED_QUARANTINE", ticker="KXETHD-1", trace_excerpt="quarantined prefix"),
    ]
    assert run_report(rows)["main_upstream_bottleneck_label"] == "MIXED_CANDIDATE_QUALITY_PROBLEM"


def test_no_live_strategy_mutation_occurs():
    report = run_report([base_row("BLOCKED_MIN_EDGE")])
    assert report["safety"]["real_money_allowed"] is False
    assert report["safety"]["scale_allowed"] is False
    assert report["safety"]["dc_override_enabled"] is False
    assert report["safety"]["live_strategy_mutated"] is False
    assert all(idea["simulation_only"] for idea in report["candidate_filter_ideas"])


if __name__ == "__main__":
    test_empty_logs_do_not_crash()
    test_events_before_shadow_start_ignored()
    test_grouping_by_reason_prefix_and_action()
    test_price_confidence_edge_reward_risk_buckets_work()
    test_quarantined_candidates_counted_but_filter_idea_simulation_only()
    test_main_bottleneck_market_quality_dominance()
    test_main_bottleneck_edge_weakness_dominance()
    test_main_bottleneck_quarantine_dominance()
    test_main_bottleneck_mixed_problem()
    test_no_live_strategy_mutation_occurs()
    print("UPSTREAM_CANDIDATE_QUALITY_AUTOPSY_TESTS_OK")
