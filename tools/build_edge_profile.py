"""
tools/build_edge_profile.py
---------------------------
Build historical edge profiles from clean settled paper trades only.

Reads logs/paper_trades.jsonl using the same terminal-state conflict handling
as tools/performance_report.py and writes data/edge_profile.json.

Usage:
  python3 tools/build_edge_profile.py
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from performance_report import (
    TRADES_LOG,
    build_terminal_key_sets,
    classify_settled_records,
    get_pnl,
    load_trades,
)

import sys


ROOT = Path(__file__).parent.parent
OUTPUT_PATH = ROOT / "data" / "edge_profile.json"
sys.path.insert(0, str(ROOT))

from brain.strategy_utils import normalize_strategy


def _num(rec: dict, field: str) -> Optional[float]:
    value = rec.get(field)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence_bucket(confidence: Optional[float]) -> str:
    if confidence is None:
        return "unknown"
    if confidence < 0.65:
        return "<0.65"
    if confidence < 0.70:
        return "0.65-0.70"
    if confidence < 0.75:
        return "0.70-0.75"
    if confidence < 0.80:
        return "0.75-0.80"
    if confidence < 0.90:
        return "0.80-0.90"
    return ">=0.90"


def _edge_bucket(edge: Optional[float]) -> str:
    if edge is None:
        return "unknown"
    if edge < 0.03:
        return "<0.03"
    if edge < 0.05:
        return "0.03-0.05"
    if edge < 0.10:
        return "0.05-0.10"
    if edge < 0.25:
        return "0.10-0.25"
    if edge < 0.50:
        return "0.25-0.50"
    return ">=0.50"


def _market_type(rec: dict) -> str:
    for field in ("market_type", "event_type", "asset_class"):
        value = rec.get(field)
        if value:
            return str(value).upper()

    strategy = str(rec.get("strategy") or "")
    if "_" in strategy:
        suffix = strategy.rsplit("_", 1)[-1]
        if suffix:
            return suffix.upper()

    ticker = str(rec.get("ticker") or "").upper()
    if any(token in ticker for token in ("BTC", "ETH", "XRP", "SOL", "DOGE")):
        return "CRYPTO"
    if any(token in ticker for token in ("NBA", "NFL", "MLB", "NHL", "WNBA", "NCAA")):
        return "SPORTS"
    if any(token in ticker for token in ("PRES", "SENATE", "HOUSE", "TRUMP", "BIDEN")):
        return "POLITICS"
    return "OTHER"


def _action_type(rec: dict) -> str:
    return str(rec.get("action") or "UNKNOWN").upper()


def _strategy(rec: dict) -> str:
    return normalize_strategy(rec.get("strategy"))


def _new_bucket() -> dict[str, Any]:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "_edge_sum": 0.0,
        "_edge_count": 0,
        "_confidence_sum": 0.0,
        "_confidence_count": 0,
    }


def _add_trade(bucket: dict[str, Any], rec: dict) -> None:
    pnl = get_pnl(rec)
    edge = _num(rec, "edge")
    confidence = _num(rec, "confidence")

    bucket["trades"] += 1
    if pnl > 0:
        bucket["wins"] += 1
    elif pnl < 0:
        bucket["losses"] += 1
    bucket["total_pnl"] += pnl

    if edge is not None:
        bucket["_edge_sum"] += edge
        bucket["_edge_count"] += 1
    if confidence is not None:
        bucket["_confidence_sum"] += confidence
        bucket["_confidence_count"] += 1


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    trades = bucket["trades"]
    edge_count = bucket["_edge_count"]
    confidence_count = bucket["_confidence_count"]

    return {
        "trades": trades,
        "wins": bucket["wins"],
        "losses": bucket["losses"],
        "win_rate": round(bucket["wins"] / trades, 4) if trades else 0.0,
        "total_pnl": round(bucket["total_pnl"], 2),
        "avg_pnl": round(bucket["total_pnl"] / trades, 4) if trades else 0.0,
        "avg_edge": round(bucket["_edge_sum"] / edge_count, 6) if edge_count else None,
        "avg_confidence": (
            round(bucket["_confidence_sum"] / confidence_count, 6)
            if confidence_count else None
        ),
    }


def _finalize_group(group: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: _finalize_bucket(bucket)
        for key, bucket in sorted(group.items(), key=lambda item: item[0])
    }


def build_profile() -> dict[str, Any]:
    all_records = load_trades()
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted_settled = classify_settled_records(
        all_records,
        settled_keys,
        forced_close_keys,
        void_keys,
    )

    groups = {
        "by_ticker": defaultdict(_new_bucket),
        "by_market_type": defaultdict(_new_bucket),
        "by_confidence_bucket": defaultdict(_new_bucket),
        "by_edge_bucket": defaultdict(_new_bucket),
        "by_action_type": defaultdict(_new_bucket),
        "by_strategy": defaultdict(_new_bucket),
    }

    overall = _new_bucket()

    for rec in clean_settled:
        confidence = _num(rec, "confidence")
        edge = _num(rec, "edge")

        _add_trade(overall, rec)
        _add_trade(groups["by_ticker"][str(rec.get("ticker") or "UNKNOWN")], rec)
        _add_trade(groups["by_market_type"][_market_type(rec)], rec)
        _add_trade(groups["by_confidence_bucket"][_confidence_bucket(confidence)], rec)
        _add_trade(groups["by_edge_bucket"][_edge_bucket(edge)], rec)
        _add_trade(groups["by_action_type"][_action_type(rec)], rec)
        _add_trade(groups["by_strategy"][_strategy(rec)], rec)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_log": str(TRADES_LOG),
        "clean_settled_trades": len(clean_settled),
        "conflicted_settled_trades_excluded": len(conflicted_settled),
        "overall": _finalize_bucket(overall),
        "profiles": {
            name: _finalize_group(group)
            for name, group in groups.items()
        },
    }


def main() -> None:
    profile = build_profile()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    print(f"[EDGE_PROFILE] wrote {OUTPUT_PATH}")
    print(f"[EDGE_PROFILE] clean_settled_trades={profile['clean_settled_trades']}")
    print(
        "[EDGE_PROFILE] conflicted_settled_trades_excluded="
        f"{profile['conflicted_settled_trades_excluded']}"
    )


if __name__ == "__main__":
    main()
