"""
logs/market_snapshot_logger.py
------------------------------
Research-only market snapshot logging.

M-48A records Kalshi BTC 15-minute quote snapshots for replay and
entry-window/liquidity analysis. This module is intentionally passive:
it does not approve trades, block trades, size trades, or mutate scanner
output.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = ROOT / "logs" / "btc_15m_entry_window_snapshots.jsonl"

REQUIRED_FIELDS = (
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "spread",
    "volume",
    "liquidity",
)


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _time_to_expiry_seconds(market: dict, now: datetime) -> Optional[float]:
    explicit = _as_float(market.get("time_to_expiry_seconds"))
    if explicit is not None:
        return explicit

    end_ts = (
        _parse_ts(market.get("close_time"))
        or _parse_ts(market.get("result_time"))
        or _parse_ts(market.get("expected_expiration_time"))
    )
    if end_ts is None:
        return None
    return (end_ts - now).total_seconds()


def _entry_window_bucket(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if 240 < seconds <= 300:
        return "5m_to_4m"
    if 180 < seconds <= 240:
        return "4m_to_3m"
    if 120 < seconds <= 180:
        return "3m_to_2m"
    if 60 < seconds <= 120:
        return "2m_to_1m"
    if 30 < seconds <= 60:
        return "final_60s"
    if 0 <= seconds <= 30:
        return "final_30s"
    return "outside_window"


def is_btc_15m_market(market: dict) -> tuple[bool, str]:
    """Return whether a scanner/opportunity record is safely BTC 15-minute."""
    ticker = str(market.get("ticker") or "").upper()
    title = str(market.get("title") or "").upper()
    question = str(market.get("question") or "").upper()
    series = str(market.get("series_ticker") or market.get("series") or "").upper()
    text = f"{ticker} {title} {question} {series}"

    if "KXBTC15M" in ticker or "BTC15M" in ticker or "BTC15M" in series:
        return True, "ticker_or_series_contains_btc15m"

    has_btc = any(token in text for token in ("BTC", "BITCOIN"))
    has_15m = (
        "15M" in text
        or "15-MIN" in text
        or "15 MIN" in text
        or "15-MINUTE" in text
        or "15 MINUTE" in text
    )
    if has_btc and has_15m:
        return True, "text_contains_btc_and_15m"

    return False, "not_safely_identified_as_btc15m"


def _snapshot_from_market(market: dict, now: datetime, scan_id: Optional[str]) -> dict:
    yes_bid = _as_float(market.get("yes_bid"))
    yes_ask = _as_float(market.get("yes_ask"))
    no_bid = _as_float(market.get("no_bid"))
    no_ask = _as_float(market.get("no_ask"))
    yes_mid = round((yes_bid + yes_ask) / 2, 6) if yes_bid is not None and yes_ask is not None else None
    no_mid = round((no_bid + no_ask) / 2, 6) if no_bid is not None and no_ask is not None else None

    spread = _as_float(market.get("spread"))
    if spread is None and yes_bid is not None and yes_ask is not None:
        spread = round(yes_ask - yes_bid, 6)

    volume = _as_float(market.get("volume"))
    liquidity = _as_float(market.get("liquidity"))
    if liquidity is None and volume is not None:
        liquidity = volume / 10.0

    seconds = _time_to_expiry_seconds(market, now)
    bucket = _entry_window_bucket(seconds)

    raw_time_fields_present = bool(
        market.get("close_time")
        or market.get("result_time")
        or market.get("expected_expiration_time")
        or market.get("time_to_expiry_seconds")
    )

    record = {
        "timestamp_utc": now.isoformat(),
        "scan_id": scan_id,
        "ticker": market.get("ticker"),
        "market_id": market.get("market_id"),
        "event_id": market.get("event_id"),
        "series_ticker": market.get("series_ticker") or market.get("series"),
        "title": market.get("title"),
        "question": market.get("question"),
        "close_time": market.get("close_time"),
        "result_time": market.get("result_time"),
        "expected_expiration_time": market.get("expected_expiration_time"),
        "time_to_expiry_seconds": round(seconds, 3) if seconds is not None else None,
        "entry_window_bucket": bucket,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_mid": yes_mid,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "no_mid": no_mid,
        "spread": spread,
        "volume": volume,
        "liquidity": liquidity,
        "open_interest": _as_float(market.get("open_interest")),
        "scanner_confidence": _as_float(market.get("confidence")),
        "scanner_edge": _as_float(market.get("edge")),
        "strategy": market.get("strategy") or market.get("raw_strategy"),
        "source": market.get("scanner_source") or market.get("source") or "kalshi_scanner",
        "btc_price": None,
        "btc_price_source": None,
        "btc_price_timestamp": None,
        "classification_reason": is_btc_15m_market(market)[1],
        "raw_time_fields_present": raw_time_fields_present,
    }

    missing = [field for field in REQUIRED_FIELDS if record.get(field) is None]
    if not raw_time_fields_present:
        missing.append("time_fields")
    record["missing_fields"] = sorted(set(missing))

    reject_reasons = []
    if any(record.get(k) is None for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")):
        reject_reasons.append("REJECT_MISSING_BID_ASK")
    if spread is None:
        reject_reasons.append("REJECT_MISSING_SPREAD")
    elif spread > 0.05:
        reject_reasons.append("REJECT_WIDE_SPREAD")
    if liquidity is None:
        reject_reasons.append("REJECT_MISSING_LIQUIDITY")
    elif liquidity < 1000:
        reject_reasons.append("REJECT_THIN_LIQUIDITY")
    if seconds is None:
        reject_reasons.append("REJECT_UNKNOWN_TIME_TO_EXPIRY")
    elif seconds <= 30:
        reject_reasons.append("REJECT_FINAL_30S")
    elif seconds > 300 or seconds < 60:
        reject_reasons.append("REJECT_OUTSIDE_RESEARCH_WINDOW")
    reject_reasons.append("REJECT_NO_SYNCED_BTC_PRICE")
    record["research_gate_status"] = "PASS" if not reject_reasons else "REJECT"
    record["research_reject_reasons"] = reject_reasons

    return record


def log_btc15m_snapshots(
    markets: list[dict],
    scan_id: Optional[str] = None,
    log_path: Path | str = DEFAULT_LOG_PATH,
) -> dict:
    """
    Append BTC 15-minute snapshots for the provided scanner records.

    Returns counters for observability. All exceptions are swallowed so this
    research logger cannot break dashboard scanning or paper execution.
    """
    stats = {
        "seen": len(markets or []),
        "matched": 0,
        "skipped_not_btc15m": 0,
        "written": 0,
        "errors": 0,
    }
    if not markets:
        return stats

    now = datetime.now(timezone.utc)
    path = Path(log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for market in markets:
                try:
                    matched, _reason = is_btc_15m_market(market)
                    if not matched:
                        stats["skipped_not_btc15m"] += 1
                        continue
                    stats["matched"] += 1
                    record = _snapshot_from_market(market, now, scan_id)
                    f.write(json.dumps(record, sort_keys=True) + "\n")
                    stats["written"] += 1
                except Exception as exc:
                    stats["errors"] += 1
                    print(f"[BTC15M_SNAPSHOT] skipped record: {exc}")
    except Exception as exc:
        stats["errors"] += 1
        print(f"[BTC15M_SNAPSHOT] logger failed: {exc}")
    return stats
