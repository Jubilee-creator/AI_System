#!/usr/bin/env python3
"""
Read-only external data health report.

This script inspects external/research data directories and paper-trade
coverage. It does not fetch data, trade, edit logs, or change proof logic.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

KALSHI_DIR = ROOT / "data" / "external" / "kalshi"
CRYPTO_DIR = ROOT / "data" / "external" / "crypto"
JOINED_DIR = ROOT / "data" / "research" / "joined"
PAPER_TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"

PRIORITY_MARKETS = [
    "KXBTCD / BTC daily",
    "BTC15M",
    "KXETHD / ETH daily",
    "ETH15M",
]


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


def _market_prefix(ticker: str) -> str:
    text = str(ticker or "").upper()
    if text.startswith("KXBTCD"):
        return "KXBTCD / BTC daily"
    if "BTC15M" in text or text.startswith("KXBTC15M"):
        return "BTC15M"
    if text.startswith("KXETHD"):
        return "KXETHD / ETH daily"
    if "ETH15M" in text or text.startswith("KXETH15M"):
        return "ETH15M"
    if text.startswith("KXXRP"):
        return "XRP"
    if text.startswith("KXSOL"):
        return "SOL"
    if text.startswith("KXDOGE"):
        return "DOGE"
    return "OTHER"


def _iter_files(path: Path) -> Iterable[Path]:
    if not path.exists():
        return []
    return [p for p in path.rglob("*") if p.is_file() and p.name != ".gitkeep"]


def _newest_mtime(path: Path) -> Optional[datetime]:
    files = list(_iter_files(path))
    if not files:
        return None
    newest = max(p.stat().st_mtime for p in files)
    return datetime.fromtimestamp(newest, tz=timezone.utc)


def _fmt_ts(value: Optional[datetime]) -> str:
    if value is None:
        return "n/a"
    return value.isoformat()


def _print_area(label: str, path: Path) -> None:
    files = list(_iter_files(path))
    print(f"  {label:<24s} exists={str(path.exists()):<5s} files={len(files):>4} newest={_fmt_ts(_newest_mtime(path))}")


def main() -> None:
    rows = _load_jsonl(PAPER_TRADES_LOG)
    prefixes = Counter(_market_prefix(str(row.get("ticker") or "")) for row in rows)

    print("=" * 72)
    print("EXTERNAL DATA HEALTH REPORT")
    print("=" * 72)
    print("Read-only. No fetch, no trading, no log mutation, no proof changes.")
    print()

    print("DATA DIRECTORIES")
    print("-" * 72)
    _print_area("data/external/kalshi", KALSHI_DIR)
    _print_area("data/external/crypto", CRYPTO_DIR)
    _print_area("data/research/joined", JOINED_DIR)

    print()
    print("PAPER TRADE LOG")
    print("-" * 72)
    print(f"  exists:             {PAPER_TRADES_LOG.exists()}")
    print(f"  rows:               {len(rows)}")

    print()
    print("MARKET PREFIXES IN PAPER TRADES")
    print("-" * 72)
    if prefixes:
        for prefix, count in sorted(prefixes.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {prefix:<24s} {count:>5}")
    else:
        print("  (none)")

    print()
    print("PRIORITY EXTERNAL COLLECTION TARGETS")
    print("-" * 72)
    for item in PRIORITY_MARKETS:
        print(f"  - {item}")

    print()
    print("RESULT: EXTERNAL_DATA_HEALTH_OK")


if __name__ == "__main__":
    main()
