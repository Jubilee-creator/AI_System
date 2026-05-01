"""
brokers/underlying_price_client.py
----------------------------------
Read-only underlying price snapshots for research logging.

M-48B intentionally does not feed prices into trade execution. It provides
one fail-soft BTC/USD spot snapshot per scan so Kalshi quote snapshots can
later be replayed against a synchronized underlying reference.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests


COINBASE_BTC_USD_TICKER_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
DEFAULT_TIMEOUT_SECONDS = 2.5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(
    price: Optional[float],
    source: str,
    latency_ms: float,
    error: Optional[str] = None,
    symbol: str = "BTC-USD",
    timestamp_utc: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "price": price,
        "source": source,
        "timestamp_utc": timestamp_utc or _now_iso(),
        "fetch_latency_ms": round(latency_ms, 3),
        "error": error,
    }


def fetch_btc_usd_price(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """
    Fetch one BTC/USD spot snapshot from Coinbase Exchange public ticker.

    Returns a stable dict whether the request succeeds or fails. Exceptions
    are swallowed so callers can use this in dashboard/scanner loops safely.
    """
    started = time.perf_counter()
    source = "coinbase_exchange_public_ticker"
    try:
        response = requests.get(
            COINBASE_BTC_USD_TICKER_URL,
            headers={"User-Agent": "AI_System research logger"},
            timeout=timeout,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            return _snapshot(
                price=None,
                source=source,
                latency_ms=latency_ms,
                error=f"http_status_{response.status_code}",
            )

        payload = response.json()
        raw_price = payload.get("price")
        if raw_price is None:
            return _snapshot(
                price=None,
                source=source,
                latency_ms=latency_ms,
                error="missing_price",
            )

        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return _snapshot(
                price=None,
                source=source,
                latency_ms=latency_ms,
                error=f"invalid_price:{raw_price}",
            )

        return _snapshot(
            price=price,
            source=source,
            latency_ms=latency_ms,
            timestamp_utc=_now_iso(),
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return _snapshot(
            price=None,
            source=source,
            latency_ms=latency_ms,
            error=exc.__class__.__name__,
        )


def main() -> None:
    print(json.dumps(fetch_btc_usd_price(), sort_keys=True))


if __name__ == "__main__":
    main()
