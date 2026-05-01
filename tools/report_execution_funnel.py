#!/usr/bin/env python3
"""
tools/report_execution_funnel.py
--------------------------------
Read-only BET_NO execution-funnel report.

Uses logs/execution_funnel.jsonl to show what happens to non-PASS scanner
opportunities after Dashboard passes them toward PaperTrader.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import load_trades  # noqa: E402


FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
SCANNER_LOG = ROOT / "logs" / "scanner_opportunities.jsonl"


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
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except OSError:
        return rows
    return rows


def pct(part: int, total: int) -> str:
    if not total:
        return "0.0%"
    return f"{part / total * 100:.1f}%"


def action(row: Dict[str, Any], key: str) -> str:
    return str(row.get(key) or "UNKNOWN").upper()


def latest_scan(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    scan_ids = [row.get("scan_id") for row in rows if row.get("scan_id")]
    if scan_ids:
        latest = scan_ids[-1]
        return [row for row in rows if row.get("scan_id") == latest]
    ts = rows[-1].get("timestamp_utc")
    return [row for row in rows if row.get("timestamp_utc") == ts]


def print_counts(title: str, counts: Counter) -> None:
    print()
    print(title)
    print("-" * len(title))
    if not counts:
        print("(none)")
        return
    total = sum(counts.values())
    for key, value in counts.most_common(20):
        print(f"{key:<36} {value:>6} ({pct(value, total):>6})")


def print_action_summary(title: str, rows: List[Dict[str, Any]], key: str) -> None:
    counts = Counter(action(row, key) for row in rows)
    total = sum(counts.values())
    yes = counts.get("BET_YES", 0)
    no = counts.get("BET_NO", 0)
    passed = counts.get("PASS", 0)
    arb = counts.get("ARB", 0)
    print()
    print(title)
    print("-" * len(title))
    print(
        f"total={total}  "
        f"BET_YES={yes} ({pct(yes, total)})  "
        f"BET_NO={no} ({pct(no, total)})  "
        f"PASS={passed} ({pct(passed, total)})  "
        f"ARB={arb} ({pct(arb, total)})"
    )
    if total:
        print(f"raw counts: {dict(counts)}")


def group_reason_by_action(rows: List[Dict[str, Any]]) -> Dict[str, Counter]:
    grouped: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        grouped[action(row, "scanner_action")][str(row.get("final_reason") or "UNKNOWN")] += 1
    return grouped


def main() -> None:
    funnel_rows = read_jsonl(FUNNEL_LOG)
    scanner_rows = read_jsonl(SCANNER_LOG)
    trades = load_trades()
    latest_funnel = latest_scan(funnel_rows)

    print("=" * 86)
    print("AI_SYSTEM EXECUTION FUNNEL REPORT")
    print("=" * 86)
    print("Read-only report. No execution, signal, risk, sizing, or proof changes.")

    print()
    print("LOG STATUS")
    print("----------")
    print(f"execution funnel log: {FUNNEL_LOG}")
    print(f"exists: {FUNNEL_LOG.exists()}")
    print(f"funnel rows: {len(funnel_rows)}")
    if funnel_rows:
        print(f"first row timestamp: {funnel_rows[0].get('timestamp_utc')}")
        print(f"latest row timestamp: {funnel_rows[-1].get('timestamp_utc')}")
        print(f"latest scan_id: {funnel_rows[-1].get('scan_id')}")
    else:
        print("No funnel rows yet. Restart/run Dashboard for future non-PASS opportunities.")

    print_action_summary("SCANNER-SIDE ACTIONS", scanner_rows, "scanner_action")
    print_action_summary("FUNNEL ROW SCANNER ACTIONS", funnel_rows, "scanner_action")
    print_action_summary("LATEST FUNNEL ROW SCANNER ACTIONS", latest_funnel, "scanner_action")
    print_action_summary("FUNNEL EXECUTED ACTIONS", [r for r in funnel_rows if r.get("executed_action")], "executed_action")
    print_action_summary("PAPER TRADE LOG ACTIONS", trades, "action")

    print_counts("FUNNEL FINAL REASONS", Counter(str(r.get("final_reason") or "UNKNOWN") for r in funnel_rows))
    print_counts("COUNCIL DECISIONS IN FUNNEL", Counter(str(r.get("council_decision") or "UNKNOWN") for r in funnel_rows))

    print()
    print("FINAL REASON BY SCANNER ACTION")
    print("------------------------------")
    grouped = group_reason_by_action(funnel_rows)
    if not grouped:
        print("(none)")
    for scanner_action, counts in sorted(grouped.items()):
        total = sum(counts.values())
        detail = ", ".join(f"{reason}={count}" for reason, count in counts.most_common(8))
        print(f"{scanner_action:<10} n={total:>5}  {detail}")

    no_rows = [r for r in funnel_rows if action(r, "scanner_action") == "BET_NO"]
    no_opened = [r for r in no_rows if r.get("paper_trade_opened")]
    no_converted = [
        r for r in no_rows
        if r.get("executed_action") and action(r, "executed_action") != "BET_NO"
    ]
    mismatches = [r for r in funnel_rows if r.get("handoff_action_mismatch") is True]

    print()
    print("BET_NO FUNNEL VERDICT")
    print("---------------------")
    print(f"scanner BET_NO rows in funnel: {len(no_rows)}")
    print(f"BET_NO rows opened as paper trades: {len(no_opened)}")
    print(f"BET_NO rows converted to another executed action: {len(no_converted)}")
    print(f"handoff mismatch rows: {len(mismatches)}")
    if no_rows:
        print("BET_NO now reaches Dashboard/PaperTrader funnel logging.")
        if no_opened:
            print("At least one BET_NO scanner opportunity opened a paper trade.")
        else:
            print("No BET_NO scanner opportunity has opened a paper trade in the funnel log.")
    else:
        print("No BET_NO funnel rows yet. Existing scanner logs had BET_NO, but future funnel evidence is still needed.")

    warnings: List[str] = []
    if not funnel_rows:
        warnings.append("NO_EXECUTION_FUNNEL_HISTORY_YET")
    if scanner_rows and not funnel_rows:
        warnings.append("SCANNER_LOG_EXISTS_BUT_FUNNEL_LOG_EMPTY")
    if no_rows and not no_opened:
        warnings.append("BET_NO_REACHES_FUNNEL_BUT_DOES_NOT_OPEN")
    if no_converted:
        warnings.append("BET_NO_CONVERTED_TO_OTHER_EXECUTED_ACTION")
    trade_counts = Counter(action(row, "action") for row in trades)
    if trade_counts.get("BET_NO", 0) == 0:
        warnings.append("PAPER_TRADE_LOG_BET_NO_ZERO")

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
    if funnel_rows:
        print("Execution-funnel evidence is available. Use BET_NO final reasons before changing execution.")
    else:
        print("Execution-funnel evidence is not available yet. This report is waiting for future Dashboard scans.")
    print("This report does not prove edge or profitability.")


if __name__ == "__main__":
    main()
