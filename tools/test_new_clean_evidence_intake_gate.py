#!/usr/bin/env python3
"""
Phase 9Z — New Clean Evidence Intake Gate Tests
Sentinel: PROVEN_NEW_CLEAN_EVIDENCE_INTAKE_GATE_OK
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_new_clean_evidence_intake_gate as rpt

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
    entry_price: float | None = 0.85,
    risk_edge: float = 0.06,
    model_probability: float | None = 0.88,
    economic_pnl: float = 0.0,
    recorded_pnl: float = 0.0,
    pnl: float = 0.0,
    clv: float | None = 0.0,
    data_collection_override: bool = False,
    bootstrap_provisional: bool = False,
    side_coverage_test: bool = False,
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
        "yes_ask": None if entry_price is None else round(min(0.99, entry_price + 0.01), 2),
        "yes_bid": None if entry_price is None else round(max(0.0, entry_price - 0.01), 2),
        "model_probability": model_probability,
        "risk_edge": risk_edge,
        "edge": risk_edge,
        "size": 5.0,
        "capital_at_risk": None if entry_price is None else round(entry_price * 5.0, 2),
        "payout_notional": 5.0,
        "max_profit_if_win": None if entry_price is None else round((1.0 - entry_price) * 5.0, 2),
        "max_loss_if_loss": None if entry_price is None else round(entry_price * 5.0, 2),
        "economic_pnl": economic_pnl,
        "recorded_pnl": recorded_pnl,
        "pnl": pnl,
        "clv": clv,
        "data_collection_override": data_collection_override,
        "bootstrap_provisional": bootstrap_provisional,
        "side_coverage_test": side_coverage_test,
        "bootstrap_era_council_allow": bootstrap_era_council_allow,
    }


def baseline_rows() -> list[dict]:
    return [
        make_row(timestamp="2099-01-01T00:00:00Z", ticker="BASE-1", entry_price=0.00, model_probability=0.60, risk_edge=0.03, economic_pnl=0.10, recorded_pnl=0.10, pnl=0.10, clv=0.01),
        make_row(timestamp="2099-01-01T00:00:01Z", ticker="BASE-2", entry_price=0.85, model_probability=0.88, risk_edge=0.06, result="LOSS", economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01),
        make_row(timestamp="2099-01-01T00:00:02Z", ticker="BASE-3", entry_price=0.65, model_probability=0.72, risk_edge=0.04, result="WIN", economic_pnl=1.75, recorded_pnl=1.75, pnl=1.75, clv=0.03),
        make_row(timestamp="2099-01-01T00:00:03Z", ticker="BASE-4", entry_price=0.90, model_probability=0.93, risk_edge=0.04, result="WIN", economic_pnl=0.40, recorded_pnl=0.40, pnl=0.40, clv=0.02),
    ]


def contaminated_rows() -> list[dict]:
    return [
        make_row(timestamp="2099-01-01T00:00:10Z", ticker="KXETH-BAD", entry_price=0.85, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01),
        make_row(timestamp="2099-01-01T00:00:11Z", ticker="DC-BAD", entry_price=0.85, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, data_collection_override=True),
        make_row(timestamp="2099-01-01T00:00:12Z", ticker="BOOT-BAD", entry_price=0.85, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, bootstrap_provisional=True),
        make_row(timestamp="2099-01-01T00:00:13Z", ticker="SIDE-BAD", entry_price=0.85, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, side_coverage_test=True),
        make_row(timestamp="2099-01-01T00:00:14Z", ticker="OPEN-BAD", status="OPEN", result="WIN", entry_price=0.85, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
        make_row(timestamp="2099-01-01T00:00:15Z", ticker="LEG-BAD", accounting_version="legacy_hybrid_or_unversioned", entry_price=0.85, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
        make_row(timestamp="2099-01-01T00:00:16Z", ticker="NO-PROB", entry_price=0.85, model_probability=None, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
        make_row(timestamp="2099-01-01T00:00:17Z", ticker="NO-ENTRY", entry_price=None, model_probability=0.75, economic_pnl=0.50, recorded_pnl=0.50, pnl=0.50, clv=0.01),
    ]


def new_clean_rows(n: int, *, start: int = 0) -> list[dict]:
    rows: list[dict] = []
    for i in range(n):
        idx = start + i
        minute = 1 + (idx // 60)
        second = idx % 60
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:{minute:02d}:{second:02d}Z",
                ticker=f"NEW-{idx}",
                entry_price=0.85 if i % 2 == 0 else 0.65,
                risk_edge=0.12 if i % 3 == 0 else 0.04,
                model_probability=0.92 if i % 3 == 0 else 0.72,
                result="WIN" if i % 4 != 0 else "LOSS",
                economic_pnl=0.75 if i % 4 != 0 else -4.25,
                recorded_pnl=0.75 if i % 4 != 0 else -4.25,
                pnl=0.75 if i % 4 != 0 else -4.25,
                clv=0.02 if i % 4 != 0 else -0.02,
                bootstrap_era_council_allow=(i % 7 == 0),
            )
        )
    return rows


def baseline_snapshot(rows: list[dict]) -> dict[str, object]:
    return rpt.delta.registry.build_registry_state(rows)["snapshot"]


def build_state_for(rows: list[dict], base: list[dict]) -> dict:
    return rpt.build_intake_state(
        rows,
        baseline_snapshot=baseline_snapshot(base),
        baseline_commit="synthetic-baseline",
    )


def test_read_only_and_current_live_report() -> None:
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
        fail("sentinel_missing")
        return
    if "STALE DATA REFUSAL RULES" not in out:
        fail("refusal_rules_missing")
        return
    ok("read_only_and_current_live_report")


def test_status_ladder_and_hash_logic() -> None:
    base = baseline_rows()
    baseline_snap = baseline_snapshot(base)
    cases = [
        (0, "NO_NEW_CLEAN_EVIDENCE"),
        (10, "INSUFFICIENT_NEW_EVIDENCE"),
        (20, "MINIMUM_RECHECK_READY"),
        (50, "PROMOTION_RECHECK_READY"),
        (100, "STRONG_RECHECK_READY"),
    ]
    for n, expected in cases:
        rows = base + contaminated_rows() + new_clean_rows(n)
        state = build_state_for(rows, base)
        if state["intake_status"] != expected:
            fail(f"intake_status_{n}", state["intake_status"])
            return
        if n == 0 and state["current_snapshot"]["cohort_hash"] != baseline_snap["cohort_hash"]:
            fail("hash_unchanged", state["current_snapshot"]["cohort_hash"])
            return
        if n > 0 and state["current_snapshot"]["cohort_hash"] == baseline_snap["cohort_hash"]:
            fail(f"hash_changed_{n}", state["current_snapshot"]["cohort_hash"])
            return
    ok("status_ladder_and_hash_logic")


def test_exclusions_and_zero_entry() -> None:
    base = baseline_rows()
    rows = base + contaminated_rows()
    state = build_state_for(rows, base)
    if state["clean_rows"][0].get("entry_price") != 0.0:
        fail("zero_entry_preserved", state["clean_rows"][0].get("entry_price"))
        return
    if state["raw_total"] != len(rows):
        fail("raw_total", state["raw_total"])
        return
    if state["kxeth_rows"] < 1 or state["data_collection_override_rows"] < 1 or state["bootstrap_provisional_rows"] < 1:
        fail("contamination_counts", (state["kxeth_rows"], state["data_collection_override_rows"], state["bootstrap_provisional_rows"]))
        return
    if state["open_rows"] < 1 or state["missing_model_probability_rows"] < 1 or state["missing_entry_price_rows"] < 1:
        fail("missing_counts", (state["open_rows"], state["missing_model_probability_rows"], state["missing_entry_price_rows"]))
        return
    ok("exclusions_and_zero_entry")


def test_report_text_and_permissions() -> None:
    base = baseline_rows()
    rows = base + contaminated_rows() + new_clean_rows(0)
    state = build_state_for(rows, base)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rpt.render_report(state)
    out = buf.getvalue()
    required = [
        "EVIDENCE INTAKE SUMMARY",
        "SETTLEMENT THROUGHPUT",
        "NEW EVIDENCE QUALITY",
        "STALE DATA REFUSAL RULES",
        "NO_NEW_CLEAN_EVIDENCE",
        "live_patch_permission",
        "raw rows newer than baseline",
        "recommendation: do not start another research phase",
    ]
    for token in required:
        if token not in out:
            fail("report_token_missing", token)
            return
    ok("report_text_and_permissions")


def main() -> None:
    print()
    print("=" * 90)
    print("PHASE 9Z — NEW CLEAN EVIDENCE INTAKE GATE TESTS")
    print("Sentinel: PROVEN_NEW_CLEAN_EVIDENCE_INTAKE_GATE_OK")
    print("=" * 90)
    print()

    test_read_only_and_current_live_report()
    test_status_ladder_and_hash_logic()
    test_exclusions_and_zero_entry()
    test_report_text_and_permissions()

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
