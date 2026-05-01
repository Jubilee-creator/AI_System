#!/usr/bin/env python3
"""
tools/report_side_coverage.py
-----------------------------
Read-only report for SIDE_BALANCED_RESEARCH shadow diagnostics.

This report never trades. It verifies that side coverage remains shadow-only,
proof-ineligible, and isolated from normal paper-trading proof.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (  # noqa: E402
    SIDE_BALANCED_RESEARCH_ENABLED,
    SIDE_BALANCED_RESEARCH_EXECUTE,
    SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE,
    SIDE_BALANCED_RESEARCH_SHADOW_ONLY,
)

SCANNER_LOG = ROOT / "logs" / "scanner_opportunities.jsonl"
SHADOW_LOG = ROOT / "logs" / "side_coverage_shadow.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
PAPER_LOG = ROOT / "logs" / "paper_trades.jsonl"
UNKNOWN_RUN = "UNKNOWN_RUN"
REQUIRED_ISOLATION_FIELDS = (
    "coverage_mode",
    "shadow_only",
    "side_coverage_test",
    "proof_eligible",
    "data_collection_override",
    "normal_strategy_trade",
    "would_execute",
)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return rows
    return rows


def action_value(row: Dict[str, Any]) -> str:
    return str(row.get("scanner_action") or row.get("action") or "UNKNOWN").upper()


def run_scan_key(row: Dict[str, Any]) -> Tuple[str, str]:
    return (
        str(row.get("run_id") or UNKNOWN_RUN),
        str(row.get("scan_id") or "UNKNOWN_SCAN"),
    )


def pct(part: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{part / total * 100:.1f}%"


def group_scanner_rows(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[run_scan_key(row)].append(row)
    return groups


def ordered_group_keys(rows: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    keys = []
    seen = set()
    for row in rows:
        key = run_scan_key(row)
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def shadow_candidate_from_scanner(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    bet_no_rank = 0
    non_pass_rank = 0
    for original_rank, row in enumerate(rows, start=1):
        action = action_value(row)
        if action == "PASS":
            continue
        non_pass_rank += 1
        if action == "BET_NO":
            bet_no_rank += 1
            return {
                "ticker": row.get("ticker"),
                "original_rank": original_rank,
                "scan_non_pass_rank": non_pass_rank,
                "side_queue_rank": bet_no_rank,
                "confidence": row.get("confidence"),
                "edge": row.get("edge"),
                "price_no": row.get("price_no"),
                "no_bid": row.get("no_bid"),
                "no_ask": row.get("no_ask"),
                "final_reason": "NATURAL_BET_NO_AVAILABLE",
            }
    return {"final_reason": "NO_NATURAL_BET_NO_AVAILABLE"}


def print_distribution(title: str, rows: List[Dict[str, Any]]) -> None:
    counts = Counter(action_value(row) for row in rows)
    total = sum(counts.values())
    print()
    print(title)
    print("-" * len(title))
    print(
        f"total={total}  BET_YES={counts.get('BET_YES', 0)} ({pct(counts.get('BET_YES', 0), total)})  "
        f"BET_NO={counts.get('BET_NO', 0)} ({pct(counts.get('BET_NO', 0), total)})  "
        f"PASS={counts.get('PASS', 0)} ({pct(counts.get('PASS', 0), total)})"
    )
    if total:
        print(f"raw counts: {dict(counts)}")


def main() -> None:
    scanner_rows = read_jsonl(SCANNER_LOG)
    shadow_rows = read_jsonl(SHADOW_LOG)
    funnel_rows = read_jsonl(FUNNEL_LOG)
    paper_rows = read_jsonl(PAPER_LOG)
    scanner_groups = group_scanner_rows(scanner_rows)
    recent_keys = ordered_group_keys(scanner_rows)[-12:]

    print("=" * 86)
    print("AI_SYSTEM SIDE COVERAGE REPORT")
    print("=" * 86)
    print("Read-only report. Shadow diagnostics only; no execution and no proof changes.")

    print()
    print("CONFIG")
    print("------")
    print(f"SIDE_BALANCED_RESEARCH_ENABLED:        {SIDE_BALANCED_RESEARCH_ENABLED}")
    print(f"SIDE_BALANCED_RESEARCH_SHADOW_ONLY:    {SIDE_BALANCED_RESEARCH_SHADOW_ONLY}")
    print(f"SIDE_BALANCED_RESEARCH_EXECUTE:        {SIDE_BALANCED_RESEARCH_EXECUTE}")
    print(f"SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE: {SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE}")

    print()
    print("LOG STATUS")
    print("----------")
    print(f"scanner log: {SCANNER_LOG} exists={SCANNER_LOG.exists()} rows={len(scanner_rows)}")
    print(f"shadow log:  {SHADOW_LOG} exists={SHADOW_LOG.exists()} rows={len(shadow_rows)}")
    print(f"funnel log:  {FUNNEL_LOG} exists={FUNNEL_LOG.exists()} rows={len(funnel_rows)}")
    print(f"paper log:   {PAPER_LOG} exists={PAPER_LOG.exists()} rows={len(paper_rows)}")

    print_distribution("SCANNER NATURAL SIDE DISTRIBUTION", scanner_rows)

    print()
    print("RECENT RUN / SCAN NATURAL BET_NO SHADOW CANDIDATES")
    print("--------------------------------------------------")
    if not recent_keys:
        print("(no scanner rows)")
    else:
        print(
            f"{'run_id':<30} {'scan_id':<18} {'Y':>4} {'N':>4} "
            f"{'candidate':<34} {'orig':>5} {'nonpass':>7} {'reason'}"
        )
        for key in recent_keys:
            rows = scanner_groups.get(key, [])
            counts = Counter(action_value(row) for row in rows)
            candidate = shadow_candidate_from_scanner(rows)
            print(
                f"{key[0]:<30} {key[1]:<18} "
                f"{counts.get('BET_YES', 0):>4} {counts.get('BET_NO', 0):>4} "
                f"{str(candidate.get('ticker') or '-'): <34} "
                f"{str(candidate.get('original_rank') or '-'):>5} "
                f"{str(candidate.get('scan_non_pass_rank') or '-'):>7} "
                f"{candidate.get('final_reason')}"
            )

    print()
    print("SHADOW LOG SUMMARY")
    print("------------------")
    if not shadow_rows:
        print("(no shadow rows logged yet; config is disabled by default)")
    else:
        reasons = Counter(str(row.get("final_reason") or "UNKNOWN") for row in shadow_rows)
        print(f"final reasons: {dict(reasons)}")
        print(f"proof_eligible True rows: {sum(1 for row in shadow_rows if row.get('proof_eligible') is True)}")
        print(f"would_execute True rows: {sum(1 for row in shadow_rows if row.get('would_execute') is True)}")
        print("recent rows:")
        for row in shadow_rows[-10:]:
            print(
                f"  {row.get('run_id')} {row.get('scan_id')} "
                f"{row.get('ticker') or '-'} reason={row.get('final_reason')} "
                f"proof_eligible={row.get('proof_eligible')} would_execute={row.get('would_execute')}"
            )

    coverage_paper_rows = [row for row in paper_rows if row.get("side_coverage_test")]
    coverage_funnel_rows = [row for row in funnel_rows if row.get("side_coverage_test")]

    warnings = []
    if SIDE_BALANCED_RESEARCH_EXECUTE:
        warnings.append("SIDE_BALANCED_RESEARCH_EXECUTE_TRUE_IN_PHASE_5M")
    if SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE:
        warnings.append("SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE_TRUE")
    if any(row.get("proof_eligible") is True for row in shadow_rows):
        warnings.append("SHADOW_ROW_PROOF_ELIGIBLE_TRUE")
    if any(row.get("would_execute") is True for row in shadow_rows):
        warnings.append("SHADOW_ROW_WOULD_EXECUTE_TRUE")
    if coverage_paper_rows:
        warnings.append("SIDE_COVERAGE_ROWS_FOUND_IN_PAPER_TRADES")
    if coverage_funnel_rows:
        warnings.append("SIDE_COVERAGE_ROWS_FOUND_IN_EXECUTION_FUNNEL")
    for row in shadow_rows:
        missing = [field for field in REQUIRED_ISOLATION_FIELDS if field not in row]
        if missing:
            warnings.append("SHADOW_ROW_MISSING_ISOLATION_FIELDS")
            break
        if row.get("coverage_mode") != "SIDE_BALANCED_RESEARCH":
            warnings.append("SHADOW_ROW_BAD_COVERAGE_MODE")
            break
        if row.get("shadow_only") is not True or row.get("side_coverage_test") is not True:
            warnings.append("SHADOW_ROW_BAD_SHADOW_FLAGS")
            break
        if row.get("normal_strategy_trade") is not False:
            warnings.append("SHADOW_ROW_NORMAL_STRATEGY_TRUE")
            break

    print()
    print("PROOF ISOLATION CHECK")
    print("---------------------")
    print(f"shadow rows: {len(shadow_rows)}")
    print(f"paper rows with side_coverage_test=True: {len(coverage_paper_rows)}")
    print(f"funnel rows with side_coverage_test=True: {len(coverage_funnel_rows)}")
    print("coverage rows are expected only in logs/side_coverage_shadow.jsonl during Phase 5M")

    print()
    print("WARNINGS")
    print("--------")
    if warnings:
        for warning in sorted(set(warnings)):
            print(f"[!] {warning}")
    else:
        print("(none)")

    print()
    print("BOTTOM LINE")
    print("-----------")
    print("SIDE_BALANCED_RESEARCH is shadow-only in this phase.")
    print("This report does not prove edge, profitability, or production BET_NO execution.")


if __name__ == "__main__":
    main()

