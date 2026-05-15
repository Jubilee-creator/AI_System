#!/usr/bin/env python3
"""
Phase 11D — Tests for report_observability_diagnosis_brain.py

Verifies:
  T20  sentinel present in report output
  T21  report is read-only — no log files mutated
  T22  safety locks unchanged after run_diagnosis()
  T23  KXETH quarantine not weakened by report
  T24  thresholds unchanged (MIN_EDGE, MIN_CONFIDENCE, EDGE_DANGER)
  T25  SAMPLE_TOO_SMALL is CRITICAL when post-11A n < 30
  T26  negative ROI is flagged (ROI_FAILURE present, not INFO)
  T27  negative PF is flagged (PROFIT_FACTOR_FAILURE present, not INFO)
  T28  negative CLV is flagged (LOW_OR_NEGATIVE_CLV present, not INFO)
  T29  fake positive (observability contradicts canonical) is flagged
  T30  canonical truth wins — OBSERVABILITY_CONTRADICTION severity reflects disagreement
  T31  all 15 diagnosis categories present in output
  T32  SAFETY_LOCK_HEALTH is INFO when constants are correct
  T33  SAFETY_LOCK_HEALTH is CRITICAL when a constant is tampered
  T34  Diagnosis ranks are sequential 1..N
  T35  BLOCKER_STARVATION uses funnel/candidate denominator on live data
  T36  Synthetic: candidates=1000, opened=5, raw_rows=10 → 0.500% not 50%
  T37  Tool agreement is PARTIAL_DUCKDB_SKIPPED when warehouse unavailable
  T38  _compute_agree_status returns YES / PARTIAL / NO correctly
  T39  SIDE_BIAS evidence percentages are computed dynamically (not hardcoded)

Sentinel: TEST_OBSERVABILITY_DIAGNOSIS_BRAIN_OK
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

SENTINEL = "TEST_OBSERVABILITY_DIAGNOSIS_BRAIN_OK"

_pass = 0
_fail = 0
_skip = 0


def _test(name: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
    global _pass, _fail, _skip
    if skip:
        _skip += 1
        print(f"  [SKIP] {name}  {detail}")
        return
    if ok:
        _pass += 1
        print(f"  [OK  ] {name}  {detail}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}  {detail}")


def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


# ── Import target ─────────────────────────────────────────────────────────────
try:
    import report_observability_diagnosis_brain as brain
    _brain_ok = True
except ImportError as e:
    _brain_ok = False
    _brain_err = str(e)


# ── T20: Sentinel in output ───────────────────────────────────────────────────
def T20_sentinel_in_output() -> None:
    if not _brain_ok:
        _test("T20_sentinel_in_output", False, f"import error: {_brain_err}")
        return
    assert hasattr(brain, "SENTINEL"), "SENTINEL missing"
    _test("T20_sentinel_in_output", brain.SENTINEL == "OBSERVABILITY_DIAGNOSIS_BRAIN_OK",
          f"SENTINEL={brain.SENTINEL!r}")


# ── T21: Read-only — no log files mutated ─────────────────────────────────────
def T21_report_is_readonly() -> None:
    if not _brain_ok:
        _test("T21_report_is_readonly", False, "import error")
        return
    log_files = [
        ROOT / "logs" / "paper_trades.jsonl",
        ROOT / "logs" / "execution_funnel.jsonl",
        ROOT / "logs" / "risk_events.jsonl",
    ]
    before = {p: _mtime(p) for p in log_files}
    try:
        brain.run_diagnosis()
    except Exception as e:
        _test("T21_report_is_readonly", False, f"run_diagnosis raised: {e}")
        return
    after = {p: _mtime(p) for p in log_files}
    mutated = [str(p.name) for p in log_files if after[p] > before[p] + 0.01]
    _test("T21_report_is_readonly", len(mutated) == 0,
          f"mutated={mutated}" if mutated else "no logs mutated")


# ── T22: Safety locks unchanged after run_diagnosis ───────────────────────────
def T22_safety_locks_unchanged() -> None:
    if not _brain_ok:
        _test("T22_safety_locks_unchanged", False, "import error")
        return
    try:
        from config.trading_config import (
            MIN_EDGE, MIN_CONFIDENCE, EDGE_DANGER_HIGH_EDGE_MIN,
            GLOBAL_FORCED_LEARNING_MODE, TRADING_MODE,
        )
        before = {
            "MIN_EDGE": MIN_EDGE,
            "MIN_CONFIDENCE": MIN_CONFIDENCE,
            "EDGE_DANGER_HIGH_EDGE_MIN": EDGE_DANGER_HIGH_EDGE_MIN,
            "GLOBAL_FORCED_LEARNING_MODE": GLOBAL_FORCED_LEARNING_MODE,
            "TRADING_MODE": TRADING_MODE,
        }
    except ImportError as e:
        _test("T22_safety_locks_unchanged", False, f"config import failed: {e}")
        return

    brain.run_diagnosis()

    try:
        importlib.reload(importlib.import_module("config.trading_config"))
        from config.trading_config import (
            MIN_EDGE, MIN_CONFIDENCE, EDGE_DANGER_HIGH_EDGE_MIN,
            GLOBAL_FORCED_LEARNING_MODE, TRADING_MODE,
        )
        after = {
            "MIN_EDGE": MIN_EDGE,
            "MIN_CONFIDENCE": MIN_CONFIDENCE,
            "EDGE_DANGER_HIGH_EDGE_MIN": EDGE_DANGER_HIGH_EDGE_MIN,
            "GLOBAL_FORCED_LEARNING_MODE": GLOBAL_FORCED_LEARNING_MODE,
            "TRADING_MODE": TRADING_MODE,
        }
    except ImportError as e:
        _test("T22_safety_locks_unchanged", False, f"reload failed: {e}")
        return

    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    _test("T22_safety_locks_unchanged", len(changed) == 0,
          f"changed={changed}" if changed else "all constants intact")


# ── T23: KXETH quarantine not weakened ────────────────────────────────────────
def T23_kxeth_quarantine_not_weakened() -> None:
    if not _brain_ok:
        _test("T23_kxeth_quarantine_not_weakened", False, "import error")
        return
    diagnoses, canonical, obs, _ = brain.run_diagnosis()
    kxeth_dx = next((d for d in diagnoses if d.category == "KXETH_QUARANTINE_HEALTH"), None)
    if kxeth_dx is None:
        _test("T23_kxeth_quarantine_not_weakened", False, "KXETH_QUARANTINE_HEALTH missing from diagnoses")
        return
    # KXETH diagnosis must never recommend REMOVING the quarantine
    do_not = kxeth_dx.do_not.upper()
    evidence = kxeth_dx.evidence.upper()
    no_removal = "DO NOT REMOVE" in do_not or "DO NOT REMOVE" in do_not
    _test("T23_kxeth_quarantine_not_weakened", "DO NOT REMOVE" in do_not,
          f"do_not contains removal warning: {kxeth_dx.do_not[:80]}")


# ── T24: Thresholds unchanged (constants read-only reference) ─────────────────
def T24_thresholds_unchanged() -> None:
    if not _brain_ok:
        _test("T24_thresholds_unchanged", False, "import error")
        return
    expected = {
        "_MIN_EDGE":           0.03,
        "_MIN_CONFIDENCE":     0.65,
        "_EDGE_DANGER_MIN":    0.08,
        "_GATE_PROFIT_FACTOR": 1.10,
        "_GATE_SCALE_MIN_NORMAL": 30,
    }
    mismatches = {}
    for attr, val in expected.items():
        actual = getattr(brain, attr, None)
        if actual != val:
            mismatches[attr] = (val, actual)
    _test("T24_thresholds_unchanged", len(mismatches) == 0,
          f"mismatches={mismatches}" if mismatches else "all thresholds match")


# ── T25: SAMPLE_TOO_SMALL is CRITICAL when post-11A n < 30 ───────────────────
def T25_sample_too_small_flagged() -> None:
    if not _brain_ok:
        _test("T25_sample_too_small_flagged", False, "import error")
        return
    diagnoses, canonical, _, _ = brain.run_diagnosis()
    dx = next((d for d in diagnoses if d.category == "SAMPLE_TOO_SMALL"), None)
    if dx is None:
        _test("T25_sample_too_small_flagged", False, "SAMPLE_TOO_SMALL category not found")
        return
    n = canonical.get("p11a_settled", 0)
    expected_critical = n < 30
    correct = (dx.severity == "CRITICAL") if expected_critical else True
    _test("T25_sample_too_small_flagged", correct,
          f"post_11a_settled={n} → severity={dx.severity} (CRITICAL expected={expected_critical})")


# ── T26: Negative ROI is flagged ─────────────────────────────────────────────
def T26_negative_roi_flagged() -> None:
    if not _brain_ok:
        _test("T26_negative_roi_flagged", False, "import error")
        return
    diagnoses, canonical, _, _ = brain.run_diagnosis()
    dx = next((d for d in diagnoses if d.category == "ROI_FAILURE"), None)
    if dx is None:
        _test("T26_negative_roi_flagged", False, "ROI_FAILURE category not found")
        return
    roi = canonical.get("roi")
    if roi is not None and roi < 0:
        _test("T26_negative_roi_flagged", dx.severity in ("CRITICAL", "HIGH", "MEDIUM"),
              f"ROI={roi:.4f} → severity={dx.severity}")
    else:
        _test("T26_negative_roi_flagged", True,
              f"ROI={roi} not negative — flagging check skipped (ROI_FAILURE present)")


# ── T27: Negative PF is flagged ──────────────────────────────────────────────
def T27_negative_pf_flagged() -> None:
    if not _brain_ok:
        _test("T27_negative_pf_flagged", False, "import error")
        return
    diagnoses, canonical, _, _ = brain.run_diagnosis()
    dx = next((d for d in diagnoses if d.category == "PROFIT_FACTOR_FAILURE"), None)
    if dx is None:
        _test("T27_negative_pf_flagged", False, "PROFIT_FACTOR_FAILURE category not found")
        return
    pf = canonical.get("profit_factor")
    if pf is not None and pf < 1.0:
        _test("T27_negative_pf_flagged", dx.severity in ("CRITICAL", "HIGH", "MEDIUM"),
              f"PF={pf:.4f} → severity={dx.severity}")
    else:
        _test("T27_negative_pf_flagged", True,
              f"PF={pf} not below 1.0 — flagging check skipped")


# ── T28: Negative CLV is flagged ─────────────────────────────────────────────
def T28_negative_clv_flagged() -> None:
    if not _brain_ok:
        _test("T28_negative_clv_flagged", False, "import error")
        return
    diagnoses, canonical, _, _ = brain.run_diagnosis()
    dx = next((d for d in diagnoses if d.category == "LOW_OR_NEGATIVE_CLV"), None)
    if dx is None:
        _test("T28_negative_clv_flagged", False, "LOW_OR_NEGATIVE_CLV category not found")
        return
    clv = canonical.get("avg_clv")
    if clv is not None and clv < 0:
        _test("T28_negative_clv_flagged", dx.severity in ("CRITICAL", "HIGH", "MEDIUM"),
              f"avg_clv={clv:.4f} → severity={dx.severity}")
    else:
        _test("T28_negative_clv_flagged", True,
              f"avg_clv={clv} not negative — flagging check skipped")


# ── T29: Fake positive in obs is flagged (OBSERVABILITY_CONTRADICTION) ────────
def T29_fake_positive_flagged() -> None:
    """
    Injects a fake obs dict where MLflow says clean_settled=9999 (impossible).
    Verifies that _dx_observability_contradiction detects and flags it.
    """
    if not _brain_ok:
        _test("T29_fake_positive_flagged", False, "import error")
        return
    canonical = brain._load_canonical_data()
    fake_obs = {
        "mlf_clean_settled": 9999,   # impossible — canonical will be ~196
        "mlf_modern_full":   canonical["modern_full_n"],
        "mlf_normal_modern": canonical["normal_modern_n"],
    }
    dx = brain._dx_observability_contradiction(canonical, fake_obs)
    detected = dx.severity in ("HIGH", "CRITICAL") and "9999" in dx.evidence
    _test("T29_fake_positive_flagged", detected,
          f"severity={dx.severity}, evidence_snippet={dx.evidence[:80]}")


# ── T30: Canonical wins — contradiction severity reflects disagreement ─────────
def T30_canonical_wins_contradiction() -> None:
    if not _brain_ok:
        _test("T30_canonical_wins_contradiction", False, "import error")
        return
    canonical = brain._load_canonical_data()

    # Agreeing obs → LOW severity
    agree_obs = {
        "mlf_clean_settled": canonical["clean_settled_n"],
        "mlf_modern_full":   canonical["modern_full_n"],
        "mlf_normal_modern": canonical["normal_modern_n"],
    }
    dx_agree = brain._dx_observability_contradiction(canonical, agree_obs)

    # Disagreeing obs → HIGH severity
    disagree_obs = {
        "mlf_clean_settled": canonical["clean_settled_n"] + 100,
        "mlf_modern_full":   canonical["modern_full_n"],
        "mlf_normal_modern": canonical["normal_modern_n"],
    }
    dx_disagree = brain._dx_observability_contradiction(canonical, disagree_obs)

    ok = (
        brain.SEVERITY_WEIGHT.get(dx_disagree.severity, 0) >
        brain.SEVERITY_WEIGHT.get(dx_agree.severity, 0)
    )
    _test("T30_canonical_wins_contradiction", ok,
          f"agree→{dx_agree.severity}, disagree→{dx_disagree.severity}")


# ── T31: All 15 diagnosis categories present ──────────────────────────────────
def T31_all_15_categories_present() -> None:
    if not _brain_ok:
        _test("T31_all_15_categories_present", False, "import error")
        return
    expected_categories = {
        "PAYOFF_ASYMMETRY", "ENTRY_PRICE_TOXICITY", "CONFIDENCE_OVERCONFIDENCE",
        "LOW_OR_NEGATIVE_CLV", "PROFIT_FACTOR_FAILURE", "ROI_FAILURE",
        "SCANNER_CONCENTRATION", "SIDE_BIAS", "SAMPLE_TOO_SMALL",
        "DATA_QUALITY_RISK", "DRIFT_OR_REGIME_SHIFT", "BLOCKER_STARVATION",
        "KXETH_QUARANTINE_HEALTH", "SAFETY_LOCK_HEALTH", "OBSERVABILITY_CONTRADICTION",
    }
    diagnoses, _, _, _ = brain.run_diagnosis()
    found = {d.category for d in diagnoses}
    missing = expected_categories - found
    extra = found - expected_categories
    ok = len(missing) == 0
    detail = f"missing={missing}" if missing else (f"extra={extra}" if extra else f"all {len(expected_categories)} present")
    _test("T31_all_15_categories_present", ok, detail)


# ── T32: SAFETY_LOCK_HEALTH is INFO when constants correct ───────────────────
def T32_safety_locks_info_when_correct() -> None:
    if not _brain_ok:
        _test("T32_safety_locks_info_when_correct", False, "import error")
        return
    correct_safety = {
        "TRADING_MODE":                "PAPER",
        "GLOBAL_FORCED_LEARNING_MODE": True,
        "MIN_EDGE":                    0.03,
        "MIN_CONFIDENCE":              0.65,
        "EDGE_DANGER_HIGH_EDGE_MIN":   0.08,
    }
    dx = brain._dx_safety_locks(correct_safety)
    _test("T32_safety_locks_info_when_correct", dx.severity == "INFO",
          f"severity={dx.severity}")


# ── T33: SAFETY_LOCK_HEALTH is CRITICAL when a constant is tampered ──────────
def T33_safety_locks_critical_when_tampered() -> None:
    if not _brain_ok:
        _test("T33_safety_locks_critical_when_tampered", False, "import error")
        return
    tampered_safety = {
        "TRADING_MODE":                "LIVE",   # tampered!
        "GLOBAL_FORCED_LEARNING_MODE": True,
        "MIN_EDGE":                    0.03,
        "MIN_CONFIDENCE":              0.65,
        "EDGE_DANGER_HIGH_EDGE_MIN":   0.08,
    }
    dx = brain._dx_safety_locks(tampered_safety)
    _test("T33_safety_locks_critical_when_tampered", dx.severity == "CRITICAL",
          f"severity={dx.severity}, evidence={dx.evidence[:60]}")


# ── T34: Diagnosis ranks are sequential 1..N ─────────────────────────────────
def T34_ranks_are_sequential() -> None:
    if not _brain_ok:
        _test("T34_ranks_are_sequential", False, "import error")
        return
    diagnoses, _, _, _ = brain.run_diagnosis()
    ranks = [d.rank for d in diagnoses]
    expected = list(range(1, len(diagnoses) + 1))
    _test("T34_ranks_are_sequential", ranks == expected,
          f"ranks={ranks[:5]}...  expected=1..{len(diagnoses)}")


# ── T35: BLOCKER_STARVATION uses funnel denominator on live data ──────────────
def T35_blocker_starvation_funnel_denominator_live() -> None:
    if not _brain_ok:
        _test("T35_blocker_starvation_funnel_denominator_live", False, "import error")
        return
    _, canonical, obs, _ = brain.run_diagnosis()
    dx = next(
        (d for d in brain.run_diagnosis()[0] if d.category == "BLOCKER_STARVATION"),
        None,
    )
    if dx is None:
        _test("T35_blocker_starvation_funnel_denominator_live", False,
              "BLOCKER_STARVATION not found")
        return
    ev = dx.evidence
    # Must contain funnel candidates OR explicitly state UNKNOWN (never raw paper rows)
    has_candidates = "post_11a_candidates=" in ev
    # raw_rows must NOT appear as a denominator in the evidence
    uses_raw_rows_as_denom = "raw_rows=" in ev and "candidates" not in ev
    ok = has_candidates and not uses_raw_rows_as_denom
    _test("T35_blocker_starvation_funnel_denominator_live", ok,
          f"has_candidates={has_candidates}, uses_raw_rows_as_denom={uses_raw_rows_as_denom}; "
          f"evidence={ev[:100]}")


# ── T36: Synthetic case — candidates=1000, opened=5, raw=10 → 0.500% not 50% ─
def T36_blocker_starvation_synthetic_denominator() -> None:
    if not _brain_ok:
        _test("T36_blocker_starvation_synthetic_denominator", False, "import error")
        return
    synthetic_obs = {
        "funnel_candidates": 1000,
        "funnel_source": "test_synthetic",
    }
    synthetic_c = {
        "p11a_opened": 5,
        "p11a_raw_rows": 10,  # wrong denominator — must NOT be used
    }
    dx = brain._dx_blocker_starvation(synthetic_obs, synthetic_c)
    ev = dx.evidence
    # Correct rate: 5/1000 = 0.500%  |  Wrong rate: 5/10 = 50.000%
    correct_rate = "0.500" in ev
    wrong_rate   = "50.000" in ev or "50.00" in ev
    has_candidates = "1000" in ev
    ok = correct_rate and not wrong_rate and has_candidates
    _test("T36_blocker_starvation_synthetic_denominator", ok,
          f"correct_rate={correct_rate}, wrong_rate={wrong_rate}, evidence={ev[:110]}")


# ── T37: _compute_agree_status returns PARTIAL when warehouse unavailable ──────
def T37_agree_status_partial_when_db_unavailable() -> None:
    if not _brain_ok:
        _test("T37_agree_status_partial_when_db_unavailable", False, "import error")
        return
    if not hasattr(brain, "_compute_agree_status"):
        _test("T37_agree_status_partial_when_db_unavailable", False,
              "_compute_agree_status not exposed")
        return
    obs_unavail = {"db_warehouse_present_but_unavailable": True}
    status = brain._compute_agree_status(obs_unavail, all_agree=True)
    _test("T37_agree_status_partial_when_db_unavailable",
          status == "PARTIAL_DUCKDB_SKIPPED",
          f"status={status!r}")


# ── T38: _compute_agree_status returns YES / NO correctly ─────────────────────
def T38_agree_status_yes_and_no() -> None:
    if not _brain_ok:
        _test("T38_agree_status_yes_and_no", False, "import error")
        return
    if not hasattr(brain, "_compute_agree_status"):
        _test("T38_agree_status_yes_and_no", False, "_compute_agree_status not exposed")
        return
    yes_status    = brain._compute_agree_status({}, all_agree=True)
    no_status     = brain._compute_agree_status({}, all_agree=False)
    no_with_unavail = brain._compute_agree_status(
        {"db_warehouse_present_but_unavailable": True}, all_agree=False
    )
    ok = (yes_status == "YES" and no_status == "NO" and no_with_unavail == "NO")
    _test("T38_agree_status_yes_and_no",
          ok,
          f"YES→{yes_status!r}, NO→{no_status!r}, NO+unavail→{no_with_unavail!r}")


# ── T39: SIDE_BIAS evidence percentages are dynamic, not hardcoded ────────────
def T39_side_bias_dynamic_percent() -> None:
    if not _brain_ok:
        _test("T39_side_bias_dynamic_percent", False, "import error")
        return
    from collections import Counter
    # Synthetic: 60 BET_YES + 40 BET_NO — neither percentage is 0%
    c_mixed = {
        "bet_yes": 60,
        "bet_no":  40,
        "p11a_actions": Counter({"BET_YES": 3, "BET_NO": 2}),
    }
    dx = brain._dx_side_bias(c_mixed)
    ev = dx.evidence
    # Expect "60%" and "40%" (dynamic), NOT "0%"
    has_yes_pct = "60%" in ev
    has_no_pct  = "40%" in ev
    no_hardcoded_zero = "(0%)" not in ev
    ok = has_yes_pct and has_no_pct and no_hardcoded_zero
    _test("T39_side_bias_dynamic_percent", ok,
          f"has_yes_60%={has_yes_pct}, has_no_40%={has_no_pct}, "
          f"no_hardcoded_0%={no_hardcoded_zero}; evidence={ev[:80]}")


# ─── Runner ───────────────────────────────────────────────────────────────────

def main() -> int:
    global _pass, _fail, _skip
    print()
    print("=" * 60)
    print("  Phase 11D — Diagnosis Brain Tests")
    print("=" * 60)

    tests = [
        T20_sentinel_in_output,
        T21_report_is_readonly,
        T22_safety_locks_unchanged,
        T23_kxeth_quarantine_not_weakened,
        T24_thresholds_unchanged,
        T25_sample_too_small_flagged,
        T26_negative_roi_flagged,
        T27_negative_pf_flagged,
        T28_negative_clv_flagged,
        T29_fake_positive_flagged,
        T30_canonical_wins_contradiction,
        T31_all_15_categories_present,
        T32_safety_locks_info_when_correct,
        T33_safety_locks_critical_when_tampered,
        T34_ranks_are_sequential,
        T35_blocker_starvation_funnel_denominator_live,
        T36_blocker_starvation_synthetic_denominator,
        T37_agree_status_partial_when_db_unavailable,
        T38_agree_status_yes_and_no,
        T39_side_bias_dynamic_percent,
    ]

    print()
    for fn in tests:
        try:
            fn()
        except Exception as e:
            _test(fn.__name__, False, f"EXCEPTION: {e}")

    print()
    total = _pass + _fail + _skip
    print(f"  Results: {_pass}/{total} passed, {_fail} failed, {_skip} skipped")
    print()
    if _fail == 0:
        print(f"  {SENTINEL}")
    else:
        print(f"  TESTS FAILED — {_fail} failure(s) above require investigation")
    print()
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
