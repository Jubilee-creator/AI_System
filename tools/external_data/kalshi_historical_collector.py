#!/usr/bin/env python3
"""
Read-only Kalshi historical/live market data collector skeleton.

Dry-run is enabled by default. Non-dry-run performs GET-only requests and
writes raw JSON under data/external/kalshi/{endpoint_mode}/{ticker}/{period}m/.
No order endpoints exist or are called here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT_ROOT = ROOT / "data" / "external" / "kalshi"
VALID_PERIODS = {1, 60, 1440}
FORBIDDEN_PATH_MARKERS = (
    "/orders",
    "/portfolio/orders",
    "/exchange/orders",
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fetched_at_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_auth_config() -> bool:
    key_id = os.getenv("KALSHI_API_KEY_ID", "")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", str(ROOT / "kalshi_private_key.pem"))
    return bool(key_id) and Path(key_path).exists()


def _query(args: argparse.Namespace) -> str:
    params = [
        f"start_ts={args.start_ts}",
        f"end_ts={args.end_ts}",
        f"period_interval={args.period_minutes}",
    ]
    return "&".join(params)


def _historical_path(args: argparse.Namespace) -> str:
    return f"/historical/markets/{args.ticker}/candlesticks?{_query(args)}"


def _live_path(args: argparse.Namespace) -> Optional[str]:
    if not args.series_ticker:
        return None
    return f"/series/{args.series_ticker}/markets/{args.ticker}/candlesticks?{_query(args)}"


def _candidate_paths(args: argparse.Namespace) -> List[Tuple[str, str]]:
    if args.endpoint_mode == "historical":
        return [("historical", _historical_path(args))]
    if args.endpoint_mode == "live":
        live = _live_path(args)
        return [("live", live)] if live else []
    paths: List[Tuple[str, str]] = []
    live = _live_path(args)
    if live:
        paths.append(("live", live))
    paths.append(("historical", _historical_path(args)))
    return paths


def _path_is_safe(path: str) -> bool:
    lowered = path.lower()
    if any(marker in lowered for marker in FORBIDDEN_PATH_MARKERS):
        return False
    if "/candlesticks" not in lowered:
        return False
    return lowered.startswith("/series/") or lowered.startswith("/historical/markets/")


def _wrap_payload(endpoint_mode: str, args: argparse.Namespace, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": "kalshi",
        "endpoint_mode": endpoint_mode,
        "ticker": args.ticker,
        "series_ticker": args.series_ticker,
        "period_interval": args.period_minutes,
        "start_ts": int(args.start_ts),
        "end_ts": int(args.end_ts),
        "fetched_at_utc": _fetched_at_utc(),
        "path": path,
        "payload": payload,
    }


def _save_raw(endpoint_mode: str, ticker: str, period_minutes: int, payload: Dict[str, Any]) -> Path:
    out_dir = OUT_ROOT / endpoint_mode / ticker / f"{period_minutes}m"
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
    parser.add_argument("--endpoint-mode", choices=("live", "historical", "auto"), default="auto")
    parser.add_argument("--period-minutes", type=int, choices=sorted(VALID_PERIODS), default=1)
    parser.add_argument("--start-ts", default=None)
    parser.add_argument("--end-ts", default=None)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    return parser.parse_args()


def _validation_warnings(args: argparse.Namespace) -> List[str]:
    warnings: List[str] = []
    if args.start_ts is None or args.end_ts is None:
        warnings.append("non-dry-run requires start_ts and end_ts")
    else:
        for label, raw in (("start_ts", args.start_ts), ("end_ts", args.end_ts)):
            try:
                int(raw)
            except (TypeError, ValueError):
                warnings.append(f"{label} must be an integer Unix timestamp")
    if args.endpoint_mode == "live" and not args.series_ticker:
        warnings.append("live mode requires --series-ticker")
    if args.endpoint_mode == "auto" and not args.series_ticker:
        warnings.append("auto mode without --series-ticker will use historical only; live needs series_ticker")
    return warnings


def _can_fetch(args: argparse.Namespace) -> bool:
    warnings = _validation_warnings(args)
    fatal = [
        warning for warning in warnings
        if warning != "auto mode without --series-ticker will use historical only; live needs series_ticker"
    ]
    return not fatal


def _fetch_one(endpoint_mode: str, path: str, args: argparse.Namespace) -> Optional[Path]:
    if not _path_is_safe(path):
        print(f"  safety_refusal: unsafe_non_candlestick_path mode={endpoint_mode} path={path}")
        return None

    payload = _fetch(path)
    if payload is None:
        print(f"  {endpoint_mode}_fetch: no payload")
        return None

    wrapped = _wrap_payload(endpoint_mode, args, path, payload)
    return _save_raw(endpoint_mode, args.ticker, args.period_minutes, wrapped)


def main() -> None:
    args = parse_args()
    candidate_paths = _candidate_paths(args)
    warnings = _validation_warnings(args)
    auth_ok = _has_auth_config()

    print("=" * 72)
    print("KALSHI HISTORICAL COLLECTOR")
    print("=" * 72)
    print("Read-only. GET-only. No orders. No trading behavior changes.")
    print(f"  ticker:           {args.ticker}")
    print(f"  series_ticker:    {args.series_ticker or 'n/a'}")
    print(f"  endpoint_mode:    {args.endpoint_mode}")
    print(f"  period_interval:  {args.period_minutes}")
    print(f"  start_ts:         {args.start_ts or 'n/a'}")
    print(f"  end_ts:           {args.end_ts or 'n/a'}")
    print(f"  dry_run:          {args.dry_run}")
    print(f"  auth_config_ok:   {auth_ok}")
    if warnings:
        print("  validation_warnings:")
        for warning in warnings:
            print(f"    - {warning}")
    print("  candidate_paths:")
    if candidate_paths:
        for endpoint_mode, path in candidate_paths:
            safe = _path_is_safe(path)
            print(f"    - {endpoint_mode}: {path}  safe={safe}")
    else:
        print("    - none")

    if args.dry_run:
        print("  action:           dry run only; no API request and no file write")
        print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")
        return

    if not _can_fetch(args):
        print("  action:           skipped fetch; invalid non-dry-run arguments")
        print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")
        return

    if not auth_ok:
        print("  action:           skipped fetch; missing Kalshi API key id or private key path")
        print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")
        return

    for endpoint_mode, path in candidate_paths:
        out_path = _fetch_one(endpoint_mode, path, args)
        if out_path is not None:
            print(f"  endpoint_used:    {endpoint_mode}")
            print(f"  saved_raw_json:   {out_path}")
            print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")
            return
        if args.endpoint_mode != "auto":
            break

    print("  action:           fetch unavailable or returned no payload; no file written")
    if args.endpoint_mode == "auto" and args.series_ticker:
        print("  fallback:         live failed or unavailable; historical also failed or unavailable")
    elif args.endpoint_mode == "auto":
        print("  fallback:         historical-only auto mode used because series_ticker is missing")
    print("RESULT: KALSHI_HISTORICAL_COLLECTOR_OK")


if __name__ == "__main__":
    main()
