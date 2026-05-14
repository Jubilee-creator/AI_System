#!/usr/bin/env python3
"""
Phase 10J - Calibration + Payoff Truth Map
Sentinel: CALIBRATION_PAYOFF_TRUTH_MAP_OK

Read-only report answering: when is model probability actually trustworthy?
It uses clean settled proof rows for outcome truth and candidate/funnel rows
only for blocker diagnostics.  It does not modify logs, thresholds, scanner
ordering, PaperTrader, risk state, or live-money settings.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (  # noqa: E402
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    QUARANTINED_TICKER_PREFIXES,
    TRADING_MODE,
)
from tools import report_probability_calibration_payoff_truth as calib  # noqa: E402
from tools.report_accounting_version_proof_cohorts import (  # noqa: E402
    economic_pnl_value,
    entry_price,
    load_trades,
    risk_edge,
)
from tools.report_payoff_geometry_candidate_quality_autopsy import (  # noqa: E402
    FUNNEL_LOG,
    SCANNER_TAIL_BYTES,
    TRADES_LOG,
    action_of,
    final_reason,
    read_jsonl,
    reward_risk,
    side_entry_price,
)

SENTINEL = "CALIBRATION_PAYOFF_TRUTH_MAP_OK"
MIN_TRUST_N = 30
MAX_ACCEPTABLE_CALIBRATION_ERROR = 0.10


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def model_probability(row: dict[str, Any]) -> float | None:
    value = calib.model_probability_value(row)
    if value is not None:
        return value
    return _as_float(row.get("confidence"))


def action_side(row: dict[str, Any]) -> str:
    return str(row.get("scanner_action") or row.get("intended_action") or row.get("action") or "UNKNOWN").upper()


def market_family(row: dict[str, Any]) -> str:
    ticker = str(row.get("ticker") or "UNKNOWN")
    return ticker.split("-", 1)[0] if "-" in ticker else ticker


def short_expiry_family(row: dict[str, Any]) -> str:
    ticker = str(row.get("ticker") or "").upper()
    tte = _as_float(row.get("time_to_expiry"))
    if "15M" in ticker:
        return "15M"
    if tte is not None:
        if tte <= 1.0:
            return "<=1h"
        if tte <= 6.0:
            return "1-6h"
        if tte <= 24.0:
            return "6-24h"
        return ">24h"
    return "unknown"


def confidence_bucket(row: dict[str, Any]) -> str:
    prob = model_probability(row)
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


def entry_bucket(row: dict[str, Any]) -> str:
    price = entry_price(row)
    if price is None:
        price = side_entry_price(row)
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


def reward_risk_bucket(row: dict[str, Any]) -> str:
    rr = reward_risk(entry_price(row) if entry_price(row) is not None else side_entry_price(row))
    if rr is None:
        return "missing"
    if rr < 0.15:
        return "<0.15"
    if rr < 0.25:
        return "0.15-0.25"
    if rr < 0.50:
        return "0.25-0.50"
    if rr < 1.00:
        return "0.50-1.00"
    return "1.00+"


def model_margin(row: dict[str, Any]) -> float | None:
    prob = model_probability(row)
    price = entry_price(row)
    if price is None:
        price = side_entry_price(row)
    if prob is None or price is None:
        return None
    return prob - price


def model_margin_bucket(row: dict[str, Any]) -> str:
    margin = model_margin(row)
    if margin is None:
        return "missing"
    if margin < 0.0:
        return "<0"
    if margin < 0.03:
        return "0.00-0.03"
    if margin < 0.05:
        return "0.03-0.05"
    if margin < 0.10:
        return "0.05-0.10"
    return "0.10+"


def edge_bucket(row: dict[str, Any]) -> str:
    edge = risk_edge(row)
    if edge is None:
        edge = _as_float(row.get("edge"))
    if edge is None:
        return "missing"
    if edge < 0.03:
        return "<0.03"
    if edge < 0.05:
        return "0.03-0.05"
    if edge < 0.08:
        return "0.05-0.08"
    if edge < 0.10:
        return "0.08-0.10"
    return "0.10+"


def council_bucket(row: dict[str, Any]) -> str:
    decision = str(row.get("council_decision") or "UNKNOWN").upper()
    if bool(row.get("bootstrap_era_council_allow")):
        return "BOOTSTRAP_ERA_ALLOW"
    if bool(row.get("bootstrap_provisional")):
        return "BOOTSTRAP_PROVISIONAL"
    return decision


def _avg(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def _profit_factor(rows: list[dict[str, Any]]) -> float | None:
    wins = 0.0
    losses = 0.0
    for row in rows:
        pnl = economic_pnl_value(row)
        if pnl is None:
            continue
        if pnl > 0:
            wins += pnl
        elif pnl < 0:
            losses += pnl
    if wins <= 0 or losses >= 0:
        return None
    return wins / abs(losses)


def _bucket_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [row for row in rows if str(row.get("result") or "").upper() == "WIN"]
    losses = [row for row in rows if str(row.get("result") or "").upper() == "LOSS"]
    n = len(wins) + len(losses)
    win_rate = len(wins) / n if n else None
    probs = [model_probability(row) for row in rows]
    entries = [entry_price(row) if entry_price(row) is not None else side_entry_price(row) for row in rows]
    margins = [model_margin(row) for row in rows]
    rrs = [reward_risk(price) for price in entries]
    pnls = [economic_pnl_value(row) for row in rows]
    total_pnl = sum(value for value in pnls if value is not None)
    capital = [calib.capital_at_risk_value(row) for row in rows]
    total_capital = sum(value for value in capital if value is not None)
    avg_model = _avg(probs)
    avg_entry = _avg(entries)
    calibration_error = (win_rate - avg_model) if win_rate is not None and avg_model is not None else None
    breakeven_wr = avg_entry
    wr_margin = (win_rate - breakeven_wr) if win_rate is not None and breakeven_wr is not None else None
    roi = total_pnl / total_capital if total_capital > 0 else None
    pf = _profit_factor(rows)
    avg_win = _avg([economic_pnl_value(row) for row in wins])
    avg_loss = _avg([economic_pnl_value(row) for row in losses])
    avg_rr = _avg(rrs)
    avg_margin = _avg(margins)
    status, flags = classify_bucket({
        "n": n,
        "win_rate": win_rate,
        "breakeven_wr": breakeven_wr,
        "wr_margin": wr_margin,
        "roi": roi,
        "profit_factor": pf,
        "calibration_error": calibration_error,
        "avg_model_margin": avg_margin,
        "avg_entry_price": avg_entry,
        "avg_reward_risk": avg_rr,
        "total_economic_pnl": total_pnl,
    })
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "breakeven_wr": breakeven_wr,
        "wr_margin": wr_margin,
        "roi": roi,
        "profit_factor": pf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_reward_risk": avg_rr,
        "avg_entry_price": avg_entry,
        "avg_model_probability": avg_model,
        "avg_model_margin": avg_margin,
        "calibration_error": calibration_error,
        "total_economic_pnl": total_pnl,
        "status": status,
        "flags": flags,
    }


def classify_bucket(stats: dict[str, Any]) -> tuple[str, list[str]]:
    n = int(stats.get("n") or 0)
    roi = stats.get("roi")
    pf = stats.get("profit_factor")
    wr_margin = stats.get("wr_margin")
    calibration_error = stats.get("calibration_error")
    avg_margin = stats.get("avg_model_margin")
    avg_entry = stats.get("avg_entry_price")
    avg_rr = stats.get("avg_reward_risk")
    flags: list[str] = []

    if n < MIN_TRUST_N:
        if n > 0 and roi is not None and roi > 0:
            flags.append("TINY_POSITIVE_TRAP")
        status = "TOO_SMALL"
    elif (
        roi is not None and roi > 0
        and pf is not None and pf > 1.10
        and wr_margin is not None and wr_margin > 0
        and calibration_error is not None
        and abs(calibration_error) <= MAX_ACCEPTABLE_CALIBRATION_ERROR
    ):
        status = "TRUSTED"
    elif roi is not None and roi < 0 and (pf is None or pf <= 1.10):
        status = "DANGEROUS"
    else:
        status = "WEAK"

    if calibration_error is not None and calibration_error <= -MAX_ACCEPTABLE_CALIBRATION_ERROR:
        flags.append("OVERCONFIDENT")
    if avg_entry is not None and 0.80 <= avg_entry < 0.90:
        flags.append("EXPENSIVE_ENTRY_TRAP")
    if avg_entry is not None and avg_entry >= 0.90:
        flags.append("EXTREME_ENTRY_TRAP")
    if avg_margin is not None and avg_margin <= 0:
        flags.append("MODEL_PROB_BELOW_BREAKEVEN")
    if avg_rr is not None and avg_rr < 0.25:
        flags.append("WEAK_REWARD_RISK")
    if wr_margin is not None and wr_margin < 0:
        flags.append("WIN_RATE_BELOW_BREAKEVEN")
    if roi is not None and roi < 0:
        flags.append("NEGATIVE_ROI")
    if pf is not None and pf <= 1.10:
        flags.append("PF_NOT_ENOUGH")
    elif pf is None and n > 0:
        flags.append("PF_MISSING")

    return status, list(dict.fromkeys(flags))


def _group(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> list[tuple[str, dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    items = [(key, _bucket_summary(value)) for key, value in grouped.items()]
    return sorted(items, key=lambda item: (-item[1]["n"], item[0]))


def _candidate_blocker_counts(funnel_rows: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in funnel_rows:
        grouped[final_reason(row)].append(row)
    items = []
    for key, rows in grouped.items():
        count = len(rows)
        prices = [side_entry_price(row) for row in rows]
        margins = [model_margin(row) for row in rows]
        items.append((
            key,
            {
                "n": count,
                "avg_entry_price": _avg(prices),
                "avg_model_margin": _avg(margins),
                "action_mix": dict(Counter(action_of(row) for row in rows).most_common(5)),
            },
        ))
    return sorted(items, key=lambda item: (-item[1]["n"], item[0]))


def _best_bucket(groups: dict[str, list[tuple[str, dict[str, Any]]]], status: str) -> tuple[str, str, dict[str, Any]] | None:
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    for group_name, rows in groups.items():
        for key, stats in rows:
            if stats["status"] == status:
                candidates.append((group_name, key, stats))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (-(item[2].get("roi") or -999), -(item[2].get("n") or 0)))[0]


def _worst_by(groups: dict[str, list[tuple[str, dict[str, Any]]]], field: str) -> tuple[str, str, dict[str, Any]] | None:
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    for group_name, rows in groups.items():
        for key, stats in rows:
            value = stats.get(field)
            if value is not None and stats.get("n", 0) > 0:
                candidates.append((group_name, key, stats))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[2][field], -(item[2].get("n") or 0)))[0]


def _rows_with_flag(groups: dict[str, list[tuple[str, dict[str, Any]]]], flag: str) -> list[tuple[str, str, dict[str, Any]]]:
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for group_name, items in groups.items():
        for key, stats in items:
            if flag in stats.get("flags", []):
                rows.append((group_name, key, stats))
    return sorted(rows, key=lambda item: (-(item[2].get("n") or 0), item[0], item[1]))


def build_report(
    trades_path: Path = TRADES_LOG,
    funnel_path: Path = FUNNEL_LOG,
) -> dict[str, Any]:
    trades = load_trades(trades_path)
    rows = calib.calibration_rows(trades)
    funnel_rows, funnel_tail_limited = read_jsonl(funnel_path, SCANNER_TAIL_BYTES)

    groups = {
        "confidence": _group(rows, confidence_bucket),
        "entry_price": _group(rows, entry_bucket),
        "reward_risk": _group(rows, reward_risk_bucket),
        "model_minus_breakeven": _group(rows, model_margin_bucket),
        "edge": _group(rows, edge_bucket),
        "side": _group(rows, action_side),
        "market_family": _group(rows, market_family),
        "short_expiry_family": _group(rows, short_expiry_family),
        "council_decision": _group(rows, council_bucket),
    }

    trusted = [
        (group_name, key, stats)
        for group_name, items in groups.items()
        for key, stats in items
        if stats["status"] == "TRUSTED"
    ]
    dangerous = [
        (group_name, key, stats)
        for group_name, items in groups.items()
        for key, stats in items
        if stats["status"] == "DANGEROUS"
    ]
    overconfident = _rows_with_flag(groups, "OVERCONFIDENT")
    tiny_traps = _rows_with_flag(groups, "TINY_POSITIVE_TRAP")
    below_be = _rows_with_flag(groups, "MODEL_PROB_BELOW_BREAKEVEN")

    trusted.sort(key=lambda item: (-(item[2].get("roi") or -999), -(item[2].get("n") or 0)))
    dangerous.sort(key=lambda item: ((item[2].get("roi") if item[2].get("roi") is not None else 999), -(item[2].get("n") or 0)))
    overconfident.sort(key=lambda item: ((item[2].get("calibration_error") if item[2].get("calibration_error") is not None else 999), -(item[2].get("n") or 0)))

    entry_80_90 = next((stats for key, stats in groups["entry_price"] if key == "0.80-0.90"), None)
    conf_90 = next((stats for key, stats in groups["confidence"] if key == "0.90+"), None)
    confidence_groups = groups["confidence"]
    rr_groups = groups["reward_risk"]
    worst_conf = _worst_by({"confidence": confidence_groups}, "roi")
    worst_rr = _worst_by({"reward_risk": rr_groups}, "roi")

    return {
        "read_only": True,
        "counts": {
            "trades": len(trades),
            "calibration_rows": len(rows),
            "funnel_rows": len(funnel_rows),
            "funnel_tail_limited": funnel_tail_limited,
        },
        "overall": _bucket_summary(rows),
        "groups": groups,
        "final_blocker_candidate_diagnostics": _candidate_blocker_counts(funnel_rows),
        "trusted_buckets": trusted,
        "dangerous_buckets": dangerous,
        "overconfident_buckets": overconfident,
        "tiny_positive_traps": tiny_traps,
        "model_below_breakeven_buckets": below_be,
        "answers": {
            "strongest_trusted_bucket": trusted[0] if trusted else None,
            "worst_overconfidence_bucket": overconfident[0] if overconfident else None,
            "worst_payoff_bucket": dangerous[0] if dangerous else _worst_by(groups, "roi"),
            "entry_80_90_dangerous": bool(entry_80_90 and entry_80_90["status"] in {"DANGEROUS", "WEAK"} and (entry_80_90.get("roi") or 0.0) <= 0),
            "confidence_90_plus_trustworthy": bool(conf_90 and conf_90["status"] == "TRUSTED"),
            "reward_risk_predicts_better_than_confidence": bool(
                worst_rr and worst_conf and (worst_rr[2].get("roi") or 0.0) < (worst_conf[2].get("roi") or 0.0)
            ),
            "model_should_remain_frozen": True,
            "next_safest_research_step": "Forward-validate calibration by entry/reward-risk/side and require trusted buckets before live ranking or strategy changes.",
        },
        "safety": {
            "trading_mode": TRADING_MODE,
            "paper_only": TRADING_MODE == "PAPER",
            "real_money_allowed": False,
            "scale_allowed": False,
            "kelly_execution_disabled": GLOBAL_FORCED_LEARNING_MODE,
            "dc_override_enabled": DATA_COLLECTION_OVERRIDE_ENABLED,
            "kxeth_quarantine_active": "KXETH" in {str(prefix).upper() for prefix in QUARANTINED_TICKER_PREFIXES},
        },
    }


def _print_bucket_table(title: str, rows: list[tuple[str, dict[str, Any]]], limit: int = 20) -> None:
    print()
    print(title)
    print("-" * len(title))
    if not rows:
        print("(none)")
        return
    print(
        f"{'bucket':<28} {'n':>5} {'WR':>8} {'BE':>8} {'mrg':>8} {'ROI':>8} "
        f"{'PF':>8} {'avgW':>8} {'avgL':>8} {'RR':>8} {'entry':>8} {'calErr':>8} {'status':>10}"
    )
    for key, stats in rows[:limit]:
        print(
            f"{key:<28} {stats['n']:>5} {_fmt_pct(stats['win_rate']):>8} "
            f"{_fmt_pct(stats['breakeven_wr']):>8} {_fmt_pct(stats['wr_margin']):>8} "
            f"{_fmt_pct(stats['roi']):>8} {_fmt_num(stats['profit_factor']):>8} "
            f"{_fmt_num(stats['avg_win']):>8} {_fmt_num(stats['avg_loss']):>8} "
            f"{_fmt_num(stats['avg_reward_risk']):>8} {_fmt_num(stats['avg_entry_price']):>8} "
            f"{_fmt_pct(stats['calibration_error']):>8} {stats['status']:>10}"
        )
        if stats["flags"]:
            print(f"{'':<28} flags={','.join(stats['flags'])}")


def _describe_named(item: tuple[str, str, dict[str, Any]] | None) -> str:
    if item is None:
        return "NONE"
    group, key, stats = item
    return (
        f"{group}/{key} n={stats['n']} roi={_fmt_pct(stats['roi'])} "
        f"pf={_fmt_num(stats['profit_factor'])} wr={_fmt_pct(stats['win_rate'])} "
        f"be={_fmt_pct(stats['breakeven_wr'])} calErr={_fmt_pct(stats['calibration_error'])} "
        f"status={stats['status']}"
    )


def print_report(report: dict[str, Any]) -> None:
    print("=" * 104)
    print("CALIBRATION + PAYOFF TRUTH MAP")
    print("=" * 104)
    print("Read-only: no strategy, thresholds, scanner order, PaperTrader, logs, risk, or live-money state are modified.")
    print(f"trades loaded:        {_fmt_int(report['counts']['trades'])}")
    print(f"calibration rows:     {_fmt_int(report['counts']['calibration_rows'])}")
    print(f"funnel rows loaded:   {_fmt_int(report['counts']['funnel_rows'])}")
    print(f"funnel tail limited:  {report['counts']['funnel_tail_limited']}")

    _print_bucket_table("OVERALL CLEAN CALIBRATION", [("overall", report["overall"])], limit=1)
    for name, rows in report["groups"].items():
        _print_bucket_table(name.upper().replace("_", " "), rows)

    print()
    print("FINAL BLOCKER CANDIDATE DIAGNOSTICS (NO OUTCOME CREDIT)")
    print("-" * 58)
    for key, stats in report["final_blocker_candidate_diagnostics"][:15]:
        print(
            f"{key:<34} n={stats['n']:>7,} avg_entry={_fmt_num(stats['avg_entry_price'])} "
            f"m-be={_fmt_num(stats['avg_model_margin'])} actions={stats['action_mix']}"
        )

    print()
    print("DIRECT ANSWERS")
    print("-" * 104)
    print(f"strongest_trusted_bucket:       {_describe_named(report['answers']['strongest_trusted_bucket'])}")
    print(f"worst_overconfidence_bucket:    {_describe_named(report['answers']['worst_overconfidence_bucket'])}")
    print(f"worst_payoff_bucket:            {_describe_named(report['answers']['worst_payoff_bucket'])}")
    print(f"entry_0.80_0.90_dangerous:      {report['answers']['entry_80_90_dangerous']}")
    print(f"confidence_0.90_plus_trusted:   {report['answers']['confidence_90_plus_trustworthy']}")
    print(f"rr_predicts_better_than_conf:   {report['answers']['reward_risk_predicts_better_than_confidence']}")
    print(f"model_should_remain_frozen:     {report['answers']['model_should_remain_frozen']}")
    print(f"next_safest_research_step:      {report['answers']['next_safest_research_step']}")
    print(f"trusted_bucket_count:           {len(report['trusted_buckets'])}")
    print(f"dangerous_bucket_count:         {len(report['dangerous_buckets'])}")
    print(f"overconfident_bucket_count:     {len(report['overconfident_buckets'])}")
    print(f"tiny_positive_trap_count:       {len(report['tiny_positive_traps'])}")
    print(f"model_below_breakeven_count:    {len(report['model_below_breakeven_buckets'])}")

    print()
    print("SAFETY LOCKS")
    print("-" * 104)
    for key, value in report["safety"].items():
        print(f"{key:<28} {value}")

    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> int:
    print_report(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
