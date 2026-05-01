#!/usr/bin/env python3
"""
tools/audit_bad_buckets.py
--------------------------
Read-only kill-zone audit for paper-trading validation.

This report identifies historically harmful buckets as candidates for future
filters.  It does not change trading, risk, sizing, logs, proof gates, or edge
profile trust.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tools.performance_report import (
    build_terminal_key_sets,
    classify_open_records,
    classify_settled_records,
    get_clv,
    get_pnl,
    get_size,
    load_trades,
)


MIN_SAMPLE = 10


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _money(value: float) -> str:
    return f"${value:+.2f}"


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}%"


def _num(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}"


def _ratio(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def is_time_exit(rec: dict) -> bool:
    return (
        rec.get("status") == "FORCED_CLOSE"
        and (
            rec.get("result") == "TIME_EXIT"
            or rec.get("reason") == "TIME_EXIT"
            or rec.get("cleanup_reason") == "TIME_EXIT"
        )
    )


def edge_value(rec: dict) -> float | None:
    value = _as_float(rec.get("risk_edge"))
    if value is not None:
        return value
    return _as_float(rec.get("edge"))


def risk_edge_value(rec: dict) -> float | None:
    return _as_float(rec.get("risk_edge"))


def confidence_value(rec: dict) -> float | None:
    for key in ("model_probability", "original_confidence", "confidence"):
        value = _as_float(rec.get(key))
        if value is not None:
            return value
    return None


def bucket_numeric(value: float | None, cuts: list[tuple[float, str]], fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    for limit, label in cuts:
        if value < limit:
            return label
    return cuts[-1][1].replace("<", ">=")


def entry_price_bucket(rec: dict) -> str:
    value = _as_float(rec.get("entry_price"))
    if value is None:
        return "unknown"
    if value < 0.20:
        return "<0.20"
    if value < 0.40:
        return "0.20-0.39"
    if value < 0.60:
        return "0.40-0.59"
    if value < 0.80:
        return "0.60-0.79"
    return ">=0.80"


def confidence_bucket(rec: dict) -> str:
    value = confidence_value(rec)
    if value is None:
        return "unknown"
    if value < 0.50:
        return "<0.50"
    if value < 0.60:
        return "0.50-0.59"
    if value < 0.70:
        return "0.60-0.69"
    if value < 0.80:
        return "0.70-0.79"
    if value < 0.90:
        return "0.80-0.89"
    return ">=0.90"


def edge_bucket_from_value(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0:
        return "<0.00"
    if value < 0.03:
        return "0.00-0.029"
    if value < 0.05:
        return "0.03-0.049"
    if value < 0.08:
        return "0.05-0.079"
    return ">=0.08"


def edge_bucket(rec: dict) -> str:
    return edge_bucket_from_value(edge_value(rec))


def risk_edge_bucket(rec: dict) -> str:
    return edge_bucket_from_value(risk_edge_value(rec))


def market_horizon(rec: dict) -> str:
    ticker = str(rec.get("ticker") or "").upper()
    title = str(rec.get("title") or "").upper()
    if "15M" in ticker:
        return "SHORT_INTRADAY_15M"
    if "27JAN" in ticker or "2027" in title or "MINY" in ticker:
        return "LONG_TERM"
    if any(token in ticker for token in ("BTCD", "ETHD", "XRPD", "DOGED", "SOLD")):
        return "DAILY_CRYPTO"
    if any(token in ticker for token in ("BTC", "ETH", "XRP", "DOGE", "SOL")):
        return "EVENT_OTHER"
    return "UNKNOWN"


def strategy_bucket(rec: dict) -> str:
    strategy = str(rec.get("strategy") or "UNKNOWN").upper()
    raw = rec.get("raw_strategy")
    return f"{strategy} / raw={raw}" if raw else strategy


def action_bucket(rec: dict) -> str:
    return str(rec.get("action") or "UNKNOWN")


def spread_bucket(rec: dict) -> str:
    value = _as_float(rec.get("spread", rec.get("market_spread")))
    if value is None:
        return "unknown"
    if value <= 0.01:
        return "<=0.01"
    if value <= 0.03:
        return "0.01-0.03"
    if value <= 0.05:
        return "0.03-0.05"
    return ">0.05"


def volume_bucket(rec: dict) -> str:
    value = _as_float(rec.get("volume", rec.get("market_volume")))
    if value is None:
        return "unknown"
    if value < 1_000:
        return "<1k"
    if value < 10_000:
        return "1k-10k"
    if value < 100_000:
        return "10k-100k"
    return ">=100k"


def time_to_expiry_bucket(rec: dict) -> str:
    value = _as_float(rec.get("time_to_expiry"))
    if value is None:
        return "unknown"
    if value <= 1:
        return "<=1h"
    if value <= 6:
        return "1-6h"
    if value <= 24:
        return "6-24h"
    if value <= 72:
        return "1-3d"
    return ">3d"


def data_collection_bucket(rec: dict) -> str:
    return "data_collection_override=True" if rec.get("data_collection_override") else "data_collection_override=False"


def bootstrap_bucket(rec: dict) -> str:
    return "bootstrap_provisional=True" if rec.get("bootstrap_provisional") else "bootstrap_provisional=False"


def metrics(rows: list[dict]) -> dict:
    wins = [get_pnl(r) for r in rows if get_pnl(r) > 0]
    losses = [get_pnl(r) for r in rows if get_pnl(r) < 0]
    pushes = [get_pnl(r) for r in rows if get_pnl(r) == 0]
    pnl = sum(wins) + sum(losses)
    wagered = sum(get_size(r) for r in rows)
    clv_vals = [v for v in (get_clv(r) for r in rows) if v is not None]
    entry_vals = [v for v in (_as_float(r.get("entry_price")) for r in rows) if v is not None]
    conf_vals = [v for v in (confidence_value(r) for r in rows) if v is not None]
    edge_vals = [v for v in (edge_value(r) for r in rows) if v is not None]
    gross_wins = sum(wins)
    gross_losses = sum(losses)
    avg_win = gross_wins / len(wins) if wins else None
    avg_loss = gross_losses / len(losses) if losses else None
    profit_factor = gross_wins / abs(gross_losses) if gross_wins > 0 and gross_losses < 0 else None

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "pushes": len(pushes),
        "win_rate": len(wins) / (len(wins) + len(losses)) if wins or losses else 0.0,
        "pnl": pnl,
        "roi": pnl / wagered if wagered else 0.0,
        "avg_clv": _avg(clv_vals),
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_entry": _avg(entry_vals),
        "avg_confidence": _avg(conf_vals),
        "avg_edge": _avg(edge_vals),
    }


def warning_labels(name: str, label: str, rows: list[dict], m: dict, active_only: bool = False) -> list[str]:
    labels = []
    if m["n"] < MIN_SAMPLE:
        labels.append("SAMPLE_TOO_SMALL")
    if m["roi"] < 0:
        labels.append("NEGATIVE_ROI")
    if m["avg_clv"] is not None and m["avg_clv"] < 0:
        labels.append("NEGATIVE_CLV")
    if m["avg_win"] is not None and m["avg_loss"] is not None and abs(m["avg_loss"]) > m["avg_win"]:
        labels.append("BAD_PAYOUT_ASYMMETRY")
    if "edge" in name.lower() and label == ">=0.08" and m["roi"] < 0:
        labels.append("HIGH_EDGE_DANGER")
    if active_only and any(r.get("bootstrap_provisional") for r in rows):
        labels.append("BOOTSTRAP_ACTIVE_ONLY")
    labels.append("DO_NOT_SCALE")
    return labels


def load_evidence() -> tuple[list[dict], list[dict], list[dict]]:
    all_records = load_trades()
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, _conflicted = classify_settled_records(
        all_records,
        settled_keys,
        forced_close_keys,
        void_keys,
    )
    active_open, _stale_open = classify_open_records(all_records)
    time_exits = [r for r in all_records if is_time_exit(r)]
    return clean_settled + time_exits, active_open, all_records


def group_rows(rows: list[dict], dimensions: dict[str, Callable[[dict], str]]) -> list[dict]:
    groups: list[dict] = []
    for dimension, func in dimensions.items():
        bucketed: dict[str, list[dict]] = defaultdict(list)
        for rec in rows:
            bucketed[func(rec)].append(rec)
        for label, bucket_rows in bucketed.items():
            groups.append({
                "dimension": dimension,
                "bucket": label,
                "rows": bucket_rows,
                "metrics": metrics(bucket_rows),
            })
    return groups


def print_row(group: dict, active_only: bool = False) -> None:
    m = group["metrics"]
    labels = warning_labels(group["dimension"], group["bucket"], group["rows"], m, active_only)
    print(
        f"{group['dimension']:<24} {group['bucket']:<34} "
        f"n={m['n']:>3} W={m['wins']:>3} L={m['losses']:>3} P={m['pushes']:>3} "
        f"ROI={_pct(m['roi']):>8} PnL={_money(m['pnl']):>9} "
        f"CLV={_num(m['avg_clv']):>8} PF={_ratio(m['profit_factor']):>4} "
        f"entry={_num(m['avg_entry']):>8} conf={_num(m['avg_confidence'], 3):>7} "
        f"edge={_num(m['avg_edge']):>8} "
        f"{' | '.join(labels)}"
    )


def print_ranked(title: str, groups: list[dict], key_func, limit: int = 12) -> None:
    print()
    print(title)
    print("-" * len(title))
    ranked = sorted(groups, key=key_func)
    printed = 0
    for group in ranked:
        if group["metrics"]["n"] == 0:
            continue
        print_row(group)
        printed += 1
        if printed >= limit:
            break
    if printed == 0:
        print("  (no rows)")


def print_subset(title: str, rows: list[dict], dimensions: dict[str, Callable[[dict], str]]) -> None:
    print()
    print(title)
    print("-" * len(title))
    if not rows:
        print("  (no evaluated rows)")
        return
    for group in group_rows(rows, dimensions):
        print_row(group)


def main() -> None:
    evaluated, active_open, all_records = load_evidence()

    dimensions = {
        "entry_price": entry_price_bucket,
        "confidence": confidence_bucket,
        "edge": edge_bucket,
        "risk_edge": risk_edge_bucket,
        "market_horizon": market_horizon,
        "strategy": strategy_bucket,
        "action": action_bucket,
        "spread": spread_bucket,
        "volume": volume_bucket,
        "time_to_expiry": time_to_expiry_bucket,
        "data_collection": data_collection_bucket,
        "bootstrap": bootstrap_bucket,
    }
    groups = group_rows(evaluated, dimensions)

    print()
    print("=" * 100)
    print("AI_SYSTEM BAD BUCKET / KILL-ZONE AUDIT")
    print("=" * 100)
    print("Read-only report. Candidate filters only; not proof and not execution logic.")
    print(f"Log records read:        {len(all_records)}")
    print(f"Evaluated rows:          {len(evaluated)}  (clean SETTLED + TIME_EXIT)")
    print(f"Active OPEN rows:        {len(active_open)}")
    print()

    print_ranked("WORST BUCKETS BY ROI", groups, lambda g: (g["metrics"]["roi"], -g["metrics"]["n"]))
    print_ranked("WORST BUCKETS BY PNL", groups, lambda g: (g["metrics"]["pnl"], -g["metrics"]["n"]))
    print_ranked(
        "WORST BUCKETS BY AVG CLV",
        [g for g in groups if g["metrics"]["avg_clv"] is not None],
        lambda g: (g["metrics"]["avg_clv"], -g["metrics"]["n"]),
    )
    print_ranked(
        "WORST PAYOUT ASYMMETRY BUCKETS",
        [g for g in groups if g["metrics"]["avg_win"] is not None and g["metrics"]["avg_loss"] is not None],
        lambda g: (
            (g["metrics"]["avg_win"] / abs(g["metrics"]["avg_loss"]))
            if g["metrics"]["avg_loss"] else 999,
            g["metrics"]["profit_factor"] if g["metrics"]["profit_factor"] is not None else 999,
        ),
    )

    active_bootstrap = [r for r in active_open if r.get("bootstrap_provisional")]
    print()
    print("BOOTSTRAP PROVISIONAL ACTIVE BUCKET EXPOSURE")
    print("--------------------------------------------")
    if not active_bootstrap:
        print("  (no active bootstrap provisional rows)")
    else:
        for group in group_rows(active_bootstrap, dimensions):
            print_row(group, active_only=True)

    data_collection_rows = [r for r in evaluated if r.get("data_collection_override")]
    normal_modern_rows = [
        r for r in evaluated
        if r.get("risk_edge") is not None
        and r.get("model_probability") is not None
        and not r.get("data_collection_override")
        and not r.get("bootstrap_provisional")
    ]
    print_subset("DATA_COLLECTION_OVERRIDE BUCKET PERFORMANCE", data_collection_rows, dimensions)
    print_subset("NORMAL COUNCIL-APPROVED BUCKET PERFORMANCE", normal_modern_rows, dimensions)

    print()
    print("AUDIT VERDICT")
    print("-------------")
    print("Dangerous buckets are future filter candidates only. Current samples are too small for proof.")
    print("Bootstrap provisional has active exposure but no settled proof until its rows resolve.")
    print("Do not scale. Do not use this report to justify real money.")


if __name__ == "__main__":
    main()
