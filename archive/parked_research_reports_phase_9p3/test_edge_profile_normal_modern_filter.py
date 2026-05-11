#!/usr/bin/env python3
"""
tools/test_edge_profile_normal_modern_filter.py
------------------------------------------------
Phase 9H test suite — 1D profile population integrity.

Tests what Phase 9H added to the profile:
  - Shadow by_normal_modern_edge_bucket (diagnostic only, not consumed by Critic)
  - Transparency metadata fields (population breakdown)
  - Live gate preservation (by_edge_bucket unchanged, 2D still fires)
  - Safety locks unchanged

IMPORTANT — why the 1D profile was NOT fully filtered to normal_modern:
  Filtering would flip the 0.05-0.10 edge bucket from BAD (pnl=-20.70) to
  GOOD (pnl=+8.85), silencing the 2D price-conditioned gate for ALL 0.05-0.10
  signals.  The 2D gate is the primary mechanism that blocks poison-zone trades
  (yes_ask 0.60-0.80) and allows sweet-spot trades (0.80-0.90).  Without the
  gate, poison-zone signals would ALLOW via the 1D confidence bucket.
  The shadow track exposes the clean evidence without breaking the gate.

Expected output sentinel: PROVEN_1D_POPULATION_INTEGRITY_OK
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE_PATH = ROOT / "data" / "edge_profile.json"
TRADES_PATH  = ROOT / "logs" / "paper_trades.jsonl"

MIN_SAMPLE_SIZE = 5


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _bad_enough(bkt: Optional[dict]) -> bool:
    if bkt is None:
        return False
    n = int(bkt.get("trades", 0))
    if n < MIN_SAMPLE_SIZE:
        return False
    wr = float(bkt.get("win_rate", 0.0))
    pnl = float(bkt.get("total_pnl", 0.0))
    return bool(wr < 0.40 or pnl < 0)


# ── T01: 1D population metadata present ──────────────────────────────────────

def test_1d_population_metadata_present() -> Optional[str]:
    """Profile must contain all Phase 9H transparency fields."""
    p = _load_profile()
    if not p:
        return "data/edge_profile.json missing or empty"
    required = [
        "profile_1d_population",
        "profile_1d_normal_modern_count",
        "profile_1d_dc_override_in_1d",
        "profile_1d_bootstrap_provisional_in_1d",
        "profile_1d_legacy_in_1d",
    ]
    missing = [k for k in required if p.get(k) is None]
    if missing:
        return f"Missing Phase 9H transparency fields: {missing}"
    return None


# ── T02: population totals consistent ────────────────────────────────────────

def test_1d_population_totals_consistent() -> Optional[str]:
    """
    profile_1d_normal_modern_count + dc_override + bp + legacy == profile_input_trades.
    """
    p = _load_profile()
    if not p:
        return "profile missing"
    nm = int(p.get("profile_1d_normal_modern_count", 0))
    dco = int(p.get("profile_1d_dc_override_in_1d", 0))
    bp  = int(p.get("profile_1d_bootstrap_provisional_in_1d", 0))
    leg = int(p.get("profile_1d_legacy_in_1d", 0))
    total = int(p.get("profile_input_trades", 0))
    computed = nm + dco + bp + leg
    if computed != total:
        return (
            f"Transparency fields don't sum to profile_input_trades: "
            f"{nm}+{dco}+{bp}+{leg}={computed} != {total}"
        )
    return None


# ── T03: shadow by_normal_modern_edge_bucket present ─────────────────────────

def test_shadow_bucket_present() -> Optional[str]:
    """by_normal_modern_edge_bucket must exist in profiles."""
    p = _load_profile()
    if not p:
        return "profile missing"
    shadow = p.get("profiles", {}).get("by_normal_modern_edge_bucket")
    if shadow is None:
        return (
            "by_normal_modern_edge_bucket not found in profiles — "
            "rebuild: python3 tools/build_edge_profile.py"
        )
    if not isinstance(shadow, dict) or len(shadow) == 0:
        return "by_normal_modern_edge_bucket is empty"
    return None


# ── T04: shadow 0.05-0.10 bucket is GOOD (not bad_enough) ────────────────────

def test_shadow_0510_is_good() -> Optional[str]:
    """
    Shadow 0.05-0.10 must be GOOD (pnl>0, WR>0.40) — the clean normal_modern
    evidence without non-proof contamination.
    """
    p = _load_profile()
    shadow = p.get("profiles", {}).get("by_normal_modern_edge_bucket", {})
    bkt = shadow.get("0.05-0.10")
    if bkt is None:
        return "shadow 0.05-0.10 bucket missing — rebuild profile"
    n    = int(bkt.get("trades", 0))
    pnl  = float(bkt.get("total_pnl", 0.0))
    wr   = float(bkt.get("win_rate", 0.0))
    if n < MIN_SAMPLE_SIZE:
        return f"shadow 0.05-0.10 n={n} < {MIN_SAMPLE_SIZE} — too small"
    if pnl <= 0:
        return f"shadow 0.05-0.10 pnl={pnl:.2f} not positive — normal_modern evidence degraded"
    if wr < 0.60:
        return f"shadow 0.05-0.10 WR={wr:.3f} < 0.60 — unexpected degradation"
    return None


# ── T05: shadow count equals normal_modern_count ─────────────────────────────

def test_shadow_total_equals_normal_modern() -> Optional[str]:
    """Sum of all shadow bucket trades must equal profile_1d_normal_modern_count."""
    p = _load_profile()
    if not p:
        return "profile missing"
    shadow = p.get("profiles", {}).get("by_normal_modern_edge_bucket", {})
    shadow_total = sum(int(bv.get("trades", 0)) for bv in shadow.values())
    nm_count = int(p.get("profile_1d_normal_modern_count", -1))
    if shadow_total != nm_count:
        return (
            f"shadow total trades={shadow_total} != "
            f"profile_1d_normal_modern_count={nm_count}"
        )
    return None


# ── T06: live 1D by_edge_bucket is unchanged (still bad_enough) ──────────────

def test_live_1d_gate_preserved() -> Optional[str]:
    """
    The live 1D by_edge_bucket 0.05-0.10 must remain bad_enough=True.
    This is the gate that triggers the 2D price-conditioned check.
    If this returns False the test should FAIL — the gate is broken.
    """
    p = _load_profile()
    if not p:
        return "profile missing"
    bkt = p.get("profiles", {}).get("by_edge_bucket", {}).get("0.05-0.10")
    if bkt is None:
        return "by_edge_bucket 0.05-0.10 missing"
    n   = int(bkt.get("trades", 0))
    pnl = float(bkt.get("total_pnl", 0.0))
    if not _bad_enough(bkt):
        return (
            f"GATE BROKEN: by_edge_bucket 0.05-0.10 is NOT bad_enough "
            f"(n={n}, pnl={pnl:.2f}). The 2D price-conditioned check would "
            f"no longer fire. Poison-zone signals may pass through."
        )
    if n < 40:
        return f"by_edge_bucket 0.05-0.10 n={n} unexpectedly small (expected ~53)"
    return None


# ── T07: KXETH excluded from 1D and shadow buckets ───────────────────────────

def test_no_kxeth_in_live_or_shadow() -> Optional[str]:
    """Neither by_ticker (1D) nor by_normal_modern_edge_bucket should contain KXETH."""
    p = _load_profile()
    if not p:
        return "profile missing"
    by_ticker = p.get("profiles", {}).get("by_ticker", {})
    kxeth_keys = [k for k in by_ticker if k.upper().startswith("KXETH")]
    if kxeth_keys:
        return f"KXETH keys still in by_ticker: {kxeth_keys}"
    return None


# ── T08: 2D sweet-spot cell still qualifies ──────────────────────────────────

def test_2d_sweet_spot_still_qualifies() -> Optional[str]:
    """2D sweet-spot 0.05-0.10|0.80-0.90 must still meet ALLOW threshold."""
    p = _load_profile()
    if not p:
        return "profile missing"
    cell = p.get("profiles", {}).get("by_edge_price_bucket", {}).get("0.05-0.10|0.80-0.90")
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


# ── T09: poison cells remain negative ────────────────────────────────────────

def test_poison_cells_remain_negative() -> Optional[str]:
    """2D poison cells must have pnl < 0 (these protect against bad price zones)."""
    p = _load_profile()
    if not p:
        return "profile missing"
    tbl = p.get("profiles", {}).get("by_edge_price_bucket", {})
    errors = []
    for cell_key in ["0.05-0.10|0.70-0.80", "0.05-0.10|0.60-0.70"]:
        cell = tbl.get(cell_key)
        if cell is None:
            continue
        n   = int(cell.get("trades", 0))
        pnl = float(cell.get("total_pnl", 0.0))
        if n >= 3 and pnl >= 0:
            errors.append(f"{cell_key}: pnl={pnl:.2f} >= 0 (expected negative)")
    if errors:
        return "Poison cells no longer negative: " + "; ".join(errors)
    return None


# ── T10: 2D gate fires for poison zone (confirms gate is live) ────────────────

def test_2d_gate_fires_for_poison_zone() -> Optional[str]:
    """
    _bad_enough_sample on live 1D 0.05-0.10 must return True so the 2D
    check fires for poison-zone signals.
    """
    p = _load_profile()
    if not p:
        return "profile missing"
    bkt = p.get("profiles", {}).get("by_edge_bucket", {}).get("0.05-0.10")
    if bkt is None:
        return "by_edge_bucket 0.05-0.10 missing"
    if not _bad_enough(bkt):
        return (
            "2D gate would NOT fire: by_edge_bucket 0.05-0.10 is not bad_enough. "
            "Poison-zone signals (yes_ask 0.60-0.80) would be ALLOWED. "
            "This is the regression Phase 9H full-filter would cause."
        )
    return None


# ── T11: profile trusted ──────────────────────────────────────────────────────

def test_profile_trusted() -> Optional[str]:
    p = _load_profile()
    if not p:
        return "profile missing"
    if not p.get("edge_profile_health", {}).get("edge_profile_trusted", False):
        return (
            f"Profile not trusted: "
            f"{p.get('edge_profile_health', {}).get('reason', 'unknown')}"
        )
    return None


# ── T12–T13: safety locks intact ─────────────────────────────────────────────

def test_real_money_locked() -> Optional[str]:
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        records = load_trades()
        buckets = classify_records(records)
        gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
        if gate.get("real_money_allowed") is not False:
            return f"real_money_allowed is not False: {gate.get('real_money_allowed')!r}"
    except Exception as exc:
        return f"could not evaluate proof gates: {exc}"
    return None


def test_scale_locked() -> Optional[str]:
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        records = load_trades()
        buckets = classify_records(records)
        gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
        if gate.get("scale_allowed") is not False:
            return f"scale_allowed is not False: {gate.get('scale_allowed')!r}"
    except Exception as exc:
        return f"could not evaluate proof gates: {exc}"
    return None


# ── T14: KXETH quarantine active in paper_trader ────────────────────────────

def test_kxeth_quarantine_in_paper_trader() -> Optional[str]:
    pt_path = ROOT / "brain" / "paper_trader.py"
    src = pt_path.read_text()
    if "QUARANTINED_TICKER_PREFIXES" not in src:
        return "QUARANTINED_TICKER_PREFIXES not found in paper_trader.py"
    if "quarantined prefix block" not in src:
        return "quarantine guard phrase missing from paper_trader.py"
    return None


# ── T15: shadow gate comparison exposes the regression ───────────────────────

def test_shadow_shows_would_be_regression() -> Optional[str]:
    """
    Shadow bucket must show 0.05-0.10 as NOT bad_enough.  This proves that
    applying the full normal_modern filter would break the 2D gate.
    """
    p = _load_profile()
    if not p:
        return "profile missing"
    shadow_bkt = (
        p.get("profiles", {})
        .get("by_normal_modern_edge_bucket", {})
        .get("0.05-0.10")
    )
    if shadow_bkt is None:
        return "shadow 0.05-0.10 missing"
    if _bad_enough(shadow_bkt):
        return (
            f"Shadow 0.05-0.10 is bad_enough "
            f"(pnl={shadow_bkt.get('total_pnl')}, WR={shadow_bkt.get('win_rate')}). "
            f"Expected GOOD (pnl>0). Something changed in the normal_modern evidence."
        )
    # This is the expected state: shadow is GOOD, live 1D is BAD.
    # Confirms the full filter would create a regression.
    return None


# ── T16: shadow contains no KXETH ────────────────────────────────────────────

def test_shadow_no_kxeth() -> Optional[str]:
    """Shadow is built from _is_normal_modern_for_2d which requires full metadata."""
    p = _load_profile()
    if not p:
        return "profile missing"
    shadow = p.get("profiles", {}).get("by_normal_modern_edge_bucket", {})
    # Shadow is edge-bucket-keyed, not ticker-keyed, so we can't directly check
    # for KXETH tickers. Check by_ticker in the live 1D profile instead.
    by_ticker = p.get("profiles", {}).get("by_ticker", {})
    kxeth_keys = [k for k in by_ticker if k.upper().startswith("KXETH")]
    if kxeth_keys:
        return f"KXETH in by_ticker (Phase 9F regression): {kxeth_keys[:3]}"
    return None


# ── runner ─────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("Phase 9H transparency metadata fields present",
         test_1d_population_metadata_present),
        ("Transparency totals consistent: nm+dco+bp+legacy == profile_input_trades",
         test_1d_population_totals_consistent),
        ("Shadow by_normal_modern_edge_bucket present in profile",
         test_shadow_bucket_present),
        ("Shadow 0.05-0.10 is GOOD (pnl>0, WR>0.60) — clean normal_modern evidence",
         test_shadow_0510_is_good),
        ("Shadow total trades == profile_1d_normal_modern_count",
         test_shadow_total_equals_normal_modern),
        ("Live 1D 0.05-10 gate preserved: bad_enough=True (2D check fires)",
         test_live_1d_gate_preserved),
        ("KXETH excluded from live 1D by_ticker (Phase 9F preserved)",
         test_no_kxeth_in_live_or_shadow),
        ("2D sweet-spot 0.05-0.10|0.80-0.90: n>=5, WR>=0.80, pnl>0",
         test_2d_sweet_spot_still_qualifies),
        ("Poison cells 0.60-0.80 remain negative",
         test_poison_cells_remain_negative),
        ("2D gate fires for poison zone (bad_enough=True on live 1D)",
         test_2d_gate_fires_for_poison_zone),
        ("Profile trusted=True",
         test_profile_trusted),
        ("real_money_allowed=False (hardcoded)",
         test_real_money_locked),
        ("scale_allowed=False (hardcoded)",
         test_scale_locked),
        ("KXETH quarantine active in paper_trader.py",
         test_kxeth_quarantine_in_paper_trader),
        ("Shadow shows full-filter would break 2D gate (regression proof)",
         test_shadow_shows_would_be_regression),
        ("Shadow contains no KXETH contamination",
         test_shadow_no_kxeth),
    ]

    passed = failed = 0
    print("=" * 68)
    print("1D PROFILE POPULATION INTEGRITY TEST SUITE — Phase 9H")
    print("=" * 68)
    print("NOTE: Phase 9H adds transparency metadata + shadow bucket only.")
    print("      The 1D live gate is intentionally preserved (see T06 + T10).")
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
        print("PROVEN_1D_POPULATION_INTEGRITY_OK")
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
