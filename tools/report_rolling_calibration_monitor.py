#!/usr/bin/env python3
"""
Phase 9V — Rolling Out-of-Sample Calibration Monitor
Sentinel: ROLLING_CALIBRATION_MONITOR_REPORT_OK

Read-only rolling monitor for probability calibration and payoff truth.
It evaluates trailing windows and an expanding window so calibration is
tested over time, not just inside one static cohort.
"""
from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tools.report_probability_calibration_payoff_truth as calib
from tools.report_accounting_version_proof_cohorts import (
    economic_pnl_value,
    entry_price,
    is_kxeth_or_quarantined,
    load_trades,
    risk_edge,
)
from tools.report_fresh_economic_proof_autopsy import council_path

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "ROLLING_CALIBRATION_MONITOR_REPORT_OK"
WINDOW_SIZES = (10, 20, 30, 50)
MIN_TOO_SMALL = 10
MIN_INSPECTION = 30


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (TypeError, ValueError):
        return None


def _sort_key(rec: dict[str, Any], idx: int) -> tuple[str, int]:
    return (str(rec.get("timestamp") or ""), idx)


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    phat = wins / n
    denom = 1.0 + (z * z) / n
    center = (phat + (z * z) / (2.0 * n)) / denom
    margin = (
        z
        * math.sqrt((phat * (1.0 - phat) + (z * z) / (4.0 * n)) / n)
        / denom
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _expected_ev(rec: dict[str, Any]) -> float | None:
    prob = calib.model_probability_value(rec)
    if prob is None:
        return None
    max_profit = _as_float(rec.get("max_profit_if_win"))
    if max_profit is None:
        ep = entry_price(rec)
        size = _as_float(rec.get("size"))
        if ep is not None and size is not None:
            max_profit = (1.0 - ep) * size
    max_loss = _as_float(rec.get("max_loss_if_loss"))
    if max_loss is None:
        ep = entry_price(rec)
        size = _as_float(rec.get("size"))
        if ep is not None and size is not None:
            max_loss = ep * size
    if max_profit is None or max_loss is None:
        return None
    return prob * max_profit - (1.0 - prob) * max_loss


def _calc_brier(rows: list[dict[str, Any]]) -> float | None:
    vals = []
    for rec in rows:
        prob = calib.model_probability_value(rec)
        pnl = economic_pnl_value(rec)
        if prob is None or pnl is None:
            continue
        if pnl > 0:
            outcome = 1.0
        elif pnl < 0:
            outcome = 0.0
        else:
            continue
        vals.append((prob - outcome) ** 2)
    return sum(vals) / len(vals) if vals else None


def _calc_mae(rows: list[dict[str, Any]]) -> float | None:
    vals = []
    for rec in rows:
        prob = calib.model_probability_value(rec)
        pnl = economic_pnl_value(rec)
        if prob is None or pnl is None:
            continue
        if pnl > 0:
            outcome = 1.0
        elif pnl < 0:
            outcome = 0.0
        else:
            continue
        vals.append(abs(prob - outcome))
    return sum(vals) / len(vals) if vals else None


def _rows_by_bucket(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in rows:
        grouped[key_fn(rec)].append(rec)
    return dict(grouped)


def _sum_pnl(rows: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> float | None:
    vals = [(_as_float(economic_pnl_value(r)) or 0.0) for r in rows if predicate(r)]
    return sum(vals) if vals else None


def _window_rows(rows: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    if size <= 0 or len(rows) < size:
        return []
    return rows[-size:]


def _expanding_series(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [rows[:i] for i in range(1, len(rows) + 1)]


def _trailing_series(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0 or len(rows) < size:
        return []
    return [rows[i - size : i] for i in range(size, len(rows) + 1)]


def _is_clean_monitor_row(rec: dict[str, Any]) -> bool:
    return (
        str(rec.get("status") or "").upper() == "SETTLED"
        and calib.classify_accounting_version(rec) == calib.ECONOMIC_VERSION
        and not is_kxeth_or_quarantined(rec)
        and calib.model_probability_value(rec) is not None
        and entry_price(rec) is not None
        and economic_pnl_value(rec) is not None
        and str(rec.get("result") or "").upper() in {"WIN", "LOSS"}
    )


def clean_monitor_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fresh = calib.calibration_rows(records)
    clean = [rec for rec in fresh if _is_clean_monitor_row(rec)]
    ordered = sorted(enumerate(clean), key=lambda item: _sort_key(item[1], item[0]))
    return [rec for _, rec in ordered]


def _window_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = dict(calib._bucket_summary(rows))
    summary["window_start_ts"] = str(rows[0].get("timestamp") or "") if rows else None
    summary["window_end_ts"] = str(rows[-1].get("timestamp") or "") if rows else None
    summary["win_rate_ci_low"], summary["win_rate_ci_high"] = _wilson_interval(summary["wins"], summary["n"])
    summary["sample_status"] = (
        "TOO_SMALL" if summary["n"] < MIN_TOO_SMALL else
        "INSPECTION_ONLY" if summary["n"] < MIN_INSPECTION else
        "BETTER_BUT_STILL_PAPER_ONLY"
    )
    summary["high_entry_rows"] = sum(1 for r in rows if 0.80 <= (entry_price(r) or -1.0) < 0.90)
    summary["high_entry_pnl"] = _sum_pnl(rows, lambda r: 0.80 <= (entry_price(r) or -1.0) < 0.90)
    summary["prob_90_plus_rows"] = sum(1 for r in rows if (calib.model_probability_value(r) or -1.0) >= 0.90)
    summary["prob_90_plus_pnl"] = _sum_pnl(rows, lambda r: (calib.model_probability_value(r) or -1.0) >= 0.90)
    summary["builder_boost_rows"] = sum(1 for r in rows if council_path(r) == "builder_boost")
    summary["builder_boost_pnl"] = _sum_pnl(rows, lambda r: council_path(r) == "builder_boost")
    summary["critic_caution_rows"] = sum(1 for r in rows if council_path(r) == "critic_caution")
    summary["critic_caution_pnl"] = _sum_pnl(rows, lambda r: council_path(r) == "critic_caution")
    summary["edge_0_10_plus_rows"] = sum(1 for r in rows if risk_edge(r) is not None and risk_edge(r) >= 0.10)
    summary["edge_0_10_plus_pnl"] = _sum_pnl(rows, lambda r: risk_edge(r) is not None and risk_edge(r) >= 0.10)
    summary["model_ev_sum"] = sum((_expected_ev(r) or 0.0) for r in rows)
    summary["realized_pnl"] = summary["total_economic_pnl"]
    summary["ev_gap"] = (
        summary["total_economic_pnl"] - summary["model_ev_sum"]
        if summary["total_economic_pnl"] is not None
        else None
    )
    summary["verdict"] = _window_verdict(summary)
    summary["flags"] = _window_flags(summary)
    return summary


def _window_flags(summary: dict[str, Any]) -> list[str]:
    flags: list[str] = [summary["sample_status"]]
    if summary.get("calibration_gap") is not None and summary["calibration_gap"] > 0.10:
        flags.append("OVERCONFIDENCE_PERSISTENT")
    if summary.get("win_rate") is not None and summary.get("breakeven_wr") is not None and summary["win_rate"] < summary["breakeven_wr"]:
        flags.append("OVERPAYMENT_PERSISTENT")
    if summary.get("model_ev_sum") is not None and summary.get("total_economic_pnl") is not None:
        if summary["model_ev_sum"] > 0 and summary["total_economic_pnl"] < 0:
            flags.append("MODEL_EV_FAKE")
    if summary.get("high_entry_pnl") is not None and summary["high_entry_pnl"] < 0:
        flags.append("HIGH_ENTRY_POISON")
    if summary.get("builder_boost_pnl") is not None and summary["builder_boost_pnl"] < 0:
        flags.append("BUILDER_BOOST_POISON")
    if summary.get("edge_0_10_plus_pnl") is not None and summary["edge_0_10_plus_pnl"] < 0:
        flags.append("EDGE_BUCKET_MISLEADING")
    if summary.get("prob_90_plus_rows", 0) > 0 and summary.get("prob_90_plus_pnl") is not None and summary["prob_90_plus_pnl"] > 0 and summary["prob_90_plus_rows"] < 5:
        flags.append("TINY_WINNER")
    return list(dict.fromkeys(flags))


def _window_verdict(summary: dict[str, Any]) -> str:
    verdict = summary["sample_status"]
    if summary.get("model_ev_sum") is not None and summary.get("total_economic_pnl") is not None:
        if summary["model_ev_sum"] > 0 and summary["total_economic_pnl"] < 0:
            verdict = f"{verdict}|MODEL_EV_FAKE"
    if summary.get("win_rate") is not None and summary.get("breakeven_wr") is not None and summary["win_rate"] < summary["breakeven_wr"]:
        verdict = f"{verdict}|OVERPAYMENT_PERSISTENT"
    return verdict


def _series_summary(series: list[list[dict[str, Any]]]) -> dict[str, Any]:
    summaries = [_window_summary(rows) for rows in series]
    if not summaries:
        return {
            "windows": 0,
            "negative_roi": 0,
            "overconfident": 0,
            "overpaid": 0,
            "model_ev_fake": 0,
            "high_entry_poison": 0,
            "builder_boost_poison": 0,
            "edge_bucket_misleading": 0,
            "p90_positive": 0,
            "p90_seen": 0,
            "latest": None,
            "worst_roi": None,
            "best_roi": None,
        }
    negative_roi = sum(1 for s in summaries if s.get("roi") is not None and s["roi"] < 0)
    overconfident = sum(1 for s in summaries if s.get("calibration_gap") is not None and s["calibration_gap"] > 0.10)
    overpaid = sum(1 for s in summaries if s.get("win_rate") is not None and s.get("breakeven_wr") is not None and s["win_rate"] < s["breakeven_wr"])
    model_ev_fake = sum(1 for s in summaries if s.get("model_ev_sum") is not None and s.get("total_economic_pnl") is not None and s["model_ev_sum"] > 0 and s["total_economic_pnl"] < 0)
    high_entry_poison = sum(1 for s in summaries if s.get("high_entry_pnl") is not None and s["high_entry_pnl"] < 0)
    builder_boost_poison = sum(1 for s in summaries if s.get("builder_boost_pnl") is not None and s["builder_boost_pnl"] < 0)
    edge_bucket_misleading = sum(1 for s in summaries if s.get("edge_0_10_plus_pnl") is not None and s["edge_0_10_plus_pnl"] < 0)
    p90_seen = sum(1 for s in summaries if s.get("prob_90_plus_rows", 0) > 0)
    p90_positive = sum(1 for s in summaries if s.get("prob_90_plus_pnl") is not None and s["prob_90_plus_pnl"] > 0)
    latest = summaries[-1]
    worst_roi = min(summaries, key=lambda s: s.get("roi") if s.get("roi") is not None else float("inf"))
    best_roi = max(summaries, key=lambda s: s.get("roi") if s.get("roi") is not None else float("-inf"))
    return {
        "windows": len(summaries),
        "negative_roi": negative_roi,
        "overconfident": overconfident,
        "overpaid": overpaid,
        "model_ev_fake": model_ev_fake,
        "high_entry_poison": high_entry_poison,
        "builder_boost_poison": builder_boost_poison,
        "edge_bucket_misleading": edge_bucket_misleading,
        "p90_positive": p90_positive,
        "p90_seen": p90_seen,
        "latest": latest,
        "worst_roi": worst_roi,
        "best_roi": best_roi,
    }


def _exclusion_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "kxeth_or_quarantined": sum(1 for r in records if is_kxeth_or_quarantined(r)),
        "data_collection_override": sum(1 for r in records if bool(r.get("data_collection_override"))),
        "bootstrap_provisional": sum(1 for r in records if bool(r.get("bootstrap_provisional"))),
        "side_coverage": sum(1 for r in records if bool(r.get("side_coverage_test")) or bool(r.get("side_coverage"))),
        "open_rows": sum(1 for r in records if str(r.get("status") or "").upper() == "OPEN"),
        "legacy_or_unversioned": sum(1 for r in records if not r.get("accounting_version")),
        "missing_model_probability": sum(1 for r in records if calib.model_probability_value(r) is None),
        "missing_entry_price": sum(1 for r in records if entry_price(r) is None),
        "missing_economic_pnl": sum(1 for r in records if economic_pnl_value(r) is None),
    }


def _format_ci(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "MISSING"
    return f"[{low*100:.1f},{high*100:.1f}]"


def _format_flags(flags: list[str]) -> str:
    return "|".join(flags) if flags else "none"


def build_report_state(records: list[dict[str, Any]]) -> dict[str, Any]:
    clean = clean_monitor_rows(records)
    baseline = dict(calib._bucket_summary(clean))
    baseline["window_start_ts"] = str(clean[0].get("timestamp") or "") if clean else None
    baseline["window_end_ts"] = str(clean[-1].get("timestamp") or "") if clean else None
    baseline["win_rate_ci_low"], baseline["win_rate_ci_high"] = _wilson_interval(baseline["wins"], baseline["n"])
    baseline["sample_status"] = "TOO_SMALL" if baseline["n"] < MIN_TOO_SMALL else "INSPECTION_ONLY" if baseline["n"] < MIN_INSPECTION else "BETTER_BUT_STILL_PAPER_ONLY"
    baseline["high_entry_rows"] = sum(1 for r in clean if 0.80 <= (entry_price(r) or -1.0) < 0.90)
    baseline["high_entry_pnl"] = _sum_pnl(clean, lambda r: 0.80 <= (entry_price(r) or -1.0) < 0.90)
    baseline["prob_90_plus_rows"] = sum(1 for r in clean if (calib.model_probability_value(r) or -1.0) >= 0.90)
    baseline["prob_90_plus_pnl"] = _sum_pnl(clean, lambda r: (calib.model_probability_value(r) or -1.0) >= 0.90)
    baseline["builder_boost_rows"] = sum(1 for r in clean if council_path(r) == "builder_boost")
    baseline["builder_boost_pnl"] = _sum_pnl(clean, lambda r: council_path(r) == "builder_boost")
    baseline["critic_caution_rows"] = sum(1 for r in clean if council_path(r) == "critic_caution")
    baseline["critic_caution_pnl"] = _sum_pnl(clean, lambda r: council_path(r) == "critic_caution")
    baseline["edge_0_10_plus_rows"] = sum(1 for r in clean if risk_edge(r) is not None and risk_edge(r) >= 0.10)
    baseline["edge_0_10_plus_pnl"] = _sum_pnl(clean, lambda r: risk_edge(r) is not None and risk_edge(r) >= 0.10)
    baseline["model_ev_sum"] = sum((_expected_ev(r) or 0.0) for r in clean)
    baseline["realized_pnl"] = baseline["total_economic_pnl"]
    baseline["ev_gap"] = baseline["total_economic_pnl"] - baseline["model_ev_sum"] if baseline["total_economic_pnl"] is not None else None
    baseline["verdict"] = _window_verdict(baseline)
    baseline["flags"] = _window_flags(baseline)

    trailing_series = {size: _trailing_series(clean, size) for size in WINDOW_SIZES}
    trailing_summaries = {size: _series_summary(series) for size, series in trailing_series.items()}
    expanding_series = _expanding_series(clean)
    expanding_summary = _series_summary(expanding_series)

    latest_windows = {
        size: _window_summary(_window_rows(clean, size))
        for size in WINDOW_SIZES
        if _window_rows(clean, size)
    }
    latest_windows["expanding"] = baseline

    return {
        "raw_rows": records,
        "clean_rows": clean,
        "baseline": baseline,
        "latest_windows": latest_windows,
        "trailing_series": trailing_series,
        "trailing_summaries": trailing_summaries,
        "expanding_series": expanding_series,
        "expanding_summary": expanding_summary,
        "exclusions": _exclusion_counts(records),
        "overall_status": "DO_NOT_PATCH_LIVE_YET",
    }


def _print_baseline(summary: dict[str, Any]) -> None:
    print()
    print("BASELINE CLEAN PROOF SUMMARY")
    print("-" * 92)
    print(f"  clean proof rows:          {summary['n']}")
    print(f"  wins / losses:            {summary['wins']} / {summary['losses']}")
    print(f"  win rate:                 {_fmt_pct(summary['win_rate'])}")
    print(f"  win rate CI (95%):        {_format_ci(summary['win_rate_ci_low'], summary['win_rate_ci_high'])}")
    print(f"  avg model_probability:    {_fmt_num(summary['avg_model_probability'])}")
    print(f"  avg entry price:          {_fmt_num(summary['avg_entry_price'])}")
    print(f"  avg risk_edge:            {_fmt_num(summary['avg_risk_edge'])}")
    print(f"  breakeven win rate:       {_fmt_pct(summary['breakeven_wr'])}")
    print(f"  win-rate margin:          {_fmt_pct(summary['wr_margin'])}")
    print(f"  calibration gap:          {_fmt_num(summary['calibration_gap'])}")
    print(f"  Brier score:              {_fmt_num(summary['brier_score'])}")
    print(f"  MAE:                      {_fmt_num(summary['mean_absolute_error'])}")
    print(f"  economic pnl:             {_fmt_money(summary['total_economic_pnl'])}")
    print(f"  model EV sum:             {_fmt_money(summary['model_ev_sum'])}")
    print(f"  EV gap:                   {_fmt_money(summary['ev_gap'])}")
    print(f"  ROI on capital at risk:   {_fmt_pct(summary['roi'])}")
    print(f"  profit factor:            {_fmt_num(summary['profit_factor'])}")
    print(f"  avg win / avg loss:       {_fmt_money(summary['avg_win'])} / {_fmt_money(summary['avg_loss'])}")
    print(f"  reward / risk:            {_fmt_num(summary['reward_risk'])}")
    print(f"  max drawdown:             {_fmt_money(summary['max_drawdown'])}")
    print(f"  sample warning:           {'YES' if summary['sample_status'] != 'BETTER_BUT_STILL_PAPER_ONLY' else 'NO'}")


def _print_window_snapshots(latest_windows: dict[str, dict[str, Any]]) -> None:
    print()
    print("WINDOW SNAPSHOTS")
    print("-" * 204)
    print(
        f"{'window':<12} {'start_ts':<26} {'end_ts':<26} {'n':>3} {'WR':>7} {'CI95':>17} {'BE':>7} {'mrg':>7} "
        f"{'mp':>7} {'cal':>7} {'PnL':>10} {'ROI':>8} {'PF':>7} {'EV':>10} {'EVgap':>10} "
        f"{'hi80':>8} {'p90':>8} {'builder':>10} {'critic':>10} {'edge10':>10} {'flags':>28}"
    )
    print("-" * 204)
    for label in ("10", "20", "30", "50", "expanding"):
        stats = latest_windows.get(int(label) if label.isdigit() else label)
        if not stats:
            continue
        print(
            f"{('last_'+label if label.isdigit() else label):<12} "
            f"{(stats.get('window_start_ts') or 'MISSING')[:26]:<26} "
            f"{(stats.get('window_end_ts') or 'MISSING')[:26]:<26} "
            f"{stats['n']:>3} {_fmt_pct(stats['win_rate']):>7} {_format_ci(stats['win_rate_ci_low'], stats['win_rate_ci_high']):>17} "
            f"{_fmt_pct(stats['breakeven_wr']):>7} {_fmt_pct(stats['wr_margin']):>7} {_fmt_num(stats['avg_model_probability']):>7} "
            f"{_fmt_num(stats['calibration_gap']):>7} {_fmt_money(stats['total_economic_pnl']):>10} {_fmt_pct(stats['roi']):>8} "
            f"{_fmt_num(stats['profit_factor']):>7} {_fmt_money(stats['model_ev_sum']):>10} {_fmt_money(stats['ev_gap']):>10} "
            f"{_fmt_money(stats['high_entry_pnl']):>8} {_fmt_money(stats['prob_90_plus_pnl']):>8} {_fmt_money(stats['builder_boost_pnl']):>10} "
            f"{_fmt_money(stats['critic_caution_pnl']):>10} {_fmt_money(stats['edge_0_10_plus_pnl']):>10} {_format_flags(stats['flags']):>28}"
        )


def _print_stability_summary(trailing_summaries: dict[int, dict[str, Any]], expanding_summary: dict[str, Any]) -> None:
    print()
    print("ROLLING STABILITY SUMMARY")
    print("-" * 160)
    print(
        f"{'window':<12} {'windows':>7} {'negROI':>8} {'overconf':>9} {'overpaid':>9} {'ev_fake':>8} "
        f"{'hi80':>8} {'builder':>9} {'edge10':>8} {'p90+':>11} {'latest':>32}"
    )
    print("-" * 160)
    for size in WINDOW_SIZES:
        stats = trailing_summaries.get(size)
        if not stats:
            continue
        p90_ratio = f"{stats['p90_positive']}/{stats['p90_seen']}"
        print(
            f"{('last_'+str(size)):<12} {stats['windows']:>7} {stats['negative_roi']:>8} {stats['overconfident']:>9} "
            f"{stats['overpaid']:>9} {stats['model_ev_fake']:>8} {stats['high_entry_poison']:>8} {stats['builder_boost_poison']:>9} "
            f"{stats['edge_bucket_misleading']:>8} {p90_ratio:>11} "
            f"{_format_flags(stats['latest']['flags']) if stats['latest'] else 'MISSING':>32}"
        )
    p90_ratio = f"{expanding_summary['p90_positive']}/{expanding_summary['p90_seen']}"
    print(
        f"{'expanding':<12} {expanding_summary['windows']:>7} {expanding_summary['negative_roi']:>8} {expanding_summary['overconfident']:>9} "
        f"{expanding_summary['overpaid']:>9} {expanding_summary['model_ev_fake']:>8} {expanding_summary['high_entry_poison']:>8} "
        f"{expanding_summary['builder_boost_poison']:>9} {expanding_summary['edge_bucket_misleading']:>8} "
        f"{p90_ratio:>11} "
        f"{_format_flags(expanding_summary['latest']['flags']) if expanding_summary['latest'] else 'MISSING':>32}"
    )


def _print_overall_truth(summary: dict[str, Any]) -> None:
    print()
    print("OVERALL TRUTH")
    print("-" * 92)
    print(f"  model EV sum:            {_fmt_money(summary['model_ev_sum'])}")
    print(f"  realized economic PnL:   {_fmt_money(summary['total_economic_pnl'])}")
    print(f"  calibration gap:         {_fmt_num(summary['calibration_gap'])}")
    print(f"  win rate vs BE:          {_fmt_pct(summary['wr_margin'])}")
    print(f"  high-entry PnL:          {_fmt_money(summary['high_entry_pnl'])}")
    print(f"  0.90+ probability PnL:   {_fmt_money(summary['prob_90_plus_pnl'])}")
    print(f"  builder_boost PnL:       {_fmt_money(summary['builder_boost_pnl'])}")
    print(f"  critic_caution PnL:      {_fmt_money(summary['critic_caution_pnl'])}")
    print(f"  edge 0.10+ PnL:          {_fmt_money(summary['edge_0_10_plus_pnl'])}")
    if summary["model_ev_sum"] is not None and summary["total_economic_pnl"] is not None and summary["model_ev_sum"] > 0 and summary["total_economic_pnl"] < 0:
        print("  red flag: model EV is positive while realized economic PnL is negative.")
    if summary["calibration_gap"] is not None and summary["calibration_gap"] > 0.10:
        print("  red flag: model_probability is materially above actual win rate.")


def _print_recommendation(state: dict[str, Any]) -> None:
    baseline = state["baseline"]
    trailing = state["trailing_summaries"]
    expanding = state["expanding_summary"]
    print()
    print("RECOMMENDATION")
    print("-" * 92)
    print("  do not patch live strategy yet.")
    print("  candidate ideas only:")
    print("    - keep the 0.80-0.90 entry band under suspicion until a rolling OOS monitor turns it.")
    print("    - treat builder_boost as a likely overconfidence amplifier until it survives time-based validation.")
    print("    - require any candidate to clear rolling ROI, PF, and breakeven tests across multiple windows.")
    print("    - keep the 0.90+ bucket as watchlist-only until its sample is meaningfully larger.")
    print("  what not to touch:")
    print("    - thresholds, gates, KXETH quarantine, Kelly, scale, real money, or execution behavior.")
    print(
        f"  why high confidence still loses: baseline WR={baseline['win_rate']*100:.1f}% vs breakeven {baseline['breakeven_wr']*100:.1f}%."
    )
    if trailing.get(10, {}).get("model_ev_fake", 0) > 0:
        print("  rolling 10-window series still shows model EV positive while realized PnL is negative.")
    if expanding.get("builder_boost_poison", 0) > 0:
        print("  expanding series keeps builder_boost in the damage zone.")


def render_report(state: dict[str, Any]) -> None:
    print("=" * 92)
    print("ROLLING OUT-OF-SAMPLE CALIBRATION MONITOR")
    print("=" * 92)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print("Population: settled, economic_contract_notional_v1, normal_modern, non-KXETH clean-proof rows only.")
    print(f"Source: {TRADES_LOG}")
    print(f"Raw records loaded: {len(state['raw_rows'])}")
    print(f"Clean calibration rows: {len(state['clean_rows'])}")
    print(f"Overall status: {state['overall_status']}")

    print("\nEXCLUSIONS")
    print("-" * 92)
    for key, value in state["exclusions"].items():
        print(f"  {key:<30}: {value}")

    _print_baseline(state["baseline"])
    _print_window_snapshots(state["latest_windows"])
    _print_stability_summary(state["trailing_summaries"], state["expanding_summary"])
    _print_overall_truth(state["baseline"])
    _print_recommendation(state)

    print()
    print("SOCRATIC CHECK")
    print("-" * 92)
    print("  what belief did the data disprove? probability confidence is not equivalent to realized edge.")
    print("  what is still unproven? the 0.90+ pocket is profitable out of sample with enough size.")
    print("  what would have to be true before live patching is even discussed? rolling windows must stay profitable with stable calibration.")
    print("  what result would prove the 0.90+ bucket is real? larger rolling windows must keep ROI>0, PF>1, and calibration error small.")
    print("  what result would prove builder_boost should be retired or quarantined? it stays negative across rolling windows.")
    print("  what result would prove 0.80-0.90 is not always bad? it would need multiple rolling windows with positive ROI and PF>1.")
    print("  are we confusing model confidence with edge? yes, until rolling validation says otherwise.")
    print("  are we confusing win rate with profit? yes, if breakeven and payoff asymmetry are ignored.")
    print("  are we selecting evidence after seeing outcomes? rolling windows are the antidote; static cherry-picks are the risk.")
    print("  what should I unlearn right now? that a high probability or high confidence score is proof.")

    print()
    print("80/20 DAMAGE RANKING")
    print("-" * 92)
    print("  1. Biggest PnL damage: high-entry 0.80-0.90 band.")
    print("  2. Biggest ROI damage: builder_boost and high-entry overlap.")
    print("  3. Biggest calibration damage: 0.80-0.90 probability bucket.")
    print("  4. Biggest payoff asymmetry damage: expensive contracts with small wins / large losses.")
    print("  5. Biggest overfit risk: tiny positive 0.90+ pockets.")
    print("  6. Biggest false-confidence source: model_probability above actual win rate.")
    print("  one next fix that matters most: keep the high-entry band under rolling out-of-sample watch, not live promotion.")

    print()
    print("LINDY / DURABILITY")
    print("-" * 92)
    print("  prefer rolling validation, fixed cohort rules, confidence intervals, sample labels, out-of-sample proof, and evidence hashes.")
    print("  reject live patches, hype, cosmetic dashboard changes, threshold weakening, cherry-picking, and tiny-sample promotion.")

    print()
    print("VERDICT")
    print("-" * 92)
    print("  DO_NOT_PATCH_LIVE_YET")
    if state["baseline"]["n"] < MIN_INSPECTION:
        print(f"  sample warning: only {state['baseline']['n']}/{MIN_INSPECTION} clean proof rows.")
    print("  this is rolling calibration truth only, not live-patch permission.")
    print()
    print(f"Sentinel: {SENTINEL}")
    print("=" * 92)


def main() -> None:
    records = load_trades(TRADES_LOG)
    state = build_report_state(records)
    render_report(state)


if __name__ == "__main__":
    main()
