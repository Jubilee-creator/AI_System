#!/usr/bin/env python3
"""Tests for Phase 10J calibration payoff truth map."""
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
from tools import report_calibration_payoff_truth_map as report  # noqa: E402


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
        "result": "WIN",
        "action": "BET_YES",
        "scanner_action": "BET_YES",
        "entry_price": 0.80,
        "size": 5.0,
        "model_probability": 0.85,
        "confidence": 0.85,
        "risk_edge": 0.04,
        "edge": 0.04,
        "economic_pnl": 1.0,
        "recorded_pnl": 1.0,
        "pnl": 1.0,
        "capital_at_risk": 4.0,
        "payout_notional": 5.0,
        "max_profit_if_win": 1.0,
        "max_loss_if_loss": 4.0,
        "accounting_version": "economic_contract_notional_v1",
        "data_collection_override": False,
        "bootstrap_provisional": False,
        "side_coverage": False,
        "side_coverage_test": False,
        "council_decision": "ALLOW",
    }
    row.update(overrides)
    return row


def _funnel(**overrides: Any) -> dict[str, Any]:
    row = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "ticker": "KXBTCD-TEST",
        "scanner_action": "BET_YES",
        "confidence": 0.86,
        "edge": 0.04,
        "yes_ask": 0.82,
        "no_ask": 0.19,
        "final_reason": "BLOCKED_MARKET_QUALITY",
    }
    row.update(overrides)
    return row


def test_empty_and_missing_logs_safe() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        built = report.build_report(tmp / "missing_paper.jsonl", tmp / "missing_funnel.jsonl")
        if built["counts"]["calibration_rows"] != 0:
            return "missing logs should produce zero calibration rows"
        if built["overall"]["status"] != "TOO_SMALL":
            return "empty overall should be too small"
    return None


def test_report_is_read_only_and_missing_fields_safe() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        trades = tmp / "paper.jsonl"
        funnel = tmp / "funnel.jsonl"
        _write_jsonl(trades, [_trade(), {"ticker": "MISSING"}])
        _write_jsonl(funnel, [_funnel(), {"ticker": "MISSING"}])
        before = {path: _sha(path) for path in (trades, funnel)}
        built = report.build_report(trades, funnel)
        after = {path: _sha(path) for path in (trades, funnel)}
        if before != after:
            return "report modified input logs"
        if built["counts"]["funnel_rows"] != 2:
            return "missing-field funnel row not handled"
    return None


def test_trust_gate_requires_n_roi_pf_margin_and_calibration() -> str | None:
    tiny = report._bucket_summary([_trade() for _ in range(3)])
    if tiny["status"] == "TRUSTED":
        return "n<30 bucket trusted"
    negative = report._bucket_summary([
        _trade(result="LOSS", economic_pnl=-4.0, recorded_pnl=-4.0, pnl=-4.0) for _ in range(30)
    ])
    if negative["status"] == "TRUSTED":
        return "negative ROI bucket trusted"
    weak_pf_rows = [
        _trade(result="WIN", economic_pnl=0.10, recorded_pnl=0.10, pnl=0.10, max_profit_if_win=0.10) for _ in range(24)
    ] + [
        _trade(result="LOSS", economic_pnl=-1.00, recorded_pnl=-1.00, pnl=-1.00, max_loss_if_loss=1.00) for _ in range(6)
    ]
    weak_pf = report._bucket_summary(weak_pf_rows)
    if weak_pf["profit_factor"] is not None and weak_pf["profit_factor"] <= 1.10 and weak_pf["status"] == "TRUSTED":
        return "PF <= 1.10 bucket trusted"
    return None


def test_tiny_positive_overconfidence_and_below_breakeven_flags() -> str | None:
    tiny = report._bucket_summary([_trade() for _ in range(2)])
    if "TINY_POSITIVE_TRAP" not in tiny["flags"]:
        return "positive tiny sample not flagged"
    overconfident = report._bucket_summary([
        _trade(result="LOSS", model_probability=0.90, confidence=0.90, economic_pnl=-4.0, recorded_pnl=-4.0, pnl=-4.0)
        for _ in range(10)
    ])
    if "OVERCONFIDENT" not in overconfident["flags"]:
        return "overconfidence not detected"
    below_be = report._bucket_summary([
        _trade(result="LOSS", model_probability=0.70, confidence=0.70, entry_price=0.80, economic_pnl=-4.0, recorded_pnl=-4.0, pnl=-4.0)
        for _ in range(10)
    ])
    if "MODEL_PROB_BELOW_BREAKEVEN" not in below_be["flags"]:
        return "model probability below breakeven not detected"
    return None


def test_safety_locks_preserved() -> str | None:
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
        built = report.build_report(tmp / "missing_paper.jsonl", tmp / "missing_funnel.jsonl")
        safety = built["safety"]
        if safety["real_money_allowed"] is not False:
            return "report claims real money allowed"
        if safety["scale_allowed"] is not False:
            return "report claims scale allowed"
        if safety["paper_only"] is not True:
            return "report lost paper-only status"
    return None


def run_tests() -> int:
    tests = [
        test_empty_and_missing_logs_safe,
        test_report_is_read_only_and_missing_fields_safe,
        test_trust_gate_requires_n_roi_pf_margin_and_calibration,
        test_tiny_positive_overconfidence_and_below_breakeven_flags,
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
    print("\nSentinel: CALIBRATION_PAYOFF_TRUTH_MAP_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
