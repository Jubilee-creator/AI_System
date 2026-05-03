#!/usr/bin/env python3
"""
Read-only external research dataset joiner skeleton.

Reads paper trades and checks for external Kalshi/crypto inputs. It writes a
joined dataset only when enough external inputs exist. It never mutates logs or
changes proof/trading behavior.
"""
from __future__ import annotations

import bisect
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PAPER_TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
KALSHI_DIR = ROOT / "data" / "external" / "kalshi"
CRYPTO_DIR = ROOT / "data" / "external" / "crypto"
JOINED_DIR = ROOT / "data" / "research" / "joined"
MAX_NEAREST_CANDLE_AGE_SECONDS = 3600


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


def _parse_ts(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return str(value)


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_epoch(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _market_prefix(ticker: str) -> str:
    text = str(ticker or "").upper()
    if text.startswith("KXBTCD"):
        return "KXBTCD"
    if "BTC15M" in text:
        return "BTC15M"
    if text.startswith("KXETHD"):
        return "KXETHD"
    if "ETH15M" in text:
        return "ETH15M"
    if text.startswith("KXXRP"):
        return "XRP"
    if text.startswith("KXSOL"):
        return "SOL"
    if text.startswith("KXDOGE"):
        return "DOGE"
    return "OTHER"


def _crypto_symbol_for_market_prefix(prefix: str) -> Optional[str]:
    if prefix in ("KXBTCD", "BTC15M"):
        return "BTCUSDT"
    if prefix in ("KXETHD", "ETH15M"):
        return "ETHUSDT"
    return None


def _side(row: dict) -> str:
    for key in ("executed_action", "intended_action", "scanner_action", "action"):
        if row.get(key):
            return str(row[key]).upper()
    return "UNKNOWN"


def _outcome(row: dict) -> Optional[str]:
    pnl = _as_float(row.get("pnl"))
    if pnl is None:
        return None
    if pnl > 0:
        return "WIN"
    if pnl < 0:
        return "LOSS"
    return "PUSH"


def _roi(row: dict) -> Optional[float]:
    pnl = _as_float(row.get("pnl"))
    size = _as_float(row.get("size"))
    if pnl is None or not size:
        return None
    return pnl / size


def _proof_class(row: dict) -> str:
    if row.get("side_coverage_test"):
        return "SIDE_COVERAGE_EXCLUDED"
    if row.get("data_collection_override"):
        return "DATA_COLLECTION_OVERRIDE_EXCLUDED"
    if row.get("bootstrap_provisional"):
        return "BOOTSTRAP_PROVISIONAL_EXCLUDED"
    if row.get("bootstrap_era_council_allow"):
        return "BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL"
    if row.get("risk_edge") is not None and row.get("model_probability") is not None:
        return "NORMAL_MODERN_CANDIDATE"
    return "LEGACY_OR_INCOMPLETE"


def _external_inputs_available() -> bool:
    kalshi_files = [p for p in KALSHI_DIR.rglob("*") if p.is_file() and p.name != ".gitkeep"] if KALSHI_DIR.exists() else []
    crypto_files = [p for p in CRYPTO_DIR.rglob("*") if p.is_file() and p.name != ".gitkeep"] if CRYPTO_DIR.exists() else []
    return bool(kalshi_files and crypto_files)


def _mid_from_bid_ask(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def _normalize_kalshi_candle(wrapper: dict, candle: dict, source_path: Path) -> Optional[Dict[str, Any]]:
    end_ts = _as_int(candle.get("end_period_ts"))
    if end_ts is None:
        return None

    yes_bid = candle.get("yes_bid") if isinstance(candle.get("yes_bid"), dict) else {}
    yes_ask = candle.get("yes_ask") if isinstance(candle.get("yes_ask"), dict) else {}
    price = candle.get("price") if isinstance(candle.get("price"), dict) else {}
    bid_close = _as_float(yes_bid.get("close_dollars"))
    ask_close = _as_float(yes_ask.get("close_dollars"))
    yes_mid = _mid_from_bid_ask(bid_close, ask_close)

    return {
        "source_path": str(source_path),
        "ticker": str(wrapper.get("ticker") or wrapper.get("payload", {}).get("ticker") or ""),
        "endpoint_mode": wrapper.get("endpoint_mode"),
        "period_interval": _as_int(wrapper.get("period_interval")),
        "end_ts": end_ts,
        "yes_bid_close": bid_close,
        "yes_ask_close": ask_close,
        "yes_mid_close": yes_mid,
        "no_mid_close": (1.0 - yes_mid) if yes_mid is not None else None,
        "market_price_close": _as_float(price.get("close_dollars") or price.get("previous_dollars")),
        "volume": _as_float(candle.get("volume_fp")),
        "open_interest": _as_float(candle.get("open_interest_fp")),
    }


def _load_kalshi_candles() -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    by_ticker: Dict[str, List[Dict[str, Any]]] = {}
    stats = {"files": 0, "candles": 0, "skipped_files": 0, "skipped_candles": 0}
    if not KALSHI_DIR.exists():
        return by_ticker, stats

    for path in sorted(KALSHI_DIR.rglob("*.json")):
        wrapper = _read_json(path)
        if not wrapper:
            stats["skipped_files"] += 1
            continue
        payload = wrapper.get("payload")
        candles = payload.get("candlesticks") if isinstance(payload, dict) else None
        if not isinstance(candles, list):
            stats["skipped_files"] += 1
            continue
        stats["files"] += 1
        for candle in candles:
            if not isinstance(candle, dict):
                stats["skipped_candles"] += 1
                continue
            normalized = _normalize_kalshi_candle(wrapper, candle, path)
            if normalized is None:
                stats["skipped_candles"] += 1
                continue
            by_ticker.setdefault(normalized["ticker"], []).append(normalized)
            stats["candles"] += 1

    for candles in by_ticker.values():
        candles.sort(key=lambda item: item["end_ts"])
    return by_ticker, stats


def _normalize_kraken_candles(wrapper: dict, source_path: Path) -> Tuple[List[Dict[str, Any]], int]:
    payload = wrapper.get("payload")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return [], 0

    rows: List[Dict[str, Any]] = []
    skipped = 0
    for key, candles in result.items():
        if key == "last":
            continue
        if not isinstance(candles, list):
            skipped += 1
            continue
        for candle in candles:
            if not isinstance(candle, list) or len(candle) < 8:
                skipped += 1
                continue
            ts = _as_int(candle[0])
            close = _as_float(candle[4])
            if ts is None or close is None:
                skipped += 1
                continue
            rows.append({
                "source_path": str(source_path),
                "source": "kraken",
                "symbol_requested": str(wrapper.get("symbol_requested") or "").upper(),
                "symbol_mapped": wrapper.get("symbol_mapped"),
                "interval_requested": wrapper.get("interval_requested"),
                "interval_mapped": wrapper.get("interval_mapped"),
                "ts": ts,
                "open": _as_float(candle[1]),
                "high": _as_float(candle[2]),
                "low": _as_float(candle[3]),
                "close": close,
                "vwap": _as_float(candle[5]),
                "volume": _as_float(candle[6]),
                "count": _as_int(candle[7]),
            })
    return rows, skipped


def _normalize_binance_candles(wrapper: dict, source_path: Path) -> Tuple[List[Dict[str, Any]], int]:
    payload = wrapper.get("payload")
    if not isinstance(payload, list):
        return [], 0

    rows: List[Dict[str, Any]] = []
    skipped = 0
    for candle in payload:
        if not isinstance(candle, list) or len(candle) < 6:
            skipped += 1
            continue
        open_time_ms = _as_int(candle[0])
        close = _as_float(candle[4])
        if open_time_ms is None or close is None:
            skipped += 1
            continue
        rows.append({
            "source_path": str(source_path),
            "source": "binance",
            "symbol_requested": str(wrapper.get("symbol_requested") or "").upper(),
            "symbol_mapped": wrapper.get("symbol_mapped"),
            "interval_requested": wrapper.get("interval_requested"),
            "interval_mapped": wrapper.get("interval_mapped"),
            "ts": open_time_ms // 1000,
            "open": _as_float(candle[1]),
            "high": _as_float(candle[2]),
            "low": _as_float(candle[3]),
            "close": close,
            "vwap": None,
            "volume": _as_float(candle[5]),
            "count": None,
        })
    return rows, skipped


def _load_crypto_candles() -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    stats = {"files": 0, "candles": 0, "skipped_files": 0, "skipped_candles": 0}
    if not CRYPTO_DIR.exists():
        return by_symbol, stats

    for path in sorted(CRYPTO_DIR.rglob("*.json")):
        wrapper = _read_json(path)
        if not wrapper:
            stats["skipped_files"] += 1
            continue
        source = str(wrapper.get("source") or "").lower()
        if source == "kraken":
            rows, skipped = _normalize_kraken_candles(wrapper, path)
        elif source == "binance":
            rows, skipped = _normalize_binance_candles(wrapper, path)
        else:
            rows, skipped = [], 0
        if not rows and skipped == 0:
            stats["skipped_files"] += 1
            continue
        stats["files"] += 1
        stats["skipped_candles"] += skipped
        for row in rows:
            by_symbol.setdefault(row["symbol_requested"], []).append(row)
            stats["candles"] += 1

    for candles in by_symbol.values():
        candles.sort(key=lambda item: item["ts"])
    return by_symbol, stats


def _nearest_before(candles: List[Dict[str, Any]], ts_key: str, target_ts: int) -> Optional[Dict[str, Any]]:
    timestamps = [int(item[ts_key]) for item in candles]
    idx = bisect.bisect_right(timestamps, target_ts) - 1
    if idx < 0:
        return None
    return candles[idx]


def _return_before(candles: List[Dict[str, Any]], target_ts: int, minutes: int) -> Optional[float]:
    current = _nearest_before(candles, "ts", target_ts)
    prior = _nearest_before(candles, "ts", target_ts - minutes * 60)
    if not current or not prior:
        return None
    current_close = _as_float(current.get("close"))
    prior_close = _as_float(prior.get("close"))
    if current_close is None or not prior_close:
        return None
    return (current_close - prior_close) / prior_close


def _join_kalshi_features(ticker: str, entry_epoch: Optional[int], kalshi_by_ticker: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    empty = {
        "nearest_kalshi_candle_end_ts": None,
        "kalshi_candle_age_seconds": None,
        "kalshi_yes_bid_close": None,
        "kalshi_yes_ask_close": None,
        "kalshi_yes_mid_close": None,
        "kalshi_no_mid_close": None,
        "kalshi_market_price_close": None,
        "kalshi_volume": None,
        "kalshi_open_interest": None,
        "kalshi_join_status": "NO_MATCHING_KALSHI_TICKER",
    }
    if entry_epoch is None:
        empty["kalshi_join_status"] = "MISSING_ENTRY_TS"
        return empty
    candles = kalshi_by_ticker.get(ticker)
    if not candles:
        return empty
    nearest = _nearest_before(candles, "end_ts", entry_epoch)
    if not nearest:
        empty["kalshi_join_status"] = "NO_KALSHI_CANDLE_BEFORE_ENTRY"
        return empty
    age = entry_epoch - int(nearest["end_ts"])
    if age > MAX_NEAREST_CANDLE_AGE_SECONDS:
        empty["kalshi_join_status"] = "STALE_KALSHI_CANDLE"
    else:
        empty["kalshi_join_status"] = "JOINED"
    empty.update({
        "nearest_kalshi_candle_end_ts": nearest.get("end_ts"),
        "kalshi_candle_age_seconds": age,
        "kalshi_yes_bid_close": nearest.get("yes_bid_close"),
        "kalshi_yes_ask_close": nearest.get("yes_ask_close"),
        "kalshi_yes_mid_close": nearest.get("yes_mid_close"),
        "kalshi_no_mid_close": nearest.get("no_mid_close"),
        "kalshi_market_price_close": nearest.get("market_price_close"),
        "kalshi_volume": nearest.get("volume"),
        "kalshi_open_interest": nearest.get("open_interest"),
    })
    return empty


def _join_crypto_features(prefix: str, entry_epoch: Optional[int], crypto_by_symbol: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    symbol = _crypto_symbol_for_market_prefix(prefix)
    empty = {
        "nearest_crypto_candle_before_entry": None,
        "crypto_candle_age_seconds": None,
        "crypto_source": None,
        "crypto_symbol": symbol,
        "crypto_close_before_entry": None,
        "crypto_return_5m_before_entry": None,
        "crypto_return_15m_before_entry": None,
        "crypto_volatility_15m": None,
        "crypto_join_status": "NO_CRYPTO_SYMBOL_FOR_MARKET",
    }
    if symbol is None:
        return empty
    if entry_epoch is None:
        empty["crypto_join_status"] = "MISSING_ENTRY_TS"
        return empty
    candles = crypto_by_symbol.get(symbol)
    if not candles:
        empty["crypto_join_status"] = "NO_MATCHING_CRYPTO_DATA"
        return empty
    nearest = _nearest_before(candles, "ts", entry_epoch)
    if not nearest:
        empty["crypto_join_status"] = "NO_CRYPTO_CANDLE_BEFORE_ENTRY"
        return empty
    age = entry_epoch - int(nearest["ts"])
    if age > MAX_NEAREST_CANDLE_AGE_SECONDS:
        empty["crypto_join_status"] = "STALE_CRYPTO_CANDLE"
    else:
        empty["crypto_join_status"] = "JOINED"
    empty.update({
        "nearest_crypto_candle_before_entry": nearest.get("ts"),
        "crypto_candle_age_seconds": age,
        "crypto_source": nearest.get("source"),
        "crypto_close_before_entry": nearest.get("close"),
        "crypto_return_5m_before_entry": _return_before(candles, entry_epoch, 5),
        "crypto_return_15m_before_entry": _return_before(candles, entry_epoch, 15),
    })
    return empty


def _build_rows(
    trades: List[dict],
    kalshi_by_ticker: Dict[str, List[Dict[str, Any]]],
    crypto_by_symbol: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    joined: List[Dict[str, Any]] = []
    for idx, row in enumerate(trades, 1):
        if row.get("status") not in ("SETTLED", "FORCED_CLOSE", "OPEN"):
            continue
        ticker = str(row.get("ticker") or "")
        prefix = _market_prefix(ticker)
        entry_ts = _parse_ts(row.get("timestamp") or row.get("timestamp_utc"))
        entry_epoch = _parse_epoch(entry_ts)
        joined_row = {
            "trade_id": row.get("trade_id") or f"{ticker}:{row.get('timestamp') or row.get('timestamp_utc') or idx}",
            "ticker": ticker,
            "market_prefix": prefix,
            "side": _side(row),
            "entry_ts": entry_ts,
            "settlement_ts": _parse_ts(row.get("settled_at") or row.get("result_time")),
            "entry_price": row.get("entry_price"),
            "outcome": _outcome(row),
            "roi": _roi(row),
            "clv": row.get("clv"),
            "probability_confidence": row.get("model_probability", row.get("confidence")),
            "proof_class": _proof_class(row),
        }
        joined_row.update(_join_crypto_features(prefix, entry_epoch, crypto_by_symbol))
        joined_row.update(_join_kalshi_features(ticker, entry_epoch, kalshi_by_ticker))
        joined.append(joined_row)
    return joined


def _write_csv(rows: List[Dict[str, Any]]) -> Path:
    JOINED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = JOINED_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_research_joined.csv"
    fieldnames = list(rows[0].keys()) if rows else []
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_path


def _count_status(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "UNKNOWN")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _print_counts(label: str, counts: Dict[str, int]) -> None:
    print(f"  {label}:")
    if not counts:
        print("    - none")
        return
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"    - {key}: {count}")


def main() -> None:
    trades = _load_jsonl(PAPER_TRADES_LOG)
    kalshi_by_ticker, kalshi_stats = _load_kalshi_candles()
    crypto_by_symbol, crypto_stats = _load_crypto_candles()
    print("=" * 72)
    print("RESEARCH DATASET BUILDER")
    print("=" * 72)
    print("Read-only with respect to logs/trading. Writes joined research output only when inputs exist.")
    print(f"  paper_trades_exists: {PAPER_TRADES_LOG.exists()}")
    print(f"  paper_trade_rows:    {len(trades)}")
    print(f"  kalshi_dir_exists:   {KALSHI_DIR.exists()}")
    print(f"  crypto_dir_exists:   {CRYPTO_DIR.exists()}")
    print("  normalized_inputs:")
    print(
        "    kalshi_files={files} kalshi_candles={candles} "
        "skipped_files={skipped_files} skipped_candles={skipped_candles}".format(**kalshi_stats)
    )
    print(
        "    crypto_files={files} crypto_candles={candles} "
        "skipped_files={skipped_files} skipped_candles={skipped_candles}".format(**crypto_stats)
    )

    missing: List[str] = []
    if not PAPER_TRADES_LOG.exists() or not trades:
        missing.append("logs/paper_trades.jsonl rows")
    if not kalshi_stats["candles"]:
        missing.append("data/external/kalshi raw files")
    if not crypto_stats["candles"]:
        missing.append("data/external/crypto raw files")

    if missing or not _external_inputs_available():
        print("  action:             skipped joined output; missing required inputs")
        print("  missing:")
        for item in missing:
            print(f"    - {item}")
        print("  planned output dir: data/research/joined/")
        print("RESULT: RESEARCH_DATASET_BUILDER_OK")
        return

    rows = _build_rows(trades, kalshi_by_ticker, crypto_by_symbol)
    if not rows:
        print("  action:             no eligible trade rows to join")
        print("RESULT: RESEARCH_DATASET_BUILDER_OK")
        return

    out_path = _write_csv(rows)
    print(f"  joined_rows:        {len(rows)}")
    _print_counts("crypto_join_status", _count_status(rows, "crypto_join_status"))
    _print_counts("kalshi_join_status", _count_status(rows, "kalshi_join_status"))
    print(f"  wrote:              {out_path}")
    print("RESULT: RESEARCH_DATASET_BUILDER_OK")


if __name__ == "__main__":
    main()
