#!/usr/bin/env python3
"""
Phase 10N - Upstream Candidate Quality Autopsy
Sentinel: UPSTREAM_CANDIDATE_QUALITY_AUTOPSY_OK

Read-only audit of post-shadow candidate quality before PaperTrader opens.
It groups valid blockers by market/source and payoff geometry, then proposes
simulation-only upstream filter ideas without changing scanner, strategy,
thresholds, PaperTrader, risk, logs, or live-money state.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (  # noqa: E402
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    MAX_SPREAD,
    MIN_EDGE,
    MIN_VOLUME,
    QUARANTINED_TICKER_PREFIXES,
    TRADING_MODE,
)
from tools.report_candidate_to_paper_forward_blockers import (  # noqa: E402
    FUNNEL_LOG,
    SHADOW_LOG,
    SHADOW_START,
    SHADOW_START_TEXT,
    STAGE_NAMES,
    classify_stage,
)

SENTINEL = "UPSTREAM_CANDIDATE_QUALITY_AUTOPSY_OK"

ENTRY_BUCKETS = ("missing", "<0.50", "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00")
CONFIDENCE_BUCKETS = ("missing", "<0.65", "0.65-0.70", "0.70-0.75", "0.75-0.80", "0.80-0.90", "0.90+")
EDGE_BUCKETS = ("missing", "<0.00", "0.00-0.03", "0.03-0.05", "0.05-0.08", "0.08+")
RR_BUCKETS = ("missing", "<0.15", "0.15-0.25", "0.25-0.50", "0.50-1.00", "1.00+")
SPREAD_BUCKETS = ("missing", "0.00-0.01", "0.01-0.03", "0.03-0.05", "0.05-0.10", "0.10+")
VOLUME_BUCKETS = ("missing", "<100", "100-1k", "1k-10k", "10k+")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _row_ts(row: dict[str, Any]) -> datetime | None:
    return _parse_ts(row.get("timestamp_utc") or row.get("timestamp"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def rows_after(rows: list[dict[str, Any]], start: datetime = SHADOW_START) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        ts = _row_ts(row)
        if ts is not None and ts >= start:
            selected.append(row)
    return selected


def _safe_float(value: Any) -> float | None:
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


def _rate(count: int, total: int) -> float | None:
    return count / total if total else None


def _avg(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def _reason(row: dict[str, Any]) -> str:
    return str(row.get("final_reason") or row.get("reason") or "UNKNOWN")


def is_opened(row: dict[str, Any]) -> bool:
    return bool(row.get("paper_trade_opened")) or _reason(row).upper() == "TRADE_OPENED"


def action_of(row: dict[str, Any]) -> str:
    return str(row.get("scanner_action") or row.get("intended_action") or row.get("action") or "UNKNOWN").upper()


def ticker_of(row: dict[str, Any]) -> str:
    return str(row.get("ticker") or "UNKNOWN")


def ticker_prefix(ticker: Any) -> str:
    text = str(ticker or "UNKNOWN")
    return text.split("-", 1)[0] if "-" in text else text


def is_quarantined(row: dict[str, Any]) -> bool:
    ticker = ticker_of(row).upper()
    return any(ticker.startswith(str(prefix).upper()) for prefix in QUARANTINED_TICKER_PREFIXES)


def side_entry_price(row: dict[str, Any]) -> float | None:
    explicit = _safe_float(row.get("entry_price"))
    if explicit is not None:
        return explicit
    action = action_of(row)
    if action == "BET_NO":
        return _safe_float(row.get("no_ask") if row.get("no_ask") is not None else row.get("price_no"))
    if action == "BET_YES":
        return _safe_float(row.get("yes_ask") if row.get("yes_ask") is not None else row.get("price_yes"))
    if action == "ARB":
        yes_ask = _safe_float(row.get("yes_ask") if row.get("yes_ask") is not None else row.get("price_yes"))
        no_ask = _safe_float(row.get("no_ask") if row.get("no_ask") is not None else row.get("price_no"))
        if yes_ask is not None and no_ask is not None:
            return min(yes_ask, no_ask)
    return None


def reward_risk(price: float | None) -> float | None:
    if price is None or price <= 0:
        return None
    return (1.0 - price) / price


def model_probability(row: dict[str, Any]) -> float | None:
    return _safe_float(row.get("model_probability") if row.get("model_probability") is not None else row.get("confidence"))


def edge_value(row: dict[str, Any]) -> float | None:
    return _safe_float(row.get("risk_edge") if row.get("risk_edge") is not None else row.get("edge"))


def model_margin(row: dict[str, Any]) -> float | None:
    prob = model_probability(row)
    price = side_entry_price(row)
    if prob is None or price is None:
        return None
    return prob - price


def spread_value(row: dict[str, Any]) -> float | None:
    explicit = _safe_float(row.get("spread") if row.get("spread") is not None else row.get("market_spread"))
    if explicit is not None:
        return explicit
    yes_bid = _safe_float(row.get("yes_bid"))
    yes_ask = _safe_float(row.get("yes_ask"))
    if yes_bid is not None and yes_ask is not None:
        return max(0.0, yes_ask - yes_bid)
    return None


def volume_value(row: dict[str, Any]) -> float | None:
    trace = str(row.get("trace_excerpt") or "")
    explicit = _safe_float(row.get("volume") or row.get("volume_24h") or row.get("liquidity"))
    if explicit is not None:
        return explicit
    marker = "volume="
    if marker in trace:
        tail = trace.split(marker, 1)[1]
        raw = tail.split()[0].split(",")[0].split(")")[0]
        return _safe_float(raw)
    return None


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


def confidence_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.65:
        return "<0.65"
    if value < 0.70:
        return "0.65-0.70"
    if value < 0.75:
        return "0.70-0.75"
    if value < 0.80:
        return "0.75-0.80"
    if value < 0.90:
        return "0.80-0.90"
    return "0.90+"


def edge_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.00:
        return "<0.00"
    if value < MIN_EDGE:
        return "0.00-0.03"
    if value < 0.05:
        return "0.03-0.05"
    if value < 0.08:
        return "0.05-0.08"
    return "0.08+"


def reward_risk_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.15:
        return "<0.15"
    if value < 0.25:
        return "0.15-0.25"
    if value < 0.50:
        return "0.25-0.50"
    if value < 1.00:
        return "0.50-1.00"
    return "1.00+"


def spread_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value <= 0.01:
        return "0.00-0.01"
    if value <= 0.03:
        return "0.01-0.03"
    if value <= MAX_SPREAD:
        return "0.03-0.05"
    if value < 0.10:
        return "0.05-0.10"
    return "0.10+"


def volume_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 100:
        return "<100"
    if value < MIN_VOLUME:
        return "100-1k"
    if value < 10_000:
        return "1k-10k"
    return "10k+"


def _counter_table(rows: list[dict[str, Any]], key_func) -> dict[str, int]:
    return dict(Counter(key_func(row) for row in rows).most_common())


def _bucket_reason_profile(rows: list[dict[str, Any]], bucket_func) -> dict[str, dict[str, int]]:
    profile: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        profile[bucket_func(row)][_reason(row)] += 1
    return {bucket: dict(counter.most_common()) for bucket, counter in sorted(profile.items())}


def _source_summary(rows: list[dict[str, Any]], group_func, limit: int = 20, reverse: bool = True) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[group_func(row)].append(row)
    summaries = []
    for group, items in grouped.items():
        n = len(items)
        market_quality = sum(1 for row in items if classify_stage(row) == "market_quality")
        edge_blocks = sum(1 for row in items if classify_stage(row) == "edge_threshold")
        quarantine = sum(1 for row in items if is_quarantined(row))
        council = sum(1 for row in items if classify_stage(row) == "council")
        opened = sum(1 for row in items if is_opened(row))
        entries = [side_entry_price(row) for row in items]
        rrs = [reward_risk(price) for price in entries]
        margins = [model_margin(row) for row in items]
        quality_score = _candidate_quality_score(items)
        summaries.append({
            "source": group,
            "n": n,
            "opened": opened,
            "blocked": n - opened,
            "market_quality_blocks": market_quality,
            "edge_blocks": edge_blocks,
            "council_blocks": council,
            "quarantine_count": quarantine,
            "avg_entry": _avg(entries),
            "avg_reward_risk": _avg(rrs),
            "avg_model_margin": _avg(margins),
            "quality_score": quality_score,
            "top_reasons": dict(Counter(_reason(row) for row in items).most_common(5)),
        })
    summaries.sort(key=lambda row: (row["n"], -row["quality_score"] if row["quality_score"] is not None else 0.0), reverse=reverse)
    return summaries[:limit]


def _promising_sources(rows: list[dict[str, Any]], group_func, limit: int = 20) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[group_func(row)].append(row)
    summaries = []
    for group, items in grouped.items():
        if len(items) < 10:
            continue
        score = _candidate_quality_score(items)
        if score is None:
            continue
        quarantine_rate = _rate(sum(1 for row in items if is_quarantined(row)), len(items)) or 0.0
        market_quality_rate = _rate(sum(1 for row in items if classify_stage(row) == "market_quality"), len(items)) or 0.0
        summaries.append({
            "source": group,
            "n": len(items),
            "quality_score": score,
            "avg_entry": _avg([side_entry_price(row) for row in items]),
            "avg_reward_risk": _avg([reward_risk(side_entry_price(row)) for row in items]),
            "avg_model_margin": _avg([model_margin(row) for row in items]),
            "quarantine_rate": quarantine_rate,
            "market_quality_block_rate": market_quality_rate,
            "top_reasons": dict(Counter(_reason(row) for row in items).most_common(3)),
            "proof_note": "CANDIDATE_QUALITY_ONLY_NOT_PROFIT_PROOF",
        })
    summaries.sort(key=lambda row: (row["quality_score"], -row["quarantine_rate"], -row["market_quality_block_rate"]), reverse=True)
    return summaries[:limit]


def _candidate_quality_score(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    n = len(rows)
    market_quality_rate = _rate(sum(1 for row in rows if classify_stage(row) == "market_quality"), n) or 0.0
    quarantine_rate = _rate(sum(1 for row in rows if is_quarantined(row)), n) or 0.0
    edge_block_rate = _rate(sum(1 for row in rows if _reason(row) == "BLOCKED_MIN_EDGE"), n) or 0.0
    expensive_rate = _rate(sum(1 for row in rows if (side_entry_price(row) or 0.0) >= 0.80), n) or 0.0
    weak_rr_rate = _rate(sum(1 for row in rows if (reward_risk(side_entry_price(row)) or 999.0) < 0.25), n) or 0.0
    avg_margin = _avg([model_margin(row) for row in rows]) or 0.0
    avg_rr = _avg([reward_risk(side_entry_price(row)) for row in rows]) or 0.0
    score = 100.0
    score -= 25.0 * market_quality_rate
    score -= 25.0 * quarantine_rate
    score -= 20.0 * edge_block_rate
    score -= 15.0 * expensive_rate
    score -= 15.0 * weak_rr_rate
    score += min(10.0, max(-10.0, avg_margin * 100.0))
    score += min(10.0, avg_rr)
    return round(max(0.0, min(100.0, score)), 2)


def _strict_shadow_summary(shadow_rows: list[dict[str, Any]]) -> dict[str, Any]:
    current = payoff = strict = 0
    current_expensive = payoff_expensive = strict_expensive = 0
    current_weak = payoff_weak = strict_weak = 0
    strict_starved = 0
    for row in shadow_rows:
        c = row.get("current_top_3") or []
        p = row.get("payoff_aware_top_3") or []
        s = row.get("strict_payoff_top_3") or []
        if isinstance(c, list):
            current += len(c)
            current_expensive += sum(1 for item in c if isinstance(item, dict) and item.get("expensive_entry"))
            current_weak += sum(1 for item in c if isinstance(item, dict) and item.get("weak_reward_risk"))
        if isinstance(p, list):
            payoff += len(p)
            payoff_expensive += sum(1 for item in p if isinstance(item, dict) and item.get("expensive_entry"))
            payoff_weak += sum(1 for item in p if isinstance(item, dict) and item.get("weak_reward_risk"))
        if isinstance(s, list):
            strict += len(s)
            strict_expensive += sum(1 for item in s if isinstance(item, dict) and item.get("expensive_entry"))
            strict_weak += sum(1 for item in s if isinstance(item, dict) and item.get("weak_reward_risk"))
            strict_starved += max(0, 3 - len(s))
    return {
        "current_picks": current,
        "payoff_aware_picks": payoff,
        "strict_picks": strict,
        "current_expensive_rate": _rate(current_expensive, current),
        "payoff_aware_expensive_rate": _rate(payoff_expensive, payoff),
        "strict_expensive_rate": _rate(strict_expensive, strict),
        "current_weak_rr_rate": _rate(current_weak, current),
        "payoff_aware_weak_rr_rate": _rate(payoff_weak, payoff),
        "strict_weak_rr_rate": _rate(strict_weak, strict),
        "strict_starvation_slots": strict_starved,
        "strict_starvation_rate": _rate(strict_starved, current if current else strict + strict_starved),
    }


def classify_upstream_bottleneck(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "UNKNOWN_UPSTREAM_QUALITY_PROBLEM"
    n = len(rows)
    stages = Counter(classify_stage(row) for row in rows if not is_opened(row))
    quarantine_rate = _rate(sum(1 for row in rows if is_quarantined(row)), n) or 0.0
    market_rate = _rate(stages.get("market_quality", 0), n) or 0.0
    edge_rate = _rate(stages.get("edge_threshold", 0), n) or 0.0
    council_rate = _rate(stages.get("council", 0), n) or 0.0
    expensive_rate = _rate(sum(1 for row in rows if (side_entry_price(row) or 0.0) >= 0.80), n) or 0.0
    weak_rr_rate = _rate(sum(1 for row in rows if (reward_risk(side_entry_price(row)) or 999.0) < 0.25), n) or 0.0
    high_conf_edge_blocks = sum(
        1 for row in rows
        if classify_stage(row) == "edge_threshold"
        and (model_probability(row) or 0.0) >= 0.80
    )

    if quarantine_rate >= 0.50:
        return "QUARANTINED_MARKETS_STILL_ENTERING_STREAM"
    if market_rate >= 0.50:
        return "MARKET_QUALITY_UNIVERSE_TOO_WEAK"
    if edge_rate >= 0.50:
        return "EDGE_TOO_WEAK_BEFORE_PAPERTRADER"
    if council_rate >= 0.50:
        return "COUNCIL_REJECTING_TOXIC_BUCKETS"
    if expensive_rate >= 0.50:
        return "TOO_MANY_EXPENSIVE_ENTRIES"
    if weak_rr_rate >= 0.50:
        return "PAYOFF_GEOMETRY_TOO_WEAK"
    if high_conf_edge_blocks >= max(20, int(n * 0.20)):
        return "CONFIDENCE_OVERSTATED_PRE_GATE"
    if all(count == 0 for count in stages.values()):
        return "HEALTHY_STRICT_GATES_NEED_MORE_TIME"
    return "MIXED_CANDIDATE_QUALITY_PROBLEM"


def _filter_ideas(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    n = len(rows)

    def count_if(predicate) -> int:
        return sum(1 for row in rows if predicate(row))

    repeated_market_quality_prefixes = {
        prefix for prefix, count in Counter(ticker_prefix(ticker_of(row)) for row in rows if classify_stage(row) == "market_quality").items()
        if count >= 20
    }
    ideas = [
        {
            "filter_name": "pre_rank_quarantine_exclusion",
            "condition": "ticker starts with any configured quarantined prefix before ranking",
            "expected_effect": "stop spending candidate slots and PaperTrader calls on hard-quarantined markets",
            "candidates_removed": count_if(is_quarantined),
            "blocker_reduction_estimate": count_if(is_quarantined),
            "risk_of_overfitting": "LOW",
            "preserves_safety": True,
            "requires_more_proof": False,
            "simulation_only": True,
        },
        {
            "filter_name": "market_quality_repeat_deprioritization",
            "condition": "prefix has >=20 post-shadow market-quality blocks; rank behind cleaner prefixes until spread/volume improves",
            "expected_effect": "reduce repeated low-liquidity/high-spread candidate churn",
            "candidates_removed": count_if(lambda row: ticker_prefix(ticker_of(row)) in repeated_market_quality_prefixes),
            "blocker_reduction_estimate": count_if(lambda row: classify_stage(row) == "market_quality" and ticker_prefix(ticker_of(row)) in repeated_market_quality_prefixes),
            "risk_of_overfitting": "MEDIUM",
            "preserves_safety": True,
            "requires_more_proof": True,
            "simulation_only": True,
        },
        {
            "filter_name": "expensive_entry_research_gate",
            "condition": "entry_price in 0.80-0.90 requires extra model_margin and clean bucket proof",
            "expected_effect": "reduce high-WR/high-cost contracts with bad payoff asymmetry",
            "candidates_removed": count_if(lambda row: 0.80 <= (side_entry_price(row) or -1.0) < 0.90),
            "blocker_reduction_estimate": count_if(lambda row: 0.80 <= (side_entry_price(row) or -1.0) < 0.90 and not is_opened(row)),
            "risk_of_overfitting": "MEDIUM",
            "preserves_safety": True,
            "requires_more_proof": True,
            "simulation_only": True,
        },
        {
            "filter_name": "weak_reward_risk_pre_rank_penalty",
            "condition": "reward_risk < 0.25 is ranked behind candidates with positive model_margin and better payoff",
            "expected_effect": "reduce payoff-asymmetry drag before PaperTrader gates",
            "candidates_removed": count_if(lambda row: (reward_risk(side_entry_price(row)) or 999.0) < 0.25),
            "blocker_reduction_estimate": count_if(lambda row: (reward_risk(side_entry_price(row)) or 999.0) < 0.25 and not is_opened(row)),
            "risk_of_overfitting": "MEDIUM",
            "preserves_safety": True,
            "requires_more_proof": True,
            "simulation_only": True,
        },
        {
            "filter_name": "entry_price_conditioned_edge_floor",
            "condition": "higher entry_price requires proportionally stronger model_margin; reported edge alone cannot rescue bad geometry",
            "expected_effect": "force expensive contracts to clear a payoff-aware evidence hurdle",
            "candidates_removed": count_if(lambda row: (side_entry_price(row) or 0.0) >= 0.70 and (model_margin(row) or -1.0) < 0.05),
            "blocker_reduction_estimate": count_if(lambda row: (side_entry_price(row) or 0.0) >= 0.70 and (model_margin(row) or -1.0) < 0.05),
            "risk_of_overfitting": "MEDIUM",
            "preserves_safety": True,
            "requires_more_proof": True,
            "simulation_only": True,
        },
        {
            "filter_name": "side_specific_quality_profiles",
            "condition": "BET_YES and BET_NO maintain separate spread/entry/reward-risk blocker profiles before rank comparison",
            "expected_effect": "avoid treating YES and NO as interchangeable when their payoff and blocker profiles differ",
            "candidates_removed": 0,
            "blocker_reduction_estimate": 0,
            "risk_of_overfitting": "LOW",
            "preserves_safety": True,
            "requires_more_proof": True,
            "simulation_only": True,
        },
    ]
    for idea in ideas:
        idea["candidate_removal_rate"] = _rate(int(idea["candidates_removed"]), n)
    return ideas


def build_report(
    funnel_path: Path = FUNNEL_LOG,
    shadow_path: Path = SHADOW_LOG,
    shadow_start: datetime = SHADOW_START,
) -> dict[str, Any]:
    funnel_rows = rows_after(read_jsonl(funnel_path), shadow_start)
    shadow_rows = rows_after(read_jsonl(shadow_path), shadow_start)
    blocked_rows = [row for row in funnel_rows if not is_opened(row)]
    opened_rows = [row for row in funnel_rows if is_opened(row)]
    stage_counts = Counter({stage: 0 for stage in STAGE_NAMES})
    for row in blocked_rows:
        stage_counts[classify_stage(row)] += 1

    market_quality_rows = [row for row in blocked_rows if classify_stage(row) == "market_quality"]
    council_rows = [row for row in blocked_rows if classify_stage(row) == "council"]
    min_edge_rows = [row for row in blocked_rows if _reason(row) == "BLOCKED_MIN_EDGE"]
    edge_danger_rows = [row for row in blocked_rows if _reason(row) == "BLOCKED_EDGE_DANGER_GUARD"]

    return {
        "shadow_start": shadow_start.isoformat(),
        "total_funnel_events_after_start": len(funnel_rows),
        "total_blocked_events": len(blocked_rows),
        "total_opened_events": len(opened_rows),
        "blocker_counts_by_reason": dict(Counter(_reason(row) for row in blocked_rows).most_common()),
        "blocker_counts_by_stage": dict(stage_counts),
        "blocker_counts_by_ticker_prefix": _counter_table(blocked_rows, lambda row: ticker_prefix(ticker_of(row))),
        "blocker_counts_by_full_ticker": _counter_table(blocked_rows, ticker_of),
        "blocker_counts_by_action": _counter_table(blocked_rows, action_of),
        "blocker_counts_by_entry_price_bucket": _counter_table(blocked_rows, lambda row: entry_bucket(side_entry_price(row))),
        "blocker_counts_by_confidence_bucket": _counter_table(blocked_rows, lambda row: confidence_bucket(model_probability(row))),
        "blocker_counts_by_edge_bucket": _counter_table(blocked_rows, lambda row: edge_bucket(edge_value(row))),
        "blocker_counts_by_reward_risk_bucket": _counter_table(blocked_rows, lambda row: reward_risk_bucket(reward_risk(side_entry_price(row)))),
        "blocker_counts_by_spread_bucket": _counter_table(blocked_rows, lambda row: spread_bucket(spread_value(row))),
        "blocker_counts_by_volume_bucket": _counter_table(blocked_rows, lambda row: volume_bucket(volume_value(row))),
        "kxeth_quarantined_candidate_count": sum(1 for row in funnel_rows if is_quarantined(row)),
        "non_quarantined_candidate_count": sum(1 for row in funnel_rows if not is_quarantined(row)),
        "council_blocked_bucket_profile": {
            "entry_price": _bucket_reason_profile(council_rows, lambda row: entry_bucket(side_entry_price(row))),
            "confidence": _bucket_reason_profile(council_rows, lambda row: confidence_bucket(model_probability(row))),
            "edge": _bucket_reason_profile(council_rows, lambda row: edge_bucket(edge_value(row))),
            "reward_risk": _bucket_reason_profile(council_rows, lambda row: reward_risk_bucket(reward_risk(side_entry_price(row)))),
        },
        "market_quality_blocked_bucket_profile": {
            "spread": _bucket_reason_profile(market_quality_rows, lambda row: spread_bucket(spread_value(row))),
            "volume": _bucket_reason_profile(market_quality_rows, lambda row: volume_bucket(volume_value(row))),
            "ticker_prefix": _counter_table(market_quality_rows, lambda row: ticker_prefix(ticker_of(row))),
        },
        "min_edge_blocked_bucket_profile": {
            "entry_price": _bucket_reason_profile(min_edge_rows, lambda row: entry_bucket(side_entry_price(row))),
            "confidence": _bucket_reason_profile(min_edge_rows, lambda row: confidence_bucket(model_probability(row))),
            "edge": _bucket_reason_profile(min_edge_rows, lambda row: edge_bucket(edge_value(row))),
        },
        "edge_danger_blocked_bucket_profile": {
            "entry_price": _bucket_reason_profile(edge_danger_rows, lambda row: entry_bucket(side_entry_price(row))),
            "confidence": _bucket_reason_profile(edge_danger_rows, lambda row: confidence_bucket(model_probability(row))),
            "edge": _bucket_reason_profile(edge_danger_rows, lambda row: edge_bucket(edge_value(row))),
        },
        "candidate_stream_quality_score": _candidate_quality_score(funnel_rows),
        "top_20_worst_candidate_sources": _source_summary(blocked_rows, lambda row: ticker_prefix(ticker_of(row)), limit=20),
        "top_20_most_promising_candidate_sources": _promising_sources(blocked_rows, lambda row: ticker_prefix(ticker_of(row)), limit=20),
        "scanner_wasting_slots_on_untradeable_candidates": bool(funnel_rows and (
            _rate(sum(1 for row in funnel_rows if is_quarantined(row) or classify_stage(row) == "market_quality"), len(funnel_rows)) or 0.0
        ) >= 0.35),
        "payoff_aware_shadow_geometry": _strict_shadow_summary(shadow_rows),
        "main_upstream_bottleneck_label": classify_upstream_bottleneck(funnel_rows),
        "candidate_filter_ideas": _filter_ideas(funnel_rows),
        "safety": {
            "trading_mode": TRADING_MODE,
            "real_money_allowed": False,
            "scale_allowed": False,
            "kelly_disabled": bool(GLOBAL_FORCED_LEARNING_MODE),
            "dc_override_enabled": bool(DATA_COLLECTION_OVERRIDE_ENABLED),
            "kxeth_quarantine_active": bool(QUARANTINED_TICKER_PREFIXES),
            "live_strategy_mutated": False,
        },
    }


def _print_counter(title: str, counter: dict[str, int], limit: int | None = None) -> None:
    print(title)
    items = list(counter.items()) if limit is None else list(counter.items())[:limit]
    if not items:
        print("  NONE")
        return
    total = sum(counter.values())
    for key, count in items:
        print(f"  {key}: {_fmt_int(count)} ({_fmt_pct(_rate(count, total))})")


def print_report(report: dict[str, Any]) -> None:
    print("=== Upstream Candidate Quality Autopsy (Phase 10N) ===")
    print(f"shadow_start: {report['shadow_start']}")
    print(f"total_funnel_events_after_start: {_fmt_int(report['total_funnel_events_after_start'])}")
    print(f"total_blocked_events: {_fmt_int(report['total_blocked_events'])}")
    print(f"total_opened_events: {_fmt_int(report['total_opened_events'])}")
    print(f"candidate_stream_quality_score: {_fmt_num(report['candidate_stream_quality_score'], 2)} / 100")
    print(f"kxeth_quarantined_candidate_count: {_fmt_int(report['kxeth_quarantined_candidate_count'])}")
    print(f"non_quarantined_candidate_count: {_fmt_int(report['non_quarantined_candidate_count'])}")
    print(f"scanner_wasting_slots_on_untradeable_candidates: {report['scanner_wasting_slots_on_untradeable_candidates']}")
    print(f"main_upstream_bottleneck_label: {report['main_upstream_bottleneck_label']}")

    _print_counter("\nBlocker counts by reason:", report["blocker_counts_by_reason"])
    _print_counter("\nBlocker counts by stage:", report["blocker_counts_by_stage"])
    _print_counter("\nBlocker counts by ticker prefix:", report["blocker_counts_by_ticker_prefix"], 20)
    _print_counter("\nBlocker counts by full ticker:", report["blocker_counts_by_full_ticker"], 20)
    _print_counter("\nBlocker counts by action:", report["blocker_counts_by_action"])
    _print_counter("\nBlocker counts by entry price bucket:", report["blocker_counts_by_entry_price_bucket"])
    _print_counter("\nBlocker counts by confidence bucket:", report["blocker_counts_by_confidence_bucket"])
    _print_counter("\nBlocker counts by edge bucket:", report["blocker_counts_by_edge_bucket"])
    _print_counter("\nBlocker counts by reward/risk bucket:", report["blocker_counts_by_reward_risk_bucket"])
    _print_counter("\nBlocker counts by spread bucket:", report["blocker_counts_by_spread_bucket"])
    _print_counter("\nBlocker counts by volume bucket:", report["blocker_counts_by_volume_bucket"])

    print("\nBlocked bucket profiles:")
    print(f"  council_by_edge: {report['council_blocked_bucket_profile']['edge']}")
    print(f"  market_quality_by_spread: {report['market_quality_blocked_bucket_profile']['spread']}")
    print(f"  market_quality_by_volume: {report['market_quality_blocked_bucket_profile']['volume']}")
    print(f"  min_edge_by_edge: {report['min_edge_blocked_bucket_profile']['edge']}")
    print(f"  edge_danger_by_edge: {report['edge_danger_blocked_bucket_profile']['edge']}")

    print("\nPayoff-aware shadow geometry:")
    shadow = report["payoff_aware_shadow_geometry"]
    print(f"  current_picks={_fmt_int(shadow['current_picks'])} payoff_aware_picks={_fmt_int(shadow['payoff_aware_picks'])} strict_picks={_fmt_int(shadow['strict_picks'])}")
    print(f"  current_expensive_rate={_fmt_pct(shadow['current_expensive_rate'])} payoff_expensive_rate={_fmt_pct(shadow['payoff_aware_expensive_rate'])} strict_expensive_rate={_fmt_pct(shadow['strict_expensive_rate'])}")
    print(f"  current_weak_rr_rate={_fmt_pct(shadow['current_weak_rr_rate'])} payoff_weak_rr_rate={_fmt_pct(shadow['payoff_aware_weak_rr_rate'])} strict_weak_rr_rate={_fmt_pct(shadow['strict_weak_rr_rate'])}")
    print(f"  strict_starvation_slots={_fmt_int(shadow['strict_starvation_slots'])} strict_starvation_rate={_fmt_pct(shadow['strict_starvation_rate'])}")

    print("\nTop 20 worst candidate sources:")
    for row in report["top_20_worst_candidate_sources"]:
        print(
            f"  {row['source']}: n={_fmt_int(row['n'])} quality={_fmt_num(row['quality_score'], 2)} "
            f"mq={_fmt_int(row['market_quality_blocks'])} edge={_fmt_int(row['edge_blocks'])} "
            f"council={_fmt_int(row['council_blocks'])} quarantine={_fmt_int(row['quarantine_count'])} "
            f"avg_entry={_fmt_num(row['avg_entry'])} rr={_fmt_num(row['avg_reward_risk'])} reasons={row['top_reasons']}"
        )

    print("\nTop 20 most promising candidate-quality sources (not profit proof):")
    if report["top_20_most_promising_candidate_sources"]:
        for row in report["top_20_most_promising_candidate_sources"]:
            print(
                f"  {row['source']}: n={_fmt_int(row['n'])} quality={_fmt_num(row['quality_score'], 2)} "
                f"avg_entry={_fmt_num(row['avg_entry'])} rr={_fmt_num(row['avg_reward_risk'])} "
                f"margin={_fmt_num(row['avg_model_margin'])} note={row['proof_note']}"
            )
    else:
        print("  NONE")

    print("\nSimulation-only candidate filter ideas:")
    for idea in report["candidate_filter_ideas"]:
        print(
            f"  {idea['filter_name']}: condition={idea['condition']} removed={_fmt_int(idea['candidates_removed'])} "
            f"reduction_est={_fmt_int(idea['blocker_reduction_estimate'])} overfit={idea['risk_of_overfitting']} "
            f"preserves_safety={idea['preserves_safety']} simulation_only={idea['simulation_only']}"
        )

    print("\nSafety locks:")
    for key, value in report["safety"].items():
        print(f"  {key}: {value}")
    print(SENTINEL)


def main() -> None:
    print_report(build_report())


if __name__ == "__main__":
    main()
