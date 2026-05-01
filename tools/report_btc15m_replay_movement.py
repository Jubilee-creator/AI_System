#!/usr/bin/env python3
"""
tools/report_btc15m_replay_movement.py
--------------------------------------
Read-only M-48C replay report for BTC 15-minute Kalshi snapshots.

This compares row-to-row BTC spot movement against Kalshi YES/NO quote
movement. It does not affect trading, risk, sizing, proof gates, or signal
generation.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_LOG = ROOT / "logs" / "btc_15m_entry_window_snapshots.jsonl"
EXCLUDE_FINAL_30S = True
MIN_TRANSITIONS_FOR_SIGNAL = 20

BUCKET_ORDER = [
    "5m_to_4m",
    "4m_to_3m",
    "3m_to_2m",
    "2m_to_1m",
    "final_60s",
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


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _avg(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _fmt(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def _sign(value: Optional[float], eps: float = 1e-9) -> int:
    if value is None:
        return 0
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


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
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row["_ts"] = _parse_ts(row.get("timestamp_utc"))
            row["_btc_price"] = _as_float(row.get("btc_price"))
            row["_yes_mid"] = _as_float(row.get("yes_mid"))
            row["_no_mid"] = _as_float(row.get("no_mid"))
            row["_spread"] = _as_float(row.get("spread"))
            row["_liquidity"] = _as_float(row.get("liquidity"))
            row["_latency"] = _as_float(row.get("btc_price_fetch_latency_ms"))
            row["_tte"] = _as_float(row.get("time_to_expiry_seconds"))
            rows.append(row)
    return rows


def comparable(row: dict) -> bool:
    return (
        row.get("_ts") is not None
        and row.get("_btc_price") is not None
        and row.get("_yes_mid") is not None
        and row.get("_no_mid") is not None
    )


def build_transitions(rows: list[dict], exclude_final_30s: bool = EXCLUDE_FINAL_30S) -> tuple[list[dict], int]:
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    excluded_final_30 = 0

    for row in rows:
        if not comparable(row):
            continue
        if exclude_final_30s and row.get("entry_window_bucket") == "final_30s":
            excluded_final_30 += 1
            continue
        by_ticker[row.get("ticker") or "UNKNOWN"].append(row)

    transitions = []
    for ticker, group in by_ticker.items():
        group.sort(key=lambda r: r["_ts"])
        for prev, cur in zip(group, group[1:]):
            seconds = (cur["_ts"] - prev["_ts"]).total_seconds()
            if seconds <= 0:
                continue

            btc_delta = cur["_btc_price"] - prev["_btc_price"]
            btc_delta_pct = btc_delta / prev["_btc_price"] if prev["_btc_price"] else None
            yes_delta = cur["_yes_mid"] - prev["_yes_mid"]
            no_delta = cur["_no_mid"] - prev["_no_mid"]
            btc_dir = _sign(btc_delta)
            yes_dir = _sign(yes_delta)
            no_dir = _sign(no_delta)

            transitions.append({
                "ticker": ticker,
                "from_ts": prev.get("timestamp_utc"),
                "to_ts": cur.get("timestamp_utc"),
                "seconds_between_snapshots": seconds,
                "entry_window_bucket": cur.get("entry_window_bucket") or "unknown",
                "time_to_expiry_seconds": cur.get("_tte"),
                "btc_price_delta": btc_delta,
                "btc_price_delta_pct": btc_delta_pct,
                "yes_mid_delta": yes_delta,
                "no_mid_delta": no_delta,
                "btc_yes_directional_agreement": btc_dir != 0 and btc_dir == yes_dir,
                "btc_no_directional_agreement": btc_dir != 0 and no_dir == -btc_dir,
                "spread": cur.get("_spread"),
                "liquidity": cur.get("_liquidity"),
                "btc_price_fetch_latency_ms": cur.get("_latency"),
                # M-48D: contract-line distance (None for pre-M-48D rows)
                "btc_distance_to_line": _as_float(cur.get("btc_distance_to_line")),
                "btc_distance_bps": _as_float(cur.get("btc_distance_bps")),
                "is_above_line": cur.get("is_above_line"),
                "near_line_zone": cur.get("near_line_zone"),
            })

    return transitions, excluded_final_30


def build_lag_checks(rows: list[dict], exclude_final_30s: bool = EXCLUDE_FINAL_30S) -> list[dict]:
    """Compare BTC movement in interval N with Kalshi movement in interval N+1."""
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if not comparable(row):
            continue
        if exclude_final_30s and row.get("entry_window_bucket") == "final_30s":
            continue
        by_ticker[row.get("ticker") or "UNKNOWN"].append(row)

    lag_checks = []
    for ticker, group in by_ticker.items():
        group.sort(key=lambda r: r["_ts"])
        if len(group) < 3:
            continue
        for before, pivot, after in zip(group, group[1:], group[2:]):
            btc_delta = pivot["_btc_price"] - before["_btc_price"]
            next_yes_delta = after["_yes_mid"] - pivot["_yes_mid"]
            next_no_delta = after["_no_mid"] - pivot["_no_mid"]
            btc_dir = _sign(btc_delta)
            lag_checks.append({
                "ticker": ticker,
                "pivot_ts": pivot.get("timestamp_utc"),
                "entry_window_bucket": after.get("entry_window_bucket") or "unknown",
                "btc_delta_prev_interval": btc_delta,
                "next_yes_mid_delta": next_yes_delta,
                "next_no_mid_delta": next_no_delta,
                "yes_followed_btc": btc_dir != 0 and _sign(next_yes_delta) == btc_dir,
                "no_followed_btc_inverse": btc_dir != 0 and _sign(next_no_delta) == -btc_dir,
                "seconds_to_next": (after["_ts"] - pivot["_ts"]).total_seconds(),
            })
    return lag_checks


def summarize_transitions(transitions: list[dict]) -> None:
    print("\nMOVEMENT BY ENTRY WINDOW")
    print("-" * 110)
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for transition in transitions:
        by_bucket[transition["entry_window_bucket"]].append(transition)

    for bucket in BUCKET_ORDER:
        group = by_bucket.get(bucket, [])
        if not group:
            continue
        yes_agree = sum(1 for r in group if r["btc_yes_directional_agreement"])
        no_agree = sum(1 for r in group if r["btc_no_directional_agreement"])
        # M-48D: distance-to-line stats (None for pre-M-48D rows)
        dist_vals = [r["btc_distance_to_line"] for r in group if r.get("btc_distance_to_line") is not None]
        bps_vals  = [r["btc_distance_bps"]     for r in group if r.get("btc_distance_bps")     is not None]
        above_n   = sum(1 for r in group if r.get("is_above_line") is True)
        near_n    = sum(1 for r in group if r.get("near_line_zone") is True)
        dist_note = (
            f"avg_dist_bps={_fmt(_avg(bps_vals), 1):>8} above={above_n}/{len(group)} near={near_n}/{len(group)}"
            if dist_vals else "dist=n/a(pre-M48D)"
        )
        print(
            f"{bucket:<16} "
            f"n={len(group):>4} "
            f"avg_secs={_fmt(_avg([r['seconds_between_snapshots'] for r in group]), 1):>7} "
            f"avg_btc_delta={_fmt(_avg([r['btc_price_delta'] for r in group]), 2):>9} "
            f"avg_btc_pct={_fmt_pct(_avg([r['btc_price_delta_pct'] for r in group])):>7} "
            f"avg_yes_delta={_fmt(_avg([r['yes_mid_delta'] for r in group])):>8} "
            f"yes_agree={yes_agree}/{len(group)} "
            f"{dist_note}"
        )


def print_sample_transitions(transitions: list[dict], limit: int = 8) -> None:
    print("\nSAMPLE TRANSITIONS")
    print("-" * 92)
    if not transitions:
        print("No comparable transitions yet.")
        return
    for row in transitions[-limit:]:
        print(
            f"{row['ticker']} | {row['entry_window_bucket']:<14} "
            f"dt={row['seconds_between_snapshots']:.1f}s "
            f"btc={row['btc_price_delta']:+.2f} ({_fmt_pct(row['btc_price_delta_pct'])}) "
            f"yes_mid={row['yes_mid_delta']:+.4f} "
            f"no_mid={row['no_mid_delta']:+.4f} "
            f"spread={_fmt(row['spread'])} "
            f"liq={_fmt(row['liquidity'], 1)}"
        )


def summarize_lag_checks(lag_checks: list[dict]) -> None:
    print("\nBTC-LEADS / KALSHI-FOLLOWS CHECK")
    print("-" * 92)
    print("This is a crude adjacent-interval check, not proof of causal lead/lag.")
    if not lag_checks:
        print("Not enough 3-row sequences for lag checks.")
        return
    yes_follow = sum(1 for r in lag_checks if r["yes_followed_btc"])
    no_inverse = sum(1 for r in lag_checks if r["no_followed_btc_inverse"])
    print(
        f"lag_sequences={len(lag_checks)} "
        f"yes_followed_btc={yes_follow}/{len(lag_checks)} "
        f"no_followed_inverse={no_inverse}/{len(lag_checks)}"
    )
    for row in lag_checks[-6:]:
        print(
            f"{row['ticker']} | next_bucket={row['entry_window_bucket']:<14} "
            f"prev_btc={row['btc_delta_prev_interval']:+.2f} "
            f"next_yes={row['next_yes_mid_delta']:+.4f} "
            f"next_no={row['next_no_mid_delta']:+.4f} "
            f"dt_next={row['seconds_to_next']:.1f}s"
        )


def main() -> None:
    rows = load_snapshots()
    synced_rows = [r for r in rows if comparable(r)]
    final_window_synced = [
        r for r in synced_rows
        if r.get("entry_window_bucket") in {"5m_to_4m", "4m_to_3m", "3m_to_2m", "2m_to_1m", "final_60s"}
    ]
    transitions, excluded_final_30 = build_transitions(rows)
    lag_checks = build_lag_checks(rows)

    print("=" * 92)
    print("BTC 15M REPLAY MOVEMENT REPORT")
    print("=" * 92)
    print(f"log_file={SNAPSHOT_LOG}")
    print(f"total_snapshots={len(rows)}")
    print(f"synced_comparable_rows={len(synced_rows)}")
    print(f"synced_final_0_to_5m_rows_ex_final30={len(final_window_synced)}")
    print(f"row_to_row_transitions={len(transitions)}")
    print(f"excluded_final_30s_rows={excluded_final_30}")
    print(f"sample_floor_for_signal={MIN_TRANSITIONS_FOR_SIGNAL}")

    if len(transitions) < MIN_TRANSITIONS_FOR_SIGNAL:
        print("WARNING: SAMPLE_TOO_SMALL. Treat this as plumbing/replay verification only.")

    bucket_counts = Counter(r.get("entry_window_bucket") or "unknown" for r in synced_rows)
    print("\nSYNCED ROWS BY ENTRY WINDOW")
    print("-" * 92)
    for bucket in BUCKET_ORDER + ["final_30s"]:
        if bucket_counts.get(bucket):
            print(f"{bucket:<16} {bucket_counts[bucket]}")

    summarize_transitions(transitions)
    summarize_lag_checks(lag_checks)
    print_sample_transitions(transitions)

    print("\nVERDICT")
    print("-" * 92)
    if len(transitions) < MIN_TRANSITIONS_FOR_SIGNAL:
        print("NOT_ENOUGH_DATA: collect more synced snapshots before fair-value or trading logic.")
    else:
        print("ENOUGH_FOR_EXPLORATION_ONLY: still not proof of edge or profitability.")
    print("This report is read-only and does not affect trading.")


if __name__ == "__main__":
    main()
