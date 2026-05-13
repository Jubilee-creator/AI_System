#!/usr/bin/env python3
"""
Phase 10B - Candidate-to-Paper Bridge + Ghost Open Blocker Audit
Sentinel: CANDIDATE_TO_PAPER_BRIDGE_AUDIT_OK

Read-only diagnostic for the scanner/Dashboard/PaperTrader handoff.
This report does not modify logs, strategy thresholds, proof gates, risk
settings, live-money settings, or historical paper trades.
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

from brain.paper_trader import MAX_CONCURRENT_OPEN_TRADES
from config.trading_config import (
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    QUARANTINED_TICKER_PREFIXES,
    TRADING_MODE,
)
from tools.report_evidence_delta_registry_drift import BASELINE_SNAPSHOT

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
SCANNER_LOG = ROOT / "logs" / "scanner_opportunities.jsonl"
SENTINEL = "CANDIDATE_TO_PAPER_BRIDGE_AUDIT_OK"

TERMINAL_STATUSES = {"SETTLED", "FORCED_CLOSE", "VOID_LEGACY_DUPLICATE"}
VALID_BLOCKERS = {
    "BLOCKED_MARKET_QUALITY",
    "BLOCKED_MIN_EDGE",
    "BLOCKED_COUNCIL",
    "BLOCKED_RISK",
    "BLOCKED_EDGE_DANGER_GUARD",
    "BLOCKED_QUARANTINE",
    "BLOCKED_DUPLICATE_TICKER",
    "BLOCKED_MAX_OPEN_TRADES",
    "BLOCKED_CONFIDENCE",
    "BLOCKED_TRADER_DISABLED",
}


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _fmt_ts(value: datetime | None) -> str:
    return value.isoformat() if value else "MISSING"


def _fmt_ratio(value: float | None) -> str:
    return "MISSING" if value is None else f"{value:.6f}"


def _parse_jsonl_line(line: str) -> tuple[dict[str, Any] | None, bool]:
    line = line.strip()
    if not line:
        return None, False
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return None, True
    if isinstance(item, dict):
        return item, False
    return None, True


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    malformed = 0
    if not path.exists():
        return rows, malformed
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            item, bad = _parse_jsonl_line(raw)
            if bad:
                malformed += 1
            if item is not None:
                rows.append(item)
    return rows, malformed


def _read_jsonl_tail(path: Path, max_bytes: int) -> tuple[list[dict[str, Any]], int, bool]:
    rows: list[dict[str, Any]] = []
    malformed = 0
    if not path.exists():
        return rows, malformed, False
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    tail_limited = start > 0
    with path.open("rb") as handle:
        handle.seek(start)
        if tail_limited:
            handle.readline()
        for raw in handle:
            item, bad = _parse_jsonl_line(raw.decode("utf-8", errors="replace"))
            if bad:
                malformed += 1
            if item is not None:
                rows.append(item)
    return rows, malformed, tail_limited


def _row_ts(row: dict[str, Any], *fields: str) -> datetime | None:
    for field in fields:
        ts = _parse_ts(row.get(field))
        if ts is not None:
            return ts
    return None


def _after(rows: list[dict[str, Any]], baseline_ts: datetime | None, *fields: str) -> list[dict[str, Any]]:
    if baseline_ts is None:
        return list(rows)
    return [row for row in rows if (ts := _row_ts(row, *fields)) is not None and ts > baseline_ts]


def _latest_ts(rows: list[dict[str, Any]], *fields: str) -> datetime | None:
    values = [_row_ts(row, *fields) for row in rows]
    values = [value for value in values if value is not None]
    return max(values, default=None)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trade_key(row: dict[str, Any]) -> tuple[Any, ...] | None:
    ts = str(row.get("timestamp") or "")[:19]
    ticker = row.get("ticker")
    action = row.get("action")
    if not (ts and ticker and action):
        return None
    size = _as_float(row.get("size"))
    entry = _as_float(row.get("entry_price"))
    return (
        ts,
        str(ticker),
        str(action),
        round(size, 6) if size is not None else None,
        round(entry, 6) if entry is not None else None,
    )


def split_open_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved_keys = {
        key
        for row in rows
        if str(row.get("status") or "").upper() in TERMINAL_STATUSES
        for key in [_trade_key(row)]
        if key is not None
    }
    active: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("status") or "").upper() != "OPEN":
            continue
        key = _trade_key(row)
        if key is not None and key in resolved_keys:
            stale.append(row)
        else:
            active.append(row)
    return active, stale


def _has_quote_metadata(row: dict[str, Any]) -> bool:
    if row.get("price_yes") is not None or row.get("price_no") is not None:
        return True
    return any(row.get(field) is not None for field in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def _is_quarantined(row: dict[str, Any]) -> bool:
    ticker = str(row.get("ticker") or "").upper()
    return any(ticker.startswith(str(prefix).upper()) for prefix in QUARANTINED_TICKER_PREFIXES)


def is_clean_proof_row(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").upper()
    result = str(row.get("result") or "").upper()
    modern_full = (
        _as_float(row.get("risk_edge")) is not None
        and _as_float(row.get("model_probability")) is not None
        and _has_quote_metadata(row)
    )
    return (
        status == "SETTLED"
        and result in {"WIN", "LOSS"}
        and modern_full
        and not bool(row.get("data_collection_override"))
        and not bool(row.get("bootstrap_provisional"))
        and not bool(row.get("side_coverage"))
        and not bool(row.get("side_coverage_test"))
        and not _is_quarantined(row)
        and _as_float(row.get("economic_pnl")) is not None
    )


def _opened_funnel_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("paper_trade_opened") is True
        or str(row.get("final_reason") or "").upper() == "TRADE_OPENED"
        or str(row.get("final_status") or "").upper() == "TRADE_OPENED"
    ]


def _paper_entry_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("status") or "").upper() == "OPEN"]


def _match_opened_events(
    opened_rows: list[dict[str, Any]],
    paper_rows: list[dict[str, Any]],
    *,
    max_seconds: float = 300.0,
) -> tuple[int, list[dict[str, Any]]]:
    paper_candidates = list(enumerate(_paper_entry_rows(paper_rows)))
    used: set[int] = set()
    unmatched: list[dict[str, Any]] = []

    for opened in opened_rows:
        opened_ts = _row_ts(opened, "timestamp_utc", "timestamp")
        ticker = str(opened.get("ticker") or "")
        action = str(opened.get("executed_action") or opened.get("intended_action") or "").upper()
        matched_index = None
        for index, paper in paper_candidates:
            if index in used:
                continue
            if str(paper.get("ticker") or "") != ticker:
                continue
            if action and str(paper.get("action") or "").upper() != action:
                continue
            paper_ts = _row_ts(paper, "timestamp")
            if opened_ts is not None and paper_ts is not None:
                if abs((opened_ts - paper_ts).total_seconds()) > max_seconds:
                    continue
            matched_index = index
            break
        if matched_index is None:
            unmatched.append(opened)
        else:
            used.add(matched_index)

    return len(used), unmatched


def _funnel_summary(rows: list[dict[str, Any]], baseline_ts: datetime | None) -> dict[str, Any]:
    after_rows = _after(rows, baseline_ts, "timestamp_utc", "timestamp")
    reason_counts = Counter(str(row.get("final_reason") or "UNKNOWN") for row in after_rows)
    scanner_counts = Counter(str(row.get("scanner_action") or "UNKNOWN").upper() for row in after_rows)
    opened_rows = _opened_funnel_rows(after_rows)
    blocked_max_rows = [
        row for row in after_rows
        if str(row.get("final_reason") or "").upper() == "BLOCKED_MAX_OPEN_TRADES"
    ]
    max_open_setting = MAX_CONCURRENT_OPEN_TRADES
    suspicious_max_blocks = [
        row for row in blocked_max_rows
        if row.get("cap_already_full") is False
        or (
            _as_float(row.get("open_count_before")) is not None
            and int(_as_float(row.get("open_count_before")) or 0) < max_open_setting
        )
    ]
    return {
        "total_rows": len(rows),
        "all_rows": rows,
        "after_baseline_rows": len(after_rows),
        "after_rows": after_rows,
        "passed_to_paper_trader": sum(1 for row in after_rows if row.get("passed_to_paper_trader") is True),
        "paper_trader_received": sum(1 for row in after_rows if row.get("paper_trader_received") is True),
        "trade_opened": len(opened_rows),
        "opened_rows": opened_rows,
        "blocked_max_open": len(blocked_max_rows),
        "blocked_max_rows": blocked_max_rows,
        "suspicious_max_blocks": suspicious_max_blocks,
        "final_reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        "scanner_action_counts": dict(sorted(scanner_counts.items(), key=lambda item: (-item[1], item[0]))),
        "latest_timestamp": _latest_ts(rows, "timestamp_utc", "timestamp"),
        "latest_after_timestamp": _latest_ts(after_rows, "timestamp_utc", "timestamp"),
        "total_trade_opened": len(_opened_funnel_rows(rows)),
        "total_blocked_max_open": sum(
            1 for row in rows
            if str(row.get("final_reason") or "").upper() == "BLOCKED_MAX_OPEN_TRADES"
        ),
    }


def _paper_summary(rows: list[dict[str, Any]], baseline_ts: datetime | None) -> dict[str, Any]:
    after_rows = _after(rows, baseline_ts, "timestamp")
    active, stale = split_open_rows(rows)
    active_after = _after(active, baseline_ts, "timestamp")
    stale_after = _after(stale, baseline_ts, "timestamp")
    open_after = [row for row in after_rows if str(row.get("status") or "").upper() == "OPEN"]
    settled_after = [
        row for row in after_rows
        if str(row.get("status") or "").upper() in {"SETTLED", "FORCED_CLOSE"}
    ]
    clean_after = [row for row in after_rows if is_clean_proof_row(row)]
    raw_open_rows = [row for row in rows if str(row.get("status") or "").upper() == "OPEN"]
    return {
        "total_rows": len(rows),
        "all_rows": rows,
        "after_baseline_rows": len(after_rows),
        "after_rows": after_rows,
        "entry_open_rows_after_baseline": len(open_after),
        "open_rows_after_baseline": len(open_after),
        "settled_rows_after_baseline": len(settled_after),
        "clean_rows_after_baseline": len(clean_after),
        "active_open_rows_after_baseline": len(active_after),
        "stale_open_rows_after_baseline": len(stale_after),
        "raw_open_rows": len(raw_open_rows),
        "active_open_rows": len(active),
        "stale_open_rows": len(stale),
        "latest_timestamp": _latest_ts(rows, "timestamp"),
        "latest_after_timestamp": _latest_ts(after_rows, "timestamp"),
        "latest_settled_at": _latest_ts(rows, "settled_at"),
    }


def _bridge_status(
    funnel: dict[str, Any],
    paper: dict[str, Any],
    matched_any_opened: int,
    matched_after_opened: int,
    unmatched_any_opened: list[dict[str, Any]],
    max_open_false_block: bool,
) -> str:
    if unmatched_any_opened:
        return "TRADE_OPENED_NOT_WRITTEN"
    if max_open_false_block:
        return "MAX_OPEN_FALSE_BLOCK"
    if funnel["after_baseline_rows"] and paper["entry_open_rows_after_baseline"] == 0:
        reasons = set(funnel["final_reason_counts"])
        if matched_any_opened > matched_after_opened:
            reasons.discard("TRADE_OPENED")
        if reasons and reasons.issubset(VALID_BLOCKERS):
            return "CANDIDATES_BLOCKED_VALIDLY"
        return "FUNNEL_ACTIVE_PAPER_STALE"
    if funnel["after_baseline_rows"] and paper["entry_open_rows_after_baseline"] > 0:
        if matched_any_opened >= funnel["trade_opened"]:
            return "BRIDGE_HEALTHY"
    return "UNKNOWN_BRIDGE_FAILURE"


def _bridge_summary(funnel: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    matched_after_opened, unmatched_after_opened = _match_opened_events(
        funnel["opened_rows"],
        paper["after_rows"],
    )
    matched_any_opened, unmatched_any_opened = _match_opened_events(
        funnel["opened_rows"],
        paper["all_rows"],
    )
    passed = funnel["passed_to_paper_trader"]
    entry_writes = paper["entry_open_rows_after_baseline"]
    write_ratio = (entry_writes / passed) if passed else None
    paper_stale_after_funnel = (
        funnel["after_baseline_rows"] > 0
        and paper["after_baseline_rows"] == 0
        and (
            paper["latest_timestamp"] is None
            or (
                funnel["latest_after_timestamp"] is not None
                and funnel["latest_after_timestamp"] > paper["latest_timestamp"]
            )
        )
    )
    max_open_false_block = bool(funnel["suspicious_max_blocks"])
    status = _bridge_status(
        funnel=funnel,
        paper=paper,
        matched_any_opened=matched_any_opened,
        matched_after_opened=matched_after_opened,
        unmatched_any_opened=unmatched_any_opened,
        max_open_false_block=max_open_false_block,
    )
    return {
        "status": status,
        "funnel_rows_after_baseline": funnel["after_baseline_rows"],
        "paper_rows_after_baseline": paper["after_baseline_rows"],
        "paper_entry_writes_after_baseline": entry_writes,
        "trade_opened_after_baseline": funnel["trade_opened"],
        "matched_trade_opened_after_baseline": matched_after_opened,
        "unmatched_trade_opened_after_baseline": len(unmatched_after_opened),
        "matched_trade_opened_any_paper": matched_any_opened,
        "unmatched_trade_opened_any_paper": len(unmatched_any_opened),
        "trade_opened_boundary_matches": max(0, matched_any_opened - matched_after_opened),
        "passed_to_paper_trader_after_baseline": passed,
        "paper_write_to_pass_ratio": write_ratio,
        "paper_stale_after_funnel": paper_stale_after_funnel,
        "trade_opened_not_written": bool(unmatched_any_opened),
        "unmatched_examples": [
            {
                "timestamp_utc": row.get("timestamp_utc"),
                "ticker": row.get("ticker"),
                "executed_action": row.get("executed_action"),
                "final_reason": row.get("final_reason"),
            }
            for row in unmatched_any_opened[:5]
        ],
    }


def _ghost_summary(funnel: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    max_open_setting = MAX_CONCURRENT_OPEN_TRADES
    slots = max(0, max_open_setting - paper["active_open_rows"])
    raw_full = paper["raw_open_rows"] >= max_open_setting
    active_full = paper["active_open_rows"] >= max_open_setting
    suspicious_after = len(funnel["suspicious_max_blocks"])
    zero_logged_open_blocks = [
        row for row in funnel["blocked_max_rows"]
        if int(_as_float(row.get("open_count_before")) or 0) == 0
    ]
    false_block_after = bool(suspicious_after)
    likely_raw_open_bug = raw_full and not active_full and false_block_after
    if false_block_after:
        status = "MAX_OPEN_FALSE_BLOCK"
    elif funnel["blocked_max_open"] > 0:
        status = "MAX_OPEN_BLOCKS_APPEAR_VALID_AT_TIME"
    elif raw_full and not active_full:
        status = "GHOST_OPEN_PRESENT_NOT_BLOCKING"
    elif active_full:
        status = "ACTIVE_OPEN_CAP_FULL"
    else:
        status = "GHOST_OPEN_CLEAR"
    return {
        "status": status,
        "active_open_count": paper["active_open_rows"],
        "stale_open_count": paper["stale_open_rows"],
        "raw_open_count": paper["raw_open_rows"],
        "max_open_setting": max_open_setting,
        "current_open_slots": slots,
        "raw_open_rows_exceed_cap": raw_full,
        "active_open_rows_exceed_cap": active_full,
        "blocked_max_open_after_baseline": funnel["blocked_max_open"],
        "blocked_max_open_total": funnel["total_blocked_max_open"],
        "suspicious_max_open_blocks_after_baseline": suspicious_after,
        "blocked_max_open_while_logged_open_zero": bool(zero_logged_open_blocks),
        "blocked_max_open_while_current_active_zero": bool(
            funnel["blocked_max_open"] > 0 and paper["active_open_rows"] == 0
        ),
        "likely_raw_open_instead_of_active_open_bug": likely_raw_open_bug,
        "code_path": (
            "Dashboard.background_scan -> call_paper_trader_with_trace -> "
            "PaperTrader.process_signal -> _sync_open_trades_from_log -> "
            "len(self.open_trades) >= MAX_CONCURRENT_OPEN_TRADES"
        ),
    }


def _arb_summary(
    scanner_rows: list[dict[str, Any]],
    funnel_rows: list[dict[str, Any]],
    paper_rows: list[dict[str, Any]],
    baseline_ts: datetime | None,
) -> dict[str, Any]:
    scanner_after = _after(scanner_rows, baseline_ts, "timestamp_utc", "timestamp")
    funnel_after = _after(funnel_rows, baseline_ts, "timestamp_utc", "timestamp")
    scanner_arb = [row for row in scanner_after if str(row.get("scanner_action") or "").upper() == "ARB"]
    funnel_arb = [row for row in funnel_after if str(row.get("scanner_action") or "").upper() == "ARB"]
    paper_arb = [
        row for row in _after(paper_rows, baseline_ts, "timestamp")
        if str(row.get("action") or "").upper() == "ARB"
    ]
    reason_counts = Counter(str(row.get("final_reason") or "UNKNOWN") for row in funnel_arb)
    if not scanner_arb and not funnel_arb and not paper_arb:
        status = "NOT_ENOUGH_EVIDENCE"
    elif funnel_arb and any(row.get("paper_trade_opened") for row in funnel_arb) and paper_arb:
        status = "ARB_OPENED_AND_WRITTEN"
    elif funnel_arb and any(row.get("paper_trade_opened") for row in funnel_arb) and not paper_arb:
        status = "ARB_OPENED_NOT_WRITTEN"
    elif funnel_arb:
        status = "ARB_BLOCKED_BEFORE_OPEN"
    else:
        status = "ARB_SCANNER_ONLY_NOT_IN_FUNNEL"
    return {
        "status": status,
        "scanner_arb_after_baseline": len(scanner_arb),
        "funnel_arb_after_baseline": len(funnel_arb),
        "arb_passed_to_paper_trader_after_baseline": sum(1 for row in funnel_arb if row.get("passed_to_paper_trader") is True),
        "arb_trade_opened_after_baseline": sum(1 for row in funnel_arb if row.get("paper_trade_opened") is True),
        "paper_arb_rows_after_baseline": len(paper_arb),
        "final_reason_counts": dict(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        "latest_arb_funnel_timestamp": _fmt_ts(_latest_ts(funnel_arb, "timestamp_utc", "timestamp")),
        "examples": [
            {
                "timestamp_utc": row.get("timestamp_utc"),
                "ticker": row.get("ticker"),
                "final_reason": row.get("final_reason"),
                "passed_to_paper_trader": row.get("passed_to_paper_trader"),
            }
            for row in funnel_arb[-5:]
        ],
    }


def _safety_summary() -> dict[str, Any]:
    return {
        "trading_mode": TRADING_MODE,
        "paper_only": TRADING_MODE == "PAPER",
        "real_money_allowed": False,
        "scale_allowed": False,
        "kelly_execution_disabled": bool(GLOBAL_FORCED_LEARNING_MODE),
        "data_collection_override_enabled": bool(DATA_COLLECTION_OVERRIDE_ENABLED),
        "kxeth_quarantine_active": any(str(p).upper() == "KXETH" for p in QUARANTINED_TICKER_PREFIXES),
        "quarantined_prefixes": list(QUARANTINED_TICKER_PREFIXES),
    }


def _phase_9s_files() -> list[str]:
    candidates: list[Path] = []
    for directory in (ROOT / "tools", ROOT / "archive"):
        if directory.exists():
            candidates.extend(path for path in directory.glob("*9[Ss]*") if path.is_file())
    return sorted(str(path.relative_to(ROOT)) for path in candidates)


def build_bridge_state(
    *,
    funnel_path: Path = FUNNEL_LOG,
    trades_path: Path = TRADES_LOG,
    scanner_path: Path = SCANNER_LOG,
    baseline_snapshot: dict[str, Any] | None = None,
    scanner_tail_bytes: int = 50_000_000,
) -> dict[str, Any]:
    baseline = dict(BASELINE_SNAPSHOT if baseline_snapshot is None else baseline_snapshot)
    baseline_ts = _parse_ts(baseline.get("last_timestamp"))
    funnel_rows, funnel_malformed = _read_jsonl(Path(funnel_path))
    paper_rows, paper_malformed = _read_jsonl(Path(trades_path))
    scanner_rows, scanner_malformed, scanner_tail_limited = _read_jsonl_tail(
        Path(scanner_path),
        max_bytes=scanner_tail_bytes,
    )

    funnel = _funnel_summary(funnel_rows, baseline_ts)
    paper = _paper_summary(paper_rows, baseline_ts)
    bridge = _bridge_summary(funnel, paper)
    ghost = _ghost_summary(funnel, paper)
    arb = _arb_summary(scanner_rows, funnel_rows, paper_rows, baseline_ts)
    safety = _safety_summary()

    live_patch_allowed = False
    if bridge["status"] == "TRADE_OPENED_NOT_WRITTEN":
        bottleneck = "paper_trade_write_confirmation_failure"
        next_fix = "audit TradeLogger.log_trade exception path and add explicit write confirmation logging"
    elif bridge["status"] == "MAX_OPEN_FALSE_BLOCK":
        bottleneck = "ghost_open_max_open_blocker"
        next_fix = "make the max-open blocker consume active-open classification only"
    elif bridge["status"] == "CANDIDATES_BLOCKED_VALIDLY":
        bottleneck = "candidate_quality_and_council_filters"
        next_fix = "keep collecting; do not weaken thresholds or council"
    elif bridge["paper_stale_after_funnel"]:
        bottleneck = "funnel_active_but_no_openable_candidates"
        next_fix = "restart Dashboard/settle loop if expected, then wait for candidates that pass unchanged gates"
    else:
        bottleneck = "unknown_bridge_state"
        next_fix = "inspect latest Dashboard stdout and PaperTrader trace around the newest funnel rows"

    return {
        "baseline": baseline,
        "baseline_ts": baseline_ts,
        "paths": {
            "funnel": str(funnel_path),
            "paper_trades": str(trades_path),
            "scanner": str(scanner_path),
        },
        "malformed": {
            "funnel": funnel_malformed,
            "paper_trades": paper_malformed,
            "scanner": scanner_malformed,
        },
        "scanner_tail_limited": scanner_tail_limited,
        "scanner_tail_bytes": scanner_tail_bytes,
        "funnel": funnel,
        "paper": paper,
        "bridge": bridge,
        "ghost": ghost,
        "arb": arb,
        "safety": safety,
        "phase_9s_files": _phase_9s_files(),
        "live_patch_allowed": live_patch_allowed,
        "operational_bottleneck": bottleneck,
        "exact_next_operational_fix": next_fix,
    }


def _print_counts(counts: dict[str, int]) -> None:
    if not counts:
        print("  (none)")
        return
    for key, value in counts.items():
        print(f"  {key:<34} {value}")


def render_report(state: dict[str, Any]) -> None:
    funnel = state["funnel"]
    paper = state["paper"]
    bridge = state["bridge"]
    ghost = state["ghost"]
    arb = state["arb"]
    safety = state["safety"]

    print("=" * 94)
    print("CANDIDATE-TO-PAPER BRIDGE + GHOST OPEN BLOCKER AUDIT")
    print("=" * 94)
    print("Read-only: no logs, thresholds, gates, dashboard, strategy, or live-money state are modified.")
    print(f"Baseline last timestamp: {state['baseline'].get('last_timestamp')}")
    print(f"Baseline clean rows:     {state['baseline'].get('clean_row_count')}")
    print(f"Funnel log:              {state['paths']['funnel']}")
    print(f"Paper log:               {state['paths']['paper_trades']}")
    print(f"Scanner log:             {state['paths']['scanner']}")
    print(f"Scanner tail-limited:    {state['scanner_tail_limited']} ({state['scanner_tail_bytes']} bytes max)")

    print()
    print("EXECUTION FUNNEL AFTER BASELINE")
    print("-" * 94)
    print(f"  total funnel rows:               {funnel['total_rows']}")
    print(f"  rows after baseline:             {funnel['after_baseline_rows']}")
    print(f"  passed_to_paper_trader:          {funnel['passed_to_paper_trader']}")
    print(f"  paper_trader_received:           {funnel['paper_trader_received']}")
    print(f"  TRADE_OPENED rows after baseline:{funnel['trade_opened']}")
    print(f"  BLOCKED_MAX_OPEN after baseline: {funnel['blocked_max_open']}")
    print(f"  BLOCKED_MAX_OPEN total:          {funnel['total_blocked_max_open']}")
    print(f"  latest funnel timestamp:         {_fmt_ts(funnel['latest_timestamp'])}")
    print(f"  latest after-baseline funnel:    {_fmt_ts(funnel['latest_after_timestamp'])}")
    print("  final_reason counts after baseline:")
    _print_counts(funnel["final_reason_counts"])

    print()
    print("PAPER TRADES AFTER BASELINE")
    print("-" * 94)
    print(f"  total paper rows:                {paper['total_rows']}")
    print(f"  raw paper rows after baseline:   {paper['after_baseline_rows']}")
    print(f"  entry OPEN rows after baseline:  {paper['entry_open_rows_after_baseline']}")
    print(f"  clean rows after baseline:       {paper['clean_rows_after_baseline']}")
    print(f"  open rows after baseline:        {paper['open_rows_after_baseline']}")
    print(f"  settled rows after baseline:     {paper['settled_rows_after_baseline']}")
    print(f"  active open rows after baseline: {paper['active_open_rows_after_baseline']}")
    print(f"  stale open rows after baseline:  {paper['stale_open_rows_after_baseline']}")
    print(f"  latest paper timestamp:          {_fmt_ts(paper['latest_timestamp'])}")
    print(f"  latest settled_at timestamp:     {_fmt_ts(paper['latest_settled_at'])}")

    print()
    print("BRIDGE MISMATCH")
    print("-" * 94)
    print(f"  bridge_status:                   {bridge['status']}")
    print(f"  funnel rows after baseline:      {bridge['funnel_rows_after_baseline']}")
    print(f"  paper rows after baseline:       {bridge['paper_rows_after_baseline']}")
    print(f"  paper entry writes after base:   {bridge['paper_entry_writes_after_baseline']}")
    print(f"  TRADE_OPENED after baseline:     {bridge['trade_opened_after_baseline']}")
    print(f"  matched TRADE_OPENED:            {bridge['matched_trade_opened_after_baseline']}")
    print(f"  unmatched TRADE_OPENED:          {bridge['unmatched_trade_opened_after_baseline']}")
    print(f"  matched TRADE_OPENED any paper:  {bridge['matched_trade_opened_any_paper']}")
    print(f"  unmatched TRADE_OPENED any paper:{bridge['unmatched_trade_opened_any_paper']}")
    print(f"  baseline-boundary matches:       {bridge['trade_opened_boundary_matches']}")
    print(f"  write/pass ratio:                {_fmt_ratio(bridge['paper_write_to_pass_ratio'])}")
    print(f"  paper stale after funnel:        {bridge['paper_stale_after_funnel']}")
    print(f"  trade opened not written:        {bridge['trade_opened_not_written']}")
    if bridge["unmatched_examples"]:
        print("  unmatched examples:")
        for row in bridge["unmatched_examples"]:
            print(f"    {row}")

    print()
    print("GHOST-OPEN BLOCKER")
    print("-" * 94)
    print(f"  ghost_status:                    {ghost['status']}")
    print(f"  raw OPEN rows:                   {ghost['raw_open_count']}")
    print(f"  active open count:               {ghost['active_open_count']}")
    print(f"  stale open count:                {ghost['stale_open_count']}")
    print(f"  max open setting:                {ghost['max_open_setting']}")
    print(f"  current open slots:              {ghost['current_open_slots']}")
    print(f"  raw OPEN rows exceed cap:        {ghost['raw_open_rows_exceed_cap']}")
    print(f"  active OPEN rows exceed cap:     {ghost['active_open_rows_exceed_cap']}")
    print(f"  suspicious max-open blocks:      {ghost['suspicious_max_open_blocks_after_baseline']}")
    print(f"  max-open while logged open zero: {ghost['blocked_max_open_while_logged_open_zero']}")
    print(f"  max-open while current active 0: {ghost['blocked_max_open_while_current_active_zero']}")
    print(f"  likely raw-open counter bug:     {ghost['likely_raw_open_instead_of_active_open_bug']}")
    print(f"  responsible code path:           {ghost['code_path']}")

    print()
    print("ARB HANDLING")
    print("-" * 94)
    print(f"  arb_status:                      {arb['status']}")
    if state["scanner_tail_limited"]:
        print("  scanner evidence note:           scanner log is tail-limited due to file size")
    print(f"  scanner ARB after baseline:      {arb['scanner_arb_after_baseline']}")
    print(f"  funnel ARB after baseline:       {arb['funnel_arb_after_baseline']}")
    print(f"  ARB passed_to_paper_trader:      {arb['arb_passed_to_paper_trader_after_baseline']}")
    print(f"  ARB trade opened:                {arb['arb_trade_opened_after_baseline']}")
    print(f"  paper ARB rows after baseline:   {arb['paper_arb_rows_after_baseline']}")
    print(f"  latest ARB funnel timestamp:     {arb['latest_arb_funnel_timestamp']}")
    print("  ARB final_reason counts:")
    _print_counts(arb["final_reason_counts"])

    print()
    print("SAFETY LOCKS")
    print("-" * 94)
    print(f"  trading_mode:                    {safety['trading_mode']}")
    print(f"  paper_only:                      {safety['paper_only']}")
    print(f"  real_money_allowed:              {safety['real_money_allowed']}")
    print(f"  scale_allowed:                   {safety['scale_allowed']}")
    print(f"  kelly_execution_disabled:        {safety['kelly_execution_disabled']}")
    print(f"  dc_override_enabled:             {safety['data_collection_override_enabled']}")
    print(f"  KXETH quarantine active:         {safety['kxeth_quarantine_active']}")
    print(f"  quarantined prefixes:            {safety['quarantined_prefixes']}")

    print()
    print("OPERATIONAL VERDICT")
    print("-" * 94)
    print(f"  live_patch_allowed:              {state['live_patch_allowed']}")
    print(f"  operational_bottleneck:          {state['operational_bottleneck']}")
    print(f"  exact_next_operational_fix:      {state['exact_next_operational_fix']}")
    if state["phase_9s_files"]:
        print(f"  Phase 9S simulation files:       {state['phase_9s_files']}")
    else:
        print("  Phase 9S simulation files:       ABSENT")
    print("  fake_progress_now:               lowering gates, raising caps, deleting OPEN rows, or counting blocked rows as proof")
    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> None:
    render_report(build_bridge_state())


if __name__ == "__main__":
    main()
