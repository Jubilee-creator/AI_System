#!/usr/bin/env python3
"""
tools/report_scanner_side_audit.py
----------------------------------
Read-only audit of scanner-side action distribution before PaperTrader.

This report separates scanner opportunities from executed paper trades so the
system can prove whether BET_NO disappears before or inside PaperTrader.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import load_trades  # noqa: E402


SCANNER_LOG = ROOT / "logs" / "scanner_opportunities.jsonl"
UNKNOWN_RUN = "UNKNOWN_RUN"
SOURCE_FILES = {
    "market_scanner": ROOT / "brain" / "market_scanner.py",
    "decision_engine": ROOT / "engine" / "decision_engine.py",
    "dashboard": ROOT / "Dashboard.py",
    "paper_trader": ROOT / "brain" / "paper_trader.py",
}


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    except OSError:
        return rows
    return rows


def pct(part: int, total: int) -> str:
    if not total:
        return "0.0%"
    return f"{part / total * 100:.1f}%"


def action_value(row: Dict[str, Any], field: str = "scanner_action") -> str:
    return str(row.get(field) or row.get("action") or "UNKNOWN").upper()


def row_run_id(row: Dict[str, Any]) -> str:
    return str(row.get("run_id") or UNKNOWN_RUN)


def row_scan_id(row: Dict[str, Any]) -> str:
    return str(row.get("scan_id") or "UNKNOWN_SCAN")


def counter_by(rows: List[Dict[str, Any]], field: str) -> Counter:
    counts: Counter = Counter()
    for row in rows:
        counts[str(row.get(field) or "UNKNOWN")] += 1
    return counts


def latest_scan_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    keyed_rows = [row for row in rows if row.get("scan_id")]
    if keyed_rows:
        latest_key = (row_run_id(keyed_rows[-1]), row_scan_id(keyed_rows[-1]))
        return [
            row for row in rows
            if (row_run_id(row), row_scan_id(row)) == latest_key
        ]
    latest_ts = rows[-1].get("timestamp_utc")
    return [row for row in rows if row.get("timestamp_utc") == latest_ts]


def print_action_distribution(title: str, rows: List[Dict[str, Any]], field: str = "scanner_action") -> None:
    counts = Counter(action_value(row, field) for row in rows)
    total = sum(counts.values())
    yes = counts.get("BET_YES", 0)
    no = counts.get("BET_NO", 0)
    passed = counts.get("PASS", 0)
    arb = counts.get("ARB", 0)
    other = total - yes - no - passed - arb
    print()
    print(title)
    print("-" * len(title))
    print(
        f"total={total}  "
        f"BET_YES={yes} ({pct(yes, total)})  "
        f"BET_NO={no} ({pct(no, total)})  "
        f"PASS={passed} ({pct(passed, total)})  "
        f"ARB={arb} ({pct(arb, total)})  "
        f"other={other}"
    )
    if total:
        print(f"raw counts: {dict(counts)}")


def inspect_source() -> Dict[str, Any]:
    scanner = read_text(SOURCE_FILES["market_scanner"])
    engine = read_text(SOURCE_FILES["decision_engine"])
    dashboard = read_text(SOURCE_FILES["dashboard"])
    trader = read_text(SOURCE_FILES["paper_trader"])

    return {
        "decision_engine_has_bet_no_branch": 'action="BET_NO"' in engine,
        "decision_engine_computes_no_edge": "no_edge = compute_edge(1.0 - model_prob, signal.price_no)" in engine,
        "scanner_exports_decision_action": '"action": decision.action' in scanner,
        "scanner_appends_pass_opportunities": 'if result["action"] == "PASS"' in scanner
        and "opportunities.append(result)" in scanner,
        "scanner_return_includes_opportunities": "return opportunities" in scanner,
        "continuous_scanner_writes_latest_snapshot": "latest_opportunities.json" in scanner,
        "dashboard_logs_scanner_opportunities": "log_scanner_opportunities(" in dashboard,
        "dashboard_skips_pass_before_paper": 'if opp["action"] == "PASS":' in dashboard
        and "continue" in dashboard,
        "dashboard_passes_intended_action": "intended_action=opp.get(\"action\")" in dashboard,
        "paper_trader_accepts_intended_action": "intended_action: Optional[str] = None" in trader,
        "paper_trader_rederives_side": "if estimated_prob >= 0.5:" in trader
        and 'action = "BET_YES"' in trader
        and 'action = "BET_NO"' in trader,
        "model_probability_uses_yes_prior": "prior = signal.yes_mid if signal.yes_mid is not None else signal.price_yes" in engine,
        "scanner_uses_yes_mid_price_history": "price_hist.append(yes_mid)" in scanner,
        "order_book_imbalance_hardcoded_zero": "order_book_imbalance=0.0" in scanner,
    }


def trace_summary(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    traced = [
        row for row in trades
        if row.get("scanner_action") is not None
        or row.get("intended_action") is not None
        or row.get("executed_action") is not None
    ]
    mismatches = 0
    measurable = 0
    scanner_counts: Counter = Counter()
    executed_counts: Counter = Counter()
    for row in traced:
        scanner_action = row.get("intended_action") or row.get("scanner_action")
        executed_action = row.get("executed_action") or row.get("action")
        if scanner_action:
            scanner_counts[str(scanner_action).upper()] += 1
        if executed_action:
            executed_counts[str(executed_action).upper()] += 1
        if scanner_action and executed_action:
            measurable += 1
            if str(scanner_action).upper() != str(executed_action).upper():
                mismatches += 1
    return {
        "traced_rows": len(traced),
        "measurable_rows": measurable,
        "mismatches": mismatches,
        "scanner_counts": dict(scanner_counts),
        "executed_counts": dict(executed_counts),
    }


def print_family_distribution(rows: List[Dict[str, Any]]) -> None:
    groups: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        family = str(row.get("market_family") or "UNKNOWN")
        groups[family][action_value(row)] += 1
    print()
    print("SCANNER ACTIONS BY MARKET FAMILY")
    print("--------------------------------")
    if not groups:
        print("(no scanner opportunity rows)")
        return
    for family, counts in sorted(groups.items(), key=lambda item: sum(item[1].values()), reverse=True)[:15]:
        total = sum(counts.values())
        print(
            f"{family:<24} n={total:>4}  "
            f"YES={counts.get('BET_YES', 0):>4}  "
            f"NO={counts.get('BET_NO', 0):>4}  "
            f"PASS={counts.get('PASS', 0):>4}  "
            f"ARB={counts.get('ARB', 0):>4}"
        )


def main() -> None:
    scanner_rows = read_jsonl(SCANNER_LOG)
    latest_rows = latest_scan_rows(scanner_rows)
    trades = load_trades()
    source = inspect_source()
    traces = trace_summary(trades)

    print("=" * 86)
    print("AI_SYSTEM SCANNER SIDE AUDIT")
    print("=" * 86)
    print("Read-only report. No execution, signal, risk, sizing, or proof changes.")

    print()
    print("SOURCE PATH CHECK")
    print("-----------------")
    print(f"decision_engine has BET_NO branch:          {source['decision_engine_has_bet_no_branch']}")
    print(f"decision_engine computes NO edge:           {source['decision_engine_computes_no_edge']}")
    print(f"scanner exports decision.action:            {source['scanner_exports_decision_action']}")
    print(f"scanner keeps PASS rows in return list:      {source['scanner_appends_pass_opportunities']}")
    print(f"scanner returns opportunities to caller:     {source['scanner_return_includes_opportunities']}")
    print(f"continuous scanner has latest snapshot:      {source['continuous_scanner_writes_latest_snapshot']}")
    print(f"Dashboard logs scanner opportunities:        {source['dashboard_logs_scanner_opportunities']}")
    print(f"Dashboard skips PASS before PaperTrader:     {source['dashboard_skips_pass_before_paper']}")
    print(f"Dashboard passes intended action:            {source['dashboard_passes_intended_action']}")
    print(f"PaperTrader accepts intended action:         {source['paper_trader_accepts_intended_action']}")
    print(f"PaperTrader re-derives side:                 {source['paper_trader_rederives_side']}")
    print(f"model probability uses YES prior:            {source['model_probability_uses_yes_prior']}")
    print(f"scanner price history uses YES mid:          {source['scanner_uses_yes_mid_price_history']}")
    print(f"order book imbalance hardcoded zero:         {source['order_book_imbalance_hardcoded_zero']}")

    print()
    print("SCANNER OPPORTUNITY LOG STATUS")
    print("------------------------------")
    print(f"path: {SCANNER_LOG}")
    print(f"exists: {SCANNER_LOG.exists()}")
    print(f"total scanner rows: {len(scanner_rows)}")
    if scanner_rows:
        print(f"first row timestamp: {scanner_rows[0].get('timestamp_utc')}")
        print(f"latest row timestamp: {scanner_rows[-1].get('timestamp_utc')}")
        print(f"latest run_id: {row_run_id(scanner_rows[-1])}")
        print(f"latest scan_id: {scanner_rows[-1].get('scan_id')}")
        if any(row_run_id(row) == UNKNOWN_RUN for row in scanner_rows):
            print("legacy rows without run_id: present (reported as UNKNOWN_RUN)")
    else:
        print("current scanner-side distribution cannot be proven yet from historical files")

    print_action_distribution("ALL SCANNER-SIDE ACTIONS", scanner_rows)
    print_action_distribution("LATEST SCANNER-SIDE ACTIONS", latest_rows)
    print_family_distribution(scanner_rows)

    print_action_distribution("EXECUTED PAPER TRADE ACTIONS", trades, field="action")

    print()
    print("HANDOFF TRACE SUMMARY")
    print("---------------------")
    print(f"traced trade rows: {traces['traced_rows']}")
    print(f"measurable intended/executed rows: {traces['measurable_rows']}")
    print(f"measured mismatches: {traces['mismatches']}")
    print(f"trace scanner action counts: {traces['scanner_counts']}")
    print(f"trace executed action counts: {traces['executed_counts']}")

    warnings: List[str] = []
    scanner_counts = Counter(action_value(row) for row in scanner_rows)
    trade_counts = Counter(action_value(row, "action") for row in trades)
    scanner_total_side = scanner_counts.get("BET_YES", 0) + scanner_counts.get("BET_NO", 0)
    trade_total_side = trade_counts.get("BET_YES", 0) + trade_counts.get("BET_NO", 0)

    if not scanner_rows:
        warnings.append("NO_SCANNER_OPPORTUNITY_HISTORY_YET")
    if scanner_rows and scanner_counts.get("BET_NO", 0) == 0:
        warnings.append("SCANNER_BET_NO_ZERO_IN_OBSERVED_ROWS")
    if trade_total_side and trade_counts.get("BET_NO", 0) == 0:
        warnings.append("EXECUTED_BET_NO_ZERO_IN_TRADE_LOG")
    if scanner_total_side and scanner_counts.get("BET_YES", 0) / scanner_total_side > 0.80:
        warnings.append("SCANNER_SIDE_DISTRIBUTION_OVER_80_PERCENT_YES")
    if source["paper_trader_rederives_side"]:
        warnings.append("PAPER_TRADER_STILL_REDERIVES_SIDE")
    if source["model_probability_uses_yes_prior"] and source["scanner_uses_yes_mid_price_history"]:
        warnings.append("YES_MID_USED_AS_BOTH_PRIOR_AND_SIGNAL_HISTORY")
    if source["order_book_imbalance_hardcoded_zero"]:
        warnings.append("ORDER_BOOK_IMBALANCE_NOT_WIRED")

    print()
    print("WARNINGS")
    print("--------")
    if warnings:
        for warning in warnings:
            print(f"[!] {warning}")
    else:
        print("(none)")

    print()
    print("BOTTOM LINE")
    print("-----------")
    if scanner_rows:
        print("Scanner-side action distribution is now observable from logs/scanner_opportunities.jsonl.")
        if scanner_counts.get("BET_NO", 0) == 0:
            print("Observed scanner rows have not produced BET_NO yet.")
        else:
            print("Observed scanner rows include BET_NO; compare this against executed trade actions.")
    else:
        print("Scanner-side action distribution is not proven from old data.")
        print("Future Dashboard scan cycles will write scanner opportunities before PaperTrader.")
    print("This report does not prove edge or profitability.")


if __name__ == "__main__":
    main()
