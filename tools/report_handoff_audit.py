#!/usr/bin/env python3
"""
tools/report_handoff_audit.py
-----------------------------
Read-only scanner-vs-paper handoff audit.

Checks whether the intended scanner action survives into paper-trader execution.
This tool does not trade, mutate logs, or change strategy/risk/sizing behavior.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import load_trades  # noqa: E402


FILES = {
    "market_scanner": ROOT / "brain" / "market_scanner.py",
    "dashboard": ROOT / "Dashboard.py",
    "paper_trader": ROOT / "brain" / "paper_trader.py",
    "decision_engine": ROOT / "engine" / "decision_engine.py",
}


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _slice(text: str, start_token: str, end_token: str) -> str:
    start = text.find(start_token)
    if start < 0:
        return ""
    end = text.find(end_token, start)
    return text[start:] if end < 0 else text[start:end]


def inspect_source() -> dict[str, Any]:
    scanner = _read(FILES["market_scanner"])
    dashboard = _read(FILES["dashboard"])
    trader = _read(FILES["paper_trader"])
    engine = _read(FILES["decision_engine"])

    dashboard_handoff = _slice(
        dashboard,
        "estimated_prob = opp.get(\"confidence\", 0.5)",
        "trace_counts = classify_execution_trace",
    )
    trader_signature = _slice(
        trader,
        "def process_signal(",
        ") -> Optional[Dict[str, Any]]:",
    )

    return {
        "decision_engine_has_bet_no_branch": 'action="BET_NO"' in engine,
        "decision_engine_computes_no_edge": "no_edge = compute_edge(1.0 - model_prob, signal.price_no)" in engine,
        "scanner_creates_decision_action": '"action": decision.action' in scanner,
        "dashboard_reads_opportunity_action": 'opp["action"]' in dashboard or 'opp.get("action")' in dashboard,
        "dashboard_passes_action_to_paper_trader": (
            "opp.get(\"action\")" in dashboard_handoff
            or "opp[\"action\"]" in dashboard_handoff
            or "intended_action" in dashboard_handoff
        ),
        "dashboard_passes_confidence_only": "estimated_prob = opp.get(\"confidence\", 0.5)" in dashboard_handoff,
        "paper_trader_accepts_intended_action": (
            "intended_action" in trader_signature
            or "action:" in trader_signature
            or "side:" in trader_signature
        ),
        "paper_trader_rederives_side": (
            "if estimated_prob >= 0.5:" in trader
            and 'action = "BET_YES"' in trader
            and 'action = "BET_NO"' in trader
        ),
        "paper_trader_flips_no_probability": "estimated_prob = 1.0 - estimated_prob" in trader,
    }


def inspect_logs(records: list[dict]) -> dict[str, Any]:
    fields = Counter()
    action_counts = Counter()
    status_counts = Counter()
    for row in records:
        for field in (
            "scanner_action",
            "intended_action",
            "executed_action",
            "paper_action",
            "action",
            "scanner_confidence",
            "scanner_edge",
        ):
            if row.get(field) is not None:
                fields[field] += 1
        action = str(row.get("action") or "UNKNOWN").upper()
        action_counts[action] += 1
        status_counts[str(row.get("status") or "UNKNOWN").upper()] += 1

    measurable = (
        fields["intended_action"] > 0
        and (fields["executed_action"] > 0 or fields["action"] > 0)
    ) or (
        fields["scanner_action"] > 0
        and (fields["executed_action"] > 0 or fields["action"] > 0)
    )

    mismatch_count = None
    if measurable:
        mismatch_count = 0
        for row in records:
            intended = row.get("intended_action") or row.get("scanner_action")
            executed = row.get("executed_action") or row.get("paper_action") or row.get("action")
            if intended and executed and str(intended).upper() != str(executed).upper():
                mismatch_count += 1

    return {
        "field_counts": dict(fields),
        "action_counts": dict(action_counts),
        "status_counts": dict(status_counts),
        "mismatch_measurable": measurable,
        "mismatch_count": mismatch_count,
    }


def yn(value: Any) -> str:
    return "YES" if value else "NO"


def main() -> None:
    records = load_trades()
    source = inspect_source()
    logs = inspect_logs(records)
    action_counts = Counter(logs["action_counts"])
    yes = action_counts.get("BET_YES", 0)
    no = action_counts.get("BET_NO", 0)
    total_actions = yes + no + action_counts.get("ARB", 0)

    print("=" * 86)
    print("AI_SYSTEM HANDOFF AUDIT")
    print("=" * 86)
    print("Read-only report. No execution, signal, risk, sizing, or log changes.")

    print()
    print("SOURCE HANDOFF CHECK")
    print("--------------------")
    print(f"decision_engine has BET_NO branch:      {yn(source['decision_engine_has_bet_no_branch'])}")
    print(f"decision_engine computes NO edge:       {yn(source['decision_engine_computes_no_edge'])}")
    print(f"scanner exports decision.action:        {yn(source['scanner_creates_decision_action'])}")
    print(f"Dashboard reads opportunity action:      {yn(source['dashboard_reads_opportunity_action'])}")
    print(f"Dashboard passes action to PaperTrader:  {yn(source['dashboard_passes_action_to_paper_trader'])}")
    print(f"Dashboard passes confidence only:        {yn(source['dashboard_passes_confidence_only'])}")
    print(f"PaperTrader accepts intended action:     {yn(source['paper_trader_accepts_intended_action'])}")
    print(f"PaperTrader re-derives side:             {yn(source['paper_trader_rederives_side'])}")
    print(f"PaperTrader flips NO probability:        {yn(source['paper_trader_flips_no_probability'])}")

    print()
    print("LOG TRACEABILITY CHECK")
    print("----------------------")
    fields = logs["field_counts"]
    print(f"log rows:                              {len(records)}")
    print(f"rows with scanner_action:              {fields.get('scanner_action', 0)}")
    print(f"rows with intended_action:             {fields.get('intended_action', 0)}")
    print(f"rows with executed_action:             {fields.get('executed_action', 0)}")
    print(f"rows with action:                      {fields.get('action', 0)}")
    print(f"mismatch measurable from current logs: {yn(logs['mismatch_measurable'])}")
    print(f"measured mismatch count:               {logs['mismatch_count'] if logs['mismatch_count'] is not None else 'n/a'}")

    print()
    print("CURRENT ACTION DISTRIBUTION")
    print("---------------------------")
    yes_pct = yes / total_actions * 100 if total_actions else 0.0
    no_pct = no / total_actions * 100 if total_actions else 0.0
    print(f"BET_YES={yes} ({yes_pct:.1f}%)  BET_NO={no} ({no_pct:.1f}%)  total_side_actions={total_actions}")
    print(f"all action counts: {dict(action_counts)}")

    warnings = []
    if source["scanner_creates_decision_action"] and not source["dashboard_passes_action_to_paper_trader"]:
        warnings.append("SCANNER_ACTION_NOT_PASSED_TO_PAPER_TRADER")
    if not source["paper_trader_accepts_intended_action"]:
        warnings.append("PAPER_TRADER_HAS_NO_INTENDED_ACTION_PARAMETER")
    if source["paper_trader_rederives_side"]:
        warnings.append("PAPER_TRADER_REDERIVES_SIDE_FROM_CONFIDENCE")
    if not logs["mismatch_measurable"]:
        warnings.append("CURRENT_LOGS_CANNOT_MEASURE_INTENDED_VS_EXECUTED_MISMATCH")
    if no == 0:
        warnings.append("BET_NO_ZERO_IN_CURRENT_LOGS")

    print()
    print("WARNINGS")
    print("--------")
    for warning in warnings:
        print(f"[!] {warning}")

    print()
    print("MINIMAL NEXT PATCH RECOMMENDATION")
    print("---------------------------------")
    print("Add future logging fields only, before changing execution:")
    print("- scanner_action / intended_action from opportunity['action']")
    print("- executed_action from PaperTrader's final action")
    print("- handoff_action_mismatch boolean")
    print("Then run at least one scanner cycle and audit whether mismatches occur live.")

    print()
    print("VERDICT")
    print("-------")
    if warnings:
        print("Handoff is not auditable from current logs and source inspection shows action can be dropped.")
    else:
        print("No handoff issue detected by static/log inspection.")
    print("This report is diagnostic only. It does not prove edge and does not fix execution.")


if __name__ == "__main__":
    main()
