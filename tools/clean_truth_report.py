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
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from brain.strategy_utils import normalize_strategy
from brain.edge_profile_health import edge_profile_health
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
EDGE_PROFILE_PATH = ROOT / "data" / "edge_profile.json"
HORIZON_ORDER = [
    "SHORT_INTRADAY_15M",
    "DAILY_CRYPTO",
    "LONG_TERM",
    "EVENT_OTHER",
    "UNKNOWN",
]
ENTRY_PRICE_ORDER = [
    "<0.20",
    "0.20-0.39",
    "0.40-0.59",
    "0.60-0.79",
    ">=0.80",
    "unknown",
]
EDGE_ORDER = [
    "<0.00",
    "0.00-0.009",
    "0.01-0.019",
    "0.02-0.029",
    "0.03-0.049",
    "0.05-0.079",
    ">=0.08",
    "unknown",
]
CALIBRATION_BUCKET_ORDER = [
    "<0.50",
    "0.50-0.59",
    "0.60-0.69",
    "0.70-0.79",
    "0.80-0.89",
    ">=0.90",
    "unknown",
]
ROW_QUALITY_ORDER = [
    "MODERN_FULL_METADATA",
    "MODERN_EDGE_ONLY",
    "LEGACY_EDGE_ONLY",
    "MISSING_EDGE",
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


def _fmt_ratio(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _fmt_field(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


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


def edge_source(rec: dict) -> str:
    if _as_float(rec.get("risk_edge")) is not None:
        return "risk_edge"
    if _as_float(rec.get("edge")) is not None:
        return "edge"
    return "missing"


def edge_source_quality(rec: dict) -> str:
    if _as_float(rec.get("risk_edge")) is not None:
        return "RISK_EDGE_AVAILABLE"
    if _as_float(rec.get("edge")) is not None:
        return "LEGACY_EDGE_ONLY"
    return "MISSING_EDGE"


def has_quote_metadata(rec: dict) -> bool:
    if rec.get("price_yes") is not None or rec.get("price_no") is not None:
        return True
    quote_fields = ("yes_bid", "yes_ask", "no_bid", "no_ask")
    return any(rec.get(field) is not None for field in quote_fields)


def row_quality_group(rec: dict) -> str:
    has_risk_edge = _as_float(rec.get("risk_edge")) is not None
    has_model_prob = _as_float(rec.get("model_probability")) is not None
    has_edge = _as_float(rec.get("edge")) is not None
    has_quotes = has_quote_metadata(rec)

    if has_risk_edge and has_model_prob and has_quotes:
        return "MODERN_FULL_METADATA"
    if has_risk_edge:
        return "MODERN_EDGE_ONLY"
    if has_edge:
        return "LEGACY_EDGE_ONLY"
    return "MISSING_EDGE"


def calibration_probability(rec: dict):
    value = _as_float(rec.get("model_probability"))
    if value is not None:
        return value
    return _as_float(rec.get("confidence"))


def calibration_bucket(rec: dict) -> str:
    probability = calibration_probability(rec)
    if probability is None:
        return "unknown"
    if probability < 0.50:
        return "<0.50"
    if probability < 0.60:
        return "0.50-0.59"
    if probability < 0.70:
        return "0.60-0.69"
    if probability < 0.80:
        return "0.70-0.79"
    if probability < 0.90:
        return "0.80-0.89"
    return ">=0.90"


def realized_outcome(rec: dict):
    pnl = get_pnl(rec)
    if pnl > 0:
        return 1.0
    if pnl < 0:
        return 0.0
    return None


def edge_source_quality_flags(source: str, rows: list[dict], metrics: dict,
                              high_edge_losers: int, overall_metrics: dict) -> list[str]:
    flags = []
    missing_quote_rows = [
        r for r in rows
        if not any(r.get(k) is not None for k in ("price_yes", "price_no", "yes_ask", "no_ask", "yes_bid", "no_bid"))
    ]
    if source == "LEGACY_EDGE_ONLY" and (metrics["total_pnl"] < 0 or high_edge_losers > 0):
        flags.append("LEGACY_EDGE_DRIVING_LOSSES")
    if source == "RISK_EDGE_AVAILABLE" and metrics["count"] < SAMPLE_WARNING_THRESHOLD:
        flags.append("RISK_EDGE_NOT_PROVEN")
    if source == "MISSING_EDGE" or missing_quote_rows:
        flags.append("MISSING_METADATA")
    if overall_metrics["flag"] == "NEGATIVE_EXPECTANCY":
        flags.append("DO_NOT_SCALE")
    return flags


def modern_legacy_flags(group: str, rows: list[dict], metrics: dict,
                        high_edge_losers: int, overall_metrics: dict) -> list[str]:
    flags = []
    if group == "LEGACY_EDGE_ONLY":
        flags.append("LEGACY_CONTAMINATION")
    if group.startswith("MODERN") and metrics["count"] < SAMPLE_WARNING_THRESHOLD:
        flags.append("MODERN_SAMPLE_TOO_SMALL")
    if group in {"MODERN_EDGE_ONLY", "MISSING_EDGE"}:
        flags.append("MISSING_METADATA")
    if group == "MODERN_FULL_METADATA" and any(not has_quote_metadata(r) for r in rows):
        flags.append("MISSING_METADATA")
    if metrics["total_pnl"] < 0 or metrics["flag"] == "NEGATIVE_EXPECTANCY":
        flags.append("NEGATIVE_EXPECTANCY")
    if overall_metrics["total_pnl"] < 0 or overall_metrics["flag"] == "NEGATIVE_EXPECTANCY":
        flags.append("DO_NOT_SCALE")
    if high_edge_losers and group == "LEGACY_EDGE_ONLY":
        flags.append("LEGACY_CONTAMINATION")
    return list(dict.fromkeys(flags))


def calibration_flags(metrics: dict, avg_clv: float | None, sample_count: int) -> list[str]:
    flags = []
    if sample_count < SAMPLE_WARNING_THRESHOLD:
        flags.append("SAMPLE_TOO_SMALL")

    avg_pred = metrics.get("avg_predicted_probability")
    win_rate = metrics.get("realized_win_rate")
    gap = metrics.get("calibration_gap")
    if avg_pred is not None and win_rate is not None and gap is not None:
        if gap < -0.10:
            flags.append("OVERCONFIDENT")
        elif gap > 0.10:
            flags.append("UNDERCONFIDENT")

    if metrics.get("roi", 0.0) < 0:
        flags.append("NEGATIVE_ROI")
    if avg_clv is not None and avg_clv < 0:
        flags.append("NEGATIVE_CLV")
    flags.append("DO_NOT_SCALE")
    return flags


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


def entry_price_bucket(rec: dict) -> str:
    price = _as_float(rec.get("entry_price"))
    if price is None:
        return "unknown"
    if price < 0.20:
        return "<0.20"
    if price < 0.40:
        return "0.20-0.39"
    if price < 0.60:
        return "0.40-0.59"
    if price < 0.80:
        return "0.60-0.79"
    return ">=0.80"


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
        v for v in (edge_value(r) for r in rows) if v is not None
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


def calc_asymmetry(rows: list[dict]) -> dict:
    wins = [get_pnl(r) for r in rows if get_pnl(r) > 0]
    losses = [get_pnl(r) for r in rows if get_pnl(r) < 0]
    pushes = [get_pnl(r) for r in rows if get_pnl(r) == 0]
    gross_wins = sum(wins)
    gross_losses = sum(losses)
    total_pnl = gross_wins + gross_losses
    wagered = sum(get_size(r) for r in rows)
    avg_win = gross_wins / len(wins) if wins else None
    avg_loss = gross_losses / len(losses) if losses else None

    payoff_ratio = None
    breakeven_win_rate = None
    actual_minus_breakeven = None
    profit_factor = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        abs_avg_loss = abs(avg_loss)
        payoff_ratio = avg_win / abs_avg_loss if abs_avg_loss else None
        breakeven_win_rate = abs_avg_loss / (avg_win + abs_avg_loss)
    if gross_wins > 0 and gross_losses < 0:
        profit_factor = gross_wins / abs(gross_losses)

    win_loss_count = len(wins) + len(losses)
    win_rate = len(wins) / win_loss_count if win_loss_count else 0.0
    if breakeven_win_rate is not None:
        actual_minus_breakeven = win_rate - breakeven_win_rate

    flag = ""
    if actual_minus_breakeven is not None:
        if actual_minus_breakeven < 0:
            flag = "NEGATIVE_EXPECTANCY"
        elif len(rows) < SAMPLE_WARNING_THRESHOLD:
            flag = "WATCHLIST_NOT_PROVEN"
        else:
            flag = "POSITIVE_EXPECTANCY_SAMPLE_GE_30"

    return {
        "count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "pushes": len(pushes),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff_ratio,
        "profit_factor": profit_factor,
        "breakeven_win_rate": breakeven_win_rate,
        "actual_minus_breakeven": actual_minus_breakeven,
        "total_pnl": total_pnl,
        "roi": total_pnl / wagered if wagered else 0.0,
        "flag": flag,
    }


def calc_calibration(rows: list[dict]) -> dict:
    scored = []
    for rec in rows:
        probability = calibration_probability(rec)
        outcome = realized_outcome(rec)
        if probability is None or outcome is None:
            continue
        scored.append((probability, outcome))

    expected_wins = sum(prob for prob, _ in scored)
    actual_wins = sum(outcome for _, outcome in scored)
    brier = (
        sum((prob - outcome) ** 2 for prob, outcome in scored) / len(scored)
        if scored else None
    )
    gap = actual_wins - expected_wins
    return {
        "scored_count": len(scored),
        "expected_wins": expected_wins,
        "actual_wins": actual_wins,
        "expected_minus_actual": expected_wins - actual_wins,
        "actual_minus_expected": gap,
        "brier_score": brier,
    }


def calc_bucket_calibration(rows: list[dict]) -> dict:
    metrics = calc_metrics(rows)
    probabilities = [
        p for p in (calibration_probability(r) for r in rows)
        if p is not None
    ]
    outcomes = [
        o for o in (realized_outcome(r) for r in rows)
        if o is not None
    ]
    avg_pred = _avg(probabilities)
    realized_win_rate = sum(outcomes) / len(outcomes) if outcomes else None
    calibration_gap = (
        realized_win_rate - avg_pred
        if realized_win_rate is not None and avg_pred is not None
        else None
    )
    return {
        **metrics,
        "avg_predicted_probability": avg_pred,
        "realized_win_rate": realized_win_rate,
        "calibration_gap": calibration_gap,
    }


def row_true_ev(rec: dict) -> dict | None:
    probability = calibration_probability(rec)
    entry_price = _as_float(rec.get("entry_price"))
    if probability is None or entry_price is None or entry_price <= 0:
        return None

    win_profit_per_contract = 1.0 - entry_price
    loss_per_contract = entry_price
    payout_ratio = (
        win_profit_per_contract / loss_per_contract
        if loss_per_contract > 0 else None
    )
    predicted_ev_per_contract = (
        probability * win_profit_per_contract
        - (1.0 - probability) * loss_per_contract
    )
    predicted_ev_roi = predicted_ev_per_contract / entry_price
    realized_roi = get_pnl(rec) / get_size(rec) if get_size(rec) else None
    ev_gap = (
        realized_roi - predicted_ev_roi
        if realized_roi is not None else None
    )

    return {
        "predicted_probability": probability,
        "entry_price": entry_price,
        "win_profit_per_contract": win_profit_per_contract,
        "loss_per_contract": loss_per_contract,
        "payout_ratio": payout_ratio,
        "predicted_ev_per_contract": predicted_ev_per_contract,
        "predicted_ev_roi": predicted_ev_roi,
        "realized_roi": realized_roi,
        "ev_gap": ev_gap,
    }


def calc_true_ev(rows: list[dict]) -> dict:
    asymmetry = calc_asymmetry(rows)
    ev_rows = [ev for ev in (row_true_ev(r) for r in rows) if ev is not None]
    clv_vals = [v for v in (get_clv(r) for r in rows) if v is not None]
    edge_vals = [edge_value(r) for r in rows if edge_value(r) is not None]

    return {
        **asymmetry,
        "ev_count": len(ev_rows),
        "avg_predicted_probability": _avg([
            ev["predicted_probability"] for ev in ev_rows
        ]),
        "avg_entry_price": _avg([ev["entry_price"] for ev in ev_rows]),
        "avg_payout_ratio": _avg([
            ev["payout_ratio"] for ev in ev_rows
            if ev["payout_ratio"] is not None
        ]),
        "avg_predicted_ev_roi": _avg([
            ev["predicted_ev_roi"] for ev in ev_rows
        ]),
        "avg_realized_roi": _avg([
            ev["realized_roi"] for ev in ev_rows
            if ev["realized_roi"] is not None
        ]),
        "avg_ev_gap": _avg([
            ev["ev_gap"] for ev in ev_rows
            if ev["ev_gap"] is not None
        ]),
        "avg_clv": _avg(clv_vals),
        "avg_edge": _avg(edge_vals),
    }


def true_ev_flags(name: str, rows: list[dict], metrics: dict) -> list[str]:
    flags = []
    if metrics["count"] < SAMPLE_WARNING_THRESHOLD:
        flags.append("SAMPLE_TOO_SMALL")
    if metrics["avg_predicted_ev_roi"] is not None and metrics["avg_predicted_ev_roi"] < 0:
        flags.append("NEGATIVE_PREDICTED_EV")
    if metrics["roi"] < 0:
        flags.append("NEGATIVE_REALIZED_ROI")
    if (
        metrics["avg_payout_ratio"] is not None
        and metrics["avg_payout_ratio"] < 1.0
    ) or (
        metrics["payoff_ratio"] is not None
        and metrics["payoff_ratio"] < 1.0
    ):
        flags.append("BAD_PAYOUT_ASYMMETRY")
    if metrics["avg_ev_gap"] is not None and metrics["avg_ev_gap"] < -0.10:
        flags.append("OVERPREDICTED_EV")
    if metrics["avg_clv"] is not None and metrics["avg_clv"] < 0:
        flags.append("NEGATIVE_CLV")
    if name == "LEGACY_EDGE_ONLY" or any(row_quality_group(r) == "LEGACY_EDGE_ONLY" for r in rows):
        flags.append("LEGACY_CONTAMINATION")
    flags.append("DO_NOT_SCALE")
    return list(dict.fromkeys(flags))


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
    label = "  [tiny sample]" if 0 < m["count"] < 5 else ""
    print(f"{name:<28} n={m['count']:>3}  W={m['wins']:>3}  L={m['losses']:>3}  P={m['pushes']:>3}  "
          f"WR={_fmt_pct(m['win_rate']):>6}  PnL={_fmt_money(m['total_pnl']):>9}  "
          f"Avg={_fmt_money(m['avg_pnl']):>8}  ROI={_fmt_pct(m['roi']):>8}{label}")
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


def print_asymmetry_row(name: str, rows: list[dict]) -> None:
    m = calc_asymmetry(rows)
    sample_note = "tiny sample" if 0 < m["count"] < 5 else ""
    flag = m["flag"] or ""
    print(
        f"{name:<34} n={m['count']:>3} W={m['wins']:>3} L={m['losses']:>3} P={m['pushes']:>3} "
        f"WR={_fmt_pct(m['win_rate']):>6} "
        f"avgW={_fmt_money(m['avg_win']) if m['avg_win'] is not None else 'n/a':>9} "
        f"avgL={_fmt_money(m['avg_loss']) if m['avg_loss'] is not None else 'n/a':>9} "
        f"W/L={_fmt_ratio(m['payoff_ratio']):>4} "
        f"PF={_fmt_ratio(m['profit_factor']):>4} "
        f"BE_WR={_fmt_pct(m['breakeven_win_rate']) if m['breakeven_win_rate'] is not None else 'n/a':>6} "
        f"WR-BE={_fmt_pct(m['actual_minus_breakeven']) if m['actual_minus_breakeven'] is not None else 'n/a':>7} "
        f"PnL={_fmt_money(m['total_pnl']):>9} ROI={_fmt_pct(m['roi']):>8} "
        f"{flag}{' | ' + sample_note if sample_note else ''}"
    )


def print_asymmetry_group(title: str, rows: list[dict], key_func, order: list[str] | None = None) -> None:
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
        print_asymmetry_row(str(key), bucket_rows)
        printed = True
    if not printed:
        print("  (no rows)")


def edge_bucket_floor(bucket: str) -> float | None:
    if bucket == "<0.00":
        return -1.0
    if bucket == "0.00-0.009":
        return 0.0
    if bucket == "0.01-0.019":
        return 0.01
    if bucket == "0.02-0.029":
        return 0.02
    if bucket == "0.03-0.049":
        return 0.03
    if bucket == "0.05-0.079":
        return 0.05
    if bucket == ">=0.08":
        return 0.08
    return None


def edge_truth_flags(bucket: str, rows: list[dict], metrics: dict, overall_metrics: dict,
                     inverted: bool) -> list[str]:
    flags = []
    if metrics["count"] < SAMPLE_WARNING_THRESHOLD:
        flags.append("EDGE_NOT_PROVEN")
    if metrics["count"] >= 5 and metrics["roi"] < 0:
        flags.append("EDGE_FAILED")
    if bucket == ">=0.08" and metrics["roi"] < 0:
        flags.append("HIGH_EDGE_DANGER")
    if inverted:
        flags.append("EDGE_INVERTED")
    if overall_metrics["flag"] == "NEGATIVE_EXPECTANCY":
        flags.append("DO_NOT_SCALE")
    return flags


def print_edge_source_quality(rows: list[dict]) -> None:
    print()
    print("EDGE SOURCE QUALITY")
    print("-------------------")
    print("RISK_EDGE_AVAILABLE uses explicit risk_edge. LEGACY_EDGE_ONLY falls back to old edge.")

    groups = group_by(rows, edge_source_quality)
    overall = calc_asymmetry(rows)
    high_edge_failures_by_source = defaultdict(int)
    for rec in rows:
        edge = edge_value(rec)
        if edge is not None and edge >= 0.08 and get_pnl(rec) < 0:
            high_edge_failures_by_source[edge_source_quality(rec)] += 1

    for source in ["RISK_EDGE_AVAILABLE", "LEGACY_EDGE_ONLY", "MISSING_EDGE"]:
        source_rows = groups.get(source, [])
        if not source_rows:
            continue
        metrics = calc_asymmetry(source_rows)
        edge_vals = [edge_value(r) for r in source_rows if edge_value(r) is not None]
        high_edge_losers = high_edge_failures_by_source.get(source, 0)
        flags = edge_source_quality_flags(source, source_rows, metrics, high_edge_losers, overall)
        print(
            f"{source:<20} n={metrics['count']:>3} "
            f"W={metrics['wins']:>3} L={metrics['losses']:>3} P={metrics['pushes']:>3} "
            f"WR={_fmt_pct(metrics['win_rate']):>6} "
            f"PnL={_fmt_money(metrics['total_pnl']):>9} "
            f"ROI={_fmt_pct(metrics['roi']):>8} "
            f"avg_edge={_fmt_num(_avg(edge_vals)):>8} "
            f"avgW={_fmt_money(metrics['avg_win']) if metrics['avg_win'] is not None else 'n/a':>9} "
            f"avgL={_fmt_money(metrics['avg_loss']) if metrics['avg_loss'] is not None else 'n/a':>9} "
            f"PF={_fmt_ratio(metrics['profit_factor']):>4} "
            f"high_edge_losers={high_edge_losers} "
            f"{' | '.join(flags) if flags else 'OK_TO_WATCH'}"
        )

    if high_edge_failures_by_source.get("LEGACY_EDGE_ONLY", 0) > 0:
        print(
            "[EDGE SOURCE WARNING] Legacy edge-only rows are driving high-edge "
            "failures. Treat old high-edge buckets separately from modern risk_edge rows."
        )


def print_modern_vs_legacy_evidence(rows: list[dict]) -> None:
    print()
    print("MODERN VS LEGACY EVIDENCE")
    print("-------------------------")
    print("MODERN_FULL_METADATA requires risk_edge + model_probability + quote metadata.")
    print("MODERN_EDGE_ONLY has risk_edge but lacks complete quote/model context.")
    print("LEGACY_EDGE_ONLY has only old edge fallback. MISSING_EDGE has no usable edge.")

    groups = group_by(rows, row_quality_group)
    overall = calc_asymmetry(rows)
    high_edge_failures_by_group = defaultdict(int)
    modern_full_rows = groups.get("MODERN_FULL_METADATA", [])
    modern_rows = modern_full_rows + groups.get("MODERN_EDGE_ONLY", [])

    for rec in rows:
        edge = edge_value(rec)
        if edge is not None and edge >= 0.08 and get_pnl(rec) < 0:
            high_edge_failures_by_group[row_quality_group(rec)] += 1

    for group in [
        "MODERN_FULL_METADATA",
        "MODERN_EDGE_ONLY",
        "LEGACY_EDGE_ONLY",
        "MISSING_EDGE",
    ]:
        group_rows = groups.get(group, [])
        if not group_rows:
            continue
        metrics = calc_asymmetry(group_rows)
        edge_vals = [edge_value(r) for r in group_rows if edge_value(r) is not None]
        clv_vals = [v for v in (get_clv(r) for r in group_rows) if v is not None]
        high_edge_losers = high_edge_failures_by_group.get(group, 0)
        flags = modern_legacy_flags(group, group_rows, metrics, high_edge_losers, overall)
        print(
            f"{group:<22} n={metrics['count']:>3} "
            f"W={metrics['wins']:>3} L={metrics['losses']:>3} P={metrics['pushes']:>3} "
            f"WR={_fmt_pct(metrics['win_rate']):>6} "
            f"PnL={_fmt_money(metrics['total_pnl']):>9} "
            f"ROI={_fmt_pct(metrics['roi']):>8} "
            f"avg_edge={_fmt_num(_avg(edge_vals)):>8} "
            f"avgW={_fmt_money(metrics['avg_win']) if metrics['avg_win'] is not None else 'n/a':>9} "
            f"avgL={_fmt_money(metrics['avg_loss']) if metrics['avg_loss'] is not None else 'n/a':>9} "
            f"PF={_fmt_ratio(metrics['profit_factor']):>4} "
            f"avgCLV={_fmt_num(_avg(clv_vals)):>8} "
            f"high_edge_losers={high_edge_losers} "
            f"{' | '.join(flags) if flags else 'OK_TO_WATCH'}"
        )

    if not modern_full_rows:
        print(
            "[MODERN VERDICT] No modern full-metadata evaluated rows yet. "
            "Do not judge current model quality from legacy rows."
        )
    elif len(modern_full_rows) < SAMPLE_WARNING_THRESHOLD:
        print("[MODERN VERDICT] Modern sample is too small. Watch only.")
    else:
        modern_full_metrics = calc_asymmetry(modern_full_rows)
        if modern_full_metrics["roi"] < 0:
            print("[MODERN VERDICT] Modern sample negative. Do not scale.")
        else:
            print("[MODERN VERDICT] Modern sample is non-negative, but still requires calibration review before scaling.")

    if modern_rows and len(modern_rows) < SAMPLE_WARNING_THRESHOLD:
        print("[MODERN WARNING] All modern evaluated evidence is still below 30 rows.")

    if modern_full_rows:
        dc_count = sum(1 for r in modern_full_rows if r.get("data_collection_override"))
        normal_count = len(modern_full_rows) - dc_count
        print()
        print(f"DATA_COLLECTION_OVERRIDE split (MODERN_FULL_METADATA n={len(modern_full_rows)}):")
        print(f"  council_approved (normal trades): n={normal_count}")
        print(f"  council_rejected (data_collection_override=True): n={dc_count}")
        if dc_count > 0 and normal_count == 0:
            print(
                "  [WARNING] ALL modern evidence is from council-REJECTED data_collection_override trades. "
                "This is NOT evidence of normal operation edge. "
                "The system has zero evaluated council-approved modern trades."
            )
        elif dc_count > 0:
            print(
                f"  [NOTE] {dc_count}/{len(modern_full_rows)} modern trades are council-rejected data_collection. "
                f"Separate council-approved evidence: n={normal_count}."
            )


def print_modern_calibration_report(rows: list[dict]) -> None:
    print()
    print("MODERN CALIBRATION REPORT")
    print("-------------------------")
    print("Primary sample: evaluated MODERN_FULL_METADATA rows only.")
    print("Predicted probability uses model_probability first, then confidence fallback.")
    print("Outcome is 1 for positive PnL, 0 for negative PnL; pushes are excluded from calibration scoring.")

    modern_rows = [r for r in rows if row_quality_group(r) == "MODERN_FULL_METADATA"]
    if len(modern_rows) < 5:
        print(
            "[CALIBRATION WARNING] Modern calibration sample too small. "
            "This is plumbing verification, not statistical evidence."
        )

    overall = calc_calibration(modern_rows)
    print(
        f"Modern rows: n={len(modern_rows)} scored={overall['scored_count']} "
        f"Brier={_fmt_ratio(overall['brier_score'], 4)} "
        f"ExpectedWins={_fmt_ratio(overall['expected_wins'], 2)} "
        f"ActualWins={_fmt_ratio(overall['actual_wins'], 2)} "
        f"ExpectedMinusActual={_fmt_ratio(overall['expected_minus_actual'], 2)}"
    )

    if overall["scored_count"] == 0:
        print("[CALIBRATION VERDICT] No scored modern rows yet. Confidence is unproven.")
    elif overall["expected_minus_actual"] > max(1.0, overall["scored_count"] * 0.10):
        print("[CALIBRATION WARNING] OVERCONFIDENT: expected wins meaningfully exceed actual wins.")
    elif overall["scored_count"] < SAMPLE_WARNING_THRESHOLD:
        print("[CALIBRATION VERDICT] Confidence is unproven; modern sample is below 30 rows.")

    groups = group_by(modern_rows, calibration_bucket)
    printed = False
    for bucket in CALIBRATION_BUCKET_ORDER:
        bucket_rows = groups.get(bucket, [])
        if not bucket_rows:
            continue
        m = calc_bucket_calibration(bucket_rows)
        clv_vals = [v for v in (get_clv(r) for r in bucket_rows) if v is not None]
        avg_clv = _avg(clv_vals)
        flags = calibration_flags(m, avg_clv, m["count"])
        print(
            f"{bucket:<10} n={m['count']:>3} "
            f"W={m['wins']:>3} L={m['losses']:>3} P={m['pushes']:>3} "
            f"avg_pred={_fmt_pct(m['avg_predicted_probability']) if m['avg_predicted_probability'] is not None else 'n/a':>6} "
            f"realized={_fmt_pct(m['realized_win_rate']) if m['realized_win_rate'] is not None else 'n/a':>6} "
            f"gap={_fmt_pct(m['calibration_gap']) if m['calibration_gap'] is not None else 'n/a':>7} "
            f"PnL={_fmt_money(m['total_pnl']):>9} "
            f"ROI={_fmt_pct(m['roi']):>8} "
            f"avgCLV={_fmt_num(avg_clv):>8} "
            f"avgEdge={_fmt_num(m['avg_risk_edge']):>8} "
            f"{' | '.join(flags)}"
        )
        printed = True

    if not printed:
        print("  (no modern full-metadata calibration buckets)")


def print_true_ev_row(name: str, rows: list[dict]) -> None:
    m = calc_true_ev(rows)
    flags = true_ev_flags(name, rows, m)
    print(
        f"{name:<36} n={m['count']:>3} ev_n={m['ev_count']:>3} "
        f"W={m['wins']:>3} L={m['losses']:>3} P={m['pushes']:>3} "
        f"WR={_fmt_pct(m['win_rate']):>6} "
        f"avgProb={_fmt_pct(m['avg_predicted_probability']) if m['avg_predicted_probability'] is not None else 'n/a':>6} "
        f"entry={_fmt_num(m['avg_entry_price']):>8} "
        f"payRatio={_fmt_ratio(m['avg_payout_ratio']):>5} "
        f"predEVROI={_fmt_pct(m['avg_predicted_ev_roi']) if m['avg_predicted_ev_roi'] is not None else 'n/a':>8} "
        f"realROI={_fmt_pct(m['roi']):>8} "
        f"EVgap={_fmt_pct(m['avg_ev_gap']) if m['avg_ev_gap'] is not None else 'n/a':>8} "
        f"CLV={_fmt_num(m['avg_clv']):>8} "
        f"PF={_fmt_ratio(m['profit_factor']):>4} "
        f"{' | '.join(flags)}"
    )


def print_true_ev_group(title: str, rows: list[dict], key_func,
                        order: list[str] | None = None) -> None:
    print()
    print(title)
    print("-" * len(title))
    groups = group_by(rows, key_func)
    keys = order or sorted(groups)
    printed = False
    for key in keys:
        group_rows = groups.get(key, [])
        if not group_rows:
            continue
        print_true_ev_row(str(key), group_rows)
        printed = True
    if not printed:
        print("  (no rows)")


def print_true_ev_report(rows: list[dict]) -> None:
    print()
    print("TRUE EV + PAYOUT ASYMMETRY REPORT")
    print("---------------------------------")
    print("Predicted probability uses model_probability first, then confidence fallback.")
    print("Formulas: win_profit=1-entry_price; loss=entry_price;")
    print("predEV/contract=p*(1-entry)-(1-p)*entry; predEVROI=predEV/entry.")
    print("realROI is realized PnL divided by trade size. EVgap=realROI-predEVROI.")

    print_true_ev_row("OVERALL", rows)
    print_true_ev_group(
        "TRUE EV BY EVIDENCE QUALITY",
        rows,
        row_quality_group,
        ROW_QUALITY_ORDER,
    )
    print_true_ev_group(
        "TRUE EV BY ENTRY PRICE BUCKET",
        rows,
        entry_price_bucket,
        ENTRY_PRICE_ORDER,
    )
    print_true_ev_group(
        "TRUE EV BY EDGE BUCKET",
        rows,
        edge_bucket,
        EDGE_ORDER,
    )
    print_true_ev_group(
        "TRUE EV BY CONFIDENCE / PROBABILITY BUCKET",
        rows,
        calibration_bucket,
        CALIBRATION_BUCKET_ORDER,
    )
    print_true_ev_group("TRUE EV BY STRATEGY", rows, strategy_key)
    print_true_ev_group(
        "TRUE EV BY MARKET HORIZON",
        rows,
        market_horizon,
        HORIZON_ORDER,
    )

    modern_full = [r for r in rows if row_quality_group(r) == "MODERN_FULL_METADATA"]
    if len(modern_full) < SAMPLE_WARNING_THRESHOLD:
        print(
            "[TRUE EV VERDICT] Modern full-metadata sample is too small. "
            "This report is evidence plumbing, not proof of edge."
        )
    modern_metrics = calc_true_ev(modern_full)
    if modern_full and modern_metrics["roi"] < 0:
        print("[TRUE EV VERDICT] Modern realized ROI is negative. Do not scale.")
    elif modern_full:
        print("[TRUE EV VERDICT] Modern realized ROI is non-negative, but not statistically proven.")


def print_edge_truth_audit(rows: list[dict]) -> None:
    print()
    print("EDGE TRUTH AUDIT (clean settled + time exits)")
    print("---------------------------------------------")
    print("Edge label here means risk_edge if present, else logged edge.")
    print("Logged edge is model probability minus entry price minus fee estimate;")
    print("this audit checks whether that label maps to realized ROI.")

    groups = group_by(rows, edge_bucket)
    overall = calc_asymmetry(rows)
    roi_by_bucket = {}
    avg_edge_by_bucket = {}
    for bucket in EDGE_ORDER:
        bucket_rows = groups.get(bucket, [])
        if not bucket_rows:
            continue
        metrics = calc_asymmetry(bucket_rows)
        edge_vals = [edge_value(r) for r in bucket_rows if edge_value(r) is not None]
        roi_by_bucket[bucket] = metrics["roi"]
        avg_edge_by_bucket[bucket] = _avg(edge_vals)

    best_prior_roi = None
    inversion_flags = set()
    for bucket in EDGE_ORDER:
        if bucket not in roi_by_bucket:
            continue
        floor = edge_bucket_floor(bucket)
        if floor is None:
            continue
        roi = roi_by_bucket[bucket]
        if best_prior_roi is not None and roi < best_prior_roi:
            inversion_flags.add(bucket)
        best_prior_roi = roi if best_prior_roi is None else max(best_prior_roi, roi)

    printed = False
    for bucket in EDGE_ORDER:
        bucket_rows = groups.get(bucket, [])
        if not bucket_rows:
            continue
        metrics = calc_asymmetry(bucket_rows)
        avg_pred_edge = avg_edge_by_bucket.get(bucket)
        flags = edge_truth_flags(
            bucket,
            bucket_rows,
            metrics,
            overall,
            bucket in inversion_flags,
        )
        print(
            f"{bucket:<12} n={metrics['count']:>3} W={metrics['wins']:>3} "
            f"L={metrics['losses']:>3} P={metrics['pushes']:>3} "
            f"WR={_fmt_pct(metrics['win_rate']):>6} "
            f"avg_edge={_fmt_num(avg_pred_edge):>8} "
            f"PnL={_fmt_money(metrics['total_pnl']):>9} "
            f"ROI={_fmt_pct(metrics['roi']):>8} "
            f"avgW={_fmt_money(metrics['avg_win']) if metrics['avg_win'] is not None else 'n/a':>9} "
            f"avgL={_fmt_money(metrics['avg_loss']) if metrics['avg_loss'] is not None else 'n/a':>9} "
            f"PF={_fmt_ratio(metrics['profit_factor']):>4} "
            f"{' | '.join(flags) if flags else 'OK_TO_WATCH'}"
        )
        printed = True

    if not printed:
        print("  (no edge buckets)")

    high_edge = groups.get(">=0.08", [])
    if high_edge:
        high_metrics = calc_asymmetry(high_edge)
        if high_metrics["roi"] < 0:
            print(
                "[EDGE WARNING] High-edge bucket is negative. Treat current "
                "high-edge labels as dangerous until proven otherwise."
            )
    if overall["flag"] == "NEGATIVE_EXPECTANCY":
        print("[EDGE WARNING] Overall evaluated expectancy is negative. DO_NOT_SCALE.")


def inferred_edge_fee(rec: dict) -> float | None:
    confidence = _as_float(rec.get("original_confidence"))
    if confidence is None:
        confidence = _as_float(rec.get("confidence"))
    entry = _as_float(rec.get("entry_price"))
    edge = edge_value(rec)
    if confidence is None or entry is None or edge is None:
        return None
    return confidence - entry - edge


def high_edge_loser_flags(rec: dict) -> list[str]:
    flags = []
    entry = _as_float(rec.get("entry_price"))
    exit_price = _as_float(rec.get("exit_price"))
    clv = get_clv(rec)
    inferred_fee = inferred_edge_fee(rec)

    if edge_source(rec) == "edge":
        flags.append("LEGACY_EDGE_FALLBACK")
    if not any(rec.get(k) is not None for k in ("price_yes", "price_no", "yes_ask", "no_ask", "yes_bid", "no_bid")):
        flags.append("NO_QUOTE_FIELDS")
    if entry is not None and entry < 0.20:
        flags.append("CHEAP_ENTRY_LOTTERY")
    if inferred_fee is not None and abs(inferred_fee - 0.01) < 0.0001:
        flags.append("EDGE_EQUALS_CONF_MINUS_ENTRY_MINUS_0.01")
    if clv is not None and clv < 0:
        flags.append("NEGATIVE_CLV")
    if rec.get("status") == "SETTLED" and exit_price == 0:
        flags.append("FULL_SETTLEMENT_LOSS")
    if rec.get("strategy") and str(rec.get("strategy")).upper().startswith("TREND"):
        flags.append("TREND_SOURCE")
    return flags


def print_high_edge_loser_forensics(rows: list[dict]) -> None:
    losers = [
        r for r in rows
        if edge_value(r) is not None and edge_value(r) >= 0.08 and get_pnl(r) < 0
    ]

    print()
    print("HIGH EDGE LOSER FORENSICS")
    print("-------------------------")
    print("Rows shown: evaluated trades with edge >= 0.08 and negative PnL.")
    print("model_probability below means logged side confidence when explicit model_probability is absent.")

    if not losers:
        print("  (no high-edge losers)")
        return

    source_counts = defaultdict(int)
    flag_counts = defaultdict(int)
    for rec in losers:
        source_counts[edge_source(rec)] += 1
        for flag in high_edge_loser_flags(rec):
            flag_counts[flag] += 1

    print(
        "Summary: "
        + ", ".join(f"{source}={count}" for source, count in sorted(source_counts.items()))
        + " | "
        + ", ".join(f"{flag}={count}" for flag, count in sorted(flag_counts.items()))
    )

    for rec in losers:
        confidence = _as_float(rec.get("original_confidence"))
        if confidence is None:
            confidence = _as_float(rec.get("confidence"))
        inferred_fee = inferred_edge_fee(rec)
        quote_bits = {
            "yes_price": rec.get("yes_price") or rec.get("price_yes") or rec.get("yes_ask"),
            "no_price": rec.get("no_price") or rec.get("price_no") or rec.get("no_ask"),
            "yes_bid": rec.get("yes_bid"),
            "no_bid": rec.get("no_bid"),
            "market_mid": rec.get("market_mid"),
        }
        title = rec.get("title") or rec.get("question") or ""
        if len(str(title)) > 70:
            title = str(title)[:67] + "..."

        print()
        print(f"- {rec.get('timestamp', 'n/a')} | {rec.get('ticker', 'n/a')}")
        if title:
            print(f"  market/question: {title}")
        print(
            f"  horizon={market_horizon(rec)} strategy={rec.get('strategy', 'n/a')} "
            f"raw_strategy={rec.get('raw_strategy', 'n/a')} side={rec.get('action', 'n/a')} "
            f"edge_quality={edge_source_quality(rec)}"
        )
        print(
            f"  outcome={rec.get('status', 'n/a')}/{rec.get('result', 'n/a')} "
            f"entry={_fmt_field(_as_float(rec.get('entry_price')))} "
            f"exit={_fmt_field(_as_float(rec.get('exit_price')))} "
            f"size={_fmt_money(get_size(rec))} pnl={_fmt_money(get_pnl(rec))} "
            f"clv={_fmt_num(get_clv(rec))}"
        )
        print(
            f"  model_probability={_fmt_num(confidence)} "
            f"confidence={_fmt_num(_as_float(rec.get('confidence')))} "
            f"original_confidence={_fmt_num(_as_float(rec.get('original_confidence')))} "
            f"council_confidence={_fmt_num(_as_float(rec.get('council_confidence')))}"
        )
        print(
            f"  edge_source={edge_source(rec)} edge={_fmt_num(_as_float(rec.get('edge')))} "
            f"original_edge={_fmt_num(_as_float(rec.get('original_edge')))} "
            f"adjusted_edge={_fmt_num(_as_float(rec.get('adjusted_edge')))} "
            f"risk_edge={_fmt_num(_as_float(rec.get('risk_edge')))} "
            f"inferred_fee={_fmt_num(inferred_fee)}"
        )
        print(
            "  quotes: "
            f"yes_price={_fmt_field(quote_bits['yes_price'])} "
            f"no_price={_fmt_field(quote_bits['no_price'])} "
            f"yes_bid={_fmt_field(quote_bits['yes_bid'])} "
            f"no_bid={_fmt_field(quote_bits['no_bid'])} "
            f"market_mid={_fmt_field(quote_bits['market_mid'])}"
        )
        print(f"  decision_reason={rec.get('reasoning') or rec.get('decision_reason') or 'n/a'}")
        print(f"  flags={', '.join(high_edge_loser_flags(rec)) or 'none'}")


def print_payout_asymmetry_report(rows: list[dict]) -> None:
    print()
    print("PAYOUT / ASYMMETRY DIAGNOSTICS (clean settled + time exits)")
    print("-----------------------------------------------------------")
    print("A group can win more often than it loses and still lose money if")
    print("avg losses are larger than avg wins. BE_WR is the breakeven win rate.")
    print_asymmetry_row("OVERALL", rows)
    print_asymmetry_group("ASYMMETRY BY STRATEGY", rows, strategy_key)
    print_asymmetry_group("ASYMMETRY BY MARKET HORIZON", rows, market_horizon, HORIZON_ORDER)
    print_asymmetry_group("ASYMMETRY BY ENTRY PRICE", rows, entry_price_bucket, ENTRY_PRICE_ORDER)
    print_asymmetry_group(
        "ASYMMETRY BY EDGE BUCKET",
        rows,
        edge_bucket,
        EDGE_ORDER,
    )


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
        for bucket in EDGE_ORDER:
            bucket_rows = edge_groups.get(bucket, [])
            if not bucket_rows:
                continue
            print_metrics(f"    edge {bucket}", bucket_rows)


def print_entry_price_inside_horizons(rows: list[dict]) -> None:
    print()
    print("ENTRY PRICE BUCKETS INSIDE MARKET HORIZON")
    print("-----------------------------------------")
    horizon_groups = group_by(rows, market_horizon)
    printed = False
    for horizon in HORIZON_ORDER:
        horizon_rows = horizon_groups.get(horizon, [])
        if not horizon_rows:
            continue
        printed = True
        print()
        print(f"{horizon}")
        print("-" * len(horizon))
        entry_groups = group_by(horizon_rows, entry_price_bucket)
        for bucket in ENTRY_PRICE_ORDER:
            bucket_rows = entry_groups.get(bucket, [])
            if not bucket_rows:
                continue
            print_metrics(f"  entry {bucket}", bucket_rows)
    if not printed:
        print("  (no rows)")


def collect_zone_candidates(rows: list[dict]) -> list[dict]:
    candidates = []
    specs = [
        ("horizon", market_horizon, HORIZON_ORDER),
        ("entry_price", entry_price_bucket, ENTRY_PRICE_ORDER),
        ("strategy", strategy_key, None),
        ("confidence", confidence_bucket, ["<0.50", "0.50-0.54", "0.55-0.59", "0.60-0.64",
                                           "0.65-0.69", "0.70-0.74", "0.75-0.79",
                                           ">=0.80", "unknown"]),
        ("edge", edge_bucket, EDGE_ORDER),
    ]

    for scope, key_func, order in specs:
        groups = group_by(rows, key_func)
        keys = order or sorted(groups)
        for key in keys:
            bucket_rows = groups.get(key, [])
            if not bucket_rows:
                continue
            candidates.append({
                "scope": scope,
                "bucket": key,
                "metrics": calc_metrics(bucket_rows),
            })

    horizon_groups = group_by(rows, market_horizon)
    for horizon in HORIZON_ORDER:
        horizon_rows = horizon_groups.get(horizon, [])
        if not horizon_rows:
            continue
        entry_groups = group_by(horizon_rows, entry_price_bucket)
        for bucket in ENTRY_PRICE_ORDER:
            bucket_rows = entry_groups.get(bucket, [])
            if not bucket_rows:
                continue
            candidates.append({
                "scope": "horizon_entry",
                "bucket": f"{horizon} / entry {bucket}",
                "metrics": calc_metrics(bucket_rows),
            })

    return candidates


def _zone_reason(m: dict, mode: str) -> list[str]:
    reasons = []
    if mode == "danger":
        if m["roi"] < -0.25:
            reasons.append(f"ROI {_fmt_pct(m['roi'])}")
        if m["win_rate"] == 0:
            reasons.append("win_rate 0.0%")
        if m["avg_clv"] is not None and m["avg_clv"] < 0:
            reasons.append(f"avg_clv {_fmt_num(m['avg_clv'])}")
    else:
        if m["roi"] > 0:
            reasons.append(f"ROI {_fmt_pct(m['roi'])}")
        if m["avg_clv"] is not None and m["avg_clv"] > 0:
            reasons.append(f"avg_clv {_fmt_num(m['avg_clv'])}")
        if m["win_rate"] >= 0.50:
            reasons.append(f"win_rate {_fmt_pct(m['win_rate'])}")
    return reasons


def print_zone_report(title: str, rows: list[dict], mode: str) -> None:
    print()
    print(title)
    print("-" * len(title))
    hits = []
    for candidate in collect_zone_candidates(rows):
        m = candidate["metrics"]
        if m["count"] < 2:
            continue
        reasons = _zone_reason(m, mode)
        if reasons:
            hits.append((candidate, reasons))

    if mode == "danger":
        hits.sort(key=lambda x: (x[0]["metrics"]["roi"], x[0]["metrics"]["avg_clv"] or 0))
    else:
        hits.sort(key=lambda x: (-x[0]["metrics"]["roi"], -(x[0]["metrics"]["avg_clv"] or 0)))

    if not hits:
        print("  (no buckets met criteria)")
        return

    for candidate, reasons in hits:
        m = candidate["metrics"]
        sample_note = "tiny sample" if m["count"] < 5 else "small sample" if m["count"] < SAMPLE_WARNING_THRESHOLD else "sample>=30"
        proof_note = ""
        if mode == "promising" and m["count"] < SAMPLE_WARNING_THRESHOLD:
            proof_note = " | watchlist only, not proven"
        print(
            f"{candidate['scope']:<14} {candidate['bucket']:<42} "
            f"n={m['count']:>3} WR={_fmt_pct(m['win_rate']):>6} "
            f"PnL={_fmt_money(m['total_pnl']):>9} ROI={_fmt_pct(m['roi']):>8} "
            f"CLV={_fmt_num(m['avg_clv'])} | {sample_note}{proof_note} | "
            f"{'; '.join(reasons)}"
        )


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


# ── Proof Gate Thresholds ────────────────────────────────────────────────────
_GATE_MIN_MODERN_WATCHLIST  = 30    # modern full-metadata trades needed for watchlist
_GATE_MIN_NORMAL_WATCHLIST  = 30    # council-approved modern trades needed for watchlist
_GATE_MIN_MODERN_SCALE      = 100   # modern full-metadata trades needed for scale eligibility
_GATE_MIN_NORMAL_SCALE      = 30    # council-approved modern trades needed for scale eligibility
_GATE_MIN_PROFIT_FACTOR     = 1.10  # minimum profit factor for scale eligibility


def evaluate_proof_gates(buckets: dict, evaluated: list) -> dict:
    """
    Read-only verdict: is the system proven enough to scale beyond learning mode?

    Tiers (first failing hard gate wins):
      NOT_PROVEN          — no council-approved modern trades at all
      DATA_COLLECTION_ONLY — sample too small or normal-approved count too low
      WATCHLIST           — enough samples but ROI/CLV/profit-factor not positive
      PAPER_VALIDATION_READY — performance gates pass, scale volume not yet met
      SCALE_ELIGIBLE      — all gates pass; still requires human sign-off

    scale_allowed and real_money_allowed are always False in code.
    """
    clean_settled = buckets["clean_settled"]
    active_opens  = buckets["active_open"]

    modern_full = [r for r in evaluated if row_quality_group(r) == "MODERN_FULL_METADATA"]
    modern_full_count  = len(modern_full)
    dc_count           = sum(1 for r in modern_full if r.get("data_collection_override"))
    normal_modern_count = modern_full_count - dc_count
    dc_pct = round(dc_count / modern_full_count * 100, 1) if modern_full_count else 0.0

    # Overall clean-settled performance (time exits excluded from this slice)
    cs_asym   = calc_asymmetry(clean_settled)
    cs_roi    = cs_asym["roi"]
    cs_pf     = cs_asym["profit_factor"]
    cs_clv_vals = [v for v in (get_clv(r) for r in clean_settled) if v is not None]
    cs_avg_clv  = round(sum(cs_clv_vals) / len(cs_clv_vals), 4) if cs_clv_vals else None

    # Modern full-metadata performance
    if modern_full:
        m_asym  = calc_asymmetry(modern_full)
        m_roi   = m_asym["roi"]
        m_pf    = m_asym["profit_factor"]
        m_clv_vals = [v for v in (get_clv(r) for r in modern_full) if v is not None]
        m_avg_clv  = round(sum(m_clv_vals) / len(m_clv_vals), 4) if m_clv_vals else None
    else:
        m_roi = m_pf = m_avg_clv = None

    active_open_count   = len(active_opens)
    risk_override_count = sum(1 for r in evaluated if r.get("risk_override_used"))
    try:
        profile = json.loads(EDGE_PROFILE_PATH.read_text()) if EDGE_PROFILE_PATH.exists() else {}
    except (OSError, json.JSONDecodeError):
        profile = {}
    edge_health = edge_profile_health(profile, EDGE_PROFILE_PATH)

    warnings = []
    if active_open_count > 0:
        warnings.append(
            f"{active_open_count} active unresolved open trade(s) — settlement still pending"
        )
    if risk_override_count > 0:
        warnings.append(
            f"{risk_override_count} evaluated trade(s) used learning risk override"
        )

    # ── Hard gates (first matching gate wins) ────────────────────────────────
    if modern_full_count == 0:
        verdict = "NOT_PROVEN"
        reason  = (
            "No modern full-metadata trades evaluated "
            "(requires risk_edge + model_probability + quote data)."
        )
        next_req = "Execute at least 1 modern full-metadata trade."

    elif normal_modern_count == 0:
        verdict = "NOT_PROVEN"
        reason  = (
            f"All {dc_count} modern full-metadata trade(s) are council-REJECTED "
            "data_collection_override. Zero council-approved normal trades exist. "
            "Data-collection evidence is NOT proof of normal model operation."
        )
        next_req = (
            "Resolve critic deadlock: rebuild edge_profile with >= 5 settled trades per "
            "bucket so council can approve at least 1 signal without data-collection override."
        )

    elif modern_full_count < _GATE_MIN_MODERN_WATCHLIST:
        verdict = "DATA_COLLECTION_ONLY"
        reason  = (
            f"Modern full-metadata sample too small: "
            f"{modern_full_count}/{_GATE_MIN_MODERN_WATCHLIST} minimum. "
            f"Normal council-approved: {normal_modern_count}."
        )
        next_req = (
            f"Accumulate {_GATE_MIN_MODERN_WATCHLIST - modern_full_count} more "
            "modern full-metadata trades."
        )

    elif normal_modern_count < _GATE_MIN_NORMAL_WATCHLIST:
        verdict = "DATA_COLLECTION_ONLY"
        reason  = (
            f"Normal council-approved modern trades: "
            f"{normal_modern_count}/{_GATE_MIN_NORMAL_WATCHLIST} minimum. "
            f"{dc_count} data_collection_override trades do NOT count as normal proof."
        )
        next_req = (
            f"Accumulate {_GATE_MIN_NORMAL_WATCHLIST - normal_modern_count} more "
            "council-approved (non-override) modern trades."
        )

    else:
        # Sample size gates passed — now check performance
        roi_ok = m_roi is not None and m_roi > 0
        clv_ok = m_avg_clv is not None and m_avg_clv > 0
        pf_ok  = m_pf is not None and m_pf > _GATE_MIN_PROFIT_FACTOR
        cs_ok  = cs_roi > 0

        perf_failures = []
        if not roi_ok:
            perf_failures.append(
                f"modern ROI {m_roi * 100:+.1f}% (need > 0%)"
                if m_roi is not None else "modern ROI unavailable"
            )
        if not clv_ok:
            perf_failures.append(
                f"avg CLV {m_avg_clv:+.4f} (need > 0)"
                if m_avg_clv is not None else "avg CLV unavailable"
            )
        if not pf_ok:
            perf_failures.append(
                f"profit factor {m_pf:.2f} (need > {_GATE_MIN_PROFIT_FACTOR:.2f})"
                if m_pf is not None else "profit factor unavailable"
            )
        if not cs_ok:
            perf_failures.append(
                f"clean-settled ROI {cs_roi * 100:+.1f}% (need > 0%)"
            )

        if perf_failures:
            verdict  = "WATCHLIST"
            reason   = (
                "Sample size gates passed but performance gates not met: "
                + "; ".join(perf_failures) + "."
            )
            next_req = (
                "Achieve positive modern ROI, positive avg CLV, "
                f"profit factor > {_GATE_MIN_PROFIT_FACTOR:.2f}, positive clean-settled ROI."
            )
        elif (modern_full_count >= _GATE_MIN_MODERN_SCALE
              and normal_modern_count >= _GATE_MIN_NORMAL_SCALE):
            verdict  = "SCALE_ELIGIBLE"
            reason   = (
                f"All gates met: {modern_full_count} modern full-metadata, "
                f"{normal_modern_count} council-approved, "
                f"modern ROI {m_roi * 100:+.1f}%, "
                f"avg CLV {m_avg_clv:+.4f}, profit factor {m_pf:.2f}. "
                "Explicit human approval required before any position size increase."
            )
            next_req = "Human review and explicit sign-off. System cannot self-authorize scaling."
        else:
            gap      = max(0, _GATE_MIN_MODERN_SCALE - modern_full_count)
            verdict  = "PAPER_VALIDATION_READY"
            reason   = (
                f"Performance gates pass but scale volume not met: "
                f"{modern_full_count}/{_GATE_MIN_MODERN_SCALE} modern full-metadata trades."
            )
            next_req = (
                f"Accumulate {gap} more modern full-metadata trades "
                "to reach SCALE_ELIGIBLE."
            )

    return {
        "verdict":                    verdict,
        "reason":                     reason,
        "warnings":                   warnings,
        "next_requirement":           next_req,
        "scale_allowed":              False,   # never programmatic; always human sign-off
        "real_money_allowed":         False,
        "modern_full_count":          modern_full_count,
        "normal_modern_count":        normal_modern_count,
        "dc_count":                   dc_count,
        "dc_pct":                     dc_pct,
        "clean_settled_count":        len(clean_settled),
        "clean_settled_roi_pct":      round(cs_roi * 100, 2),
        "clean_settled_avg_clv":      cs_avg_clv,
        "clean_settled_profit_factor": round(cs_pf, 4) if cs_pf is not None else None,
        "modern_roi_pct":             round(m_roi * 100, 2) if m_roi is not None else None,
        "modern_avg_clv":             m_avg_clv,
        "modern_profit_factor":       round(m_pf, 4) if m_pf is not None else None,
        "active_open_count":          active_open_count,
        "risk_override_count":        risk_override_count,
        "edge_profile_health":        edge_health,
    }


def print_proof_gate_verdict(gate: dict) -> None:
    """Print the structured proof gate verdict section."""
    v    = gate["verdict"]
    sep  = "=" * 92

    def _pct(val):
        return f"{val:+.2f}%" if val is not None else "n/a"

    def _clv(val):
        return _fmt_num(val) if val is not None else "n/a"

    def _pf(val):
        return _fmt_ratio(val) if val is not None else "n/a"

    print()
    print(sep)
    print("PROOF GATE VERDICT")
    print(sep)
    print(f"  Verdict:                          {v}")
    print(f"  Reason:                           {gate['reason']}")
    print()
    print(f"  Modern full-metadata count:       {gate['modern_full_count']}"
          f"  (watchlist >= {_GATE_MIN_MODERN_WATCHLIST}  |  scale >= {_GATE_MIN_MODERN_SCALE})")
    print(f"  Normal council-approved modern:   {gate['normal_modern_count']}"
          f"  (need >= {_GATE_MIN_NORMAL_WATCHLIST})")
    print(f"  Data-collection override count:   {gate['dc_count']}"
          f"  ({gate['dc_pct']:.1f}% of modern — NOT normal proof)")
    print(f"  Clean settled count:              {gate['clean_settled_count']}")
    print(f"  Clean settled ROI:                {_pct(gate['clean_settled_roi_pct'])}"
          "  (overall; need > 0% for scale)")
    print(f"  Clean settled avg CLV:            {_clv(gate['clean_settled_avg_clv'])}")
    print(f"  Clean settled profit factor:      {_pf(gate['clean_settled_profit_factor'])}")
    print(f"  Modern ROI:                       {_pct(gate['modern_roi_pct'])}")
    print(f"  Modern avg CLV:                   {_clv(gate['modern_avg_clv'])}")
    print(f"  Modern profit factor:             {_pf(gate['modern_profit_factor'])}"
          f"  (need > {_GATE_MIN_PROFIT_FACTOR:.2f} for scale)")
    print(f"  Active unresolved/open trades:    {gate['active_open_count']}")
    print(f"  Risk overrides used (evaluated):  {gate['risk_override_count']}")
    health = gate.get("edge_profile_health") or {}
    print(f"  Edge profile trusted:             {health.get('edge_profile_trusted')}")
    print(f"  Edge profile age hours:           {health.get('edge_profile_age_hours')}")
    print(f"  Edge profile reason:              {health.get('reason')}")
    print()
    print(f"  Scale allowed:                    {'YES' if gate['scale_allowed'] else 'NO'}")
    print(f"  Real money allowed:               NO  (paper trading only — hard rule)")
    print()
    if gate["warnings"]:
        print("  WARNINGS:")
        for w in gate["warnings"]:
            print(f"    [!] {w}")
        print()
    print(f"  Next requirement:                 {gate['next_requirement']}")
    print(sep)


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
    print(
        "[WARN] Bucket-level results with n < 5 are tiny samples. "
        "Positive-looking buckets are watchlist only, not proven."
    )

    evaluated = clean_settled + time_exits
    print_payout_asymmetry_report(evaluated)
    print_modern_vs_legacy_evidence(evaluated)
    print_modern_calibration_report(evaluated)
    print_true_ev_report(evaluated)
    print_edge_truth_audit(evaluated)
    print_edge_source_quality(evaluated)
    print_high_edge_loser_forensics(evaluated)
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
        EDGE_ORDER,
    )
    print_group_table(
        "ENTRY PRICE BUCKETS (clean settled + time exits)",
        evaluated,
        entry_price_bucket,
        ENTRY_PRICE_ORDER,
    )
    print_horizon_breakdown(evaluated)
    print_entry_price_inside_horizons(evaluated)
    print_group_table(
        "CLOSE_TIME HORIZON LENGTH (when timestamp fields allow)",
        evaluated,
        close_time_horizon_bucket,
        ["<=1h", "1-6h", "6-24h", "1-3d", "3-30d", ">30d",
         "invalid_negative", "unknown"],
    )
    print_zone_report("DANGER ZONES", evaluated, "danger")
    print_zone_report("PROMISING ZONES", evaluated, "promising")

    gate = evaluate_proof_gates(buckets, evaluated)
    print_proof_gate_verdict(gate)


if __name__ == "__main__":
    main()
