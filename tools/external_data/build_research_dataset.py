#!/usr/bin/env python3
"""
Read-only external research dataset joiner skeleton.

Reads paper trades and checks for external Kalshi/crypto inputs. It writes a
joined dataset only when enough external inputs exist. It never mutates logs or
changes proof/trading behavior.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PAPER_TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
KALSHI_DIR = ROOT / "data" / "external" / "kalshi"
CRYPTO_DIR = ROOT / "data" / "external" / "crypto"
JOINED_DIR = ROOT / "data" / "research" / "joined"


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


def _build_rows(trades: List[dict]) -> List[Dict[str, Any]]:
    joined: List[Dict[str, Any]] = []
    for idx, row in enumerate(trades, 1):
        if row.get("status") not in ("SETTLED", "FORCED_CLOSE", "OPEN"):
            continue
        ticker = str(row.get("ticker") or "")
        joined.append({
            "trade_id": row.get("trade_id") or f"{ticker}:{row.get('timestamp') or row.get('timestamp_utc') or idx}",
            "ticker": ticker,
            "market_prefix": _market_prefix(ticker),
            "side": _side(row),
            "entry_ts": _parse_ts(row.get("timestamp") or row.get("timestamp_utc")),
            "settlement_ts": _parse_ts(row.get("settled_at") or row.get("result_time")),
            "entry_price": row.get("entry_price"),
            "outcome": _outcome(row),
            "roi": _roi(row),
            "clv": row.get("clv"),
            "probability_confidence": row.get("model_probability", row.get("confidence")),
            "proof_class": _proof_class(row),
            "nearest_crypto_candle_before_entry": None,
            "crypto_return_5m_before_entry": None,
            "crypto_return_15m_before_entry": None,
            "crypto_volatility_15m": None,
        })
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


def main() -> None:
    trades = _load_jsonl(PAPER_TRADES_LOG)
    print("=" * 72)
    print("RESEARCH DATASET BUILDER")
    print("=" * 72)
    print("Read-only with respect to logs/trading. Writes joined research output only when inputs exist.")
    print(f"  paper_trades_exists: {PAPER_TRADES_LOG.exists()}")
    print(f"  paper_trade_rows:    {len(trades)}")
    print(f"  kalshi_dir_exists:   {KALSHI_DIR.exists()}")
    print(f"  crypto_dir_exists:   {CRYPTO_DIR.exists()}")

    missing: List[str] = []
    if not PAPER_TRADES_LOG.exists() or not trades:
        missing.append("logs/paper_trades.jsonl rows")
    if not KALSHI_DIR.exists() or not any(p for p in KALSHI_DIR.rglob("*") if p.is_file() and p.name != ".gitkeep"):
        missing.append("data/external/kalshi raw files")
    if not CRYPTO_DIR.exists() or not any(p for p in CRYPTO_DIR.rglob("*") if p.is_file() and p.name != ".gitkeep"):
        missing.append("data/external/crypto raw files")

    if missing or not _external_inputs_available():
        print("  action:             skipped joined output; missing required inputs")
        print("  missing:")
        for item in missing:
            print(f"    - {item}")
        print("  planned output dir: data/research/joined/")
        print("RESULT: RESEARCH_DATASET_BUILDER_OK")
        return

    rows = _build_rows(trades)
    if not rows:
        print("  action:             no eligible trade rows to join")
        print("RESULT: RESEARCH_DATASET_BUILDER_OK")
        return

    out_path = _write_csv(rows)
    print(f"  joined_rows:        {len(rows)}")
    print(f"  wrote:              {out_path}")
    print("RESULT: RESEARCH_DATASET_BUILDER_OK")


if __name__ == "__main__":
    main()
