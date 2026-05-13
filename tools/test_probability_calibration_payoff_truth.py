#!/usr/bin/env python3
"""
Phase 9U — Probability Calibration + Payoff EV Truth Tests
Sentinel: PROVEN_PROBABILITY_CALIBRATION_PAYOFF_TRUTH_OK
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_probability_calibration_payoff_truth as rpt

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
    *,
    timestamp: str,
    ticker: str,
    status: str = "SETTLED",
    result: str = "WIN",
    accounting_version: str = "economic_contract_notional_v1",
    entry_price: float = 0.85,
    size: float = 5.0,
    risk_edge: float = 0.06,
    model_probability: float | None = 0.88,
    pnl: float = 0.0,
    economic_pnl: float = 0.0,
    recorded_pnl: float = 0.0,
    clv: float | None = 0.0,
    data_collection_override: bool = False,
    bootstrap_provisional: bool = False,
    side_coverage_test: bool = False,
    council_reason: str = "No Builder boost; Critic allowed with caution. Builder: No historically strong matching pattern found.",
    bootstrap_era_council_allow: bool = False,
) -> dict:
    yes_bid = round(max(0.0, entry_price - 0.01), 2)
    yes_ask = round(min(0.99, entry_price + 0.01), 2)
    no_bid = round(max(0.0, 1.0 - entry_price - 0.01), 2)
    no_ask = round(min(0.99, 1.0 - entry_price + 0.01), 2)
    row = {
        "timestamp": timestamp,
        "ticker": ticker,
        "status": status,
        "result": result,
        "action": "BET_YES",
        "accounting_version": accounting_version,
        "entry_price": entry_price,
        "yes_ask": yes_ask,
        "yes_bid": yes_bid,
        "no_ask": no_ask,
        "no_bid": no_bid,
        "price_yes": yes_ask,
        "price_no": no_ask,
        "yes_mid": round((yes_bid + yes_ask) / 2, 3),
        "no_mid": round((no_bid + no_ask) / 2, 3),
        "market_mid": round((yes_bid + yes_ask) / 2, 3),
        "size": size,
        "risk_edge": risk_edge,
        "edge": risk_edge,
        "model_probability": model_probability,
        "capital_at_risk": round(entry_price * size, 2),
        "payout_notional": round(size, 2),
        "max_profit_if_win": round((1.0 - entry_price) * size, 2),
        "max_loss_if_loss": round(entry_price * size, 2),
        "pnl": pnl,
        "economic_pnl": economic_pnl,
        "recorded_pnl": recorded_pnl,
        "clv": clv,
        "data_collection_override": data_collection_override,
        "bootstrap_provisional": bootstrap_provisional,
        "side_coverage_test": side_coverage_test,
        "council_reason": council_reason,
        "bootstrap_era_council_allow": bootstrap_era_council_allow,
    }
    return row


def synthetic_rows() -> list[dict]:
    rows = []
    for i in range(20):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:00:{i:02d}Z",
                ticker=f"GOOD-{i}",
                entry_price=0.60 if i < 10 else 0.85,
                risk_edge=0.04 if i < 10 else 0.12,
                model_probability=0.62 if i < 10 else 0.88,
                result="WIN" if i % 2 == 0 else "LOSS",
                economic_pnl=1.20 if i % 2 == 0 else -4.20,
                recorded_pnl=1.20 if i % 2 == 0 else -4.20,
                pnl=1.20 if i % 2 == 0 else -4.20,
                clv=0.02 if i % 2 == 0 else -0.01,
                council_reason="No Builder boost; Critic allowed with caution. Builder: No historically strong matching pattern found.",
            )
        )
    rows.extend(
        [
            make_row(timestamp="2099-01-01T00:01:00Z", ticker="KXETH-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01),
            make_row(timestamp="2099-01-01T00:01:01Z", ticker="DC-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, data_collection_override=True),
            make_row(timestamp="2099-01-01T00:01:02Z", ticker="BOOT-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, bootstrap_provisional=True),
            make_row(timestamp="2099-01-01T00:01:03Z", ticker="SIDE-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, side_coverage_test=True),
            make_row(timestamp="2099-01-01T00:01:04Z", ticker="OPEN-BAD", status="OPEN", result="WIN", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:01:05Z", ticker="LEG-BAD", accounting_version="legacy_hybrid_or_unversioned", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:01:06Z", ticker="NO-PROB", entry_price=0.85, risk_edge=0.06, model_probability=None, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:01:07Z", ticker="ZERO-ENTRY", entry_price=0.0, risk_edge=0.04, model_probability=0.55, economic_pnl=0.0, recorded_pnl=0.0, pnl=0.0, clv=None),
        ]
    )
    return rows


def test_population_filters_and_zero_values() -> None:
    rows = synthetic_rows()
    fresh = rpt.fresh_proof_rows(rows)
    cal = rpt.calibration_rows(rows)
    if len(fresh) != 21:
        fail("fresh_rows_count", str(len(fresh)))
        return
    if len(cal) != 21:
        fail("calibration_rows_count", str(len(cal)))
        return
    if any(r["ticker"] in {"KXETH-BAD", "DC-BAD", "BOOT-BAD", "SIDE-BAD", "OPEN-BAD", "LEG-BAD"} for r in cal):
        fail("contamination_exclusion", [r["ticker"] for r in cal])
        return
    if rpt.entry_price({"entry_price": 0.0, "yes_ask": 0.87}) != 0.0:
        fail("entry_price_zero_preserved")
        return
    if rpt.model_probability_value({"model_probability": 0.0}) != 0.0:
        fail("model_probability_zero_preserved")
        return
    ok("population_filters_and_zero_values")


def test_bucket_math_and_calibration_metrics() -> None:
    rows = synthetic_rows()
    cal = rpt.calibration_rows(rows)
    summary = rpt._bucket_summary(cal)
    expected_roi = summary["total_economic_pnl"] / summary["total_capital_at_risk"]
    if round(summary["breakeven_wr"], 4) != round(summary["avg_entry_price"], 4):
        fail("breakeven_math", str(summary))
        return
    if round(summary["roi"], 6) != round(expected_roi, 6):
        fail("roi_math", f"got={summary['roi']} expected={expected_roi}")
        return
    if summary["profit_factor"] is None:
        fail("pf_missing", str(summary))
        return
    if summary["brier_score"] is None or summary["mean_absolute_error"] is None:
        fail("calibration_metrics_missing", str(summary))
        return
    if rpt.model_probability_bucket(0.92) != "0.90+" or rpt.model_probability_bucket(0.59) != "<0.60":
        fail("prob_bucket_math")
        return
    if rpt.entry_price_bucket_value(0.85) != "0.80-0.90" or rpt.edge_bucket_value(0.06) != "0.05-0.10":
        fail("price_edge_bucket_math")
        return
    if summary["brier_score"] is None or summary["mean_absolute_error"] is None:
        fail("calibration_metrics_missing", str(summary))
        return
    ok("bucket_math_and_calibration_metrics")


def test_bucket_status_and_cells() -> None:
    rows = synthetic_rows()
    cal = rpt.calibration_rows(rows)
    prob_buckets = rpt.summarize_buckets(cal, lambda r: rpt.model_probability_bucket(rpt.model_probability_value(r)), list(rpt.PROBABILITY_BUCKETS))
    if "0.80-0.90" not in prob_buckets:
        fail("prob_bucket_missing", list(prob_buckets))
        return
    if rpt._bucket_status(prob_buckets["0.80-0.90"]) not in {"OVERPAID", "POISON"}:
        fail("prob_bucket_status", rpt._bucket_status(prob_buckets["0.80-0.90"]))
        return
    cells = rpt.summarize_cells(cal)
    cell_key = "0.80-0.90|0.80-0.90"
    if cell_key not in cells:
        fail("cell_missing", list(cells))
        return
    if rpt._cell_status(cells[cell_key]) not in {"POISON", "OVERPAID", "GOOD", "MODEL_EDGE_FAKE"}:
        fail("cell_status", rpt._cell_status(cells[cell_key]))
        return
    ok("bucket_status_and_cells")


def test_report_is_read_only_and_prints_sentinel() -> None:
    before = file_hash(TRADES_LOG)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        state = rpt.build_report_state(synthetic_rows())
        rpt.render_report(state)
    after = file_hash(TRADES_LOG)
    out = buf.getvalue()
    if before != after:
        fail("report_read_only", "paper_trades hash changed")
        return
    if rpt.SENTINEL not in out:
        fail("report_sentinel", "sentinel missing")
        return
    if "DO_NOT_PATCH_LIVE_YET" not in out:
        fail("report_verdict", "missing verdict")
        return
    ok("report_is_read_only_and_prints_sentinel")


def main() -> None:
    print()
    print("=" * 84)
    print("PHASE 9U — PROBABILITY CALIBRATION + PAYOFF EV TRUTH TESTS")
    print("Sentinel: PROVEN_PROBABILITY_CALIBRATION_PAYOFF_TRUTH_OK")
    print("=" * 84)
    print()

    test_population_filters_and_zero_values()
    test_bucket_math_and_calibration_metrics()
    test_bucket_status_and_cells()
    test_report_is_read_only_and_prints_sentinel()

    print()
    total = len(PASS) + len(FAIL)
    print(f"Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"FAILED: {', '.join(FAIL)}")
        print("Sentinel NOT reached.")
        sys.exit(1)
    print()
    print("PROVEN_PROBABILITY_CALIBRATION_PAYOFF_TRUTH_OK")


if __name__ == "__main__":
    main()
