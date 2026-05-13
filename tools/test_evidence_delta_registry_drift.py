#!/usr/bin/env python3
"""
Phase 9Y — Evidence Delta + Registry Drift Monitor Tests
Sentinel: PROVEN_EVIDENCE_DELTA_REGISTRY_DRIFT_OK
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_evidence_delta_registry_drift as rpt

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
        "council_reason": council_reason,
        "bootstrap_era_council_allow": bootstrap_era_council_allow,
    }


def fixture_rows() -> list[dict]:
    rows = [
        make_row(
            timestamp="2099-01-01T00:00:00Z",
            ticker="BLD-1",
            entry_price=0.85,
            risk_edge=0.12,
            model_probability=0.88,
            result="LOSS",
            economic_pnl=-4.25,
            recorded_pnl=-4.25,
            pnl=-4.25,
            clv=-0.01,
            council_reason="Builder found positive historical pattern.",
        ),
        make_row(
            timestamp="2099-01-01T00:00:01Z",
            ticker="CRT-1",
            entry_price=0.90,
            risk_edge=0.04,
            model_probability=0.93,
            result="WIN",
            economic_pnl=0.40,
            recorded_pnl=0.40,
            pnl=0.40,
            clv=0.02,
        ),
        make_row(
            timestamp="2099-01-01T00:00:02Z",
            ticker="BOOT-1",
            entry_price=0.65,
            risk_edge=0.04,
            model_probability=0.72,
            result="WIN",
            economic_pnl=1.75,
            recorded_pnl=1.75,
            pnl=1.75,
            clv=0.03,
            bootstrap_era_council_allow=True,
            council_reason="Bootstrap allow path for early diagnostic testing.",
        ),
        make_row(
            timestamp="2099-01-01T00:00:03Z",
            ticker="ZERO-1",
            entry_price=0.0,
            risk_edge=0.03,
            model_probability=0.60,
            result="WIN",
            economic_pnl=0.10,
            recorded_pnl=0.10,
            pnl=0.10,
            clv=0.01,
        ),
        make_row(timestamp="2099-01-01T00:00:04Z", ticker="KXETH-BAD", entry_price=0.85, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01),
        make_row(timestamp="2099-01-01T00:00:05Z", ticker="DC-BAD", entry_price=0.85, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, data_collection_override=True),
        make_row(timestamp="2099-01-01T00:00:06Z", ticker="BOOT-BAD", entry_price=0.85, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, bootstrap_provisional=True),
        make_row(timestamp="2099-01-01T00:00:07Z", ticker="SIDE-BAD", entry_price=0.85, economic_pnl=-4.25, recorded_pnl=-4.25, pnl=-4.25, clv=-0.01, side_coverage_test=True),
        make_row(timestamp="2099-01-01T00:00:08Z", ticker="OPEN-BAD", status="OPEN", result="WIN", entry_price=0.85, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
        make_row(timestamp="2099-01-01T00:00:09Z", ticker="LEG-BAD", accounting_version="legacy_hybrid_or_unversioned", entry_price=0.85, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
        make_row(timestamp="2099-01-01T00:00:10Z", ticker="NO-PROB", entry_price=0.85, model_probability=None, economic_pnl=0.75, recorded_pnl=0.75, pnl=0.75, clv=0.01),
        make_row(timestamp="2099-01-01T00:00:11Z", ticker="NO-ENTRY", entry_price=None, model_probability=0.75, economic_pnl=0.50, recorded_pnl=0.50, pnl=0.50, clv=0.01),
    ]
    return rows


def build_baseline_snapshot(rows: list[dict]) -> tuple[dict, dict[str, str]]:
    state = rpt.registry.build_registry_state(rows)
    pocket_classes = {entry.name: entry.classification for entry in state["pocket_entries"]}
    return state["snapshot"], pocket_classes


def test_read_only_and_determinism() -> None:
    rows = fixture_rows()
    baseline_snapshot, baseline_classes = build_baseline_snapshot(rows)
    before = file_hash(TRADES_LOG)
    state = rpt.build_delta_state(rows, baseline_snapshot=baseline_snapshot, baseline_pocket_classes=baseline_classes)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rpt.render_report(state)
    after = file_hash(TRADES_LOG)
    out = buf.getvalue()
    if before != after:
        fail("report_read_only", "paper_trades hash changed")
        return
    if rpt.SENTINEL not in out:
        fail("sentinel_missing")
        return
    if state["hash_changed"]:
        fail("expected_same_hash", state["current_snapshot"]["cohort_hash"])
        return
    if state["evidence_status"] != "NO_NEW_CLEAN_EVIDENCE_SINCE_9X":
        fail("same_evidence_status", state["evidence_status"])
        return
    if state["delta"]["clean_row_delta"] != 0:
        fail("clean_row_delta", state["delta"]["clean_row_delta"])
        return
    ok("read_only_and_determinism")


def test_hash_change_and_drift_logic() -> None:
    rows = fixture_rows()
    baseline_snapshot, baseline_classes = build_baseline_snapshot(rows)
    mutated = [dict(row) for row in rows]
    mutated[0]["economic_pnl"] = -3.75
    mutated[0]["recorded_pnl"] = -3.75
    mutated[0]["pnl"] = -3.75
    state = rpt.build_delta_state(mutated, baseline_snapshot=baseline_snapshot, baseline_pocket_classes=baseline_classes)
    if not state["hash_changed"]:
        fail("hash_change_not_detected")
        return
    if state["evidence_status"] != "NEW_CLEAN_EVIDENCE_DETECTED":
        fail("new_evidence_status", state["evidence_status"])
        return
    if rpt.classify_drift("WATCHLIST_ONLY", "RESEARCH_QUARANTINE") != "WORSENED":
        fail("drift_worsened")
        return
    if rpt.classify_drift("RESEARCH_QUARANTINE", "WATCHLIST_ONLY") != "IMPROVED":
        fail("drift_improved")
        return
    if rpt.classify_drift("WATCHLIST_ONLY", "WATCHLIST_ONLY") != "UNCHANGED":
        fail("drift_unchanged")
        return
    if rpt.classify_drift(None, "WATCHLIST_ONLY") != "NEW":
        fail("drift_new")
        return
    if rpt.classify_drift("WATCHLIST_ONLY", None) != "REMOVED":
        fail("drift_removed")
        return
    ok("hash_change_and_drift_logic")


def test_registry_and_exclusions() -> None:
    rows = fixture_rows()
    baseline_snapshot, baseline_classes = build_baseline_snapshot(rows)
    state = rpt.build_delta_state(rows, baseline_snapshot=baseline_snapshot, baseline_pocket_classes=baseline_classes)
    current = state["current_snapshot"]
    if current["clean_row_count"] != 4:
        fail("clean_row_count", current["clean_row_count"])
        return
    if current["quarantine_count"] < 1 or current["watchlist_count"] < 1:
        fail("registry_counts", (current["quarantine_count"], current["watchlist_count"]))
        return
    row_map = {row["name"]: row for row in state["drift_rows"]}
    if row_map["builder_boost|0.80-0.90"]["current_class"] != "RESEARCH_QUARANTINE":
        fail("builder_class", row_map["builder_boost|0.80-0.90"]["current_class"])
        return
    if row_map["critic_caution|0.80-0.90"]["current_class"] not in {"WATCHLIST_ONLY", "PROMISING_BUT_UNPROVEN"}:
        fail("critic_class", row_map["critic_caution|0.80-0.90"]["current_class"])
        return
    if row_map["probability|0.90+"]["current_class"] != "WATCHLIST_ONLY":
        fail("probability_class", row_map["probability|0.90+"]["current_class"])
        return
    if row_map["entry|0.80-0.90"]["current_class"] != "RESEARCH_QUARANTINE":
        fail("entry_class", row_map["entry|0.80-0.90"]["current_class"])
        return
    if row_map["bootstrap_era_allow|0.60-0.70"]["current_class"] != "WATCHLIST_ONLY":
        fail("bootstrap_watchlist", row_map["bootstrap_era_allow|0.60-0.70"]["current_class"])
        return
    ok("registry_and_exclusions")


def test_report_text_and_permissions() -> None:
    rows = fixture_rows()
    baseline_snapshot, baseline_classes = build_baseline_snapshot(rows)
    state = rpt.build_delta_state(rows, baseline_snapshot=baseline_snapshot, baseline_pocket_classes=baseline_classes)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rpt.render_report(state)
    out = buf.getvalue()
    required = [
        "BASELINE SNAPSHOT",
        "CURRENT SNAPSHOT",
        "DELTA SUMMARY",
        "REGISTRY DRIFT TABLE",
        "TERMINAL REFUSAL RULES",
        "NO_NEW_CLEAN_EVIDENCE_SINCE_9X",
        "DO_NOT_PATCH_LIVE_YET",
        "live_patch_permission",
    ]
    for token in required:
        if token not in out:
            fail("report_token_missing", token)
            return
    ok("report_text_and_permissions")


def main() -> None:
    print()
    print("=" * 90)
    print("PHASE 9Y — EVIDENCE DELTA + REGISTRY DRIFT MONITOR TESTS")
    print("Sentinel: PROVEN_EVIDENCE_DELTA_REGISTRY_DRIFT_OK")
    print("=" * 90)
    print()

    test_read_only_and_determinism()
    test_hash_change_and_drift_logic()
    test_registry_and_exclusions()
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
