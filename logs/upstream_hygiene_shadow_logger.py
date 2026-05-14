"""
logs/upstream_hygiene_shadow_logger.py
--------------------------------------
Passive upstream hygiene shadow logger.

Records what the candidate stream would look like under conservative hygiene
filters. This is observability only and must not mutate opportunities, scanner
order, PaperTrader, council, risk, thresholds, or execution.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from config.trading_config import (
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    MIN_EDGE,
    MIN_VOLUME,
    QUARANTINED_TICKER_PREFIXES,
    TRADING_MODE,
)


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "upstream_hygiene_shadow.jsonl"
NOTE = "SHADOW_ONLY_NOT_EXECUTION"
WEAK_REWARD_RISK = 0.25
EXPENSIVE_LOW = 0.80
EXPENSIVE_HIGH = 0.90


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _action(row: Dict[str, Any]) -> str:
    return str(row.get("action") or row.get("scanner_action") or "UNKNOWN").upper()


def _ticker(row: Dict[str, Any]) -> str:
    return str(row.get("ticker") or "UNKNOWN")


def _entry_price(row: Dict[str, Any]) -> Optional[float]:
    explicit = _safe_float(row.get("entry_price"))
    if explicit is not None:
        return explicit
    action = _action(row)
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


def _reward_risk(price: Optional[float]) -> Optional[float]:
    if price is None or price <= 0:
        return None
    return (1.0 - price) / price


def _model_probability(row: Dict[str, Any]) -> Optional[float]:
    return _safe_float(row.get("model_probability") if row.get("model_probability") is not None else row.get("confidence"))


def _model_margin(row: Dict[str, Any]) -> Optional[float]:
    prob = _model_probability(row)
    price = _entry_price(row)
    if prob is None or price is None:
        return None
    return prob - price


def _edge(row: Dict[str, Any]) -> Optional[float]:
    return _safe_float(row.get("risk_edge") if row.get("risk_edge") is not None else row.get("edge"))


def _spread(row: Dict[str, Any]) -> Optional[float]:
    explicit = _safe_float(row.get("spread") if row.get("spread") is not None else row.get("market_spread"))
    if explicit is not None:
        return explicit
    bid = _safe_float(row.get("yes_bid"))
    ask = _safe_float(row.get("yes_ask"))
    if bid is not None and ask is not None:
        return max(0.0, ask - bid)
    return None


def _volume(row: Dict[str, Any]) -> Optional[float]:
    return _safe_float(row.get("volume") or row.get("volume_24h") or row.get("liquidity"))


def _is_quarantined(row: Dict[str, Any]) -> bool:
    ticker = _ticker(row).upper()
    return any(ticker.startswith(str(prefix).upper()) for prefix in QUARANTINED_TICKER_PREFIXES)


def _is_weak_rr(row: Dict[str, Any]) -> bool:
    rr = _reward_risk(_entry_price(row))
    return rr is not None and rr < WEAK_REWARD_RISK


def _is_expensive_80_90(row: Dict[str, Any]) -> bool:
    price = _entry_price(row)
    return price is not None and EXPENSIVE_LOW <= price < EXPENSIVE_HIGH


def _is_market_quality_weak(row: Dict[str, Any]) -> bool:
    spread = _spread(row)
    volume = _volume(row)
    return bool((spread is not None and spread > 0.05) or (volume is not None and volume < MIN_VOLUME))


def _is_min_edge_weak(row: Dict[str, Any]) -> bool:
    edge = _edge(row)
    return bool(edge is not None and edge < MIN_EDGE)


def _avg(values: list[Optional[float]]) -> Optional[float]:
    nums = [value for value in values if value is not None]
    return round(sum(nums) / len(nums), 8) if nums else None


def _rate(count: int, total: int) -> Optional[float]:
    return round(count / total, 8) if total else None


def _quality_score(rows: list[Dict[str, Any]]) -> Optional[float]:
    if not rows:
        return None
    n = len(rows)
    quarantine_rate = _rate(sum(1 for row in rows if _is_quarantined(row)), n) or 0.0
    market_quality_rate = _rate(sum(1 for row in rows if _is_market_quality_weak(row)), n) or 0.0
    min_edge_rate = _rate(sum(1 for row in rows if _is_min_edge_weak(row)), n) or 0.0
    expensive_rate = _rate(sum(1 for row in rows if _is_expensive_80_90(row)), n) or 0.0
    weak_rr_rate = _rate(sum(1 for row in rows if _is_weak_rr(row)), n) or 0.0
    avg_margin = _avg([_model_margin(row) for row in rows]) or 0.0
    avg_rr = _avg([_reward_risk(_entry_price(row)) for row in rows]) or 0.0
    score = 100.0
    score -= 25.0 * quarantine_rate
    score -= 25.0 * market_quality_rate
    score -= 20.0 * min_edge_rate
    score -= 15.0 * expensive_rate
    score -= 15.0 * weak_rr_rate
    score += min(10.0, max(-10.0, avg_margin * 100.0))
    score += min(10.0, avg_rr)
    return round(max(0.0, min(100.0, score)), 2)


def _starvation_risk(removed_count: int, before_count: int, after_count: int) -> str:
    removal_rate = _rate(removed_count, before_count) or 0.0
    if after_count == 0 or removal_rate >= 0.85:
        return "HIGH"
    if removal_rate >= 0.60:
        return "MEDIUM_HIGH"
    if removal_rate >= 0.40:
        return "MEDIUM"
    return "LOW"


def _classification(variant_name: str, removed_count: int, before_count: int, after_count: int, aggressive: bool = False) -> str:
    if aggressive:
        return "RESEARCH_ONLY_AGGRESSIVE"
    if variant_name == "current":
        return "BASELINE"
    if removed_count == 0:
        return "COSMETIC_ONLY"
    risk = _starvation_risk(removed_count, before_count, after_count)
    if risk == "HIGH":
        return "TOO_AGGRESSIVE_STARVES_SYSTEM"
    if variant_name == "stack1_quarantine_only" and risk == "LOW":
        return "SAFE_TO_SHADOW_TEST"
    return "PROMISING_BUT_NEEDS_MORE_DATA"


def _summarize_variant(name: str, before_count: int, rows: list[Dict[str, Any]], removed_count: int, aggressive: bool = False) -> Dict[str, Any]:
    entries = [_entry_price(row) for row in rows]
    rrs = [_reward_risk(price) for price in entries]
    margins = [_model_margin(row) for row in rows]
    count = len(rows)
    return {
        "name": name,
        "candidate_count": count,
        "removed_count": removed_count,
        "removal_rate": _rate(removed_count, before_count),
        "avg_entry": _avg(entries),
        "avg_reward_risk": _avg(rrs),
        "avg_model_margin": _avg(margins),
        "quality_score": _quality_score(rows),
        "quarantine_count": sum(1 for row in rows if _is_quarantined(row)),
        "weak_reward_risk_count": sum(1 for row in rows if _is_weak_rr(row)),
        "expensive_80_90_count": sum(1 for row in rows if _is_expensive_80_90(row)),
        "market_quality_estimate": sum(1 for row in rows if _is_market_quality_weak(row)),
        "min_edge_estimate": sum(1 for row in rows if _is_min_edge_weak(row)),
        "starvation_risk": _starvation_risk(removed_count, before_count, count),
        "safety_classification": _classification(name, removed_count, before_count, count, aggressive=aggressive),
        "safe_for_shadow_only_validation": not aggressive and _starvation_risk(removed_count, before_count, count) != "HIGH",
        "live_deployable": False,
        "shadow_only": True,
    }


def _non_pass_candidates(opportunities: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [row for row in opportunities if _action(row) != "PASS"]


def build_shadow_row(
    opportunities: Iterable[Dict[str, Any]],
    scan_id: Optional[str] = None,
    run_id: Optional[str] = None,
    timestamp_utc: Optional[str] = None,
) -> Dict[str, Any]:
    candidates = _non_pass_candidates(opportunities)
    before_count = len(candidates)
    stack1 = [row for row in candidates if not _is_quarantined(row)]
    weak_rr_variant = [row for row in candidates if not _is_weak_rr(row)]
    expensive_variant = [row for row in candidates if not _is_expensive_80_90(row)]
    aggressive = [
        row for row in candidates
        if not (_is_quarantined(row) or _is_weak_rr(row) or _is_expensive_80_90(row))
    ]
    current = _summarize_variant("current", before_count, candidates, 0)
    stack1_summary = _summarize_variant("stack1_quarantine_only", before_count, stack1, before_count - len(stack1))
    weak_summary = _summarize_variant("research_variant_weak_rr", before_count, weak_rr_variant, before_count - len(weak_rr_variant))
    expensive_summary = _summarize_variant("research_variant_expensive_entry", before_count, expensive_variant, before_count - len(expensive_variant))
    aggressive_summary = _summarize_variant("aggressive_stack_quarantine_weak_rr_expensive", before_count, aggressive, before_count - len(aggressive), aggressive=True)

    return {
        "timestamp_utc": timestamp_utc or datetime.now(timezone.utc).isoformat(),
        "run_id": run_id or "UNKNOWN_RUN",
        "scan_id": scan_id,
        "shadow_only": True,
        "execution_changed": False,
        "live_strategy_mutated": False,
        "live_deployable": False,
        "note": NOTE,
        "current_candidate_count": before_count,
        "current_blocker_estimates": {
            "quarantine": current["quarantine_count"],
            "weak_reward_risk": current["weak_reward_risk_count"],
            "expensive_80_90": current["expensive_80_90_count"],
            "market_quality": current["market_quality_estimate"],
            "min_edge": current["min_edge_estimate"],
        },
        "quarantine_exclusion_candidate_count": stack1_summary["candidate_count"],
        "quarantine_removed_count": before_count - len(stack1),
        "weak_reward_risk_removed_count": before_count - len(weak_rr_variant),
        "expensive_entry_removed_count": before_count - len(expensive_variant),
        "current_avg_entry": current["avg_entry"],
        "current_avg_reward_risk": current["avg_reward_risk"],
        "current_avg_model_margin": current["avg_model_margin"],
        "current_quality_score": current["quality_score"],
        "stack1_avg_entry": stack1_summary["avg_entry"],
        "stack1_avg_reward_risk": stack1_summary["avg_reward_risk"],
        "stack1_avg_model_margin": stack1_summary["avg_model_margin"],
        "stack1_quality_score": stack1_summary["quality_score"],
        "research_variant_weak_rr_quality_score": weak_summary["quality_score"],
        "research_variant_expensive_entry_quality_score": expensive_summary["quality_score"],
        "aggressive_stack_quality_score": aggressive_summary["quality_score"],
        "variants": {
            "current": current,
            "stack1_quarantine_only": stack1_summary,
            "research_variant_weak_rr": weak_summary,
            "research_variant_expensive_entry": expensive_summary,
            "aggressive_stack": aggressive_summary,
        },
        "starvation_risk": {
            "current": current["starvation_risk"],
            "stack1_quarantine_only": stack1_summary["starvation_risk"],
            "research_variant_weak_rr": weak_summary["starvation_risk"],
            "research_variant_expensive_entry": expensive_summary["starvation_risk"],
            "aggressive_stack": aggressive_summary["starvation_risk"],
        },
        "safety_classification": {
            "current": current["safety_classification"],
            "stack1_quarantine_only": stack1_summary["safety_classification"],
            "research_variant_weak_rr": weak_summary["safety_classification"],
            "research_variant_expensive_entry": expensive_summary["safety_classification"],
            "aggressive_stack": aggressive_summary["safety_classification"],
        },
        "safety_locks": {
            "trading_mode": TRADING_MODE,
            "real_money_allowed": False,
            "scale_allowed": False,
            "kelly_disabled": bool(GLOBAL_FORCED_LEARNING_MODE),
            "dc_override_enabled": bool(DATA_COLLECTION_OVERRIDE_ENABLED),
            "kxeth_quarantine_active": bool(QUARANTINED_TICKER_PREFIXES),
        },
    }


def log_upstream_hygiene_shadow(
    opportunities: Iterable[Dict[str, Any]],
    scan_id: Optional[str] = None,
    run_id: Optional[str] = None,
    path: Path = LOG_PATH,
) -> Dict[str, Any]:
    """Append one scan-level upstream hygiene shadow row. Fail-soft by design."""
    try:
        row = build_shadow_row(opportunities, scan_id=scan_id, run_id=run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as exc:
        return {"written": 0, "errors": 1, "error": exc.__class__.__name__, "execution_changed": False}
    return {
        "written": 1,
        "errors": 0,
        "path": str(path),
        "candidate_count": row.get("current_candidate_count", 0),
        "quarantine_removed_count": row.get("quarantine_removed_count", 0),
        "weak_reward_risk_removed_count": row.get("weak_reward_risk_removed_count", 0),
        "expensive_entry_removed_count": row.get("expensive_entry_removed_count", 0),
        "stack1_quality_score": row.get("stack1_quality_score"),
        "execution_changed": False,
        "live_strategy_mutated": False,
    }
