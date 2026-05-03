#!/usr/bin/env python3
"""
Read-only external data coverage planner for paper-trade research.

This tool inspects paper trades plus existing raw external data and prints the
next safest Kalshi/crypto fetch targets. It does not fetch data, place orders,
mutate logs, or change proof/trading behavior.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PAPER_TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
KALSHI_DIR = ROOT / "data" / "external" / "kalshi"
CRYPTO_DIR = ROOT / "data" / "external" / "crypto"

RELEVANT_STATUSES = {"SETTLED", "FORCED_CLOSE", "OPEN"}
SETTLED_STATUSES = {"SETTLED", "FORCED_CLOSE"}
DEFAULT_WINDOW_PAD_SECONDS = 15 * 60
DEFAULT_LIMIT = 20


def _load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_epoch(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def _fmt_ts(epoch: Optional[int]) -> str:
    if epoch is None:
        return "n/a"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _iter_data_files(path: Path) -> Iterable[Path]:
    if not path.exists():
        return []
    return [p for p in path.rglob("*.json") if p.is_file()]


def _market_prefix(ticker: str) -> str:
    text = str(ticker or "").upper()
    if text.startswith("KXBTCD"):
        return "KXBTCD"
    if text.startswith("KXBTC15M") or "BTC15M" in text:
        return "BTC15M"
    if text.startswith("KXETHD"):
        return "KXETHD"
    if text.startswith("KXETH15M") or "ETH15M" in text:
        return "ETH15M"
    if text.startswith("KXXRPD") or text.startswith("KXXRP15M"):
        return "XRP"
    if text.startswith("KXSOL"):
        return "SOL"
    if text.startswith("KXDOGE"):
        return "DOGE"
    return "OTHER"


def _crypto_symbol_for_prefix(prefix: str) -> Optional[str]:
    if prefix in ("KXBTCD", "BTC15M"):
        return "BTCUSDT"
    if prefix in ("KXETHD", "ETH15M"):
        return "ETHUSDT"
    return None


def _infer_series_ticker(ticker: str) -> Optional[str]:
    text = str(ticker or "").upper()
    if not text.startswith("KX"):
        return None
    for prefix in ("KXBTCD", "KXETHD", "KXBTC15M", "KXETH15M", "KXXRP15M", "KXXRPD", "KXSOL", "KXDOGE"):
        if text.startswith(prefix):
            return prefix
    return text.split("-", 1)[0] if "-" in text else text


def _trade_entry_epoch(row: dict) -> Optional[int]:
    return _parse_epoch(row.get("timestamp") or row.get("timestamp_utc"))


def _trade_settlement_epoch(row: dict) -> Optional[int]:
    return _parse_epoch(row.get("settled_at") or row.get("result_time") or row.get("close_time"))


def _trade_window(row: dict, pad_seconds: int) -> Tuple[Optional[int], Optional[int]]:
    entry = _trade_entry_epoch(row)
    settlement = _trade_settlement_epoch(row)
    if entry is None and settlement is None:
        return None, None
    start = min(ts for ts in (entry, settlement) if ts is not None) - pad_seconds
    end = max(ts for ts in (entry, settlement) if ts is not None) + pad_seconds
    if end <= start:
        end = start + 3600
    return start, end


def _merge_windows(windows: List[Tuple[int, int]], gap_seconds: int = 3600) -> List[Tuple[int, int]]:
    if not windows:
        return []
    sorted_windows = sorted(windows)
    merged = [sorted_windows[0]]
    for start, end in sorted_windows[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + gap_seconds:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _window_covered(coverage: List[Tuple[int, int]], start: Optional[int], end: Optional[int]) -> bool:
    if start is None or end is None:
        return False
    return any(cov_start <= start and cov_end >= end for cov_start, cov_end in coverage)


def _load_kalshi_coverage() -> Dict[str, List[Tuple[int, int]]]:
    coverage: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
    for path in _iter_data_files(KALSHI_DIR):
        wrapper = _read_json(path)
        if not wrapper:
            continue
        ticker = str(wrapper.get("ticker") or "")
        payload = wrapper.get("payload")
        candles = payload.get("candlesticks") if isinstance(payload, dict) else None
        timestamps: List[int] = []
        if isinstance(candles, list):
            for candle in candles:
                if isinstance(candle, dict):
                    ts = _as_int(candle.get("end_period_ts"))
                    if ts is not None:
                        timestamps.append(ts)
        start = min(timestamps) if timestamps else _as_int(wrapper.get("start_ts"))
        end = max(timestamps) if timestamps else _as_int(wrapper.get("end_ts"))
        if ticker and start is not None and end is not None:
            coverage[ticker].append((start, end))
    return {ticker: _merge_windows(windows) for ticker, windows in coverage.items()}


def _load_crypto_coverage() -> Dict[str, List[Tuple[int, int]]]:
    coverage: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
    for path in _iter_data_files(CRYPTO_DIR):
        wrapper = _read_json(path)
        if not wrapper:
            continue
        symbol = str(wrapper.get("symbol_requested") or "").upper()
        source = str(wrapper.get("source") or "").lower()
        payload = wrapper.get("payload")
        timestamps: List[int] = []
        if source == "kraken" and isinstance(payload, dict):
            result = payload.get("result")
            if isinstance(result, dict):
                for key, candles in result.items():
                    if key == "last" or not isinstance(candles, list):
                        continue
                    for candle in candles:
                        if isinstance(candle, list) and candle:
                            ts = _as_int(candle[0])
                            if ts is not None:
                                timestamps.append(ts)
        elif source == "binance" and isinstance(payload, list):
            for candle in payload:
                if isinstance(candle, list) and candle:
                    ts_ms = _as_int(candle[0])
                    if ts_ms is not None:
                        timestamps.append(ts_ms // 1000)
        if symbol and timestamps:
            coverage[symbol].append((min(timestamps), max(timestamps)))
    return {symbol: _merge_windows(windows) for symbol, windows in coverage.items()}


def _build_trade_records(trades: List[dict], include_open: bool, pad_seconds: int) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    allowed_statuses = RELEVANT_STATUSES if include_open else SETTLED_STATUSES
    for row in trades:
        status = str(row.get("status") or "")
        if status not in allowed_statuses:
            continue
        ticker = str(row.get("ticker") or "")
        if not ticker:
            continue
        start, end = _trade_window(row, pad_seconds)
        prefix = _market_prefix(ticker)
        records.append({
            "ticker": ticker,
            "status": status,
            "series_ticker": _infer_series_ticker(ticker),
            "market_prefix": prefix,
            "crypto_symbol": _crypto_symbol_for_prefix(prefix),
            "entry_epoch": _trade_entry_epoch(row),
            "settlement_epoch": _trade_settlement_epoch(row),
            "start_ts": start,
            "end_ts": end,
        })
    return records


def _group_ticker_targets(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for record in records:
        ticker = record["ticker"]
        target = grouped.setdefault(ticker, {
            "ticker": ticker,
            "series_ticker": record["series_ticker"],
            "market_prefix": record["market_prefix"],
            "crypto_symbol": record["crypto_symbol"],
            "trade_count": 0,
            "settled_count": 0,
            "open_count": 0,
            "windows": [],
        })
        target["trade_count"] += 1
        if record["status"] in SETTLED_STATUSES:
            target["settled_count"] += 1
        if record["status"] == "OPEN":
            target["open_count"] += 1
        if record["start_ts"] is not None and record["end_ts"] is not None:
            target["windows"].append((record["start_ts"], record["end_ts"]))
    for target in grouped.values():
        target["windows"] = _merge_windows(target["windows"])
    return grouped


def _group_crypto_targets(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for record in records:
        symbol = record["crypto_symbol"]
        if not symbol:
            continue
        target = grouped.setdefault(symbol, {"symbol": symbol, "trade_count": 0, "windows": []})
        target["trade_count"] += 1
        if record["start_ts"] is not None and record["end_ts"] is not None:
            target["windows"].append((record["start_ts"], record["end_ts"]))
    for target in grouped.values():
        target["windows"] = _merge_windows(target["windows"])
    return grouped


def _kalshi_command(ticker: str, series_ticker: Optional[str], start: int, end: int) -> str:
    parts = [
        "python3 tools/external_data/kalshi_historical_collector.py",
        f"--ticker {ticker}",
    ]
    if series_ticker:
        parts.append(f"--series-ticker {series_ticker}")
        parts.append("--endpoint-mode auto")
    else:
        parts.append("--endpoint-mode historical")
    parts.extend([
        f"--start-ts {start}",
        f"--end-ts {end}",
        "--period-minutes 1",
        "--dry-run",
    ])
    return " ".join(parts)


def _crypto_command(symbol: str, start: int, end: int) -> str:
    return (
        "python3 tools/external_data/crypto_candle_collector.py "
        f"--symbol {symbol} --interval 1m --source kraken "
        f"--start-time {start} --end-time {end} --dry-run"
    )


def _print_windows(windows: List[Tuple[int, int]], indent: str = "    ") -> None:
    if not windows:
        print(f"{indent}- none")
        return
    for start, end in windows:
        print(f"{indent}- {start} -> {end}  ({_fmt_ts(start)} to {_fmt_ts(end)})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan read-only external data coverage for paper trades.")
    parser.add_argument("--include-open", action="store_true", help="Include OPEN trades in addition to settled/forced-close trades.")
    parser.add_argument("--window-pad-minutes", type=int, default=15)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pad_seconds = max(args.window_pad_minutes, 0) * 60
    trades = _load_jsonl(PAPER_TRADES_LOG)
    records = _build_trade_records(trades, include_open=args.include_open, pad_seconds=pad_seconds)
    kalshi_coverage = _load_kalshi_coverage()
    crypto_coverage = _load_crypto_coverage()
    ticker_targets = _group_ticker_targets(records)
    crypto_targets = _group_crypto_targets(records)

    kalshi_missing: List[Tuple[str, Optional[str], int, int, int]] = []
    kalshi_covered_tickers = 0
    for ticker, target in ticker_targets.items():
        coverage = kalshi_coverage.get(ticker, [])
        windows = target["windows"]
        if windows and all(_window_covered(coverage, start, end) for start, end in windows):
            kalshi_covered_tickers += 1
            continue
        for start, end in windows:
            if not _window_covered(coverage, start, end):
                kalshi_missing.append((ticker, target["series_ticker"], start, end, target["trade_count"]))

    crypto_missing: List[Tuple[str, int, int, int]] = []
    crypto_covered_windows = 0
    crypto_total_windows = 0
    for symbol, target in crypto_targets.items():
        coverage = crypto_coverage.get(symbol, [])
        for start, end in target["windows"]:
            crypto_total_windows += 1
            if _window_covered(coverage, start, end):
                crypto_covered_windows += 1
            else:
                crypto_missing.append((symbol, start, end, target["trade_count"]))

    status_counts = Counter(str(row.get("status") or "UNKNOWN") for row in trades)
    prefix_counts = Counter(record["market_prefix"] for record in records)

    print("=" * 72)
    print("EXTERNAL DATA COVERAGE PLAN")
    print("=" * 72)
    print("Read-only planner. No network fetch, no orders, no trading/proof changes.")
    print(f"  paper_trades_exists:        {PAPER_TRADES_LOG.exists()}")
    print(f"  total_trades_scanned:       {len(trades)}")
    print(f"  settled_trades:             {sum(status_counts.get(status, 0) for status in SETTLED_STATUSES)}")
    print(f"  open_trades:                {status_counts.get('OPEN', 0)}")
    print(f"  planning_scope:             {'settled_plus_open' if args.include_open else 'settled_only'}")
    print(f"  planned_trade_rows:         {len(records)}")
    print(f"  window_pad_minutes:         {args.window_pad_minutes}")
    print(f"  unique_kalshi_tickers:      {len(ticker_targets)}")
    print(f"  tickers_with_kalshi_raw:    {sum(1 for ticker in ticker_targets if ticker in kalshi_coverage)}")
    print(f"  tickers_fully_covered:      {kalshi_covered_tickers}")
    print(f"  tickers_missing_coverage:   {len(ticker_targets) - kalshi_covered_tickers}")
    print(f"  crypto_symbols_needed:      {len(crypto_targets)}")
    print(f"  crypto_windows_covered:     {crypto_covered_windows}")
    print(f"  crypto_windows_missing:     {len(crypto_missing)}")
    print()

    print("PLANNED MARKET PREFIXES")
    print("-" * 72)
    if prefix_counts:
        for prefix, count in sorted(prefix_counts.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {prefix:<12s} {count:>5}")
    else:
        print("  (none)")
    print()

    print("EXISTING KALSHI COVERAGE")
    print("-" * 72)
    if kalshi_coverage:
        for ticker, windows in sorted(kalshi_coverage.items()):
            print(f"  {ticker}")
            _print_windows(windows, indent="    ")
    else:
        print("  (none)")
    print()

    print("EXISTING CRYPTO COVERAGE")
    print("-" * 72)
    if crypto_coverage:
        for symbol, windows in sorted(crypto_coverage.items()):
            print(f"  {symbol}")
            _print_windows(windows, indent="    ")
    else:
        print("  (none)")
    print()

    print("PRIORITY KALSHI FETCH COMMANDS")
    print("-" * 72)
    if kalshi_missing:
        for ticker, series_ticker, start, end, trade_count in sorted(kalshi_missing, key=lambda item: (-item[4], item[0], item[2]))[:args.limit]:
            print(f"  # trades={trade_count} ticker={ticker} series={series_ticker or 'UNKNOWN'}")
            print(f"  {_kalshi_command(ticker, series_ticker, start, end)}")
    else:
        print("  (none)")
    print()

    print("PRIORITY CRYPTO FETCH COMMANDS")
    print("-" * 72)
    if crypto_missing:
        for symbol, start, end, trade_count in sorted(crypto_missing, key=lambda item: (-item[3], item[0], item[1]))[:args.limit]:
            print(f"  # trades={trade_count} symbol={symbol}")
            print(f"  {_crypto_command(symbol, start, end)}")
    else:
        print("  (none)")
    print()

    print("NOTES")
    print("-" * 72)
    print("  - Commands are dry-run suggestions only.")
    print("  - Use --no-dry-run manually only after reviewing each target window.")
    print("  - Kraken --end-time is advisory in the current collector; Kraken OHLC fetches from --start-time.")
    print("  - Raw JSON remains under ignored data/external/ paths.")
    print("  - Joined CSV remains under ignored data/research/joined/ paths.")
    print("RESULT: EXTERNAL_DATA_COVERAGE_PLAN_OK")


if __name__ == "__main__":
    main()
