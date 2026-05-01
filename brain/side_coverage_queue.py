"""
brain/side_coverage_queue.py
----------------------------
Shadow-only side coverage diagnostics.

This module never calls PaperTrader and never executes trades. It inspects the
natural scanner opportunity list and records which BET_NO candidate would be
selected for SIDE_BALANCED_RESEARCH if a later phase enables execution.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config.trading_config import (
    SIDE_BALANCED_RESEARCH_ENABLED,
    SIDE_BALANCED_RESEARCH_EXECUTE,
    SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE,
    SIDE_BALANCED_RESEARCH_SHADOW_ONLY,
)


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "side_coverage_shadow.jsonl"
COVERAGE_MODE = "SIDE_BALANCED_RESEARCH"


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mid(bid: Any, ask: Any) -> Optional[float]:
    bid_float = _safe_float(bid)
    ask_float = _safe_float(ask)
    if bid_float is None or ask_float is None:
        return None
    return round((bid_float + ask_float) / 2.0, 6)


def _action(opportunity: Dict[str, Any]) -> str:
    return str(opportunity.get("action") or "").upper()


def _empty_row(
    *,
    run_id: Optional[str],
    scan_id: Optional[str],
    final_reason: str,
    selected_reason: str,
) -> Dict[str, Any]:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id or "UNKNOWN_RUN",
        "scan_id": scan_id,
        "coverage_mode": COVERAGE_MODE,
        "shadow_only": True,
        "side_coverage_test": True,
        "proof_eligible": False,
        "data_collection_override": True,
        "normal_strategy_trade": False,
        "ticker": None,
        "scanner_action": None,
        "intended_action": None,
        "original_rank": None,
        "side_queue_rank": None,
        "confidence": None,
        "edge": None,
        "price_yes": None,
        "price_no": None,
        "no_bid": None,
        "no_ask": None,
        "no_mid": None,
        "selected_for_side_coverage_reason": selected_reason,
        "final_reason": final_reason,
        "would_execute": False,
        "risk_allowed": None,
        "risk_block_reason": None,
        "council_decision": None,
        "council_reason": None,
    }


def select_shadow_candidate(
    opportunities: Iterable[Dict[str, Any]],
    *,
    run_id: Optional[str],
    scan_id: Optional[str],
    open_count: Optional[int] = None,
    max_open_trades: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Return one shadow diagnostic row for the highest-ranked natural BET_NO.

    Selection is based on the scanner-provided order. PASS rows are ignored,
    BET_YES rows are never inverted, and no synthetic opportunity is created.
    """
    if not SIDE_BALANCED_RESEARCH_ENABLED:
        return _empty_row(
            run_id=run_id,
            scan_id=scan_id,
            final_reason="SIDE_COVERAGE_DISABLED",
            selected_reason="disabled_by_config",
        )

    if SIDE_BALANCED_RESEARCH_EXECUTE:
        return _empty_row(
            run_id=run_id,
            scan_id=scan_id,
            final_reason="SIDE_COVERAGE_EXECUTION_NOT_IMPLEMENTED",
            selected_reason="execute_flag_true_but_phase_5m_is_shadow_only",
        )

    rows: List[Dict[str, Any]] = list(opportunities)
    bet_no_seen = 0
    selected = None
    selected_original_rank = None
    selected_side_rank = None

    for original_rank, opportunity in enumerate(rows, start=1):
        action = _action(opportunity)
        if action == "BET_NO":
            bet_no_seen += 1
            if selected is None:
                selected = opportunity
                selected_original_rank = original_rank
                selected_side_rank = bet_no_seen

    if selected is None:
        return _empty_row(
            run_id=run_id,
            scan_id=scan_id,
            final_reason="SIDE_COVERAGE_NO_BET_NO_AVAILABLE",
            selected_reason="no_natural_bet_no_in_scan",
        )

    price_no = _safe_float(selected.get("price_no"))
    no_ask = _safe_float(selected.get("no_ask", selected.get("price_no")))
    no_bid = _safe_float(selected.get("no_bid"))
    no_mid = _mid(no_bid, no_ask)

    missing_fields = []
    for key in ("ticker", "confidence", "edge"):
        if selected.get(key) in (None, ""):
            missing_fields.append(key)
    if missing_fields:
        final_reason = "SIDE_COVERAGE_MISSING_FIELDS"
        selected_reason = "missing_fields=" + ",".join(missing_fields)
    elif price_no is None or price_no <= 0 or price_no >= 1:
        final_reason = "SIDE_COVERAGE_INVALID_NO_PRICE"
        selected_reason = "invalid_natural_no_price"
    elif open_count is not None and max_open_trades is not None and open_count >= max_open_trades:
        final_reason = "SIDE_COVERAGE_CAP_FULL"
        selected_reason = "natural_bet_no_found_but_cap_full"
    else:
        final_reason = "SIDE_COVERAGE_SHADOW_ONLY"
        selected_reason = "highest_ranked_natural_bet_no_shadow_selected"

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id or "UNKNOWN_RUN",
        "scan_id": scan_id,
        "coverage_mode": COVERAGE_MODE,
        "shadow_only": True,
        "side_coverage_test": True,
        "proof_eligible": bool(SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE),
        "data_collection_override": True,
        "normal_strategy_trade": False,
        "ticker": selected.get("ticker"),
        "scanner_action": "BET_NO",
        "intended_action": "BET_NO",
        "original_rank": selected_original_rank,
        "side_queue_rank": selected_side_rank,
        "confidence": _safe_float(selected.get("confidence")),
        "edge": _safe_float(selected.get("edge")),
        "price_yes": _safe_float(selected.get("price_yes")),
        "price_no": price_no,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "no_mid": no_mid,
        "selected_for_side_coverage_reason": selected_reason,
        "final_reason": final_reason,
        "would_execute": False,
        "risk_allowed": None,
        "risk_block_reason": None,
        "council_decision": None,
        "council_reason": None,
    }


def log_shadow_diagnostic(row: Dict[str, Any]) -> Dict[str, Any]:
    """Append one shadow diagnostic row. Fail-soft; never affects scanning."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as exc:
        return {"written": 0, "errors": 1, "error": exc.__class__.__name__}
    return {"written": 1, "errors": 0, "path": str(LOG_PATH)}

