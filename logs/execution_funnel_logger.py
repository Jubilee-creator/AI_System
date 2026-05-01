"""
logs/execution_funnel_logger.py
-------------------------------
Passive execution-funnel logger.

Records how non-PASS scanner opportunities move through Dashboard and
PaperTrader. This is observability only and must not affect execution.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "execution_funnel.jsonl"


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _short_text(value: Any, limit: int = 500) -> Optional[str]:
    if value is None:
        return None
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _final_reason(trace_text: str, trade: Optional[Dict[str, Any]]) -> str:
    trace = trace_text or ""
    if trade:
        return "TRADE_OPENED"
    if "global open-trades cap" in trace or "max_open_trades_reached" in trace:
        return "BLOCKED_MAX_OPEN_TRADES"
    if "duplicate ticker guard" in trace:
        return "BLOCKED_DUPLICATE_TICKER"
    if "market quality filter" in trace or "[FILTER] skipped" in trace:
        return "BLOCKED_MARKET_QUALITY"
    if "edge danger guard" in trace:
        return "BLOCKED_EDGE_DANGER_GUARD"
    if "min edge block" in trace or "blocked: below min edge" in trace:
        return "BLOCKED_MIN_EDGE"
    if "stop: council block" in trace or "[COUNCIL] BLOCKED:" in trace:
        return "BLOCKED_COUNCIL"
    if "risk decision=BLOCK" in trace or "Trade blocked by risk manager" in trace:
        return "BLOCKED_RISK"
    if "trader disabled" in trace:
        return "BLOCKED_TRADER_DISABLED"
    if "confidence below learning floor" in trace:
        return "BLOCKED_CONFIDENCE"
    return "BLOCKED_OR_SKIPPED_UNKNOWN"


def _council_decision(trace_text: str, trade: Optional[Dict[str, Any]]) -> Optional[str]:
    if trade and trade.get("council_decision") is not None:
        return str(trade.get("council_decision"))
    trace = trace_text or ""
    if "DATA_COLLECTION_OVERRIDE" in trace:
        return "BLOCK_OVERRIDDEN_DATA_COLLECTION"
    if "BOOTSTRAP_PROVISIONAL" in trace or "[COUNCIL] PROVISIONAL" in trace:
        return "PROVISIONAL"
    if "[COUNCIL] BLOCKED:" in trace or "stop: council block" in trace:
        return "BLOCK"
    if "[COUNCIL] ALLOW:" in trace:
        return "ALLOW"
    if "[COUNCIL] ERROR:" in trace:
        return "ERROR"
    return None


def log_execution_funnel(
    opportunity: Dict[str, Any],
    scan_id: Optional[str],
    run_id: Optional[str],
    trace_text: str,
    trade: Optional[Dict[str, Any]],
    trace_counts: Optional[Dict[str, Any]] = None,
    rank_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append one non-PASS opportunity funnel row. Fail-soft by design."""
    scanner_action = str(opportunity.get("action") or "UNKNOWN").upper()
    executed_action = None
    if trade:
        executed_action = trade.get("executed_action") or trade.get("action")
    intended_action = scanner_action if scanner_action else None
    mismatch = (
        bool(intended_action and executed_action)
        and str(intended_action).upper() != str(executed_action).upper()
    )
    trace_counts = trace_counts or {}
    rank_context = rank_context or {}
    final_reason = _final_reason(trace_text, trade)

    row = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id or "UNKNOWN_RUN",
        "scan_id": scan_id,
        "ticker": opportunity.get("ticker"),
        "opportunity_rank": rank_context.get("opportunity_rank"),
        "scan_non_pass_rank": rank_context.get("scan_non_pass_rank"),
        "scanner_action": scanner_action,
        "confidence": _safe_float(opportunity.get("confidence")),
        "edge": _safe_float(opportunity.get("edge")),
        "open_slots_before": rank_context.get("open_slots_before"),
        "open_count_before": rank_context.get("open_count_before"),
        "max_open_trades": rank_context.get("max_open_trades"),
        "cap_already_full": rank_context.get("cap_already_full"),
        "first_bet_no_rank_in_scan": rank_context.get("first_bet_no_rank_in_scan"),
        "first_bet_yes_rank_in_scan": rank_context.get("first_bet_yes_rank_in_scan"),
        "dashboard_seen": True,
        "dashboard_skip_reason": None,
        "passed_to_paper_trader": True,
        "paper_trader_received": "[TRACE] entering paper_trader" in (trace_text or ""),
        "intended_action": intended_action,
        "executed_action": executed_action,
        "handoff_action_mismatch": mismatch if executed_action else None,
        "risk_allowed": trade.get("risk_manager_approved_initially") if trade else None,
        "risk_block_reason": trade.get("risk_manager_block_reason") if trade else None,
        "council_decision": _council_decision(trace_text, trade),
        "council_reason": _short_text(trade.get("council_reason")) if trade else None,
        "paper_trade_opened": bool(trade),
        "final_status": "TRADE_OPENED" if trade else "NO_TRADE",
        "final_reason": final_reason,
        "market_filter_blocked": int(trace_counts.get("market_filter_blocked", 0) or 0),
        "council_blocked": int(trace_counts.get("council_blocked", 0) or 0),
        "council_overridden": int(trace_counts.get("council_overridden", 0) or 0),
        "risk_blocked": int(trace_counts.get("risk_blocked", 0) or 0),
        "other_blocked": int(trace_counts.get("other_blocked", 0) or 0),
        "trace_excerpt": _short_text(trace_text),
    }

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as exc:
        return {"written": 0, "errors": 1, "error": exc.__class__.__name__}

    return {"written": 1, "errors": 0, "path": str(LOG_PATH)}
