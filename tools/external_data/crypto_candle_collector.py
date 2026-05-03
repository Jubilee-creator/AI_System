#!/usr/bin/env python3
"""
Read-only public crypto candle collector skeleton.

Dry-run is enabled by default. Non-dry-run uses public market-data endpoints and
saves raw JSON under data/external/crypto/{source}/{symbol}/{interval}/.
It never trades.
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
from typing import Any, Dict, Optional, Tuple


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT_ROOT = ROOT / "data" / "external" / "crypto"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

KRAKEN_SYMBOLS = {
    "BTCUSDT": "XBTUSD",
    "BTCUSD": "XBTUSD",
    "ETHUSDT": "ETHUSD",
    "ETHUSD": "ETHUSD",
}

KRAKEN_INTERVALS = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "4h": "240",
    "1d": "1440",
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fetched_at_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_binance_url(args: argparse.Namespace) -> str:
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


def _build_kraken_url(args: argparse.Namespace) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    symbol = args.symbol.upper()
    mapped_symbol = KRAKEN_SYMBOLS.get(symbol)
    mapped_interval = KRAKEN_INTERVALS.get(args.interval)
    if mapped_symbol is None:
        return None, None, mapped_interval, f"unsupported_kraken_symbol={symbol}"
    if mapped_interval is None:
        return None, mapped_symbol, None, f"unsupported_kraken_interval={args.interval}"

    params: Dict[str, Any] = {
        "pair": mapped_symbol,
        "interval": mapped_interval,
    }
    if args.start_time:
        params["since"] = args.start_time
    return KRAKEN_OHLC_URL + "?" + urllib.parse.urlencode(params), mapped_symbol, mapped_interval, None


def _fetch_json(url: str) -> Tuple[Optional[Any], Optional[str]]:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw), None
    except urllib.error.HTTPError as exc:
        return None, f"http_error_{exc.code}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return None, f"network_or_parse_failure: {exc}"


def _wrap_payload(
    source: str,
    symbol_requested: str,
    symbol_mapped: str,
    interval_requested: str,
    interval_mapped: str,
    payload: Any,
) -> Dict[str, Any]:
    return {
        "source": source,
        "symbol_requested": symbol_requested.upper(),
        "symbol_mapped": symbol_mapped,
        "interval_requested": interval_requested,
        "interval_mapped": interval_mapped,
        "fetched_at_utc": _fetched_at_utc(),
        "payload": payload,
    }


def _save_raw(source: str, symbol: str, interval: str, payload: Dict[str, Any]) -> Path:
    out_dir = OUT_ROOT / source / symbol.upper() / interval
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_utc_stamp()}_{source}_raw.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only crypto candle collector skeleton.")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--start-time", default=None)
    parser.add_argument("--end-time", default=None)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--source", choices=("binance", "kraken", "auto"), default="auto")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    return parser.parse_args()


def _try_binance(args: argparse.Namespace) -> Tuple[Optional[str], Optional[str]]:
    url = _build_binance_url(args)
    payload, error = _fetch_json(url)
    if error:
        return None, error
    wrapped = _wrap_payload(
        source="binance",
        symbol_requested=args.symbol,
        symbol_mapped=args.symbol.upper(),
        interval_requested=args.interval,
        interval_mapped=args.interval,
        payload=payload,
    )
    out_path = _save_raw("binance", args.symbol, args.interval, wrapped)
    return str(out_path), None


def _try_kraken(args: argparse.Namespace) -> Tuple[Optional[str], Optional[str]]:
    url, mapped_symbol, mapped_interval, build_error = _build_kraken_url(args)
    if build_error:
        return None, build_error
    assert url is not None
    assert mapped_symbol is not None
    assert mapped_interval is not None
    payload, error = _fetch_json(url)
    if error:
        return None, error
    wrapped = _wrap_payload(
        source="kraken",
        symbol_requested=args.symbol,
        symbol_mapped=mapped_symbol,
        interval_requested=args.interval,
        interval_mapped=mapped_interval,
        payload=payload,
    )
    out_path = _save_raw("kraken", args.symbol, args.interval, wrapped)
    return str(out_path), None


def main() -> None:
    args = parse_args()
    binance_url = _build_binance_url(args)
    kraken_url, kraken_symbol, kraken_interval, kraken_build_error = _build_kraken_url(args)

    print("=" * 72)
    print("CRYPTO CANDLE COLLECTOR")
    print("=" * 72)
    print("Read-only. Public market data only. No orders. No trading behavior changes.")
    print(f"  symbol:      {args.symbol.upper()}")
    print(f"  interval:    {args.interval}")
    print(f"  source:      {args.source}")
    print(f"  start_time:  {args.start_time or 'n/a'}")
    print(f"  end_time:    {args.end_time or 'n/a'}")
    print(f"  limit:       {args.limit}")
    print(f"  dry_run:     {args.dry_run}")
    print(f"  binance_url: {binance_url if args.source in ('binance', 'auto') else 'n/a'}")
    if kraken_build_error:
        kraken_display = f"n/a ({kraken_build_error})"
    else:
        kraken_display = str(kraken_url)
    print(f"  kraken_url:  {kraken_display if args.source in ('kraken', 'auto') else 'n/a'}")
    print(f"  fallback:    {'binance_to_kraken' if args.source == 'auto' else 'disabled'}")
    if kraken_symbol or kraken_interval:
        print(f"  kraken_map:  symbol={kraken_symbol or 'n/a'} interval={kraken_interval or 'n/a'}")

    if args.dry_run:
        print("  action:      dry run only; no network request and no file write")
        print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
        return

    if args.source == "binance":
        out_path, error = _try_binance(args)
        if error:
            print(f"  binance_error: {error}")
            print("  action:        binance failed gracefully; no fallback because source=binance")
            print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
            return
        print(f"  source_used:   binance")
        print(f"  saved_raw_json: {out_path}")
        print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
        return

    if args.source == "kraken":
        out_path, error = _try_kraken(args)
        if error:
            print(f"  kraken_error:  {error}")
            print("  action:        kraken failed gracefully; no file written")
            print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
            return
        print(f"  source_used:   kraken")
        print(f"  saved_raw_json: {out_path}")
        print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
        return

    out_path, error = _try_binance(args)
    if out_path:
        print("  source_used:   binance")
        print(f"  saved_raw_json: {out_path}")
        print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
        return

    print(f"  binance_error: {error}")
    print("  fallback:      attempting kraken")
    out_path, kraken_error = _try_kraken(args)
    if out_path:
        print("  source_used:   kraken")
        print(f"  saved_raw_json: {out_path}")
        print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")
        return

    print(f"  kraken_error:  {kraken_error}")
    print("  action:        all configured sources failed gracefully; no file written")
    print("RESULT: CRYPTO_CANDLE_COLLECTOR_OK")


if __name__ == "__main__":
    main()
