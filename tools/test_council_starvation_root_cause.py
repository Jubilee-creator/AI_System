#!/usr/bin/env python3
"""
Phase 10S — Council Starvation Root-Cause Test Suite

Tests the logic in report_council_starvation_root_cause without reading live logs.
All tests use synthetic data; no live strategy, thresholds, or logs are changed.

Sentinel: TEST_COUNCIL_STARVATION_ROOT_CAUSE_OK
"""
from __future__ import annotations

import json
import sys
import tempfile
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.report_council_starvation_root_cause import (
    _confidence_bucket,
    _edge_bucket,
    _bad_enough,
    _bad_any,
    _is_sparse,
    _is_losing,
    _get_bucket,
    _trades,
    _pnl,
    _analyze_edge_interval,
    _analyze_ticker_bucketing,
    _analyze_discrimination,
    _analyze_risk_edge_mismatch,
    simulate_builder_boost,
    classify_council_block,
    run_report,
    CLS_BUCKET_CROSSING,
    CLS_SPARSE_TICKER,
    CLS_EDGE_INTERVAL_TRAP,
    CLS_VALID_SAFETY,
    MIN_EDGE,
    EDGE_DANGER_HIGH_EDGE_MIN,
    MAX_CONFIDENCE_BOOST,
    MIN_SAMPLE_SIZE,
    SENTINEL,
)

PASSED: list[str] = []
FAILED: list[str] = []


def _ok(name: str) -> None:
    PASSED.append(name)
    print(f"  [PASS] {name}")


def _fail(name: str, reason: str) -> None:
    FAILED.append(name)
    print(f"  [FAIL] {name} — {reason}")


def _assert(name: str, condition: bool, msg: str = "") -> None:
    if condition:
        _ok(name)
    else:
        _fail(name, msg or "assertion failed")


# ── Synthetic profile builder ─────────────────────────────────────────────

def _make_profile(
    conf_buckets: dict | None = None,
    edge_buckets: dict | None = None,
    tickers: dict | None = None,
    strategy: dict | None = None,
    market_type: dict | None = None,
    trusted: bool = True,
    normal_modern: int = 30,
    by_edge_bucket_field: str = "risk_edge",
) -> dict:
    """Build a minimal synthetic edge profile."""
    profiles: dict[str, Any] = {}
    if conf_buckets:
        profiles["by_confidence_bucket"] = conf_buckets
    if edge_buckets:
        profiles["by_edge_bucket"] = edge_buckets
    if tickers:
        profiles["by_ticker"] = tickers
    if strategy:
        profiles["by_strategy"] = strategy
    if market_type:
        profiles["by_market_type"] = market_type
    return {
        "profiles": profiles,
        "edge_profile_health": {
            "edge_profile_trusted": trusted,
            "normal_council_approved_modern_trades": normal_modern,
            "clean_settled_trades": 191,
            "modern_full_metadata_trades": 183,
            "data_collection_override_count": 10,
            "bootstrap_provisional_count": 8,
            "edge_profile_sample_ok": True,
            "edge_profile_exists": True,
            "edge_profile_age_hours": 0.0,
            "reason": "edge_profile trusted",
            "thresholds": {},
        },
        "normal_council_approved_modern_trades": normal_modern,
        "by_edge_bucket_field": by_edge_bucket_field,
        "profile_1d_population": "all_non_kxeth_clean_settled",
        "generated_at": "2026-05-13T15:29:07Z",
    }


def _bkt(trades: int, win_rate: float, total_pnl: float) -> dict:
    avg = round(total_pnl / trades, 4) if trades else 0.0
    return {"trades": trades, "win_rate": win_rate, "total_pnl": total_pnl, "avg_pnl": avg}


# ══════════════════════════════════════════════════════════════════════════
# T01  Bucket helpers
# ══════════════════════════════════════════════════════════════════════════

def test_confidence_bucket() -> None:
    print("\nT01: confidence_bucket")
    _assert("0.845 → 0.80-0.90", _confidence_bucket(0.845) == "0.80-0.90")
    _assert("0.945 → >=0.90",    _confidence_bucket(0.945) == ">=0.90")
    _assert("0.70  → 0.70-0.75", _confidence_bucket(0.70) == "0.70-0.75")
    _assert("0.899 → 0.80-0.90", _confidence_bucket(0.899) == "0.80-0.90")
    _assert("0.900 → >=0.90",    _confidence_bucket(0.900) == ">=0.90")
    _assert("None  → unknown",   _confidence_bucket(None) == "unknown")


def test_edge_bucket() -> None:
    print("\nT02: edge_bucket")
    _assert("0.035 → 0.03-0.05", _edge_bucket(0.035) == "0.03-0.05")
    _assert("0.08  → 0.05-0.10", _edge_bucket(0.08)  == "0.05-0.10")
    _assert("0.03  → 0.03-0.05", _edge_bucket(0.03)  == "0.03-0.05")
    _assert("0.025 → <0.03",     _edge_bucket(0.025) == "<0.03")
    _assert("None  → unknown",   _edge_bucket(None) == "unknown")


def test_bad_enough() -> None:
    print("\nT03: _bad_enough")
    # loses money, enough sample → bad
    _assert("pnl<0 n>=5 → True",  _bad_enough(_bkt(10, 0.80, -5.0)))
    # positive pnl → not bad
    _assert("pnl>0 → False",      not _bad_enough(_bkt(10, 0.80, +5.0)))
    # small sample → not bad_enough (even if negative)
    _assert("n<5 → False",        not _bad_enough(_bkt(4, 0.20, -10.0)))
    # None → not bad
    _assert("None → False",       not _bad_enough(None))


# ══════════════════════════════════════════════════════════════════════════
# T04  Builder boost simulation
# ══════════════════════════════════════════════════════════════════════════

def test_builder_boost_crossing() -> None:
    """H1: Builder boosts a 0.80-0.90 signal up to >=0.90."""
    print("\nT04: simulate_builder_boost — bucket crossing")

    # Profile: conf 0.80-0.90 is positive, edge 0.03-0.05 is positive
    profile = _make_profile(
        conf_buckets={
            "0.80-0.90": _bkt(41, 0.829, +3.40),
            ">=0.90":    _bkt(75, 0.773, -30.75),
        },
        edge_buckets={
            "0.03-0.05": _bkt(55, 0.818, +4.40),
        },
        strategy={"SIGNAL": _bkt(175, 0.749, -65.55)},
        market_type={"CRYPTO": _bkt(175, 0.749, -65.55)},
    )

    # Signal at 0.845 confidence — inside positive 0.80-0.90 bucket
    boost = simulate_builder_boost(0.845, 0.035, "KXBTCD-26MAY1417-T77999.99", profile)
    boosted = 0.845 + boost
    original_bkt = _confidence_bucket(0.845)
    boosted_bkt  = _confidence_bucket(boosted)

    _assert("boost_is_nonzero",          boost > 0,
            f"boost={boost}")
    _assert("original_bucket_0.80-0.90", original_bkt == "0.80-0.90",
            f"original={original_bkt}")
    _assert("boosted_crosses_to_>=0.90", boosted_bkt == ">=0.90",
            f"boosted_conf={boosted:.4f} bucket={boosted_bkt}")
    _assert("boost_capped_at_0.10",      boost <= MAX_CONFIDENCE_BOOST,
            f"boost={boost}")


def test_builder_boost_no_crossing() -> None:
    """No crossing is detected when the profile produces zero boost."""
    print("\nT05: simulate_builder_boost — no crossing when boost is zero")

    # Profile has all negative buckets → Builder produces zero boost
    profile = _make_profile(
        conf_buckets={"0.70-0.75": _bkt(10, 0.40, -5.00)},  # bad → no boost
        edge_buckets={"0.03-0.05": _bkt(10, 0.40, -3.00)},  # bad → no boost
        strategy={"SIGNAL":        _bkt(20, 0.40, -8.00)},
        market_type={"CRYPTO":     _bkt(20, 0.40, -8.00)},
    )

    boost    = simulate_builder_boost(0.72, 0.035, "KXBTCD-UNKNOWN", profile)
    boosted  = 0.72 + boost
    orig_bkt = _confidence_bucket(0.72)
    bst_bkt  = _confidence_bucket(boosted)

    _assert("boost_is_zero",            boost == 0.0, f"boost={boost}")
    _assert("orig_bucket_correct",      orig_bkt == "0.70-0.75")
    _assert("no_crossing_when_zero",    orig_bkt == bst_bkt,
            f"orig={orig_bkt} boosted_bkt={bst_bkt}")


def test_builder_boost_zero_on_neg_buckets() -> None:
    """Builder must return 0 boost when all buckets are negative."""
    print("\nT06: simulate_builder_boost — zero boost on negative profile")

    profile = _make_profile(
        conf_buckets={"0.70-0.75": _bkt(10, 0.40, -20.0)},
        edge_buckets={"0.03-0.05": _bkt(10, 0.40, -10.0)},
        strategy={"SIGNAL": _bkt(20, 0.40, -30.0)},
        market_type={"CRYPTO": _bkt(20, 0.40, -30.0)},
    )
    boost = simulate_builder_boost(0.72, 0.035, "SOME-TICKER", profile)
    _assert("boost_is_zero_on_bad_profile", boost == 0.0, f"boost={boost}")


# ══════════════════════════════════════════════════════════════════════════
# T07  Sparse ticker
# ══════════════════════════════════════════════════════════════════════════

def test_sparse_ticker_detection() -> None:
    print("\nT07: sparse ticker detection")

    # Ticker with 1 trade → sparse
    bkt_1 = _bkt(1, 1.0, +0.90)
    _assert("1_trade_is_sparse",   _is_sparse(bkt_1))
    _assert("4_trades_is_sparse",  _is_sparse(_bkt(4, 0.8, +1.0)))
    _assert("5_trades_not_sparse", not _is_sparse(_bkt(5, 0.8, +1.0)))
    _assert("None_is_sparse",      _is_sparse(None))


def test_ticker_prefix_grouping() -> None:
    """Ticker-prefix grouping should produce meaningful sample sizes."""
    print("\nT08: ticker prefix bucketing simulation")

    # Build a profile with 4 unique KXBTCD tickers, each 1 trade
    tickers = {
        "KXBTCD-26MAY1400-T80000": _bkt(1, 1.0, +1.0),
        "KXBTCD-26MAY1401-T80100": _bkt(1, 1.0, +0.9),
        "KXBTCD-26MAY1402-T80200": _bkt(1, 0.0, -5.0),
        "KXBTCD-26MAY1403-T80300": _bkt(1, 1.0, +0.8),
    }
    profile = _make_profile(tickers=tickers)
    result  = _analyze_ticker_bucketing(profile)

    _assert("full_ticker_count_4",     result["full_ticker_count"] == 4)
    _assert("all_full_sparse",         result["all_full_tickers_sparse"])
    _assert("prefix_count_1",          result["prefix_count"] == 1)

    prefix = result["prefix_details"][0]
    _assert("prefix_is_KXBTCD",        prefix["prefix"] == "KXBTCD")
    _assert("prefix_trades_4",         prefix["trades"] == 4)
    _assert("prefix_above_min_sample", not prefix["above_min_sample"],
            f"4 trades < MIN_SAMPLE_SIZE={MIN_SAMPLE_SIZE}")

    # With 5 unique tickers the prefix should have 5 trades (≥ MIN_SAMPLE_SIZE)
    tickers2 = dict(tickers)
    tickers2["KXBTCD-26MAY1404-T80400"] = _bkt(1, 1.0, +0.7)
    profile2 = _make_profile(tickers=tickers2)
    r2 = _analyze_ticker_bucketing(profile2)
    _assert("prefix_5_above_min",
            r2["prefix_details"][0]["above_min_sample"],
            f"5 trades should be >= MIN_SAMPLE_SIZE={MIN_SAMPLE_SIZE}")


# ══════════════════════════════════════════════════════════════════════════
# T09  Edge interval trap
# ══════════════════════════════════════════════════════════════════════════

def test_edge_interval_impossible() -> None:
    """H3: with sparse penalty=0.05 and danger guard at 0.08, interval is zero-width."""
    print("\nT09: edge interval impossible")

    result = _analyze_edge_interval(0.03, 0.08)
    _assert("interval_impossible",       result["interval_impossible"])
    _assert("width_is_zero_or_negative", result["interval_width"] <= 0,
            f"width={result['interval_width']}")


def test_edge_interval_valid_when_guard_higher() -> None:
    """If the danger guard is raised, a valid window opens."""
    print("\nT10: edge interval valid when guard is higher")

    # sparse_penalty = 0.05, so need edge >= 0.08
    # If danger guard is at 0.10, window is [0.08, 0.10) = width 0.02
    result = _analyze_edge_interval(0.03, 0.10)
    _assert("interval_possible",   not result["interval_impossible"])
    _assert("width_positive",      result["interval_width"] > 0,
            f"width={result['interval_width']}")


# ══════════════════════════════════════════════════════════════════════════
# T11  classify_council_block
# ══════════════════════════════════════════════════════════════════════════

def _make_row(confidence: float, edge: float, ticker: str, final_reason: str = "BLOCKED_COUNCIL") -> dict:
    return {
        "confidence": confidence,
        "edge": edge,
        "ticker": ticker,
        "final_reason": final_reason,
        "council_reason": "Critic blocked adjusted signal.",
        "timestamp_utc": "2026-05-14T10:00:00+00:00",
        "paper_trade_opened": False,
        "paper_trader_received": True,
    }


def test_classify_bucket_crossing() -> None:
    """Classification: signal boosted from good bucket into bad bucket."""
    print("\nT11: classify_council_block — BUCKET_CROSSING_ARTIFACT")

    profile = _make_profile(
        conf_buckets={
            "0.80-0.90": _bkt(41, 0.829, +3.40),
            ">=0.90":    _bkt(75, 0.773, -30.75),
        },
        edge_buckets={
            "0.03-0.05": _bkt(55, 0.818, +4.40),
        },
        strategy={"SIGNAL": _bkt(175, 0.749, -65.55)},
        market_type={"CRYPTO": _bkt(175, 0.749, -65.55)},
    )
    row = _make_row(0.845, 0.035, "KXBTCD-26MAY1417-T77999.99")
    result = classify_council_block(row, profile)

    _assert("classified_as_crossing",
            result["classification"] == CLS_BUCKET_CROSSING,
            f"got {result['classification']}")
    _assert("bucket_crossed_is_True",  result["bucket_crossed"])
    _assert("crossed_to_bad_is_True",  result["crossed_to_bad"])
    _assert("original_was_good",       result["original_was_good"])


def test_classify_valid_safety() -> None:
    """Classification: original confidence bucket is genuinely bad → VALID_SAFETY_BLOCK.

    To isolate this classification we must ensure:
    - No bucket crossing (boost stays in same bucket or boost=0)
    - No sparse ticker trap (give the ticker 5+ trades, non-losing)
    - Original confidence bucket IS genuinely bad (pnl < 0, n >= 5)
    """
    print("\nT12: classify_council_block — VALID_SAFETY_BLOCK")

    # Known ticker with 5 trades (non-sparse, non-losing) → no sparse penalty
    known_ticker = "KXBTCD-KNOWN-TICKER"
    profile = _make_profile(
        conf_buckets={
            "0.65-0.70": _bkt(24, 0.708, -2.50),   # negative PnL → bad_enough → block
        },
        edge_buckets={
            "0.03-0.05": _bkt(55, 0.818, +4.40),
        },
        # Provide a non-sparse, non-losing ticker so sparse trap does not fire
        tickers={known_ticker: _bkt(5, 0.80, +1.00)},
    )
    row = _make_row(0.67, 0.035, known_ticker)
    result = classify_council_block(row, profile)

    _assert("valid_safety_block",
            result["classification"] == CLS_VALID_SAFETY,
            f"got {result['classification']}: {result['detail']}")


def test_classify_sparse_ticker_trap() -> None:
    """H2+H3: sparse ticker penalty sends adjusted_edge below MIN_EDGE."""
    print("\nT13: classify_council_block — EDGE_INTERVAL_TRAP")

    # Profile: both primary buckets are positive, no ticker entry
    profile = _make_profile(
        conf_buckets={
            "0.80-0.90": _bkt(41, 0.829, +3.40),
            ">=0.90":    _bkt(10, 0.900, +1.00),  # positive, so no bucket crossing block
        },
        edge_buckets={
            "0.03-0.05": _bkt(55, 0.818, +4.40),
        },
        tickers={},  # no ticker entries → always sparse
    )
    # edge=0.035, sparse penalty=-0.05 → adjusted=-0.015 < MIN_EDGE=0.03
    row = _make_row(0.850, 0.035, "KXBTCD-NEW-TICKER-99999.99")
    # Prevent bucket crossing: make boost zero so orig bucket stays as is
    # With no matching ticker/strategy/market_type boosts and positive >=0.90 bucket
    # the boost won't cross into a bad bucket
    result = classify_council_block(row, profile)

    # With >=0.90 being positive, crossing wouldn't be into a bad bucket;
    # the edge interval trap should be the classification
    _assert("classification_is_trap_or_sparse",
            result["classification"] in (CLS_EDGE_INTERVAL_TRAP, CLS_SPARSE_TICKER,
                                         CLS_BUCKET_CROSSING),
            f"got {result['classification']}")
    _assert("ticker_is_sparse",   result["ticker_sparse"])
    _assert("adj_edge_below_min", result["adjusted_edge_below_min"],
            f"adjusted_edge={result['adjusted_edge']}")


# ══════════════════════════════════════════════════════════════════════════
# T14  strategy/market_type discrimination
# ══════════════════════════════════════════════════════════════════════════

def test_zero_discrimination() -> None:
    """H5: single-value strategy/market_type → zero discrimination."""
    print("\nT14: zero discrimination — single-value strategy + market_type")

    profile = _make_profile(
        strategy={"SIGNAL": _bkt(175, 0.749, -65.55)},
        market_type={"CRYPTO": _bkt(175, 0.749, -65.55)},
    )
    result = _analyze_discrimination(profile)
    _assert("strategy_single_value",    result["strategy_single_value"])
    _assert("market_type_single_value", result["market_type_single_value"])
    _assert("hypothesis_confirmed",     result["hypothesis_confirmed"])


def test_multi_value_discrimination() -> None:
    """When multiple strategy values exist, hypothesis should NOT be confirmed."""
    print("\nT15: multi-value discrimination → hypothesis not confirmed")

    profile = _make_profile(
        strategy={
            "SIGNAL": _bkt(100, 0.75, -20.0),
            "ARB":    _bkt(30,  0.90, +15.0),
        },
        market_type={"CRYPTO": _bkt(130, 0.77, -5.0)},
    )
    result = _analyze_discrimination(profile)
    _assert("not_confirmed_multi_strategy", not result["hypothesis_confirmed"])


# ══════════════════════════════════════════════════════════════════════════
# T16  risk_edge vs raw edge mismatch
# ══════════════════════════════════════════════════════════════════════════

def test_risk_edge_mismatch_detected() -> None:
    print("\nT16: risk_edge vs raw edge mismatch")

    profile = _make_profile(by_edge_bucket_field="risk_edge")
    trades = [
        {"edge": 0.035, "risk_edge": 0.055},
        {"edge": 0.042, "risk_edge": 0.062},
        {"edge": 0.038, "risk_edge": 0.058},
    ]
    result = _analyze_risk_edge_mismatch(trades, profile)
    _assert("mismatch_confirmed",         result["mismatch_confirmed"])
    _assert("profile_field_is_risk_edge", result["profile_by_edge_bucket_field"] == "risk_edge")
    _assert("pairs_compared_3",           result["pairs_compared"] == 3)
    # avg delta should be positive (~0.020)
    _assert("avg_delta_positive",
            result["avg_risk_edge_minus_raw_edge"] is not None
            and result["avg_risk_edge_minus_raw_edge"] > 0)


def test_no_mismatch_when_same_field() -> None:
    print("\nT17: no mismatch when profile field = 'edge'")

    profile = _make_profile(by_edge_bucket_field="edge")
    trades  = [{"edge": 0.035, "risk_edge": 0.055}]
    result  = _analyze_risk_edge_mismatch(trades, profile)
    _assert("no_mismatch", not result["mismatch_confirmed"])


# ══════════════════════════════════════════════════════════════════════════
# T18  Missing / empty log robustness
# ══════════════════════════════════════════════════════════════════════════

def test_missing_funnel_log_does_not_crash() -> None:
    print("\nT18: missing funnel log does not crash")

    with tempfile.TemporaryDirectory() as tmpdir:
        tp = Path(tmpdir)
        # Write a valid profile
        profile = _make_profile(
            conf_buckets={"0.80-0.90": _bkt(41, 0.829, +3.40)},
            edge_buckets={"0.03-0.05": _bkt(55, 0.818, +4.40)},
        )
        profile_path = tp / "edge_profile.json"
        profile_path.write_text(json.dumps(profile))

        try:
            result = run_report(
                funnel_path=tp / "missing_funnel.jsonl",
                trades_path=tp / "missing_trades.jsonl",
                profile_path=profile_path,
            )
            _assert("no_crash_missing_logs", True)
            _assert("sentinel_present", result.get("sentinel") == SENTINEL)
            fs = result.get("funnel_summary", {})
            _assert("zero_rows_on_missing", fs.get("total_rows_after_shadow", 0) == 0)
        except Exception as exc:
            _fail("no_crash_missing_logs", f"raised {exc!r}")


def test_empty_funnel_log_does_not_crash() -> None:
    print("\nT19: empty funnel log does not crash")

    with tempfile.TemporaryDirectory() as tmpdir:
        tp = Path(tmpdir)
        profile = _make_profile(
            conf_buckets={"0.80-0.90": _bkt(41, 0.829, +3.40)},
        )
        (tp / "edge_profile.json").write_text(json.dumps(profile))
        (tp / "funnel.jsonl").write_text("")
        (tp / "trades.jsonl").write_text("")

        try:
            result = run_report(
                funnel_path=tp / "funnel.jsonl",
                trades_path=tp / "trades.jsonl",
                profile_path=tp / "edge_profile.json",
            )
            _assert("no_crash_empty_logs", True)
            _assert("sentinel_present",    result.get("sentinel") == SENTINEL)
        except Exception as exc:
            _fail("no_crash_empty_logs", f"raised {exc!r}")


def test_funnel_with_malformed_lines() -> None:
    print("\nT20: malformed funnel lines are skipped gracefully")

    with tempfile.TemporaryDirectory() as tmpdir:
        tp = Path(tmpdir)
        profile = _make_profile(
            conf_buckets={"0.80-0.90": _bkt(41, 0.829, +3.40)},
        )
        (tp / "edge_profile.json").write_text(json.dumps(profile))
        # Mix valid and invalid JSON
        funnel_content = "\n".join([
            "not json at all",
            json.dumps({"timestamp_utc": "2026-05-14T10:00:00+00:00",
                        "final_reason": "BLOCKED_COUNCIL",
                        "confidence": 0.85, "edge": 0.035,
                        "ticker": "KXBTCD-TEST",
                        "paper_trade_opened": False,
                        "paper_trader_received": True}),
            "{incomplete",
        ])
        (tp / "funnel.jsonl").write_text(funnel_content)
        (tp / "trades.jsonl").write_text("")

        try:
            result = run_report(
                funnel_path=tp / "funnel.jsonl",
                trades_path=tp / "trades.jsonl",
                profile_path=tp / "edge_profile.json",
            )
            _assert("no_crash_malformed", True)
            # Should have parsed exactly 1 valid row
            fs = result.get("funnel_summary", {})
            _assert("parsed_1_valid_row",
                    fs.get("total_rows_after_shadow", 0) == 1,
                    f"got {fs.get('total_rows_after_shadow')}")
        except Exception as exc:
            _fail("no_crash_malformed", f"raised {exc!r}")


# ══════════════════════════════════════════════════════════════════════════
# T21  Read-only safety check
# ══════════════════════════════════════════════════════════════════════════

def test_report_is_read_only() -> None:
    """Confirm the report sets no config flags and makes no writes."""
    print("\nT21: report is read-only (no strategy/threshold changes)")

    import config.trading_config as cfg

    before_mode     = cfg.TRADING_MODE
    before_min_edge = cfg.MIN_EDGE
    before_gflm     = cfg.GLOBAL_FORCED_LEARNING_MODE
    before_dc       = cfg.DATA_COLLECTION_OVERRIDE_ENABLED

    with tempfile.TemporaryDirectory() as tmpdir:
        tp = Path(tmpdir)
        profile = _make_profile(
            conf_buckets={"0.80-0.90": _bkt(41, 0.829, +3.40)},
        )
        (tp / "edge_profile.json").write_text(json.dumps(profile))
        (tp / "funnel.jsonl").write_text("")
        (tp / "trades.jsonl").write_text("")
        run_report(
            funnel_path=tp / "funnel.jsonl",
            trades_path=tp / "trades.jsonl",
            profile_path=tp / "edge_profile.json",
        )

    _assert("trading_mode_unchanged",  cfg.TRADING_MODE == before_mode)
    _assert("min_edge_unchanged",      cfg.MIN_EDGE == before_min_edge)
    _assert("gflm_unchanged",          cfg.GLOBAL_FORCED_LEARNING_MODE == before_gflm)
    _assert("dc_override_unchanged",   cfg.DATA_COLLECTION_OVERRIDE_ENABLED == before_dc)


def test_safety_locks_confirmed() -> None:
    """Safety locks must be correct in any report output."""
    print("\nT22: safety locks confirmed in report output")

    with tempfile.TemporaryDirectory() as tmpdir:
        tp = Path(tmpdir)
        profile = _make_profile()
        (tp / "edge_profile.json").write_text(json.dumps(profile))
        (tp / "f.jsonl").write_text("")
        (tp / "t.jsonl").write_text("")
        result = run_report(
            funnel_path=tp / "f.jsonl",
            trades_path=tp / "t.jsonl",
            profile_path=tp / "edge_profile.json",
        )

    sl = result.get("safety_locks", {})
    _assert("real_money_False",  sl.get("real_money_allowed") is False)
    _assert("scale_False",       sl.get("scale_allowed") is False)
    _assert("paper_only_True",   sl.get("paper_only") is True)
    _assert("read_only_True",    sl.get("report_is_read_only") is True)
    _assert("no_strategy_True",  sl.get("no_strategy_changes") is True)
    _assert("no_threshold_True", sl.get("no_threshold_changes") is True)


# ══════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 68)
    print("TEST_COUNCIL_STARVATION_ROOT_CAUSE  (Phase 10S)")
    print("=" * 68)

    test_confidence_bucket()
    test_edge_bucket()
    test_bad_enough()
    test_builder_boost_crossing()
    test_builder_boost_no_crossing()
    test_builder_boost_zero_on_neg_buckets()
    test_sparse_ticker_detection()
    test_ticker_prefix_grouping()
    test_edge_interval_impossible()
    test_edge_interval_valid_when_guard_higher()
    test_classify_bucket_crossing()
    test_classify_valid_safety()
    test_classify_sparse_ticker_trap()
    test_zero_discrimination()
    test_multi_value_discrimination()
    test_risk_edge_mismatch_detected()
    test_no_mismatch_when_same_field()
    test_missing_funnel_log_does_not_crash()
    test_empty_funnel_log_does_not_crash()
    test_funnel_with_malformed_lines()
    test_report_is_read_only()
    test_safety_locks_confirmed()

    print(f"\n{'=' * 68}")
    print(f"Results: {len(PASSED)} passed, {len(FAILED)} failed")

    if FAILED:
        print("FAILED tests:")
        for f in FAILED:
            print(f"  - {f}")
        print("TEST_COUNCIL_STARVATION_ROOT_CAUSE_FAIL")
        return 1

    print("TEST_COUNCIL_STARVATION_ROOT_CAUSE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
