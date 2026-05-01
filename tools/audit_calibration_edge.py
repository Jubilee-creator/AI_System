#!/usr/bin/env python3
"""
tools/audit_calibration_edge.py
-------------------------------
Read-only calibration and edge monotonicity audit.

This report checks whether confidence/probability and predicted edge rank
trades in the correct direction. It is a diagnostics tool only and does not
change trading, risk, sizing, logs, proof gates, or edge profile trust.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

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
PROOF_SAMPLE = 30


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


def has_quote_metadata(rec: dict) -> bool:
    if rec.get("price_yes") is not None or rec.get("price_no") is not None:
        return True
    return any(rec.get(k) is not None for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def row_quality(rec: dict) -> str:
    if (
        _as_float(rec.get("risk_edge")) is not None
        and _as_float(rec.get("model_probability")) is not None
        and has_quote_metadata(rec)
    ):
        return "MODERN_FULL_METADATA"
    if _as_float(rec.get("risk_edge")) is not None:
        return "MODERN_EDGE_ONLY"
    if _as_float(rec.get("edge")) is not None:
        return "LEGACY_EDGE_ONLY"
    return "MISSING_EDGE"


def probability(rec: dict) -> float | None:
    for key in ("model_probability", "original_confidence", "confidence"):
        value = _as_float(rec.get(key))
        if value is not None:
            return value
    return None


def edge_value(rec: dict) -> float | None:
    value = _as_float(rec.get("risk_edge"))
    if value is not None:
        return value
    return _as_float(rec.get("edge"))


def realized_outcome(rec: dict) -> float | None:
    pnl = get_pnl(rec)
    if pnl > 0:
        return 1.0
    if pnl < 0:
        return 0.0
    return None


def probability_bucket(rec: dict) -> str:
    value = probability(rec)
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


def edge_bucket(rec: dict) -> str:
    value = edge_value(rec)
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


def class_bucket(rec: dict) -> str:
    if rec.get("bootstrap_provisional"):
        return "bootstrap_provisional"
    if rec.get("data_collection_override"):
        return "data_collection_override"
    quality = row_quality(rec)
    if quality == "LEGACY_EDGE_ONLY":
        return "legacy_rows"
    if quality == "MODERN_FULL_METADATA":
        return "normal_council_approved"
    return quality.lower()


def metrics(rows: list[dict]) -> dict:
    wins = [get_pnl(r) for r in rows if get_pnl(r) > 0]
    losses = [get_pnl(r) for r in rows if get_pnl(r) < 0]
    pushes = [get_pnl(r) for r in rows if get_pnl(r) == 0]
    pnl = sum(wins) + sum(losses)
    wagered = sum(get_size(r) for r in rows)
    probs = [v for v in (probability(r) for r in rows) if v is not None]
    outcomes = [
        (probability(r), realized_outcome(r))
        for r in rows
        if probability(r) is not None and realized_outcome(r) is not None
    ]
    edges = [v for v in (edge_value(r) for r in rows) if v is not None]
    clvs = [v for v in (get_clv(r) for r in rows) if v is not None]
    gross_wins = sum(wins)
    gross_losses = sum(losses)
    profit_factor = gross_wins / abs(gross_losses) if gross_wins > 0 and gross_losses < 0 else None
    brier = None
    if outcomes:
        brier = sum((p - y) ** 2 for p, y in outcomes) / len(outcomes)

    win_loss_count = len(wins) + len(losses)
    expected_wins = sum(p for p, _y in outcomes)
    actual_wins = sum(y for _p, y in outcomes)

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "pushes": len(pushes),
        "expected_wins": expected_wins,
        "actual_wins": actual_wins,
        "expected_win_rate": _avg(probs),
        "actual_win_rate": len(wins) / win_loss_count if win_loss_count else None,
        "gap": (len(wins) / win_loss_count - _avg(probs)) if win_loss_count and probs else None,
        "brier": brier,
        "pnl": pnl,
        "roi": pnl / wagered if wagered else 0.0,
        "avg_clv": _avg(clvs),
        "profit_factor": profit_factor,
        "avg_edge": _avg(edges),
    }


def group_by(rows: list[dict], key_func) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for rec in rows:
        grouped[key_func(rec)].append(rec)
    return grouped


def calibration_flags(m: dict) -> list[str]:
    flags = []
    if m["n"] < MIN_SAMPLE:
        flags.append("SAMPLE_TOO_SMALL")
    gap = m.get("gap")
    if gap is not None and gap < -0.10:
        flags.append("OVERCONFIDENT")
    if gap is not None and gap > 0.10:
        flags.append("UNDERCONFIDENT")
    if m["roi"] < 0:
        flags.append("NEGATIVE_ROI")
    if m["avg_clv"] is not None and m["avg_clv"] < 0:
        flags.append("NEGATIVE_CLV")
    flags.append("DO_NOT_SCALE")
    return flags


def edge_flags(bucket: str, m: dict, inverted: bool) -> list[str]:
    flags = []
    if inverted:
        flags.append("EDGE_INVERTED")
    if bucket == ">=0.08" and m["roi"] < 0:
        flags.append("HIGH_EDGE_DANGER")
    if m["roi"] < 0:
        flags.append("NEGATIVE_ROI")
    if m["avg_clv"] is not None and m["avg_clv"] < 0:
        flags.append("NEGATIVE_CLV")
    if m["n"] < MIN_SAMPLE:
        flags.append("SAMPLE_TOO_SMALL")
    flags.append("DO_NOT_SCALE")
    return flags


def print_calibration_table(rows: list[dict], title: str) -> None:
    print()
    print(title)
    print("-" * len(title))
    order = ["<0.50", "0.50-0.59", "0.60-0.69", "0.70-0.79", "0.80-0.89", ">=0.90", "unknown"]
    groups = group_by(rows, probability_bucket)
    printed = False
    for bucket in order:
        bucket_rows = groups.get(bucket, [])
        if not bucket_rows:
            continue
        m = metrics(bucket_rows)
        print(
            f"{bucket:<10} n={m['n']:>3} "
            f"ExpW={m['expected_wins']:>5.2f} ActW={m['actual_wins']:>5.2f} "
            f"ExpWR={_pct(m['expected_win_rate']):>7} ActWR={_pct(m['actual_win_rate']):>7} "
            f"Gap={_pct(m['gap']):>7} Brier={_num(m['brier'], 4):>8} "
            f"ROI={_pct(m['roi']):>8} CLV={_num(m['avg_clv']):>8} "
            f"PF={_ratio(m['profit_factor']):>4} "
            f"{' | '.join(calibration_flags(m))}"
        )
        printed = True
    if not printed:
        print("  (no scored rows)")


def monotonic_direction(bucket_metrics: list[tuple[str, dict]]) -> str:
    usable = [
        (bucket, m)
        for bucket, m in bucket_metrics
        if bucket != "unknown" and m["n"] > 0
    ]
    if len(usable) < 3:
        return "UNPROVEN"

    rois = [m["roi"] for _bucket, m in usable]
    nondecreasing = all(rois[i] <= rois[i + 1] for i in range(len(rois) - 1))
    nonincreasing = all(rois[i] >= rois[i + 1] for i in range(len(rois) - 1))
    if nondecreasing:
        return "MONOTONIC_POSITIVE"
    if nonincreasing:
        return "INVERTED"

    highest_bucket, highest_metrics = usable[-1]
    lower = usable[:-1]
    lower_avg_roi = sum(m["roi"] for _b, m in lower) / len(lower)
    if highest_metrics["roi"] < lower_avg_roi:
        return "PARTIALLY_INVERTED"
    return "MIXED"


def print_edge_table(rows: list[dict], title: str) -> str:
    print()
    print(title)
    print("-" * len(title))
    order = ["<0.00", "0.00-0.029", "0.03-0.049", "0.05-0.079", ">=0.08", "unknown"]
    groups = group_by(rows, edge_bucket)
    bucket_metrics = [(bucket, metrics(groups.get(bucket, []))) for bucket in order if groups.get(bucket)]
    direction = monotonic_direction(bucket_metrics)
    printed = False
    for bucket, m in bucket_metrics:
        inverted = direction in {"INVERTED", "PARTIALLY_INVERTED", "MIXED"} and bucket == ">=0.08"
        print(
            f"{bucket:<10} n={m['n']:>3} "
            f"avg_edge={_num(m['avg_edge']):>8} "
            f"ROI={_pct(m['roi']):>8} CLV={_num(m['avg_clv']):>8} "
            f"PnL={_money(m['pnl']):>9} PF={_ratio(m['profit_factor']):>4} "
            f"{' | '.join(edge_flags(bucket, m, inverted))}"
        )
        printed = True
    if not printed:
        print("  (no edge rows)")
    print(f"Monotonicity verdict: {direction}")
    return direction


def print_trade_class_comparison(rows: list[dict]) -> None:
    print()
    print("TRADE CLASS COMPARISON")
    print("----------------------")
    order = [
        "data_collection_override",
        "bootstrap_provisional",
        "normal_council_approved",
        "legacy_rows",
        "modern_edge_only",
        "missing_edge",
    ]
    groups = group_by(rows, class_bucket)
    for bucket in order:
        bucket_rows = groups.get(bucket, [])
        if not bucket_rows:
            continue
        m = metrics(bucket_rows)
        print(
            f"{bucket:<28} n={m['n']:>3} "
            f"ExpW={m['expected_wins']:>5.2f} ActW={m['actual_wins']:>5.2f} "
            f"ExpWR={_pct(m['expected_win_rate']):>7} ActWR={_pct(m['actual_win_rate']):>7} "
            f"ROI={_pct(m['roi']):>8} CLV={_num(m['avg_clv']):>8} "
            f"PF={_ratio(m['profit_factor']):>4} avgEdge={_num(m['avg_edge']):>8} "
            f"{' | '.join(calibration_flags(m))}"
        )


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


def verdicts(rows: list[dict], edge_direction: str) -> tuple[str, str, list[str], list[str]]:
    modern = [r for r in rows if row_quality(r) == "MODERN_FULL_METADATA"]
    all_metrics = metrics(rows)
    modern_metrics = metrics(modern)
    confidence_verdict = "WATCHLIST"
    edge_verdict = "WATCHLIST"

    if len(modern) < PROOF_SAMPLE:
        confidence_verdict = "WATCHLIST: modern sample too small"
    elif modern_metrics["gap"] is not None and abs(modern_metrics["gap"]) > 0.10:
        confidence_verdict = "NO: confidence miscalibrated"
    elif modern_metrics["roi"] <= 0:
        confidence_verdict = "NO: calibrated enough is not enough without positive ROI"
    else:
        confidence_verdict = "YES: preliminary, still paper-only"

    if len(modern) < PROOF_SAMPLE:
        edge_verdict = "WATCHLIST: modern sample too small"
    elif edge_direction in {"INVERTED", "PARTIALLY_INVERTED"}:
        edge_verdict = "NO: edge is inverted"
    elif modern_metrics["roi"] <= 0 or (modern_metrics["avg_clv"] is not None and modern_metrics["avg_clv"] <= 0):
        edge_verdict = "NO: edge not translating to ROI/CLV"
    else:
        edge_verdict = "YES: preliminary, still paper-only"

    dangerous = []
    promising = []
    by_edge = group_by(rows, edge_bucket)
    for bucket, bucket_rows in by_edge.items():
        m = metrics(bucket_rows)
        if m["roi"] < 0 or (m["avg_clv"] is not None and m["avg_clv"] < 0):
            dangerous.append(f"edge {bucket}")
        elif m["n"] >= 2:
            promising.append(f"edge {bucket} watchlist only")

    if all_metrics["roi"] < 0:
        dangerous.append("overall evaluated sample")
    return confidence_verdict, edge_verdict, sorted(set(dangerous)), sorted(set(promising))


def main() -> None:
    evaluated, active_open, all_records = load_evidence()
    modern = [r for r in evaluated if row_quality(r) == "MODERN_FULL_METADATA"]
    active_bootstrap = [r for r in active_open if r.get("bootstrap_provisional")]

    print()
    print("=" * 100)
    print("AI_SYSTEM CALIBRATION + EDGE MONOTONICITY AUDIT")
    print("=" * 100)
    print("Read-only report. Future filter candidates only; not proof and not execution logic.")
    print(f"Log records read:              {len(all_records)}")
    print(f"Evaluated rows:                {len(evaluated)}  (clean SETTLED + TIME_EXIT)")
    print(f"Modern full-metadata rows:     {len(modern)}")
    print(f"Active bootstrap provisional:  {len(active_bootstrap)}")
    print()

    print_calibration_table(evaluated, "CALIBRATION BY PROBABILITY / CONFIDENCE BUCKET")
    print_calibration_table(modern, "MODERN-ONLY CALIBRATION")
    edge_direction = print_edge_table(evaluated, "EDGE MONOTONICITY BY EDGE BUCKET")
    modern_edge_direction = print_edge_table(modern, "MODERN-ONLY EDGE MONOTONICITY")
    print_trade_class_comparison(evaluated)

    confidence_verdict, edge_verdict, dangerous, promising = verdicts(evaluated, modern_edge_direction)

    print()
    print("FINAL VERDICT")
    print("-------------")
    print(f"Confidence usable: {confidence_verdict}")
    print(f"Edge usable:       {edge_verdict}")
    print("Dangerous buckets:")
    for item in dangerous[:12]:
        print(f"  - {item}")
    if not dangerous:
        print("  - none identified")
    print("Buckets deserving more data:")
    for item in promising[:12]:
        print(f"  - {item}")
    if not promising:
        print("  - none yet")
    print("Do not scale. Do not use this report to justify real money.")


if __name__ == "__main__":
    main()
