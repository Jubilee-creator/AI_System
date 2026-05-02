#!/usr/bin/env python3
"""
tools/test_edge_trust_gate.py
------------------------------
Verifies the edge trust gate state and its structural correctness.

Tests:
  1. Edge profile exists and is parseable
  2. edge_profile_health reports correct gate state (sample sizes vs thresholds)
  3. BOOTSTRAP_ALLOW_ENABLED = True (Phase 6B-2 deadlock fix is active)
  4. DATA_COLLECTION_OVERRIDE_ENABLED = False (trust deadlock guard is off)
  5. Trust gate thresholds are sane (config constants)
  6. data_collection_override trades are correctly EXCLUDED from normal proof
  7. bootstrap_provisional trades are correctly EXCLUDED from normal proof
  8. bootstrap_era_allow trades are correctly INCLUDED in normal proof
  9. edge_profile_health() function is consistent with build_edge_profile counts
 10. Bootstrap min thresholds are >= normal thresholds where required

Expected result: PROVEN_EDGE_TRUST_GATE_OK
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── 1. Edge profile exists and is readable ────────────────────────────────────

def test_edge_profile_exists() -> str | None:
    path = ROOT / "data" / "edge_profile.json"
    if not path.exists():
        return f"edge_profile.json not found at {path}"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return f"edge_profile.json is not valid JSON: {exc}"
    if not data:
        return "edge_profile.json is empty"
    if "edge_profile_health" not in data:
        return "edge_profile.json missing 'edge_profile_health' key"
    return None


# ── 2. Gate state reflects actual sample counts ───────────────────────────────

def test_gate_state_reflects_counts() -> str | None:
    path = ROOT / "data" / "edge_profile.json"
    if not path.exists():
        return "edge_profile.json not found"
    data = json.loads(path.read_text())
    health = data.get("edge_profile_health", {})
    thresholds = health.get("thresholds", {})

    cs = int(data.get("clean_settled_trades", 0))
    mf = int(data.get("modern_full_metadata_trades", 0))
    nm = int(data.get("normal_council_approved_modern_trades", 0))

    req_cs = int(thresholds.get("min_clean_settled", 30))
    req_mf = int(thresholds.get("min_modern_full", 30))
    req_nm = int(thresholds.get("min_normal_modern", 10))

    sample_ok = cs >= req_cs and mf >= req_mf and nm >= req_nm
    reported_ok = health.get("edge_profile_sample_ok", False)

    if sample_ok != reported_ok:
        return (
            f"edge_profile_sample_ok={reported_ok} but computed sample_ok={sample_ok} "
            f"(cs={cs}/{req_cs}, mf={mf}/{req_mf}, nm={nm}/{req_nm})"
        )

    trusted = health.get("edge_profile_trusted", False)
    if trusted and not sample_ok:
        return (
            f"edge_profile_trusted=True but sample_ok={sample_ok} — "
            f"trust reported without meeting sample gates"
        )
    return None


# ── 3. BOOTSTRAP_ALLOW_ENABLED = True ─────────────────────────────────────────

def test_bootstrap_allow_is_on() -> str | None:
    from config.trading_config import BOOTSTRAP_ALLOW_ENABLED
    if not BOOTSTRAP_ALLOW_ENABLED:
        return (
            "BOOTSTRAP_ALLOW_ENABLED=False — Phase 6B-2 trust deadlock fix is disabled. "
            "bootstrap_era_allow signals cannot accumulate and trust gate is permanently blocked."
        )
    return None


# ── 4. DATA_COLLECTION_OVERRIDE_ENABLED = False ───────────────────────────────

def test_dc_override_is_off() -> str | None:
    from config.trading_config import DATA_COLLECTION_OVERRIDE_ENABLED
    if DATA_COLLECTION_OVERRIDE_ENABLED:
        return (
            "DATA_COLLECTION_OVERRIDE_ENABLED=True — trust deadlock guard is active. "
            "Council BLOCKs are overridden and trades may bypass the normal proof path."
        )
    return None


# ── 5. Trust gate threshold sanity ────────────────────────────────────────────

def test_trust_gate_thresholds_sane() -> str | None:
    from config.trading_config import (
        EDGE_PROFILE_MAX_AGE_HOURS,
        EDGE_PROFILE_MIN_CLEAN_SETTLED,
        EDGE_PROFILE_MIN_MODERN_FULL,
        EDGE_PROFILE_MIN_NORMAL_MODERN,
    )
    errors = []
    if not (5 <= EDGE_PROFILE_MIN_CLEAN_SETTLED <= 500):
        errors.append(f"EDGE_PROFILE_MIN_CLEAN_SETTLED={EDGE_PROFILE_MIN_CLEAN_SETTLED} out of sane range")
    if not (5 <= EDGE_PROFILE_MIN_MODERN_FULL <= 500):
        errors.append(f"EDGE_PROFILE_MIN_MODERN_FULL={EDGE_PROFILE_MIN_MODERN_FULL} out of sane range")
    if not (1 <= EDGE_PROFILE_MIN_NORMAL_MODERN <= EDGE_PROFILE_MIN_MODERN_FULL):
        errors.append(
            f"EDGE_PROFILE_MIN_NORMAL_MODERN={EDGE_PROFILE_MIN_NORMAL_MODERN} should be in "
            f"[1, EDGE_PROFILE_MIN_MODERN_FULL={EDGE_PROFILE_MIN_MODERN_FULL}]"
        )
    if not (1 <= EDGE_PROFILE_MAX_AGE_HOURS <= 720):
        errors.append(f"EDGE_PROFILE_MAX_AGE_HOURS={EDGE_PROFILE_MAX_AGE_HOURS} out of sane range (1–720h)")
    return "; ".join(errors) if errors else None


# ── 6. data_collection_override excluded from normal proof ────────────────────

def test_dc_override_excluded_from_normal_proof() -> str | None:
    path = ROOT / "data" / "edge_profile.json"
    if not path.exists():
        return "edge_profile.json not found"
    data = json.loads(path.read_text())
    mf      = int(data.get("modern_full_metadata_trades", 0))
    dc      = int(data.get("data_collection_override_count", 0))
    bp      = int(data.get("bootstrap_provisional_count", 0))
    normal  = int(data.get("normal_council_approved_modern_trades", 0))

    expected = max(0, mf - dc - bp)
    if normal != expected:
        return (
            f"normal_council_approved_modern={normal} but expected {expected} "
            f"(modern_full={mf} - dc={dc} - provisional={bp}). "
            "data_collection_override may not be excluded correctly."
        )
    return None


# ── 7. bootstrap_provisional excluded from normal proof ───────────────────────

def test_bootstrap_provisional_excluded() -> str | None:
    path = ROOT / "data" / "edge_profile.json"
    if not path.exists():
        return "edge_profile.json not found"
    data = json.loads(path.read_text())
    bp     = int(data.get("bootstrap_provisional_count", 0))
    normal = int(data.get("normal_council_approved_modern_trades", 0))
    mf     = int(data.get("modern_full_metadata_trades", 0))
    dc     = int(data.get("data_collection_override_count", 0))

    # If ALL modern trades are bootstrap_provisional and no dc, normal must be 0
    if mf > 0 and dc == 0 and bp == mf and normal > 0:
        return (
            f"normal_council_approved_modern={normal} but all {mf} modern trades are "
            "bootstrap_provisional — they should not count as normal proof"
        )
    return None


# ── 8. bootstrap_era_allow INCLUDED in normal proof ───────────────────────────

def test_bootstrap_era_allow_counted_as_normal() -> str | None:
    """
    Verify: if era_allow > 0, the count is reflected in normal_council_approved_modern.
    Formula: normal = max(0, modern_full - dc - bootstrap_provisional)
    bootstrap_era_allow trades are NOT subtracted — they count as normal.
    """
    path = ROOT / "data" / "edge_profile.json"
    if not path.exists():
        return "edge_profile.json not found"
    data = json.loads(path.read_text())
    era    = int(data.get("bootstrap_era_allow_count", 0))
    mf     = int(data.get("modern_full_metadata_trades", 0))
    dc     = int(data.get("data_collection_override_count", 0))
    bp     = int(data.get("bootstrap_provisional_count", 0))
    normal = int(data.get("normal_council_approved_modern_trades", 0))

    if era == 0:
        # Cannot verify inclusion if there are none yet
        return None

    # era_allow should be >= normal (it contributes to it, not excluded)
    expected_min_normal = era
    if normal < expected_min_normal:
        return (
            f"bootstrap_era_allow_count={era} but normal_council_approved_modern={normal} — "
            f"era_allow trades should count as normal proof (normal should be >= {expected_min_normal})"
        )
    return None


# ── 9. edge_profile_health() is consistent with stored profile ────────────────

def test_edge_profile_health_function_consistent() -> str | None:
    """Re-run edge_profile_health() on the stored profile and compare to stored result."""
    from brain.edge_profile_health import edge_profile_health

    path = ROOT / "data" / "edge_profile.json"
    if not path.exists():
        return "edge_profile.json not found"
    data = json.loads(path.read_text())

    fresh_health = edge_profile_health(data, path)
    stored_health = data.get("edge_profile_health", {})

    for key in ("edge_profile_sample_ok", "edge_profile_trusted",
                "clean_settled_trades", "modern_full_metadata_trades",
                "normal_council_approved_modern_trades"):
        fresh_val = fresh_health.get(key)
        stored_val = stored_health.get(key)
        if fresh_val != stored_val:
            return (
                f"edge_profile_health()['{key}'] = {fresh_val!r} but stored = {stored_val!r}; "
                "profile may be stale — run build_edge_profile.py"
            )
    return None


# ── 10. Bootstrap min thresholds vs normal thresholds ─────────────────────────

def test_bootstrap_thresholds_vs_normal() -> str | None:
    from config.trading_config import (
        BOOTSTRAP_MIN_CONFIDENCE,
        BOOTSTRAP_MIN_EDGE,
        MIN_CONFIDENCE,
        MIN_EDGE,
    )
    errors = []
    if BOOTSTRAP_MIN_EDGE < MIN_EDGE:
        errors.append(
            f"BOOTSTRAP_MIN_EDGE={BOOTSTRAP_MIN_EDGE} < MIN_EDGE={MIN_EDGE} — "
            "bootstrap bar is lower than the normal signal minimum"
        )
    if not (0.5 < BOOTSTRAP_MIN_CONFIDENCE <= 1.0):
        errors.append(
            f"BOOTSTRAP_MIN_CONFIDENCE={BOOTSTRAP_MIN_CONFIDENCE} out of range (0.5, 1.0]"
        )
    return "; ".join(errors) if errors else None


# ── Runner ─────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("edge profile exists and parseable",         test_edge_profile_exists),
        ("gate state reflects actual counts",          test_gate_state_reflects_counts),
        ("BOOTSTRAP_ALLOW_ENABLED = True",             test_bootstrap_allow_is_on),
        ("DATA_COLLECTION_OVERRIDE_ENABLED = False",   test_dc_override_is_off),
        ("trust gate thresholds sane",                 test_trust_gate_thresholds_sane),
        ("dc_override excluded from normal proof",     test_dc_override_excluded_from_normal_proof),
        ("bootstrap_provisional excluded",             test_bootstrap_provisional_excluded),
        ("bootstrap_era_allow counts as normal",       test_bootstrap_era_allow_counted_as_normal),
        ("edge_profile_health() consistent",           test_edge_profile_health_function_consistent),
        ("bootstrap thresholds vs normal thresholds",  test_bootstrap_thresholds_vs_normal),
    ]

    fails: list[str] = []

    print("=" * 70)
    print("TEST: EDGE TRUST GATE INTEGRITY")
    print("=" * 70)

    for name, fn in tests:
        err = fn()
        if err is None:
            print(f"[PASS] {name}")
        else:
            print(f"[FAIL] {name}")
            print(f"       {err}")
            fails.append(f"{name}: {err}")

    path = ROOT / "data" / "edge_profile.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            health = data.get("edge_profile_health", {})
            thresholds = health.get("thresholds", {})
            print()
            print("CURRENT GATE STATE:")
            print(f"  edge_profile_trusted:              {health.get('edge_profile_trusted')}")
            print(f"  clean_settled_trades:              {data.get('clean_settled_trades', 0)}"
                  f"  (need {thresholds.get('min_clean_settled', 30)})")
            print(f"  modern_full_metadata_trades:       {data.get('modern_full_metadata_trades', 0)}"
                  f"  (need {thresholds.get('min_modern_full', 30)})")
            print(f"  normal_council_approved_modern:    {data.get('normal_council_approved_modern_trades', 0)}"
                  f"  (need {thresholds.get('min_normal_modern', 10)})")
            print(f"  data_collection_override_count:    {data.get('data_collection_override_count', 0)}")
            print(f"  bootstrap_provisional_count:       {data.get('bootstrap_provisional_count', 0)}")
            print(f"  bootstrap_era_allow_count:         {data.get('bootstrap_era_allow_count', 0)}")
            print(f"  trust reason:  {health.get('reason', 'n/a')}")
        except Exception:
            pass

    print()
    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
        print("RESULT: FAILED")
        sys.exit(1)
    else:
        print("RESULT: PROVEN_EDGE_TRUST_GATE_OK")


if __name__ == "__main__":
    main()
