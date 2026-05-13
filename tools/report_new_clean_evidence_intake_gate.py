#!/usr/bin/env python3
"""
Phase 9Z — New Clean Evidence Intake Gate + Settlement Throughput Monitor
Sentinel: NEW_CLEAN_EVIDENCE_INTAKE_GATE_REPORT_OK

Read-only gate that refuses new strategic conclusions when the clean proof
cohort has not materially changed since Phase 9Y / Phase 9X.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_evidence_delta_registry_drift as delta
import tools.report_probability_calibration_payoff_truth as calib
from tools.report_accounting_version_proof_cohorts import (
    classify_accounting_version,
    economic_pnl_value,
    entry_price,
    is_kxeth_or_quarantined,
    load_trades,
    recorded_pnl_value,
    stored_pnl_value,
)
from tools.report_fresh_economic_proof_autopsy import council_path, edge_bucket, price_bucket
from tools.report_probability_calibration_payoff_truth import model_probability_value
from tools.report_accounting_version_proof_cohorts import active_open_rows

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "NEW_CLEAN_EVIDENCE_INTAKE_GATE_REPORT_OK"

BASELINE_PHASE = "PHASE_9Y_EVIDENCE_DELTA_REGISTRY_DRIFT / PHASE_9X_RESEARCH_QUARANTINE_REGISTRY"
BASELINE_COMMIT = "583b7ad / 4059e26"
BASELINE_COHORT_HASH = delta.BASELINE_COHORT_HASH
BASELINE_CLEAN_ROWS = delta.BASELINE_SNAPSHOT["clean_row_count"]
BASELINE_FIRST_TIMESTAMP = delta.BASELINE_SNAPSHOT["first_timestamp"]
BASELINE_LAST_TIMESTAMP = delta.BASELINE_SNAPSHOT["last_timestamp"]


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _classify_intake_status(new_clean_rows: int) -> str:
    if new_clean_rows == 0:
        return "NO_NEW_CLEAN_EVIDENCE"
    if new_clean_rows < 20:
        return "INSUFFICIENT_NEW_EVIDENCE"
    if new_clean_rows < 50:
        return "MINIMUM_RECHECK_READY"
    if new_clean_rows < 100:
        return "PROMOTION_RECHECK_READY"
    return "STRONG_RECHECK_READY"


def _throughput_status(
    *,
    new_clean_rows: int,
    active_open_count: int,
    raw_rows_newer_than_baseline: int,
    contamination_new_ratio: float | None,
) -> str:
    if active_open_count > 0:
        return "BACKLOG_OPEN_ROWS"
    if contamination_new_ratio is not None and contamination_new_ratio > 0.50:
        return "CONTAMINATION_HEAVY"
    if new_clean_rows == 0:
        return "NO_NEW_CLEAN_EVIDENCE"
    if new_clean_rows < 20:
        return "STALE"
    if raw_rows_newer_than_baseline <= 0:
        return "STALE"
    return "HEALTHY"


def _new_clean_rows_since_baseline(
    clean_rows: list[dict[str, Any]],
    baseline_last_timestamp: str | None,
) -> list[dict[str, Any]]:
    cutoff = _parse_ts(baseline_last_timestamp)
    if cutoff is None:
        return list(clean_rows)
    out: list[dict[str, Any]] = []
    for rec in clean_rows:
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None and ts > cutoff:
            out.append(rec)
    return out


def _row_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = dict(calib._bucket_summary(rows)) if rows else {
        "n": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": None,
        "avg_model_probability": None,
        "avg_entry_price": None,
        "avg_risk_edge": None,
        "breakeven_wr": None,
        "breakeven_gap": None,
        "calibration_gap": None,
        "total_economic_pnl": None,
        "total_expected_ev": None,
        "roi": None,
        "profit_factor": None,
        "avg_win": None,
        "avg_loss": None,
        "reward_risk": None,
        "max_drawdown": None,
    }
    summary["clv"] = summary.get("avg_clv")
    return summary


def build_intake_state(
    records: list[dict[str, Any]],
    *,
    baseline_snapshot: dict[str, Any] | None = None,
    baseline_commit: str = BASELINE_COMMIT,
) -> dict[str, Any]:
    baseline_snapshot = dict(delta.BASELINE_SNAPSHOT if baseline_snapshot is None else baseline_snapshot)
    current_delta = delta.build_delta_state(records, baseline_snapshot=baseline_snapshot, baseline_pocket_classes=delta.BASELINE_POCKET_CLASSES)
    current_snapshot = dict(current_delta["current_snapshot"])
    clean_rows = list(current_delta["current_state"]["clean_rows"])
    new_clean_rows = _new_clean_rows_since_baseline(clean_rows, baseline_snapshot.get("last_timestamp"))
    new_clean_summary = _row_summary(new_clean_rows)

    raw_total = len(records)
    open_rows = sum(1 for r in records if str(r.get("status") or "").upper() == "OPEN")
    settled_rows = sum(1 for r in records if str(r.get("status") or "").upper() in {"SETTLED", "FORCED_CLOSE"})
    missing_outcome_rows = sum(
        1
        for r in records
        if str(r.get("status") or "").upper() not in {"OPEN", "SETTLED", "FORCED_CLOSE"}
        and str(r.get("result") or "").upper() not in {"WIN", "LOSS", "TIME_EXIT"}
    )
    kxeth_rows = sum(1 for r in records if is_kxeth_or_quarantined(r))
    dc_rows = sum(1 for r in records if bool(r.get("data_collection_override")))
    bootstrap_rows = sum(1 for r in records if bool(r.get("bootstrap_provisional")))
    side_rows = sum(1 for r in records if bool(r.get("side_coverage_test")) or bool(r.get("side_coverage")))
    legacy_rows = sum(1 for r in records if classify_accounting_version(r) == "legacy_hybrid_or_unversioned")
    unknown_rows = sum(1 for r in records if classify_accounting_version(r) == "unknown_other")
    missing_prob_rows = sum(1 for r in records if model_probability_value(r) is None)
    missing_entry_rows = sum(1 for r in records if entry_price(r) is None)
    missing_econ_rows = sum(1 for r in records if economic_pnl_value(r) is None)

    active_open_count = len(active_open_rows(records))
    stale_open_count = max(0, open_rows - active_open_count)
    raw_rows_newer_than_baseline = sum(
        1
        for r in records
        if (ts := _parse_ts(r.get("timestamp"))) is not None and _parse_ts(baseline_snapshot.get("last_timestamp")) is not None and ts > _parse_ts(baseline_snapshot.get("last_timestamp"))
    )
    contamination_new_rows = 0
    if raw_rows_newer_than_baseline > 0:
        contamination_new_rows = sum(
            1
            for r in records
            if (ts := _parse_ts(r.get("timestamp"))) is not None
            and _parse_ts(baseline_snapshot.get("last_timestamp")) is not None
            and ts > _parse_ts(baseline_snapshot.get("last_timestamp"))
            and (
                is_kxeth_or_quarantined(r)
                or bool(r.get("data_collection_override"))
                or bool(r.get("bootstrap_provisional"))
                or bool(r.get("side_coverage_test"))
                or bool(r.get("side_coverage"))
                or classify_accounting_version(r) in {"legacy_hybrid_or_unversioned", "unknown_other"}
                or model_probability_value(r) is None
                or entry_price(r) is None
                or economic_pnl_value(r) is None
            )
        )
    contamination_new_ratio = (
        contamination_new_rows / raw_rows_newer_than_baseline if raw_rows_newer_than_baseline > 0 else None
    )
    intake_status = _classify_intake_status(len(new_clean_rows))
    throughput_status = _throughput_status(
        new_clean_rows=len(new_clean_rows),
        active_open_count=active_open_count,
        raw_rows_newer_than_baseline=raw_rows_newer_than_baseline,
        contamination_new_ratio=contamination_new_ratio,
    )

    latest_open_candidates = [
        ts for ts in (_parse_ts(r.get("timestamp")) for r in records if str(r.get("status") or "").upper() == "OPEN")
        if ts is not None
    ]
    latest_settled_candidates = [
        ts
        for ts in (
            _parse_ts(r.get("timestamp"))
            for r in records
            if str(r.get("status") or "").upper() in {"SETTLED", "FORCED_CLOSE"}
        )
        if ts is not None
    ]
    newest_clean_candidates = [ts for ts in (_parse_ts(r.get("timestamp")) for r in clean_rows) if ts is not None]
    latest_open_ts = max(latest_open_candidates, default=None)
    latest_settled_ts = max(latest_settled_candidates, default=None)
    newest_clean_ts = max(newest_clean_candidates, default=None)
    now = datetime.now(timezone.utc)
    newest_clean_age_hours = (
        (now - newest_clean_ts).total_seconds() / 3600 if newest_clean_ts is not None else None
    )

    return {
        "baseline_phase": BASELINE_PHASE,
        "baseline_commit": baseline_commit,
        "baseline_snapshot": baseline_snapshot,
        "current_commit": delta._current_commit(),
        "current_state": current_delta["current_state"],
        "current_snapshot": current_snapshot,
        "clean_rows": clean_rows,
        "new_clean_rows": new_clean_rows,
        "new_clean_summary": new_clean_summary,
        "intake_status": intake_status,
        "throughput_status": throughput_status,
        "raw_total": raw_total,
        "open_rows": open_rows,
        "settled_rows": settled_rows,
        "missing_outcome_rows": missing_outcome_rows,
        "kxeth_rows": kxeth_rows,
        "data_collection_override_rows": dc_rows,
        "bootstrap_provisional_rows": bootstrap_rows,
        "side_coverage_rows": side_rows,
        "legacy_rows": legacy_rows,
        "unknown_rows": unknown_rows,
        "missing_model_probability_rows": missing_prob_rows,
        "missing_entry_price_rows": missing_entry_rows,
        "missing_economic_pnl_rows": missing_econ_rows,
        "active_open_count": active_open_count,
        "stale_open_count": stale_open_count,
        "raw_rows_newer_than_baseline": raw_rows_newer_than_baseline,
        "contamination_new_rows": contamination_new_rows,
        "contamination_new_ratio": contamination_new_ratio,
        "latest_open_ts": latest_open_ts,
        "latest_settled_ts": latest_settled_ts,
        "newest_clean_ts": newest_clean_ts,
        "newest_clean_age_hours": newest_clean_age_hours,
        "overall_status": "DO_NOT_PATCH_LIVE_YET",
    }


def _print_counts(state: dict[str, Any]) -> None:
    print()
    print("SETTLEMENT THROUGHPUT")
    print("-" * 94)
    print(f"  raw total rows:                 {state['raw_total']}")
    print(f"  settled rows:                   {state['settled_rows']}")
    print(f"  open rows:                      {state['open_rows']}")
    print(f"  active open rows:               {state['active_open_count']}")
    print(f"  stale open rows:                {state['stale_open_count']}")
    print(f"  missing outcome rows:           {state['missing_outcome_rows']}")
    print(f"  KXETH/quarantined rows:         {state['kxeth_rows']}")
    print(f"  data_collection_override rows:  {state['data_collection_override_rows']}")
    print(f"  bootstrap_provisional rows:     {state['bootstrap_provisional_rows']}")
    print(f"  side-coverage rows:             {state['side_coverage_rows']}")
    print(f"  legacy/unversioned rows:        {state['legacy_rows']}")
    print(f"  unknown_other rows:             {state['unknown_rows']}")
    print(f"  missing model_probability rows: {state['missing_model_probability_rows']}")
    print(f"  missing entry_price rows:       {state['missing_entry_price_rows']}")
    print(f"  missing economic_pnl rows:      {state['missing_economic_pnl_rows']}")


def _print_snapshot(state: dict[str, Any]) -> None:
    snap = state["current_snapshot"]
    base = state["baseline_snapshot"]
    print()
    print("EVIDENCE INTAKE SUMMARY")
    print("-" * 94)
    print(f"  baseline cohort hash:          {base['cohort_hash']}")
    print(f"  current cohort hash:           {snap['cohort_hash']}")
    print(f"  hash changed:                  {'YES' if snap['cohort_hash'] != base['cohort_hash'] else 'NO'}")
    print(f"  baseline clean rows:           {BASELINE_CLEAN_ROWS}")
    print(f"  current clean rows:            {snap['clean_row_count']}")
    print(f"  new clean rows since baseline:  {len(state['new_clean_rows'])}")
    print(f"  raw rows newer than baseline:   {state['raw_rows_newer_than_baseline']}")
    print(f"  contaminated newer rows:        {state['contamination_new_rows']}")
    print(f"  first clean timestamp:         {base['first_timestamp']}")
    print(f"  last clean timestamp:          {snap['last_timestamp']}")
    print(f"  newest clean age (hours):      {_fmt_num(state['newest_clean_age_hours'], 1)}")
    print(f"  latest open row timestamp:     {state['latest_open_ts']}")
    print(f"  latest settled row timestamp:  {state['latest_settled_ts']}")
    print(f"  intake gate status:            {state['intake_status']}")
    print(f"  settlement throughput status:   {state['throughput_status']}")
    print(f"  live_patch_permission:         NO")
    print(f"  overall status:                {state['overall_status']}")


def _print_new_evidence_summary(state: dict[str, Any]) -> None:
    print()
    print("NEW EVIDENCE QUALITY")
    print("-" * 94)
    if not state["new_clean_rows"]:
        print("  NO_NEW_CLEAN_EVIDENCE — do not draw a new conclusion.")
        print("  raw rows newer than baseline: 0")
        print("  new clean rows: 0")
        print(f"  contaminated newer rows: {state['contamination_new_rows']}")
        return

    s = state["new_clean_summary"]
    new_rows = state["new_clean_rows"]
    high_entry_rows = [r for r in new_rows if price_bucket(entry_price(r)) == "0.80-0.90"]
    builder_rows = [r for r in new_rows if council_path(r) == "builder_boost"]
    p90_rows = [r for r in new_rows if model_probability_value(r) is not None and model_probability_value(r) >= 0.90]
    edge10_rows = [r for r in new_rows if edge_bucket(delta._as_float(r.get("risk_edge"))) == "0.10+"]
    print(f"  n:                             {s['n']}")
    print(f"  wins / losses:                 {s['wins']} / {s['losses']}")
    print(f"  win rate:                      {_fmt_pct(s['win_rate'])}")
    print(f"  avg model_probability:         {_fmt_num(s['avg_model_probability'])}")
    print(f"  avg entry_price:               {_fmt_num(s['avg_entry_price'])}")
    print(f"  calibration gap:               {_fmt_num(s['calibration_gap'])}")
    print(f"  breakeven wr:                  {_fmt_pct(s['breakeven_wr'])}")
    print(f"  win-rate margin:               {_fmt_pct(s['breakeven_gap'])}")
    print(f"  economic pnl:                  {_fmt_money(s['total_economic_pnl'])}")
    print(f"  ROI:                           {_fmt_pct(s['roi'])}")
    print(f"  PF:                            {_fmt_num(s['profit_factor'])}")
    print(f"  avg win / avg loss:            {_fmt_money(s['avg_win'])} / {_fmt_money(s['avg_loss'])}")
    print(f"  reward / risk:                 {_fmt_num(s['reward_risk'])}")
    print(f"  model EV sum:                  {_fmt_money(s['total_expected_ev'])}")
    print(f"  realized PnL:                  {_fmt_money(s['total_economic_pnl'])}")
    print(f"  EV gap:                        {_fmt_money(s['ev_gap'])}")
    print(f"  high-entry 0.80-0.90 rows/PnL:  {len(high_entry_rows)} / {_fmt_money(sum((economic_pnl_value(r) or 0.0) for r in high_entry_rows))}")
    print(f"  builder_boost rows/PnL:        {len(builder_rows)} / {_fmt_money(sum((economic_pnl_value(r) or 0.0) for r in builder_rows))}")
    print(f"  probability 0.90+ rows/PnL:    {len(p90_rows)} / {_fmt_money(sum((economic_pnl_value(r) or 0.0) for r in p90_rows))}")
    print(f"  edge 0.10+ rows/PnL:           {len(edge10_rows)} / {_fmt_money(sum((economic_pnl_value(r) or 0.0) for r in edge10_rows))}")


def _print_refusal_rules() -> None:
    print()
    print("STALE DATA REFUSAL RULES")
    print("-" * 94)
    print("  - refuse any new strategy conclusion when new clean rows = 0.")
    print("  - refuse any promotion claim when new clean rows < 50.")
    print("  - refuse any live patch when cohort hash is unchanged.")
    print("  - refuse any live patch based on the same evidence hash as Phase 9X / 9Y.")
    print("  - refuse any live patch based on watchlist-only pockets.")
    print("  - refuse any live patch based on tiny positive samples.")
    print("  - refuse any live patch based on 0.90+ probability alone.")
    print("  - refuse any live patch based on model EV alone.")
    print("  - refuse any live patch based on critic_caution alone.")
    print("  - refuse any live patch in research-quarantine pockets.")
    print("  - refuse scaling, Kelly, real money, cap increases, threshold weakening, or KXETH quarantine removal.")


def render_report(state: dict[str, Any]) -> None:
    print("=" * 94)
    print("NEW CLEAN EVIDENCE INTAKE GATE + SETTLEMENT THROUGHPUT MONITOR")
    print("=" * 94)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print("Purpose: refuse new strategic conclusions when clean evidence has not changed enough.")
    print(f"Source: {TRADES_LOG}")
    print(f"Baseline phase: {state['baseline_phase']}")
    print(f"Baseline commit: {state['baseline_commit']}")
    print(f"Current commit:  {state['current_commit']}")
    print(f"Raw records loaded: {len(state['current_state']['records'])}")
    print(f"Clean rows used:    {len(state['clean_rows'])}")

    _print_counts(state)
    _print_snapshot(state)
    _print_new_evidence_summary(state)
    _print_refusal_rules()

    print()
    print("RECHECK GUIDANCE")
    print("-" * 94)
    print("  minimum new clean rows for Phase 10A recheck: 20")
    print("  minimum new clean rows for promotion recheck: 50")
    print("  strong new clean rows for deeper review:      100")
    print("  recommendation: do not start another research phase until at least MINIMUM_RECHECK_READY.")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> None:
    records = load_trades()
    state = build_intake_state(records)
    render_report(state)


if __name__ == "__main__":
    main()
