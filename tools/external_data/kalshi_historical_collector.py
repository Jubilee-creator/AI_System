#!/usr/bin/env python3
"""
Read-only Kalshi historical/live market data collector skeleton.

Dry-run is enabled by default. Non-dry-run performs GET-only requests and
writes raw JSON under data/external/kalshi/{ticker}/. No order endpoints exist
or are called here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT_ROOT = ROOT / "data" / "external" / "kalshi"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _has_auth_config() -> bool:
    key_id = os.getenv("KALSHI_API_KEY_ID", "")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", str(ROOT / "kalshi_private_key.pem"))
    return bool(key_id) and Path(key_path).exists()


def _build_path(args: argparse.Namespace) -> str:
    params = []
    if args.series_ticker:
        params.append(f"series_ticker={args.series_ticker}")
    if args.start_ts:
        params.append(f"min_ts={args.start_ts}")
    if args.end_ts:
        params.append(f"max_ts={args.end_ts}")
    params.append(f"period_interval={args.period_minutes}")
    query = "&".join(params)
    suffix = f"?{query}" if query else ""
    return f"/markets/{args.ticker}/candlesticks{suffix}"


def _save_raw(ticker: str, payload: Dict[str, Any]) -> Path:
    out_dir = OUT_ROOT / ticker
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_utc_stamp()}_kalshi_raw.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return out_path


def _fetch(path: str) -> Optional[dict]:
    try:
        from brokers.kalshi_client import kalshi_get
    except Exception as exc:
        print(f"  Kalshi client unavailable: {exc}")
        return None

    try:
        return kalshi_get(path, silent=True)
    except Exception as exc:
        print(f"  Kalshi GET failed gracefully: {exc}")
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Kalshi historical collector skeleton.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--series-ticker", default=None)
    parser.add_argument("--period-minutes", type=int, default=1)
    parser.add_argument("--start-ts", default=None)
    parser.add_argument("--end-ts", default=None)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = _build_path(args)
    auth_ok = _has_auth_config()

    print("=" * 72)
    print("KALSHI HISTORICAL COLLECTOR")
    print("=" * 72)
    print("Read-only. GET-only. No orders. No trading behavior changes.")
    print(f"  ticker:          {args.ticker}")
    print(f"  series_ticker:   {args.series_ticker or 'n/a'}")
    print(f"  period_minutes:  {args.period_minutes}")
    print(f"  start_ts:        {args.start_ts or 'n/a'}")
    print(f"  end_ts:          {args.end_ts or 'n/a'}")
    print(f"  dry_run:         {args.dry_run}")
    print(f"  auth_config_ok:  {auth_ok}")
    print(f"  would_get:       {path}")

    if args.dry_run:
        print("  action:          dry run only; no API request and no file write")
        print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")
        return

    if not auth_ok:
        print("  action:          skipped fetch; missing Kalshi API key id or private key path")
        print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")
        return

    payload = _fetch(path)
    if payload is None:
        print("  action:          fetch unavailable or returned no payload; no file written")
        print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")
        return

    out_path = _save_raw(args.ticker, payload)
    print(f"  saved_raw_json:  {out_path}")
    print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")


if __name__ == "__main__":
    main()
