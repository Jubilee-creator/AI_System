#!/usr/bin/env python3
"""
tools/clean_truth_report.py
---------------------------
Read-only truth report for paper-trading validation.

This report separates append-only log states so profitability is evaluated
without mixing clean settlements, time exits, forced closes, void cleanup rows,
and active opens.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from brain.strategy_utils import normalize_strategy
from tools.performance_report import (
    build_terminal_key_sets,
    classify_open_records,
    classify_settled_records,
    get_clv,
    get_pnl,
    get_size,
    load_trades,
)


SAMPLE_WARNING_THRESHOLD = 30
HORIZON_ORDER = [
    "SHORT_INTRADAY_15M",
    "DAILY_CRYPTO",
    "LONG_TERM",
    "EVENT_OTHER",
    "UNKNOWN",
]


def _as_float(value: Any):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt_money(value: float) -> str:
    return f"${value:+.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}"


def parse_ts(value: Any):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_time_exit(rec: dict) -> bool:
    return (
        rec.get("status") == "FORCED_CLOSE"
        and (
            rec.get("result") == "TIME_EXIT"
            or rec.get("reason") == "TIME_EXIT"
            or rec.get("cleanup_reason") == "TIME_EXIT"
        )
    )


def is_learning_trade(rec: dict) -> bool:
    if rec.get("is_learning_trade") is True or rec.get("learning_trade") is True:
        return True
    confidence = _as_float(rec.get("confidence"))
    size = _as_float(rec.get("size"))
    if confidence is not None and 0.50 <= confidence < 0.65:
        return True
    return size is not None and size <= 5.0


def confidence_bucket(rec: dict) -> str:
    confidence = _as_float(rec.get("confidence"))
    if confidence is None:
        return "unknown"
    if confidence < 0.50:
        return "<0.50"
    if confidence < 0.55:
        return "0.50-0.54"
    if confidence < 0.60:
        return "0.55-0.59"
    if confidence < 0.65:
        return "0.60-0.64"
    if confidence < 0.70:
        return "0.65-0.69"
    if confidence < 0.75:
        return "0.70-0.74"
    if confidence < 0.80:
        return "0.75-0.79"
    return ">=0.80"


def edge_value(rec: dict):
    value = _as_float(rec.get("risk_edge"))
    if value is not None:
        return value
    return _as_float(rec.get("edge"))


def edge_bucket(rec: dict) -> str:
    edge = edge_value(rec)
    if edge is None:
        return "unknown"
    if edge < 0.00:
        return "<0.00"
    if edge < 0.01:
        return "0.00-0.009"
    if edge < 0.02:
        return "0.01-0.019"
    if edge < 0.03:
        return "0.02-0.029"
    if edge < 0.05:
        return "0.03-0.049"
    if edge < 0.08:
        return "0.05-0.079"
    return ">=0.08"


def market_horizon(rec: dict) -> str:
    ticker = str(rec.get("ticker") or "").upper()
    title = str(rec.get("title") or "").upper()
    text = f"{ticker} {title}"

    if "15M" in ticker:
        return "SHORT_INTRADAY_15M"
    if "27JAN" in ticker or "2027" in text or "MINY" in ticker:
        return "LONG_TERM"
    if any(token in ticker for token in ("BTCD", "ETHD", "XRPD", "DOGED", "SOLD")):
        return "DAILY_CRYPTO"

    entry_ts = parse_ts(rec.get("timestamp"))
    end_ts = parse_ts(rec.get("close_time")) or parse_ts(rec.get("result_time"))
    if entry_ts and end_ts:
        hours = (end_ts - entry_ts).total_seconds() / 3600
        if hours <= 1:
            return "SHORT_INTRADAY_15M"
        if hours <= 36:
            return "DAILY_CRYPTO"
        if hours >= 24 * 30:
            return "LONG_TERM"

    if any(asset in ticker for asset in ("BTC", "ETH", "XRP", "DOGE", "SOL")):
        return "EVENT_OTHER"
    return "UNKNOWN"


def close_time_horizon_bucket(rec: dict) -> str:
    entry_ts = parse_ts(rec.get("timestamp"))
    end_ts = parse_ts(rec.get("close_time")) or parse_ts(rec.get("result_time"))
    if not entry_ts or not end_ts:
        return "unknown"

    hours = (end_ts - entry_ts).total_seconds() / 3600
    if hours < 0:
        return "invalid_negative"
    if hours <= 1:
        return "<=1h"
    if hours <= 6:
        return "1-6h"
    if hours <= 24:
        return "6-24h"
    if hours <= 72:
        return "1-3d"
    if hours <= 24 * 30:
        return "3-30d"
    return ">30d"


def calc_metrics(rows: list[dict]) -> dict:
    wins = [r for r in rows if get_pnl(r) > 0]
    losses = [r for r in rows if get_pnl(r) < 0]
    pushes = [r for r in rows if get_pnl(r) == 0]
    pnl = sum(get_pnl(r) for r in rows)
    wagered = sum(get_size(r) for r in rows)

    confidence_vals = [
        v for v in (_as_float(r.get("confidence")) for r in rows) if v is not None
    ]
    original_edge_vals = [
        v for v in (_as_float(r.get("original_edge")) for r in rows) if v is not None
    ]
    adjusted_edge_vals = [
        v for v in (
            _as_float(r.get("adjusted_edge", r.get("edge"))) for r in rows
        )
        if v is not None
    ]
    risk_edge_vals = [
        v for v in (_as_float(r.get("risk_edge")) for r in rows) if v is not None
    ]
    clv_vals = [v for v in (get_clv(r) for r in rows) if v is not None]

    return {
        "count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "pushes": len(pushes),
        "win_rate": len(wins) / (len(wins) + len(losses)) if wins or losses else 0.0,
        "total_pnl": pnl,
        "avg_pnl": pnl / len(rows) if rows else 0.0,
        "roi": pnl / wagered if wagered else 0.0,
        "avg_confidence": _avg(confidence_vals),
        "avg_original_edge": _avg(original_edge_vals),
        "avg_adjusted_edge": _avg(adjusted_edge_vals),
        "avg_risk_edge": _avg(risk_edge_vals),
        "avg_clv": _avg(clv_vals),
    }


def group_by(rows: list[dict], key_func) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in rows:
        groups[key_func(rec)].append(rec)
    return dict(groups)


def strategy_key(rec: dict) -> str:
    strategy = normalize_strategy(rec.get("strategy"))
    raw_strategy = rec.get("raw_strategy")
    if raw_strategy:
        return f"{strategy} / raw={str(raw_strategy).upper()}"
    return strategy


def print_metrics(name: str, rows: list[dict]) -> None:
    m = calc_metrics(rows)
    print(f"{name:<28} n={m['count']:>3}  W={m['wins']:>3}  L={m['losses']:>3}  P={m['pushes']:>3}  "
          f"WR={_fmt_pct(m['win_rate']):>6}  PnL={_fmt_money(m['total_pnl']):>9}  "
          f"Avg={_fmt_money(m['avg_pnl']):>8}  ROI={_fmt_pct(m['roi']):>8}")
    print(
        f"{'':<28} avg_conf={_fmt_num(m['avg_confidence'])}  "
        f"orig_edge={_fmt_num(m['avg_original_edge'])}  "
        f"adj_edge={_fmt_num(m['avg_adjusted_edge'])}  "
        f"risk_edge={_fmt_num(m['avg_risk_edge'])}  "
        f"clv={_fmt_num(m['avg_clv'])}"
    )


def print_group_table(title: str, rows: list[dict], key_func, order: list[str] | None = None) -> None:
    print()
    print(title)
    print("-" * len(title))
    groups = group_by(rows, key_func)
    keys = order or sorted(groups)
    printed = False
    for key in keys:
        bucket_rows = groups.get(key, [])
        if not bucket_rows:
            continue
        print_metrics(str(key), bucket_rows)
        printed = True
    if not printed:
        print("  (no rows)")


def print_horizon_breakdown(rows: list[dict]) -> None:
    print()
    print("MARKET HORIZON BREAKDOWN (clean settled + time exits)")
    print("------------------------------------------------------")
    groups = group_by(rows, market_horizon)
    for horizon in HORIZON_ORDER:
        horizon_rows = groups.get(horizon, [])
        if not horizon_rows:
            continue
        print_metrics(horizon, horizon_rows)

        strategy_groups = group_by(horizon_rows, strategy_key)
        if len(strategy_groups) > 1:
            for strategy, strategy_rows in sorted(strategy_groups.items()):
                print(f"  strategy={strategy}")
                print_metrics(f"    {strategy}", strategy_rows)

        edge_groups = group_by(horizon_rows, edge_bucket)
        for bucket in ["<0.00", "0.00-0.009", "0.01-0.019", "0.02-0.029",
                       "0.03-0.049", "0.05-0.079", ">=0.08", "unknown"]:
            bucket_rows = edge_groups.get(bucket, [])
            if not bucket_rows:
                continue
            print_metrics(f"    edge {bucket}", bucket_rows)


def classify_records(all_records: list[dict]) -> dict[str, list[dict]]:
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted_settled = classify_settled_records(
        all_records,
        settled_keys,
        forced_close_keys,
        void_keys,
    )
    active_open, stale_open = classify_open_records(all_records)
    time_exits = [r for r in all_records if is_time_exit(r)]
    forced_other = [
        r for r in all_records
        if r.get("status") == "FORCED_CLOSE" and not is_time_exit(r)
    ]
    voided = [r for r in all_records if r.get("status") == "VOID_LEGACY_DUPLICATE"]

    return {
        "active_open": active_open,
        "stale_open": stale_open,
        "clean_settled": clean_settled,
        "conflicted_settled": conflicted_settled,
        "time_exit": time_exits,
        "forced_close": forced_other,
        "voided": voided,
        "learning": [r for r in all_records if is_learning_trade(r)],
        "normal": [r for r in all_records if not is_learning_trade(r)],
    }


def print_disagreement_note() -> None:
    print("AUDIT NOTE")
    print("----------")
    print("performance_report.py is conflict-safe: SETTLED rows sharing a trade key")
    print("with FORCED_CLOSE or VOID_LEGACY_DUPLICATE are excluded from performance.")
    print("local_audit.py dedupes by (ticker, timestamp) and counts SETTLED rows after")
    print("that dedupe, so terminal-state conflicts can be counted as profitable rows.")
    print("Use this report or performance_report.py as the validation truth source.")


def main() -> None:
    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]
    time_exits = buckets["time_exit"]

    print()
    print("=" * 92)
    print("AI_SYSTEM CLEAN TRUTH REPORT")
    print("=" * 92)
    print(f"Log records read: {len(all_records)}")
    print_disagreement_note()

    print()
    print("TERMINAL STATE SEPARATION")
    print("-------------------------")
    print(f"OPEN active:              {len(buckets['active_open'])}")
    print(f"OPEN stale:               {len(buckets['stale_open'])}")
    print(f"SETTLED clean:            {len(clean_settled)}")
    print(f"SETTLED conflicted:       {len(buckets['conflicted_settled'])}")
    print(f"TIME_EXIT trades:         {len(time_exits)}")
    print(f"FORCED_CLOSE other:       {len(buckets['forced_close'])}")
    print(f"VOID / legacy duplicate:  {len(buckets['voided'])}")
    print(f"Learning-classified rows: {len(buckets['learning'])}")
    print(f"Normal-classified rows:   {len(buckets['normal'])}")

    print()
    print("CORE PERFORMANCE")
    print("----------------")
    print_metrics("Clean SETTLED", clean_settled)
    print_metrics("TIME_EXIT", time_exits)

    if len(clean_settled) < SAMPLE_WARNING_THRESHOLD:
        print(
            f"[WARN] Clean settled sample size {len(clean_settled)} < "
            f"{SAMPLE_WARNING_THRESHOLD}; profitability is not statistically reliable."
        )
    if len(time_exits) < SAMPLE_WARNING_THRESHOLD:
        print(
            f"[WARN] TIME_EXIT sample size {len(time_exits)} < "
            f"{SAMPLE_WARNING_THRESHOLD}; time-exit behavior is not statistically reliable."
        )

    evaluated = clean_settled + time_exits
    print_group_table("LEARNING VS NORMAL (clean settled + time exits)", evaluated,
                      lambda r: "LEARNING" if is_learning_trade(r) else "NORMAL")
    print_group_table("STRATEGY BREAKDOWN (clean settled + time exits)", evaluated, strategy_key)
    print_group_table(
        "CONFIDENCE BUCKETS (clean settled + time exits)",
        evaluated,
        confidence_bucket,
        ["<0.50", "0.50-0.54", "0.55-0.59", "0.60-0.64", "0.65-0.69",
         "0.70-0.74", "0.75-0.79", ">=0.80", "unknown"],
    )
    print_group_table(
        "EDGE BUCKETS USING risk_edge ELSE edge (clean settled + time exits)",
        evaluated,
        edge_bucket,
        ["<0.00", "0.00-0.009", "0.01-0.019", "0.02-0.029",
         "0.03-0.049", "0.05-0.079", ">=0.08", "unknown"],
    )
    print_horizon_breakdown(evaluated)
    print_group_table(
        "CLOSE_TIME HORIZON LENGTH (when timestamp fields allow)",
        evaluated,
        close_time_horizon_bucket,
        ["<=1h", "1-6h", "6-24h", "1-3d", "3-30d", ">30d",
         "invalid_negative", "unknown"],
    )

    print()
    print("=" * 92)


if __name__ == "__main__":
    main()
