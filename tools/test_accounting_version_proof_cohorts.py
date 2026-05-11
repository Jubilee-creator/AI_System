#!/usr/bin/env python3
"""
Phase 9Q — Accounting-Version Proof Cohort Monitor Tests
Sentinel: PROVEN_ACCOUNTING_VERSION_PROOF_COHORTS_OK

Synthetic tests for read-only proof cohort classification and accounting metrics.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_accounting_version_proof_cohorts as rpt

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
PASS: list[str] = []
FAIL: list[str] = []


def ok(name: str) -> None:
    PASS.append(name)
    print(f"  PASS  {name}")


def fail(name: str, msg: str = "") -> None:
    FAIL.append(name)
    print(f"  FAIL  {name}  {msg}")


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_row(
    ticker: str = "KXBTCD-TEST",
    status: str = "SETTLED",
    result: str = "WIN",
    accounting_version=None,
    entry_price: float = 0.85,
    size: float = 10.0,
    risk_edge: float = 0.07,
    economic_pnl=None,
    recorded_pnl=None,
    data_collection_override: bool = False,
    bootstrap_provisional: bool = False,
) -> dict:
    row = {
        "timestamp": f"2099-01-01T00:00:{len(PASS)+len(FAIL):02d}+00:00",
        "ticker": ticker,
        "action": "BET_YES",
        "status": status,
        "result": result,
        "entry_price": entry_price,
        "yes_ask": entry_price,
        "size": size,
        "risk_edge": risk_edge,
        "edge": risk_edge,
        "model_probability": min(0.99, entry_price + risk_edge),
        "yes_bid": max(0.01, entry_price - 0.01),
        "no_bid": max(0.01, 1.0 - entry_price - 0.01),
        "no_ask": min(0.99, 1.0 - entry_price + 0.01),
        "data_collection_override": data_collection_override,
        "bootstrap_provisional": bootstrap_provisional,
        "side_coverage_test": False,
        "payout_notional": size,
        "capital_at_risk": round(entry_price * size, 2),
        "max_profit_if_win": round((1.0 - entry_price) * size, 2),
        "max_loss_if_loss": round(entry_price * size, 2),
        "pnl": 0.0,
    }
    if accounting_version is not None:
        row["accounting_version"] = accounting_version
    if economic_pnl is not None:
        row["economic_pnl"] = economic_pnl
    if recorded_pnl is not None:
        row["recorded_pnl"] = recorded_pnl
    return row


def test_classification() -> None:
    cases = [
        ({}, "legacy_hybrid_or_unversioned"),
        ({"accounting_version": "economic_contract_notional_v1"}, "economic_contract_notional_v1"),
        ({"accounting_version": "time_exit_mark_to_market_v1"}, "time_exit_mark_to_market_v1"),
        ({"accounting_version": "future_v9"}, "unknown_other"),
    ]
    for row, expected in cases:
        got = rpt.classify_accounting_version(row)
        if got != expected:
            fail("classification", f"expected {expected}, got {got}")
            return
    ok("classification")


def test_roi_and_pnl_separation() -> None:
    rows = [
        make_row(result="WIN", accounting_version="economic_contract_notional_v1",
                 economic_pnl=1.50, recorded_pnl=1.25, entry_price=0.85, size=10),
        make_row(result="LOSS", accounting_version="economic_contract_notional_v1",
                 economic_pnl=-8.50, recorded_pnl=-9.00, entry_price=0.85, size=10),
    ]
    metrics = rpt.cohort_metrics(rows)["economic_contract_notional_v1"]
    if round(metrics["total_economic_pnl"], 2) != -7.00:
        fail("economic_pnl_total", metrics)
        return
    if round(metrics["total_recorded_pnl"], 2) != -7.75:
        fail("recorded_pnl_total", metrics)
        return
    if round(metrics["roi_on_capital_at_risk"], 4) != round(-7.0 / 17.0, 4):
        fail("capital_at_risk_roi", metrics)
        return
    ok("roi_and_pnl_separation")


def test_kxeth_and_normal_modern_filters() -> None:
    rows = [
        make_row(ticker="KXBTCD-GOOD", accounting_version="economic_contract_notional_v1", economic_pnl=1.0, recorded_pnl=1.0),
        make_row(ticker="KXETH-QUARANTINED", result="LOSS", accounting_version="economic_contract_notional_v1",
                 economic_pnl=-100.0, recorded_pnl=-100.0),
        make_row(ticker="KXBTCD-DC", result="LOSS", accounting_version="economic_contract_notional_v1",
                 economic_pnl=-100.0, recorded_pnl=-100.0,
                 data_collection_override=True),
        make_row(ticker="KXBTCD-BOOT", result="LOSS", accounting_version="economic_contract_notional_v1",
                 economic_pnl=-100.0, recorded_pnl=-100.0,
                 bootstrap_provisional=True),
    ]
    metrics = rpt.cohort_metrics(rows)["economic_contract_notional_v1"]
    if metrics["clean_proof_rows"] != 1:
        fail("clean_proof_excludes_kxeth_and_contaminated", metrics)
        return
    if metrics["kxeth_rows"] != 1:
        fail("kxeth_count", metrics)
        return
    if metrics["total_economic_pnl"] != 1.0:
        fail("kxeth_contaminated_pnl", metrics)
        return
    if metrics["win_rate"] != 1.0:
        fail("kxeth_contaminated_win_rate", metrics)
        return
    if round(metrics["roi_on_capital_at_risk"], 4) != round(1.0 / 8.5, 4):
        fail("contaminated_roi", metrics)
        return
    ok("kxeth_and_normal_modern_filters")


def test_zero_values_preserved() -> None:
    if rpt.stored_pnl_value({"pnl": 0.0, "realized_pnl": 9.0}) != 0.0:
        fail("stored_pnl_zero_preserved")
        return
    if rpt.entry_price({"entry_price": 0.0, "yes_ask": 0.85}) != 0.0:
        fail("entry_price_zero_preserved")
        return
    ok("zero_values_preserved")


def test_sweet_spot_detection() -> None:
    sweet = make_row(
        ticker="KXBTCD-SWEET",
        accounting_version="economic_contract_notional_v1",
        entry_price=0.85,
        risk_edge=0.07,
        economic_pnl=1.5,
        recorded_pnl=1.5,
    )
    wrong_price = make_row(
        ticker="KXBTCD-LOW",
        accounting_version="economic_contract_notional_v1",
        entry_price=0.75,
        risk_edge=0.07,
        economic_pnl=2.5,
        recorded_pnl=2.5,
    )
    wrong_edge = make_row(
        ticker="KXBTCD-EDGE",
        accounting_version="economic_contract_notional_v1",
        entry_price=0.85,
        risk_edge=0.11,
        economic_pnl=1.5,
        recorded_pnl=1.5,
    )
    metrics = rpt.sweet_spot_metrics([sweet, wrong_price, wrong_edge])
    if metrics["count"] != 1 or metrics["wins"] != 1:
        fail("sweet_spot_detection", metrics)
        return
    if round(metrics["roi_on_capital_at_risk"], 4) != round(1.5 / 8.5, 4):
        fail("sweet_spot_roi", metrics)
        return
    ok("sweet_spot_detection")


def test_sample_warning() -> None:
    rows = [
        make_row(ticker=f"KXBTCD-SMALL-{i}", accounting_version="economic_contract_notional_v1",
                 economic_pnl=1.0, recorded_pnl=1.0)
        for i in range(3)
    ]
    metrics = rpt.cohort_metrics(rows)["economic_contract_notional_v1"]
    if metrics["sample_ge_30"] or not metrics["minimum_sample_warning"] or not metrics["too_small_to_trust"]:
        fail("sample_warning", metrics)
        return
    if metrics["clean_proof_rows"] != 3:
        fail("sample_count_clean_proof", metrics)
        return
    ok("sample_warning")


def test_two_row_economic_sample_not_enough_data() -> None:
    rows = [
        make_row(ticker="KXBTCD-FRESH-1", accounting_version="economic_contract_notional_v1",
                 economic_pnl=1.0, recorded_pnl=1.0),
        make_row(ticker="KXBTCD-FRESH-2", result="LOSS", accounting_version="economic_contract_notional_v1",
                 economic_pnl=-8.5, recorded_pnl=-8.5),
    ]
    metrics = rpt.cohort_metrics(rows)["economic_contract_notional_v1"]
    if metrics["clean_proof_rows"] != 2:
        fail("two_row_sample_count", metrics)
        return
    if metrics["sample_ge_30"] or not metrics["too_small_to_trust"]:
        fail("two_row_not_enough_data", metrics)
        return
    ok("two_row_economic_sample_not_enough_data")


def test_report_does_not_write_paper_log_and_prints_sentinel() -> None:
    before = file_hash(TRADES_LOG)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rpt.main()
    after = file_hash(TRADES_LOG)
    out = buf.getvalue()
    if before != after:
        fail("report_read_only", "paper_trades.jsonl hash changed")
        return
    if rpt.SENTINEL not in out:
        fail("report_sentinel", "sentinel missing")
        return
    ok("report_read_only_and_sentinel")


def main() -> None:
    print()
    print("=" * 72)
    print("PHASE 9Q — ACCOUNTING-VERSION PROOF COHORT TESTS")
    print("Sentinel: PROVEN_ACCOUNTING_VERSION_PROOF_COHORTS_OK")
    print("=" * 72)
    print()

    test_classification()
    test_roi_and_pnl_separation()
    test_kxeth_and_normal_modern_filters()
    test_zero_values_preserved()
    test_sweet_spot_detection()
    test_sample_warning()
    test_two_row_economic_sample_not_enough_data()
    test_report_does_not_write_paper_log_and_prints_sentinel()

    print()
    total = len(PASS) + len(FAIL)
    print(f"Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"FAILED: {', '.join(FAIL)}")
        print("Sentinel NOT reached.")
        sys.exit(1)
    print()
    print("Sentinel: PROVEN_ACCOUNTING_VERSION_PROOF_COHORTS_OK")
    print()


if __name__ == "__main__":
    main()
