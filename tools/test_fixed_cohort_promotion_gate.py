#!/usr/bin/env python3
"""
Phase 9W — Fixed-Cohort Promotion Gate Tests
Sentinel: PROVEN_FIXED_COHORT_PROMOTION_GATE_OK
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_fixed_cohort_promotion_gate as rpt

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
    return {
        "timestamp": timestamp,
        "ticker": ticker,
        "status": status,
        "result": result,
        "action": "BET_YES",
        "accounting_version": accounting_version,
        "entry_price": entry_price,
        "yes_ask": round(min(0.99, entry_price + 0.01), 2),
        "yes_bid": round(max(0.0, entry_price - 0.01), 2),
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
    rows: list[dict] = []
    # 20 builder_boost high-entry rows: poison.
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
    # 20 critic_caution 0.90+ rows: positive but sample too small.
    for i in range(20, 40):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:01:{i-20:02d}Z",
                ticker=f"CRT-{i}",
                entry_price=0.90,
                risk_edge=0.04,
                model_probability=0.93,
                result="LOSS" if i == 39 else "WIN",
                economic_pnl=-0.20 if i == 39 else 0.40,
                recorded_pnl=-0.20 if i == 39 else 0.40,
                pnl=-0.20 if i == 39 else 0.40,
                clv=-0.02 if i == 39 else 0.02,
                council_reason="No Builder boost; Critic allowed with caution. Builder: No historically strong matching pattern found.",
            )
        )
    # 20 lower-entry rows: mixed but not promotable.
    for i in range(40, 60):
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
    rows.extend(
        [
            make_row(timestamp="2099-01-01T00:03:00Z", ticker="KXETH-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01),
            make_row(timestamp="2099-01-01T00:03:01Z", ticker="DC-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, data_collection_override=True),
            make_row(timestamp="2099-01-01T00:03:02Z", ticker="BOOT-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, bootstrap_provisional=True),
            make_row(timestamp="2099-01-01T00:03:03Z", ticker="SIDE-BAD", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, side_coverage_test=True),
            make_row(timestamp="2099-01-01T00:03:04Z", ticker="OPEN-BAD", status="OPEN", result="WIN", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:03:05Z", ticker="LEG-BAD", accounting_version="legacy_hybrid_or_unversioned", entry_price=0.85, risk_edge=0.06, model_probability=0.88, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:03:06Z", ticker="NO-PROB", entry_price=0.85, risk_edge=0.06, model_probability=None, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
            make_row(timestamp="2099-01-01T00:03:07Z", ticker="ZERO-ENTRY", entry_price=0.0, risk_edge=0.04, model_probability=0.55, economic_pnl=0.0, recorded_pnl=0.0, pnl=0.0, clv=None),
        ]
    )
    return rows


def test_population_and_exclusions() -> None:
    rows = synthetic_rows()
    clean = rpt._clean_rows(rows)
    if len(clean) != 61:
        fail("clean_rows_count", len(clean))
        return
    state = rpt.build_report_state(rows)
    counts = state["counts"]
    if counts["excluded_total"] != 7:
        fail("excluded_total", counts["excluded_total"])
        return
    if counts["excluded_kxeth_or_quarantined"] != 1:
        fail("excluded_kxeth", counts["excluded_kxeth_or_quarantined"])
        return
    if counts["excluded_data_collection_override"] != 1 or counts["excluded_bootstrap_provisional"] != 1:
        fail("override_bootstrap_exclusion", counts)
        return
    if counts["excluded_open_rows"] != 1 or counts["excluded_legacy_or_unversioned"] != 0 or counts["excluded_unknown_other"] != 1:
        fail("open_legacy_exclusion", counts)
        return
    if rpt.entry_price({"entry_price": 0.0, "yes_ask": 0.87}) != 0.0:
        fail("entry_price_zero_preserved")
        return
    ok("population_and_exclusions")


def test_gate_status_logic() -> None:
    rejected = {
        "n": 20,
        "roi": -0.15,
        "profit_factor": 0.35,
        "win_rate": 0.55,
        "breakeven_wr": 0.85,
        "breakeven_gap": -0.30,
        "calibration_abs": 0.28,
        "total_expected_ev": 12.0,
        "total_economic_pnl": -18.0,
        "max_drawdown_flag": True,
        "total_economic_pnl": -18.0,
    }
    watchlist = {
        "n": 20,
        "roi": 0.05,
        "profit_factor": 1.08,
        "win_rate": 0.92,
        "breakeven_wr": 0.90,
        "breakeven_gap": 0.02,
        "calibration_abs": 0.04,
        "total_expected_ev": 4.0,
        "total_economic_pnl": 5.0,
        "max_drawdown_flag": False,
    }
    eligible = {
        "n": 60,
        "roi": 0.12,
        "profit_factor": 1.35,
        "win_rate": 0.75,
        "breakeven_wr": 0.65,
        "breakeven_gap": 0.10,
        "calibration_abs": 0.03,
        "total_expected_ev": 10.0,
        "total_economic_pnl": 12.0,
        "max_drawdown_flag": False,
    }
    if rpt.promotion_gate_status("builder_boost|0.80-0.90", rejected, 0, 4) != "REJECTED_POISON":
        fail("rejected_status")
        return
    if rpt.promotion_gate_status("critic_caution|0.90-1.00", watchlist, 1, 4) != "WATCHLIST_ONLY":
        fail("watchlist_status")
        return
    if rpt.promotion_gate_status("critic_caution|0.90-1.00", eligible, 3, 4) != "PROMOTION_ELIGIBLE_PAPER_ONLY":
        fail("eligible_status")
        return
    ok("gate_status_logic")


def test_cell_and_candidate_tables() -> None:
    rows = synthetic_rows()
    state = rpt.build_report_state(rows)
    cell_map = {r.spec.name: r for r in state["cell_results"]}
    if "builder_boost|0.80-0.90" not in cell_map:
        fail("builder_cell_missing")
        return
    if cell_map["builder_boost|0.80-0.90"].status != "REJECTED_POISON":
        fail("builder_cell_status", cell_map["builder_boost|0.80-0.90"].status)
        return
    if "critic_caution|0.90-1.00" not in cell_map:
        fail("critic_cell_missing")
        return
    if cell_map["critic_caution|0.90-1.00"].status not in {"WATCHLIST_ONLY", "PROMISING_BUT_UNPROVEN"}:
        fail("critic_cell_status", cell_map["critic_caution|0.90-1.00"].status)
        return
    if state["overall_status"] != "DO_NOT_PATCH_LIVE_YET":
        fail("overall_status", state["overall_status"])
        return
    if not state["small_positive_cells"]:
        fail("small_positive_cells_missing")
        return
    ok("cell_and_candidate_tables")


def test_report_read_only_and_sentinel() -> None:
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
        fail("sentinel_missing")
        return
    if "DO_NOT_PATCH_LIVE_YET" not in out:
        fail("verdict_missing")
        return
    ok("report_read_only_and_sentinel")


def main() -> None:
    print()
    print("=" * 90)
    print("PHASE 9W — FIXED-COHORT PROMOTION GATE TESTS")
    print("Sentinel: PROVEN_FIXED_COHORT_PROMOTION_GATE_OK")
    print("=" * 90)
    print()

    test_population_and_exclusions()
    test_gate_status_logic()
    test_cell_and_candidate_tables()
    test_report_read_only_and_sentinel()

    print()
    total = len(PASS) + len(FAIL)
    print(f"Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"FAILED: {', '.join(FAIL)}")
        print("Sentinel NOT reached.")
        sys.exit(1)
    print("Sentinel reached.")


if __name__ == "__main__":
    main()
