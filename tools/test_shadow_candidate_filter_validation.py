#!/usr/bin/env python3
"""
Phase 9T — Shadow Candidate Filter Validation Tests
Sentinel: PROVEN_SHADOW_CANDIDATE_FILTER_VALIDATION_OK
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_shadow_candidate_filter_validation as rpt

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
    pnl: float = 0.0,
    economic_pnl: float = 0.0,
    recorded_pnl: float = 0.0,
    clv: float | None = None,
    data_collection_override: bool = False,
    bootstrap_provisional: bool = False,
    side_coverage_test: bool = False,
    council_reason: str = "No Builder boost; Critic allowed with caution. Builder: No historically strong matching pattern found.",
    bootstrap_era_council_allow: bool = False,
) -> dict:
    yes_bid = round(max(0.0, entry_price - 0.01), 2)
    yes_ask = round(min(0.99, entry_price + 0.01), 2)
    model_probability = round(min(0.99, max(0.01, entry_price + risk_edge)), 3)
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
        "model_probability": model_probability,
        "risk_edge": risk_edge,
        "edge": risk_edge,
        "size": size,
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
    # discovery rows: allowlist should freeze to the positive A cell only
    for i in range(5):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:00:0{i}Z",
                ticker=f"DISC-A-{i}",
                entry_price=0.85,
                risk_edge=0.06,
                result="WIN",
                economic_pnl=1.00,
                recorded_pnl=1.00,
                pnl=1.00,
                clv=0.02,
            )
        )
    for i in range(5, 10):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:00:{i}Z",
                ticker=f"DISC-B-{i}",
                entry_price=0.85,
                risk_edge=0.12,
                council_reason="Builder found positive historical pattern.",
                result="LOSS",
                economic_pnl=-4.25,
                recorded_pnl=-4.25,
                pnl=-4.25,
                clv=-0.01,
            )
        )
    # validation rows: same cell A plus a validation-only cell B that must not be adopted
    rows.extend(
        [
            make_row(timestamp="2099-01-01T00:01:00Z", ticker="VAL-A-1", entry_price=0.85, risk_edge=0.06, result="WIN", economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:01:01Z", ticker="VAL-B-1", entry_price=0.85, risk_edge=0.12, council_reason="Builder found positive historical pattern.", result="WIN", economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:01:02Z", ticker="VAL-B-2", entry_price=0.85, risk_edge=0.12, council_reason="Builder found positive historical pattern.", result="LOSS", economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01),
            make_row(timestamp="2099-01-01T00:01:03Z", ticker="VAL-ZERO", entry_price=0.0, risk_edge=0.04, result="WIN", economic_pnl=0.0, recorded_pnl=0.0, pnl=0.0, clv=None),
            make_row(timestamp="2099-01-01T00:01:04Z", ticker="KXETH-VAL", entry_price=0.85, risk_edge=0.06, result="WIN", economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:01:05Z", ticker="VAL-DC", entry_price=0.85, risk_edge=0.06, result="WIN", economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01, data_collection_override=True),
            make_row(timestamp="2099-01-01T00:01:06Z", ticker="VAL-BOOT", entry_price=0.85, risk_edge=0.06, result="WIN", economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01, bootstrap_provisional=True),
            make_row(timestamp="2099-01-01T00:01:07Z", ticker="VAL-OPEN", status="OPEN", entry_price=0.85, risk_edge=0.06, result="WIN", economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
        ]
    )
    return rows


def test_split_and_fresh_filter() -> None:
    rows = synthetic_rows()
    fresh = rpt.fresh_proof_rows(rows)
    disc, val, cutoff = rpt.split_discovery_validation(fresh, discovery_fraction=0.60)
    if len(fresh) != 14:
        fail("fresh_rows_count", str(len(fresh)))
        return
    if len(disc) != 8 or len(val) != 6 or cutoff is None:
        fail("split_counts", f"disc={len(disc)} val={len(val)} cutoff={cutoff}")
        return
    ids = {r["ticker"] for r in fresh}
    if {"KXETH-VAL", "VAL-DC", "VAL-BOOT", "VAL-OPEN"} & ids:
        fail("fresh_filter_exclusion", str(ids))
        return
    if rpt.entry_price({"entry_price": 0.0, "yes_ask": 0.87}) != 0.0:
        fail("entry_price_zero_preserved")
        return
    ok("split_and_fresh_filter")


def test_metrics_and_zero_values() -> None:
    rows = synthetic_rows()
    fresh = rpt.fresh_proof_rows(rows)
    summary = rpt._summary(fresh)
    if round(summary["breakeven_wr"], 4) != round(summary["avg_entry_price"], 4):
        fail("breakeven_math", str(summary))
        return
    expected_roi = summary["total_economic_pnl"] / summary["total_capital_at_risk"]
    expected_pf = sum(
        max(0.0, float(r.get("economic_pnl") or 0.0))
        for r in fresh
    ) / abs(
        sum(min(0.0, float(r.get("economic_pnl") or 0.0)) for r in fresh)
    )
    if round(summary["roi"], 6) != round(expected_roi, 6):
        fail("roi_math", f"got={summary['roi']} expected={expected_roi}")
        return
    if summary["profit_factor"] is None or round(summary["profit_factor"], 6) != round(expected_pf, 6):
        fail("pf_math", str(summary["profit_factor"]))
        return
    if summary["avg_clv"] is None:
        fail("clv_missing", str(summary))
        return
    if summary["max_drawdown"] is None:
        fail("drawdown_missing", str(summary))
        return
    if rpt.stored_pnl_value({"pnl": 0.0, "realized_pnl": 7.5}) != 0.0:
        fail("stored_pnl_zero_preserved")
        return
    if rpt.recorded_pnl_value({"recorded_pnl": 0.0}) != 0.0:
        fail("recorded_pnl_zero_preserved")
        return
    ok("metrics_and_zero_values")


def test_frozen_discovery_filters() -> None:
    rows = synthetic_rows()
    fresh = rpt.fresh_proof_rows(rows)
    disc, val, _ = rpt.split_discovery_validation(fresh, discovery_fraction=0.60)
    specs = rpt.build_candidate_specs(disc)
    cell_spec = next(s for s in specs if s.name == "discovery_cells_positive_pnl")
    if len(cell_spec.frozen_cells) != 1:
        fail("frozen_cell_count", str(cell_spec.frozen_cells))
        return
    if "0.05-0.10|0.80-0.90" not in cell_spec.frozen_cells:
        fail("frozen_cell_identity", str(cell_spec.frozen_cells))
        return
    passed_val = cell_spec.apply(val)
    if any(r["ticker"] == "VAL-B-1" for r in passed_val):
        fail("validation_reoptimized", [r["ticker"] for r in passed_val])
        return
    if not any(r["ticker"] == "VAL-A-1" for r in passed_val):
        fail("validation_missing_frozen_cell")
        return
    ok("frozen_discovery_filters")


def test_candidate_status_and_report_read_only() -> None:
    rows = synthetic_rows()
    before = file_hash(TRADES_LOG)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        state = rpt.build_report_state(rows)
        rpt.render_report(state)
    after = file_hash(TRADES_LOG)
    out = buf.getvalue()
    if before != after:
        fail("report_read_only", "paper_trades hash changed")
        return
    if rpt.SENTINEL not in out:
        fail("report_sentinel", "sentinel missing")
        return
    if "SHADOW_VALIDATION_TOO_SMALL" not in out and "PROMISING_BUT_UNPROVEN" not in out and "FAILED_SHADOW_VALIDATION" not in out:
        fail("report_status_label", "missing expected status label")
        return
    if state["overall_status"] not in {"PROMISING_BUT_UNPROVEN", "FAILED_SHADOW_VALIDATION", "SHADOW_VALIDATION_TOO_SMALL", "DISCOVERY_ONLY"}:
        fail("overall_status", state["overall_status"])
        return
    ok("candidate_status_and_report_read_only")


def main() -> None:
    print()
    print("=" * 72)
    print("PHASE 9T — SHADOW CANDIDATE FILTER VALIDATION TESTS")
    print("Sentinel: PROVEN_SHADOW_CANDIDATE_FILTER_VALIDATION_OK")
    print("=" * 72)
    print()

    test_split_and_fresh_filter()
    test_metrics_and_zero_values()
    test_frozen_discovery_filters()
    test_candidate_status_and_report_read_only()

    print()
    total = len(PASS) + len(FAIL)
    print(f"Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"FAILED: {', '.join(FAIL)}")
        print("Sentinel NOT reached.")
        sys.exit(1)
    print()
    print("Sentinel: PROVEN_SHADOW_CANDIDATE_FILTER_VALIDATION_OK")
    print()


if __name__ == "__main__":
    main()
