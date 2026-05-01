#!/usr/bin/env python3
"""
tools/report_btc15m_entry_windows.py
------------------------------------
Read-only summary for M-48A BTC 15-minute entry-window snapshots.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_LOG = ROOT / "logs" / "btc_15m_entry_window_snapshots.jsonl"
BUCKET_ORDER = [
    "5m_to_4m",
    "4m_to_3m",
    "3m_to_2m",
    "2m_to_1m",
    "final_60s",
    "final_30s",
    "outside_window",
    "unknown",
]


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _fmt_num(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def load_snapshots(path: Path = SNAPSHOT_LOG) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def summarize(rows: list[dict]) -> None:
    print("=" * 72)
    print("BTC 15M ENTRY WINDOW SNAPSHOT REPORT")
    print("=" * 72)
    print(f"log_file={SNAPSHOT_LOG}")
    print(f"total_snapshots={len(rows)}")

    if not rows:
        print("No snapshots found yet.")
        print("This is expected until Dashboard/scanner sees BTC 15-minute markets.")
        return

    timestamps = [r.get("timestamp_utc") for r in rows if r.get("timestamp_utc")]
    most_recent = max(timestamps) if timestamps else "n/a"
    print(f"most_recent_snapshot={most_recent}")

    missing_counts = Counter()
    ticker_counts = Counter()
    gate_counts = Counter()
    btc_source_counts = Counter()
    no_synced_btc_count = 0
    btc_price_rows = 0
    btc_price_timestamps = []
    bucket_rows: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        bucket = row.get("entry_window_bucket") or "unknown"
        bucket_rows[bucket].append(row)
        ticker_counts[row.get("ticker") or "UNKNOWN"] += 1
        gate_counts[row.get("research_gate_status") or "UNKNOWN"] += 1
        if _as_float(row.get("btc_price")) is not None:
            btc_price_rows += 1
        if row.get("btc_price_timestamp"):
            btc_price_timestamps.append(row.get("btc_price_timestamp"))
        btc_source_counts[row.get("btc_price_source") or "MISSING"] += 1
        if "REJECT_NO_SYNCED_BTC_PRICE" in (row.get("research_reject_reasons") or []):
            no_synced_btc_count += 1
        for field in row.get("missing_fields") or []:
            missing_counts[field] += 1

    unknown_time = len(bucket_rows.get("unknown", []))
    final_1_to_5 = sum(len(bucket_rows.get(b, [])) for b in (
        "5m_to_4m",
        "4m_to_3m",
        "3m_to_2m",
        "2m_to_1m",
    ))
    final_0_to_5 = final_1_to_5 + len(bucket_rows.get("final_60s", [])) + len(bucket_rows.get("final_30s", []))
    print(f"unknown_time_to_expiry_pct={(unknown_time / len(rows)):.1%}")
    print(f"final_1_to_5_minute_bucket_pct={(final_1_to_5 / len(rows)):.1%}")
    print(f"final_0_to_5_minute_bucket_pct={(final_0_to_5 / len(rows)):.1%}")
    print(f"btc_price_present_pct={(btc_price_rows / len(rows)):.1%}")
    print(f"reject_no_synced_btc_price_pct={(no_synced_btc_count / len(rows)):.1%}")
    print(f"most_recent_btc_price_timestamp={max(btc_price_timestamps) if btc_price_timestamps else 'n/a'}")

    print("\nSNAPSHOTS BY ENTRY WINDOW")
    print("-" * 72)
    for bucket in BUCKET_ORDER:
        group = bucket_rows.get(bucket, [])
        if not group:
            continue
        spreads = [_as_float(r.get("spread")) for r in group]
        liquidity = [_as_float(r.get("liquidity")) for r in group]
        volume = [_as_float(r.get("volume")) for r in group]
        btc_prices = [_as_float(r.get("btc_price")) for r in group]
        spreads = [v for v in spreads if v is not None]
        liquidity = [v for v in liquidity if v is not None]
        volume = [v for v in volume if v is not None]
        btc_prices = [v for v in btc_prices if v is not None]
        print(
            f"{bucket:<16} "
            f"n={len(group):>5} "
            f"avg_spread={_fmt_num(_avg(spreads)):>8} "
            f"avg_liquidity={_fmt_num(_avg(liquidity), 2):>10} "
            f"avg_volume={_fmt_num(_avg(volume), 2):>10} "
            f"avg_btc={_fmt_num(_avg(btc_prices), 2):>10}"
        )

    extra_buckets = sorted(set(bucket_rows) - set(BUCKET_ORDER))
    for bucket in extra_buckets:
        group = bucket_rows[bucket]
        print(f"{bucket:<16} n={len(group):>5}")

    print("\nSNAPSHOTS BY RESEARCH GATE STATUS")
    print("-" * 72)
    for status, count in gate_counts.most_common():
        print(f"{status:<24} {count}")

    print("\nBTC PRICE SOURCES")
    print("-" * 72)
    for source, count in btc_source_counts.most_common():
        print(f"{source:<40} {count}")

    print("\nMISSING FIELD COUNTS")
    print("-" * 72)
    if missing_counts:
        for field, count in missing_counts.most_common():
            print(f"{field:<24} {count}")
    else:
        print("No required field gaps detected.")

    print("\nTOP TICKERS BY SNAPSHOT COUNT")
    print("-" * 72)
    for ticker, count in ticker_counts.most_common(10):
        print(f"{ticker:<36} {count}")

    print("\nRESEARCH REJECT REASONS")
    print("-" * 72)
    reject_counts = Counter()
    for row in rows:
        for reason in row.get("research_reject_reasons") or []:
            reject_counts[reason] += 1
    if reject_counts:
        for reason, count in reject_counts.most_common():
            print(f"{reason:<36} {count}")
    else:
        print("No research reject reasons logged.")

    print("\nCONTRACT LINE COVERAGE (M-48D)")
    print("-" * 72)
    with_line = [r for r in rows if _as_float(r.get("contract_line")) is not None]
    without_line_n = len(rows) - len(with_line)
    print(f"rows_with_contract_line={len(with_line)}/{len(rows)}")
    print(f"rows_without_contract_line={without_line_n}/{len(rows)}")
    parse_statuses: Counter = Counter(
        r.get("contract_line_parse_status") or "not_logged" for r in rows
    )
    print("parse_status_breakdown:")
    for status, count in parse_statuses.most_common():
        print(f"  {status:<30} {count}")

    if with_line:
        print("\nDISTANCE TO CONTRACT LINE BY ENTRY WINDOW")
        print("-" * 72)
        for bucket in BUCKET_ORDER:
            group = [r for r in with_line if r.get("entry_window_bucket") == bucket]
            if not group:
                continue
            distances = [_as_float(r.get("btc_distance_to_line")) for r in group]
            distances = [d for d in distances if d is not None]
            bps_vals = [_as_float(r.get("btc_distance_bps")) for r in group]
            bps_vals = [b for b in bps_vals if b is not None]
            above = sum(1 for r in group if r.get("is_above_line") is True)
            near = sum(1 for r in group if r.get("near_line_zone") is True)
            print(
                f"{bucket:<16} "
                f"n={len(group):>4} "
                f"avg_dist_usd={_fmt_num(_avg(distances), 2):>10} "
                f"avg_dist_bps={_fmt_num(_avg(bps_vals), 2):>9} "
                f"above_line={above}/{len(group)} "
                f"near_zone(<=20bps)={near}/{len(group)}"
            )

    print("\nVERDICT")
    print("-" * 72)
    print("Research-only. This report does not prove edge and does not affect trading.")


def main() -> None:
    rows = load_snapshots()
    summarize(rows)


if __name__ == "__main__":
    main()
