#!/usr/bin/env python3
"""
Phase 11A — Surgical Fix Test Suite

Tests for three targeted fixes:
  1. Builder must not boost confidence into a confirmed-losing bucket
  2. Builder and Critic must look up ticker by prefix (by_ticker_prefix)
  3. Safety constants must remain unchanged

Tests:
  T01  _is_confirmed_bad_bucket: returns False for missing bucket
  T02  _is_confirmed_bad_bucket: returns False for small sample (< _BUILDER_MIN_SAMPLE_BAD)
  T03  _is_confirmed_bad_bucket: returns True when win_rate < 0.40
  T04  _is_confirmed_bad_bucket: returns True when total_pnl < 0
  T05  _is_confirmed_bad_bucket: returns False for healthy bucket
  T06  _cap_boost_to_avoid_bad_buckets: no cap when no boundary is crossed
  T07  _cap_boost_to_avoid_bad_buckets: no cap when crossed bucket is healthy
  T08  _cap_boost_to_avoid_bad_buckets: caps boost when crossing into confirmed-bad bucket
  T09  _cap_boost_to_avoid_bad_buckets: floor at 0 when capped value would be negative
  T10  builder suggest: prefers by_ticker_prefix over by_ticker for boost
  T11  builder suggest: falls back to by_ticker when prefix group is absent
  T12  builder suggest: 0.845 confidence not boosted into >=0.90 bad bucket
  T13  critic critique: uses by_ticker_prefix (non-sparse when prefix data present)
  T14  critic critique: falls back to by_ticker when prefix group absent
  T15  constants: _CONFIDENCE_BUCKET_BOUNDARIES is the expected tuple
  T16  constants: _BUILDER_MIN_SAMPLE_BAD == 5
  T17  safety: MIN_EDGE == 0.03 unchanged
  T18  safety: MIN_CONFIDENCE == 0.65 unchanged
  T19  safety: MAX_CONFIDENCE_BOOST == 0.10 unchanged
  T20  readonly: importing builder/critic does not mutate config or logs

  -- build_edge_profile.py integration tests --
  T21  build_profile() creates by_ticker_prefix group with entries
  T22  by_ticker_prefix keys are correctly formed (no hyphens, uppercase)
  T23  KXETH is excluded from by_ticker_prefix and by_ticker
  T24  _is_excluded_ticker classifies KXETH tickers as excluded; others as not excluded
  T25  data/edge_profile.json is covered by .gitignore and must not be staged

Note: data/edge_profile.json is generated local runtime state.
It is covered by the data/* pattern in .gitignore and must never be committed.
T21–T23 call build_profile() which reads the live trade log and returns a dict;
they do NOT write to disk.

Sentinel: TEST_PHASE_11A_OK
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))  # needed: build_edge_profile imports performance_report

from brain.builder_brain import (
    MAX_CONFIDENCE_BOOST,
    _BUILDER_MIN_SAMPLE_BAD,
    _CONFIDENCE_BUCKET_BOUNDARIES,
    _cap_boost_to_avoid_bad_buckets,
    _is_confirmed_bad_bucket,
    suggest_signal_improvement,
)
from brain.critic_brain import critique_signal
from config.trading_config import MIN_CONFIDENCE, MIN_EDGE
from tools.build_edge_profile import _is_excluded_ticker, build_profile

PASSED: list[str] = []
FAILED: list[str] = []
SENTINEL = "TEST_PHASE_11A_OK"


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


# ── Synthetic data helpers ────────────────────────────────────────────────────

def _bkt(
    trades: int = 10,
    win_rate: float = 0.80,
    total_pnl: float = 20.0,
) -> dict[str, Any]:
    wins = int(trades * win_rate)
    return {
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / trades if trades > 0 else 0.0,
    }


def _make_profile(
    conf_buckets: dict[str, Any] | None = None,
    edge_buckets: dict[str, Any] | None = None,
    tickers: dict[str, Any] | None = None,
    ticker_prefixes: dict[str, Any] | None = None,
    strategy: dict[str, Any] | None = None,
    market_type: dict[str, Any] | None = None,
    normal_modern: int = 20,
    clean_settled: int = 50,
    modern_full: int = 40,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clean_settled_trades": clean_settled,
        "modern_full_metadata_trades": modern_full,
        "normal_council_approved_modern_trades": normal_modern,
        "data_collection_override_count": 0,
        "bootstrap_provisional_count": 0,
        "profiles": {
            "by_confidence_bucket": conf_buckets or {},
            "by_edge_bucket": edge_buckets or {},
            "by_ticker": tickers or {},
            "by_ticker_prefix": ticker_prefixes or {},
            "by_strategy": strategy or {},
            "by_market_type": market_type or {},
        },
    }


def _tmpfile(profile: dict[str, Any]) -> Path:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(profile, f)
    return Path(path)


# ── T01–T05: _is_confirmed_bad_bucket unit tests ──────────────────────────────

def test_T01_missing_bucket() -> None:
    result = _is_confirmed_bad_bucket({}, ">=0.90")
    _assert("T01_is_confirmed_bad_bucket_missing", not result, "empty profile → False")


def test_T02_small_sample() -> None:
    p = _make_profile(conf_buckets={">=0.90": _bkt(trades=3, win_rate=0.2, total_pnl=-5)})
    result = _is_confirmed_bad_bucket(p, ">=0.90")
    _assert(
        "T02_is_confirmed_bad_bucket_small_sample",
        not result,
        f"trades=3 < {_BUILDER_MIN_SAMPLE_BAD} → False",
    )


def test_T03_bad_win_rate() -> None:
    p = _make_profile(conf_buckets={">=0.90": _bkt(trades=8, win_rate=0.35, total_pnl=1)})
    result = _is_confirmed_bad_bucket(p, ">=0.90")
    _assert("T03_is_confirmed_bad_bucket_bad_win_rate", result, "win_rate=0.35 < 0.40 → True")


def test_T04_negative_pnl() -> None:
    p = _make_profile(conf_buckets={">=0.90": _bkt(trades=8, win_rate=0.55, total_pnl=-1)})
    result = _is_confirmed_bad_bucket(p, ">=0.90")
    _assert("T04_is_confirmed_bad_bucket_negative_pnl", result, "total_pnl < 0 → True")


def test_T05_healthy_bucket() -> None:
    p = _make_profile(conf_buckets={">=0.90": _bkt(trades=8, win_rate=0.75, total_pnl=20)})
    result = _is_confirmed_bad_bucket(p, ">=0.90")
    _assert("T05_is_confirmed_bad_bucket_healthy", not result, "wr=0.75, pnl=20 → False")


# ── T06–T09: _cap_boost_to_avoid_bad_buckets unit tests ──────────────────────

def test_T06_no_boundary_crossed() -> None:
    p = _make_profile(conf_buckets={">=0.90": _bkt(trades=8, win_rate=0.2, total_pnl=-30)})
    # confidence=0.84, boost=0.05 → boosted=0.89 — 0.90 boundary not crossed
    result = _cap_boost_to_avoid_bad_buckets(0.84, 0.05, p)
    _assert(
        "T06_cap_no_boundary_crossed",
        abs(result - 0.05) < 1e-9,
        f"no boundary crossed → boost unchanged, got {result}",
    )


def test_T07_target_bucket_healthy() -> None:
    p = _make_profile(conf_buckets={">=0.90": _bkt(trades=8, win_rate=0.75, total_pnl=20)})
    # confidence=0.845, boost=0.10 → crosses 0.90 but target is healthy
    result = _cap_boost_to_avoid_bad_buckets(0.845, 0.10, p)
    _assert(
        "T07_cap_target_bucket_healthy",
        abs(result - 0.10) < 1e-9,
        f"healthy target → no cap, got {result}",
    )


def test_T08_crossing_bad_bucket() -> None:
    p = _make_profile(conf_buckets={">=0.90": _bkt(trades=8, win_rate=0.2, total_pnl=-30)})
    # confidence=0.845, boost=0.10 → boosted=0.945, crosses 0.90 into bad bucket
    result = _cap_boost_to_avoid_bad_buckets(0.845, 0.10, p)
    max_safe = 0.90 - 0.845  # = 0.055; cap must be strictly below this
    _assert(
        "T08_cap_crossing_bad_bucket",
        result < max_safe,
        f"expected cap < {max_safe}, got {result:.6f}",
    )


def test_T09_floor_at_zero() -> None:
    # confidence=0.6499 is just below the 0.65 boundary; a large boost would cross it.
    # The cap computes boundary - epsilon - confidence, which approaches 0.
    p = _make_profile(conf_buckets={"0.65-0.70": _bkt(trades=8, win_rate=0.2, total_pnl=-10)})
    result = _cap_boost_to_avoid_bad_buckets(0.6499, 0.10, p)
    _assert(
        "T09_cap_floored_at_zero",
        result >= 0.0,
        f"capped value must be >= 0, got {result}",
    )


# ── T10–T12: Builder suggest_signal_improvement integration tests ─────────────

def test_T10_builder_uses_ticker_prefix() -> None:
    # Profile: ONLY by_ticker_prefix["KXSOL15M"] has good data.
    # No conf/edge/by_ticker data. Boost must come from the prefix bucket.
    p = _make_profile(
        ticker_prefixes={"KXSOL15M": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
    )
    tmp = _tmpfile(p)
    try:
        signal = {
            "confidence": 0.72,
            "edge": 0.04,
            "ticker": "KXSOL15M-26MAY1417-T100",
            "strategy": "SIGNAL_CRYPTO",
        }
        result = suggest_signal_improvement(signal, tmp)
        boost = result.get("confidence_boost", 0.0)
        reason = result.get("reason", "")
        _assert(
            "T10_builder_uses_ticker_prefix",
            boost > 0.0 and "KXSOL15M" in reason,
            f"expected boost>0 and prefix in reason; got boost={boost}, reason={reason!r}",
        )
    finally:
        os.unlink(tmp)


def test_T11_builder_falls_back_to_full_ticker() -> None:
    # Profile: ONLY by_ticker["KXSOL15M-26MAY1417-T100"] has good data. No prefix group.
    # Boost must come from the by_ticker fallback.
    p = _make_profile(
        tickers={"KXSOL15M-26MAY1417-T100": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
    )
    tmp = _tmpfile(p)
    try:
        signal = {
            "confidence": 0.72,
            "edge": 0.04,
            "ticker": "KXSOL15M-26MAY1417-T100",
            "strategy": "SIGNAL_CRYPTO",
        }
        result = suggest_signal_improvement(signal, tmp)
        boost = result.get("confidence_boost", 0.0)
        _assert(
            "T11_builder_falls_back_to_full_ticker",
            boost > 0.0,
            f"expected positive boost from by_ticker fallback, got {boost}",
        )
    finally:
        os.unlink(tmp)


def test_T12_builder_caps_crossing_into_bad_bucket() -> None:
    # The 0.845 + 0.10 = 0.945 scenario: Builder must cap below 0.90.
    p = _make_profile(
        conf_buckets={
            "0.80-0.90": _bkt(trades=10, win_rate=0.80, total_pnl=20),
            ">=0.90": _bkt(trades=10, win_rate=0.25, total_pnl=-30),
        },
        edge_buckets={"0.03-0.05": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        ticker_prefixes={"KXBTCD": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
    )
    tmp = _tmpfile(p)
    try:
        signal = {
            "confidence": 0.845,
            "edge": 0.04,
            "ticker": "KXBTCD-26MAY1417-T77999.99",
            "strategy": "SIGNAL_CRYPTO",
        }
        result = suggest_signal_improvement(signal, tmp)
        boost = result.get("confidence_boost", 0.0)
        boosted = 0.845 + boost
        _assert(
            "T12_builder_caps_crossing_into_bad_bucket",
            boosted < 0.90,
            f"boosted confidence {boosted:.6f} must stay below 0.90; boost={boost}",
        )
    finally:
        os.unlink(tmp)


# ── T13–T14: Critic critique_signal integration tests ────────────────────────

def test_T13_critic_uses_ticker_prefix() -> None:
    # All five bucket groups are populated so no sparse penalties from conf/edge/strategy/mkt.
    # by_ticker_prefix["KXSOL15M"] has enough data → ticker not sparse → adj == 0.0.
    # normalize_strategy("SIGNAL_CRYPTO") → "SIGNAL"; _market_type → "CRYPTO".
    p = _make_profile(
        conf_buckets={"0.65-0.70": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        edge_buckets={"0.05-0.10": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        ticker_prefixes={"KXSOL15M": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        strategy={"SIGNAL": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        market_type={"CRYPTO": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
    )
    tmp = _tmpfile(p)
    try:
        signal = {
            "confidence": 0.69,
            "edge": 0.06,
            "ticker": "KXSOL15M-26MAY1417-T100",
            "strategy": "SIGNAL_CRYPTO",
            "market_type": "CRYPTO",
        }
        result = critique_signal(signal, tmp)
        adj = result.get("confidence_adjustment", -99)
        _assert(
            "T13_critic_uses_ticker_prefix",
            adj >= 0.0,
            f"expected adj >= 0 (all buckets non-sparse via prefix), got {adj}",
        )
    finally:
        os.unlink(tmp)


def test_T14_critic_falls_back_to_full_ticker() -> None:
    # No prefix group; by_ticker has the full contract name.
    # Fallback finds the data → ticker not sparse → adj == 0.0.
    p = _make_profile(
        conf_buckets={"0.65-0.70": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        edge_buckets={"0.05-0.10": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        tickers={"KXSOL15M-26MAY1417-T100": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        strategy={"SIGNAL": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
        market_type={"CRYPTO": _bkt(trades=10, win_rate=0.80, total_pnl=20)},
    )
    tmp = _tmpfile(p)
    try:
        signal = {
            "confidence": 0.69,
            "edge": 0.06,
            "ticker": "KXSOL15M-26MAY1417-T100",
            "strategy": "SIGNAL_CRYPTO",
            "market_type": "CRYPTO",
        }
        result = critique_signal(signal, tmp)
        adj = result.get("confidence_adjustment", -99)
        _assert(
            "T14_critic_falls_back_to_full_ticker",
            adj >= 0.0,
            f"expected adj >= 0 (all buckets non-sparse via fallback), got {adj}",
        )
    finally:
        os.unlink(tmp)


# ── T15–T19: Constants and safety locks ──────────────────────────────────────

def test_T15_confidence_bucket_boundaries() -> None:
    expected = (0.65, 0.70, 0.75, 0.80, 0.90)
    _assert(
        "T15_confidence_bucket_boundaries",
        _CONFIDENCE_BUCKET_BOUNDARIES == expected,
        f"expected {expected}, got {_CONFIDENCE_BUCKET_BOUNDARIES}",
    )


def test_T16_builder_min_sample_bad() -> None:
    _assert(
        "T16_builder_min_sample_bad",
        _BUILDER_MIN_SAMPLE_BAD == 5,
        f"expected 5, got {_BUILDER_MIN_SAMPLE_BAD}",
    )


def test_T17_min_edge_unchanged() -> None:
    _assert("T17_min_edge_unchanged", MIN_EDGE == 0.03, f"expected 0.03, got {MIN_EDGE}")


def test_T18_min_confidence_unchanged() -> None:
    _assert(
        "T18_min_confidence_unchanged",
        MIN_CONFIDENCE == 0.65,
        f"expected 0.65, got {MIN_CONFIDENCE}",
    )


def test_T19_max_confidence_boost_unchanged() -> None:
    _assert(
        "T19_max_confidence_boost_unchanged",
        MAX_CONFIDENCE_BOOST == 0.10,
        f"expected 0.10, got {MAX_CONFIDENCE_BOOST}",
    )


# ── T20: Read-only safety ─────────────────────────────────────────────────────

def test_T20_readonly_no_file_mutations() -> None:
    files_to_check = [
        ROOT / "config" / "trading_config.py",
        ROOT / "logs" / "paper_trades.jsonl",
        ROOT / "logs" / "execution_funnel.jsonl",
    ]
    existing = [f for f in files_to_check if f.exists()]
    if not existing:
        _ok("T20_readonly_no_file_mutations")
        return
    before = {f: f.stat().st_mtime for f in existing}
    import importlib
    import brain.builder_brain
    import brain.critic_brain
    importlib.reload(brain.builder_brain)
    importlib.reload(brain.critic_brain)
    after = {f: f.stat().st_mtime for f in existing}
    changed = [str(f) for f in existing if after[f] != before[f]]
    _assert(
        "T20_readonly_no_file_mutations",
        not changed,
        f"unexpected writes to: {changed}",
    )


# ── T21–T25: build_edge_profile.py integration tests ─────────────────────────

# build_profile() reads the live trade log and returns a dict.  It does NOT
# write to disk (that is done by main()).  Calling it here is safe and tests the
# actual code path that produces data/edge_profile.json at runtime.
# data/edge_profile.json is generated local runtime state covered by the
# data/* .gitignore pattern.  It must never be staged or committed.

_PROFILE_CACHE: dict[str, Any] | None = None


def _get_live_profile() -> dict[str, Any]:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        _PROFILE_CACHE = build_profile()
    return _PROFILE_CACHE


def test_T21_build_profile_creates_ticker_prefix_group() -> None:
    profile = _get_live_profile()
    groups = profile.get("profiles", {})
    has_group = "by_ticker_prefix" in groups
    entry_count = len(groups.get("by_ticker_prefix", {}))
    _assert(
        "T21_build_profile_creates_ticker_prefix_group",
        has_group and entry_count > 0,
        f"expected by_ticker_prefix group with entries; has_group={has_group}, entries={entry_count}",
    )


def test_T22_ticker_prefix_keys_correct_form() -> None:
    profile = _get_live_profile()
    prefix_group = profile.get("profiles", {}).get("by_ticker_prefix", {})
    if not prefix_group:
        _fail("T22_ticker_prefix_keys_correct_form", "by_ticker_prefix is empty")
        return
    # Keys must be uppercase and contain no hyphens (they are ticker.split("-")[0].upper())
    bad_keys = [k for k in prefix_group if "-" in k or k != k.upper()]
    _assert(
        "T22_ticker_prefix_keys_correct_form",
        not bad_keys,
        f"expected no-hyphen, uppercase keys; bad: {bad_keys}",
    )


def test_T23_kxeth_excluded_from_profile() -> None:
    profile = _get_live_profile()
    groups = profile.get("profiles", {})
    kxeth_in_prefix = any(k.startswith("KXETH") for k in groups.get("by_ticker_prefix", {}))
    kxeth_in_ticker = any(str(k).upper().startswith("KXETH") for k in groups.get("by_ticker", {}))
    _assert(
        "T23_kxeth_excluded_from_profile",
        not kxeth_in_prefix and not kxeth_in_ticker,
        f"KXETH must be absent; in_prefix={kxeth_in_prefix}, in_ticker={kxeth_in_ticker}",
    )


def test_T24_is_excluded_ticker_classifies_correctly() -> None:
    kxeth_ok = _is_excluded_ticker("KXETH-26MAY2025-T1")
    kxethusd_ok = _is_excluded_ticker("KXETHUSD-26JUN2025-T2500")
    kxbtcd_ok = not _is_excluded_ticker("KXBTCD-26MAY1417-T77999.99")
    kxsol_ok = not _is_excluded_ticker("KXSOL15M-26MAY1417-T100")
    none_ok = not _is_excluded_ticker(None)
    _assert(
        "T24_is_excluded_ticker_classifies_correctly",
        kxeth_ok and kxethusd_ok and kxbtcd_ok and kxsol_ok and none_ok,
        f"kxeth={kxeth_ok}, kxethusd={kxethusd_ok}, kxbtcd={kxbtcd_ok}, kxsol={kxsol_ok}, none={none_ok}",
    )


def test_T25_edge_profile_json_is_gitignored() -> None:
    import subprocess
    result = subprocess.run(
        ["git", "check-ignore", "-q", "data/edge_profile.json"],
        cwd=str(ROOT),
        capture_output=True,
    )
    _assert(
        "T25_edge_profile_json_is_gitignored",
        result.returncode == 0,
        "data/edge_profile.json must be covered by .gitignore — do NOT force-add it",
    )


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    test_T01_missing_bucket,
    test_T02_small_sample,
    test_T03_bad_win_rate,
    test_T04_negative_pnl,
    test_T05_healthy_bucket,
    test_T06_no_boundary_crossed,
    test_T07_target_bucket_healthy,
    test_T08_crossing_bad_bucket,
    test_T09_floor_at_zero,
    test_T10_builder_uses_ticker_prefix,
    test_T11_builder_falls_back_to_full_ticker,
    test_T12_builder_caps_crossing_into_bad_bucket,
    test_T13_critic_uses_ticker_prefix,
    test_T14_critic_falls_back_to_full_ticker,
    test_T15_confidence_bucket_boundaries,
    test_T16_builder_min_sample_bad,
    test_T17_min_edge_unchanged,
    test_T18_min_confidence_unchanged,
    test_T19_max_confidence_boost_unchanged,
    test_T20_readonly_no_file_mutations,
    test_T21_build_profile_creates_ticker_prefix_group,
    test_T22_ticker_prefix_keys_correct_form,
    test_T23_kxeth_excluded_from_profile,
    test_T24_is_excluded_ticker_classifies_correctly,
    test_T25_edge_profile_json_is_gitignored,
]


def run_all() -> None:
    print(f"\n=== Phase 11A Test Suite ({len(TESTS)} tests) ===\n")
    for fn in TESTS:
        try:
            fn()
        except Exception as exc:
            _fail(fn.__name__, f"exception: {exc}")

    print(f"\nResults: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("\nFailed tests:")
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)
    print(f"\n{SENTINEL}")


if __name__ == "__main__":
    run_all()
