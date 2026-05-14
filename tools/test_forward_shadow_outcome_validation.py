#!/usr/bin/env python3
"""Tests for Phase 10K forward shadow outcome validation."""
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
from tools import report_forward_shadow_outcome_validation as report  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))


def _sha(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pick(ticker: str = "KXBTCD-TEST", action: str = "BET_YES", price: float = 0.70) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "action": action,
        "entry_price": price,
        "reward_risk": (1.0 - price) / price,
        "confidence": 0.78,
        "rank": 1,
    }


def _shadow(**overrides: Any) -> dict[str, Any]:
    pick = _pick()
    row = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "run_id": "r1",
        "scan_id": "s1",
        "shadow_only": True,
        "execution_changed": False,
        "current_top_3": [pick],
        "payoff_aware_top_3": [pick],
        "strict_payoff_top_3": [pick],
    }
    row.update(overrides)
    return row


def _trade(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp": "2026-01-01T00:01:00+00:00",
        "settled_at": "2026-01-01T00:05:00+00:00",
        "ticker": "KXBTCD-TEST",
        "status": "SETTLED",
        "result": "WIN",
        "action": "BET_YES",
        "scanner_action": "BET_YES",
        "entry_price": 0.70,
        "size": 5.0,
        "model_probability": 0.78,
        "confidence": 0.78,
        "economic_pnl": 1.50,
        "recorded_pnl": 1.50,
        "pnl": 1.50,
        "capital_at_risk": 3.50,
        "payout_notional": 5.0,
        "max_profit_if_win": 1.50,
        "max_loss_if_loss": 3.50,
        "accounting_version": "economic_contract_notional_v1",
        "data_collection_override": False,
        "bootstrap_provisional": False,
        "side_coverage": False,
        "side_coverage_test": False,
        "council_decision": "ALLOW",
        "risk_edge": 0.04,
        "bootstrap_era_council_allow": True,
        "yes_bid": 0.69,
        "yes_ask": 0.70,
        "no_bid": 0.29,
        "no_ask": 0.30,
    }
    row.update(overrides)
    return row


def test_empty_logs_safe() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        built = report.build_report(tmp / "missing_shadow.jsonl", tmp / "missing_paper.jsonl")
        if built["shadow_rows"] != 0:
            return "missing shadow log should produce zero rows"
        if built["modes"]["current_shadow"]["maturity"] != "IMMATURE":
            return "empty current shadow should be immature"
        if built["answers"]["enough_proof_for_live_patching"] is not False:
            return "empty report allowed live patching"
    return None


def test_read_only_and_unmatched_shadow_safe() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shadow = tmp / "shadow.jsonl"
        trades = tmp / "paper.jsonl"
        _write_jsonl(shadow, [_shadow()])
        _write_jsonl(trades, [])
        before = {path: _sha(path) for path in (shadow, trades)}
        built = report.build_report(shadow, trades)
        after = {path: _sha(path) for path in (shadow, trades)}
        if before != after:
            return "report modified logs"
        if built["modes"]["payoff_aware_shadow"]["settled_rows"] != 0:
            return "unmatched shadow should have zero settled rows"
    return None


def test_partial_settlement_safe() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shadow = tmp / "shadow.jsonl"
        trades = tmp / "paper.jsonl"
        _write_jsonl(shadow, [_shadow()])
        _write_jsonl(trades, [_trade()])
        built = report.build_report(shadow, trades)
        if built["modes"]["payoff_aware_shadow"]["settled_rows"] != 1:
            return "matched settled row not counted"
        if built["modes"]["payoff_aware_shadow"]["maturity"] != "PARTIAL":
            return "single settled row should be partial"
        if built["answers"]["do_not_deploy_warning"] is not True:
            return "partial evidence should trigger do-not-deploy"
    return None


def test_trusted_threshold_enforced() -> str | None:
    good_rows = [_trade(timestamp=f"2026-01-01T00:{i % 60:02d}:00+00:00", settled_at=f"2026-01-01T01:{i % 60:02d}:00+00:00") for i in range(29)]
    summary = report._summarize_mode([_pick()], good_rows)
    if summary["trusted"]:
        return "n<30 settled rows trusted"
    good_rows.append(_trade(timestamp="2026-01-01T00:59:00+00:00", settled_at="2026-01-01T01:59:00+00:00"))
    summary = report._summarize_mode([_pick()], good_rows)
    if not summary["trusted"]:
        return "30 profitable settled rows should be trusted"
    bad_rows = [
        _trade(result="LOSS", economic_pnl=-3.50, recorded_pnl=-3.50, pnl=-3.50)
        for _ in range(30)
    ]
    if report._summarize_mode([_pick()], bad_rows)["trusted"]:
        return "negative outcome trusted"
    return None


def test_safety_locks_preserved() -> str | None:
    if trading_config.TRADING_MODE != "PAPER":
        return "TRADING_MODE changed"
    if not trading_config.GLOBAL_FORCED_LEARNING_MODE:
        return "Kelly/forced learning changed"
    if trading_config.DATA_COLLECTION_OVERRIDE_ENABLED:
        return "dc override changed"
    if "KXETH" not in {str(prefix).upper() for prefix in trading_config.QUARANTINED_TICKER_PREFIXES}:
        return "KXETH quarantine missing"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        built = report.build_report(tmp / "missing_shadow.jsonl", tmp / "missing_paper.jsonl")
        safety = built["safety"]
        if safety["real_money_allowed"] is not False:
            return "real money allowed"
        if safety["scale_allowed"] is not False:
            return "scale allowed"
        if safety["paper_only"] is not True:
            return "paper-only lost"
    return None


def run_tests() -> int:
    tests = [
        test_empty_logs_safe,
        test_read_only_and_unmatched_shadow_safe,
        test_partial_settlement_safe,
        test_trusted_threshold_enforced,
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
    print("\nSentinel: FORWARD_SHADOW_OUTCOME_VALIDATION_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
