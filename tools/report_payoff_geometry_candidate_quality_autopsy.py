#!/usr/bin/env python3
"""
Phase 10D - Payoff Geometry + Candidate Quality Failure Autopsy
Sentinel: PAYOFF_GEOMETRY_CANDIDATE_QUALITY_AUTOPSY_OK

Read-only report for candidate-level economics. It separates ex-ante scanner /
funnel candidate geometry from settled clean proof outcomes so blocked rows are
not counted as profit evidence.
"""
from __future__ import annotations

import json
import math
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
from tools.report_accounting_version_proof_cohorts import (  # noqa: E402
    ECONOMIC_VERSION,
    classify_accounting_version,
    economic_pnl_value,
    entry_price,
    is_clean_proof_row,
    is_kxeth_or_quarantined,
    load_trades,
)
from tools.report_fresh_economic_proof_autopsy import (  # noqa: E402
    fresh_proof_rows,
    risk_edge,
    summarize_rows,
)

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
SCANNER_LOG = ROOT / "logs" / "scanner_opportunities.jsonl"
SCANNER_TAIL_BYTES = 50_000_000
SENTINEL = "PAYOFF_GEOMETRY_CANDIDATE_QUALITY_AUTOPSY_OK"

MIN_POCKET_SAMPLE = 30
MIN_WATCH_SAMPLE = 15
EXPENSIVE_ENTRY = 0.80
WEAK_REWARD_RISK = 0.25


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def parse_jsonl_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def read_jsonl(path: Path, max_tail_bytes: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], False
    start = 0
    tail_limited = False
    if max_tail_bytes is not None:
        size = path.stat().st_size
        if size > max_tail_bytes:
            start = size - max_tail_bytes
            tail_limited = True
    rows: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        handle.seek(start)
        if start:
            handle.readline()
        for raw in handle:
            row = parse_jsonl_line(raw.decode("utf-8", errors="replace"))
            if row is not None:
                rows.append(row)
    return rows, tail_limited


def market_family(ticker: Any) -> str:
    if not ticker:
        return "UNKNOWN"
    ticker_text = str(ticker)
    return ticker_text.split("-", 1)[0] if "-" in ticker_text else ticker_text


def action_of(row: dict[str, Any]) -> str:
    return str(row.get("scanner_action") or row.get("action") or "UNKNOWN").upper()


def side_entry_price(row: dict[str, Any]) -> float | None:
    explicit = _as_float(row.get("entry_price"))
    if explicit is not None:
        return explicit
    action = action_of(row)
    if action == "BET_NO":
        return _as_float(row.get("no_ask") if row.get("no_ask") is not None else row.get("price_no"))
    if action == "BET_YES":
        return _as_float(row.get("yes_ask") if row.get("yes_ask") is not None else row.get("price_yes"))
    if action == "ARB":
        yes_ask = _as_float(row.get("yes_ask") if row.get("yes_ask") is not None else row.get("price_yes"))
        no_ask = _as_float(row.get("no_ask") if row.get("no_ask") is not None else row.get("price_no"))
        if yes_ask is not None and no_ask is not None:
            return min(yes_ask, no_ask)
    return None


def breakeven_wr(price: float | None) -> float | None:
    if price is None:
        return None
    return price


def reward_risk(price: float | None) -> float | None:
    if price is None or price <= 0:
        return None
    return (1.0 - price) / price


def model_probability(row: dict[str, Any]) -> float | None:
    return _as_float(row.get("model_probability") if row.get("model_probability") is not None else row.get("confidence"))


def model_minus_breakeven(row: dict[str, Any]) -> float | None:
    prob = model_probability(row)
    be = breakeven_wr(side_entry_price(row))
    if prob is None or be is None:
        return None
    return prob - be


def spread_value(row: dict[str, Any]) -> float | None:
    value = _as_float(row.get("spread") if row.get("spread") is not None else row.get("market_spread"))
    if value is not None:
        return value
    yes_bid = _as_float(row.get("yes_bid"))
    yes_ask = _as_float(row.get("yes_ask"))
    if yes_bid is not None and yes_ask is not None:
        return max(0.0, yes_ask - yes_bid)
    return None


def overround_value(row: dict[str, Any]) -> float | None:
    explicit = _as_float(row.get("overround"))
    if explicit is not None:
        return explicit
    yes_ask = _as_float(row.get("yes_ask") if row.get("yes_ask") is not None else row.get("price_yes"))
    no_ask = _as_float(row.get("no_ask") if row.get("no_ask") is not None else row.get("price_no"))
    if yes_ask is not None and no_ask is not None:
        return yes_ask + no_ask - 1.0
    yes_plus_no = _as_float(row.get("yes_plus_no"))
    if yes_plus_no is not None:
        return yes_plus_no - 1.0
    return None


def edge_value(row: dict[str, Any]) -> float | None:
    return _as_float(
        row.get("risk_edge")
        if row.get("risk_edge") is not None
        else row.get("edge")
    )


def entry_bucket(price: float | None) -> str:
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


def reward_risk_bucket(rr: float | None) -> str:
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


def breakeven_bucket(be: float | None) -> str:
    if be is None:
        return "missing"
    if be < 0.60:
        return "<0.60"
    if be < 0.70:
        return "0.60-0.70"
    if be < 0.80:
        return "0.70-0.80"
    if be < 0.90:
        return "0.80-0.90"
    return "0.90+"


def model_margin_bucket(margin: float | None) -> str:
    if margin is None:
        return "missing"
    if margin < 0:
        return "<0"
    if margin < 0.03:
        return "0.00-0.03"
    if margin < 0.05:
        return "0.03-0.05"
    if margin < 0.10:
        return "0.05-0.10"
    return "0.10+"


def edge_bucket(edge: float | None) -> str:
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


def confidence_bucket(prob: float | None) -> str:
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


def rank_bucket(row: dict[str, Any]) -> str:
    rank = _as_float(row.get("scan_non_pass_rank") if row.get("scan_non_pass_rank") is not None else row.get("opportunity_rank"))
    if rank is None:
        return "missing"
    if rank <= 3:
        return "1-3"
    if rank <= 10:
        return "4-10"
    if rank <= 25:
        return "11-25"
    return "26+"


def candidate_quality_flags(row: dict[str, Any]) -> set[str]:
    price = side_entry_price(row)
    rr = reward_risk(price)
    margin = model_minus_breakeven(row)
    edge = edge_value(row)
    flags: set[str] = set()
    if price is not None and price >= EXPENSIVE_ENTRY:
        flags.add("expensive_entry")
    if rr is not None and rr < WEAK_REWARD_RISK:
        flags.add("weak_reward_risk")
    if margin is not None and margin <= 0:
        flags.add("model_below_breakeven")
    if edge is not None and edge >= 0.05 and (
        "expensive_entry" in flags
        or "weak_reward_risk" in flags
        or "model_below_breakeven" in flags
    ):
        flags.add("model_edge_bad_geometry")
    return flags


def clean_fresh_rows(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in fresh_proof_rows(trades)
        if classify_accounting_version(row) == ECONOMIC_VERSION
        and is_clean_proof_row(row)
        and not is_kxeth_or_quarantined(row)
        and economic_pnl_value(row) is not None
    ]


def summarize_candidate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [side_entry_price(row) for row in rows]
    prices = [value for value in prices if value is not None]
    rrs = [reward_risk(side_entry_price(row)) for row in rows]
    rrs = [value for value in rrs if value is not None]
    margins = [model_minus_breakeven(row) for row in rows]
    margins = [value for value in margins if value is not None]
    probs = [model_probability(row) for row in rows]
    probs = [value for value in probs if value is not None]
    edges = [edge_value(row) for row in rows]
    edges = [value for value in edges if value is not None]
    spreads = [spread_value(row) for row in rows]
    spreads = [value for value in spreads if value is not None]
    overrounds = [overround_value(row) for row in rows]
    overrounds = [value for value in overrounds if value is not None]

    flags = Counter(flag for row in rows for flag in candidate_quality_flags(row))
    opened = sum(1 for row in rows if bool(row.get("paper_trade_opened")) or str(row.get("final_reason") or "").upper() == "TRADE_OPENED")
    blocked = sum(1 for row in rows if not (bool(row.get("paper_trade_opened")) or str(row.get("final_reason") or "").upper() == "TRADE_OPENED"))

    return {
        "n": len(rows),
        "opened": opened,
        "blocked": blocked,
        "avg_entry": sum(prices) / len(prices) if prices else None,
        "avg_reward_risk": sum(rrs) / len(rrs) if rrs else None,
        "avg_model_margin": sum(margins) / len(margins) if margins else None,
        "avg_probability": sum(probs) / len(probs) if probs else None,
        "avg_edge": sum(edges) / len(edges) if edges else None,
        "avg_spread": sum(spreads) / len(spreads) if spreads else None,
        "avg_overround": sum(overrounds) / len(overrounds) if overrounds else None,
        "expensive_entry": flags.get("expensive_entry", 0),
        "weak_reward_risk": flags.get("weak_reward_risk", 0),
        "model_below_breakeven": flags.get("model_below_breakeven", 0),
        "model_edge_bad_geometry": flags.get("model_edge_bad_geometry", 0),
    }


def summarize_settled_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = summarize_rows(rows)
    base["avg_model_margin"] = _avg(model_minus_breakeven(row) for row in rows)
    base["avg_reward_risk"] = _avg(reward_risk(side_entry_price(row)) for row in rows)
    base["avg_spread"] = _avg(spread_value(row) for row in rows)
    base["avg_overround"] = _avg(overround_value(row) for row in rows)
    return base


def _avg(values: Any) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def group_by(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    return dict(grouped)


def ordered_candidate_groups(
    rows: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
    order: list[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    groups = group_by(rows, key_fn)
    keys = order or sorted(groups)
    pairs = [(key, summarize_candidate_rows(groups[key])) for key in keys if key in groups]
    if order is None:
        pairs.sort(key=lambda item: (-item[1]["n"], item[0]))
    return pairs


def ordered_settled_groups(
    rows: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], str],
    order: list[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    groups = group_by(rows, key_fn)
    keys = order or sorted(groups)
    pairs = [(key, summarize_settled_rows(groups[key])) for key in keys if key in groups]
    if order is None:
        pairs.sort(key=lambda item: (-item[1]["n"], item[0]))
    return pairs


def final_reason(row: dict[str, Any]) -> str:
    return str(row.get("final_reason") or "UNKNOWN")


def action_key(row: dict[str, Any]) -> str:
    return action_of(row)


def family_key(row: dict[str, Any]) -> str:
    return market_family(row.get("ticker"))


def build_report(
    trades_path: Path = TRADES_LOG,
    funnel_path: Path = FUNNEL_LOG,
    scanner_path: Path = SCANNER_LOG,
    scanner_tail_bytes: int = SCANNER_TAIL_BYTES,
) -> dict[str, Any]:
    trades = load_trades(trades_path)
    funnel_rows, _ = read_jsonl(funnel_path)
    scanner_rows, scanner_tail_limited = read_jsonl(scanner_path, max_tail_bytes=scanner_tail_bytes)
    fresh_rows = clean_fresh_rows(trades)

    candidate_rows = list(funnel_rows)
    candidate_source = "execution_funnel"
    if not candidate_rows:
        candidate_rows = [row for row in scanner_rows if action_of(row) != "PASS"]
        candidate_source = "scanner_non_pass_tail"

    opened_funnel = [
        row
        for row in candidate_rows
        if bool(row.get("paper_trade_opened")) or str(row.get("final_reason") or "").upper() == "TRADE_OPENED"
    ]
    blocked_funnel = [row for row in candidate_rows if row not in opened_funnel]

    candidate_summary = summarize_candidate_rows(candidate_rows)
    settled_summary = summarize_settled_rows(fresh_rows)

    bad_geometry_rows = [
        row for row in candidate_rows
        if {"expensive_entry", "weak_reward_risk", "model_below_breakeven"} & candidate_quality_flags(row)
    ]
    good_geometry_rows = [
        row for row in candidate_rows
        if side_entry_price(row) is not None
        and (side_entry_price(row) or 0.0) < 0.70
        and (reward_risk(side_entry_price(row)) or 0.0) >= 0.50
        and (model_minus_breakeven(row) or -1.0) > 0.03
    ]
    opened_good_geometry = [
        row for row in fresh_rows
        if side_entry_price(row) is not None
        and (side_entry_price(row) or 0.0) < 0.70
        and (reward_risk(side_entry_price(row)) or 0.0) >= 0.50
    ]

    arb_rows = [row for row in candidate_rows if action_of(row) == "ARB"]
    bet_no_rows = [row for row in candidate_rows if action_of(row) == "BET_NO"]
    bet_yes_rows = [row for row in candidate_rows if action_of(row) == "BET_YES"]

    arb_reason_counts = Counter(final_reason(row) for row in arb_rows)
    bet_no_reason_counts = Counter(final_reason(row) for row in bet_no_rows)

    arb_opened = sum(1 for row in arb_rows if row.get("paper_trade_opened") or final_reason(row) == "TRADE_OPENED")
    bet_no_opened = sum(1 for row in bet_no_rows if row.get("paper_trade_opened") or final_reason(row) == "TRADE_OPENED")

    if not arb_rows:
        arb_verdict = "NO_RECENT_ARB_EVIDENCE"
    elif arb_opened == 0:
        arb_verdict = "UNPROVEN_BLOCKED_EDGE"
    else:
        arb_verdict = "ARB_OPENED_REQUIRES_OUTCOME_AUDIT"

    if not bet_no_rows:
        bet_no_verdict = "NO_BET_NO_CANDIDATES"
    elif bet_no_opened == 0:
        bet_no_verdict = "UNDER_TESTED_BLOCKED_BEFORE_OPEN"
    else:
        bet_no_verdict = "BET_NO_HAS_OPENED_ROWS"

    enough_sample_pockets = []
    tiny_positive_traps = []
    for key, stats in ordered_settled_groups(
        fresh_rows,
        lambda row: f"{edge_bucket(edge_value(row))}|{entry_bucket(side_entry_price(row))}",
    ):
        if stats["n"] >= MIN_POCKET_SAMPLE and stats.get("roi") is not None and stats.get("roi") > 0:
            enough_sample_pockets.append((key, stats))
        elif 0 < stats["n"] < MIN_POCKET_SAMPLE and stats.get("total_economic_pnl", 0.0) > 0:
            tiny_positive_traps.append((key, stats))

    if enough_sample_pockets:
        pocket_verdict = "HAS_SAMPLE_SUPPORTED_POSITIVE_POCKET"
    elif tiny_positive_traps:
        pocket_verdict = "ONLY_TINY_POSITIVE_TRAPS"
    else:
        pocket_verdict = "NO_ACTIONABLE_POSITIVE_POCKET"

    if settled_summary["n"]:
        reason_current_failure = (
            "payoff_geometry_and_overconfidence: realized win rate is below "
            "breakeven win rate and losses dominate wins"
        )
    elif candidate_summary["n"]:
        reason_current_failure = "candidate_pipeline_blocked_before_new_clean_proof"
    else:
        reason_current_failure = "no_candidate_evidence_available"

    economically_bad = (
        candidate_summary["expensive_entry"]
        + candidate_summary["weak_reward_risk"]
        + candidate_summary["model_below_breakeven"]
    )
    scanner_quality = (
        "mostly_economically_bad_candidates"
        if candidate_summary["n"] and economically_bad >= candidate_summary["n"] * 0.50
        else "mixed_or_insufficient_candidate_evidence"
    )

    return {
        "paths": {
            "trades": str(trades_path),
            "funnel": str(funnel_path),
            "scanner": str(scanner_path),
        },
        "scanner_tail_limited": scanner_tail_limited,
        "candidate_source": candidate_source,
        "counts": {
            "trades": len(trades),
            "fresh_clean_settled": len(fresh_rows),
            "funnel_rows": len(funnel_rows),
            "scanner_rows_loaded": len(scanner_rows),
            "candidate_rows": len(candidate_rows),
            "blocked_candidates": len(blocked_funnel),
            "opened_candidates": len(opened_funnel),
            "bad_geometry_candidates": len(bad_geometry_rows),
            "good_geometry_candidates": len(good_geometry_rows),
            "opened_good_geometry_clean_rows": len(opened_good_geometry),
        },
        "candidate_summary": candidate_summary,
        "settled_summary": settled_summary,
        "candidate_groups": {
            "entry_price": ordered_candidate_groups(candidate_rows, lambda row: entry_bucket(side_entry_price(row)), ["<0.50", "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00", "missing"]),
            "reward_risk": ordered_candidate_groups(candidate_rows, lambda row: reward_risk_bucket(reward_risk(side_entry_price(row))), ["<0.15", "0.15-0.25", "0.25-0.50", "0.50-1.00", "1.00+", "missing"]),
            "breakeven": ordered_candidate_groups(candidate_rows, lambda row: breakeven_bucket(breakeven_wr(side_entry_price(row))), ["<0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90+", "missing"]),
            "model_margin": ordered_candidate_groups(candidate_rows, lambda row: model_margin_bucket(model_minus_breakeven(row)), ["<0", "0.00-0.03", "0.03-0.05", "0.05-0.10", "0.10+", "missing"]),
            "edge": ordered_candidate_groups(candidate_rows, lambda row: edge_bucket(edge_value(row)), ["<0.03", "0.03-0.05", "0.05-0.08", "0.08-0.10", "0.10+", "missing"]),
            "confidence": ordered_candidate_groups(candidate_rows, lambda row: confidence_bucket(model_probability(row)), ["<0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90+", "missing"]),
            "rank": ordered_candidate_groups(candidate_rows, rank_bucket, ["1-3", "4-10", "11-25", "26+", "missing"]),
            "action": ordered_candidate_groups(candidate_rows, action_key, ["BET_YES", "BET_NO", "ARB", "UNKNOWN"]),
            "final_reason": ordered_candidate_groups(candidate_rows, final_reason),
            "market_family": ordered_candidate_groups(candidate_rows, family_key),
        },
        "settled_groups": {
            "entry_price": ordered_settled_groups(fresh_rows, lambda row: entry_bucket(side_entry_price(row)), ["<0.50", "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00", "missing"]),
            "reward_risk": ordered_settled_groups(fresh_rows, lambda row: reward_risk_bucket(reward_risk(side_entry_price(row))), ["<0.15", "0.15-0.25", "0.25-0.50", "0.50-1.00", "1.00+", "missing"]),
            "model_margin": ordered_settled_groups(fresh_rows, lambda row: model_margin_bucket(model_minus_breakeven(row)), ["<0", "0.00-0.03", "0.03-0.05", "0.05-0.10", "0.10+", "missing"]),
            "edge_price_cell": ordered_settled_groups(fresh_rows, lambda row: f"{edge_bucket(edge_value(row))}|{entry_bucket(side_entry_price(row))}"),
            "market_family": ordered_settled_groups(fresh_rows, family_key),
            "action": ordered_settled_groups(fresh_rows, action_key, ["BET_YES", "BET_NO", "ARB", "UNKNOWN"]),
        },
        "arb": {
            "rows": len(arb_rows),
            "opened": arb_opened,
            "reason_counts": dict(arb_reason_counts.most_common()),
            "summary": summarize_candidate_rows(arb_rows),
            "verdict": arb_verdict,
        },
        "bet_no": {
            "rows": len(bet_no_rows),
            "opened": bet_no_opened,
            "reason_counts": dict(bet_no_reason_counts.most_common()),
            "summary": summarize_candidate_rows(bet_no_rows),
            "verdict": bet_no_verdict,
        },
        "bet_yes": {
            "rows": len(bet_yes_rows),
            "summary": summarize_candidate_rows(bet_yes_rows),
        },
        "pockets": {
            "sample_supported_positive": enough_sample_pockets,
            "tiny_positive_traps": tiny_positive_traps,
            "verdict": pocket_verdict,
        },
        "safety": {
            "trading_mode": TRADING_MODE,
            "paper_only": TRADING_MODE == "PAPER",
            "real_money_allowed": False,
            "scale_allowed": False,
            "kelly_execution_disabled": GLOBAL_FORCED_LEARNING_MODE,
            "dc_override_enabled": DATA_COLLECTION_OVERRIDE_ENABLED,
            "kxeth_quarantine_active": "KXETH" in {str(prefix).upper() for prefix in QUARANTINED_TICKER_PREFIXES},
            "quarantined_prefixes": list(QUARANTINED_TICKER_PREFIXES),
        },
        "verdict": {
            "current_failure": reason_current_failure,
            "scanner_quality": scanner_quality,
            "model": "overconfident_until_proven_otherwise",
            "live_patch_allowed": False,
            "next_best_fix": "read_only_shadow_payoff_filtering_and_new_clean_evidence_collection",
        },
    }


def _print_candidate_table(title: str, rows: list[tuple[str, dict[str, Any]]], limit: int | None = None) -> None:
    print()
    print(title)
    print("-" * len(title))
    if not rows:
        print("(none)")
        return
    print(f"{'bucket':<32} {'n':>7} {'open':>6} {'blk':>7} {'avg_ep':>8} {'RR':>8} {'m-be':>8} {'edge':>8} {'exp':>7} {'weakRR':>7} {'badEdge':>8}")
    display = rows[:limit] if limit else rows
    for key, stats in display:
        print(
            f"{key:<32} {stats['n']:>7} {stats['opened']:>6} {stats['blocked']:>7} "
            f"{_fmt_num(stats['avg_entry']):>8} {_fmt_num(stats['avg_reward_risk']):>8} "
            f"{_fmt_num(stats['avg_model_margin']):>8} {_fmt_num(stats['avg_edge']):>8} "
            f"{stats['expensive_entry']:>7} {stats['weak_reward_risk']:>7} "
            f"{stats['model_edge_bad_geometry']:>8}"
        )


def _print_settled_table(title: str, rows: list[tuple[str, dict[str, Any]]], limit: int | None = None) -> None:
    print()
    print(title)
    print("-" * len(title))
    if not rows:
        print("(none)")
        return
    print(f"{'bucket':<32} {'n':>5} {'WR':>8} {'BE':>8} {'mrg':>8} {'PnL':>9} {'ROI':>8} {'PF':>8} {'RR':>8}")
    display = rows[:limit] if limit else rows
    for key, stats in display:
        print(
            f"{key:<32} {stats['n']:>5} {_fmt_pct(stats.get('win_rate')):>8} "
            f"{_fmt_pct(stats.get('breakeven_wr')):>8} {_fmt_pct(stats.get('wr_margin')):>8} "
            f"{_fmt_money(stats.get('total_economic_pnl')):>9} {_fmt_pct(stats.get('roi')):>8} "
            f"{_fmt_num(stats.get('profit_factor')):>8} {_fmt_num(stats.get('avg_reward_risk')):>8}"
        )


def print_report(report: dict[str, Any]) -> None:
    counts = report["counts"]
    cs = report["candidate_summary"]
    ss = report["settled_summary"]

    print("=" * 94)
    print("PAYOFF GEOMETRY + CANDIDATE QUALITY FAILURE AUTOPSY")
    print("=" * 94)
    print("Read-only: no logs, thresholds, gates, strategy, dashboard, or live-money state are modified.")
    print(f"Candidate source:          {report['candidate_source']}")
    print(f"Scanner tail-limited:      {report['scanner_tail_limited']} ({SCANNER_TAIL_BYTES} bytes)")
    print(f"Paper trades loaded:       {_fmt_int(counts['trades'])}")
    print(f"Fresh clean settled rows:  {_fmt_int(counts['fresh_clean_settled'])}")
    print(f"Funnel rows loaded:        {_fmt_int(counts['funnel_rows'])}")
    print(f"Scanner rows loaded:       {_fmt_int(counts['scanner_rows_loaded'])}")
    print(f"Candidate rows analyzed:   {_fmt_int(counts['candidate_rows'])}")
    print(f"Opened candidate rows:     {_fmt_int(counts['opened_candidates'])}")
    print(f"Blocked candidate rows:    {_fmt_int(counts['blocked_candidates'])}")

    print()
    print("CANDIDATE ECONOMICS")
    print("-" * 94)
    print(f"  avg entry price:                  {_fmt_num(cs['avg_entry'])}")
    print(f"  avg reward/risk:                  {_fmt_num(cs['avg_reward_risk'])}")
    print(f"  avg model probability - BE:       {_fmt_num(cs['avg_model_margin'])}")
    print(f"  avg reported edge:                {_fmt_num(cs['avg_edge'])}")
    print(f"  avg spread:                       {_fmt_num(cs['avg_spread'])}")
    print(f"  avg overround:                    {_fmt_num(cs['avg_overround'])}")
    print(f"  expensive entry candidates:       {_fmt_int(cs['expensive_entry'])}")
    print(f"  weak reward/risk candidates:      {_fmt_int(cs['weak_reward_risk'])}")
    print(f"  model below breakeven candidates: {_fmt_int(cs['model_below_breakeven'])}")
    print(f"  model-edge bad-geometry rows:     {_fmt_int(cs['model_edge_bad_geometry'])}")

    print()
    print("SETTLED CLEAN PROOF ECONOMICS")
    print("-" * 94)
    print(f"  clean rows:                       {_fmt_int(ss['n'])}")
    print(f"  wins / losses:                    {ss.get('wins', 0)} / {ss.get('losses', 0)}")
    print(f"  realized win rate:                {_fmt_pct(ss.get('win_rate'))}")
    print(f"  breakeven win rate:               {_fmt_pct(ss.get('breakeven_wr'))}")
    print(f"  win-rate margin:                  {_fmt_pct(ss.get('wr_margin'))}")
    print(f"  economic PnL:                     {_fmt_money(ss.get('total_economic_pnl'))}")
    print(f"  ROI on capital at risk:           {_fmt_pct(ss.get('roi'))}")
    print(f"  profit factor:                    {_fmt_num(ss.get('profit_factor'))}")
    print(f"  avg win / avg loss:               {_fmt_money(ss.get('avg_win'))} / {_fmt_money(ss.get('avg_loss'))}")
    print(f"  avg reward/risk:                  {_fmt_num(ss.get('avg_reward_risk'))}")

    _print_candidate_table("CANDIDATES BY ENTRY PRICE", report["candidate_groups"]["entry_price"])
    _print_candidate_table("CANDIDATES BY REWARD/RISK", report["candidate_groups"]["reward_risk"])
    _print_candidate_table("CANDIDATES BY MODEL PROBABILITY MINUS BREAKEVEN", report["candidate_groups"]["model_margin"])
    _print_candidate_table("CANDIDATES BY ACTION SIDE", report["candidate_groups"]["action"])
    _print_candidate_table("CANDIDATES BY SCANNER RANK", report["candidate_groups"]["rank"])
    _print_candidate_table("CANDIDATES BY FINAL BLOCKER", report["candidate_groups"]["final_reason"], limit=16)
    _print_candidate_table("CANDIDATES BY MARKET FAMILY", report["candidate_groups"]["market_family"], limit=20)

    _print_settled_table("SETTLED CLEAN OUTCOMES BY ENTRY PRICE", report["settled_groups"]["entry_price"])
    _print_settled_table("SETTLED CLEAN OUTCOMES BY REWARD/RISK", report["settled_groups"]["reward_risk"])
    _print_settled_table("SETTLED CLEAN OUTCOMES BY EDGE x ENTRY CELL", report["settled_groups"]["edge_price_cell"], limit=20)
    _print_settled_table("SETTLED CLEAN OUTCOMES BY MARKET FAMILY", report["settled_groups"]["market_family"], limit=20)

    print()
    print("ARB AND BET_NO TRUTH")
    print("-" * 94)
    print(f"  ARB rows:                         {_fmt_int(report['arb']['rows'])}")
    print(f"  ARB opened:                       {_fmt_int(report['arb']['opened'])}")
    print(f"  ARB verdict:                      {report['arb']['verdict']}")
    print(f"  ARB blockers:                     {report['arb']['reason_counts']}")
    print(f"  BET_NO rows:                      {_fmt_int(report['bet_no']['rows'])}")
    print(f"  BET_NO opened:                    {_fmt_int(report['bet_no']['opened'])}")
    print(f"  BET_NO verdict:                   {report['bet_no']['verdict']}")
    print(f"  BET_NO blockers:                  {report['bet_no']['reason_counts']}")

    print()
    print("POCKET VERDICT")
    print("-" * 94)
    print(f"  sample-supported positive pockets:{len(report['pockets']['sample_supported_positive'])}")
    print(f"  tiny positive traps:              {len(report['pockets']['tiny_positive_traps'])}")
    print(f"  pocket verdict:                   {report['pockets']['verdict']}")
    for key, stats in report["pockets"]["tiny_positive_traps"][:8]:
        print(
            f"  tiny trap {key}: n={stats['n']} pnl={_fmt_money(stats.get('total_economic_pnl'))} "
            f"roi={_fmt_pct(stats.get('roi'))}"
        )

    print()
    print("ROOT CAUSE VERDICT")
    print("-" * 94)
    print(f"  exact failure:                    {report['verdict']['current_failure']}")
    print(f"  scanner quality:                  {report['verdict']['scanner_quality']}")
    print(f"  model verdict:                    {report['verdict']['model']}")
    print(f"  live patch allowed:               {report['verdict']['live_patch_allowed']}")
    print(f"  next best fix:                    {report['verdict']['next_best_fix']}")

    print()
    print("SAFETY LOCKS")
    print("-" * 94)
    for key, value in report["safety"].items():
        print(f"  {key:<30} {value}")

    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> int:
    report = build_report()
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
