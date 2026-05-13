#!/usr/bin/env python3
"""
Phase 9V — Rolling Calibration Monitor Tests
Sentinel: PROVEN_ROLLING_CALIBRATION_MONITOR_OK
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_rolling_calibration_monitor as rpt

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
    return {
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


def synthetic_rows() -> list[dict]:
    rows = []
    # 20 early builder_boost rows, mostly losing, high-entry poison
    for i in range(20):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:00:{i:02d}Z",
                ticker=f"BLD-{i}",
                entry_price=0.85,
                risk_edge=0.12,
                model_probability=0.88,
                result="WIN" if i % 5 == 0 else "LOSS",
                economic_pnl=0.75 if i % 5 == 0 else -4.25,
                recorded_pnl=0.75 if i % 5 == 0 else -4.25,
                pnl=0.75 if i % 5 == 0 else -4.25,
                clv=0.01 if i % 5 == 0 else -0.01,
                council_reason="Builder found positive historical pattern.",
            )
        )
    # 20 middle 0.90+ critic_caution rows, small positive pocket
    for i in range(20, 40):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:01:{i-20:02d}Z",
                ticker=f"CRT-{i}",
                entry_price=0.92,
                risk_edge=0.04,
                model_probability=0.93,
                result="WIN" if i % 4 != 0 else "LOSS",
                economic_pnl=0.40 if i % 4 != 0 else -4.60,
                recorded_pnl=0.40 if i % 4 != 0 else -4.60,
                pnl=0.40 if i % 4 != 0 else -4.60,
                clv=0.02 if i % 4 != 0 else -0.02,
                council_reason="No Builder boost; Critic allowed with caution. Builder: No historically strong matching pattern found.",
            )
        )
    # 15 later lower-entry rows, mixed but less harmful
    for i in range(40, 55):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:02:{i-40:02d}Z",
                ticker=f"LOW-{i}",
                entry_price=0.65,
                risk_edge=0.04,
                model_probability=0.72,
                result="WIN" if i % 3 != 0 else "LOSS",
                economic_pnl=1.75 if i % 3 != 0 else -2.25,
                recorded_pnl=1.75 if i % 3 != 0 else -2.25,
                pnl=1.75 if i % 3 != 0 else -2.25,
                clv=0.03 if i % 3 != 0 else -0.03,
                council_reason="No Builder boost; Critic allowed with caution. Builder: No historically strong matching pattern found.",
            )
        )
    # Contaminated rows to ensure exclusion logic is active.
    rows.extend(
        [
            make_row(timestamp="2099-01-01T00:03:00Z", ticker="KXETH-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01),
            make_row(timestamp="2099-01-01T00:03:01Z", ticker="DC-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, data_collection_override=True),
            make_row(timestamp="2099-01-01T00:03:02Z", ticker="BOOT-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, bootstrap_provisional=True),
            make_row(timestamp="2099-01-01T00:03:03Z", ticker="SIDE-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, side_coverage_test=True),
            make_row(timestamp="2099-01-01T00:03:04Z", ticker="OPEN-BAD", status="OPEN", result="WIN", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:03:05Z", ticker="LEG-BAD", accounting_version="legacy_hybrid_or_unversioned", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:03:06Z", ticker="NO-PROB", entry_price=0.85, risk_edge=0.06, model_probability=None, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
        ]
    )
    return rows


def test_sorting_and_window_slices() -> None:
    rows = [
        make_row(timestamp="2099-01-01T00:00:03Z", ticker="B"),
        make_row(timestamp="2099-01-01T00:00:01Z", ticker="A"),
        make_row(timestamp="2099-01-01T00:00:02Z", ticker="C"),
    ]
    ordered = rpt.clean_monitor_rows(rows)
    if [r["ticker"] for r in ordered] != ["A", "C", "B"]:
        fail("sorting", [r["ticker"] for r in ordered])
        return
    if len(rpt._window_rows(ordered, 2)) != 2:
        fail("window_rows")
        return
    if len(rpt._trailing_series(ordered, 2)) != 2:
        fail("trailing_series")
        return
    if len(rpt._expanding_series(ordered)) != 3:
        fail("expanding_series")
        return
    ok("sorting_and_window_slices")


def test_confidence_and_metrics() -> None:
    rows = synthetic_rows()
    clean = rpt.clean_monitor_rows(rows)
    summary = rpt._window_summary(clean[:10])
    low, high = rpt._wilson_interval(summary["wins"], summary["n"])
    if summary["n"] != 10:
        fail("window_size", summary["n"])
        return
    if low is None or high is None or low >= high:
        fail("wilson_interval", (low, high))
        return
    if round(summary["roi"], 6) == 0.0:
        fail("roi_zero_unexpected", summary["roi"])
        return
    if summary["brier_score"] is None or summary["calibration_gap"] is None:
        fail("metrics_missing", summary)
        return
    if summary["model_ev_sum"] is None or summary["ev_gap"] is None:
        fail("ev_missing", summary)
        return
    ok("confidence_and_metrics")


def test_rolling_series_and_red_flags() -> None:
    rows = synthetic_rows()
    state = rpt.build_report_state(rows)
    clean = state["clean_rows"]
    if len(clean) != 55:
        fail("clean_rows_count", len(clean))
        return
    if len(state["trailing_series"][10]) != len(clean) - 10 + 1:
        fail("trailing_10_count", len(state["trailing_series"][10]))
        return
    if len(state["trailing_series"][20]) != len(clean) - 20 + 1:
        fail("trailing_20_count", len(state["trailing_series"][20]))
        return
    if len(state["trailing_series"][30]) != len(clean) - 30 + 1:
        fail("trailing_30_count", len(state["trailing_series"][30]))
        return
    if len(state["trailing_series"][50]) != len(clean) - 50 + 1:
        fail("trailing_50_count", len(state["trailing_series"][50]))
        return
    if state["trailing_summaries"][10]["model_ev_fake"] <= 0:
        fail("model_ev_fake_missing", state["trailing_summaries"][10])
        return
    if state["trailing_summaries"][10]["high_entry_poison"] <= 0:
        fail("high_entry_poison_missing", state["trailing_summaries"][10])
        return
    if state["trailing_summaries"][10]["builder_boost_poison"] <= 0:
        fail("builder_poison_missing", state["trailing_summaries"][10])
        return
    if state["trailing_summaries"][10]["edge_bucket_misleading"] <= 0:
        fail("edge_poison_missing", state["trailing_summaries"][10])
        return
    if state["expanding_summary"]["windows"] != len(clean):
        fail("expanding_windows", state["expanding_summary"])
        return
    if state["overall_status"] != "DO_NOT_PATCH_LIVE_YET":
        fail("overall_status", state["overall_status"])
        return
    ok("rolling_series_and_red_flags")


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
        fail("sentinel", "missing")
        return
    if "DO_NOT_PATCH_LIVE_YET" not in out:
        fail("verdict", "missing")
        return
    ok("report_is_read_only_and_prints_sentinel")


def test_zero_and_missing_helpers() -> None:
    if rpt.entry_price({"entry_price": 0.0, "yes_ask": 0.88}) != 0.0:
        fail("zero_entry_price")
        return
    if rpt.calib.model_probability_value({"model_probability": 0.0}) != 0.0:
        fail("zero_probability")
        return
    if rpt._wilson_interval(0, 0) != (None, None):
        fail("wilson_empty")
        return
    ok("zero_and_missing_helpers")


def main() -> None:
    print()
    print("=" * 92)
    print("PHASE 9V — ROLLING CALIBRATION MONITOR TESTS")
    print("Sentinel: PROVEN_ROLLING_CALIBRATION_MONITOR_OK")
    print("=" * 92)
    print()

    test_sorting_and_window_slices()
    test_confidence_and_metrics()
    test_rolling_series_and_red_flags()
    test_zero_and_missing_helpers()
    test_report_is_read_only_and_prints_sentinel()

    print()
    total = len(PASS) + len(FAIL)
    print(f"Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"FAILED: {', '.join(FAIL)}")
        print("Sentinel NOT reached.")
        sys.exit(1)
    print()
    print("PROVEN_ROLLING_CALIBRATION_MONITOR_OK")


if __name__ == "__main__":
    main()
