#!/usr/bin/env python3
"""
Phase 10A — Proof Operations Hardening
Sentinel: PROOF_OPERATIONS_HARDENING_REPORT_OK

Read-only diagnostic for evidence production and settlement throughput.
This report does not change strategy, thresholds, gates, logs, or execution.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_evidence_delta_registry_drift as delta
import tools.report_new_clean_evidence_intake_gate as intake
from tools.report_accounting_version_proof_cohorts import (
    classify_accounting_version,
    economic_pnl_value,
    entry_price,
    is_kxeth_or_quarantined,
    load_trades,
)
from tools.report_probability_calibration_payoff_truth import model_probability_value

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
SENTINEL = "PROOF_OPERATIONS_HARDENING_REPORT_OK"
BASELINE_HASH = delta.BASELINE_COHORT_HASH
BASELINE_CLEAN_ROWS = delta.BASELINE_SNAPSHOT["clean_row_count"]
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _counts_by_reason(records: list[dict[str, Any]], *, after_ts: datetime | None = None) -> dict[str, int]:
    counts = {
        "kxeth": 0,
        "data_collection_override": 0,
        "bootstrap_provisional": 0,
        "side_coverage": 0,
        "legacy_or_unversioned": 0,
        "unknown_other": 0,
        "missing_model_probability": 0,
        "missing_entry_price": 0,
        "missing_economic_pnl": 0,
        "missing_result": 0,
    }
    for rec in records:
        ts = _parse_ts(rec.get("timestamp"))
        if after_ts is not None and (ts is None or ts <= after_ts):
            continue
        if is_kxeth_or_quarantined(rec):
            counts["kxeth"] += 1
        if bool(rec.get("data_collection_override")):
            counts["data_collection_override"] += 1
        if bool(rec.get("bootstrap_provisional")):
            counts["bootstrap_provisional"] += 1
        if bool(rec.get("side_coverage")) or bool(rec.get("side_coverage_test")):
            counts["side_coverage"] += 1
        cohort = classify_accounting_version(rec)
        if cohort == "legacy_hybrid_or_unversioned":
            counts["legacy_or_unversioned"] += 1
        elif cohort == "unknown_other":
            counts["unknown_other"] += 1
        if model_probability_value(rec) is None:
            counts["missing_model_probability"] += 1
        if entry_price(rec) is None:
            counts["missing_entry_price"] += 1
        if economic_pnl_value(rec) is None:
            counts["missing_economic_pnl"] += 1
        if str(rec.get("result") or "").upper() not in {"WIN", "LOSS", "TIME_EXIT"}:
            counts["missing_result"] += 1
    return counts


def _load_funnel_rows() -> list[dict[str, Any]]:
    return _read_jsonl(FUNNEL_LOG)


def _funnel_state(rows: list[dict[str, Any]], baseline_ts: datetime | None) -> dict[str, Any]:
    latest_ts = None
    recent_rows = 0
    paper_opened = 0
    passed_to_paper = 0
    blocked_max_open = 0
    dashboard_seen = 0
    final_reasons: dict[str, int] = {}
    for rec in rows:
        ts = _parse_ts(rec.get("timestamp_utc") or rec.get("timestamp"))
        if ts is not None and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
        if baseline_ts is not None and ts is not None and ts > baseline_ts:
            recent_rows += 1
        if rec.get("paper_trade_opened"):
            paper_opened += 1
        if rec.get("passed_to_paper_trader"):
            passed_to_paper += 1
        if rec.get("final_reason") == "BLOCKED_MAX_OPEN_TRADES":
            blocked_max_open += 1
        if rec.get("dashboard_seen"):
            dashboard_seen += 1
        reason = str(rec.get("final_reason") or "UNKNOWN")
        final_reasons[reason] = final_reasons.get(reason, 0) + 1
    return {
        "total_rows": len(rows),
        "recent_rows": recent_rows,
        "latest_timestamp": latest_ts.isoformat() if latest_ts else None,
        "paper_trade_opened": paper_opened,
        "passed_to_paper_trader": passed_to_paper,
        "blocked_max_open": blocked_max_open,
        "dashboard_seen": dashboard_seen,
        "final_reasons": final_reasons,
    }


def _dashboard_truth_state() -> dict[str, Any]:
    try:
        from tools.report_health import check_dashboard_process, check_settle_loop

        dash = check_dashboard_process()
        settle = check_settle_loop()
        return {
            "dashboard_running": bool(dash.get("running")),
            "dashboard_pids": dash.get("pids", []),
            "auto_settle_running": bool(settle.get("running")),
            "auto_settle_pid": settle.get("loop_pid"),
            "heartbeat_age_seconds": settle.get("heartbeat_age_seconds"),
            "last_log_line": settle.get("last_log_line"),
        }
    except Exception as exc:
        return {"dashboard_running": False, "auto_settle_running": False, "error": str(exc)}


def _research_truth_state() -> dict[str, Any]:
    try:
        from tools.report_health import check_research_truth

        return dict(check_research_truth())
    except Exception as exc:
        return {"loaded": False, "error": str(exc)}


def _throughput_status(state: dict[str, Any]) -> str:
    if state["active_open_count"] > 0:
        return "SETTLEMENT_BACKLOG"
    if state["new_clean_rows"] == 0:
        return "PROOF_OPS_STALE"
    if state["raw_rows_newer_than_baseline"] == 0:
        return "CANDIDATE_PIPELINE_STALE"
    if state["contamination_new_ratio"] is not None and state["contamination_new_ratio"] > 0.50:
        return "CONTAMINATION_BLOCKING_PROOF"
    return "PROOF_OPS_HEALTHY"


def _overall_status(state: dict[str, Any], dashboard: dict[str, Any], funnel_state: dict[str, Any]) -> str:
    if state["active_open_count"] > 0:
        return "SETTLEMENT_BACKLOG"
    if state["new_clean_rows"] == 0:
        return "PROOF_OPS_STALE"
    if state["raw_rows_newer_than_baseline"] == 0 or funnel_state["recent_rows"] == 0:
        return "CANDIDATE_PIPELINE_STALE"
    if state["contamination_new_ratio"] is not None and state["contamination_new_ratio"] > 0.50:
        return "CONTAMINATION_BLOCKING_PROOF"
    if not dashboard.get("dashboard_running") or not dashboard.get("auto_settle_running"):
        return "DASHBOARD_TRUTH_RISK"
    return "PROOF_OPS_HEALTHY"


def build_hardening_state(
    records: list[dict[str, Any]],
    *,
    baseline_snapshot: dict[str, Any] | None = None,
    funnel_rows: list[dict[str, Any]] | None = None,
    dashboard_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    baseline_snapshot = dict(delta.BASELINE_SNAPSHOT if baseline_snapshot is None else baseline_snapshot)
    intake_state = intake.build_intake_state(
        records,
        baseline_snapshot=baseline_snapshot,
        baseline_commit=intake.BASELINE_COMMIT,
    )
    baseline_ts = _parse_ts(baseline_snapshot.get("last_timestamp"))
    funnel_rows = _load_funnel_rows() if funnel_rows is None else list(funnel_rows)
    dashboard_state = _dashboard_truth_state() if dashboard_state is None else dict(dashboard_state)
    research_truth = _research_truth_state()
    funnel_state = _funnel_state(funnel_rows, baseline_ts)

    new_clean_rows = list(intake_state["new_clean_rows"])
    post_baseline_rows = [
        rec for rec in records
        if (ts := _parse_ts(rec.get("timestamp"))) is not None and baseline_ts is not None and ts > baseline_ts
    ]
    post_baseline_exclusions = _counts_by_reason(records, after_ts=baseline_ts)
    recent_clean_ts = intake_state["newest_clean_ts"] or _parse_ts(baseline_snapshot.get("last_timestamp"))
    recent_clean_age_hours = intake_state["newest_clean_age_hours"]
    latest_raw_ts = max(
        [ts for ts in (_parse_ts(r.get("timestamp")) for r in records) if ts is not None],
        default=None,
    )

    return {
        "baseline_snapshot": baseline_snapshot,
        "intake_state": intake_state,
        "funnel_state": funnel_state,
        "dashboard_state": dashboard_state,
        "research_truth": research_truth,
        "overall_status": _overall_status(intake_state, dashboard_state, funnel_state),
        "throughput_status": _throughput_status(intake_state),
        "proof_ops_status": "PROOF_OPS_HEALTHY" if len(new_clean_rows) > 0 else "PROOF_OPS_STALE",
        "candidate_pipeline_status": "CANDIDATE_PIPELINE_STALE" if funnel_state["recent_rows"] == 0 and intake_state["raw_rows_newer_than_baseline"] == 0 else "ACTIVE",
        "dashboard_truth_risk": (not dashboard_state.get("dashboard_running", False)) or (not dashboard_state.get("auto_settle_running", False)),
        "raw_total": len(records),
        "latest_raw_ts": latest_raw_ts.isoformat() if latest_raw_ts else None,
        "latest_clean_ts": intake_state["newest_clean_ts"].isoformat() if intake_state["newest_clean_ts"] else None,
        "newest_clean_age_hours": recent_clean_age_hours,
        "post_baseline_rows": len(post_baseline_rows),
        "post_baseline_exclusions": post_baseline_exclusions,
        "clean_rows": len(intake_state["clean_rows"]),
        "new_clean_rows": len(new_clean_rows),
        "new_clean_summary": intake_state["new_clean_summary"],
    }


def _print_section(title: str) -> None:
    print()
    print(title)
    print("-" * 90)


def _print_evidence(state: dict[str, Any]) -> None:
    intake_state = state["intake_state"]
    base = state["baseline_snapshot"]
    print(f"  baseline cohort hash:          {base['cohort_hash']}")
    print(f"  current cohort hash:           {intake_state['current_snapshot']['cohort_hash']}")
    print(f"  hash changed:                  {'YES' if intake_state['current_snapshot']['cohort_hash'] != base['cohort_hash'] else 'NO'}")
    print(f"  raw total rows:                {state['raw_total']}")
    print(f"  clean rows:                    {state['clean_rows']}")
    print(f"  new clean rows since baseline: {state['new_clean_rows']}")
    print(f"  rows after baseline:           {state['post_baseline_rows']}")
    print(f"  latest raw row:                {state['latest_raw_ts']}")
    print(f"  latest clean row:              {state['latest_clean_ts']}")
    print(f"  newest clean age (hours):      {_fmt_num(state['newest_clean_age_hours'], 1)}")
    print(f"  proof_ops_status:              {state['proof_ops_status']}")
    print(f"  throughput_status:             {state['throughput_status']}")
    print(f"  candidate_pipeline_status:      {state['candidate_pipeline_status']}")
    print(f"  overall_status:                {state['overall_status']}")


def _print_settlement(state: dict[str, Any]) -> None:
    intake_state = state["intake_state"]
    truth = state["research_truth"]
    print(f"  open rows:                     {intake_state['open_rows']}")
    print(f"  active open rows:              {intake_state['active_open_count']}")
    print(f"  stale open rows:               {intake_state['stale_open_count']}")
    print(f"  latest open row:               {intake_state['latest_open_ts']}")
    print(f"  latest settled row:            {intake_state['latest_settled_ts']}")
    print(f"  time exit excluded:            {truth.get('time_exit_excluded_count', 'MISSING')}")
    print(f"  recent funnel rows:            {state['funnel_state']['recent_rows']}")
    print(f"  auto_settle_running:           {state['dashboard_state'].get('auto_settle_running', False)}")
    print(f"  dashboard_running:             {state['dashboard_state'].get('dashboard_running', False)}")


def _print_field_completeness(state: dict[str, Any]) -> None:
    intake_state = state["intake_state"]
    print(f"  missing model_probability rows: {intake_state['missing_model_probability_rows']}")
    print(f"  missing entry_price rows:       {intake_state['missing_entry_price_rows']}")
    print(f"  missing economic_pnl rows:      {intake_state['missing_economic_pnl_rows']}")
    print(f"  missing outcome rows:           {intake_state['missing_outcome_rows']}")
    print(f"  missing accounting_version rows:{intake_state['legacy_rows'] + intake_state['unknown_rows']}")
    print(f"  legacy/unversioned rows:        {intake_state['legacy_rows']}")
    print(f"  unknown accounting rows:        {intake_state['unknown_rows']}")


def _print_contamination(state: dict[str, Any]) -> None:
    intake_state = state["intake_state"]
    exclusions = state["post_baseline_exclusions"]
    print(f"  KXETH/quarantined rows:         {intake_state['kxeth_rows']}")
    print(f"  data_collection_override rows:  {intake_state['data_collection_override_rows']}")
    print(f"  bootstrap_provisional rows:     {intake_state['bootstrap_provisional_rows']}")
    print(f"  side-coverage rows:             {intake_state['side_coverage_rows']}")
    print(f"  contaminated after baseline:    {sum(exclusions.values())}")
    print(f"  post-baseline legacy rows:      {exclusions['legacy_or_unversioned']}")
    print(f"  post-baseline KXETH rows:       {exclusions['kxeth']}")
    print(f"  post-baseline dc_override rows: {exclusions['data_collection_override']}")
    print(f"  post-baseline bootstrap rows:   {exclusions['bootstrap_provisional']}")
    print(f"  post-baseline side-coverage:    {exclusions['side_coverage']}")


def _print_funnel(state: dict[str, Any]) -> None:
    funnel = state["funnel_state"]
    print(f"  funnel rows:                   {funnel['total_rows']}")
    print(f"  funnel rows after baseline:    {funnel['recent_rows']}")
    print(f"  paper_trade_opened:            {funnel['paper_trade_opened']}")
    print(f"  passed_to_paper_trader:        {funnel['passed_to_paper_trader']}")
    print(f"  blocked_max_open_trades:       {funnel['blocked_max_open']}")
    print(f"  dashboard_seen:                {funnel['dashboard_seen']}")
    print(f"  latest funnel row:             {funnel['latest_timestamp']}")
    print(f"  final_reasons:                 {dict(sorted(funnel['final_reasons'].items(), key=lambda kv: (-kv[1], kv[0])))}")


def _print_dashboard_truth(state: dict[str, Any]) -> None:
    dash = state["dashboard_state"]
    truth = state["research_truth"]
    print(f"  dashboard_running:             {dash.get('dashboard_running', False)}")
    print(f"  auto_settle_running:           {dash.get('auto_settle_running', False)}")
    if dash.get("heartbeat_age_seconds") is not None:
        print(f"  heartbeat_age_seconds:         {dash['heartbeat_age_seconds']:.0f}")
    print(f"  proof_verdict:                 {truth.get('proof_verdict', 'MISSING')}")
    print(f"  dashboard_truth_risk:          {state['dashboard_truth_risk']}")


def _print_refusal_rules() -> None:
    print()
    print("OPERATIONAL REFUSAL RULES")
    print("-" * 90)
    print("  - refuse strategic conclusions when new clean rows = 0.")
    print("  - refuse promotion claims when the clean cohort hash is unchanged.")
    print("  - refuse live patching while the dashboard or auto-settle loop is down.")
    print("  - refuse live patching when active open rows exist or settlement backlog is present.")
    print("  - refuse live patching when recent raw rows are contaminated or incomplete.")
    print("  - refuse live patching when proof fields are missing on fresh rows.")
    print("  - refuse scaling, Kelly, real money, cap increases, threshold weakening, or KXETH quarantine removal.")


def render_report(state: dict[str, Any]) -> None:
    print("=" * 90)
    print("PROOF OPERATIONS HARDENING / EVIDENCE PRODUCTION + SETTLEMENT THROUGHPUT")
    print("=" * 90)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print(f"Source: {TRADES_LOG}")
    print(f"Funnel: {FUNNEL_LOG}")
    print(f"Baseline phase: PHASE_9Y_RESEARCH_QUARANTINE_REGISTRY + PHASE_9Z_NEW_CLEAN_EVIDENCE_INTAKE")
    print(f"Baseline cohort hash: {state['baseline_snapshot']['cohort_hash']}")
    print(f"Baseline clean rows:  {BASELINE_CLEAN_ROWS}")
    print(f"Baseline last ts:     {BASELINE_LAST_TIMESTAMP}")

    _print_section("EVIDENCE PRODUCTION")
    _print_evidence(state)

    _print_section("SETTLEMENT THROUGHPUT")
    _print_settlement(state)

    _print_section("FIELD COMPLETENESS")
    _print_field_completeness(state)

    _print_section("CONTAMINATION")
    _print_contamination(state)

    _print_section("CANDIDATE / EXECUTION FUNNEL")
    _print_funnel(state)

    _print_section("DASHBOARD TRUTH")
    _print_dashboard_truth(state)
    print("  NO_NEW_CLEAN_EVIDENCE is still the correct conclusion until new clean settled rows appear.")

    _print_refusal_rules()

    print()
    print("REPAIR PRIORITIES")
    print("-" * 90)
    print("  1. Restart / re-enable the dashboard and auto-settle loop if they are expected to be running.")
    print("  2. Produce new clean settled rows after the Phase 9Z baseline.")
    print("  3. Reduce ghost OPEN log pollution so operational state is easier to audit.")
    print("  4. Keep KXETH quarantine and proof gates intact.")
    print("  5. Do not discuss live strategy until clean evidence actually changes.")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> None:
    records = load_trades()
    state = build_hardening_state(records)
    render_report(state)


if __name__ == "__main__":
    main()
