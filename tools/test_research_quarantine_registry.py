#!/usr/bin/env python3
"""
Phase 9X — Research Quarantine Registry Tests
Sentinel: PROVEN_RESEARCH_QUARANTINE_REGISTRY_OK
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_research_quarantine_registry as rpt

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
        "yes_ask": round(min(0.99, entry_price + 0.01), 2),
        "yes_bid": round(max(0.0, entry_price - 0.01), 2),
        "model_probability": model_probability,
        "risk_edge": risk_edge,
        "edge": risk_edge,
        "size": 5.0,
        "capital_at_risk": round(entry_price * 5.0, 2),
        "payout_notional": 5.0,
        "max_profit_if_win": round((1.0 - entry_price) * 5.0, 2),
        "max_loss_if_loss": round(entry_price * 5.0, 2),
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


def synthetic_rows() -> list[dict]:
    rows = []
    # Canonical clean cohort with one clearly quarantined pocket and one watchlist pocket.
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
    for i in range(20, 40):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:01:{i-20:02d}Z",
                ticker=f"CRT-{i}",
                entry_price=0.90,
                risk_edge=0.04,
                model_probability=0.93,
                result="WIN" if i < 39 else "LOSS",
                economic_pnl=0.40 if i < 39 else -0.20,
                recorded_pnl=0.40 if i < 39 else -0.20,
                pnl=0.40 if i < 39 else -0.20,
                clv=0.02 if i < 39 else -0.02,
                council_reason="No Builder boost; Critic allowed with caution. Builder: No historically strong matching pattern found.",
            )
        )
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
    for i in range(60, 64):
        rows.append(
            make_row(
                timestamp=f"2099-01-01T00:02:{i-40:02d}Z",
                ticker=f"BOOT-{i}",
                entry_price=0.65,
                risk_edge=0.04,
                model_probability=0.72,
                result="WIN",
                economic_pnl=1.75,
                recorded_pnl=1.75,
                pnl=1.75,
                clv=0.03,
                council_reason="Bootstrap allow path for early diagnostic testing.",
                bootstrap_era_council_allow=True,
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
        ]
    )
    return rows


def test_evidence_hash_and_population() -> None:
    rows = synthetic_rows()
    state = rpt.build_registry_state(rows)
    snap = state["snapshot"]
    if snap["clean_row_count"] != 64:
        fail("clean_row_count", snap["clean_row_count"])
        return
    if len(snap["cohort_hash"]) != 64:
        fail("cohort_hash_length", snap["cohort_hash"])
        return
    again = rpt.build_registry_state(rows)["snapshot"]["cohort_hash"]
    if again != snap["cohort_hash"]:
        fail("cohort_hash_determinism", (snap["cohort_hash"], again))
        return
    ok("evidence_hash_and_population")


def test_registry_classifications() -> None:
    rows = synthetic_rows()
    state = rpt.build_registry_state(rows)
    by_name = {entry.name: entry for entry in state["pocket_entries"]}
    if by_name["builder_boost|0.80-0.90"].classification != "RESEARCH_QUARANTINE":
        fail("builder_quarantine", by_name["builder_boost|0.80-0.90"].classification)
        return
    if by_name["entry|0.80-0.90"].classification != "RESEARCH_QUARANTINE":
        fail("entry_quarantine", by_name["entry|0.80-0.90"].classification)
        return
    if by_name["edge|0.10+"].classification != "RESEARCH_QUARANTINE":
        fail("edge_quarantine", by_name["edge|0.10+"].classification)
        return
    if by_name["critic_caution|0.80-0.90"].classification not in {"WATCHLIST_ONLY", "PROMISING_BUT_UNPROVEN"}:
        fail("critic_watchlist", by_name["critic_caution|0.80-0.90"].classification)
        return
    if by_name["probability|0.90+"].classification != "WATCHLIST_ONLY":
        fail("probability_watchlist", by_name["probability|0.90+"].classification)
        return
    if by_name["bootstrap_era_allow|0.60-0.70"].classification != "WATCHLIST_ONLY":
        fail("bootstrap_watchlist", by_name["bootstrap_era_allow|0.60-0.70"].classification)
        return
    ok("registry_classifications")


def test_report_read_only_and_terminal_rules() -> None:
    rows = synthetic_rows()
    before = file_hash(TRADES_LOG)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        state = rpt.build_registry_state(rows)
        rpt.render_report(state)
    after = file_hash(TRADES_LOG)
    out = buf.getvalue()
    if before != after:
        fail("report_read_only", "paper_trades hash changed")
        return
    if rpt.SENTINEL not in out:
        fail("sentinel_missing")
        return
    if "SINCE PRIOR PROOF REPORTS" not in out:
        fail("since_prior_proof_missing")
        return
    if "TERMINAL REFUSAL RULES" not in out:
        fail("refusal_rules_missing")
        return
    if "RESEARCH_QUARANTINE" not in out or "WATCHLIST_ONLY" not in out:
        fail("classification_labels_missing")
        return
    ok("report_read_only_and_terminal_rules")


def test_upgrade_and_retirement_requirements() -> None:
    rows = synthetic_rows()
    state = rpt.build_registry_state(rows)
    pocket = {entry.name: entry for entry in state["pocket_entries"]}
    builder = pocket["builder_boost|0.80-0.90"]
    if "multi-window validation clears" not in builder.upgrade_requirement:
        fail("upgrade_requirement_missing")
        return
    if "Retire immediately" not in builder.retirement_requirement:
        fail("retirement_requirement_missing")
        return
    ok("upgrade_and_retirement_requirements")


def main() -> None:
    print()
    print("=" * 86)
    print("PHASE 9X — RESEARCH QUARANTINE REGISTRY TESTS")
    print("Sentinel: PROVEN_RESEARCH_QUARANTINE_REGISTRY_OK")
    print("=" * 86)
    print()

    test_evidence_hash_and_population()
    test_registry_classifications()
    test_upgrade_and_retirement_requirements()
    test_report_read_only_and_terminal_rules()

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
