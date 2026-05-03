#!/usr/bin/env python3
"""
Read-only public crypto candle collector skeleton.

Dry-run is enabled by default. Non-dry-run uses Binance public klines and saves
raw JSON under data/external/crypto/{symbol}/{interval}/. It never trades.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT_ROOT = ROOT / "data" / "external" / "crypto"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_url(args: argparse.Namespace) -> str:
    params: Dict[str, Any] = {
        "symbol": args.symbol.upper(),
        "interval": args.interval,
        "limit": args.limit,
    }
    if args.start_time:
        params["startTime"] = args.start_time
    if args.end_time:
        params["endTime"] = args.end_time
    return BINANCE_KLINES_URL + "?" + urllib.parse.urlencode(params)


def _fetch_json(url: str) -> Optional[Any]:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"  network_or_parse_failure: {exc}")
        return None


def _save_raw(symbol: str, interval: str, payload: Any) -> Path:
    out_dir = OUT_ROOT / symbol.upper() / interval
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_utc_stamp()}_binance_klines_raw.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only crypto candle collector skeleton.")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--start-time", default=None)
    parser.add_argument("--end-time", default=None)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    url = _build_url(args)

    print("=" * 72)
    print("CRYPTO CANDLE COLLECTOR")
    print("=" * 72)
    print("Read-only. Public market data only. No orders. No trading behavior changes.")
    print(f"  symbol:      {args.symbol.upper()}")
    print(f"  interval:    {args.interval}")
    print(f"  start_time:  {args.start_time or 'n/a'}")
    print(f"  end_time:    {args.end_time or 'n/a'}")
    print(f"  limit:       {args.limit}")
    print(f"  dry_run:     {args.dry_run}")
    print(f"  would_get:   {url}")

    if args.dry_run:
        print("  action:      dry run only; no network request and no file write")
        print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
        return

    payload = _fetch_json(url)
    if payload is None:
        print("  action:      fetch failed gracefully; no file written")
        print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
        return

    out_path = _save_raw(args.symbol, args.interval, payload)
    print(f"  saved_raw_json: {out_path}")
    print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")


if __name__ == "__main__":
    main()
