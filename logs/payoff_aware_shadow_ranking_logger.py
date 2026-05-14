"""
logs/payoff_aware_shadow_ranking_logger.py
------------------------------------------
Passive payoff-aware ranking shadow logger.

This module records what the current scanner order selected versus what
payoff-aware ranking would have selected. It is observability only and must not
mutate opportunities, scanner order, thresholds, PaperTrader, or risk state.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "payoff_aware_shadow_ranking.jsonl"
MAX_SHADOW_PICKS = 3
EXPENSIVE_ENTRY = 0.80
WEAK_REWARD_RISK = 0.25
EXTREME_MARGIN = 0.10
EXTREME_CONFIDENCE = 0.90
STRICT_WEAK_RR_MARGIN = 0.12
STRICT_WEAK_RR_CONFIDENCE = 0.92


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


def payoff_score(row: Dict[str, Any]) -> Optional[float]:
    margin = _model_margin(row)
    price = _entry_price(row)
    rr = _reward_risk(price)
    edge = _edge(row) or 0.0
    if margin is None or price is None or rr is None:
        return None
    score = margin * min(rr, 5.0)
    score += 0.03 * edge
    if price >= EXPENSIVE_ENTRY:
        score -= 0.25
    if rr < WEAK_REWARD_RISK:
        score -= 0.20
    if margin <= 0:
        score -= 0.50
    return round(score, 8)


def strict_payoff_allowed(row: Dict[str, Any]) -> bool:
    price = _entry_price(row)
    rr = _reward_risk(price)
    margin = _model_margin(row)
    prob = _model_probability(row)
    if price is None or rr is None or margin is None or prob is None:
        return False
    if margin <= 0:
        return False
    if price >= EXPENSIVE_ENTRY and not (margin >= EXTREME_MARGIN and prob >= EXTREME_CONFIDENCE):
        return False
    if rr < WEAK_REWARD_RISK and not (margin >= STRICT_WEAK_RR_MARGIN and prob >= STRICT_WEAK_RR_CONFIDENCE):
        return False
    return True


def _is_expensive(row: Dict[str, Any]) -> bool:
    price = _entry_price(row)
    return price is not None and price >= EXPENSIVE_ENTRY


def _is_weak_rr(row: Dict[str, Any]) -> bool:
    rr = _reward_risk(_entry_price(row))
    return rr is not None and rr < WEAK_REWARD_RISK


def _is_toxic_80_90(row: Dict[str, Any]) -> bool:
    price = _entry_price(row)
    return price is not None and 0.80 <= price < 0.90


def _is_bad_geometry(row: Dict[str, Any]) -> bool:
    edge = _edge(row)
    return bool(edge is not None and edge >= 0.05 and (_is_expensive(row) or _is_weak_rr(row) or (_model_margin(row) or 0.0) <= 0))


def _non_pass_ranked(opportunities: Iterable[Dict[str, Any]]) -> list[tuple[int, Dict[str, Any]]]:
    ranked: list[tuple[int, Dict[str, Any]]] = []
    rank = 0
    for opportunity in opportunities:
        if _action(opportunity) == "PASS":
            continue
        rank += 1
        ranked.append((rank, opportunity))
    return ranked


def _pick_payload(rank: int, row: Dict[str, Any], score: Optional[float]) -> Dict[str, Any]:
    price = _entry_price(row)
    rr = _reward_risk(price)
    return {
        "ticker": row.get("ticker"),
        "action": _action(row),
        "rank": rank,
        "score": score,
        "entry_price": price,
        "reward_risk": rr,
        "model_margin": _model_margin(row),
        "confidence": _model_probability(row),
        "edge": _edge(row),
        "expensive_entry": _is_expensive(row),
        "weak_reward_risk": _is_weak_rr(row),
        "toxic_80_90": _is_toxic_80_90(row),
        "model_edge_bad_geometry": _is_bad_geometry(row),
    }


def _summarize(picks: list[Dict[str, Any]], target_count: int = MAX_SHADOW_PICKS) -> Dict[str, Any]:
    n = len(picks)

    def avg(field: str) -> Optional[float]:
        values = [_safe_float(row.get(field)) for row in picks]
        values = [value for value in values if value is not None]
        return round(sum(values) / len(values), 8) if values else None

    return {
        "count": n,
        "target_count": target_count,
        "starvation_count": max(0, target_count - n),
        "avg_entry": avg("entry_price"),
        "avg_reward_risk": avg("reward_risk"),
        "avg_model_margin": avg("model_margin"),
        "expensive_entry_rate": _rate(sum(1 for row in picks if row.get("expensive_entry")), n),
        "weak_reward_risk_rate": _rate(sum(1 for row in picks if row.get("weak_reward_risk")), n),
        "toxic_80_90_rate": _rate(sum(1 for row in picks if row.get("toxic_80_90")), n),
        "model_edge_bad_geometry_rate": _rate(sum(1 for row in picks if row.get("model_edge_bad_geometry")), n),
    }


def _rate(count: int, total: int) -> Optional[float]:
    return round(count / total, 8) if total else None


def build_shadow_row(
    opportunities: Iterable[Dict[str, Any]],
    scan_id: Optional[str],
    run_id: Optional[str],
    timestamp_utc: Optional[str] = None,
) -> Dict[str, Any]:
    ranked = _non_pass_ranked(opportunities)
    current = [_pick_payload(rank, row, None) for rank, row in ranked[:MAX_SHADOW_PICKS]]

    scored = [
        (score, rank, idx, row)
        for idx, (rank, row) in enumerate(ranked)
        for score in [payoff_score(row)]
        if score is not None
    ]
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    payoff = [_pick_payload(rank, row, score) for score, rank, _, row in scored[:MAX_SHADOW_PICKS]]

    strict_scored = [item for item in scored if strict_payoff_allowed(item[3])]
    strict = [_pick_payload(rank, row, score) for score, rank, _, row in strict_scored[:MAX_SHADOW_PICKS]]

    current_ids = {(row.get("ticker"), row.get("action"), row.get("rank")) for row in current}
    payoff_ids = {(row.get("ticker"), row.get("action"), row.get("rank")) for row in payoff}
    strict_ids = {(row.get("ticker"), row.get("action"), row.get("rank")) for row in strict}

    return {
        "timestamp_utc": timestamp_utc or datetime.now(timezone.utc).isoformat(),
        "run_id": run_id or "UNKNOWN_RUN",
        "scan_id": scan_id,
        "shadow_only": True,
        "execution_changed": False,
        "candidate_count": len(ranked),
        "current_top_3": current,
        "payoff_aware_top_3": payoff,
        "strict_payoff_top_3": strict,
        "overlap_current_payoff": _rate(len(current_ids & payoff_ids), MAX_SHADOW_PICKS),
        "overlap_current_strict": _rate(len(current_ids & strict_ids), MAX_SHADOW_PICKS),
        "current_summary": _summarize(current),
        "payoff_aware_summary": _summarize(payoff),
        "strict_payoff_summary": _summarize(strict),
        "strict_starvation_count": max(0, MAX_SHADOW_PICKS - len(strict)),
    }


def log_payoff_aware_shadow_ranking(
    opportunities: Iterable[Dict[str, Any]],
    scan_id: Optional[str] = None,
    run_id: Optional[str] = None,
    path: Path = LOG_PATH,
) -> Dict[str, Any]:
    """Append one scan-level shadow row. Fail-soft by design."""
    try:
        row = build_shadow_row(opportunities, scan_id=scan_id, run_id=run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as exc:
        return {"written": 0, "errors": 1, "error": exc.__class__.__name__}
    return {
        "written": 1,
        "errors": 0,
        "path": str(path),
        "candidate_count": row.get("candidate_count", 0),
        "strict_starvation_count": row.get("strict_starvation_count", 0),
    }
