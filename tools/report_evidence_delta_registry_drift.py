#!/usr/bin/env python3
"""
Phase 9Y — Evidence Delta + Registry Drift Monitor
Sentinel: EVIDENCE_DELTA_REGISTRY_DRIFT_REPORT_OK

Read-only comparison of the current clean proof cohort against the Phase 9X
registry snapshot. The report does not modify logs, thresholds, gates,
execution behavior, or trading state.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_research_quarantine_registry as registry
from tools.report_accounting_version_proof_cohorts import load_trades

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "EVIDENCE_DELTA_REGISTRY_DRIFT_REPORT_OK"

BASELINE_PHASE = "PHASE_9X_RESEARCH_QUARANTINE_REGISTRY"
BASELINE_COMMIT = "4059e26"
BASELINE_COHORT_HASH = "a2b8f3a38de8d97f0065de033cd479f8062f998a988c7dfb21c821654e464c7d"

BASELINE_SNAPSHOT = {
    "clean_row_count": 53,
    "first_timestamp": "2026-05-11T00:05:02.735654+00:00",
    "last_timestamp": "2026-05-13T06:12:58.607614+00:00",
    "cohort_hash": BASELINE_COHORT_HASH,
    "total_economic_pnl": -27.75,
    "roi": -0.13,
    "profit_factor": 0.5630,
    "avg_model_probability": 0.8779,
    "avg_entry_price": 0.8028,
    "calibration_gap": 0.1798,
    "high_entry_pnl": -26.40,
    "builder_boost_pnl": -24.00,
    "watchlist_count": 6,
    "quarantine_count": 9,
    "rejected_count": 0,
    "promotion_blocked_count": 0,
    "promotion_eligible_count": 0,
}

BASELINE_POCKET_CLASSES = {
    "builder_boost|0.80-0.90": "RESEARCH_QUARANTINE",
    "entry|0.80-0.90": "RESEARCH_QUARANTINE",
    "edge|0.10+": "RESEARCH_QUARANTINE",
    "cell|0.05-0.10|0.80-0.90": "RESEARCH_QUARANTINE",
    "cell|0.10+|0.80-0.90": "RESEARCH_QUARANTINE",
    "entry|0.70-0.80": "RESEARCH_QUARANTINE",
    "edge|0.05-0.10": "RESEARCH_QUARANTINE",
    "bootstrap_era_allow|0.70-0.80": "RESEARCH_QUARANTINE",
    "bootstrap_era_allow|0.80-0.90": "RESEARCH_QUARANTINE",
    "bootstrap_era_allow|0.60-0.70": "WATCHLIST_ONLY",
    "cell|0.03-0.05|0.80-0.90": "WATCHLIST_ONLY",
    "critic_caution|0.80-0.90": "WATCHLIST_ONLY",
    "probability|0.90+": "WATCHLIST_ONLY",
    "edge|0.03-0.05": "DO_NOT_PATCH_LIVE_YET",
    "critic_caution|0.90-1.00": "WATCHLIST_ONLY",
    "cell|0.90+|0.90-1.00": "WATCHLIST_ONLY",
}

IMPORTANT_POCKETS = [
    "builder_boost|0.80-0.90",
    "critic_caution|0.80-0.90",
    "critic_caution|0.90-1.00",
    "probability|0.90+",
    "entry|0.80-0.90",
    "entry|0.70-0.80",
    "edge|0.10+",
    "edge|0.05-0.10",
    "edge|0.03-0.05",
    "cell|0.05-0.10|0.80-0.90",
    "cell|0.10+|0.80-0.90",
    "cell|0.03-0.05|0.80-0.90",
    "cell|0.90+|0.90-1.00",
    "bootstrap_era_allow|0.60-0.70",
    "bootstrap_era_allow|0.70-0.80",
    "bootstrap_era_allow|0.80-0.90",
]

SEVERITY = {
    "PROMOTION_ELIGIBLE_PAPER_ONLY": 0,
    "PROMISING_BUT_UNPROVEN": 1,
    "WATCHLIST_ONLY": 2,
    "PROMOTION_BLOCKED": 3,
    "DO_NOT_PATCH_LIVE_YET": 4,
    "REJECTED_POISON": 5,
    "RESEARCH_QUARANTINE": 6,
    None: -1,
}


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


def _current_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or "MISSING"
    except Exception:
        return "MISSING"


def classify_drift(previous_class: str | None, current_class: str | None) -> str:
    if previous_class is None and current_class is None:
        return "UNCHANGED"
    if previous_class is None:
        return "NEW"
    if current_class is None:
        return "REMOVED"
    prev = SEVERITY.get(previous_class, 99)
    curr = SEVERITY.get(current_class, 99)
    if curr < prev:
        return "IMPROVED"
    if curr > prev:
        return "WORSENED"
    return "UNCHANGED"


def _snapshot_deltas(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        "clean_row_delta": current["clean_row_count"] - baseline["clean_row_count"],
        "new_clean_rows_added": max(0, current["clean_row_count"] - baseline["clean_row_count"]),
        "pnl_delta": (_as_float(current["total_economic_pnl"]) or 0.0) - (_as_float(baseline["total_economic_pnl"]) or 0.0),
        "roi_delta": (_as_float(current["roi"]) or 0.0) - (_as_float(baseline["roi"]) or 0.0),
        "pf_delta": (_as_float(current["profit_factor"]) or 0.0) - (_as_float(baseline["profit_factor"]) or 0.0),
        "calibration_gap_delta": (_as_float(current["calibration_gap"]) or 0.0) - (_as_float(baseline["calibration_gap"]) or 0.0),
        "high_entry_pnl_delta": (_as_float(current["high_entry_pnl"]) or 0.0) - (_as_float(baseline["high_entry_pnl"]) or 0.0),
        "builder_boost_pnl_delta": (_as_float(current["builder_boost_pnl"]) or 0.0) - (_as_float(baseline["builder_boost_pnl"]) or 0.0),
    }


def _registry_map(state: dict[str, Any]) -> dict[str, Any]:
    return {entry.name: entry for entry in state["pocket_entries"]}


def _current_cohort_status(hash_changed: bool) -> str:
    return "NEW_CLEAN_EVIDENCE_DETECTED" if hash_changed else "NO_NEW_CLEAN_EVIDENCE_SINCE_9X"


def _drift_rows(
    current_state: dict[str, Any],
    baseline_pocket_classes: dict[str, str],
) -> list[dict[str, Any]]:
    current_map = _registry_map(current_state)
    names = sorted(set(IMPORTANT_POCKETS) | set(baseline_pocket_classes) | set(current_map))
    rows: list[dict[str, Any]] = []
    for name in names:
        current_entry = current_map.get(name)
        current_class = current_entry.classification if current_entry else None
        previous_class = baseline_pocket_classes.get(name)
        drift = classify_drift(previous_class, current_class)
        summary = current_entry.summary if current_entry else {}
        rows.append(
            {
                "name": name,
                "previous_class": previous_class or "MISSING",
                "current_class": current_class or "MISSING",
                "status_drift": drift,
                "n": summary.get("n", 0),
                "wins": summary.get("wins", 0),
                "losses": summary.get("losses", 0),
                "win_rate": summary.get("win_rate"),
                "breakeven_wr": summary.get("breakeven_wr"),
                "breakeven_gap": summary.get("breakeven_gap"),
                "avg_model_probability": summary.get("avg_model_probability"),
                "calibration_gap": summary.get("calibration_abs"),
                "avg_entry_price": summary.get("avg_entry_price"),
                "economic_pnl": summary.get("total_economic_pnl"),
                "roi": summary.get("roi"),
                "profit_factor": summary.get("profit_factor"),
                "avg_win": summary.get("avg_win"),
                "avg_loss": summary.get("avg_loss"),
                "reward_risk": summary.get("reward_risk"),
                "model_ev": summary.get("total_expected_ev"),
                "realized_pnl": summary.get("total_economic_pnl"),
                "ev_gap": summary.get("ev_gap"),
                "reason": ", ".join(current_entry.reasons[:4]) if current_entry and current_entry.reasons else "baseline or missing",
                "live_patch_permission": "NO",
            }
        )
    return rows


def build_delta_state(
    records: list[dict[str, Any]],
    *,
    baseline_snapshot: dict[str, Any] | None = None,
    baseline_pocket_classes: dict[str, str] | None = None,
) -> dict[str, Any]:
    current_state = registry.build_registry_state(records)
    current_snapshot = dict(current_state["snapshot"])
    baseline_snapshot = dict(BASELINE_SNAPSHOT if baseline_snapshot is None else baseline_snapshot)
    baseline_pocket_classes = dict(BASELINE_POCKET_CLASSES if baseline_pocket_classes is None else baseline_pocket_classes)

    hash_changed = current_snapshot["cohort_hash"] != baseline_snapshot["cohort_hash"]
    deltas = _snapshot_deltas(current_snapshot, baseline_snapshot)
    drift_rows = _drift_rows(current_state, baseline_pocket_classes)

    classification_changes = [row for row in drift_rows if row["status_drift"] != "UNCHANGED"]
    new_quarantine_pockets = [row["name"] for row in drift_rows if row["current_class"] == "RESEARCH_QUARANTINE" and row["previous_class"] != "RESEARCH_QUARANTINE"]
    worsened_watchlist = [row["name"] for row in drift_rows if row["previous_class"] == "WATCHLIST_ONLY" and row["current_class"] in {"PROMOTION_BLOCKED", "DO_NOT_PATCH_LIVE_YET", "REJECTED_POISON", "RESEARCH_QUARANTINE"}]
    improved_watchlist = [row["name"] for row in drift_rows if row["previous_class"] == "WATCHLIST_ONLY" and row["current_class"] in {"PROMISING_BUT_UNPROVEN", "PROMOTION_ELIGIBLE_PAPER_ONLY"}]
    promotion_eligible = [row["name"] for row in drift_rows if row["current_class"] == "PROMOTION_ELIGIBLE_PAPER_ONLY"]

    return {
        "baseline_phase": BASELINE_PHASE,
        "baseline_commit": BASELINE_COMMIT,
        "baseline_snapshot": baseline_snapshot,
        "baseline_pocket_classes": baseline_pocket_classes,
        "current_commit": _current_commit(),
        "current_state": current_state,
        "current_snapshot": current_snapshot,
        "hash_changed": hash_changed,
        "evidence_status": _current_cohort_status(hash_changed),
        "delta": deltas,
        "drift_rows": drift_rows,
        "classification_changes": classification_changes,
        "new_quarantine_pockets": new_quarantine_pockets,
        "worsened_watchlist_pockets": worsened_watchlist,
        "improved_watchlist_pockets": improved_watchlist,
        "promotion_eligible_pockets": promotion_eligible,
        "overall_status": "DO_NOT_PATCH_LIVE_YET",
    }


def _print_snapshot(label: str, snapshot: dict[str, Any]) -> None:
    print()
    print(label)
    print("-" * 90)
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


def _print_delta(state: dict[str, Any]) -> None:
    d = state["delta"]
    print()
    print("DELTA SUMMARY")
    print("-" * 90)
    print(f"  hash changed:           {'YES' if state['hash_changed'] else 'NO'}")
    print(f"  status:                 {state['evidence_status']}")
    print(f"  new clean rows added:    {d['new_clean_rows_added']}")
    print(f"  clean row delta:         {d['clean_row_delta']}")
    print(f"  pnl delta:               {_fmt_money(d['pnl_delta'])}")
    print(f"  roi delta:               {_fmt_pct(d['roi_delta'])}")
    print(f"  pf delta:                {_fmt_num(d['pf_delta'])}")
    print(f"  calibration gap delta:   {_fmt_num(d['calibration_gap_delta'])}")
    print(f"  high-entry pnl delta:    {_fmt_money(d['high_entry_pnl_delta'])}")
    print(f"  builder_boost pnl delta: {_fmt_money(d['builder_boost_pnl_delta'])}")
    print(f"  classification changes:   {len(state['classification_changes'])}")
    print(f"  new quarantine pockets:   {len(state['new_quarantine_pockets'])}")
    print(f"  worsened watchlist:       {len(state['worsened_watchlist_pockets'])}")
    print(f"  improved watchlist:       {len(state['improved_watchlist_pockets'])}")
    print(f"  promotion-eligible:       {len(state['promotion_eligible_pockets'])}")


def _print_drift_table(rows: list[dict[str, Any]]) -> None:
    print()
    print("REGISTRY DRIFT TABLE")
    print("-" * 170)
    print(
        f"{'pocket':<34} {'prev':<24} {'curr':<24} {'drift':<12} {'n':>4} {'WR':>7} {'BE':>7} {'mrg':>7} "
        f"{'PnL':>10} {'ROI':>8} {'PF':>7} {'live_patch_permission':>21}"
    )
    print("-" * 170)
    for row in rows:
        print(
            f"{row['name']:<34} {row['previous_class']:<24} {row['current_class']:<24} {row['status_drift']:<12} "
            f"{row['n']:>4} {_fmt_pct(row['win_rate']):>7} {_fmt_pct(row['breakeven_wr']):>7} {_fmt_pct(row['breakeven_gap']):>7} "
            f"{_fmt_money(row['economic_pnl']):>10} {_fmt_pct(row['roi']):>8} {_fmt_num(row['profit_factor']):>7} {row['live_patch_permission']:>5}"
        )
        print(
            f"    reason: {row['reason']}"
        )
        print(
            f"    CLV: {_fmt_num(row['calibration_gap'])}  model_ev: {_fmt_money(row['model_ev'])}  ev_gap: {_fmt_money(row['ev_gap'])}"
        )


def _print_refusal_rules() -> None:
    print()
    print("TERMINAL REFUSAL RULES")
    print("-" * 90)
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
    print("=" * 90)
    print("EVIDENCE DELTA + REGISTRY DRIFT MONITOR")
    print("=" * 90)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print("Purpose: compare the current clean proof cohort to the Phase 9X registry snapshot.")
    print(f"Source: {TRADES_LOG}")
    print(f"Baseline phase: {state['baseline_phase']} @ {state['baseline_commit']}")
    print(f"Current commit: {state['current_commit']}")
    print(f"Raw records loaded: {len(state['current_state']['records'])}")
    print(f"Clean rows used:    {len(state['current_state']['clean_rows'])}")
    print(f"Overall status:     {state['overall_status']}")

    _print_snapshot("BASELINE SNAPSHOT", state["baseline_snapshot"])
    _print_snapshot("CURRENT SNAPSHOT", state["current_snapshot"])
    _print_delta(state)
    _print_drift_table(state["drift_rows"])
    _print_refusal_rules()

    print()
    print("INTERPRETATION")
    print("-" * 90)
    print("  RESEARCH_QUARANTINE pockets remain blocked from live-patch discussion.")
    print("  WATCHLIST_ONLY pockets remain unproven and below promotion standards.")
    print("  PROMOTION_ELIGIBLE_PAPER_ONLY would still be paper-only in this phase.")
    print("  If the cohort hash is unchanged, the correct answer is no new clean evidence and no new conclusion.")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> None:
    records = load_trades()
    state = build_delta_state(records)
    render_report(state)


if __name__ == "__main__":
    main()
