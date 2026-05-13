#!/usr/bin/env python3
"""
Phase 9X — Research Quarantine Registry + Evidence Snapshot Ledger
Sentinel: RESEARCH_QUARANTINE_REGISTRY_REPORT_OK

Read-only registry that classifies important pockets into quarantine,
poison, watchlist, promotion-blocked, and paper-only promotion categories.
It also emits a deterministic evidence snapshot hash for the clean proof
cohort so the same evidence can be referenced later without ambiguity.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_fixed_cohort_promotion_gate as gate
from tools.report_accounting_version_proof_cohorts import (
    economic_pnl_value,
    entry_price,
    is_kxeth_or_quarantined,
    load_trades,
)
from tools.report_fresh_economic_proof_autopsy import council_path, edge_bucket, price_bucket
from tools.report_probability_calibration_payoff_truth import calibration_rows, model_probability_value, clean_proof_rows

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "RESEARCH_QUARANTINE_REGISTRY_REPORT_OK"
MIN_PROMOTION_ROWS = gate.MIN_PROMOTION_ROWS
MIN_POCKET_SAMPLE = 5
MAX_CALIBRATION_GAP = gate.MAX_CALIBRATION_GAP

QUARANTINE_NAMES = {
    "builder_boost|0.80-0.90",
    "entry|0.80-0.90",
    "edge|0.10+",
    "builder_boost|0.80-0.90",
    "cell|0.05-0.10|0.80-0.90",
    "cell|0.10+|0.80-0.90",
}


@dataclass(frozen=True)
class RegistryEntry:
    name: str
    classification: str
    summary: dict[str, Any]
    reasons: list[str]
    upgrade_requirement: str
    retirement_requirement: str


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _sorted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda r: (str(r.get("timestamp") or ""), str(r.get("ticker") or "")))


def _clean_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _sorted_rows(calibration_rows(records))


def _cohort_hash(rows: list[dict[str, Any]]) -> str:
    payload = []
    for rec in rows:
        payload.append(
            {
                "timestamp": rec.get("timestamp"),
                "ticker": rec.get("ticker"),
                "accounting_version": rec.get("accounting_version"),
                "result": rec.get("result"),
                "status": rec.get("status"),
                "entry_price": entry_price(rec),
                "model_probability": model_probability_value(rec),
                "economic_pnl": economic_pnl_value(rec),
                "council_path": council_path(rec),
                "risk_edge": _as_float(rec.get("risk_edge")),
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = gate._summary(rows)
    high_entry_rows = [r for r in rows if price_bucket(entry_price(r)) == "0.80-0.90"]
    builder_rows = [r for r in rows if council_path(r) == "builder_boost"]
    return {
        "clean_row_count": len(rows),
        "first_timestamp": rows[0].get("timestamp") if rows else None,
        "last_timestamp": rows[-1].get("timestamp") if rows else None,
        "cohort_hash": _cohort_hash(rows),
        "total_economic_pnl": summary["total_economic_pnl"],
        "roi": summary["roi"],
        "profit_factor": summary["profit_factor"],
        "avg_model_probability": summary["avg_model_probability"],
        "avg_entry_price": summary["avg_entry_price"],
        "calibration_gap": summary["calibration_gap"],
        "high_entry_pnl": sum((_as_float(economic_pnl_value(r)) or 0.0) for r in high_entry_rows),
        "builder_boost_pnl": sum((_as_float(economic_pnl_value(r)) or 0.0) for r in builder_rows),
        "watchlist_count": 0,
        "quarantine_count": 0,
        "rejected_count": 0,
        "promotion_blocked_count": 0,
        "promotion_eligible_count": 0,
    }


def _classify_entry(result: gate.PocketResult) -> RegistryEntry:
    summary = result.summary
    name = result.spec.name
    reasons = list(result.reasons)
    classification = "DO_NOT_PATCH_LIVE_YET"
    upgrade = "Require larger rolling windows, ROI > 0, PF > 1.20, WR above breakeven by 3pp, and calibration gap <= 10pp."
    retire = "Retire if later clean cohorts keep ROI < 0, PF < 1, WR below breakeven, or realized PnL remains negative."

    poison = (
        summary.get("roi") is not None
        and summary["roi"] < 0
        and summary.get("profit_factor") is not None
        and summary["profit_factor"] < 1.0
        and summary.get("win_rate") is not None
        and summary.get("breakeven_wr") is not None
        and summary["win_rate"] < summary["breakeven_wr"]
    )
    quarantine_trigger = (
        name in QUARANTINE_NAMES
        or "HIGH_ENTRY_POISON" in reasons
        or "BUILDER_HIGH_ENTRY_OVERLAP" in reasons
        or "MODEL_EV_FAKE" in reasons
    )
    if quarantine_trigger:
        classification = "RESEARCH_QUARANTINE"
    elif poison:
        classification = "REJECTED_POISON"
    elif result.status == "PROMOTION_ELIGIBLE_PAPER_ONLY":
        classification = "PROMOTION_ELIGIBLE_PAPER_ONLY"
    elif result.status == "PROMISING_BUT_UNPROVEN":
        classification = "PROMISING_BUT_UNPROVEN"
    elif result.status == "WATCHLIST_ONLY":
        classification = "WATCHLIST_ONLY"
    elif result.status == "TINY_SAMPLE":
        classification = "WATCHLIST_ONLY"
    elif result.status == "REJECTED_POISON":
        classification = "REJECTED_POISON"
    elif result.status == "PROMOTION_BLOCKED":
        classification = "PROMOTION_BLOCKED"
    if classification == "RESEARCH_QUARANTINE":
        upgrade = "Only upgrade if multi-window validation clears high-entry poison, calibration damage, and payoff asymmetry."
        retire = "Retire immediately if the same pocket remains negative on rolling out-of-sample windows."
    elif classification == "WATCHLIST_ONLY":
        upgrade = "Require sample floor >= 50, stable rolling ROI/PF, and calibration gap <= 10pp."
        retire = "Retire if added sample keeps the pocket below breakeven or negative ROI."
    elif classification == "PROMISING_BUT_UNPROVEN":
        upgrade = "Require at least 3 passing rolling windows and a clean out-of-sample cohort."
        retire = "Retire if validation windows fail ROI/PF or the edge disappears in newer rows."
    elif classification == "PROMOTION_BLOCKED":
        upgrade = "Require sample floor >= 50 plus rolling ROI/PF/win-rate-margin standards."
        retire = "Keep blocked if the pocket stays below promotion floors, even if it looks cosmetically attractive."
    elif classification == "PROMOTION_ELIGIBLE_PAPER_ONLY":
        upgrade = "Eligible for a future dedicated promotion phase only; still no live patch in Phase 9X."
        retire = "Retire if any future cohort violates promotion standards or calibration drifts."
    elif classification == "REJECTED_POISON":
        upgrade = "No upgrade until the pocket clears breakeven, ROI > 0, and PF > 1 on rolling windows."
        retire = "Keep rejected if realized PnL remains negative or PF stays below 1."
    return RegistryEntry(name=name, classification=classification, summary=summary, reasons=reasons, upgrade_requirement=upgrade, retirement_requirement=retire)


def build_registry_state(records: list[dict[str, Any]]) -> dict[str, Any]:
    clean_rows = _clean_rows(records)
    gate_state = gate.build_report_state(records)
    pocket_entries = [_classify_entry(result) for result in gate_state["candidate_results"]]

    # Additional registry entries for the poison/monitoring bands explicitly called out by the prompt.
    extra_specs = {
        "entry|0.70-0.80": lambda r: price_bucket(entry_price(r)) == "0.70-0.80",
        "edge|0.05-0.10": lambda r: edge_bucket(_as_float(r.get("risk_edge"))) == "0.05-0.10",
        "edge|0.03-0.05": lambda r: edge_bucket(_as_float(r.get("risk_edge"))) == "0.03-0.05",
        "cell|0.10+|0.80-0.90": lambda r: edge_bucket(_as_float(r.get("risk_edge"))) == "0.10+" and price_bucket(entry_price(r)) == "0.80-0.90",
        "bootstrap_era_allow|0.60-0.70": lambda r: council_path(r) == "bootstrap_era_allow" and price_bucket(entry_price(r)) == "0.60-0.70",
        "bootstrap_era_allow|0.70-0.80": lambda r: council_path(r) == "bootstrap_era_allow" and price_bucket(entry_price(r)) == "0.70-0.80",
        "bootstrap_era_allow|0.80-0.90": lambda r: council_path(r) == "bootstrap_era_allow" and price_bucket(entry_price(r)) == "0.80-0.90",
    }
    for name, predicate in extra_specs.items():
        rows = [r for r in clean_rows if predicate(r)]
        if not rows:
            continue
        result = gate._evaluate_pocket(
            name,
            clean_rows,
            gate.PocketSpec(name=name, description=name, predicate=predicate),
        )
        pocket_entries.append(_classify_entry(result))

    snapshot = _evidence_snapshot(clean_rows)
    snapshot["watchlist_count"] = sum(1 for p in pocket_entries if p.classification == "WATCHLIST_ONLY")
    snapshot["quarantine_count"] = sum(1 for p in pocket_entries if p.classification == "RESEARCH_QUARANTINE")
    snapshot["rejected_count"] = sum(1 for p in pocket_entries if p.classification == "REJECTED_POISON")
    snapshot["promotion_blocked_count"] = sum(1 for p in pocket_entries if p.classification == "PROMOTION_BLOCKED")
    snapshot["promotion_eligible_count"] = sum(1 for p in pocket_entries if p.classification == "PROMOTION_ELIGIBLE_PAPER_ONLY")

    return {
        "records": records,
        "clean_rows": clean_rows,
        "gate_state": gate_state,
        "snapshot": snapshot,
        "pocket_entries": pocket_entries,
        "overall_status": "DO_NOT_PATCH_LIVE_YET",
    }


def _print_snapshot(snapshot: dict[str, Any]) -> None:
    print()
    print("EVIDENCE SNAPSHOT")
    print("-" * 86)
    print(f"  clean row count:        {snapshot['clean_row_count']}")
    print(f"  first timestamp:        {snapshot['first_timestamp']}")
    print(f"  last timestamp:         {snapshot['last_timestamp']}")
    print(f"  cohort hash:            {snapshot['cohort_hash']}")
    print(f"  total economic pnl:     {_fmt_money(snapshot['total_economic_pnl'])}")
    print(f"  roi:                    {_fmt_pct(snapshot['roi'])}")
    print(f"  profit factor:          {_fmt_num(snapshot['profit_factor'])}")
    print(f"  avg model_probability:  {_fmt_num(snapshot['avg_model_probability'])}")
    print(f"  avg entry_price:        {_fmt_num(snapshot['avg_entry_price'])}")
    print(f"  calibration gap:        {_fmt_num(snapshot['calibration_gap'])}")
    print(f"  high-entry pnl:         {_fmt_money(snapshot['high_entry_pnl'])}")
    print(f"  builder_boost pnl:      {_fmt_money(snapshot['builder_boost_pnl'])}")
    print(f"  watchlist count:        {snapshot['watchlist_count']}")
    print(f"  quarantine count:       {snapshot['quarantine_count']}")
    print(f"  rejected count:         {snapshot['rejected_count']}")
    print(f"  promotion blocked:      {snapshot['promotion_blocked_count']}")
    print(f"  promotion-eligible:     {snapshot['promotion_eligible_count']}")


def _print_registry(pockets: list[RegistryEntry]) -> None:
    print()
    print("RESEARCH QUARANTINE REGISTRY")
    print("-" * 140)
    print(
        f"{'pocket':<34} {'class':<22} {'n':>4} {'WR':>7} {'BE':>7} {'mrg':>7} {'PnL':>10} "
        f"{'ROI':>8} {'PF':>7} {'RR':>7} {'live':>6}"
    )
    print("-" * 140)
    for entry in sorted(pockets, key=lambda e: (e.classification, e.name)):
        s = entry.summary
        print(
            f"{entry.name:<34} {entry.classification:<22} {s['n']:>4} {_fmt_pct(s['win_rate']):>7} "
            f"{_fmt_pct(s['breakeven_wr']):>7} {_fmt_pct(s['breakeven_gap']):>7} {_fmt_money(s['total_economic_pnl']):>10} "
            f"{_fmt_pct(s['roi']):>8} {_fmt_num(s['profit_factor']):>7} {_fmt_num(s['reward_risk']):>7} {'NO':>6}"
        )
        print(f"    reason: {', '.join(entry.reasons[:4]) if entry.reasons else 'none'}")
        print(f"    upgrade: {entry.upgrade_requirement}")
        print(f"    retire:  {entry.retirement_requirement}")


def _print_terminal_refusal_rules() -> None:
    print()
    print("TERMINAL REFUSAL RULES")
    print("-" * 86)
    print("  - refuse live patching based on WATCHLIST_ONLY pockets.")
    print("  - refuse live patching based on tiny positive samples.")
    print("  - refuse live patching based on 0.90+ probability alone.")
    print("  - refuse live patching based on critic_caution alone.")
    print("  - refuse live patching based on model EV alone.")
    print("  - refuse live patching while realized PnL is negative.")
    print("  - refuse live patching while PF < 1.20.")
    print("  - refuse live patching while WR is below breakeven.")
    print("  - refuse live patching while calibration gap is too large.")
    print("  - refuse live patching in any RESEARCH_QUARANTINE pocket.")
    print("  - refuse scaling, Kelly, real money, cap increases, threshold weakening, or KXETH quarantine removal.")


def render_report(state: dict[str, Any]) -> None:
    print("=" * 86)
    print("RESEARCH QUARANTINE REGISTRY + EVIDENCE SNAPSHOT LEDGER")
    print("=" * 86)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print("Population: settled, economic_contract_notional_v1, normal_modern, non-KXETH clean proof rows only.")
    print(f"Raw records loaded: {len(state['records'])}")
    print(f"Clean rows used:    {len(state['clean_rows'])}")
    print(f"Overall status:     {state['overall_status']}")

    _print_snapshot(state["snapshot"])
    print()
    print("SINCE PRIOR PROOF REPORTS")
    print("-" * 86)
    print("  This registry consolidates the same clean Phase 9N+ evidence into durable pocket labels.")
    print("  The clean proof cohort is unchanged; the key wounds remain high-entry poison and builder_boost overlap.")
    print("  No new live evidence was created here. This is classification and retention logic only.")
    _print_registry(state["pocket_entries"])
    _print_terminal_refusal_rules()

    print()
    print("PROMOTION / QUARANTINE INTERPRETATION")
    print("-" * 86)
    print("  RESEARCH_QUARANTINE pockets are permanently blocked from live-patch discussion until a new cohort proves otherwise.")
    print("  REJECTED_POISON pockets are negative EV, below breakeven, or PF<1 on the current evidence.")
    print("  WATCHLIST_ONLY pockets may be monitored but remain below promotion standards.")
    print("  PROMISING_BUT_UNPROVEN pockets need more rolling proof before they can be discussed.")
    print("  PROMOTION_BLOCKED pockets fail one or more hard standards and remain blocked.")
    print("  PROMOTION_ELIGIBLE_PAPER_ONLY pockets are paper-only and still cannot be patched live in Phase 9X.")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> None:
    records = load_trades()
    state = build_registry_state(records)
    render_report(state)


if __name__ == "__main__":
    main()
