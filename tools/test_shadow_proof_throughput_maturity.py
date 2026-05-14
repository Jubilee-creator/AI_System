#!/usr/bin/env python3
"""Tests for Phase 10L shadow proof throughput maturity audit."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import report_shadow_proof_throughput_maturity as report  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + ("\n" if rows else ""))


def _sha(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pick(ticker: str = "KXBTCD-TEST", action: str = "BET_YES", price: float = 0.70) -> dict[str, Any]:
    return {"ticker": ticker, "action": action, "entry_price": price, "rank": 1}


def _shadow(**overrides: Any) -> dict[str, Any]:
    pick = _pick()
    row = {
        "timestamp_utc": "2026-01-01T00:00:00+00:00",
        "scan_id": "s1",
        "run_id": "r1",
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
        "risk_edge": 0.04,
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
        "bootstrap_era_council_allow": True,
        "side_coverage": False,
        "side_coverage_test": False,
        "council_decision": "ALLOW",
        "yes_bid": 0.69,
        "yes_ask": 0.70,
        "no_bid": 0.29,
        "no_ask": 0.30,
    }
    row.update(overrides)
    return row


def _build(shadow_rows: list[dict[str, Any]], trade_rows: list[dict[str, Any]]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shadow = tmp / "shadow.jsonl"
        trades = tmp / "paper.jsonl"
        _write_jsonl(shadow, shadow_rows)
        _write_jsonl(trades, trade_rows)
        return report.build_report(shadow, trades)


def test_empty_logs_and_read_only() -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shadow = tmp / "missing_shadow.jsonl"
        trades = tmp / "missing_paper.jsonl"
        before = {path: _sha(path) for path in (shadow, trades)}
        built = report.build_report(shadow, trades)
        after = {path: _sha(path) for path in (shadow, trades)}
        if before != after:
            return "report modified missing inputs"
        if built["shadow_rows"] != 0:
            return "empty shadow count wrong"
    return None


def test_open_rows_counted_but_excluded() -> str | None:
    built = _build([_shadow()], [_trade(status="OPEN", result=None, economic_pnl=0.0, settled_at=None)])
    if built["open_rows_after_shadow_start"] != 1:
        return "open row not counted"
    if built["clean_settled_trades_after_shadow_start"] != 0:
        return "open row counted as clean settled"
    if built["excluded_rows_by_reason"].get("not_settled") != 1:
        return "open exclusion not recorded"
    if built["main_bottleneck"] != "OPEN_TRADES_NOT_SETTLING":
        return f"wrong bottleneck {built['main_bottleneck']}"
    return None


def test_settled_clean_counted_correctly() -> str | None:
    built = _build([_shadow()], [_trade()])
    if built["settled_trades_after_shadow_start"] != 1:
        return "settled count wrong"
    if built["clean_settled_trades_after_shadow_start"] != 1:
        return "clean settled not counted"
    if built["shadow_match"]["current"]["matched"] != 1:
        return "clean trade did not match shadow pick"
    return None


def test_dirty_exclusions() -> str | None:
    rows = [
        _trade(ticker="KXETH-TEST"),
        _trade(data_collection_override=True),
        _trade(bootstrap_provisional=True),
        _trade(side_coverage=True),
        _trade(economic_pnl=None),
        _trade(accounting_version="legacy_hybrid_or_unversioned"),
        _trade(entry_price=None, yes_ask=None),
        _trade(action="", scanner_action=""),
    ]
    built = _build([_shadow()], rows)
    reasons = built["excluded_rows_by_reason"]
    for expected in (
        "kxeth_or_quarantined",
        "data_collection_override",
        "bootstrap_provisional",
        "side_coverage_contamination",
        "missing_economic_pnl",
        "wrong_accounting_version",
        "missing_entry_price",
        "missing_action",
        "not_normal_modern",
    ):
        if reasons.get(expected, 0) <= 0:
            return f"missing exclusion {expected}"
    if built["main_bottleneck"] != "SETTLED_BUT_NOT_CLEAN":
        return f"dirty settled bottleneck wrong: {built['main_bottleneck']}"
    return None


def test_unmatched_shadow_mismatch_reasons() -> str | None:
    no_open = _build([_shadow()], [])
    if no_open["shadow_match"]["current"]["unmatched_reasons"].get("no_opened_trade") != 1:
        return "no opened trade reason missing"
    ticker = _build([_shadow()], [_trade(ticker="OTHER")])
    if ticker["shadow_match"]["current"]["unmatched_reasons"].get("ticker_mismatch") != 1:
        return "ticker mismatch missing"
    action = _build([_shadow()], [_trade(action="BET_NO", scanner_action="BET_NO")])
    if action["shadow_match"]["current"]["unmatched_reasons"].get("action_mismatch") != 1:
        return "action mismatch missing"
    price = _build([_shadow()], [_trade(entry_price=0.71, yes_ask=0.71)])
    if price["shadow_match"]["current"]["unmatched_reasons"].get("price_mismatch") != 1:
        return "price mismatch missing"
    return None


def test_bottleneck_no_opened_rows() -> str | None:
    built = _build([_shadow()], [])
    if built["main_bottleneck"] != "NO_FORWARD_OPENED_TRADES":
        return f"wrong no-open bottleneck {built['main_bottleneck']}"
    return None


def run_tests() -> int:
    tests = [
        test_empty_logs_and_read_only,
        test_open_rows_counted_but_excluded,
        test_settled_clean_counted_correctly,
        test_dirty_exclusions,
        test_unmatched_shadow_mismatch_reasons,
        test_bottleneck_no_opened_rows,
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
    print("\nSentinel: SHADOW_PROOF_THROUGHPUT_MATURITY_TESTS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
