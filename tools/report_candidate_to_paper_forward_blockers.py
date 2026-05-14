#!/usr/bin/env python3
"""
Phase 10M - Candidate-to-Paper Forward Blocker Audit
Sentinel: CANDIDATE_TO_PAPER_FORWARD_BLOCKERS_OK

Read-only audit of candidate flow after the Phase 10G shadow logger started.
It explains why post-shadow candidates are not becoming paper_trades rows
without changing strategy, thresholds, scanner order, PaperTrader, risk, or
logs.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
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

SHADOW_START_TEXT = "2026-05-14T03:22:46.375517+00:00"
SHADOW_START = datetime.fromisoformat(SHADOW_START_TEXT).astimezone(timezone.utc)
SHADOW_LOG = ROOT / "logs" / "payoff_aware_shadow_ranking.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "CANDIDATE_TO_PAPER_FORWARD_BLOCKERS_OK"

STAGE_NAMES = (
    "scanner",
    "council",
    "risk",
    "market_quality",
    "edge_threshold",
    "max_open_exposure",
    "duplicate_protection",
    "paper_trader",
    "logging_failure",
    "process_dashboard_not_running",
    "unknown",
)

VALID_SAFETY_STAGES = {
    "council",
    "risk",
    "market_quality",
    "edge_threshold",
    "max_open_exposure",
    "duplicate_protection",
    "paper_trader",
}


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _row_ts(row: dict[str, Any]) -> datetime | None:
    return _parse_ts(row.get("timestamp_utc") or row.get("timestamp") or row.get("created_at"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _rows_after(rows: list[dict[str, Any]], start: datetime) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        ts = _row_ts(row)
        if ts is not None and ts >= start:
            selected.append(row)
    return selected


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _short(value: Any, limit: int = 220) -> str:
    if value is None:
        return "MISSING"
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _reason(row: dict[str, Any]) -> str:
    return str(row.get("final_reason") or row.get("reason") or "UNKNOWN")


def _is_trade_opened(row: dict[str, Any]) -> bool:
    reason = _reason(row).upper()
    return bool(row.get("paper_trade_opened")) or reason == "TRADE_OPENED"


def classify_stage(row: dict[str, Any]) -> str:
    """Map a funnel row to the most likely blocker stage."""
    reason = _reason(row).upper()
    trace = str(row.get("trace_excerpt") or row.get("message") or "").lower()
    council = str(row.get("council_decision") or "").upper()
    risk_reason = str(row.get("risk_block_reason") or "").lower()

    if _is_trade_opened(row):
        return "paper_trader"
    if not bool(row.get("dashboard_seen", True)):
        return "process_dashboard_not_running"
    if row.get("passed_to_paper_trader") is False:
        return "scanner"
    if row.get("paper_trader_received") is False:
        return "paper_trader"
    if "COUNCIL" in reason or council == "BLOCK" or "council block" in trace:
        return "council"
    if "RISK" in reason or risk_reason or "risk decision=block" in trace:
        return "risk"
    if "MARKET_QUALITY" in reason or "market quality filter" in trace:
        return "market_quality"
    if "MIN_EDGE" in reason or "CONFIDENCE" in reason or "EDGE_DANGER" in reason:
        return "edge_threshold"
    if "max_open" in reason.lower() or "EXPOSURE" in reason or "global open-trades cap" in trace:
        return "max_open_exposure"
    if "DUPLICATE" in reason or "duplicate ticker" in trace:
        return "duplicate_protection"
    if "QUARANTINE" in reason or "quarantined prefix" in trace:
        return "paper_trader"
    if "TRADER_DISABLED" in reason:
        return "paper_trader"
    if "PAPER_TRADE_LOG" in reason or "LOG_WRITE" in reason:
        return "logging_failure"
    if reason in {"UNKNOWN", "BLOCKED_OR_SKIPPED_UNKNOWN"}:
        return "unknown"
    return "unknown"


def _stage_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    counts = Counter({stage: 0 for stage in STAGE_NAMES})
    for row in rows:
        if _is_trade_opened(row):
            continue
        counts[classify_stage(row)] += 1
    return counts


def _shadow_pick_count(rows: list[dict[str, Any]]) -> int:
    total = 0
    for row in rows:
        for key in ("current_top_3", "payoff_aware_top_3", "strict_payoff_top_3"):
            raw = row.get(key)
            if isinstance(raw, list):
                total += len(raw)
    return total


def _candidate_count(shadow_rows: list[dict[str, Any]], funnel_rows: list[dict[str, Any]]) -> int:
    if funnel_rows:
        return len(funnel_rows)
    counts = [
        int(row.get("candidate_count"))
        for row in shadow_rows
        if isinstance(row.get("candidate_count"), int)
    ]
    return sum(counts) if counts else _shadow_pick_count(shadow_rows)


def _top_messages(rows: list[dict[str, Any]], limit: int = 10) -> list[tuple[str, int]]:
    messages = Counter()
    for row in rows:
        if _is_trade_opened(row):
            continue
        msg = (
            row.get("risk_block_reason")
            or row.get("council_reason")
            or row.get("dashboard_skip_reason")
            or row.get("trace_excerpt")
            or row.get("final_reason")
            or "UNKNOWN"
        )
        messages[_short(msg, 180)] += 1
    return messages.most_common(limit)


def _last_events(rows: list[dict[str, Any]], limit: int = 50) -> list[dict[str, Any]]:
    selected = sorted(rows, key=lambda row: _row_ts(row) or datetime.min.replace(tzinfo=timezone.utc))[-limit:]
    return [
        {
            "timestamp_utc": row.get("timestamp_utc") or row.get("timestamp"),
            "scan_id": row.get("scan_id"),
            "ticker": row.get("ticker"),
            "action": row.get("scanner_action") or row.get("intended_action") or row.get("action"),
            "final_reason": _reason(row),
            "stage": classify_stage(row),
            "passed_to_paper_trader": bool(row.get("passed_to_paper_trader")),
            "paper_trader_received": bool(row.get("paper_trader_received")),
            "paper_trade_opened": _is_trade_opened(row),
            "open_count_before": row.get("open_count_before"),
            "open_slots_before": row.get("open_slots_before"),
            "trace_excerpt": _short(row.get("trace_excerpt"), 240),
        }
        for row in selected
    ]


def _paper_trade_logging_health(opened_events: int, paper_rows: int) -> tuple[str, bool | None]:
    if opened_events > 0 and paper_rows == 0:
        return ("WRITE_FAILURE_SUSPECTED", False)
    if opened_events > 0 and paper_rows < opened_events:
        return ("PARTIAL_WRITE_MISMATCH", False)
    if opened_events > 0:
        return ("HEALTHY_FOR_OPENED_EVENTS", True)
    return ("NOT_TESTED_NO_TRADE_OPENED_EVENTS", None)


def classify_main_blocker(
    shadow_rows: list[dict[str, Any]],
    funnel_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
) -> str:
    candidate_count = _candidate_count(shadow_rows, funnel_rows)
    if candidate_count == 0:
        return "NO_CANDIDATES_AFTER_SHADOW_START"
    if not funnel_rows:
        return "CANDIDATES_NOT_REACHING_EXECUTION_FUNNEL"

    opened_events = sum(1 for row in funnel_rows if _is_trade_opened(row))
    paper_received = sum(1 for row in funnel_rows if bool(row.get("paper_trader_received")))
    if paper_received == 0:
        return "PAPER_TRADER_NOT_CALLED"
    if opened_events > 0 and not trade_rows:
        return "PAPER_TRADE_LOG_WRITE_FAILURE"

    blocked = [row for row in funnel_rows if not _is_trade_opened(row)]
    if opened_events == 0 and blocked:
        stages = [classify_stage(row) for row in blocked]
        unique = set(stages)
        if unique == {"council"}:
            return "COUNCIL_BLOCKING_ALL"
        if unique == {"risk"}:
            return "RISK_BLOCKING_ALL"
        if unique == {"edge_threshold"}:
            return "EDGE_FILTER_BLOCKING_ALL"
        if unique == {"market_quality"}:
            return "MARKET_QUALITY_BLOCKING_ALL"
        if unique == {"max_open_exposure"}:
            return "MAX_OPEN_OR_EXPOSURE_BLOCKING"
        if unique == {"duplicate_protection"}:
            return "DUPLICATE_PROTECTION_BLOCKING"
        if unique and unique.issubset(VALID_SAFETY_STAGES):
            return "EXPECTED_SAFETY_BLOCK_NOT_A_BUG"
        return "UNKNOWN_FORWARD_BLOCKER"

    if opened_events > 0 and trade_rows:
        return "EXPECTED_SAFETY_BLOCK_NOT_A_BUG"
    return "UNKNOWN_FORWARD_BLOCKER"


def build_report(
    shadow_path: Path = SHADOW_LOG,
    funnel_path: Path = FUNNEL_LOG,
    trades_path: Path = TRADES_LOG,
    shadow_start: datetime = SHADOW_START,
) -> dict[str, Any]:
    shadow_rows = _rows_after(_read_jsonl(shadow_path), shadow_start)
    funnel_rows = _rows_after(_read_jsonl(funnel_path), shadow_start)
    trade_rows = _rows_after(_read_jsonl(trades_path), shadow_start)

    opened_events = [row for row in funnel_rows if _is_trade_opened(row)]
    paper_logged_events = [
        row for row in funnel_rows
        if _reason(row).upper() == "PAPER_TRADE_LOGGED" or bool(row.get("paper_trade_logged"))
    ]
    blocked = [row for row in funnel_rows if not _is_trade_opened(row)]
    stage_counts = _stage_counts(funnel_rows)
    paper_received = sum(1 for row in funnel_rows if bool(row.get("paper_trader_received")))
    passed_to_paper = sum(1 for row in funnel_rows if bool(row.get("passed_to_paper_trader")))
    before_paper = sum(
        1 for row in funnel_rows
        if row.get("passed_to_paper_trader") is False or row.get("paper_trader_received") is False
    )
    logging_health, logging_bool = _paper_trade_logging_health(len(opened_events), len(trade_rows))
    latest_funnel_ts = max((_row_ts(row) for row in funnel_rows if _row_ts(row) is not None), default=None)

    return {
        "shadow_start": shadow_start.isoformat(),
        "shadow_rows_after_start": len(shadow_rows),
        "shadow_pick_count_after_start": _shadow_pick_count(shadow_rows),
        "candidate_count_after_start": _candidate_count(shadow_rows, funnel_rows),
        "execution_funnel_events_after_start": len(funnel_rows),
        "trade_opened_events_after_start": len(opened_events),
        "paper_trade_logged_events_after_start": len(paper_logged_events),
        "paper_trades_rows_after_start": len(trade_rows),
        "blocked_candidates_by_reason": dict(Counter(_reason(row) for row in blocked).most_common()),
        "blocker_counts_by_stage": dict(stage_counts),
        "top_recurring_blocker_messages": _top_messages(funnel_rows),
        "last_50_relevant_funnel_events": _last_events(funnel_rows),
        "passed_to_paper_trader_count": passed_to_paper,
        "paper_trader_received_count": paper_received,
        "candidates_dying_before_paper_trader": before_paper,
        "paper_trader_is_being_called": paper_received > 0,
        "paper_trades_write_health": logging_health,
        "paper_trades_write_healthy": logging_bool,
        "research_watchlist_mode": str(TRADING_MODE),
        "global_forced_learning_mode": bool(GLOBAL_FORCED_LEARNING_MODE),
        "data_collection_override_enabled": bool(DATA_COLLECTION_OVERRIDE_ENABLED),
        "quarantined_ticker_prefixes": list(QUARANTINED_TICKER_PREFIXES),
        "research_watchlist_blocking_execution": False,
        "latest_funnel_timestamp": latest_funnel_ts.isoformat() if latest_funnel_ts else None,
        "main_blocker_label": classify_main_blocker(shadow_rows, funnel_rows, trade_rows),
    }


def print_report(report: dict[str, Any]) -> None:
    print("=== Candidate-to-Paper Forward Blocker Audit (Phase 10M) ===")
    print(f"shadow_start: {report['shadow_start']}")
    print(f"shadow_rows_after_start: {_fmt_int(report['shadow_rows_after_start'])}")
    print(f"shadow_pick_count_after_start: {_fmt_int(report['shadow_pick_count_after_start'])}")
    print(f"candidate_count_after_start: {_fmt_int(report['candidate_count_after_start'])}")
    print(f"execution_funnel_events_after_start: {_fmt_int(report['execution_funnel_events_after_start'])}")
    print(f"trade_opened_events_after_start: {_fmt_int(report['trade_opened_events_after_start'])}")
    print(f"paper_trade_logged_events_after_start: {_fmt_int(report['paper_trade_logged_events_after_start'])}")
    print(f"paper_trades_rows_after_start: {_fmt_int(report['paper_trades_rows_after_start'])}")
    print(f"passed_to_paper_trader_count: {_fmt_int(report['passed_to_paper_trader_count'])}")
    print(f"paper_trader_received_count: {_fmt_int(report['paper_trader_received_count'])}")
    print(f"candidates_dying_before_paper_trader: {_fmt_int(report['candidates_dying_before_paper_trader'])}")
    print(f"paper_trader_is_being_called: {report['paper_trader_is_being_called']}")
    print(f"paper_trades_write_health: {report['paper_trades_write_health']}")
    print(f"research_watchlist_mode: {report['research_watchlist_mode']}")
    print(f"research_watchlist_blocking_execution: {report['research_watchlist_blocking_execution']}")
    print(f"latest_funnel_timestamp: {report['latest_funnel_timestamp']}")
    print(f"main_blocker_label: {report['main_blocker_label']}")

    print("\nBlocked candidates by reason:")
    if report["blocked_candidates_by_reason"]:
        for reason, count in report["blocked_candidates_by_reason"].items():
            print(f"  {reason}: {_fmt_int(count)}")
    else:
        print("  NONE")

    print("\nBlocker counts by stage:")
    total_stage_blocks = sum(report["blocker_counts_by_stage"].values())
    for stage in STAGE_NAMES:
        count = int(report["blocker_counts_by_stage"].get(stage, 0))
        pct = count / total_stage_blocks if total_stage_blocks else None
        print(f"  {stage}: {_fmt_int(count)} ({_fmt_pct(pct)})")

    print("\nTop recurring blocker messages:")
    if report["top_recurring_blocker_messages"]:
        for message, count in report["top_recurring_blocker_messages"]:
            print(f"  {_fmt_int(count)}x {message}")
    else:
        print("  NONE")

    print("\nLast 50 relevant funnel events after shadow start:")
    if report["last_50_relevant_funnel_events"]:
        for event in report["last_50_relevant_funnel_events"]:
            print(
                "  "
                f"{event['timestamp_utc']} scan={event['scan_id']} ticker={event['ticker']} "
                f"action={event['action']} reason={event['final_reason']} stage={event['stage']} "
                f"received={event['paper_trader_received']} opened={event['paper_trade_opened']}"
            )
    else:
        print("  NONE")

    print("\nOperational read:")
    if report["main_blocker_label"] == "EXPECTED_SAFETY_BLOCK_NOT_A_BUG":
        print("  Candidates are reaching PaperTrader; explicit safety/economic gates are blocking opens.")
    elif report["main_blocker_label"] == "PAPER_TRADE_LOG_WRITE_FAILURE":
        print("  TRADE_OPENED appeared without paper_trades rows; inspect TradeLogger write path immediately.")
    elif report["main_blocker_label"] == "PAPER_TRADER_NOT_CALLED":
        print("  Execution funnel exists but PaperTrader receipt is absent; inspect dashboard handoff/process state.")
    else:
        print("  Main label identifies the dominant forward blocker; inspect stage/reason counts before patching.")

    print(f"\nSafety locks: real_money=OFF scale=OFF kelly=OFF kxeth_quarantine={bool(QUARANTINED_TICKER_PREFIXES)}")
    print(SENTINEL)


def main() -> None:
    print_report(build_report())


if __name__ == "__main__":
    main()
