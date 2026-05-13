#!/usr/bin/env python3
"""
Phase 9U — Probability Calibration + Payoff EV Truth
Sentinel: PROBABILITY_CALIBRATION_PAYOFF_TRUTH_REPORT_OK

Read-only report that checks whether model probability is calibrated,
whether reported edge matches realized payoff truth, and which zones are
overconfident, overpaid, or poison.
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.report_accounting_version_proof_cohorts import (
    ECONOMIC_VERSION,
    classify_accounting_version,
    clean_settled_rows,
    economic_pnl_value,
    entry_price,
    is_clean_proof_row,
    is_kxeth_or_quarantined,
    load_trades,
    payout_notional_value,
    capital_at_risk_value,
    recorded_pnl_value,
    risk_edge,
)
from tools.report_fresh_economic_proof_autopsy import (
    council_path,
    edge_bucket,
    fresh_proof_rows,
    price_bucket,
    summarize_rows,
)

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "PROBABILITY_CALIBRATION_PAYOFF_TRUTH_REPORT_OK"
MIN_CELL_SAMPLE = 5
MIN_PROOF_SAMPLE = 30

PROBABILITY_BUCKETS = ("<0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90+")
ENTRY_BUCKETS = ("0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00")
EDGE_BUCKETS = ("<0.03", "0.03-0.05", "0.05-0.10", "0.10+")
COUNCIL_PATHS = ("builder_boost", "critic_caution", "bootstrap_era_allow", "other")


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


def model_probability_value(rec: dict[str, Any]) -> float | None:
    return _as_float(rec.get("model_probability"))


def realized_outcome(rec: dict[str, Any]) -> float | None:
    pnl = economic_pnl_value(rec)
    if pnl is None:
        return None
    if pnl > 0:
        return 1.0
    if pnl < 0:
        return 0.0
    return None


def clv_value(rec: dict[str, Any]) -> float | None:
    value = _as_float(rec.get("clv"))
    if value is not None:
        return value
    ep = entry_price(rec)
    exit_price = _as_float(rec.get("exit_price"))
    if ep is not None and exit_price is not None:
        return exit_price - ep
    return None


def model_probability_bucket(prob: float | None) -> str:
    if prob is None:
        return "missing"
    if prob < 0.60:
        return "<0.60"
    if prob < 0.70:
        return "0.60-0.70"
    if prob < 0.80:
        return "0.70-0.80"
    if prob < 0.90:
        return "0.80-0.90"
    return "0.90+"


def entry_price_bucket_value(price: float | None) -> str:
    if price is None:
        return "missing"
    if price < 0.50:
        return "<0.50"
    if price < 0.60:
        return "0.50-0.60"
    if price < 0.70:
        return "0.60-0.70"
    if price < 0.80:
        return "0.70-0.80"
    if price < 0.90:
        return "0.80-0.90"
    return "0.90-1.00"


def edge_bucket_value(edge: float | None) -> str:
    if edge is None:
        return "missing"
    if edge < 0.03:
        return "<0.03"
    if edge < 0.05:
        return "0.03-0.05"
    if edge < 0.10:
        return "0.05-0.10"
    return "0.10+"


def model_price_cell_key(rec: dict[str, Any]) -> str:
    return f"{model_probability_bucket(model_probability_value(rec))}|{entry_price_bucket_value(entry_price(rec))}"


def _is_calibration_row(rec: dict[str, Any]) -> bool:
    return (
        is_clean_proof_row(rec)
        and classify_accounting_version(rec) == ECONOMIC_VERSION
        and not is_kxeth_or_quarantined(rec)
        and model_probability_value(rec) is not None
        and entry_price(rec) is not None
        and economic_pnl_value(rec) is not None
        and str(rec.get("result") or "").upper() in {"WIN", "LOSS"}
    )


def calibration_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rec for rec in fresh_proof_rows(records) if _is_calibration_row(rec)]


def clean_proof_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rec for rec in fresh_proof_rows(records) if _is_calibration_row(rec)]


def _sum(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _profit_factor(rows: list[dict[str, Any]]) -> float | None:
    gross_wins = 0.0
    gross_losses = 0.0
    for rec in rows:
        pnl = economic_pnl_value(rec)
        if pnl is None:
            continue
        if pnl > 0:
            gross_wins += pnl
        elif pnl < 0:
            gross_losses += pnl
    if gross_wins <= 0 or gross_losses >= 0:
        return None
    return gross_wins / abs(gross_losses)


def _max_drawdown(rows: list[dict[str, Any]]) -> float | None:
    ordered = sorted(rows, key=lambda rec: str(rec.get("timestamp") or ""))
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    seen = False
    for rec in ordered:
        pnl = economic_pnl_value(rec)
        if pnl is None:
            continue
        seen = True
        cumulative += pnl
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst if seen else None


def _expected_ev(rec: dict[str, Any]) -> float | None:
    prob = model_probability_value(rec)
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


def _bucket_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [r for r in rows if str(r.get("result") or "").upper() == "WIN"]
    losses = [r for r in rows if str(r.get("result") or "").upper() == "LOSS"]
    win_loss_n = len(wins) + len(losses)
    probs = [v for v in (model_probability_value(r) for r in rows) if v is not None]
    eps = [v for v in (entry_price(r) for r in rows) if v is not None]
    edges = [v for v in (risk_edge(r) for r in rows) if v is not None]
    clvs = [v for v in (clv_value(r) for r in rows) if v is not None]
    capitals = [v for v in (capital_at_risk_value(r) for r in rows) if v is not None]
    payouts = [v for v in (payout_notional_value(r) for r in rows) if v is not None]
    max_profits = [_as_float(r.get("max_profit_if_win")) for r in rows]
    max_losses = [_as_float(r.get("max_loss_if_loss")) for r in rows]
    evs = [_expected_ev(r) for r in rows]
    outcomes = [realized_outcome(r) for r in rows]
    clean_outcomes = [o for o in outcomes if o is not None]
    probs_and_outcomes = [(model_probability_value(r), realized_outcome(r)) for r in rows]

    total_economic_pnl = _sum(economic_pnl_value(r) for r in rows)
    total_recorded_pnl = _sum(recorded_pnl_value(r) for r in rows)
    total_capital = _sum(capital_at_risk_value(r) for r in rows)
    total_payout = _sum(payout_notional_value(r) for r in rows)
    total_expected_ev = _sum(evs)
    avg_expected_ev = total_expected_ev / len(rows) if rows and total_expected_ev is not None else None
    avg_ev_gap = None
    if total_expected_ev is not None and total_economic_pnl is not None:
        avg_ev_gap = (total_economic_pnl - total_expected_ev) / len(rows)
    avg_model_prob = _avg(probs)
    avg_entry = _avg(eps)
    avg_edge = _avg(edges)
    avg_clv = _avg(clvs)
    avg_capital = _avg(capitals)
    avg_payout = _avg(payouts)
    avg_win = _avg(economic_pnl_value(r) for r in wins)
    avg_loss = _avg(economic_pnl_value(r) for r in losses)
    profit_factor = _profit_factor(rows)
    reward_risk = None
    total_max_profit = _sum(max_profits)
    total_max_loss = _sum(max_losses)
    if total_max_profit is not None and total_max_loss is not None and total_max_loss > 0:
        reward_risk = total_max_profit / total_max_loss
    win_rate = len(wins) / win_loss_n if win_loss_n else None
    breakeven_wr = avg_entry
    wr_margin = (win_rate - breakeven_wr) if win_rate is not None and breakeven_wr is not None else None
    roi = total_economic_pnl / total_capital if total_economic_pnl is not None and total_capital and total_capital > 0 else None
    brier = None
    mae = None
    avg_overconfidence = None
    avg_underconfidence = None
    calibration_gap = None
    calibration_slope = None
    calibration_intercept = None
    diffs = []
    sq_errors = []
    abs_errors = []
    over = []
    under = []
    probs_only = []
    outcomes_only = []
    for prob, outcome in probs_and_outcomes:
        if prob is None or outcome is None:
            continue
        error = outcome - prob
        diffs.append(prob - outcome)
        sq_errors.append(error * error)
        abs_errors.append(abs(error))
        over.append(max(prob - outcome, 0.0))
        under.append(max(outcome - prob, 0.0))
        probs_only.append(prob)
        outcomes_only.append(outcome)
    if sq_errors:
        brier = sum(sq_errors) / len(sq_errors)
    if abs_errors:
        mae = sum(abs_errors) / len(abs_errors)
    if diffs:
        calibration_gap = sum(diffs) / len(diffs)
        avg_overconfidence = sum(over) / len(over) if over else None
        avg_underconfidence = sum(under) / len(under) if under else None
    if len(probs_only) >= 2:
        mean_p = sum(probs_only) / len(probs_only)
        mean_y = sum(outcomes_only) / len(outcomes_only)
        var_p = sum((p - mean_p) ** 2 for p in probs_only)
        if var_p > 0:
            cov_py = sum((p - mean_p) * (y - mean_y) for p, y in zip(probs_only, outcomes_only))
            calibration_slope = cov_py / var_p
            calibration_intercept = mean_y - calibration_slope * mean_p

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "breakeven_wr": breakeven_wr,
        "wr_margin": wr_margin,
        "total_economic_pnl": total_economic_pnl,
        "total_recorded_pnl": total_recorded_pnl,
        "total_capital_at_risk": total_capital,
        "total_payout_notional": total_payout,
        "total_expected_ev": total_expected_ev,
        "avg_expected_ev": avg_expected_ev,
        "ev_gap_per_trade": avg_ev_gap,
        "roi": roi,
        "avg_model_probability": avg_model_prob,
        "avg_entry_price": avg_entry,
        "avg_risk_edge": avg_edge,
        "avg_clv": avg_clv,
        "avg_capital_at_risk": avg_capital,
        "avg_payout_notional": avg_payout,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "reward_risk": reward_risk,
        "max_drawdown": _max_drawdown(rows),
        "brier_score": brier,
        "mean_absolute_error": mae,
        "calibration_gap": calibration_gap,
        "avg_overconfidence": avg_overconfidence,
        "avg_underconfidence": avg_underconfidence,
        "calibration_slope": calibration_slope,
        "calibration_intercept": calibration_intercept,
        "sample_ge_5": len(rows) >= MIN_CELL_SAMPLE,
        "sample_ge_30": len(rows) >= MIN_PROOF_SAMPLE,
    }


def _bucket_rows(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in rows:
        groups[key_fn(rec)].append(rec)
    return dict(groups)


def summarize_buckets(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str], order: Iterable[str]) -> dict[str, dict[str, Any]]:
    groups = _bucket_rows(rows, key_fn)
    return {key: _bucket_summary(groups.get(key, [])) for key in order if groups.get(key, [])}


def summarize_cells(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    order = [
        f"{pb}|{eb}"
        for pb in PROBABILITY_BUCKETS
        for eb in ENTRY_BUCKETS
    ]
    return summarize_buckets(rows, model_price_cell_key, order)


def _overall_status(baseline: dict[str, Any], buckets: dict[str, dict[str, Any]], cells: dict[str, dict[str, Any]]) -> str:
    if baseline["n"] < MIN_PROOF_SAMPLE:
        return "CALIBRATED_BUT_UNPROVEN"
    strong_cell = any(
        stats["n"] >= 30
        and stats.get("win_rate") is not None
        and stats.get("breakeven_wr") is not None
        and stats.get("roi") is not None
        and stats.get("profit_factor") is not None
        and stats["win_rate"] > stats["breakeven_wr"]
        and stats["roi"] > 0
        and stats["profit_factor"] > 1.0
        for stats in cells.values()
    )
    if strong_cell:
        return "REAL_CANDIDATE"
    return "DO_NOT_PATCH_LIVE_YET"


def _bucket_status(summary: dict[str, Any]) -> str:
    if summary["n"] < MIN_CELL_SAMPLE:
        return "TOO_SMALL"
    if summary.get("roi") is not None and summary["roi"] < 0 and (summary.get("profit_factor") or 0) < 1.0:
        return "POISON"
    if summary.get("calibration_gap") is not None and summary["calibration_gap"] <= -0.10:
        return "OVERCONFIDENT"
    if summary.get("win_rate") is not None and summary.get("breakeven_wr") is not None and summary["win_rate"] < summary["breakeven_wr"]:
        return "OVERPAID"
    if summary.get("calibration_gap") is not None and summary["calibration_gap"] > 0.10:
        return "UNDERPRICED"
    return "GOOD"


def _cell_status(summary: dict[str, Any]) -> str:
    if summary["n"] < MIN_CELL_SAMPLE:
        return "TOO_SMALL"
    if summary.get("win_rate") is not None and summary.get("breakeven_wr") is not None and summary["win_rate"] < summary["breakeven_wr"]:
        if summary.get("roi") is not None and summary["roi"] < 0:
            return "POISON"
        return "OVERPAID"
    if summary.get("roi") is not None and summary["roi"] < 0 and (summary.get("profit_factor") or 0) < 1.0:
        return "POISON"
    if summary.get("avg_model_probability") is not None and summary.get("avg_entry_price") is not None:
        model_edge = summary["avg_model_probability"] - summary["avg_entry_price"]
        actual_edge = summary["win_rate"] - summary["avg_entry_price"] if summary["win_rate"] is not None else None
        if model_edge > 0 and actual_edge is not None and actual_edge < 0:
            return "MODEL_EDGE_FAKE"
    return "GOOD"


def _row_identity(rec: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(rec.get("timestamp") or ""),
        str(rec.get("ticker") or ""),
        str(rec.get("action") or ""),
        _as_float(rec.get("size")),
        _as_float(rec.get("entry_price")),
    )


def build_report_state(records: list[dict[str, Any]]) -> dict[str, Any]:
    fresh_rows = fresh_proof_rows(records)
    calibration = calibration_rows(records)
    baseline = _bucket_summary(calibration)
    baseline["rows"] = calibration
    baseline["total_rows"] = len(fresh_rows)
    baseline["clean_proof_rows"] = len(calibration)
    baseline["fresh_rows"] = len(fresh_rows)
    baseline["calibration_rows"] = len(calibration)
    baseline["sample_warning"] = len(calibration) < MIN_PROOF_SAMPLE

    probability_buckets = summarize_buckets(
        calibration,
        lambda r: model_probability_bucket(model_probability_value(r)),
        list(PROBABILITY_BUCKETS),
    )
    entry_buckets = summarize_buckets(
        calibration,
        lambda r: entry_price_bucket_value(entry_price(r)),
        list(ENTRY_BUCKETS),
    )
    edge_buckets = summarize_buckets(
        calibration,
        lambda r: edge_bucket_value(risk_edge(r)),
        list(EDGE_BUCKETS),
    )
    cells = summarize_cells(calibration)
    council = summarize_buckets(calibration, council_path, list(COUNCIL_PATHS))

    counts = {
        "raw_records": len(records),
        "fresh_rows": len(fresh_rows),
        "calibration_rows": len(calibration),
        "excluded_total": len(records) - len(fresh_rows),
        "excluded_kxeth_or_quarantined": sum(1 for r in records if is_kxeth_or_quarantined(r)),
        "excluded_data_collection_override": sum(1 for r in records if bool(r.get("data_collection_override"))),
        "excluded_bootstrap_provisional": sum(1 for r in records if bool(r.get("bootstrap_provisional"))),
        "excluded_side_coverage": sum(1 for r in records if bool(r.get("side_coverage_test")) or bool(r.get("side_coverage"))),
        "excluded_open_rows": sum(1 for r in records if str(r.get("status") or "").upper() == "OPEN"),
        "excluded_legacy_or_unversioned": sum(1 for r in records if not r.get("accounting_version")),
        "excluded_missing_entry_price": sum(1 for r in records if entry_price(r) is None),
        "excluded_missing_model_probability": sum(1 for r in records if model_probability_value(r) is None),
        "excluded_missing_economic_pnl": sum(1 for r in records if economic_pnl_value(r) is None),
        "excluded_missing_clv": sum(1 for r in records if clv_value(r) is None),
    }

    overall_status = _overall_status(baseline, probability_buckets, cells)
    if calibration and baseline["n"] < MIN_PROOF_SAMPLE:
        overall_status = "CALIBRATED_BUT_UNPROVEN"

    return {
        "records": records,
        "fresh_rows": fresh_rows,
        "calibration_rows": calibration,
        "baseline": baseline,
        "probability_buckets": probability_buckets,
        "entry_buckets": entry_buckets,
        "edge_buckets": edge_buckets,
        "cells": cells,
        "council": council,
        "counts": counts,
        "overall_status": overall_status,
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print()
    print("BASELINE CLEAN PROOF SUMMARY")
    print("-" * 84)
    print(f"  clean proof rows:          {summary['n']}")
    print(f"  wins / losses:            {summary['wins']} / {summary['losses']}")
    print(f"  win rate:                 {_fmt_pct(summary['win_rate'])}")
    print(f"  avg model_probability:    {_fmt_num(summary['avg_model_probability'])}")
    print(f"  avg entry price:          {_fmt_num(summary['avg_entry_price'])}")
    print(f"  avg risk_edge:            {_fmt_num(summary['avg_risk_edge'])}")
    print(f"  breakeven win rate:       {_fmt_pct(summary['breakeven_wr'])}")
    print(f"  win-rate margin:          {_fmt_pct(summary['wr_margin'])}")
    print(f"  economic pnl:             {_fmt_money(summary['total_economic_pnl'])}")
    print(f"  recorded pnl:             {_fmt_money(summary['total_recorded_pnl'])}")
    print(f"  expected EV sum:          {_fmt_money(summary['total_expected_ev'])}")
    print(f"  EV gap per trade:         {_fmt_money(summary['ev_gap_per_trade'])}")
    print(f"  ROI on capital at risk:   {_fmt_pct(summary['roi'])}")
    print(f"  profit factor:            {_fmt_num(summary['profit_factor'])}")
    print(f"  avg win / avg loss:       {_fmt_money(summary['avg_win'])} / {_fmt_money(summary['avg_loss'])}")
    print(f"  reward / risk:            {_fmt_num(summary['reward_risk'])}")
    print(f"  max drawdown:             {_fmt_money(summary['max_drawdown'])}")
    print(f"  brier score:              {_fmt_num(summary['brier_score'])}")
    print(f"  MAE:                      {_fmt_num(summary['mean_absolute_error'])}")
    print(f"  calibration gap:          {_fmt_num(summary['calibration_gap'])}")
    print(f"  calibration slope:        {_fmt_num(summary['calibration_slope'])}")
    print(f"  calibration intercept:    {_fmt_num(summary['calibration_intercept'])}")
    print(f"  avg overconfidence:       {_fmt_num(summary['avg_overconfidence'])}")
    print(f"  avg underconfidence:      {_fmt_num(summary['avg_underconfidence'])}")
    print(f"  avg CLV:                  {_fmt_num(summary['avg_clv'])}")
    print(f"  sample warning:           {'YES' if summary['sample_ge_30'] is False else 'NO'}")


def _print_bucket_table(title: str, buckets: dict[str, dict[str, Any]], order: Iterable[str], kind: str) -> None:
    print()
    print(title)
    print("-" * 146)
    print(
        f"{'bucket':<16} {'n':>4} {'WR':>7} {'BE':>7} {'mrg':>7} {'mp':>7} {'cal_err':>8} {'PnL':>10} {'ROI':>8} "
        f"{'PF':>7} {'RR':>7} {'avg_ep':>8} {'avg_clv':>8} {'tag':>14}"
    )
    print("-" * 146)
    for key in order:
        stats = buckets.get(key)
        if not stats:
            continue
        if kind == "probability":
            tag = _bucket_status(stats)
        else:
            tag = "POS" if (stats.get("total_economic_pnl") or 0) > 0 else "POISON" if (stats.get("total_economic_pnl") or 0) < 0 else "FLAT"
        print(
            f"{key:<16} {stats['n']:>4} {_fmt_pct(stats['win_rate']):>7} {_fmt_pct(stats['breakeven_wr']):>7} "
            f"{_fmt_pct(stats['wr_margin']):>7} {_fmt_num(stats['avg_model_probability']):>7} "
            f"{_fmt_num(stats['calibration_gap']):>8} {_fmt_money(stats['total_economic_pnl']):>10} "
            f"{_fmt_pct(stats['roi']):>8} {_fmt_num(stats['profit_factor']):>7} {_fmt_num(stats['reward_risk']):>7} "
            f"{_fmt_num(stats['avg_entry_price'], 4):>8} {_fmt_num(stats['avg_clv'], 4):>8} {tag:>14}"
        )


def _print_cells(cells: dict[str, dict[str, Any]]) -> None:
    print()
    print("MODEL PROBABILITY × ENTRY PRICE CELLS")
    print("-" * 156)
    print(
        f"{'cell':<20} {'n':>4} {'WR':>7} {'BE':>7} {'mrg':>7} {'mp':>7} {'ep':>7} {'m-edge':>8} {'a-edge':>8} "
        f"{'PnL':>10} {'ROI':>8} {'PF':>7} {'RR':>7} {'tag':>16}"
    )
    print("-" * 156)
    for key in sorted(cells):
        stats = cells[key]
        if stats["n"] == 0:
            continue
        model_edge = None
        actual_edge = None
        if stats.get("avg_model_probability") is not None and stats.get("avg_entry_price") is not None:
            model_edge = stats["avg_model_probability"] - stats["avg_entry_price"]
        if stats.get("win_rate") is not None and stats.get("avg_entry_price") is not None:
            actual_edge = stats["win_rate"] - stats["avg_entry_price"]
        tag = _cell_status(stats)
        print(
            f"{key:<20} {stats['n']:>4} {_fmt_pct(stats['win_rate']):>7} {_fmt_pct(stats['breakeven_wr']):>7} "
            f"{_fmt_pct(stats['wr_margin']):>7} {_fmt_num(stats['avg_model_probability']):>7} {_fmt_num(stats['avg_entry_price']):>7} "
            f"{_fmt_num(model_edge):>8} {_fmt_num(actual_edge):>8} {_fmt_money(stats['total_economic_pnl']):>10} "
            f"{_fmt_pct(stats['roi']):>8} {_fmt_num(stats['profit_factor']):>7} {_fmt_num(stats['reward_risk']):>7} {tag:>16}"
        )


def _print_overconfidence_autopsy(buckets: dict[str, dict[str, Any]], cells: dict[str, dict[str, Any]]) -> None:
    print()
    print("OVERCONFIDENCE / OVERPAYMENT AUTOPSY")
    print("-" * 84)
    overconfident = sorted(
        ((k, v) for k, v in buckets.items() if (v.get("calibration_gap") or 0.0) > 0 and v["n"] >= MIN_CELL_SAMPLE),
        key=lambda item: (
            item[1].get("calibration_gap") if item[1].get("calibration_gap") is not None else float("-inf"),
            item[1].get("total_economic_pnl") if item[1].get("total_economic_pnl") is not None else 0.0,
        ),
        reverse=True,
    )
    underconfident = sorted(
        ((k, v) for k, v in buckets.items() if (v.get("calibration_gap") or 0.0) < 0 and v["n"] >= MIN_CELL_SAMPLE),
        key=lambda item: (
            item[1].get("calibration_gap") if item[1].get("calibration_gap") is not None else float("inf"),
            item[1].get("total_economic_pnl") if item[1].get("total_economic_pnl") is not None else 0.0,
        ),
    )
    if overconfident:
        key, stats = overconfident[0]
        print(
            f"  most overconfident bucket: {key}  cal_err={_fmt_num(stats.get('calibration_gap'))}  "
            f"WR={_fmt_pct(stats.get('win_rate'))}  mp={_fmt_num(stats.get('avg_model_probability'))}  "
            f"PnL={_fmt_money(stats.get('total_economic_pnl'))}  ROI={_fmt_pct(stats.get('roi'))}"
        )
    if underconfident:
        key, stats = underconfident[0]
        print(
            f"  most underconfident bucket: {key}  cal_err={_fmt_num(stats.get('calibration_gap'))}  "
            f"WR={_fmt_pct(stats.get('win_rate'))}  mp={_fmt_num(stats.get('avg_model_probability'))}  "
            f"PnL={_fmt_money(stats.get('total_economic_pnl'))}  ROI={_fmt_pct(stats.get('roi'))}"
        )
    poison_cells = sorted(
        ((k, v) for k, v in cells.items() if v["n"] >= MIN_CELL_SAMPLE and (v.get("roi") or 0) < 0),
        key=lambda item: item[1].get("total_economic_pnl") or 0.0,
    )
    if poison_cells:
        key, stats = poison_cells[0]
        print(
            f"  worst 2D cell:            {key}  WR={_fmt_pct(stats.get('win_rate'))}  BE={_fmt_pct(stats.get('breakeven_wr'))}  "
            f"PnL={_fmt_money(stats.get('total_economic_pnl'))}  ROI={_fmt_pct(stats.get('roi'))}"
        )
    print("  note: positive model EV on paper does not help if realized economic PnL is negative.")
    print("  note: high entry price raises breakeven; high win rate can still lose when payouts are too small.")


def _print_payoff_truth(summary: dict[str, Any]) -> None:
    print()
    print("PAYOFF EV TRUTH")
    print("-" * 84)
    print(f"  model EV sum:            {_fmt_money(summary['total_expected_ev'])}")
    print(f"  realized economic PnL:   {_fmt_money(summary['total_economic_pnl'])}")
    print(f"  EV gap per trade:        {_fmt_money(summary['ev_gap_per_trade'])}")
    if summary["total_expected_ev"] is not None and summary["total_expected_ev"] > 0 and summary["total_economic_pnl"] is not None and summary["total_economic_pnl"] < 0:
        print("  red flag: model EV is positive while realized economic PnL is negative.")
    if summary["win_rate"] is not None and summary["breakeven_wr"] is not None and summary["win_rate"] < summary["breakeven_wr"]:
        print("  red flag: win rate is below breakeven even if headline accuracy looks strong.")


def _print_recommendation(summary: dict[str, Any], prob_buckets: dict[str, dict[str, Any]], cells: dict[str, dict[str, Any]], council: dict[str, dict[str, Any]]) -> None:
    print()
    print("RECOMMENDATION")
    print("-" * 84)
    print("  do not patch live strategy yet.")
    print("  candidate ideas only:")
    print("    - block the 0.80-0.90 entry band unless a future validation run proves it out.")
    print("    - treat builder_boost as a risk flag until it shows positive validation EV.")
    print("    - require a cell to clear sample size, ROI, PF, and breakeven margin before discussion.")
    print("    - treat high probability buckets as suspect until calibration error is acceptably small.")
    print("  what not to touch:")
    print("    - thresholds, gates, KXETH quarantine, Kelly, scale, real money, or execution behavior.")
    if summary["win_rate"] is not None and summary["breakeven_wr"] is not None:
        print(
            f"  why high win rate still loses: WR={summary['win_rate']*100:.1f}% but breakeven is {summary['breakeven_wr']*100:.1f}%."
        )
    overconfident = max(
        ((k, v) for k, v in prob_buckets.items() if (v.get("calibration_gap") or 0.0) > 0 and v["n"] >= MIN_CELL_SAMPLE),
        key=lambda item: item[1].get("calibration_gap") or 0.0,
        default=None,
    )
    if overconfident is not None:
        key, stats = overconfident
        print(f"  most overconfident probability pocket: {key} with calibration gap {_fmt_num(stats.get('calibration_gap'))}.")
    if council.get("builder_boost", {}).get("n"):
        print("  builder_boost remains a likely overconfidence amplifier until it validates out of sample.")


def render_report(state: dict[str, Any]) -> None:
    print("=" * 84)
    print("PROBABILITY CALIBRATION + PAYOFF EV TRUTH")
    print("=" * 84)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print("Population: settled, economic_contract_notional_v1, normal_modern, non-KXETH clean-proof rows only.")
    print(f"Source: {TRADES_LOG}")
    print(f"Raw records loaded: {state['counts']['raw_records']}")
    print(f"Fresh proof rows:    {state['counts']['fresh_rows']}")
    print(f"Calibration rows:    {state['counts']['calibration_rows']}")
    print(f"Overall status:      {state['overall_status']}")

    print("\nEXCLUSIONS")
    print("-" * 84)
    for key, value in state["counts"].items():
        if key.startswith("excluded_"):
            print(f"  {key:<32}: {value}")

    _print_summary(state["baseline"])
    _print_bucket_table(
        "MODEL PROBABILITY BUCKETS",
        state["probability_buckets"],
        PROBABILITY_BUCKETS,
        "probability",
    )
    _print_bucket_table(
        "ENTRY PRICE BUCKETS",
        state["entry_buckets"],
        ENTRY_BUCKETS,
        "entry",
    )
    _print_bucket_table(
        "REPORTED EDGE BUCKETS",
        state["edge_buckets"],
        EDGE_BUCKETS,
        "edge",
    )
    _print_cells(state["cells"])

    print()
    print("COUNCIL PATH CALIBRATION")
    print("-" * 136)
    print(
        f"{'path':<18} {'n':>4} {'WR':>7} {'BE':>7} {'mrg':>7} {'mp':>7} {'cal_err':>8} {'PnL':>10} {'ROI':>8} "
        f"{'PF':>7} {'RR':>7} {'avg_ep':>8} {'avg_loss':>9} {'tag':>14}"
    )
    print("-" * 136)
    for key in COUNCIL_PATHS:
        stats = state["council"].get(key)
        if not stats:
            continue
        tag = "HELPING" if (stats.get("roi") or 0) > 0 and (stats.get("profit_factor") or 0) > 1 else "HURTING"
        print(
            f"{key:<18} {stats['n']:>4} {_fmt_pct(stats['win_rate']):>7} {_fmt_pct(stats['breakeven_wr']):>7} "
            f"{_fmt_pct(stats['wr_margin']):>7} {_fmt_num(stats['avg_model_probability']):>7} {_fmt_num(stats['calibration_gap']):>8} "
            f"{_fmt_money(stats['total_economic_pnl']):>10} {_fmt_pct(stats['roi']):>8} {_fmt_num(stats['profit_factor']):>7} "
            f"{_fmt_num(stats['reward_risk']):>7} {_fmt_num(stats['avg_entry_price'], 4):>8} {_fmt_money(stats['avg_loss']):>9} {tag:>14}"
        )

    _print_overconfidence_autopsy(state["probability_buckets"], state["cells"])
    _print_payoff_truth(state["baseline"])
    _print_recommendation(state["baseline"], state["probability_buckets"], state["cells"], state["council"])

    print()
    print("VERDICT")
    print("-" * 84)
    print("  DO_NOT_PATCH_LIVE_YET")
    if state["baseline"]["n"] < MIN_PROOF_SAMPLE:
        print(
            f"  sample warning: only {state['baseline']['n']}/{MIN_PROOF_SAMPLE} clean proof rows with usable model_probability."
        )
    print("  this is not proof of a live candidate; it is calibration and payoff truth only.")
    print()
    print(f"Sentinel: {SENTINEL}")
    print("=" * 84)


def main() -> None:
    records = load_trades(TRADES_LOG)
    state = build_report_state(records)
    render_report(state)


if __name__ == "__main__":
    main()
