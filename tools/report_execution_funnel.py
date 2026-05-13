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
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import load_trades  # noqa: E402


FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
SCANNER_LOG = ROOT / "logs" / "scanner_opportunities.jsonl"
UNKNOWN_RUN = "UNKNOWN_RUN"
SCANNER_TAIL_BYTES = 50_000_000


def read_jsonl(path: Path, max_tail_bytes: Optional[int] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        size = path.stat().st_size
        start = 0
        if max_tail_bytes is not None and size > max_tail_bytes:
            start = size - max_tail_bytes
        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()
            for raw in handle:
                line = raw.decode("utf-8", errors="replace").strip()
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


def scanner_tail_limited() -> bool:
    try:
        return SCANNER_LOG.exists() and SCANNER_LOG.stat().st_size > SCANNER_TAIL_BYTES
    except OSError:
        return False


def pct(part: int, total: int) -> str:
    if not total:
        return "0.0%"
    return f"{part / total * 100:.1f}%"


def action(row: Dict[str, Any], key: str) -> str:
    return str(row.get(key) or "UNKNOWN").upper()


def row_run_id(row: Dict[str, Any]) -> str:
    return str(row.get("run_id") or UNKNOWN_RUN)


def row_scan_id(row: Dict[str, Any]) -> str:
    return str(row.get("scan_id") or "UNKNOWN_SCAN")


def scan_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return (row_run_id(row), row_scan_id(row))


def format_scan_key(key: Tuple[str, str]) -> str:
    run_id, scan_id = key
    return f"{run_id}/{scan_id}"


def as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def avg(values: List[Any]) -> Optional[float]:
    nums = [as_float(value) for value in values]
    nums = [value for value in nums if value is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def fmt_num(value: Optional[float], digits: int = 3) -> str:
    if value is None:
        return "--"
    return f"{value:.{digits}f}"


def latest_scan(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    keyed_rows = [row for row in rows if row.get("scan_id")]
    if keyed_rows:
        latest = scan_key(keyed_rows[-1])
        return [row for row in rows if scan_key(row) == latest]
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


def scan_join_rows(
    scanner_rows: List[Dict[str, Any]],
    funnel_rows: List[Dict[str, Any]],
    limit: int = 12,
) -> List[Dict[str, Any]]:
    scanner_by_scan: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    funnel_by_scan: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    ordered_scan_keys: List[Tuple[str, str]] = []

    for row in scanner_rows:
        key = scan_key(row)
        if key not in scanner_by_scan:
            ordered_scan_keys.append(key)
        scanner_by_scan[key][action(row, "scanner_action")] += 1
        scanner_by_scan[key]["_total"] += 1

    for row in funnel_rows:
        key = scan_key(row)
        funnel_by_scan[key][action(row, "scanner_action")] += 1
        funnel_by_scan[key]["_total"] += 1
        if row.get("paper_trade_opened"):
            funnel_by_scan[key]["_opened"] += 1
        if row.get("final_reason") == "BLOCKED_MAX_OPEN_TRADES":
            funnel_by_scan[key]["_blocked_max_open"] += 1
        if row.get("handoff_action_mismatch") is True:
            funnel_by_scan[key]["_mismatch"] += 1

    rows: List[Dict[str, Any]] = []
    for key in ordered_scan_keys[-limit:]:
        scanner_counts = scanner_by_scan.get(key, Counter())
        funnel_counts = funnel_by_scan.get(key, Counter())
        note = ""
        if scanner_counts.get("BET_NO", 0) and not funnel_counts.get("BET_NO", 0):
            note = "SCANNER_BET_NO_NOT_IN_FUNNEL"
        elif funnel_counts.get("BET_NO", 0) and not funnel_counts.get("_opened", 0):
            note = "FUNNEL_BET_NO_NO_OPEN"
        rows.append({
            "run_id": key[0],
            "scan_id": key[1],
            "scan_key": format_scan_key(key),
            "scanner_total": scanner_counts.get("_total", 0),
            "scanner_yes": scanner_counts.get("BET_YES", 0),
            "scanner_no": scanner_counts.get("BET_NO", 0),
            "scanner_pass": scanner_counts.get("PASS", 0),
            "funnel_total": funnel_counts.get("_total", 0),
            "funnel_yes": funnel_counts.get("BET_YES", 0),
            "funnel_no": funnel_counts.get("BET_NO", 0),
            "opened": funnel_counts.get("_opened", 0),
            "blocked_max_open": funnel_counts.get("_blocked_max_open", 0),
            "mismatch": funnel_counts.get("_mismatch", 0),
            "note": note,
        })
    return rows


def print_scan_join_audit(scanner_rows: List[Dict[str, Any]], funnel_rows: List[Dict[str, Any]]) -> None:
    print()
    print("RUN / SCAN JOIN AUDIT")
    print("---------------------")
    rows = scan_join_rows(scanner_rows, funnel_rows)
    if not rows:
        print("(no scan rows)")
        return
    print(
        "run_id                  scan_id              "
        "scan_total  scan_Y  scan_N  scan_PASS  "
        "funnel_total  funnel_Y  funnel_N  opened  max_open  mismatch  note"
    )
    for row in rows:
        print(
            f"{row['run_id']:<23} "
            f"{row['scan_id']:<20} "
            f"{row['scanner_total']:>10} "
            f"{row['scanner_yes']:>7} "
            f"{row['scanner_no']:>7} "
            f"{row['scanner_pass']:>10} "
            f"{row['funnel_total']:>13} "
            f"{row['funnel_yes']:>9} "
            f"{row['funnel_no']:>9} "
            f"{row['opened']:>7} "
            f"{row['blocked_max_open']:>9} "
            f"{row['mismatch']:>9}  "
            f"{row['note']}"
        )


def rank_cap_rows(
    scanner_rows: List[Dict[str, Any]],
    funnel_rows: List[Dict[str, Any]],
    limit: int = 20,
) -> List[Dict[str, Any]]:
    scanner_by_scan: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    funnel_by_scan: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    ordered_scan_keys: List[Tuple[str, str]] = []

    for row in scanner_rows:
        key = scan_key(row)
        if key not in scanner_by_scan:
            ordered_scan_keys.append(key)
        scanner_by_scan[key].append(row)

    for row in funnel_rows:
        funnel_by_scan[scan_key(row)].append(row)

    rows: List[Dict[str, Any]] = []
    for key in ordered_scan_keys:
        scan_rows = scanner_by_scan[key]
        non_pass = [row for row in scan_rows if action(row, "scanner_action") != "PASS"]
        if not non_pass:
            continue

        first_yes_rank = None
        first_no_rank = None
        yes_ranks: List[int] = []
        no_ranks: List[int] = []
        yes_conf: List[Any] = []
        no_conf: List[Any] = []
        yes_edge: List[Any] = []
        no_edge: List[Any] = []

        for rank, row in enumerate(non_pass, start=1):
            scanner_action = action(row, "scanner_action")
            if scanner_action == "BET_YES":
                yes_ranks.append(rank)
                yes_conf.append(row.get("confidence"))
                yes_edge.append(row.get("edge"))
                if first_yes_rank is None:
                    first_yes_rank = rank
            elif scanner_action == "BET_NO":
                no_ranks.append(rank)
                no_conf.append(row.get("confidence"))
                no_edge.append(row.get("edge"))
                if first_no_rank is None:
                    first_no_rank = rank

        if first_no_rank is None:
            continue

        scan_funnel = funnel_by_scan.get(key, [])
        first_funnel_no_index = None
        for idx, row in enumerate(scan_funnel):
            if action(row, "scanner_action") == "BET_NO":
                first_funnel_no_index = idx
                break

        opened_before_first_no = 0
        max_open_blocks_before_or_at_no = 0
        first_no_cap_full = None
        first_no_final_reason = None
        if first_funnel_no_index is not None:
            rows_to_no = scan_funnel[: first_funnel_no_index + 1]
            opened_before_first_no = sum(1 for row in scan_funnel[:first_funnel_no_index] if row.get("paper_trade_opened"))
            max_open_blocks_before_or_at_no = sum(
                1 for row in rows_to_no
                if row.get("final_reason") == "BLOCKED_MAX_OPEN_TRADES"
            )
            first_no_row = scan_funnel[first_funnel_no_index]
            first_no_cap_full = first_no_row.get("cap_already_full")
            first_no_final_reason = first_no_row.get("final_reason")

        max_open_candidates = [as_int(row.get("max_open_trades")) for row in scan_funnel]
        max_open_candidates = [value for value in max_open_candidates if value is not None]
        max_open = max_open_candidates[-1] if max_open_candidates else 3
        yes_before_first_no = sum(
            1 for row in non_pass[: first_no_rank - 1]
            if action(row, "scanner_action") == "BET_YES"
        )
        likely_after_slots = (
            first_no_cap_full is True
            or first_no_final_reason == "BLOCKED_MAX_OPEN_TRADES"
            or yes_before_first_no >= max_open
        )

        rows.append({
            "run_id": key[0],
            "scan_id": key[1],
            "scan_key": format_scan_key(key),
            "first_yes_rank": first_yes_rank,
            "first_no_rank": first_no_rank,
            "yes_before_first_no": yes_before_first_no,
            "opened_before_first_no": opened_before_first_no,
            "max_open_blocks_before_or_at_no": max_open_blocks_before_or_at_no,
            "no_avg_rank": avg(no_ranks),
            "yes_avg_rank": avg(yes_ranks),
            "no_avg_confidence": avg(no_conf),
            "yes_avg_confidence": avg(yes_conf),
            "no_avg_edge": avg(no_edge),
            "yes_avg_edge": avg(yes_edge),
            "likely_after_slots": likely_after_slots,
            "funnel_no_seen": first_funnel_no_index is not None,
        })

    return rows[-limit:]


def print_rank_cap_audit(scanner_rows: List[Dict[str, Any]], funnel_rows: List[Dict[str, Any]]) -> None:
    print()
    print("RANK / CAP AUDIT")
    print("----------------")
    rows = rank_cap_rows(scanner_rows, funnel_rows)
    if not rows:
        print("(no scanner scans with BET_NO)")
        return

    print(
        "run_id                  scan_id              "
        "first_Y  first_N  Y_before_N  opened_before_N  max_blocks_to_N  "
        "avg_rank_Y  avg_rank_N  avg_conf_Y  avg_conf_N  avg_edge_Y  avg_edge_N  likely_after_slots"
    )
    for row in rows:
        print(
            f"{row['run_id']:<23} "
            f"{row['scan_id']:<20} "
            f"{str(row['first_yes_rank'] or '--'):>7} "
            f"{str(row['first_no_rank'] or '--'):>8} "
            f"{row['yes_before_first_no']:>10} "
            f"{row['opened_before_first_no']:>16} "
            f"{row['max_open_blocks_before_or_at_no']:>15} "
            f"{fmt_num(row['yes_avg_rank'], 1):>11} "
            f"{fmt_num(row['no_avg_rank'], 1):>11} "
            f"{fmt_num(row['yes_avg_confidence'], 3):>10} "
            f"{fmt_num(row['no_avg_confidence'], 3):>10} "
            f"{fmt_num(row['yes_avg_edge'], 4):>10} "
            f"{fmt_num(row['no_avg_edge'], 4):>10} "
            f"{'YES' if row['likely_after_slots'] else 'NO'}"
        )

    all_no_ranks: List[Any] = []
    all_yes_ranks: List[Any] = []
    all_no_conf: List[Any] = []
    all_yes_conf: List[Any] = []
    all_no_edge: List[Any] = []
    all_yes_edge: List[Any] = []

    scanner_by_scan: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in scanner_rows:
        scanner_by_scan[scan_key(row)].append(row)
    for scan_rows in scanner_by_scan.values():
        non_pass = [row for row in scan_rows if action(row, "scanner_action") != "PASS"]
        for rank, row in enumerate(non_pass, start=1):
            scanner_action = action(row, "scanner_action")
            if scanner_action == "BET_YES":
                all_yes_ranks.append(rank)
                all_yes_conf.append(row.get("confidence"))
                all_yes_edge.append(row.get("edge"))
            elif scanner_action == "BET_NO":
                all_no_ranks.append(rank)
                all_no_conf.append(row.get("confidence"))
                all_no_edge.append(row.get("edge"))

    likely_count = sum(1 for row in rows if row["likely_after_slots"])
    print()
    print(
        "overall scanner non-PASS ranks: "
        f"BET_YES avg={fmt_num(avg(all_yes_ranks), 2)}  "
        f"BET_NO avg={fmt_num(avg(all_no_ranks), 2)}"
    )
    print(
        "overall scanner confidence/edge: "
        f"BET_YES conf={fmt_num(avg(all_yes_conf), 3)} edge={fmt_num(avg(all_yes_edge), 4)}  "
        f"BET_NO conf={fmt_num(avg(all_no_conf), 3)} edge={fmt_num(avg(all_no_edge), 4)}"
    )
    print(f"recent BET_NO scans likely after slots filled: {likely_count}/{len(rows)}")
    print("Scanner ranks are reconstructed within run_id + scan_id groups.")
    if any(row_run_id(row) == UNKNOWN_RUN for row in scanner_rows + funnel_rows):
        print("Rows without run_id are labeled UNKNOWN_RUN; old restart boundaries cannot be proven.")


def print_bet_no_sample(scanner_rows: List[Dict[str, Any]], limit: int = 10) -> None:
    print()
    print("BET_NO SCANNER SAMPLE")
    print("---------------------")
    no_rows = [row for row in scanner_rows if action(row, "scanner_action") == "BET_NO"]
    if not no_rows:
        print("(none)")
        return
    for row in no_rows[-limit:]:
        reasoning = str(row.get("reasoning") or "")
        if len(reasoning) > 90:
            reasoning = reasoning[:87] + "..."
        print(
            f"{row_run_id(row):<23} "
            f"{row_scan_id(row):<18} "
            f"{str(row.get('ticker')):<32} "
            f"conf={row.get('confidence')} "
            f"edge={row.get('edge')} "
            f"yes_mid={row.get('yes_mid')} "
            f"no_mid={row.get('no_mid')} "
            f"{reasoning}"
        )


def main() -> None:
    funnel_rows = read_jsonl(FUNNEL_LOG)
    scanner_rows = read_jsonl(SCANNER_LOG, max_tail_bytes=SCANNER_TAIL_BYTES)
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
    print(f"scanner log: {SCANNER_LOG}")
    print(f"scanner rows loaded: {len(scanner_rows)}")
    if scanner_tail_limited():
        print(f"scanner rows are tail-limited to last {SCANNER_TAIL_BYTES} bytes")
    if funnel_rows:
        print(f"first row timestamp: {funnel_rows[0].get('timestamp_utc')}")
        print(f"latest row timestamp: {funnel_rows[-1].get('timestamp_utc')}")
        print(f"latest run_id: {row_run_id(funnel_rows[-1])}")
        print(f"latest scan_id: {funnel_rows[-1].get('scan_id')}")
        if any(row_run_id(row) == UNKNOWN_RUN for row in funnel_rows + scanner_rows):
            print("legacy rows without run_id: present (reported as UNKNOWN_RUN)")
    else:
        print("No funnel rows yet. Restart/run Dashboard for future non-PASS opportunities.")

    print_action_summary("SCANNER-SIDE ACTIONS", scanner_rows, "scanner_action")
    print_action_summary("FUNNEL ROW SCANNER ACTIONS", funnel_rows, "scanner_action")
    print_action_summary("LATEST FUNNEL ROW SCANNER ACTIONS", latest_funnel, "scanner_action")
    print_action_summary("FUNNEL EXECUTED ACTIONS", [r for r in funnel_rows if r.get("executed_action")], "executed_action")
    print_action_summary("PAPER TRADE LOG ACTIONS", trades, "action")

    print_counts("FUNNEL FINAL REASONS", Counter(str(r.get("final_reason") or "UNKNOWN") for r in funnel_rows))
    print_counts("COUNCIL DECISIONS IN FUNNEL", Counter(str(r.get("council_decision") or "UNKNOWN") for r in funnel_rows))
    print_scan_join_audit(scanner_rows, funnel_rows)
    print_rank_cap_audit(scanner_rows, funnel_rows)
    print_bet_no_sample(scanner_rows)

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
