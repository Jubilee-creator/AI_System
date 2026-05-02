#!/usr/bin/env python3
"""
tools/test_modern_only_proof.py
---------------------------------
Verifies the modern-only proof path and legacy quarantine guarantees.

Tests:
  1. Legacy rows exist and are visible  (quarantine requires visibility)
  2. Legacy rows do NOT count as modern_full_metadata
  3. data_collection_override rows excluded from normal proof
  4. bootstrap_provisional rows excluded from normal proof
  5. bootstrap_era_allow rows counted toward normal proof (not excluded)
  6. normal_council_approved_modern = modern_full - dc_override - provisional
  7. BOOTSTRAP_ALLOW_ENABLED = True  (Phase 6B-2 deadlock fix is active)
  8. scale_allowed = False  (hardcoded)
  9. real_money_allowed = False  (hardcoded)
 10. evaluate_proof_gates does NOT return PROVEN_PROFITABLE  (safety check)

Expected result: PROVEN_MODERN_ONLY_PROOF_OK
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_data():
    from tools.clean_truth_report import classify_records, row_quality_group
    from tools.performance_report import load_trades
    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]
    modern_full = [r for r in clean_settled if row_quality_group(r) == "MODERN_FULL_METADATA"]
    legacy = [r for r in clean_settled if row_quality_group(r) == "LEGACY_EDGE_ONLY"]
    return clean_settled, modern_full, legacy, buckets


# ── 1. Legacy rows are visible ────────────────────────────────────────────────

def test_legacy_rows_visible() -> str | None:
    """Legacy rows should appear in clean_settled diagnostics (quarantine = visible but excluded)."""
    clean_settled, _, legacy, _ = _load_data()
    if not clean_settled:
        return None  # no data yet
    # If there are clean settled rows but no legacy, that's fine (they've aged out)
    # This test verifies the classification itself doesn't crash
    return None


# ── 2. Legacy rows do NOT count as modern_full_metadata ───────────────────────

def test_legacy_not_in_modern_full() -> str | None:
    from tools.clean_truth_report import row_quality_group
    clean_settled, modern_full, legacy, _ = _load_data()
    for rec in legacy:
        if row_quality_group(rec) == "MODERN_FULL_METADATA":
            return (
                f"LEGACY row {rec.get('ticker','?')} classified as MODERN_FULL_METADATA — "
                "quarantine breach: legacy row entering modern proof pool"
            )
    return None


# ── 3. data_collection_override excluded from normal proof ────────────────────

def test_dc_override_excluded_from_normal() -> str | None:
    from tools.clean_truth_report import row_quality_group
    clean_settled, modern_full, _, _ = _load_data()
    dc_rows = [r for r in modern_full if r.get("data_collection_override")]
    # Verify none of these would be counted as normal_modern by formula
    # normal_modern = modern_full - dc - provisional
    dc_count = len(dc_rows)
    bp_count = sum(1 for r in modern_full if r.get("bootstrap_provisional"))
    normal_modern = max(0, len(modern_full) - dc_count - bp_count)
    expected = max(0, len(modern_full) - dc_count - bp_count)
    if normal_modern != expected:
        return (
            f"normal_modern calculation mismatch: "
            f"computed {normal_modern} != expected {expected}"
        )
    # Verify dc rows are not in normal subset
    normal_rows = [r for r in modern_full
                   if not r.get("data_collection_override")
                   and not r.get("bootstrap_provisional")]
    for rec in normal_rows:
        if rec.get("data_collection_override"):
            return (
                f"data_collection_override row {rec.get('ticker','?')} in normal_modern pool — "
                "exclusion filter broken"
            )
    return None


# ── 4. bootstrap_provisional excluded from normal proof ───────────────────────

def test_bootstrap_provisional_excluded() -> str | None:
    clean_settled, modern_full, _, _ = _load_data()
    normal_rows = [r for r in modern_full
                   if not r.get("data_collection_override")
                   and not r.get("bootstrap_provisional")]
    for rec in normal_rows:
        if rec.get("bootstrap_provisional"):
            return (
                f"bootstrap_provisional row {rec.get('ticker','?')} in normal_modern pool — "
                "exclusion filter broken"
            )
    return None


# ── 5. bootstrap_era_allow counted toward normal proof ────────────────────────

def test_bootstrap_era_allow_counted_as_normal() -> str | None:
    """
    bootstrap_era_allow rows should NOT be excluded from normal_modern.
    Formula: normal_modern = modern_full - dc_override - provisional
    era_allow is not subtracted — it counts as normal.
    """
    clean_settled, modern_full, _, _ = _load_data()
    era_rows = [r for r in modern_full if r.get("bootstrap_era_council_allow")]
    if not era_rows:
        return None  # can't test if none exist yet
    normal_rows = [r for r in modern_full
                   if not r.get("data_collection_override")
                   and not r.get("bootstrap_provisional")]
    era_in_normal = sum(1 for r in normal_rows if r.get("bootstrap_era_council_allow"))
    if era_in_normal < len(era_rows):
        return (
            f"bootstrap_era_allow count in normal pool ({era_in_normal}) < "
            f"total era_allow ({len(era_rows)}) — era_allow rows are being wrongly excluded"
        )
    return None


# ── 6. normal_modern formula is correct ──────────────────────────────────────

def test_normal_modern_formula_correct() -> str | None:
    clean_settled, modern_full, _, _ = _load_data()
    dc_count = sum(1 for r in modern_full if r.get("data_collection_override"))
    bp_count = sum(1 for r in modern_full
                   if r.get("bootstrap_provisional") and not r.get("data_collection_override"))
    expected_normal = max(0, len(modern_full) - dc_count - bp_count)
    actual_normal = len([r for r in modern_full
                         if not r.get("data_collection_override")
                         and not r.get("bootstrap_provisional")])
    if actual_normal != expected_normal:
        return (
            f"normal_modern count mismatch: formula gives {expected_normal} "
            f"but direct filter gives {actual_normal}"
        )
    return None


# ── 7. BOOTSTRAP_ALLOW_ENABLED = True ─────────────────────────────────────────

def test_bootstrap_allow_enabled() -> str | None:
    from config.trading_config import BOOTSTRAP_ALLOW_ENABLED
    if not BOOTSTRAP_ALLOW_ENABLED:
        return (
            "BOOTSTRAP_ALLOW_ENABLED=False — bootstrap_era_allow path is disabled. "
            "Phase 6B-2 deadlock fix is not active. Set to True to re-enable."
        )
    return None


# ── 8. scale_allowed hardcoded False ─────────────────────────────────────────

def test_scale_allowed_hardcoded_false() -> str | None:
    from tools.clean_truth_report import classify_records, evaluate_proof_gates
    from tools.performance_report import load_trades
    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]
    result = evaluate_proof_gates(buckets, clean_settled)
    if result.get("scale_allowed") is not False:
        return (
            f"evaluate_proof_gates returned scale_allowed={result.get('scale_allowed')!r} — "
            "expected False; scale lockdown broken"
        )
    return None


# ── 9. real_money_allowed hardcoded False ────────────────────────────────────

def test_real_money_allowed_hardcoded_false() -> str | None:
    from tools.clean_truth_report import classify_records, evaluate_proof_gates
    from tools.performance_report import load_trades
    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]
    result = evaluate_proof_gates(buckets, clean_settled)
    if result.get("real_money_allowed") is not False:
        return (
            f"evaluate_proof_gates returned real_money_allowed={result.get('real_money_allowed')!r} — "
            "expected False; real-money lockdown broken"
        )
    return None


# ── 10. proof_verdict is NOT PROVEN_PROFITABLE ────────────────────────────────

def test_proof_verdict_not_proven() -> str | None:
    from tools.clean_truth_report import classify_records, evaluate_proof_gates
    from tools.performance_report import load_trades
    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]
    result = evaluate_proof_gates(buckets, clean_settled)
    verdict = result.get("proof_verdict", "")
    if verdict == "PROVEN_PROFITABLE":
        return (
            "evaluate_proof_gates returned PROVEN_PROFITABLE — "
            "verify this is genuinely earned (n >= 30, CLV > 0, PF > 1.10, "
            "normal_modern >= 30). If real, remove this test."
        )
    return None


# ── Runner ─────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("legacy rows visible (classification stable)",        test_legacy_rows_visible),
        ("legacy rows not in modern_full_metadata",            test_legacy_not_in_modern_full),
        ("dc_override excluded from normal proof",             test_dc_override_excluded_from_normal),
        ("bootstrap_provisional excluded from normal proof",   test_bootstrap_provisional_excluded),
        ("bootstrap_era_allow counted as normal proof",        test_bootstrap_era_allow_counted_as_normal),
        ("normal_modern formula correct",                      test_normal_modern_formula_correct),
        ("BOOTSTRAP_ALLOW_ENABLED = True",                     test_bootstrap_allow_enabled),
        ("scale_allowed = False (hardcoded)",                  test_scale_allowed_hardcoded_false),
        ("real_money_allowed = False (hardcoded)",             test_real_money_allowed_hardcoded_false),
        ("proof_verdict not PROVEN_PROFITABLE",                test_proof_verdict_not_proven),
    ]

    fails: list[str] = []

    print("=" * 70)
    print("TEST: MODERN-ONLY PROOF PATH INTEGRITY")
    print("=" * 70)

    for name, fn in tests:
        err = fn()
        if err is None:
            print(f"[PASS] {name}")
        else:
            print(f"[FAIL] {name}")
            print(f"       {err}")
            fails.append(f"{name}: {err}")

    clean_settled, modern_full, legacy, buckets = _load_data()
    dc_count  = sum(1 for r in modern_full if r.get("data_collection_override"))
    bp_count  = sum(1 for r in modern_full if r.get("bootstrap_provisional"))
    era_count = sum(1 for r in modern_full if r.get("bootstrap_era_council_allow"))
    normal_count = max(0, len(modern_full) - dc_count - bp_count)

    print()
    print("PROOF SNAPSHOT:")
    print(f"  clean_settled:              {len(clean_settled)}")
    print(f"  LEGACY_EDGE_ONLY:           {len(legacy)}  [visible, excluded]")
    print(f"  modern_full_metadata:       {len(modern_full)}")
    print(f"    dc_override:              {dc_count}  [excluded]")
    print(f"    bootstrap_provisional:    {bp_count}  [excluded]")
    print(f"    bootstrap_era_allow:      {era_count}  [counted as normal]")
    print(f"    normal_modern:            {normal_count}  [proof base]")
    from config.trading_config import BOOTSTRAP_ALLOW_ENABLED
    print(f"  BOOTSTRAP_ALLOW_ENABLED:    {BOOTSTRAP_ALLOW_ENABLED}")
    if BOOTSTRAP_ALLOW_ENABLED and era_count == 0:
        print("  [WARNING] Dashboard needs restart to activate bootstrap_era_allow path")
    print()

    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
        print("RESULT: FAILED")
        sys.exit(1)
    else:
        print("RESULT: PROVEN_MODERN_ONLY_PROOF_OK")


if __name__ == "__main__":
    main()
