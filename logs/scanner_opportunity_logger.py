"""
logs/scanner_opportunity_logger.py
----------------------------------
Passive scanner opportunity logger.

Records raw scanner outputs before PaperTrader sees them. This is intentionally
read-only observability: it must not mutate opportunities or affect execution.
"""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "scanner_opportunities.jsonl"


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


def _family(ticker: Any) -> Optional[str]:
    if not ticker:
        return None
    ticker_str = str(ticker)
    return ticker_str.split("-", 1)[0] if "-" in ticker_str else ticker_str


def _short_text(value: Any, limit: int = 240) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _row_from_opportunity(
    opportunity: Dict[str, Any],
    timestamp_utc: str,
    scan_id: Optional[str],
    run_id: Optional[str],
    source: str,
) -> Dict[str, Any]:
    action = str(opportunity.get("action") or "UNKNOWN").upper()
    yes_bid = opportunity.get("yes_bid")
    yes_ask = opportunity.get("yes_ask", opportunity.get("price_yes"))
    no_bid = opportunity.get("no_bid")
    no_ask = opportunity.get("no_ask", opportunity.get("price_no"))

    return {
        "timestamp_utc": timestamp_utc,
        "scanner_timestamp": opportunity.get("timestamp"),
        "run_id": run_id or "UNKNOWN_RUN",
        "scan_id": scan_id,
        "source": source,
        "ticker": opportunity.get("ticker"),
        "title": opportunity.get("title"),
        "market_family": _family(opportunity.get("ticker")),
        "scanner_action": action,
        "confidence": _safe_float(opportunity.get("confidence")),
        "edge": _safe_float(opportunity.get("edge")),
        "kelly_frac": _safe_float(opportunity.get("kelly_frac")),
        "bet_size": _safe_float(opportunity.get("bet_size")),
        "price_yes": _safe_float(opportunity.get("price_yes")),
        "price_no": _safe_float(opportunity.get("price_no")),
        "yes_bid": _safe_float(yes_bid),
        "yes_ask": _safe_float(yes_ask),
        "yes_mid": _mid(yes_bid, yes_ask),
        "no_bid": _safe_float(no_bid),
        "no_ask": _safe_float(no_ask),
        "no_mid": _mid(no_bid, no_ask),
        "yes_plus_no": _safe_float(opportunity.get("yes_plus_no")),
        "arb_edge": _safe_float(opportunity.get("arb_edge")),
        "z_score": _safe_float(opportunity.get("z_score")),
        "volume": _safe_float(opportunity.get("volume")),
        "close_time": opportunity.get("close_time"),
        "result_time": opportunity.get("result_time"),
        "reasoning": _short_text(opportunity.get("reasoning")),
        "pass_reason": _short_text(opportunity.get("reasoning")) if action == "PASS" else None,
    }


def log_scanner_opportunities(
    opportunities: Iterable[Dict[str, Any]],
    scan_id: Optional[str] = None,
    run_id: Optional[str] = None,
    source: str = "unknown",
) -> Dict[str, Any]:
    """
    Append scanner opportunities to JSONL and return small stats.

    This function is fail-soft by design. Any internal error is returned in the
    stats payload instead of being raised into the scanner/dashboard loop.
    """
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    seen = 0
    written = 0
    errors = 0
    action_counts: Counter = Counter()

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as handle:
            for opportunity in opportunities:
                seen += 1
                try:
                    row = _row_from_opportunity(
                        opportunity=opportunity,
                        timestamp_utc=timestamp_utc,
                        scan_id=scan_id,
                        run_id=run_id,
                        source=source,
                    )
                    action_counts[row.get("scanner_action") or "UNKNOWN"] += 1
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                    written += 1
                except Exception:
                    errors += 1
    except Exception:
        errors += 1

    return {
        "seen": seen,
        "written": written,
        "errors": errors,
        "action_counts": dict(action_counts),
        "path": str(LOG_PATH),
    }
