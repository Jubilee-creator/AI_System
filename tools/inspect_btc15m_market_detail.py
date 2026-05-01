#!/usr/bin/env python3
"""
tools/inspect_btc15m_market_detail.py
--------------------------------------
M-48D Phase 1 — Read-only Kalshi API field inspector.

Fetches one live BTC 15-minute market detail response and prints every
returned field so we can confirm which field(s) contain the reference
BTC price (the price at contract-open that determines YES/NO resolution).

This script does NOT:
  - trade anything
  - write to any log or state file
  - modify scanner, risk, or council behaviour

Run:
  python3 tools/inspect_btc15m_market_detail.py

Expected output tells us which of these candidate fields holds the
reference price:
  subtitle / yes_sub_title / no_sub_title
  rules_primary / rules_secondary / rules_source
  floor_strike / cap_strike / floor_price / cap_price
  result_sources[].targets[].threshold
  question
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from brokers.kalshi_client import kalshi_get


BTC15M_SERIES = "KXBTC15M"


def _fetch_open_btc15m_tickers(limit: int = 5) -> list[str]:
    """Return up to `limit` open BTC 15-minute market tickers."""
    path = f"/markets?series_ticker={BTC15M_SERIES}&status=open&limit={limit}"
    data = kalshi_get(path)
    if not data or "markets" not in data:
        return []
    return [m["ticker"] for m in data["markets"] if m.get("ticker")]


def _print_all_fields(d: dict, indent: int = 0) -> None:
    prefix = "  " * indent
    for k, v in sorted(d.items()):
        if isinstance(v, dict):
            print(f"{prefix}{k}:")
            _print_all_fields(v, indent + 1)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            print(f"{prefix}{k}: [{len(v)} items]")
            for i, item in enumerate(v[:3]):
                print(f"{prefix}  [{i}]:")
                _print_all_fields(item, indent + 2)
        else:
            print(f"{prefix}{k}: {v!r}")


def _highlight_threshold_candidates(d: dict) -> None:
    """Print only the fields most likely to contain the reference price."""
    candidates = [
        "subtitle", "yes_sub_title", "no_sub_title",
        "rules_primary", "rules_secondary", "rules_source",
        "floor_strike", "cap_strike", "floor_price", "cap_price",
        "question", "description",
        "result_sources", "settlement_source",
        "open_time", "open_date",
    ]
    print("\n── THRESHOLD CANDIDATE FIELDS ─────────────────────────────────")
    found_any = False
    for key in candidates:
        val = d.get(key)
        if val is not None:
            found_any = True
            if isinstance(val, (dict, list)):
                print(f"  {key}: {json.dumps(val, indent=2)}")
            else:
                print(f"  {key}: {val!r}")
    if not found_any:
        print("  (none of the candidate fields were present in this response)")


def main() -> None:
    print("M-48D PHASE 1 — BTC15M Market Detail Inspector")
    print("=" * 60)
    print("Read-only. No trades, no writes, no state changes.\n")

    tickers = _fetch_open_btc15m_tickers(limit=5)
    if not tickers:
        print("[ERROR] No open BTC15M markets found via /markets endpoint.")
        print("        Check API credentials or market hours.")
        sys.exit(1)

    print(f"Found {len(tickers)} open BTC15M ticker(s): {tickers}")

    ticker = tickers[0]
    print(f"\nInspecting: {ticker}")
    print("-" * 60)

    detail = kalshi_get(f"/markets/{ticker}")
    if not detail:
        print(f"[ERROR] No detail response for {ticker}")
        sys.exit(1)

    market = detail.get("market") or detail

    print("\n── ALL FIELDS (raw) ────────────────────────────────────────")
    _print_all_fields(market)

    _highlight_threshold_candidates(market)

    print("\n── RESOLUTION SUMMARY ──────────────────────────────────────")
    print(f"  ticker:     {market.get('ticker')}")
    print(f"  title:      {market.get('title')}")
    print(f"  close_time: {market.get('close_time')}")
    print(f"  result_time:{market.get('result_time') or market.get('expected_expiration_time')}")
    print(f"  status:     {market.get('status')}")
    print(f"  yes_bid:    {market.get('yes_bid')}")
    print(f"  yes_ask:    {market.get('yes_ask')}")

    print("\n[INFO] If 'subtitle' or 'rules_primary' shows a BTC reference price,")
    print("       that field is the source for M-48D threshold capture.")
    print("[INFO] If no field contains the reference price, the fallback is to")
    print("       fetch Coinbase BTC/USD at close_time - 15 minutes via historical API.")
    print("=" * 60)


if __name__ == "__main__":
    main()
