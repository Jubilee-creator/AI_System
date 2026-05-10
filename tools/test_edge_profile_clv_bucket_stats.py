#!/usr/bin/env python3
"""
tools/test_edge_profile_clv_bucket_stats.py
--------------------------------------------
Phase 9J test suite — CLV Bucket Statistics.

Verifies:
  1.  profile_has_clv_bucket_stats=True in metadata
  2.  All 1D bucket types carry CLV fields
  3.  by_edge_price_bucket (2D) cells carry CLV fields
  4.  CLV fields have the correct structure (not empty dicts, correct types)
  5.  clv_count == trades for by_edge_bucket (all records have computable CLV)
  6.  positive_clv_count == wins (binary contract integrity)
  7.  negative_clv_count == losses (binary contract integrity)
  8.  positive_clv_count + negative_clv_count == clv_count (no zero-CLV phantom)
  9.  avg_clv × clv_count ≈ total_clv (math consistency within rounding)
 10.  overall bucket CLV fields present and consistent
 11.  Sweet-spot 2D cell avg_clv > 0
 12.  0.05-0.10 by_edge_bucket avg_clv sign matches expected payoff-structure finding
 13.  0.10-0.25 by_edge_bucket avg_clv is negative (model quality problem)
 14.  by_original_edge_bucket 0.05-0.10 avg_clv present and non-None
 15.  by_normal_modern_edge_bucket CLV fields present
 16.  No CLV field is present on buckets with zero trades
 17.  Critic brain does NOT reference any CLV field by string literal
 18.  Builder brain does NOT reference any CLV field by string literal
 19.  Safety locks unchanged: real_money_allowed=False, scale_allowed=False
 20.  MIN_EDGE=0.03, MIN_CONFIDENCE=0.65 unchanged

NOTE: Tests 2–16 require the profile to be rebuilt first:
  python3 tools/build_edge_profile.py

Expected result when all pass: PROVEN_CLV_BUCKET_STATS_OK
"""
from __future__ import annotations

import ast
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE_PATH = ROOT / "data" / "edge_profile.json"
CRITIC_PATH  = ROOT / "brain" / "critic_brain.py"
BUILDER_PATH = ROOT / "brain" / "builder_brain.py"

_CLV_FIELDS = ("clv_count", "avg_clv", "total_clv",
               "positive_clv_count", "negative_clv_count", "positive_clv_rate")


def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text())
    except Exception:
        return {}


def _has_clv_fields(bkt: dict) -> bool:
    return all(f in bkt for f in _CLV_FIELDS)


# ── 1. Metadata flag ──────────────────────────────────────────────────────────

def test_metadata_flag() -> Optional[str]:
    """profile_has_clv_bucket_stats must be True in top-level metadata."""
    profile = _load_profile()
    if not profile:
        return "data/edge_profile.json missing or empty — run build_edge_profile.py"
    if not profile.get("profile_has_clv_bucket_stats"):
        return (
            "profile_has_clv_bucket_stats not True — "
            "run: python3 tools/build_edge_profile.py"
        )
    return None


# ── 2. 1D bucket types carry CLV fields ──────────────────────────────────────

def test_1d_buckets_have_clv_fields() -> Optional[str]:
    """All cells in every 1D bucket group must carry CLV fields."""
    profile = _load_profile()
    profiles = profile.get("profiles", {})
    _1d_names = [
        "by_edge_bucket", "by_confidence_bucket", "by_action_type",
        "by_market_type", "by_strategy", "by_ticker",
        "by_normal_modern_edge_bucket", "by_original_edge_bucket",
    ]
    for name in _1d_names:
        group = profiles.get(name, {})
        for key, bkt in group.items():
            if not isinstance(bkt, dict):
                continue
            if not _has_clv_fields(bkt):
                missing = [f for f in _CLV_FIELDS if f not in bkt]
                return (
                    f"{name}['{key}'] missing CLV fields: {missing} — "
                    "rebuild profile first"
                )
    return None


# ── 3. 2D cells carry CLV fields ─────────────────────────────────────────────

def test_2d_cells_have_clv_fields() -> Optional[str]:
    """All cells in by_edge_price_bucket must carry CLV fields."""
    profile = _load_profile()
    table = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    if not table:
        return "by_edge_price_bucket missing or empty — rebuild profile first"
    for key, bkt in table.items():
        if not isinstance(bkt, dict):
            continue
        if not _has_clv_fields(bkt):
            missing = [f for f in _CLV_FIELDS if f not in bkt]
            return f"by_edge_price_bucket['{key}'] missing CLV fields: {missing}"
    return None


# ── 4. CLV field types ────────────────────────────────────────────────────────

def test_clv_field_types() -> Optional[str]:
    """CLV fields must be int/float/None — never dicts or unexpected strings."""
    profile = _load_profile()
    profiles = profile.get("profiles", {})
    for group_name, group in profiles.items():
        if not isinstance(group, dict):
            continue
        for cell_key, bkt in group.items():
            if not isinstance(bkt, dict):
                continue
            for field in _CLV_FIELDS:
                if field not in bkt:
                    return f"{group_name}['{cell_key}']['{field}'] absent"
                val = bkt[field]
                if val is not None and not isinstance(val, (int, float)):
                    return (
                        f"{group_name}['{cell_key}']['{field}']={val!r} "
                        f"has unexpected type {type(val).__name__}"
                    )
    return None


# ── 5. clv_count == trades (all records have CLV) ────────────────────────────

def test_clv_count_equals_trades_in_edge_bucket() -> Optional[str]:
    """by_edge_bucket cells: clv_count must equal trades (all records have computable CLV)."""
    profile = _load_profile()
    group = profile.get("profiles", {}).get("by_edge_bucket", {})
    if not group:
        return "by_edge_bucket missing — rebuild profile first"
    for key, bkt in group.items():
        n      = bkt.get("trades", 0)
        clv_n  = bkt.get("clv_count")
        if n == 0:
            continue
        if clv_n is None or clv_n != n:
            return (
                f"by_edge_bucket['{key}'] trades={n} but clv_count={clv_n} — "
                "some records lack computable CLV (check exit_price field)"
            )
    return None


# ── 6. positive_clv_count == wins ────────────────────────────────────────────

def test_positive_clv_count_equals_wins() -> Optional[str]:
    """positive_clv_count must equal wins in every by_edge_bucket cell (binary contract)."""
    profile = _load_profile()
    group = profile.get("profiles", {}).get("by_edge_bucket", {})
    if not group:
        return "by_edge_bucket missing"
    for key, bkt in group.items():
        wins    = bkt.get("wins", 0)
        pos_clv = bkt.get("positive_clv_count")
        if bkt.get("trades", 0) == 0:
            continue
        if pos_clv != wins:
            return (
                f"by_edge_bucket['{key}'] wins={wins} but positive_clv_count={pos_clv} — "
                "binary contract invariant violated"
            )
    return None


# ── 7. negative_clv_count == losses ──────────────────────────────────────────

def test_negative_clv_count_equals_losses() -> Optional[str]:
    """negative_clv_count must equal losses in every by_edge_bucket cell."""
    profile = _load_profile()
    group = profile.get("profiles", {}).get("by_edge_bucket", {})
    if not group:
        return "by_edge_bucket missing"
    for key, bkt in group.items():
        losses  = bkt.get("losses", 0)
        neg_clv = bkt.get("negative_clv_count")
        if bkt.get("trades", 0) == 0:
            continue
        if neg_clv != losses:
            return (
                f"by_edge_bucket['{key}'] losses={losses} but negative_clv_count={neg_clv} — "
                "binary contract invariant violated"
            )
    return None


# ── 8. pos + neg == clv_count ────────────────────────────────────────────────

def test_clv_count_decomposition() -> Optional[str]:
    """positive_clv_count + negative_clv_count must equal clv_count in by_edge_bucket."""
    profile = _load_profile()
    group = profile.get("profiles", {}).get("by_edge_bucket", {})
    if not group:
        return "by_edge_bucket missing"
    for key, bkt in group.items():
        clv_n  = bkt.get("clv_count")
        clv_p  = bkt.get("positive_clv_count")
        clv_ng = bkt.get("negative_clv_count")
        if clv_n is None:
            continue
        if clv_p + clv_ng != clv_n:
            return (
                f"by_edge_bucket['{key}'] pos({clv_p})+neg({clv_ng}) != clv_count({clv_n}) — "
                "some trades have exactly zero CLV (unexpected for binary)"
            )
    return None


# ── 9. avg_clv × clv_count ≈ total_clv ──────────────────────────────────────

def test_clv_math_consistency() -> Optional[str]:
    """avg_clv * clv_count must ≈ total_clv (within rounding) for all by_edge_bucket cells."""
    profile = _load_profile()
    group = profile.get("profiles", {}).get("by_edge_bucket", {})
    if not group:
        return "by_edge_bucket missing"
    for key, bkt in group.items():
        avg  = bkt.get("avg_clv")
        n    = bkt.get("clv_count")
        tot  = bkt.get("total_clv")
        if avg is None or n is None or tot is None:
            continue
        expected = round(avg * n, 6)
        if abs(expected - tot) > 0.01:
            return (
                f"by_edge_bucket['{key}'] avg_clv({avg}) × clv_count({n}) = {expected} "
                f"but total_clv={tot} (delta={abs(expected-tot):.4f})"
            )
    return None


# ── 10. Overall bucket CLV ────────────────────────────────────────────────────

def test_overall_bucket_has_clv() -> Optional[str]:
    """Top-level 'overall' bucket must carry CLV fields."""
    profile = _load_profile()
    overall = profile.get("overall", {})
    if not overall:
        return "overall bucket missing from profile"
    if not _has_clv_fields(overall):
        missing = [f for f in _CLV_FIELDS if f not in overall]
        return f"overall bucket missing CLV fields: {missing}"
    n   = overall.get("clv_count")
    tot = overall.get("total_clv")
    if n is None or n == 0:
        return "overall clv_count is None or 0 — all records should have CLV"
    if tot is None:
        return "overall total_clv is None"
    return None


# ── 11. Sweet-spot 2D cell avg_clv > 0 ───────────────────────────────────────

def test_sweetspot_2d_avg_clv_positive() -> Optional[str]:
    """Sweet-spot cell '0.05-0.10|0.80-0.90' must have avg_clv > 0."""
    profile = _load_profile()
    table = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    cell_key = "0.05-0.10|0.80-0.90"
    cell = table.get(cell_key)
    if cell is None:
        return f"cell '{cell_key}' not found — rebuild profile first"
    avg_clv = cell.get("avg_clv")
    if avg_clv is None:
        return f"sweet-spot cell avg_clv is None"
    if avg_clv <= 0:
        return (
            f"sweet-spot cell avg_clv={avg_clv:.4f} <= 0 — "
            "high-price zone should show positive CLV (model correct)"
        )
    return None


# ── 12. 0.05-0.10 by_edge_bucket avg_clv payoff-structure finding ─────────────

def test_edge_bucket_0510_clv_positive_pnl_negative() -> Optional[str]:
    """
    0.05-0.10 by_edge_bucket: avg_clv should be positive (model directionally correct)
    while total_pnl is negative (payoff structure problem, not model quality problem).
    """
    profile = _load_profile()
    group = profile.get("profiles", {}).get("by_edge_bucket", {})
    bkt = group.get("0.05-0.10")
    if bkt is None:
        return "by_edge_bucket['0.05-0.10'] not found — rebuild profile"
    avg_clv = bkt.get("avg_clv")
    total_pnl = bkt.get("total_pnl", 0.0)
    n = bkt.get("trades", 0)
    if n < 5:
        return f"0.05-0.10 bucket n={n} < 5 — insufficient data to validate"
    if avg_clv is None:
        return "0.05-0.10 avg_clv is None"
    if avg_clv <= 0:
        return (
            f"0.05-0.10 avg_clv={avg_clv:.4f} <= 0 — expected positive "
            "(payoff structure problem, not model quality)"
        )
    if total_pnl >= 0:
        return (
            f"0.05-0.10 total_pnl={total_pnl:.2f} >= 0 — expected negative "
            "(payoff structure problem signature: clv>0 but pnl<0)"
        )
    return None


# ── 13. 0.10-0.25 by_edge_bucket avg_clv negative ────────────────────────────

def test_edge_bucket_1025_clv_negative() -> Optional[str]:
    """
    0.10-0.25 by_edge_bucket: avg_clv should be negative (model quality problem).
    Both avg_clv < 0 AND pnl < 0 = wrong directionally, not just overpriced entry.
    """
    profile = _load_profile()
    group = profile.get("profiles", {}).get("by_edge_bucket", {})
    bkt = group.get("0.10-0.25")
    if bkt is None:
        return None  # bucket may be absent if no trades in range — skip
    n = bkt.get("trades", 0)
    if n < 3:
        return None  # too small to validate
    avg_clv = bkt.get("avg_clv")
    if avg_clv is None:
        return "0.10-0.25 avg_clv is None"
    if avg_clv >= 0:
        return (
            f"0.10-0.25 avg_clv={avg_clv:.4f} >= 0 — expected negative "
            "(model quality problem expected in this bucket)"
        )
    return None


# ── 14. by_original_edge_bucket 0.05-0.10 avg_clv present ───────────────────

def test_original_edge_shadow_clv_present() -> Optional[str]:
    """by_original_edge_bucket '0.05-0.10' must have non-None avg_clv."""
    profile = _load_profile()
    shadow = profile.get("profiles", {}).get("by_original_edge_bucket", {})
    bkt = shadow.get("0.05-0.10")
    if bkt is None:
        return "by_original_edge_bucket['0.05-0.10'] not found — rebuild profile"
    avg_clv = bkt.get("avg_clv")
    if avg_clv is None:
        return "by_original_edge_bucket['0.05-0.10'] avg_clv is None"
    return None


# ── 15. by_normal_modern_edge_bucket CLV fields ───────────────────────────────

def test_normal_modern_shadow_has_clv() -> Optional[str]:
    """All cells in by_normal_modern_edge_bucket must carry CLV fields."""
    profile = _load_profile()
    group = profile.get("profiles", {}).get("by_normal_modern_edge_bucket", {})
    if not group:
        return "by_normal_modern_edge_bucket missing or empty — rebuild profile"
    for key, bkt in group.items():
        if not _has_clv_fields(bkt):
            missing = [f for f in _CLV_FIELDS if f not in bkt]
            return f"by_normal_modern_edge_bucket['{key}'] missing: {missing}"
    return None


# ── 16. Zero-trade buckets have None CLV fields ───────────────────────────────

def test_zero_trade_buckets_have_none_clv() -> Optional[str]:
    """Buckets with trades=0 must have clv_count=None (no spurious zeros)."""
    profile = _load_profile()
    profiles = profile.get("profiles", {})
    for group_name, group in profiles.items():
        if not isinstance(group, dict):
            continue
        for cell_key, bkt in group.items():
            if not isinstance(bkt, dict):
                continue
            if bkt.get("trades", 0) != 0:
                continue
            for field in _CLV_FIELDS:
                if bkt.get(field) is not None:
                    return (
                        f"{group_name}['{cell_key}']['{field}']={bkt[field]!r} "
                        "should be None for a zero-trade bucket"
                    )
    return None


# ── 17. Critic does NOT use CLV fields ───────────────────────────────────────

def test_critic_does_not_use_clv_fields() -> Optional[str]:
    """critic_brain.py must NOT reference CLV field names — CLV is diagnostic only."""
    source = CRITIC_PATH.read_text()
    for field in _CLV_FIELDS:
        if field in source:
            return (
                f"critic_brain.py references '{field}' — "
                "CLV fields must NOT influence trading decisions"
            )
    return None


# ── 18. Builder does NOT use CLV fields ──────────────────────────────────────

def test_builder_does_not_use_clv_fields() -> Optional[str]:
    """builder_brain.py must NOT reference CLV field names — CLV is diagnostic only."""
    if not BUILDER_PATH.exists():
        return None  # Builder may not exist in all configurations
    source = BUILDER_PATH.read_text()
    for field in _CLV_FIELDS:
        if field in source:
            return (
                f"builder_brain.py references '{field}' — "
                "CLV fields must NOT influence trading decisions"
            )
    return None


# ── 19. Safety locks ─────────────────────────────────────────────────────────

def test_real_money_stays_locked() -> Optional[str]:
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades
    records = load_trades()
    buckets = classify_records(records)
    gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gate.get("real_money_allowed") is not False:
        return f"real_money_allowed is not False: {gate.get('real_money_allowed')!r}"
    return None


def test_scale_stays_locked() -> Optional[str]:
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades
    records = load_trades()
    buckets = classify_records(records)
    gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gate.get("scale_allowed") is not False:
        return f"scale_allowed is not False: {gate.get('scale_allowed')!r}"
    return None


# ── 20. Thresholds unchanged ─────────────────────────────────────────────────

def test_min_edge_unchanged() -> Optional[str]:
    from config.trading_config import MIN_EDGE
    if abs(MIN_EDGE - 0.03) > 1e-9:
        return f"MIN_EDGE changed: {MIN_EDGE} (expected 0.03)"
    return None


def test_min_confidence_unchanged() -> Optional[str]:
    from config.trading_config import MIN_CONFIDENCE
    if abs(MIN_CONFIDENCE - 0.65) > 1e-9:
        return f"MIN_CONFIDENCE changed: {MIN_CONFIDENCE} (expected 0.65)"
    return None


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("profile_has_clv_bucket_stats=True in metadata",
         test_metadata_flag),
        ("all 1D bucket groups carry CLV fields",
         test_1d_buckets_have_clv_fields),
        ("all 2D by_edge_price_bucket cells carry CLV fields",
         test_2d_cells_have_clv_fields),
        ("CLV field types are int/float/None",
         test_clv_field_types),
        ("clv_count == trades in by_edge_bucket",
         test_clv_count_equals_trades_in_edge_bucket),
        ("positive_clv_count == wins in by_edge_bucket",
         test_positive_clv_count_equals_wins),
        ("negative_clv_count == losses in by_edge_bucket",
         test_negative_clv_count_equals_losses),
        ("positive_clv_count + negative_clv_count == clv_count",
         test_clv_count_decomposition),
        ("avg_clv × clv_count ≈ total_clv (math consistency)",
         test_clv_math_consistency),
        ("overall bucket has CLV fields and non-zero clv_count",
         test_overall_bucket_has_clv),
        ("sweet-spot 2D cell '0.05-0.10|0.80-0.90' avg_clv > 0",
         test_sweetspot_2d_avg_clv_positive),
        ("0.05-0.10 by_edge_bucket: avg_clv>0 AND pnl<0 (payoff-structure problem)",
         test_edge_bucket_0510_clv_positive_pnl_negative),
        ("0.10-0.25 by_edge_bucket: avg_clv < 0 (model quality problem)",
         test_edge_bucket_1025_clv_negative),
        ("by_original_edge_bucket '0.05-0.10' avg_clv non-None",
         test_original_edge_shadow_clv_present),
        ("by_normal_modern_edge_bucket cells carry CLV fields",
         test_normal_modern_shadow_has_clv),
        ("zero-trade buckets have None CLV fields",
         test_zero_trade_buckets_have_none_clv),
        ("critic_brain.py does NOT reference CLV fields",
         test_critic_does_not_use_clv_fields),
        ("builder_brain.py does NOT reference CLV fields",
         test_builder_does_not_use_clv_fields),
        ("real_money_allowed=False",
         test_real_money_stays_locked),
        ("scale_allowed=False",
         test_scale_stays_locked),
        ("MIN_EDGE unchanged at 0.03",
         test_min_edge_unchanged),
        ("MIN_CONFIDENCE unchanged at 0.65",
         test_min_confidence_unchanged),
    ]

    passed = 0
    failed = 0
    print("=" * 68)
    print("CLV BUCKET STATS TEST SUITE — Phase 9J")
    print("=" * 68)
    print("NOTE: Tests 2–16 require: python3 tools/build_edge_profile.py first")
    print()
    for name, fn in tests:
        err = fn()
        if err is None:
            print(f"  [PASS]  {name}")
            passed += 1
        else:
            print(f"  [FAIL]  {name}")
            print(f"          {err}")
            failed += 1

    print()
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    print()
    if failed == 0:
        print("PROVEN_CLV_BUCKET_STATS_OK")
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
