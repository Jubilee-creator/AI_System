#!/usr/bin/env python3
"""
Phase 9R — Fresh Economic Proof Autopsy Tests
Sentinel: PROVEN_FRESH_ECONOMIC_PROOF_AUTOPSY_OK
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_fresh_economic_proof_autopsy as rpt

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
    data_collection_override: bool = False,
    bootstrap_provisional: bool = False,
    side_coverage_test: bool = False,
    bootstrap_era_council_allow: bool = False,
    council_reason: str = "No Builder boost; Critic allowed with caution. Builder: No historically strong matching pattern found.",
) -> dict:
    yes_bid = round(max(0.01, entry_price - 0.01), 2)
    yes_ask = round(min(0.99, entry_price + 0.01), 2)
    no_bid = round(max(0.01, 1.0 - entry_price - 0.01), 2)
    no_ask = round(min(0.99, 1.0 - entry_price + 0.01), 2)
    model_probability = round(min(0.99, max(0.01, entry_price + risk_edge)), 3)
    return {
        "timestamp": f"2099-01-01T00:00:{len(PASS)+len(FAIL):02d}+00:00",
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
        "data_collection_override": data_collection_override,
        "bootstrap_provisional": bootstrap_provisional,
        "side_coverage_test": side_coverage_test,
        "bootstrap_era_council_allow": bootstrap_era_council_allow,
        "council_decision": "ALLOW",
        "council_reason": council_reason,
    }


def test_fresh_filter_and_zero_values() -> None:
    rows = [
        make_row(ticker="KXBTCD-GOOD", entry_price=0.85, risk_edge=0.06, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75),
        make_row(ticker="KXETH-BAD", entry_price=0.85, risk_edge=0.06, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25),
        make_row(ticker="KXBTCD-DC", entry_price=0.85, risk_edge=0.06, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, data_collection_override=True),
        make_row(ticker="KXBTCD-BOOT", entry_price=0.85, risk_edge=0.06, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, bootstrap_provisional=True),
        make_row(ticker="KXBTCD-SIDE", entry_price=0.85, risk_edge=0.06, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, side_coverage_test=True),
        make_row(ticker="KXBTCD-OPEN", status="OPEN", result="WIN", entry_price=0.85, risk_edge=0.06, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75),
        make_row(ticker="KXBTCD-LEG", accounting_version="legacy_hybrid_or_unversioned", entry_price=0.85, risk_edge=0.06, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75),
    ]
    fresh = rpt.fresh_proof_rows(rows)
    if len(fresh) != 1:
        fail("fresh_filter", str(len(fresh)))
        return
    summary = rpt.summarize_rows(fresh)
    if summary["total_economic_pnl"] != 0.75 or summary["total_recorded_pnl"] != 0.75 or summary["total_stored_pnl"] != 0.75:
        fail("fresh_summary_pnl", str(summary))
        return
    if summary["win_rate"] != 1.0 or summary["breakeven_wr"] != 0.85:
        fail("fresh_summary_rate", str(summary))
        return
    if rpt.stored_pnl_value({"pnl": 0.0, "realized_pnl": 9.0}) != 0.0:
        fail("stored_pnl_zero_preserved")
        return
    if rpt.entry_price({"entry_price": 0.0, "yes_ask": 0.85}) != 0.0:
        fail("entry_price_zero_preserved")
        return
    ok("fresh_filter_and_zero_values")


def test_bucket_and_counterfactual_logic() -> None:
    rows = []
    for i in range(5):
        rows.append(
            make_row(
                ticker=f"KXBTCD-POS-{i}",
                entry_price=0.65,
                risk_edge=0.04,
                economic_pnl=1.75,
                recorded_pnl=1.75,
                pnl=1.75,
            )
        )
    rows.extend(
        [
            make_row(ticker="KXBTCD-NEG-1", entry_price=0.85, risk_edge=0.06, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75),
            make_row(ticker="KXBTCD-NEG-2", entry_price=0.85, risk_edge=0.06, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75),
            make_row(ticker="KXBTCD-NEG-3", result="LOSS", entry_price=0.85, risk_edge=0.06, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25),
        ]
    )

    cells = rpt.summarize_cells(rows)
    if rpt.price_bucket(0.85) != "0.80-0.90" or rpt.edge_bucket(0.06) != "0.05-0.10":
        fail("bucket_math", "bucket mismatch")
        return
    if rpt.cell_key(rows[0]) != "0.03-0.05|0.60-0.70":
        fail("cell_key", rpt.cell_key(rows[0]))
        return

    positive_cells = [k for k, s in cells.items() if s["n"] >= 5 and s["total_economic_pnl"] > 0]
    if positive_cells != ["0.03-0.05|0.60-0.70"]:
        fail("positive_cells", str(positive_cells))
        return

    kept = rpt.select_rows_from_cells(rows, lambda s: s["n"] >= 5 and s["total_economic_pnl"] > 0)
    if len(kept) != 5:
        fail("select_positive_cells", str(len(kept)))
        return

    keep_margin = rpt.select_rows_from_cells(rows, lambda s: s["n"] >= 5 and s["wr_margin"] is not None and s["wr_margin"] >= 0.02)
    if len(keep_margin) != 5:
        fail("select_margin_cells", str(len(keep_margin)))
        return

    scenario = rpt.scenario_summary("block_0.80-0.90", rows, [r for r in rows if rpt.price_bucket(rpt.entry_price(r)) != "0.80-0.90"])
    if scenario["removed_rows"] != 3 or round(scenario["delta_pnl"], 2) != 2.75:
        fail("counterfactual_scenario", str(scenario))
        return

    ok("bucket_and_counterfactual_logic")


def test_report_runs_read_only_and_prints_sentinel() -> None:
    before = file_hash(TRADES_LOG)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rpt.main()
    after = file_hash(TRADES_LOG)
    out = buf.getvalue()
    if before != after:
        fail("report_read_only", "paper_trades hash changed")
        return
    if rpt.SENTINEL not in out:
        fail("report_sentinel", "sentinel missing")
        return
    if "Fresh proof rows:" not in out or "COUNTERFACTUAL FILTERS" not in out:
        fail("report_sections", "missing expected section text")
        return
    ok("report_runs_read_only_and_prints_sentinel")


def main() -> None:
    print()
    print("=" * 72)
    print("PHASE 9R — FRESH ECONOMIC PROOF AUTOPSY TESTS")
    print("Sentinel: PROVEN_FRESH_ECONOMIC_PROOF_AUTOPSY_OK")
    print("=" * 72)
    print()

    test_fresh_filter_and_zero_values()
    test_bucket_and_counterfactual_logic()
    test_report_runs_read_only_and_prints_sentinel()

    print()
    total = len(PASS) + len(FAIL)
    print(f"Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"FAILED: {', '.join(FAIL)}")
        print("Sentinel NOT reached.")
        sys.exit(1)
    print()
    print("Sentinel: PROVEN_FRESH_ECONOMIC_PROOF_AUTOPSY_OK")
    print()


if __name__ == "__main__":
    main()
