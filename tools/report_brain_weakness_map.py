#!/usr/bin/env python3
"""
Phase 10I - Brain Weakness Map
Sentinel: BRAIN_WEAKNESS_MAP_OK

Daily read-only scorecard for the system's major "brains".  This report
aggregates existing proof, payoff, ordering, shadow, and safety evidence.  It
does not modify logs, thresholds, scanner ordering, PaperTrader, risk state, or
live-money settings.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (  # noqa: E402
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    QUARANTINED_TICKER_PREFIXES,
    TRADING_MODE,
)
from tools import report_candidate_priority_ordering_audit as priority_report  # noqa: E402
from tools import report_payoff_aware_shadow_forward_validation as shadow_report  # noqa: E402
from tools import report_payoff_geometry_candidate_quality_autopsy as payoff_report  # noqa: E402
from tools.report_payoff_geometry_candidate_quality_autopsy import (  # noqa: E402
    FUNNEL_LOG,
    SCANNER_LOG,
    SCANNER_TAIL_BYTES,
    TRADES_LOG,
)

SENTINEL = "BRAIN_WEAKNESS_MAP_OK"
MATURE_SHADOW_ROWS = 30


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _rate(count: int | None, total: int | None) -> float | None:
    if count is None or total is None or total <= 0:
        return None
    return count / total


def _age_minutes(timestamp: Any) -> float | None:
    if not timestamp:
        return None
    try:
        text = str(timestamp)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60.0


def _brain(
    name: str,
    status: str,
    evidence: list[str],
    main_weakness: str,
    improvement: str,
    next_action: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "evidence": evidence,
        "main_weakness": main_weakness,
        "what_would_improve_it": improvement,
        "live_patch_allowed": False,
        "next_safest_action": next_action,
    }


def _score_scanner(payoff: dict[str, Any]) -> dict[str, Any]:
    counts = payoff["counts"]
    summary = payoff["candidate_summary"]
    candidate_rows = counts["candidate_rows"]
    expensive_rate = _rate(summary["expensive_entry"], candidate_rows)
    weak_rr_rate = _rate(summary["weak_reward_risk"], candidate_rows)
    below_be_rate = _rate(summary["model_below_breakeven"], candidate_rows)
    status = "IMMATURE" if candidate_rows == 0 else "WEAK"
    if candidate_rows and (expensive_rate or 0.0) > 0.60 and (below_be_rate or 0.0) > 0.20:
        status = "DANGEROUS"
    return _brain(
        "Scanner brain",
        status,
        [
            f"candidate_rows={_fmt_int(candidate_rows)}",
            f"avg_entry={_fmt_num(summary['avg_entry'])}",
            f"expensive_entry_rate={_fmt_pct(expensive_rate)}",
            f"weak_reward_risk_rate={_fmt_pct(weak_rr_rate)}",
            f"model_below_breakeven_rate={_fmt_pct(below_be_rate)}",
            f"verdict={payoff['verdict']['scanner_quality']}",
        ],
        "Scanner produces too many economically fragile candidates before proof filters.",
        "Add more read-only candidate diagnostics by source/family/time bucket; do not loosen filters.",
        "Keep collecting paper evidence and use shadow reports to identify candidate-generation failure pockets.",
    )


def _score_probability(payoff: dict[str, Any]) -> dict[str, Any]:
    settled = payoff["settled_summary"]
    roi = settled.get("roi")
    wr_margin = settled.get("wr_margin")
    pf = settled.get("profit_factor")
    status = "IMMATURE" if settled.get("n", 0) < 30 else "WEAK"
    if roi is not None and roi < 0 and wr_margin is not None and wr_margin < 0:
        status = "WEAK"
    if roi is not None and roi < -0.20 and pf is not None and pf < 0.50:
        status = "DANGEROUS"
    return _brain(
        "Probability / confidence brain",
        status,
        [
            f"clean_settled={_fmt_int(settled.get('n'))}",
            f"realized_wr={_fmt_pct(settled.get('win_rate'))}",
            f"breakeven_wr={_fmt_pct(settled.get('breakeven_wr'))}",
            f"wr_margin={_fmt_pct(wr_margin)}",
            f"roi={_fmt_pct(roi)}",
            f"profit_factor={_fmt_num(pf)}",
            f"model_verdict={payoff['verdict']['model']}",
        ],
        "Predicted confidence is not clearing the breakeven win-rate requirement.",
        "Calibrate probability against entry price, side, market family, and short-expiry regime.",
        "Run calibration/payoff reports daily; treat confidence as suspect until CLV and ROI turn positive.",
    )


def _score_selection(priority: dict[str, Any]) -> dict[str, Any]:
    ordering = priority["ordering"]
    status = "WEAK" if priority["verdict"]["ranking_system_flawed"] else "OK"
    return _brain(
        "Selection / ranking brain",
        status,
        [
            f"ranking_logic={priority['source_inspection']['ranking_logic']}",
            f"payoff_fields_in_current_sort={priority['source_inspection']['payoff_fields_in_current_sort']}",
            f"payoff_alternative_rate={_fmt_pct(ordering.get('payoff_alternative_rate'))}",
            f"actual_top_expensive_yes_rate={_fmt_pct(ordering.get('actual_top_expensive_yes_rate'))}",
            f"best_payoff_after_slots_rate={_fmt_pct(ordering.get('best_payoff_after_slots_rate'))}",
            f"verdict={priority['verdict']['exact_weakness']}",
        ],
        "Current order is payoff-blind: edge ranks ahead of breakeven, entry price, and reward/risk.",
        "Forward-validate payoff-aware shadow picks until settled outcome evidence is mature.",
        "Do not patch ranking live; collect shadow rows and compare settled matched performance.",
    )


def _score_payoff_geometry(payoff: dict[str, Any]) -> dict[str, Any]:
    settled = payoff["settled_summary"]
    avg_rr = settled.get("avg_reward_risk")
    status = "DANGEROUS" if (settled.get("roi") or 0.0) < 0 and (avg_rr or 1.0) < 0.30 else "WEAK"
    return _brain(
        "Payoff geometry brain",
        status,
        [
            f"avg_clean_reward_risk={_fmt_num(avg_rr)}",
            f"avg_win={_fmt_num(settled.get('avg_win'))}",
            f"avg_loss={_fmt_num(settled.get('avg_loss'))}",
            f"breakeven_wr={_fmt_pct(settled.get('breakeven_wr'))}",
            f"realized_wr={_fmt_pct(settled.get('win_rate'))}",
            f"failure={payoff['verdict']['current_failure']}",
        ],
        "Payoff asymmetry is negative enough that a high win rate still loses money.",
        "Keep pressure on entry price, reward/risk, and model-margin diagnostics before any strategy patch.",
        "Prioritize proof of candidate pockets with reward/risk above 0.50 and adequate sample size.",
    )


def _score_council(payoff: dict[str, Any]) -> dict[str, Any]:
    groups = dict(payoff["candidate_groups"]["final_reason"])
    council = groups.get("BLOCKED_COUNCIL", {})
    opened = groups.get("TRADE_OPENED", {})
    status = "OK"
    if opened and (opened.get("avg_reward_risk") or 1.0) < 0.40:
        status = "WEAK"
    return _brain(
        "Council / critic brain",
        status,
        [
            f"council_blocked={_fmt_int(council.get('n', 0))}",
            f"opened_candidates={_fmt_int(opened.get('n', 0))}",
            f"opened_avg_entry={_fmt_num(opened.get('avg_entry'))}",
            f"opened_avg_reward_risk={_fmt_num(opened.get('avg_reward_risk'))}",
            f"opened_bad_geometry={_fmt_int(opened.get('model_edge_bad_geometry', 0))}",
        ],
        "Council blocks some bad rows but still allows weak payoff geometry through downstream gates.",
        "Audit council decisions by payoff bucket; do not make it more permissive.",
        "Use read-only council outcome stratification before changing any rule.",
    )


def _score_risk(payoff: dict[str, Any]) -> dict[str, Any]:
    safety = payoff["safety"]
    status = "STRONG" if all([
        safety["paper_only"],
        safety["real_money_allowed"] is False,
        safety["scale_allowed"] is False,
        safety["kelly_execution_disabled"],
        safety["kxeth_quarantine_active"],
    ]) else "DANGEROUS"
    return _brain(
        "Risk brain",
        status,
        [
            f"trading_mode={safety['trading_mode']}",
            f"real_money_allowed={safety['real_money_allowed']}",
            f"scale_allowed={safety['scale_allowed']}",
            f"kelly_execution_disabled={safety['kelly_execution_disabled']}",
            f"dc_override_enabled={safety['dc_override_enabled']}",
            f"kxeth_quarantine_active={safety['kxeth_quarantine_active']}",
        ],
        "Risk is doing its job; the remaining disease is economic quality, not missing risk locks.",
        "Keep locks unchanged while improving proof quality and candidate selection evidence.",
        "Verify lockdown reports after every change.",
    )


def _score_execution(payoff: dict[str, Any]) -> dict[str, Any]:
    groups = dict(payoff["candidate_groups"]["final_reason"])
    opened = groups.get("TRADE_OPENED", {}).get("n", 0)
    status = "OK" if opened else "IMMATURE"
    return _brain(
        "Execution / settlement brain",
        status,
        [
            f"paper_trade_opened_candidates={_fmt_int(opened)}",
            f"fresh_clean_settled={_fmt_int(payoff['counts']['fresh_clean_settled'])}",
            f"paper_trades_loaded={_fmt_int(payoff['counts']['trades'])}",
            f"candidate_source={payoff['candidate_source']}",
        ],
        "Execution path appears functional, but historical ghost OPEN rows still pollute raw logs.",
        "Keep using active-open reconciliation; only clean historical ghost rows with a separately justified audit.",
        "Let paper execution continue while monitoring bridge and settlement reports.",
    )


def _score_shadow(shadow: dict[str, Any]) -> dict[str, Any]:
    rows = shadow["rows_logged"]
    mature = shadow["evidence_maturity"]["mature_shadow_rows"] and shadow["evidence_maturity"]["mature_outcomes"]
    status = "OK" if mature else "IMMATURE"
    return _brain(
        "Shadow learning brain",
        status,
        [
            f"rows_logged={_fmt_int(rows)}",
            f"shadow_logging_active={shadow['shadow_logging_active']}",
            f"shadow_only_ok={shadow['shadow_only_ok']}",
            f"execution_changed={shadow['execution_changed']}",
            f"mature_shadow_rows={shadow['evidence_maturity']['mature_shadow_rows']}",
            f"mature_outcomes={shadow['evidence_maturity']['mature_outcomes']}",
        ],
        "Shadow logger is safe but not mature enough to justify live ranking changes.",
        "Collect at least 30 shadow scans and settled matched outcomes before acting.",
        "Keep Dashboard running and rerun shadow validation after settlements mature.",
    )


def _score_dashboard_ops(shadow: dict[str, Any]) -> dict[str, Any]:
    age = _age_minutes(shadow.get("latest_timestamp"))
    if not shadow["shadow_logging_active"]:
        status = "WEAK"
    elif age is not None and age <= 30:
        status = "OK"
    else:
        status = "WEAK"
    return _brain(
        "Dashboard / operations brain",
        status,
        [
            f"shadow_logging_active={shadow['shadow_logging_active']}",
            f"latest_shadow_timestamp={shadow.get('latest_timestamp') or 'MISSING'}",
            f"latest_shadow_age_minutes={_fmt_num(age, 1)}",
            "process_liveness=use tools/report_health.py as authority",
        ],
        "Operational liveness can be misleading if Dashboard/settle process checks diverge from fresh heartbeats.",
        "Keep Dashboard and auto-settle visibly supervised; improve liveness diagnostics separately.",
        "Run health, shadow, and bridge reports before trusting any daily state.",
    )


def _score_data_proof(payoff: dict[str, Any]) -> dict[str, Any]:
    settled = payoff["settled_summary"]
    status = "WEAK"
    if settled.get("n", 0) < 30:
        status = "IMMATURE"
    if (settled.get("roi") or 0.0) > 0 and (settled.get("profit_factor") or 0.0) > 1.10:
        status = "OK"
    return _brain(
        "Data/proof brain",
        status,
        [
            f"clean_settled={_fmt_int(settled.get('n'))}",
            f"roi={_fmt_pct(settled.get('roi'))}",
            f"profit_factor={_fmt_num(settled.get('profit_factor'))}",
            f"fresh_clean_rows={_fmt_int(payoff['counts']['fresh_clean_settled'])}",
            f"sample_supported_positive_pockets={len(payoff['pockets']['sample_supported_positive'])}",
            f"tiny_positive_traps={len(payoff['pockets']['tiny_positive_traps'])}",
        ],
        "There is enough evidence to reject current profitability, not enough to promote a new edge.",
        "Use normal_modern clean cohorts only; avoid celebrating tiny positive pockets.",
        "Keep proof gates strict and require ROI, CLV, and PF improvement before live changes.",
    )


def build_report(
    trades_path: Path = TRADES_LOG,
    funnel_path: Path = FUNNEL_LOG,
    scanner_path: Path = SCANNER_LOG,
    shadow_path: Path = shadow_report.SHADOW_LOG,
) -> dict[str, Any]:
    payoff = payoff_report.build_report(trades_path, funnel_path, scanner_path, SCANNER_TAIL_BYTES)
    priority = priority_report.build_report(trades_path, funnel_path, scanner_path, SCANNER_TAIL_BYTES)
    shadow = shadow_report.build_report(shadow_path, trades_path)

    brains = [
        _score_scanner(payoff),
        _score_probability(payoff),
        _score_selection(priority),
        _score_payoff_geometry(payoff),
        _score_council(payoff),
        _score_risk(payoff),
        _score_execution(payoff),
        _score_shadow(shadow),
        _score_dashboard_ops(shadow),
        _score_data_proof(payoff),
    ]

    severity = {"DANGEROUS": 5, "WEAK": 4, "IMMATURE": 3, "OK": 2, "STRONG": 1}
    weakest = max(brains, key=lambda row: severity.get(row["status"], 0))
    strongest = min(brains, key=lambda row: severity.get(row["status"], 9))

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "counts": {
            "trades": payoff["counts"]["trades"],
            "candidate_rows": payoff["counts"]["candidate_rows"],
            "fresh_clean_settled": payoff["counts"]["fresh_clean_settled"],
            "shadow_rows": shadow["rows_logged"],
        },
        "brains": brains,
        "weakest_brain": weakest["name"],
        "strongest_brain": strongest["name"],
        "exact_next_safest_improvement": (
            "Keep live strategy frozen, collect mature payoff-aware shadow outcomes, "
            "and build calibration/payoff diagnostics by side, entry bucket, and market family."
        ),
        "safety": {
            "trading_mode": TRADING_MODE,
            "paper_only": TRADING_MODE == "PAPER",
            "real_money_allowed": False,
            "scale_allowed": False,
            "kelly_execution_disabled": GLOBAL_FORCED_LEARNING_MODE,
            "dc_override_enabled": DATA_COLLECTION_OVERRIDE_ENABLED,
            "kxeth_quarantine_active": "KXETH" in {str(prefix).upper() for prefix in QUARANTINED_TICKER_PREFIXES},
        },
    }


def print_report(report: dict[str, Any]) -> None:
    print("=" * 96)
    print("BRAIN WEAKNESS MAP")
    print("=" * 96)
    print("Read-only: no strategy, thresholds, scanner order, PaperTrader, logs, risk, or live-money state are modified.")
    print(f"as_of:                 {report['as_of']}")
    print(f"trades loaded:         {_fmt_int(report['counts']['trades'])}")
    print(f"candidate rows:        {_fmt_int(report['counts']['candidate_rows'])}")
    print(f"fresh clean settled:   {_fmt_int(report['counts']['fresh_clean_settled'])}")
    print(f"shadow rows:           {_fmt_int(report['counts']['shadow_rows'])}")
    print()

    print("BRAIN SCORES")
    print("-" * 96)
    for item in report["brains"]:
        print(f"{item['name']}: {item['status']}")
        print(f"  main weakness: {item['main_weakness']}")
        print(f"  improve by:    {item['what_would_improve_it']}")
        print(f"  next action:   {item['next_safest_action']}")
        print(f"  live patch:    {item['live_patch_allowed']}")
        for line in item["evidence"]:
            print(f"  evidence:      {line}")
        print()

    print("SUMMARY")
    print("-" * 96)
    print(f"weakest_brain:              {report['weakest_brain']}")
    print(f"strongest_brain:            {report['strongest_brain']}")
    print(f"exact_next_safest_improvement: {report['exact_next_safest_improvement']}")

    print()
    print("SAFETY LOCKS")
    print("-" * 96)
    for key, value in report["safety"].items():
        print(f"{key:<28} {value}")

    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> int:
    print_report(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
