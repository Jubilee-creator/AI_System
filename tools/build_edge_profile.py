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
from brain.edge_profile_health import edge_profile_health

# Phase 9F: centralized ticker exclusion.
# Import the quarantine list from config; fall back to the literal if config
# is unavailable.  All profile aggregations — 1D and 2D — filter through
# _is_excluded_ticker() so adding a new quarantine prefix here is enough.
try:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES as _QUARANTINED_RAW
except ImportError:
    _QUARANTINED_RAW = ["KXETH"]

_PROFILE_EXCLUDED_PREFIXES: frozenset = frozenset(
    str(p).upper() for p in _QUARANTINED_RAW
)


def _is_excluded_ticker(ticker: Optional[str]) -> bool:
    """True when ticker belongs to a quarantined prefix excluded from all profile buckets."""
    t = str(ticker or "").upper()
    return any(t.startswith(pfx) for pfx in _PROFILE_EXCLUDED_PREFIXES)


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


def _price_bucket(price: Optional[float]) -> Optional[str]:
    """Entry price bucket for 2D (original_edge × price) profiling."""
    if price is None:
        return None
    if price < 0.10: return "<0.10"
    if price < 0.20: return "0.10-0.20"
    if price < 0.30: return "0.20-0.30"
    if price < 0.40: return "0.30-0.40"
    if price < 0.50: return "0.40-0.50"
    if price < 0.60: return "0.50-0.60"
    if price < 0.70: return "0.60-0.70"
    if price < 0.80: return "0.70-0.80"
    if price < 0.90: return "0.80-0.90"
    return "0.90-1.00"


def _is_normal_modern_for_2d(rec: dict) -> bool:
    """
    True only for normal-council-approved modern trades:
    has all modern metadata fields, is NOT data_collection_override,
    is NOT bootstrap_provisional.
    These are the only rows eligible for 2D price-conditioned proof.
    """
    if rec.get("council_decision") is None:
        return False
    if rec.get("bootstrap_provisional") is None:
        return False
    if rec.get("data_collection_override") is None:
        return False
    if rec.get("risk_edge") is None:
        return False
    if rec.get("bootstrap_era_council_allow") is None:
        return False
    if bool(rec.get("data_collection_override")):
        return False
    if bool(rec.get("bootstrap_provisional")):
        return False
    return True


def _action_type(rec: dict) -> str:
    return str(rec.get("action") or "UNKNOWN").upper()


def _strategy(rec: dict) -> str:
    return normalize_strategy(rec.get("strategy"))


def _has_quote_metadata(rec: dict) -> bool:
    if rec.get("price_yes") is not None or rec.get("price_no") is not None:
        return True
    return any(rec.get(k) is not None for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def _is_modern_full_metadata(rec: dict) -> bool:
    return (
        rec.get("risk_edge") is not None
        and rec.get("model_probability") is not None
        and _has_quote_metadata(rec)
    )


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
    risk_edge = _num(rec, "risk_edge")
    confidence = _num(rec, "confidence")

    bucket["trades"] += 1
    if pnl > 0:
        bucket["wins"] += 1
    elif pnl < 0:
        bucket["losses"] += 1
    bucket["total_pnl"] += pnl

    edge_for_profile = risk_edge if risk_edge is not None else edge
    if edge_for_profile is not None:
        bucket["_edge_sum"] += edge_for_profile
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

    # Phase 9F: exclude quarantined tickers from every profile aggregation.
    # clean_settled_trades (total) is preserved separately for the trust gate.
    excluded_for_profile = [
        r for r in clean_settled if _is_excluded_ticker(r.get("ticker"))
    ]
    clean_settled_for_profile = [
        r for r in clean_settled if not _is_excluded_ticker(r.get("ticker"))
    ]

    # Phase 9A: 2D (original_edge × entry_price) table.
    # Uses original_edge (pre-council scanner edge) — same field the Critic
    # receives — so lookup is consistent.  Built from normal_modern non-KXETH
    # trades only (clean proof-eligible evidence).
    by_edge_price_bucket: dict[str, Any] = defaultdict(_new_bucket)

    # Phase 9H: shadow 1D bucket built from normal_modern trades only.
    # NOT consumed by Critic/Builder — informational/diagnostic use only.
    # IMPORTANT: do NOT replace by_edge_bucket with this shadow until the
    # Critic is patched to fire the 2D check independently of bad_enough status.
    # Filtering 1D to normal_modern would flip 0.05-0.10 from BAD→GOOD, silencing
    # the 2D gate and potentially allowing poison-zone signals (yes_ask 0.60-0.80).
    by_normal_modern_edge_bucket: dict[str, Any] = defaultdict(_new_bucket)

    # Phase 9I: shadow 1D bucket built using original_edge for bucketing.
    # NOT consumed by Critic/Builder — diagnostic only.
    # Verifies that switching the 1D field from risk_edge → original_edge
    # produces the same _bad_enough_sample conclusion for all current buckets.
    # DO NOT promote to the live by_edge_bucket until decision-replay confirms
    # zero changed decisions for 30+ additional trades (Phase 9G recommendation).
    by_original_edge_bucket: dict[str, Any] = defaultdict(_new_bucket)

    overall = _new_bucket()
    modern_full_metadata = [
        rec for rec in clean_settled_for_profile
        if _is_modern_full_metadata(rec)
    ]
    data_collection_override_count = sum(
        1 for rec in modern_full_metadata
        if bool(rec.get("data_collection_override"))
    )
    bootstrap_provisional_count = sum(
        1 for rec in modern_full_metadata
        if bool(rec.get("bootstrap_provisional"))
    )
    # bootstrap_era_allow trades (Phase 6B-2) are NOT excluded — they count as
    # normal_council_approved_modern.  Track count separately for reporting only.
    bootstrap_era_allow_count = sum(
        1 for rec in modern_full_metadata
        if bool(rec.get("bootstrap_era_council_allow"))
    )
    normal_council_approved_modern = (
        len(modern_full_metadata) - data_collection_override_count - bootstrap_provisional_count
    )
    normal_council_approved_modern = max(0, normal_council_approved_modern)

    # Phase 9H: count non-normal records present in the 1D pool for transparency.
    legacy_in_1d_count = sum(
        1 for rec in clean_settled_for_profile
        if not _is_modern_full_metadata(rec)
    )
    dc_override_in_1d_count = sum(
        1 for rec in clean_settled_for_profile
        if bool(rec.get("data_collection_override"))
    )
    bootstrap_provisional_in_1d_count = sum(
        1 for rec in clean_settled_for_profile
        if bool(rec.get("bootstrap_provisional"))
    )

    # Phase 9I: original_edge shadow transparency counts.
    # has_field_count: records with explicit original_edge stored.
    # fallback_count:  records without original_edge that fall back to edge.
    # missing_count:   records with neither field (should be 0; guard only).
    original_edge_shadow_has_field_count = sum(
        1 for rec in clean_settled_for_profile
        if _num(rec, "original_edge") is not None
    )
    original_edge_shadow_fallback_count = sum(
        1 for rec in clean_settled_for_profile
        if _num(rec, "original_edge") is None and _num(rec, "edge") is not None
    )
    original_edge_shadow_missing_count = sum(
        1 for rec in clean_settled_for_profile
        if _num(rec, "original_edge") is None and _num(rec, "edge") is None
    )

    for rec in clean_settled_for_profile:
        confidence = _num(rec, "confidence")
        edge = _num(rec, "risk_edge")
        if edge is None:
            edge = _num(rec, "edge")

        _add_trade(overall, rec)
        _add_trade(groups["by_ticker"][str(rec.get("ticker") or "UNKNOWN")], rec)
        _add_trade(groups["by_market_type"][_market_type(rec)], rec)
        _add_trade(groups["by_confidence_bucket"][_confidence_bucket(confidence)], rec)
        _add_trade(groups["by_edge_bucket"][_edge_bucket(edge)], rec)
        _add_trade(groups["by_action_type"][_action_type(rec)], rec)
        _add_trade(groups["by_strategy"][_strategy(rec)], rec)

        # Shadow normal_modern edge bucket (Phase 9H).
        if _is_normal_modern_for_2d(rec):
            _add_trade(by_normal_modern_edge_bucket[_edge_bucket(edge)], rec)

        # Phase 9I: shadow original_edge bucket.
        # Uses original_edge (pre-council) for bucketing — consistent with
        # Critic lookup and the 2D table.  Legacy records without original_edge
        # fall back to edge (which IS their original edge — no council existed).
        orig_edge_val = _num(rec, "original_edge")
        if orig_edge_val is None:
            orig_edge_val = _num(rec, "edge")
        if orig_edge_val is not None:
            _add_trade(by_original_edge_bucket[_edge_bucket(orig_edge_val)], rec)

    # 2D price-conditioned table: normal_modern non-quarantined only.
    # clean_settled_for_profile is already quarantine-excluded; the
    # _is_excluded_ticker check below is belt-and-suspenders.
    for rec in clean_settled_for_profile:
        if _is_excluded_ticker(rec.get("ticker")):
            continue
        if not _is_normal_modern_for_2d(rec):
            continue
        orig_edge = _num(rec, "original_edge")
        if orig_edge is None:
            orig_edge = _num(rec, "edge")
        entry_price = _num(rec, "entry_price")
        if entry_price is None:
            entry_price = _num(rec, "yes_ask")
        eb = _edge_bucket(orig_edge)
        pb = _price_bucket(entry_price)
        if eb and pb:
            cell_key = f"{eb}|{pb}"
            _add_trade(by_edge_price_bucket[cell_key], rec)

    profile = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_log": str(TRADES_LOG),
        # clean_settled_trades: total conflict-resolved settled count (used by trust gate).
        # Quarantined tickers are excluded from bucket aggregations but this total is
        # preserved so edge_profile_health() can evaluate sample size correctly.
        "clean_settled_trades": len(clean_settled),
        "profile_input_trades": len(clean_settled_for_profile),
        "profile_kxeth_excluded_count": len(excluded_for_profile),
        "excluded_ticker_prefixes": sorted(_PROFILE_EXCLUDED_PREFIXES),
        "modern_full_metadata_trades": len(modern_full_metadata),
        "normal_council_approved_modern_trades": normal_council_approved_modern,
        "data_collection_override_count": data_collection_override_count,
        "bootstrap_provisional_count": bootstrap_provisional_count,
        "bootstrap_era_allow_count": bootstrap_era_allow_count,
        "conflicted_settled_trades_excluded": len(conflicted_settled),
        # Phase 9H: 1D population transparency fields.
        # 1D buckets use ALL clean_settled_for_profile (not just normal_modern).
        # This is intentional: non-normal records keep the 0.05-0.10 bucket
        # negative, forcing the 2D gate to fire. Filtering would flip the bucket
        # to GOOD and silence the 2D poison-zone guard.
        "profile_1d_population": "all_non_kxeth_clean_settled",
        "profile_1d_normal_modern_count": normal_council_approved_modern,
        "profile_1d_dc_override_in_1d": dc_override_in_1d_count,
        "profile_1d_bootstrap_provisional_in_1d": bootstrap_provisional_in_1d_count,
        "profile_1d_legacy_in_1d": legacy_in_1d_count,
        # Phase 9I: original_edge shadow metadata.
        # by_original_edge_bucket uses original_edge (pre-council) for bucketing,
        # consistent with Critic lookup and the 2D table. Shadow only — no live change.
        "profile_has_original_edge_shadow": True,
        "by_original_edge_bucket_field": "original_edge",
        "by_edge_bucket_field": "risk_edge",
        "original_edge_shadow_input_count": len(clean_settled_for_profile),
        "original_edge_shadow_has_field_count": original_edge_shadow_has_field_count,
        "original_edge_shadow_fallback_count": original_edge_shadow_fallback_count,
        "original_edge_shadow_missing_count": original_edge_shadow_missing_count,
        "edge_math_alignment_status": "SHADOW_ONLY_NO_LIVE_CHANGE",
        "overall": _finalize_bucket(overall),
        "profiles": {
            name: _finalize_group(group)
            for name, group in groups.items()
        },
    }
    # Phase 9A: attach 2D table as a peer to the 1D profile groups.
    profile["profiles"]["by_edge_price_bucket"] = _finalize_group(by_edge_price_bucket)
    # Phase 9H: attach shadow normal_modern-only 1D edge bucket (diagnostic, not consumed).
    profile["profiles"]["by_normal_modern_edge_bucket"] = _finalize_group(
        by_normal_modern_edge_bucket
    )
    # Phase 9I: shadow original_edge 1D bucket (diagnostic, not consumed by Critic/Builder).
    profile["profiles"]["by_original_edge_bucket"] = _finalize_group(
        by_original_edge_bucket
    )
    profile["edge_profile_health"] = edge_profile_health(profile, OUTPUT_PATH)
    profile["source_warning"] = (
        "UNTRUSTED: " + profile["edge_profile_health"]["reason"]
        if not profile["edge_profile_health"]["edge_profile_trusted"]
        else "trusted"
    )
    return profile


def main() -> None:
    profile = build_profile()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")
    print(f"[EDGE_PROFILE] wrote {OUTPUT_PATH}")
    print(f"[EDGE_PROFILE] clean_settled_trades={profile['clean_settled_trades']}")
    print(
        "[EDGE_PROFILE] profile_kxeth_excluded_count="
        f"{profile['profile_kxeth_excluded_count']} "
        f"excluded_prefixes={profile['excluded_ticker_prefixes']}"
    )
    print(f"[EDGE_PROFILE] profile_input_trades={profile['profile_input_trades']}")
    print(
        "[EDGE_PROFILE] modern_full_metadata_trades="
        f"{profile['modern_full_metadata_trades']}"
    )
    print(
        "[EDGE_PROFILE] normal_council_approved_modern_trades="
        f"{profile['normal_council_approved_modern_trades']}"
    )
    print(
        "[EDGE_PROFILE] data_collection_override_count="
        f"{profile['data_collection_override_count']}"
    )
    print(
        "[EDGE_PROFILE] bootstrap_provisional_count="
        f"{profile['bootstrap_provisional_count']}"
    )
    print(
        "[EDGE_PROFILE] conflicted_settled_trades_excluded="
        f"{profile['conflicted_settled_trades_excluded']}"
    )
    print(
        "[EDGE_PROFILE] trusted="
        f"{profile['edge_profile_health']['edge_profile_trusted']} "
        f"reason={profile['edge_profile_health']['reason']}"
    )


if __name__ == "__main__":
    main()
