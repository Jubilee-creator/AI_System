#!/usr/bin/env python3
"""
tools/test_edge_profile_original_edge_shadow.py
------------------------------------------------
Phase 9I test suite — Original-Edge Shadow Bucket.

Verifies:
  T01  Phase 9I metadata fields present in edge_profile.json
  T02  edge_math_alignment_status = "SHADOW_ONLY_NO_LIVE_CHANGE"
  T03  by_original_edge_bucket_field = "original_edge"
  T04  by_edge_bucket_field = "risk_edge"
  T05  profile_has_original_edge_shadow = True
  T06  by_original_edge_bucket exists in profile["profiles"]
  T07  by_edge_bucket still exists (live bucket UNCHANGED)
  T08  by_normal_modern_edge_bucket still exists (Phase 9H preserved)
  T09  by_edge_price_bucket still exists (Phase 9A preserved)
  T10  Shadow input_count == profile_input_trades (KXETH excluded)
  T11  Shadow 0.05-0.10 is the dominant bucket (most trades)
  T12  Shadow 0.05-0.10 bad_enough=True (2D gate fires identically)
  T13  Live by_edge_bucket 0.05-0.10 still bad_enough=True (gate unchanged)
  T14  Shadow n(0.05-10) >= live n(0.05-10) (signals collapse to one bucket)
  T15  2D sweet-spot 0.05-0.10|0.80-0.90 still qualifies (n>=5, WR>=0.80, pnl>0)
  T16  Poison 2D cells remain negative
  T17  Profile trusted=True
  T18  real_money_allowed=False (hardcoded)
  T19  scale_allowed=False (hardcoded)
  T20  Critic does NOT consume by_original_edge_bucket (AST check)
  T21  Builder does NOT consume by_original_edge_bucket (AST check)
  T22  KXETH quarantine active in paper_trader.py
  T23  Shadow missing_count=0 (all records have original_edge or edge field)
  T24  Shadow and live both confirm same behavioral conclusion for 0.05-0.10

NOTE: Tests T06-T14 require the profile to be rebuilt first:
  python3 tools/build_edge_profile.py

Expected result when all pass: PROVEN_ORIGINAL_EDGE_SHADOW_OK
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE_PATH = ROOT / "data" / "edge_profile.json"
CRITIC_PATH  = ROOT / "brain" / "critic_brain.py"
BUILDER_PATH = ROOT / "brain" / "builder_brain.py"
PT_PATH      = ROOT / "brain" / "paper_trader.py"

MIN_SAMPLE_SIZE = 5


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text())
    except Exception:
        return {}


def _bad_enough(bkt: Optional[dict]) -> bool:
    """Profile-format bad_enough: uses trades/win_rate/total_pnl keys."""
    if bkt is None:
        return False
    n = int(bkt.get("trades", 0))
    if n < MIN_SAMPLE_SIZE:
        return False
    wr  = float(bkt.get("win_rate", 0.0))
    pnl = float(bkt.get("total_pnl", 0.0))
    return bool(wr < 0.40 or pnl < 0)


# ── T01-T05: Phase 9I metadata ────────────────────────────────────────────────

def test_phase9i_metadata_present() -> Optional[str]:
    """Phase 9I transparency metadata fields must exist."""
    profile = _load_profile()
    if not profile:
        return "data/edge_profile.json missing or empty"
    required = [
        "profile_has_original_edge_shadow",
        "by_original_edge_bucket_field",
        "by_edge_bucket_field",
        "original_edge_shadow_input_count",
        "original_edge_shadow_has_field_count",
        "original_edge_shadow_fallback_count",
        "original_edge_shadow_missing_count",
        "edge_math_alignment_status",
    ]
    missing = [k for k in required if k not in profile]
    if missing:
        return (
            f"Missing metadata fields: {missing} — "
            "rebuild profile: python3 tools/build_edge_profile.py"
        )
    return None


def test_alignment_status() -> Optional[str]:
    """edge_math_alignment_status must be SHADOW_ONLY_NO_LIVE_CHANGE."""
    profile = _load_profile()
    v = profile.get("edge_math_alignment_status")
    if v != "SHADOW_ONLY_NO_LIVE_CHANGE":
        return f"edge_math_alignment_status={v!r} (expected 'SHADOW_ONLY_NO_LIVE_CHANGE')"
    return None


def test_original_edge_field_tag() -> Optional[str]:
    """by_original_edge_bucket_field must be 'original_edge'."""
    profile = _load_profile()
    v = profile.get("by_original_edge_bucket_field")
    if v != "original_edge":
        return f"by_original_edge_bucket_field={v!r} (expected 'original_edge')"
    return None


def test_risk_edge_field_tag() -> Optional[str]:
    """by_edge_bucket_field must be 'risk_edge'."""
    profile = _load_profile()
    v = profile.get("by_edge_bucket_field")
    if v != "risk_edge":
        return f"by_edge_bucket_field={v!r} (expected 'risk_edge')"
    return None


def test_has_shadow_flag() -> Optional[str]:
    """profile_has_original_edge_shadow must be True."""
    profile = _load_profile()
    v = profile.get("profile_has_original_edge_shadow")
    if v is not True:
        return f"profile_has_original_edge_shadow={v!r} (expected True)"
    return None


# ── T06-T09: Profile bucket structure ────────────────────────────────────────

def test_shadow_bucket_exists() -> Optional[str]:
    """by_original_edge_bucket must exist in profile['profiles']."""
    profile = _load_profile()
    if "by_original_edge_bucket" not in profile.get("profiles", {}):
        return (
            "by_original_edge_bucket not found in profiles — "
            "rebuild profile: python3 tools/build_edge_profile.py"
        )
    shadow = profile["profiles"]["by_original_edge_bucket"]
    if not isinstance(shadow, dict) or len(shadow) == 0:
        return "by_original_edge_bucket is empty"
    return None


def test_live_edge_bucket_unchanged() -> Optional[str]:
    """Live by_edge_bucket must still exist and retain its data."""
    profile = _load_profile()
    live = profile.get("profiles", {}).get("by_edge_bucket")
    if live is None:
        return "by_edge_bucket missing from profiles — live 1D bucket was removed!"
    if "0.05-0.10" not in live:
        return "by_edge_bucket missing 0.05-0.10 key — live bucket changed!"
    n = int(live["0.05-0.10"].get("trades", 0))
    if n < 40:
        return f"by_edge_bucket 0.05-0.10 n={n} unexpectedly small"
    return None


def test_phase9h_bucket_preserved() -> Optional[str]:
    """by_normal_modern_edge_bucket from Phase 9H must still exist."""
    profile = _load_profile()
    if "by_normal_modern_edge_bucket" not in profile.get("profiles", {}):
        return "by_normal_modern_edge_bucket missing — Phase 9H shadow removed!"
    return None


def test_phase9a_2d_bucket_preserved() -> Optional[str]:
    """by_edge_price_bucket from Phase 9A must still exist."""
    profile = _load_profile()
    if "by_edge_price_bucket" not in profile.get("profiles", {}):
        return "by_edge_price_bucket missing — Phase 9A 2D table removed!"
    return None


# ── T10-T14: Shadow vs live comparison ───────────────────────────────────────

def test_shadow_input_count_matches_profile() -> Optional[str]:
    """Shadow input count must equal profile_input_trades (KXETH excluded universe)."""
    profile = _load_profile()
    shadow_input = int(profile.get("original_edge_shadow_input_count", -1))
    profile_input = int(profile.get("profile_input_trades", -2))
    if shadow_input != profile_input:
        return (
            f"original_edge_shadow_input_count={shadow_input} != "
            f"profile_input_trades={profile_input} — KXETH may be included in shadow"
        )
    return None


def test_shadow_0510_is_dominant() -> Optional[str]:
    """Shadow 0.05-0.10 must have strictly more trades than any other shadow bucket."""
    profile = _load_profile()
    shadow = profile.get("profiles", {}).get("by_original_edge_bucket", {})
    if "0.05-0.10" not in shadow:
        return "shadow 0.05-0.10 bucket missing — rebuild profile first"
    n_0510 = int(shadow["0.05-0.10"]["trades"])
    for bk, bv in shadow.items():
        if bk == "0.05-0.10":
            continue
        other_n = int(bv["trades"])
        if other_n >= n_0510:
            return (
                f"shadow bucket {bk!r} has n={other_n} >= 0.05-0.10 n={n_0510} — "
                "0.05-0.10 is not the dominant bucket"
            )
    return None


def test_shadow_0510_bad_enough() -> Optional[str]:
    """Shadow 0.05-0.10 must be bad_enough=True so 2D gate fires under original_edge."""
    profile = _load_profile()
    shadow = profile.get("profiles", {}).get("by_original_edge_bucket", {})
    bkt = shadow.get("0.05-0.10")
    if bkt is None:
        return "shadow 0.05-0.10 missing — rebuild profile first"
    if not _bad_enough(bkt):
        n   = int(bkt.get("trades", 0))
        wr  = float(bkt.get("win_rate", 0.0))
        pnl = float(bkt.get("total_pnl", 0.0))
        return (
            f"shadow 0.05-0.10 bad_enough=False: n={n} WR={wr:.3f} pnl={pnl:.2f}. "
            "Switching to original_edge WOULD change Critic behavior — do NOT patch live bucket."
        )
    return None


def test_live_0510_still_bad_enough() -> Optional[str]:
    """Live by_edge_bucket 0.05-0.10 must remain bad_enough=True (gate unchanged)."""
    profile = _load_profile()
    live = profile.get("profiles", {}).get("by_edge_bucket", {})
    bkt = live.get("0.05-0.10")
    if bkt is None:
        return "live by_edge_bucket 0.05-0.10 missing"
    if not _bad_enough(bkt):
        n   = int(bkt.get("trades", 0))
        pnl = float(bkt.get("total_pnl", 0.0))
        return (
            f"REGRESSION: live by_edge_bucket 0.05-0.10 bad_enough=False "
            f"(n={n}, pnl={pnl:.2f}). 2D gate silenced!"
        )
    return None


def test_shadow_n_exceeds_live_n_for_0510() -> Optional[str]:
    """Shadow 0.05-0.10 n must be >= live by_edge_bucket 0.05-0.10 n.

    All signals collapse to original_edge 0.05-0.10; risk_edge splits them
    across three buckets. So the shadow 0.05-0.10 bucket should be larger.
    """
    profile = _load_profile()
    shadow_bkt = profile.get("profiles", {}).get("by_original_edge_bucket", {}).get("0.05-0.10")
    live_bkt   = profile.get("profiles", {}).get("by_edge_bucket", {}).get("0.05-0.10")
    if shadow_bkt is None:
        return "shadow 0.05-0.10 missing"
    if live_bkt is None:
        return "live 0.05-0.10 missing"
    shadow_n = int(shadow_bkt["trades"])
    live_n   = int(live_bkt["trades"])
    if shadow_n < live_n:
        return (
            f"shadow n={shadow_n} < live n={live_n} for 0.05-0.10 — "
            "unexpected: original_edge should collapse more records into this bucket"
        )
    return None


# ── T15-T16: 2D table integrity ───────────────────────────────────────────────

def test_2d_sweet_spot_still_qualifies() -> Optional[str]:
    """2D sweet-spot cell 0.05-0.10|0.80-0.90 must still have n>=5, WR>=0.80, pnl>0."""
    profile = _load_profile()
    cell = (
        profile.get("profiles", {})
        .get("by_edge_price_bucket", {})
        .get("0.05-0.10|0.80-0.90")
    )
    if cell is None:
        return "2D sweet-spot cell missing — rebuild profile"
    n   = int(cell.get("trades", 0))
    wr  = float(cell.get("win_rate", 0.0))
    pnl = float(cell.get("total_pnl", 0.0))
    if n < 5:
        return f"sweet-spot n={n} < 5"
    if wr < 0.80:
        return f"sweet-spot WR={wr:.3f} < 0.80"
    if pnl <= 0:
        return f"sweet-spot pnl={pnl:.2f} <= 0"
    return None


def test_poison_cells_remain_negative() -> Optional[str]:
    """Poison 2D cells (0.60-0.80 price zone) must remain pnl < 0."""
    profile = _load_profile()
    table = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    poison_cells = ["0.05-0.10|0.60-0.70", "0.05-0.10|0.70-0.80"]
    for cell_key in poison_cells:
        cell = table.get(cell_key)
        if cell is None:
            return f"poison cell {cell_key!r} missing — rebuild profile"
        pnl = float(cell.get("total_pnl", 0.0))
        n   = int(cell.get("trades", 0))
        if n >= 5 and pnl >= 0:
            return f"poison cell {cell_key!r} pnl={pnl:.2f} >= 0 — no longer negative!"
    return None


# ── T17-T19: Safety locks ─────────────────────────────────────────────────────

def test_profile_trusted() -> Optional[str]:
    """Edge profile must remain trusted=True."""
    profile = _load_profile()
    trusted = profile.get("edge_profile_health", {}).get("edge_profile_trusted")
    if trusted is not True:
        reason = profile.get("edge_profile_health", {}).get("reason", "?")
        return f"profile trusted={trusted!r}, reason: {reason}"
    return None


def test_real_money_locked() -> Optional[str]:
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades
    records = load_trades()
    buckets = classify_records(records)
    gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gate.get("real_money_allowed") is not False:
        return f"real_money_allowed={gate.get('real_money_allowed')!r} — must be False"
    return None


def test_scale_locked() -> Optional[str]:
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades
    records = load_trades()
    buckets = classify_records(records)
    gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gate.get("scale_allowed") is not False:
        return f"scale_allowed={gate.get('scale_allowed')!r} — must be False"
    return None


# ── T20-T21: Critic/Builder do NOT consume shadow ────────────────────────────

def test_critic_does_not_use_shadow_bucket() -> Optional[str]:
    """Critic must NOT look up by_original_edge_bucket — live lookup unchanged."""
    source = CRITIC_PATH.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "by_original_edge_bucket" in node.value:
                return (
                    "critic_brain.py references 'by_original_edge_bucket' — "
                    "Critic must NOT consume this shadow bucket (live change forbidden)"
                )
    return None


def test_builder_does_not_use_shadow_bucket() -> Optional[str]:
    """Builder must NOT look up by_original_edge_bucket — live lookup unchanged."""
    source = BUILDER_PATH.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "by_original_edge_bucket" in node.value:
                return (
                    "builder_brain.py references 'by_original_edge_bucket' — "
                    "Builder must NOT consume this shadow bucket (live change forbidden)"
                )
    return None


# ── T22: KXETH quarantine ─────────────────────────────────────────────────────

def test_kxeth_quarantine_active() -> Optional[str]:
    """QUARANTINED_TICKER_PREFIXES must still be imported and used in paper_trader.py."""
    source = PT_PATH.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "trading_config" in (node.module or ""):
                names = [alias.name for alias in node.names]
                if "QUARANTINED_TICKER_PREFIXES" in names:
                    return None
    return "QUARANTINED_TICKER_PREFIXES not found in paper_trader.py imports"


# ── T23-T24: Shadow integrity ─────────────────────────────────────────────────

def test_shadow_missing_count_is_zero() -> Optional[str]:
    """All profile records must have original_edge or edge field (missing_count=0)."""
    profile = _load_profile()
    missing = int(profile.get("original_edge_shadow_missing_count", -1))
    if missing < 0:
        return "original_edge_shadow_missing_count field missing from profile"
    if missing != 0:
        return (
            f"original_edge_shadow_missing_count={missing} — "
            f"{missing} records have neither original_edge nor edge field"
        )
    return None


def test_shadow_and_live_same_behavioral_conclusion() -> Optional[str]:
    """Shadow and live must both have bad_enough=True for 0.05-0.10.

    Both point to the same behavioral outcome: 2D gate fires.
    Discrepancy here means switching fields WOULD change Critic behavior.
    """
    profile = _load_profile()
    shadow_bkt = (
        profile.get("profiles", {}).get("by_original_edge_bucket", {}).get("0.05-0.10")
    )
    live_bkt = (
        profile.get("profiles", {}).get("by_edge_bucket", {}).get("0.05-0.10")
    )
    if shadow_bkt is None:
        return "shadow 0.05-0.10 missing"
    if live_bkt is None:
        return "live 0.05-0.10 missing"
    shadow_bad = _bad_enough(shadow_bkt)
    live_bad   = _bad_enough(live_bkt)
    if shadow_bad != live_bad:
        return (
            f"MISMATCH: shadow bad_enough={shadow_bad} vs live bad_enough={live_bad}. "
            "Switching to original_edge WOULD change Critic behavior — do NOT patch live bucket!"
        )
    if not shadow_bad:
        return (
            f"REGRESSION: both shadow and live show bad_enough=False for 0.05-0.10. "
            "2D gate would be silenced under both field choices."
        )
    return None


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("Phase 9I metadata fields present",
         test_phase9i_metadata_present),
        ("edge_math_alignment_status = 'SHADOW_ONLY_NO_LIVE_CHANGE'",
         test_alignment_status),
        ("by_original_edge_bucket_field = 'original_edge'",
         test_original_edge_field_tag),
        ("by_edge_bucket_field = 'risk_edge'",
         test_risk_edge_field_tag),
        ("profile_has_original_edge_shadow = True",
         test_has_shadow_flag),
        ("by_original_edge_bucket exists in profiles",
         test_shadow_bucket_exists),
        ("by_edge_bucket still exists (live bucket UNCHANGED)",
         test_live_edge_bucket_unchanged),
        ("by_normal_modern_edge_bucket still exists (Phase 9H preserved)",
         test_phase9h_bucket_preserved),
        ("by_edge_price_bucket still exists (Phase 9A preserved)",
         test_phase9a_2d_bucket_preserved),
        ("Shadow input_count == profile_input_trades",
         test_shadow_input_count_matches_profile),
        ("Shadow 0.05-0.10 is the dominant bucket (most trades)",
         test_shadow_0510_is_dominant),
        ("Shadow 0.05-0.10 bad_enough=True (2D gate fires identically)",
         test_shadow_0510_bad_enough),
        ("Live by_edge_bucket 0.05-0.10 still bad_enough=True (gate UNCHANGED)",
         test_live_0510_still_bad_enough),
        ("Shadow n(0.05-10) >= live n(0.05-10) (signals collapse to one bucket)",
         test_shadow_n_exceeds_live_n_for_0510),
        ("2D sweet-spot 0.05-0.10|0.80-0.90: n>=5, WR>=0.80, pnl>0",
         test_2d_sweet_spot_still_qualifies),
        ("Poison 2D cells 0.60-0.80 remain pnl<0",
         test_poison_cells_remain_negative),
        ("Profile trusted=True",
         test_profile_trusted),
        ("real_money_allowed=False (hardcoded)",
         test_real_money_locked),
        ("scale_allowed=False (hardcoded)",
         test_scale_locked),
        ("Critic does NOT consume by_original_edge_bucket (no live change)",
         test_critic_does_not_use_shadow_bucket),
        ("Builder does NOT consume by_original_edge_bucket (no live change)",
         test_builder_does_not_use_shadow_bucket),
        ("KXETH quarantine active in paper_trader.py",
         test_kxeth_quarantine_active),
        ("Shadow missing_count=0 (all records have original_edge or edge)",
         test_shadow_missing_count_is_zero),
        ("Shadow and live both bad_enough=True for 0.05-0.10 (same conclusion)",
         test_shadow_and_live_same_behavioral_conclusion),
    ]

    passed = 0
    failed = 0
    print("=" * 68)
    print("ORIGINAL-EDGE SHADOW BUCKET TEST SUITE — Phase 9I")
    print("=" * 68)
    print("NOTE: Tests T06-T14 require: python3 tools/build_edge_profile.py first")
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
        print("PROVEN_ORIGINAL_EDGE_SHADOW_OK")
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
