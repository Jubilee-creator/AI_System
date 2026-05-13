#!/usr/bin/env python3
"""
Phase 10A — Proof Operations Hardening Tests
Sentinel: PROVEN_PROOF_OPERATIONS_HARDENING_OK
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_proof_operations_hardening as rpt
from tools.report_accounting_version_proof_cohorts import classify_accounting_version, is_clean_proof_row, load_trades

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
PASS: list[str] = []
FAIL: list[str] = []


def template_row() -> dict:
    for rec in load_trades():
        if classify_accounting_version(rec) == "economic_contract_notional_v1" and is_clean_proof_row(rec):
            return copy.deepcopy(rec)
    raise RuntimeError("No clean economic template row found in live logs")


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
    economic_pnl: float = 0.75,
    recorded_pnl: float = 0.75,
    pnl: float = 0.75,
    data_collection_override: bool = False,
    bootstrap_provisional: bool = False,
    side_coverage: bool = False,
) -> dict:
    return {
        "timestamp": timestamp,
        "ticker": ticker,
        "status": status,
        "result": result,
        "accounting_version": accounting_version,
        "entry_price": entry_price,
        "yes_ask": None if entry_price is None else round(min(0.99, entry_price + 0.01), 2),
        "model_probability": model_probability,
        "risk_edge": risk_edge,
        "size": 5.0,
        "economic_pnl": economic_pnl,
        "recorded_pnl": recorded_pnl,
        "pnl": pnl,
        "data_collection_override": data_collection_override,
        "bootstrap_provisional": bootstrap_provisional,
        "side_coverage": side_coverage,
    }


def baseline_rows() -> list[dict]:
    base = template_row()
    one = copy.deepcopy(base)
    one.update(
        {
            "timestamp": "2026-05-13T06:12:58.607614+00:00",
            "ticker": "BASE-1",
            "result": "WIN",
            "status": "SETTLED",
            "settled_at": "2026-05-13T06:13:58.607614+00:00",
            "entry_price": 0.82,
            "yes_ask": 0.82,
            "yes_bid": 0.80,
            "price_yes": 0.82,
            "price_no": 0.18,
            "yes_price": 0.82,
            "no_price": 0.18,
            "model_probability": 0.87,
            "confidence": 0.87,
            "risk_edge": 0.05,
            "edge": 0.05,
            "original_edge": 0.05,
            "adjusted_edge": 0.05,
            "economic_pnl": 0.90,
            "recorded_pnl": 0.90,
            "pnl": 0.90,
            "clv": 0.01,
            "capital_at_risk": 4.10,
            "payout_notional": 5.0,
            "max_profit_if_win": 0.90,
            "max_loss_if_loss": 4.10,
        }
    )
    two = copy.deepcopy(one)
    two.update(
        {
            "ticker": "BASE-2",
            "result": "LOSS",
            "entry_price": 0.83,
            "yes_ask": 0.83,
            "yes_bid": 0.81,
            "price_yes": 0.83,
            "price_no": 0.17,
            "yes_price": 0.83,
            "no_price": 0.17,
            "model_probability": 0.88,
            "confidence": 0.88,
            "risk_edge": 0.06,
            "edge": 0.06,
            "original_edge": 0.06,
            "adjusted_edge": 0.06,
            "economic_pnl": -4.15,
            "recorded_pnl": -4.15,
            "pnl": -4.15,
            "clv": -0.02,
            "capital_at_risk": 4.15,
            "max_profit_if_win": 0.85,
            "max_loss_if_loss": 4.15,
        }
    )
    return [one, two]


def contaminated_rows() -> list[dict]:
    base = template_row()
    out: list[dict] = []
    for ticker, extra in [
        ("KXETH-BAD", {"ticker": "KXETH-BAD"}),
        ("DC-BAD", {"ticker": "DC-BAD", "data_collection_override": True}),
        ("BOOT-BAD", {"ticker": "BOOT-BAD", "bootstrap_provisional": True}),
        ("SIDE-BAD", {"ticker": "SIDE-BAD", "side_coverage": True}),
        ("NO-PROB", {"ticker": "NO-PROB", "model_probability": None}),
        ("NO-ENTRY", {"ticker": "NO-ENTRY", "entry_price": None, "yes_ask": None, "yes_bid": None, "price_yes": None, "price_no": None, "yes_price": None, "no_price": None}),
        ("NO-ECON", {"ticker": "NO-ECON", "economic_pnl": None, "recorded_pnl": None, "pnl": None}),
        ("NO-RESULT", {"ticker": "NO-RESULT", "result": "PENDING"}),
    ]:
        row = copy.deepcopy(base)
        row.update(
            {
                "timestamp": "2026-05-13T06:13:58.607614+00:00",
                "settled_at": "2026-05-13T06:14:58.607614+00:00",
                "status": "PENDING" if ticker == "NO-RESULT" else "SETTLED",
                "economic_pnl": -4.15 if ticker != "NO-ECON" else None,
                "recorded_pnl": -4.15 if ticker != "NO-ECON" else None,
                "pnl": -4.15 if ticker != "NO-ECON" else None,
                "result": "PENDING" if ticker == "NO-RESULT" else "LOSS",
            }
        )
        row.update(extra)
        out.append(row)
    return out


def new_clean_rows(n: int) -> list[dict]:
    base = template_row()
    rows: list[dict] = []
    for i in range(n):
        minute = 13 + (i // 60)
        second = i % 60
        loss = i % 4 == 0
        ep = 0.84 if i % 2 == 0 else 0.68
        prob = 0.91 if i % 3 == 0 else 0.73
        pnl = -4.10 if loss else 0.80
        clv = -0.02 if loss else 0.02
        row = copy.deepcopy(base)
        row.update(
            {
                "timestamp": f"2026-05-13T06:{minute:02d}:{second:02d}+00:00",
                "settled_at": f"2026-05-13T06:{minute:02d}:{(second + 1) % 60:02d}+00:00",
                "ticker": f"NEW-{i}",
                "entry_price": ep,
                "yes_ask": ep,
                "yes_bid": round(max(0.0, ep - 0.02), 2),
                "price_yes": ep,
                "price_no": round(1.0 - ep, 2),
                "yes_price": ep,
                "no_price": round(1.0 - ep, 2),
                "model_probability": prob,
                "confidence": prob,
                "risk_edge": 0.09 if i % 3 == 0 else 0.04,
                "edge": 0.09 if i % 3 == 0 else 0.04,
                "original_edge": 0.09 if i % 3 == 0 else 0.04,
                "adjusted_edge": 0.09 if i % 3 == 0 else 0.04,
                "result": "LOSS" if loss else "WIN",
                "economic_pnl": pnl,
                "recorded_pnl": pnl,
                "pnl": pnl,
                "clv": clv,
                "capital_at_risk": round(ep * 5.0, 2),
                "payout_notional": 5.0,
                "max_profit_if_win": round((1.0 - ep) * 5.0, 2),
                "max_loss_if_loss": round(ep * 5.0, 2),
            }
        )
        rows.append(
            row
        )
    return rows


def baseline_snapshot(rows: list[dict]) -> dict[str, object]:
    return rpt.delta.build_delta_state(rows, baseline_snapshot=rpt.delta.BASELINE_SNAPSHOT, baseline_pocket_classes=rpt.delta.BASELINE_POCKET_CLASSES)["current_snapshot"]


def build_state_for(rows: list[dict], base: list[dict], *, funnel_rows: list[dict] | None = None, dashboard_state: dict | None = None) -> dict:
    return rpt.build_hardening_state(
        rows,
        baseline_snapshot=baseline_snapshot(base),
        funnel_rows=funnel_rows,
        dashboard_state=dashboard_state,
    )


def healthy_dashboard_state() -> dict:
    return {
        "dashboard_running": True,
        "auto_settle_running": True,
        "heartbeat_age_seconds": 45.0,
    }


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
    if "PROOF OPS STALE" not in out and "PROOF_OPS_STALE" not in out:
        fail("stale_status_missing")
        return
    ok("read_only_and_current_live_report")


def test_evidence_and_status_paths() -> None:
    base = baseline_rows()
    state = build_state_for(base, base, dashboard_state=healthy_dashboard_state(), funnel_rows=[])
    if state["new_clean_rows"] != 0:
        fail("baseline_new_clean_rows", state["new_clean_rows"])
        return
    if state["proof_ops_status"] != "PROOF_OPS_STALE":
        fail("baseline_status", state["proof_ops_status"])
        return

    healthy = build_state_for(base + new_clean_rows(5), base, dashboard_state=healthy_dashboard_state(), funnel_rows=[
        {"timestamp_utc": "2026-05-13T06:14:00+00:00", "paper_trade_opened": True, "passed_to_paper_trader": True, "final_reason": "TRADE_OPENED", "dashboard_seen": True},
    ])
    if healthy["new_clean_rows"] != 5:
        fail("new_clean_rows", healthy["new_clean_rows"])
        return
    if healthy["proof_ops_status"] != "PROOF_OPS_HEALTHY":
        fail("healthy_status", healthy["proof_ops_status"])
        return
    if healthy["overall_status"] != "PROOF_OPS_HEALTHY":
        fail("healthy_overall", healthy["overall_status"])
        return
    if healthy["candidate_pipeline_status"] != "ACTIVE":
        fail("candidate_pipeline_active", healthy["candidate_pipeline_status"])
        return
    if healthy["intake_state"]["current_snapshot"]["cohort_hash"] == baseline_snapshot(base)["cohort_hash"]:
        fail("hash_should_change", healthy["intake_state"]["current_snapshot"]["cohort_hash"])
        return
    ok("evidence_and_status_paths")


def test_backlog_field_and_contamination_detection() -> None:
    base = baseline_rows()
    rows = base + contaminated_rows() + [
        make_row(timestamp="2026-05-13T06:13:58.607614+00:00", ticker="OPEN-BAD", status="OPEN", economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75),
    ]
    state = build_state_for(rows, base, dashboard_state={"dashboard_running": False, "auto_settle_running": False}, funnel_rows=[])
    if state["intake_state"]["active_open_count"] < 1:
        fail("active_open_missing", state["intake_state"]["active_open_count"])
        return
    if state["throughput_status"] != "SETTLEMENT_BACKLOG":
        fail("backlog_status", state["throughput_status"])
        return
    if state["overall_status"] != "SETTLEMENT_BACKLOG":
        fail("backlog_overall", state["overall_status"])
        return
    exclusions = state["post_baseline_exclusions"]
    if exclusions["kxeth"] < 1 or exclusions["data_collection_override"] < 1 or exclusions["bootstrap_provisional"] < 1 or exclusions["side_coverage"] < 1:
        fail("contamination_counts", exclusions)
        return
    if state["intake_state"]["missing_model_probability_rows"] < 1:
        fail("missing_model_probability", state["intake_state"]["missing_model_probability_rows"])
        return
    if state["intake_state"]["missing_entry_price_rows"] < 1:
        fail("missing_entry_price", state["intake_state"]["missing_entry_price_rows"])
        return
    if state["intake_state"]["missing_economic_pnl_rows"] < 1:
        fail("missing_economic_pnl", state["intake_state"]["missing_economic_pnl_rows"])
        return
    if state["intake_state"]["missing_outcome_rows"] < 1:
        fail("missing_outcome", state["intake_state"]["missing_outcome_rows"])
        return
    ok("backlog_field_and_contamination_detection")


def test_report_text_and_dashboard_truth() -> None:
    base = baseline_rows()
    state = build_state_for(base, base, dashboard_state={"dashboard_running": False, "auto_settle_running": False}, funnel_rows=[])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rpt.render_report(state)
    out = buf.getvalue()
    required = [
        "PROOF OPERATIONS HARDENING",
        "EVIDENCE PRODUCTION",
        "SETTLEMENT THROUGHPUT",
        "FIELD COMPLETENESS",
        "CONTAMINATION",
        "CANDIDATE / EXECUTION FUNNEL",
        "DASHBOARD TRUTH",
        "OPERATIONAL REFUSAL RULES",
        "PROOF_OPS_STALE",
        "CANDIDATE_PIPELINE_STALE",
        "dashboard_truth_risk",
        "Sentinel: PROOF_OPERATIONS_HARDENING_REPORT_OK",
    ]
    for token in required:
        if token not in out:
            fail("report_token_missing", token)
            return
    ok("report_text_and_dashboard_truth")


def main() -> None:
    print()
    print("=" * 90)
    print("PHASE 10A — PROOF OPERATIONS HARDENING TESTS")
    print("Sentinel: PROVEN_PROOF_OPERATIONS_HARDENING_OK")
    print("=" * 90)
    print()

    test_read_only_and_current_live_report()
    test_evidence_and_status_paths()
    test_backlog_field_and_contamination_detection()
    test_report_text_and_dashboard_truth()

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
