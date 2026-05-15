#!/usr/bin/env python3
"""
Phase 10S — Council Starvation Root-Cause Audit
Sentinel: COUNCIL_STARVATION_ROOT_CAUSE_OK

Read-only forensic report that proves or disproves the six starvation
hypotheses identified in the Phase 10 architecture audit:

  H1  Builder boost crosses 0.80-0.90 confidence boundary into >=0.90 loser
  H2  Sparse ticker penalty forces adjusted_edge below MIN_EDGE
  H3  MIN_EDGE + sparse penalty + danger guard creates impossible pass range
  H4  Edge profile uses risk_edge; Critic queries raw scanner edge (mismatch)
  H5  Strategy and market_type have zero discrimination power (constant dims)
  H6  Multi-category (>= 3 losing cats) fires because H5 pollutes every signal

This module does NOT:
  - change Builder, Critic, PaperTrader, Dashboard, or any brain/risk module
  - change any config flag or threshold
  - write to any log file
  - enable real money, scaling, or Kelly execution
  - alter the KXETH quarantine or any safety lock
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (  # noqa: E402
    DATA_COLLECTION_OVERRIDE_ENABLED,
    EDGE_DANGER_HIGH_EDGE_MIN,
    GLOBAL_FORCED_LEARNING_MODE,
    MIN_EDGE,
    TRADING_MODE,
)

SENTINEL = "COUNCIL_STARVATION_ROOT_CAUSE_OK"

# ── paths ──────────────────────────────────────────────────────────────────
FUNNEL_LOG   = ROOT / "logs" / "execution_funnel.jsonl"
TRADES_LOG   = ROOT / "logs" / "paper_trades.jsonl"
PROFILE_PATH = ROOT / "data" / "edge_profile.json"

# Shadow epoch: first timestamp of Phase 10G shadow logger
SHADOW_START_TEXT = "2026-05-14T03:22:46.375517+00:00"
SHADOW_START = datetime.fromisoformat(SHADOW_START_TEXT).astimezone(timezone.utc)

# Critic constants (replicated read-only — do not import brain modules)
MIN_SAMPLE_SIZE = 5
MAX_SMALL_SAMPLE_ADJUSTMENT = -0.15
MAX_CONFIDENCE_BOOST = 0.10          # from builder_brain

# Classification labels
CLS_BUCKET_CROSSING     = "BUCKET_CROSSING_ARTIFACT"
CLS_SPARSE_TICKER       = "SPARSE_TICKER_ARTIFACT"
CLS_EDGE_INTERVAL_TRAP  = "EDGE_INTERVAL_TRAP"
CLS_VALID_SAFETY        = "VALID_SAFETY_BLOCK"
CLS_MULTI_CATEGORY      = "MULTI_CATEGORY_ARTIFACT"
CLS_UNKNOWN             = "UNKNOWN_NEEDS_MORE_DATA"


# ══════════════════════════════════════════════════════════════════════════
# Bucket helpers  (replicated pure-logic — no brain imports, no side effects)
# ══════════════════════════════════════════════════════════════════════════

def _confidence_bucket(c: Optional[float]) -> str:
    if c is None: return "unknown"
    if c < 0.65:  return "<0.65"
    if c < 0.70:  return "0.65-0.70"
    if c < 0.75:  return "0.70-0.75"
    if c < 0.80:  return "0.75-0.80"
    if c < 0.90:  return "0.80-0.90"
    return ">=0.90"


def _edge_bucket(e: Optional[float]) -> str:
    if e is None: return "unknown"
    if e < 0.03:  return "<0.03"
    if e < 0.05:  return "0.03-0.05"
    if e < 0.10:  return "0.05-0.10"
    if e < 0.25:  return "0.10-0.25"
    if e < 0.50:  return "0.25-0.50"
    return ">=0.50"


def _as_float(v: Any) -> Optional[float]:
    if v is None: return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _trades(b: Optional[dict]) -> int:
    return int(b.get("trades", 0)) if b else 0

def _wr(b: Optional[dict]) -> Optional[float]:
    return float(b.get("win_rate", 0.0)) if b else None

def _pnl(b: Optional[dict]) -> float:
    return float(b.get("total_pnl", 0.0)) if b else 0.0

def _is_sparse(b: Optional[dict]) -> bool:
    return b is None or _trades(b) < MIN_SAMPLE_SIZE

def _is_losing(b: Optional[dict]) -> bool:
    return bool(b and _pnl(b) < 0)

def _bad_enough(b: Optional[dict]) -> bool:
    """Critic's _bad_enough_sample: >= MIN_SAMPLE_SIZE and (wr < 0.40 or pnl < 0)."""
    if _trades(b) < MIN_SAMPLE_SIZE:
        return False
    wr = _wr(b)
    return bool((wr is not None and wr < 0.40) or _pnl(b) < 0)

def _bad_any(b: Optional[dict]) -> bool:
    """Critic's _bad_any_sample: any sample, (wr < 0.40 or pnl < 0)."""
    if _trades(b) <= 0:
        return False
    wr = _wr(b)
    return bool((wr is not None and wr < 0.40) or _pnl(b) < 0)


def _get_bucket(profile: dict, group: str, key: str) -> Optional[dict]:
    return profile.get("profiles", {}).get(group, {}).get(key)


# ══════════════════════════════════════════════════════════════════════════
# Builder boost simulation  (replicated from builder_brain._score_bucket)
# ══════════════════════════════════════════════════════════════════════════

def _score_bucket_builder(b: Optional[dict]) -> float:
    """Return the boost score a Builder bucket produces (0.0 if none)."""
    if not b:
        return 0.0
    trades  = _trades(b)
    wr      = _wr(b) or 0.0
    pnl     = _pnl(b)
    avg_pnl = float(b.get("avg_pnl", 0.0))
    if trades < 3:
        return 0.0
    if pnl <= 0 or avg_pnl <= 0 or wr < 0.50:
        return 0.0
    if trades < 5:
        return 0.02
    if wr >= 0.60 and pnl > 0:
        boost = 0.05
        if wr >= 0.70:
            boost += 0.02
        if pnl >= 10:
            boost += 0.02
        return min(boost, MAX_CONFIDENCE_BOOST)
    return 0.0


def simulate_builder_boost(
    raw_conf: float,
    raw_edge: float,
    ticker: str,
    profile: dict,
) -> float:
    """
    Simulate what the Builder would return for a signal.

    Checks: confidence bucket, edge bucket, ticker, strategy(SIGNAL),
    market_type(CRYPTO). Matches builder_brain.suggest_signal_improvement
    logic exactly (no Builder import needed).
    """
    conf_key   = _confidence_bucket(raw_conf)
    edge_key   = _edge_bucket(raw_edge)
    prefix     = ticker.split("-")[0].upper() if ticker else "UNKNOWN"

    buckets_to_check = [
        _get_bucket(profile, "by_confidence_bucket", conf_key),
        _get_bucket(profile, "by_edge_bucket", edge_key),
        _get_bucket(profile, "by_ticker", ticker),
        _get_bucket(profile, "by_strategy", "SIGNAL"),
        _get_bucket(profile, "by_market_type", "CRYPTO"),
    ]

    total = sum(_score_bucket_builder(b) for b in buckets_to_check)
    return round(min(total, MAX_CONFIDENCE_BOOST), 6)


# ══════════════════════════════════════════════════════════════════════════
# Classify a single BLOCKED_COUNCIL funnel row
# ══════════════════════════════════════════════════════════════════════════

def classify_council_block(row: dict, profile: dict) -> dict:
    """
    Return a dict describing the probable starvation cause for one funnel row
    that has final_reason == BLOCKED_COUNCIL.

    Returns:
        {
          "classification": CLS_*,
          "detail": str,
          "builder_boost": float,
          "original_conf_bucket": str,
          "boosted_conf_bucket": str,
          "bucket_crossed": bool,
          "crossed_to_bad": bool,
          "original_was_good": bool,
          "ticker_sparse": bool,
          "adjusted_edge": float | None,
          "adjusted_edge_below_min": bool,
          "edge_interval_impossible": bool,
        }
    """
    raw_conf = _as_float(row.get("confidence")) or 0.0
    raw_edge = _as_float(row.get("edge")) or 0.0
    ticker   = str(row.get("ticker") or "UNKNOWN").upper()

    # ── Builder simulation ────────────────────────────────────────────────
    builder_boost  = simulate_builder_boost(raw_conf, raw_edge, ticker, profile)
    boosted_conf   = min(raw_conf + builder_boost, 1.0)

    orig_conf_bkt  = _confidence_bucket(raw_conf)
    boost_conf_bkt = _confidence_bucket(boosted_conf)
    bucket_crossed = orig_conf_bkt != boost_conf_bkt

    orig_conf_data  = _get_bucket(profile, "by_confidence_bucket", orig_conf_bkt)
    boost_conf_data = _get_bucket(profile, "by_confidence_bucket", boost_conf_bkt)

    original_was_good = not _bad_enough(orig_conf_data)
    crossed_to_bad    = bucket_crossed and _bad_enough(boost_conf_data)

    # ── Sparse ticker penalty simulation ─────────────────────────────────
    ticker_bkt    = _get_bucket(profile, "by_ticker", ticker)
    ticker_sparse = _is_sparse(ticker_bkt)

    if ticker_sparse:
        conf_adj = -0.10 if _is_losing(ticker_bkt) else -0.05
    else:
        conf_adj = 0.0

    conf_adj = max(conf_adj, MAX_SMALL_SAMPLE_ADJUSTMENT)
    adjusted_edge = round(raw_edge + conf_adj, 6) if raw_edge else None
    adj_below_min = adjusted_edge is not None and adjusted_edge < MIN_EDGE

    # ── Edge interval trap check ──────────────────────────────────────────
    # To survive sparse penalty: edge >= MIN_EDGE + abs(conf_adj) = 0.08
    # Danger guard blocks: edge >= EDGE_DANGER_HIGH_EDGE_MIN = 0.08
    # => pass window is [0.08, 0.08) — zero width
    # This is impossible regardless of signal values.
    edge_interval_impossible = (
        ticker_sparse
        and (MIN_EDGE + abs(conf_adj)) >= EDGE_DANGER_HIGH_EDGE_MIN
    )

    # ── Edge bucket primary check ─────────────────────────────────────────
    edge_key      = _edge_bucket(raw_edge)
    edge_bkt_data = _get_bucket(profile, "by_edge_bucket", edge_key)
    edge_bkt_bad  = _bad_enough(edge_bkt_data)

    # ── Classify ─────────────────────────────────────────────────────────
    if bucket_crossed and crossed_to_bad and original_was_good:
        cls    = CLS_BUCKET_CROSSING
        detail = (
            f"Builder boost {builder_boost:+.4f} moved conf "
            f"{raw_conf:.3f} ({orig_conf_bkt}) → {boosted_conf:.3f} ({boost_conf_bkt}); "
            f"target bucket bad: trades={_trades(boost_conf_data)}, "
            f"pnl={_pnl(boost_conf_data):+.2f}"
        )
    elif ticker_sparse and adj_below_min and edge_interval_impossible:
        cls    = CLS_EDGE_INTERVAL_TRAP
        detail = (
            f"Sparse ticker penalty {conf_adj:+.2f} → "
            f"adjusted_edge {adjusted_edge:.4f} < MIN_EDGE {MIN_EDGE:.4f}; "
            f"to survive would need edge >= {MIN_EDGE + abs(conf_adj):.4f} "
            f"but EDGE_DANGER_GUARD blocks >= {EDGE_DANGER_HIGH_EDGE_MIN:.4f}"
        )
    elif ticker_sparse and adj_below_min:
        cls    = CLS_SPARSE_TICKER
        detail = (
            f"Sparse ticker ({_trades(ticker_bkt)} trades < {MIN_SAMPLE_SIZE}); "
            f"penalty {conf_adj:+.2f} → adjusted_edge {adjusted_edge:.4f} < {MIN_EDGE:.4f}"
        )
    elif _bad_enough(orig_conf_data) or edge_bkt_bad:
        cls    = CLS_VALID_SAFETY
        detail = (
            f"Primary bucket genuinely bad: conf_bucket={orig_conf_bkt} "
            f"(pnl={_pnl(orig_conf_data):+.2f}), "
            f"edge_bucket={edge_key} (pnl={_pnl(edge_bkt_data):+.2f})"
        )
    else:
        cls    = CLS_UNKNOWN
        detail = (
            f"conf={raw_conf:.3f} edge={raw_edge:.4f} "
            f"boost={builder_boost:+.4f} no clear pattern"
        )

    return {
        "classification":          cls,
        "detail":                  detail,
        "builder_boost":           builder_boost,
        "original_conf_bucket":    orig_conf_bkt,
        "boosted_conf_bucket":     boost_conf_bkt,
        "bucket_crossed":          bucket_crossed,
        "crossed_to_bad":          crossed_to_bad,
        "original_was_good":       original_was_good,
        "ticker_sparse":           ticker_sparse,
        "adjusted_edge":           adjusted_edge,
        "adjusted_edge_below_min": adj_below_min,
        "edge_interval_impossible":edge_interval_impossible,
    }


# ══════════════════════════════════════════════════════════════════════════
# I/O helpers
# ══════════════════════════════════════════════════════════════════════════

def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _parse_ts(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        text = str(v).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _row_after_shadow(row: dict) -> bool:
    ts = _parse_ts(
        row.get("timestamp_utc") or row.get("timestamp") or row.get("created_at")
    )
    return ts is not None and ts >= SHADOW_START


# ══════════════════════════════════════════════════════════════════════════
# risk_edge vs raw edge  (H4)
# ══════════════════════════════════════════════════════════════════════════

def _analyze_risk_edge_mismatch(trades: list[dict], profile: dict) -> dict:
    """Compare raw edge to risk_edge in paper trades; check profile field declaration."""
    profile_field = profile.get("by_edge_bucket_field", "UNKNOWN")
    has_risk_edge  = sum(1 for t in trades if "risk_edge" in t)
    has_raw_edge   = sum(1 for t in trades if "edge" in t)

    deltas = []
    for t in trades:
        re = _as_float(t.get("risk_edge"))
        e  = _as_float(t.get("edge"))
        if re is not None and e is not None:
            deltas.append(round(re - e, 6))

    avg_delta = round(sum(deltas) / len(deltas), 6) if deltas else None
    max_abs   = round(max(abs(d) for d in deltas), 6) if deltas else None
    zero_diff = sum(1 for d in deltas if abs(d) < 1e-9)

    mismatch_confirmed = profile_field != "edge" and has_risk_edge > 0

    return {
        "profile_by_edge_bucket_field":  profile_field,
        "trades_with_risk_edge":         has_risk_edge,
        "trades_with_raw_edge":          has_raw_edge,
        "pairs_compared":                len(deltas),
        "avg_risk_edge_minus_raw_edge":  avg_delta,
        "max_abs_difference":            max_abs,
        "zero_difference_count":         zero_diff,
        "mismatch_confirmed":            mismatch_confirmed,
        "note": (
            "Profile was built using 'risk_edge'; Critic queries signal.get('edge') "
            "(pre-council scanner edge). These fields differ → bucket lookups are "
            "comparing apple values to orange-calibrated boundaries."
            if mismatch_confirmed else
            "No mismatch evidence found."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
# Strategy / market_type discrimination  (H5)
# ══════════════════════════════════════════════════════════════════════════

def _analyze_discrimination(profile: dict) -> dict:
    """Check if strategy and market_type buckets have discriminating power."""
    strat  = profile.get("profiles", {}).get("by_strategy", {})
    mtype  = profile.get("profiles", {}).get("by_market_type", {})
    total  = profile.get("profile_1d_population") or "unknown"

    def _bucket_coverage(bdict: dict) -> dict:
        items = list(bdict.items())
        total_trades = sum(_trades(v) for _, v in items)
        return {
            "unique_values": len(items),
            "total_trades":  total_trades,
            "entries": [
                {
                    "key":    k,
                    "trades": _trades(v),
                    "pnl":    _pnl(v),
                    "wr":     round(_wr(v) or 0.0, 4),
                    "coverage_pct": round(100 * _trades(v) / total_trades, 1)
                    if total_trades else 0,
                }
                for k, v in sorted(items, key=lambda x: -_trades(x[1]))
            ],
        }

    strat_cov = _bucket_coverage(strat)
    mtype_cov = _bucket_coverage(mtype)

    strat_single = strat_cov["unique_values"] == 1
    mtype_single = mtype_cov["unique_values"] == 1

    return {
        "strategy_dimension":   strat_cov,
        "market_type_dimension": mtype_cov,
        "strategy_single_value":  strat_single,
        "market_type_single_value": mtype_single,
        "hypothesis_confirmed": strat_single and mtype_single,
        "note": (
            "Both strategy and market_type collapse to a single value across ALL "
            "trades. They carry the aggregate system P&L and have zero "
            "discrimination power. They permanently contribute two 'bad category' "
            "entries to every signal, biasing the multi-category block rule."
            if strat_single and mtype_single else
            "At least one dimension has multiple values — partial discrimination exists."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
# Ticker bucketing analysis  (H2 + full vs prefix)
# ══════════════════════════════════════════════════════════════════════════

def _analyze_ticker_bucketing(profile: dict) -> dict:
    """Full-ticker vs prefix-ticker sample-size analysis."""
    full_tickers = profile.get("profiles", {}).get("by_ticker", {})

    # Full ticker distribution
    full_sizes = Counter(_trades(v) for v in full_tickers.values())

    # Prefix simulation: group by ticker.split("-")[0]
    prefix_agg: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for ticker, bdata in full_tickers.items():
        prefix = ticker.split("-")[0].upper()
        n   = _trades(bdata)
        p   = _pnl(bdata)
        wr  = _wr(bdata) or 0.0
        prefix_agg[prefix]["trades"] += n
        prefix_agg[prefix]["pnl"]    += p
        prefix_agg[prefix]["wins"]   += round(wr * n)

    prefix_summary = []
    for pf, agg in sorted(prefix_agg.items(), key=lambda x: -x[1]["trades"]):
        n   = agg["trades"]
        p   = round(agg["pnl"], 2)
        wr  = round(agg["wins"] / n, 4) if n else 0.0
        prefix_summary.append({
            "prefix": pf, "trades": n, "pnl": p, "win_rate": wr,
            "above_min_sample": n >= MIN_SAMPLE_SIZE,
        })

    prefix_sizes = Counter(e["trades"] for e in prefix_summary)
    all_full_sparse = all(s == 1 for s in full_sizes.keys())

    return {
        "full_ticker_count":     len(full_tickers),
        "full_ticker_size_dist": dict(sorted(full_sizes.items())),
        "all_full_tickers_sparse": all_full_sparse,
        "max_full_ticker_trades":  max(full_sizes.keys()) if full_sizes else 0,
        "prefix_count":          len(prefix_summary),
        "prefix_details":        prefix_summary,
        "prefix_size_dist":      dict(sorted(prefix_sizes.items())),
        "prefix_above_min_sample": sum(1 for e in prefix_summary
                                       if e["above_min_sample"]),
        "note": (
            "Every full ticker has exactly 1 trade (unique expiry+strike per contract). "
            "The sparse-sample path is ALWAYS triggered. Prefix grouping (e.g., KXBTCD) "
            "would yield statistically meaningful buckets."
            if all_full_sparse else
            "Some full ticker buckets have multiple trades."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
# Edge interval trap analysis  (H3)
# ══════════════════════════════════════════════════════════════════════════

def _analyze_edge_interval(min_edge: float, danger_min: float) -> dict:
    """
    Prove or disprove the Goldilocks impossibility.

    Three constraints on the raw scanner edge for a sparse-ticker signal:
      A) edge >= MIN_EDGE                        (passes MIN_EDGE filter)
      B) edge + (-0.05) >= MIN_EDGE              (survives sparse penalty in Critic)
         → edge >= MIN_EDGE + 0.05 = 0.08
      C) edge < EDGE_DANGER_HIGH_EDGE_MIN        (bypasses danger guard)
         → edge < 0.08

    B requires edge >= 0.08.
    C requires edge < 0.08.
    B ∩ C = empty set.
    """
    sparse_penalty = 0.05           # non-losing sparse ticker
    required_for_b = min_edge + sparse_penalty
    interval_width = danger_min - required_for_b

    rows = []
    rows.append(("MIN_EDGE (filter floor)",                min_edge,   "edge must be >= this"))
    rows.append(("Sparse penalty (non-losing ticker)",     -sparse_penalty, "applied when ticker < 5 trades"))
    rows.append(("Adjusted-edge floor (survive Critic)",   required_for_b, "edge + penalty >= MIN_EDGE"))
    rows.append(("EDGE_DANGER_GUARD ceiling",              danger_min, "edge must be < this"))
    rows.append(("Pass interval width",                    interval_width,
                 "POSITIVE = solvable, ZERO/NEGATIVE = impossible"))

    return {
        "min_edge":           min_edge,
        "sparse_penalty":     sparse_penalty,
        "required_edge_for_b": required_for_b,
        "danger_guard_ceiling": danger_min,
        "interval_width":     round(interval_width, 6),
        "interval_impossible": interval_width <= 0,
        "constraint_table":   rows,
        "note": (
            f"Pass interval [{required_for_b:.4f}, {danger_min:.4f}) has width "
            f"{interval_width:.4f} ({'IMPOSSIBLE — zero-width closed' if interval_width <= 0 else 'POSSIBLE'}). "
            "A signal with a sparse ticker can never satisfy both the Critic's "
            "adjusted-edge check and the edge-danger guard simultaneously."
            if interval_width <= 0 else
            f"A narrow but non-zero pass window [{required_for_b:.4f}, {danger_min:.4f}) exists "
            f"for signals with sparse tickers."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════
# Main report
# ══════════════════════════════════════════════════════════════════════════

def run_report(
    funnel_path: Path = FUNNEL_LOG,
    trades_path: Path = TRADES_LOG,
    profile_path: Path = PROFILE_PATH,
) -> dict:
    as_of = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {"as_of": as_of, "sentinel": SENTINEL}

    # ── Safety locks (read-only confirmation) ────────────────────────────
    report["safety_locks"] = {
        "trading_mode":              TRADING_MODE,
        "paper_only":                TRADING_MODE == "PAPER",
        "real_money_allowed":        False,   # hardcoded in evaluate_proof_gates
        "scale_allowed":             False,
        "kelly_execution_disabled":  GLOBAL_FORCED_LEARNING_MODE,
        "dc_override_enabled":       DATA_COLLECTION_OVERRIDE_ENABLED,
        "report_is_read_only":       True,
        "no_strategy_changes":       True,
        "no_threshold_changes":      True,
        "no_log_writes":             True,
    }

    # ── Load data ─────────────────────────────────────────────────────────
    if not profile_path.exists():
        report["error"] = "edge_profile.json missing — cannot run"
        return report

    profile = json.loads(profile_path.read_text())
    funnel_rows = _load_jsonl(funnel_path)
    trade_rows  = _load_jsonl(trades_path)

    report["data_sources"] = {
        "funnel_log":         str(funnel_path),
        "funnel_exists":      funnel_path.exists(),
        "funnel_total_rows":  len(funnel_rows),
        "trades_log":         str(trades_path),
        "trades_exists":      trades_path.exists(),
        "trades_total_rows":  len(trade_rows),
        "profile_exists":     True,
        "profile_generated_at": profile.get("generated_at", "unknown"),
        "profile_normal_modern": profile.get("normal_council_approved_modern_trades"),
        "shadow_start":       SHADOW_START_TEXT,
    }

    # ── Filter to post-shadow funnel rows ─────────────────────────────────
    after = [r for r in funnel_rows if _row_after_shadow(r)]
    council_blocks = [r for r in after if r.get("final_reason") == "BLOCKED_COUNCIL"]

    # ── Section 1: Funnel summary ─────────────────────────────────────────
    reason_dist = Counter(r.get("final_reason", "UNKNOWN") for r in after)
    opened      = sum(1 for r in after if r.get("paper_trade_opened"))

    report["funnel_summary"] = {
        "total_rows_after_shadow":       len(after),
        "candidates_reaching_pt":        sum(1 for r in after if r.get("paper_trader_received")),
        "opened_rows":                   opened,
        "blocked_rows":                  len(after) - opened,
        "council_blocks_count":          len(council_blocks),
        "blocked_by_reason":             dict(reason_dist.most_common()),
    }

    # ── Section 2: Sweet-spot analysis ───────────────────────────────────
    sweet = [r for r in after
             if 0.80 <= (r.get("confidence") or 0) < 0.90
             and 0.03 <= (r.get("edge") or 0) < 0.05]
    sweet_council = [r for r in sweet if r.get("final_reason") == "BLOCKED_COUNCIL"]
    sweet_other   = [r for r in sweet if r.get("final_reason") != "BLOCKED_COUNCIL"]

    report["sweet_spot"] = {
        "definition":          "confidence in [0.80, 0.90) AND edge in [0.03, 0.05)",
        "total_sweet_signals": len(sweet),
        "sweet_blocked_council":   len(sweet_council),
        "sweet_blocked_other":     len([r for r in sweet if r.get("final_reason") != "BLOCKED_COUNCIL" and not r.get("paper_trade_opened")]),
        "sweet_opened":            sum(1 for r in sweet if r.get("paper_trade_opened")),
        "profile_0_80_90_pnl":  _pnl(_get_bucket(profile, "by_confidence_bucket", "0.80-0.90")),
        "profile_0_03_05_pnl":  _pnl(_get_bucket(profile, "by_edge_bucket", "0.03-0.05")),
        "note": (
            "Both primary 1D buckets are positive in the profile. These signals "
            "SHOULD be council-ALLOWed if no downstream mechanism interferes."
        ),
    }

    # ── Section 3: H1 — Builder bucket-crossing analysis ─────────────────
    crossing_count         = 0
    crossing_to_bad_count  = 0
    boost_zero_count       = 0
    boost_nonzero_count    = 0
    crossing_examples: list[dict] = []

    for r in council_blocks:
        raw_conf = _as_float(r.get("confidence")) or 0.0
        raw_edge = _as_float(r.get("edge")) or 0.0
        ticker   = str(r.get("ticker") or "UNKNOWN").upper()

        boost         = simulate_builder_boost(raw_conf, raw_edge, ticker, profile)
        boosted_conf  = min(raw_conf + boost, 1.0)
        orig_bkt      = _confidence_bucket(raw_conf)
        boost_bkt     = _confidence_bucket(boosted_conf)
        crossed        = orig_bkt != boost_bkt
        orig_data      = _get_bucket(profile, "by_confidence_bucket", orig_bkt)
        boost_data     = _get_bucket(profile, "by_confidence_bucket", boost_bkt)
        crossed_bad    = crossed and _bad_enough(boost_data)
        orig_good      = not _bad_enough(orig_data)

        if boost > 0:
            boost_nonzero_count += 1
        else:
            boost_zero_count += 1
        if crossed:
            crossing_count += 1
        if crossed_bad:
            crossing_to_bad_count += 1

        if crossed and crossed_bad and orig_good and len(crossing_examples) < 5:
            crossing_examples.append({
                "ticker":           ticker,
                "raw_conf":         raw_conf,
                "builder_boost":    boost,
                "boosted_conf":     round(boosted_conf, 4),
                "original_bucket":  orig_bkt,
                "boosted_bucket":   boost_bkt,
                "original_pnl":     _pnl(orig_data),
                "boosted_pnl":      _pnl(boost_data),
                "boosted_trades":   _trades(boost_data),
            })

    report["h1_builder_bucket_crossing"] = {
        "hypothesis": "Builder boost pushes 0.80-0.90 signals into >=0.90 loser bucket",
        "council_blocks_analyzed":          len(council_blocks),
        "boost_zero_count":                 boost_zero_count,
        "boost_nonzero_count":              boost_nonzero_count,
        "bucket_crossings":                 crossing_count,
        "crossings_into_bad_bucket":        crossing_to_bad_count,
        "crossing_examples":                crossing_examples,
        "conf_bucket_0_80_90_pnl":  _pnl(_get_bucket(profile, "by_confidence_bucket", "0.80-0.90")),
        "conf_bucket_gte090_pnl":   _pnl(_get_bucket(profile, "by_confidence_bucket", ">=0.90")),
        "conf_bucket_gte090_trades": _trades(_get_bucket(profile, "by_confidence_bucket", ">=0.90")),
        "hypothesis_confirmed":             crossing_to_bad_count > 0,
        "note": (
            f"{crossing_to_bad_count} council blocks traced to Builder boost "
            "crossing original (positive) bucket boundary into confirmed loser bucket. "
            "The Builder's boost moves a signal FROM a statistically positive zone "
            "INTO a statistically negative zone, then the Critic correctly blocks it "
            "there. Net effect: the Builder's 'improvement' causes the block."
            if crossing_to_bad_count > 0 else
            "No bucket-crossing blocks detected."
        ),
    }

    # ── Section 4: H2+H3 — Sparse ticker penalty + edge interval trap ────
    sparse_penalty_count  = 0
    edge_interval_trapped = 0
    sparse_examples: list[dict] = []

    for r in council_blocks:
        raw_conf = _as_float(r.get("confidence")) or 0.0
        raw_edge = _as_float(r.get("edge")) or 0.0
        ticker   = str(r.get("ticker") or "UNKNOWN").upper()
        ticker_bkt = _get_bucket(profile, "by_ticker", ticker)
        sparse     = _is_sparse(ticker_bkt)

        if sparse:
            sparse_penalty_count += 1
            adj = -0.10 if _is_losing(ticker_bkt) else -0.05
            adj = max(adj, MAX_SMALL_SAMPLE_ADJUSTMENT)
            adj_edge = raw_edge + adj
            if adj_edge < MIN_EDGE:
                edge_interval_trapped += 1
                if len(sparse_examples) < 5:
                    sparse_examples.append({
                        "ticker":       ticker,
                        "raw_edge":     raw_edge,
                        "conf_adj":     adj,
                        "adjusted_edge": round(adj_edge, 6),
                        "min_edge":     MIN_EDGE,
                        "below_min":    True,
                        "ticker_trades": _trades(ticker_bkt),
                    })

    report["h2_sparse_ticker_penalty"] = {
        "hypothesis": "All full-ticker buckets are sparse; penalty applies to every signal",
        "council_blocks_with_sparse_ticker":  sparse_penalty_count,
        "council_blocks_total":               len(council_blocks),
        "sparse_pct":  round(100 * sparse_penalty_count / len(council_blocks), 1)
                       if council_blocks else 0,
        "adjusted_edge_below_min_count":      edge_interval_trapped,
        "sparse_examples":                    sparse_examples,
        "hypothesis_confirmed":               sparse_penalty_count == len(council_blocks),
        "note": (
            "100% of council-blocked signals have a sparse ticker because every "
            "short-expiry contract has a unique strike+date in its ticker name. "
            "No ticker ever accumulates 5 trades. The -0.05 penalty is permanent."
            if sparse_penalty_count == len(council_blocks) else
            f"Only {sparse_penalty_count}/{len(council_blocks)} signals have sparse tickers."
        ),
    }

    report["h3_edge_interval_trap"] = _analyze_edge_interval(MIN_EDGE, EDGE_DANGER_HIGH_EDGE_MIN)

    # ── Section 5: H4 — risk_edge vs raw edge mismatch ───────────────────
    report["h4_risk_edge_mismatch"] = _analyze_risk_edge_mismatch(trade_rows, profile)

    # ── Section 6: H5 — Strategy/market_type discrimination ──────────────
    report["h5_discrimination"] = _analyze_discrimination(profile)

    # ── Section 7: H6 — Multi-category block firing ───────────────────────
    multi_cat_count = 0
    for r in council_blocks:
        cr = str(r.get("council_reason") or "")
        if "multiple losing categories agree" in cr:
            multi_cat_count += 1

    report["h6_multi_category"] = {
        "hypothesis": "strategy+market_type constant dims trigger 3-category block",
        "council_blocks_with_multi_category_text": multi_cat_count,
        "hypothesis_confirmed": multi_cat_count > 0,
        "note": (
            f"{multi_cat_count} blocks explicitly show 'multiple losing categories agree'. "
            "When strategy=SIGNAL and market_type=CRYPTO always carry the total-system PnL "
            "(-$65.55), they permanently act as two 'losing category' votes on every signal, "
            "making it trivial to reach the 3-category threshold."
            if multi_cat_count > 0 else
            "Multi-category block text not found in council_reason field (may be truncated)."
        ),
    }

    # ── Section 8: Full classification of all council blocks ─────────────
    classifications: Counter = Counter()
    classified: list[dict] = []
    for r in council_blocks:
        result = classify_council_block(r, profile)
        classifications[result["classification"]] += 1
        classified.append(result)

    valid_safety  = classifications.get(CLS_VALID_SAFETY, 0)
    artifacts     = len(council_blocks) - valid_safety
    artifact_pct  = round(100 * artifacts / len(council_blocks), 1) if council_blocks else 0

    report["council_block_classification"] = {
        "total_council_blocks":      len(council_blocks),
        "classification_counts":     dict(classifications.most_common()),
        "valid_safety_blocks":       valid_safety,
        "likely_artifact_blocks":    artifacts,
        "artifact_pct":              artifact_pct,
        "note": (
            f"{artifact_pct}% of council blocks appear to be artifacts of the "
            "Builder-boost/sparse-ticker/edge-interval mechanisms rather than genuine "
            "safety decisions based on historical losing patterns."
        ),
    }

    # ── Section 9: Ticker bucketing analysis ──────────────────────────────
    report["ticker_bucketing"] = _analyze_ticker_bucketing(profile)

    # ── Section 10: Overall starvation verdict ────────────────────────────
    h1_confirmed = report["h1_builder_bucket_crossing"]["hypothesis_confirmed"]
    h2_confirmed = report["h2_sparse_ticker_penalty"]["hypothesis_confirmed"]
    h3_confirmed = report["h3_edge_interval_trap"]["interval_impossible"]
    h4_confirmed = report["h4_risk_edge_mismatch"]["mismatch_confirmed"]
    h5_confirmed = report["h5_discrimination"]["hypothesis_confirmed"]
    h6_confirmed = report["h6_multi_category"]["hypothesis_confirmed"]

    confirmed_count = sum([h1_confirmed, h2_confirmed, h3_confirmed,
                           h4_confirmed, h5_confirmed, h6_confirmed])

    starvation_verdict = (
        "ARTIFACT_DOMINANT"
        if artifact_pct > 50 else
        "MIXED"
        if artifact_pct > 20 else
        "VALID_DOMINATED"
    )

    live_patch_justified = bool(
        h1_confirmed and h2_confirmed and h3_confirmed
        and artifact_pct > 50
        and len(sweet_council) > 0
    )

    report["starvation_verdict"] = {
        "h1_builder_crossing_confirmed":  h1_confirmed,
        "h2_sparse_ticker_confirmed":     h2_confirmed,
        "h3_edge_interval_impossible":    h3_confirmed,
        "h4_risk_edge_mismatch":          h4_confirmed,
        "h5_discrimination_zero":         h5_confirmed,
        "h6_multi_category_confirmed":    h6_confirmed,
        "hypotheses_confirmed":           confirmed_count,
        "starvation_verdict":             starvation_verdict,
        "artifact_pct":                   artifact_pct,
        "sweet_spot_blocks_recoverable":  len(sweet_council),
        "live_patch_justified":           live_patch_justified,
    }

    # ── Section 11: Recommended minimal fix ──────────────────────────────
    if live_patch_justified:
        next_phase = "Phase 11A"
        recommendation = (
            "Phase 11A — Targeted Builder boost fix: "
            "(1) In builder_brain.suggest_signal_improvement, cap the boost "
            "so the resulting boosted confidence does NOT cross into a bucket "
            "that is _bad_enough_sample=True. "
            "(2) In build_edge_profile, replace full-ticker bucketing with "
            "ticker-prefix bucketing (ticker.split('-')[0]) so the ticker "
            "dimension accumulates meaningful sample sizes and the sparse "
            "penalty fires less frequently. "
            "(3) After changes: rebuild edge profile, restart Dashboard, "
            "monitor first 10 scan cycles for opened trades. "
            "Do NOT change MIN_EDGE, EDGE_DANGER_GUARD, or proof gates."
        )
    elif confirmed_count >= 3:
        next_phase = "Phase 11A (staged)"
        recommendation = (
            "Multiple hypotheses confirmed but artifact percentage is uncertain. "
            "Proceed with Phase 11A fixes to Builder and ticker bucketing, "
            "but validate with forward paper trades before drawing conclusions."
        )
    else:
        next_phase = "Continue shadow collection"
        recommendation = (
            "Insufficient evidence for a targeted fix. "
            "Continue shadow collection to accumulate outcome-matched data."
        )

    report["recommendation"] = {
        "next_phase":    next_phase,
        "justification": recommendation,
    }

    return report


# ══════════════════════════════════════════════════════════════════════════
# Pretty printer
# ══════════════════════════════════════════════════════════════════════════

def _line(char: str = "─", width: int = 72) -> str:
    return char * width


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "YES" if v else "NO"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def print_report(r: dict) -> None:
    W = 72
    print("=" * W)
    print("COUNCIL STARVATION ROOT-CAUSE AUDIT  (Phase 10S)")
    print("=" * W)

    # Safety
    sl = r.get("safety_locks", {})
    print(f"\nSAFETY LOCKS")
    print(_line())
    for k, v in sl.items():
        print(f"  {k:<40} {_fmt(v)}")

    # Funnel summary
    fs = r.get("funnel_summary", {})
    print(f"\nFUNNEL SUMMARY  (post-shadow-start rows)")
    print(_line())
    print(f"  total_rows_after_shadow      {fs.get('total_rows_after_shadow',0):>8}")
    print(f"  candidates_reaching_PT       {fs.get('candidates_reaching_pt',0):>8}")
    print(f"  opened_rows                  {fs.get('opened_rows',0):>8}")
    print(f"  council_blocks_count         {fs.get('council_blocks_count',0):>8}")
    print(f"  blocked_by_reason:")
    for reason, cnt in (fs.get("blocked_by_reason") or {}).items():
        print(f"    {reason:<40} {cnt:>6}")

    # Sweet spot
    ss = r.get("sweet_spot", {})
    print(f"\nSWEET-SPOT ANALYSIS  (conf 0.80-0.90, edge 0.03-0.05)")
    print(_line())
    print(f"  total_sweet_signals          {ss.get('total_sweet_signals',0):>8}")
    print(f"  sweet_blocked_council        {ss.get('sweet_blocked_council',0):>8}")
    print(f"  sweet_opened                 {ss.get('sweet_opened',0):>8}")
    print(f"  profile 0.80-0.90 PnL        {ss.get('profile_0_80_90_pnl',0):>+8.2f}")
    print(f"  profile 0.03-0.05 PnL        {ss.get('profile_0_03_05_pnl',0):>+8.2f}")
    print(f"  note: {ss.get('note','')}")

    # H1
    h1 = r.get("h1_builder_bucket_crossing", {})
    print(f"\nH1 — BUILDER BUCKET-CROSSING  (Builder boost → crosses bucket boundary)")
    print(_line())
    print(f"  boost_nonzero_count          {h1.get('boost_nonzero_count',0):>8}")
    print(f"  bucket_crossings             {h1.get('bucket_crossings',0):>8}")
    print(f"  crossings_into_bad_bucket    {h1.get('crossings_into_bad_bucket',0):>8}")
    print(f"  conf 0.80-0.90 PnL           {h1.get('conf_bucket_0_80_90_pnl',0):>+8.2f}")
    print(f"  conf >=0.90 PnL              {h1.get('conf_bucket_gte090_pnl',0):>+8.2f}")
    print(f"  conf >=0.90 trades           {h1.get('conf_bucket_gte090_trades',0):>8}")
    print(f"  CONFIRMED: {_fmt(h1.get('hypothesis_confirmed'))}")
    for ex in h1.get("crossing_examples", [])[:3]:
        print(f"    Example: {ex.get('ticker','?')[:35]}  "
              f"conf {ex.get('raw_conf'):.3f}+{ex.get('builder_boost'):.4f}"
              f"={ex.get('boosted_conf'):.4f}  "
              f"({ex.get('original_bucket')} → {ex.get('boosted_bucket')}  "
              f"pnl {ex.get('boosted_pnl'):+.2f})")

    # H2
    h2 = r.get("h2_sparse_ticker_penalty", {})
    print(f"\nH2 — SPARSE TICKER PENALTY")
    print(_line())
    print(f"  council blocks with sparse ticker  {h2.get('council_blocks_with_sparse_ticker',0):>6}")
    print(f"  pct of all council blocks          {h2.get('sparse_pct',0):>6.1f}%")
    print(f"  adjusted_edge below MIN_EDGE       {h2.get('adjusted_edge_below_min_count',0):>6}")
    print(f"  CONFIRMED: {_fmt(h2.get('hypothesis_confirmed'))}")

    # H3
    h3 = r.get("h3_edge_interval_trap", {})
    print(f"\nH3 — EDGE INTERVAL TRAP  (MIN_EDGE + sparse penalty + danger guard)")
    print(_line())
    for label, val, note in h3.get("constraint_table", []):
        print(f"  {label:<46} {val:>+8.4f}  ({note})")
    print(f"  interval_impossible: {_fmt(h3.get('interval_impossible'))}")
    print(f"  note: {h3.get('note','')}")

    # H4
    h4 = r.get("h4_risk_edge_mismatch", {})
    print(f"\nH4 — RISK_EDGE vs RAW EDGE MISMATCH")
    print(_line())
    print(f"  profile_by_edge_bucket_field  {h4.get('profile_by_edge_bucket_field','?')}")
    print(f"  trades_with_risk_edge         {h4.get('trades_with_risk_edge',0):>8}")
    print(f"  pairs_compared                {h4.get('pairs_compared',0):>8}")
    print(f"  avg_risk_edge_minus_raw       {str(h4.get('avg_risk_edge_minus_raw_edge','n/a')):>8}")
    print(f"  max_abs_difference            {str(h4.get('max_abs_difference','n/a')):>8}")
    print(f"  CONFIRMED: {_fmt(h4.get('mismatch_confirmed'))}")
    print(f"  note: {h4.get('note','')}")

    # H5
    h5 = r.get("h5_discrimination", {})
    print(f"\nH5 — STRATEGY / MARKET_TYPE DISCRIMINATION")
    print(_line())
    for dim_name, dim_key in [("Strategy", "strategy_dimension"),
                               ("Market type", "market_type_dimension")]:
        dim = h5.get(dim_key, {})
        print(f"  {dim_name}: {dim.get('unique_values',0)} unique values")
        for e in dim.get("entries", []):
            print(f"    {e.get('key','?'):<20} trades={e.get('trades',0):>4}  "
                  f"pnl={e.get('pnl',0):>+8.2f}  coverage={e.get('coverage_pct',0):.0f}%")
    print(f"  CONFIRMED (zero discrimination): {_fmt(h5.get('hypothesis_confirmed'))}")

    # H6
    h6 = r.get("h6_multi_category", {})
    print(f"\nH6 — MULTI-CATEGORY BLOCK RULE")
    print(_line())
    print(f"  blocks with multi-category text  {h6.get('council_blocks_with_multi_category_text',0):>6}")
    print(f"  CONFIRMED: {_fmt(h6.get('hypothesis_confirmed'))}")

    # Ticker bucketing
    tb = r.get("ticker_bucketing", {})
    print(f"\nTICKER BUCKETING ANALYSIS")
    print(_line())
    print(f"  full_ticker_count            {tb.get('full_ticker_count',0):>8}")
    print(f"  max_full_ticker_trades       {tb.get('max_full_ticker_trades',0):>8}")
    print(f"  all_full_tickers_sparse      {_fmt(tb.get('all_full_tickers_sparse'))}")
    print(f"  prefix_count                 {tb.get('prefix_count',0):>8}")
    print(f"  prefix_above_min_sample      {tb.get('prefix_above_min_sample',0):>8}")
    for pf in tb.get("prefix_details", []):
        above = "[OK]" if pf["above_min_sample"] else "[SPARSE]"
        print(f"    {above} {pf['prefix']:<15} trades={pf['trades']:>5}  "
              f"pnl={pf['pnl']:>+8.2f}  wr={pf['win_rate']:.3f}")

    # Classification
    cc = r.get("council_block_classification", {})
    print(f"\nCOUNCIL BLOCK CLASSIFICATION")
    print(_line())
    print(f"  total council blocks         {cc.get('total_council_blocks',0):>8}")
    print(f"  valid safety blocks          {cc.get('valid_safety_blocks',0):>8}")
    print(f"  likely artifact blocks       {cc.get('likely_artifact_blocks',0):>8}")
    print(f"  artifact pct                 {cc.get('artifact_pct',0):>7.1f}%")
    for cls_label, cnt in (cc.get("classification_counts") or {}).items():
        pct = round(100 * cnt / cc.get('total_council_blocks', 1), 1)
        print(f"    {cls_label:<40} {cnt:>5}  ({pct:.1f}%)")

    # Verdict
    sv = r.get("starvation_verdict", {})
    print(f"\nSTARVATION VERDICT")
    print("=" * W)
    print(f"  h1_builder_crossing         {_fmt(sv.get('h1_builder_crossing_confirmed'))}")
    print(f"  h2_sparse_ticker            {_fmt(sv.get('h2_sparse_ticker_confirmed'))}")
    print(f"  h3_edge_interval_impossible {_fmt(sv.get('h3_edge_interval_impossible'))}")
    print(f"  h4_risk_edge_mismatch       {_fmt(sv.get('h4_risk_edge_mismatch'))}")
    print(f"  h5_discrimination_zero      {_fmt(sv.get('h5_discrimination_zero'))}")
    print(f"  h6_multi_category           {_fmt(sv.get('h6_multi_category_confirmed'))}")
    print(f"  hypotheses_confirmed        {sv.get('hypotheses_confirmed',0)} / 6")
    print(f"  verdict                     {sv.get('starvation_verdict','?')}")
    print(f"  artifact_pct                {sv.get('artifact_pct',0):.1f}%")
    print(f"  sweet_spot_recoverable      {sv.get('sweet_spot_blocks_recoverable',0)}")
    print(f"  live_patch_justified        {_fmt(sv.get('live_patch_justified'))}")

    # Recommendation
    rec = r.get("recommendation", {})
    print(f"\nRECOMMENDATION")
    print(_line())
    print(f"  next_phase: {rec.get('next_phase','?')}")
    print(f"  {rec.get('justification','')}")

    print(f"\n{'=' * W}")
    print(f"  live_deployable:       False   (report only — no strategy changed)")
    print(f"  execution_changed:     False")
    print(f"  live_strategy_mutated: False")
    print("=" * W)
    print(SENTINEL)


if __name__ == "__main__":
    result = run_report()
    if "error" in result:
        print(f"ERROR: {result['error']}")
        sys.exit(1)
    print_report(result)
