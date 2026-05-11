#!/usr/bin/env python3
"""
tools/test_edge_profile_kxeth_exclusion.py
------------------------------------------
Phase 9F test suite — KXETH exclusion from all edge profile buckets.

Verifies:
  1.  build_edge_profile.py has a centralized _is_excluded_ticker helper
  2.  All 1D profile aggregations exclude KXETH tickers
  3.  by_ticker has no KXETH-prefixed keys
  4.  by_edge_bucket excludes KXETH-derived records (n count reduced vs raw)
  5.  by_confidence_bucket excludes KXETH records
  6.  by_market_type excludes KXETH records
  7.  by_edge_price_bucket (2D) still exists and is KXETH-free
  8.  Sweet-spot cell 0.05-0.10|0.80-0.90: n>=5, WR>=0.80, pnl>0
  9.  Poison cells 0.05-0.10|0.70-0.80 and 0.05-0.10|0.60-0.70: pnl<0
 10.  Profile remains trusted
 11.  real_money_allowed remains False (hardcoded lock)
 12.  scale_allowed remains False (hardcoded lock)
 13.  GLOBAL_FORCED_LEARNING_MODE remains True (Kelly disabled)
 14.  KXETH quarantine still active in paper_trader
 15.  QUARANTINED_TICKER_PREFIXES feeds _PROFILE_EXCLUDED_PREFIXES

Expected result when all pass: PROVEN_KXETH_EXCLUSION_OK
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE_PATH  = ROOT / "data" / "edge_profile.json"
BUILD_SCRIPT  = ROOT / "tools" / "build_edge_profile.py"
TRADES_PATH   = ROOT / "logs" / "paper_trades.jsonl"
KXETH_PREFIX  = "KXETH"


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _kxeth_in_section(section: dict) -> list[str]:
    return [k for k in section if KXETH_PREFIX in k.upper()]


# ── T01: centralized helper exists in build script ────────────────────────────

def test_build_script_has_centralized_helper() -> Optional[str]:
    """_is_excluded_ticker must be defined in build_edge_profile.py."""
    src = BUILD_SCRIPT.read_text(encoding="utf-8") if BUILD_SCRIPT.exists() else ""
    if not src:
        return "build_edge_profile.py not found"
    if "_is_excluded_ticker" not in src:
        return "_is_excluded_ticker helper not found in build_edge_profile.py"
    if "_PROFILE_EXCLUDED_PREFIXES" not in src:
        return "_PROFILE_EXCLUDED_PREFIXES constant not found in build_edge_profile.py"
    return None


# ── T02: all 1D loops use clean_settled_for_profile ───────────────────────────

def test_build_script_uses_filtered_source() -> Optional[str]:
    """Main 1D loop must iterate clean_settled_for_profile, not raw clean_settled."""
    src = BUILD_SCRIPT.read_text(encoding="utf-8") if BUILD_SCRIPT.exists() else ""
    if not src:
        return "build_edge_profile.py not found"
    if "clean_settled_for_profile" not in src:
        return "clean_settled_for_profile not found — 1D loop not updated"
    # The raw `for rec in clean_settled:` must no longer appear in the 1D loop context.
    # Look for lines that start `for rec in clean_settled` but NOT `clean_settled_for_profile`.
    loop_lines = [
        ln.strip() for ln in src.splitlines()
        if ln.strip().startswith("for rec in clean_settled")
        and "clean_settled_for_profile" not in ln
    ]
    if loop_lines:
        return (
            f"Found raw 'for rec in clean_settled' loops not updated to "
            f"clean_settled_for_profile: {loop_lines}"
        )
    return None


# ── T03: by_ticker has no KXETH keys ─────────────────────────────────────────

def test_by_ticker_no_kxeth() -> Optional[str]:
    """profile['profiles']['by_ticker'] must have zero KXETH-prefix keys."""
    profile = _load_profile()
    if not profile:
        return "edge_profile.json missing"
    bt = profile.get("profiles", {}).get("by_ticker", {})
    kxeth_keys = _kxeth_in_section(bt)
    if kxeth_keys:
        return f"KXETH keys still present in by_ticker: {kxeth_keys[:5]}"
    return None


# ── T04: by_edge_bucket n decreased vs total settled ──────────────────────────

def test_by_edge_bucket_kxeth_excluded() -> Optional[str]:
    """
    Total n in by_edge_bucket must equal profile_input_trades (non-KXETH),
    not clean_settled_trades (total).
    """
    profile = _load_profile()
    if not profile:
        return "edge_profile.json missing"
    pit    = profile.get("profile_input_trades")
    cst    = profile.get("clean_settled_trades")
    pkxeth = profile.get("profile_kxeth_excluded_count", 0)
    if pit is None:
        return "profile_input_trades field missing — profile needs rebuild after Phase 9F patch"
    if pkxeth == 0:
        return "profile_kxeth_excluded_count=0 — KXETH may not have been excluded"
    # Sum all n across by_edge_bucket; should equal profile_input_trades
    # (every record lands in exactly one edge bucket).
    by_edge = profile.get("profiles", {}).get("by_edge_bucket", {})
    total_n = sum(int(v.get("trades", 0)) for v in by_edge.values())
    if total_n != pit:
        return (
            f"by_edge_bucket total n={total_n} != profile_input_trades={pit}. "
            "Mismatch may indicate some records were not counted."
        )
    if total_n >= cst:
        return (
            f"by_edge_bucket total n={total_n} >= clean_settled_trades={cst}. "
            "KXETH may still be included."
        )
    return None


# ── T05: by_confidence_bucket excludes KXETH ─────────────────────────────────

def test_by_confidence_bucket_kxeth_excluded() -> Optional[str]:
    """
    0.80-0.90 confidence bucket pnl must be > +10.0 (was +6.20 with KXETH;
    should be ~+14.35 without — or at minimum improved over old value).
    """
    profile = _load_profile()
    if not profile:
        return "edge_profile.json missing"
    cb = profile.get("profiles", {}).get("by_confidence_bucket", {})
    cell = cb.get("0.80-0.90")
    if cell is None:
        return "0.80-0.90 confidence bucket missing"
    pnl = float(cell.get("total_pnl", 0))
    if pnl <= 6.20:
        return (
            f"0.80-0.90 conf bucket pnl={pnl:.2f} <= 6.20 (pre-exclusion value). "
            "KXETH losses may still be contaminating this bucket."
        )
    # Also check total n decreased
    pit = profile.get("profile_input_trades")
    total_n = sum(int(v.get("trades", 0)) for v in cb.values())
    cst = profile.get("clean_settled_trades", 0)
    if total_n >= cst:
        return (
            f"by_confidence_bucket total n={total_n} >= clean_settled_trades={cst}. "
            "KXETH may still be included."
        )
    return None


# ── T06: by_market_type excludes KXETH records ───────────────────────────────

def test_by_market_type_kxeth_excluded() -> Optional[str]:
    """
    by_market_type['CRYPTO'] n must equal profile_input_trades (all trades are CRYPTO).
    """
    profile = _load_profile()
    if not profile:
        return "edge_profile.json missing"
    pit = profile.get("profile_input_trades")
    if pit is None:
        return "profile_input_trades field missing"
    mt = profile.get("profiles", {}).get("by_market_type", {})
    crypto = mt.get("CRYPTO", {})
    n = int(crypto.get("trades", 0))
    if n != pit:
        return (
            f"by_market_type['CRYPTO'] n={n} != profile_input_trades={pit}. "
            "KXETH records may still be counted here."
        )
    return None


# ── T07: by_edge_price_bucket (2D) still exists ───────────────────────────────

def test_2d_table_still_present() -> Optional[str]:
    """by_edge_price_bucket must exist and be non-empty after rebuild."""
    profile = _load_profile()
    if not profile:
        return "edge_profile.json missing"
    table = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    if not table:
        return "by_edge_price_bucket missing or empty after rebuild"
    kxeth_keys = _kxeth_in_section(table)
    if kxeth_keys:
        return f"KXETH keys found in by_edge_price_bucket: {kxeth_keys}"
    return None


# ── T08: sweet-spot cell still qualifies ──────────────────────────────────────

def test_sweet_spot_cell_qualifies() -> Optional[str]:
    """0.05-0.10|0.80-0.90 must have n>=5, WR>=0.80, pnl>0."""
    profile = _load_profile()
    table   = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    cell_key = "0.05-0.10|0.80-0.90"
    cell = table.get(cell_key)
    if cell is None:
        return f"sweet-spot cell '{cell_key}' missing after rebuild"
    n   = int(cell.get("trades", 0))
    wr  = float(cell.get("win_rate", 0))
    pnl = float(cell.get("total_pnl", 0))
    if n < 5:
        return f"sweet-spot n={n} < 5"
    if wr < 0.80:
        return f"sweet-spot WR={wr:.3f} < 0.80"
    if pnl <= 0:
        return f"sweet-spot pnl={pnl:.2f} <= 0"
    return None


# ── T09: poison cells remain negative ────────────────────────────────────────

def test_poison_cells_remain_negative() -> Optional[str]:
    """Both known poison cells must still have total_pnl < 0 after rebuild."""
    profile = _load_profile()
    table   = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    poison_keys = ["0.05-0.10|0.70-0.80", "0.05-0.10|0.60-0.70"]
    for ck in poison_keys:
        cell = table.get(ck)
        if cell is None:
            continue
        pnl = float(cell.get("total_pnl", 0))
        if pnl >= 0:
            return f"poison cell '{ck}' pnl={pnl:.2f} >= 0 after rebuild — unexpected"
    return None


# ── T10: profile remains trusted ─────────────────────────────────────────────

def test_profile_trusted() -> Optional[str]:
    """edge_profile_health.edge_profile_trusted must be True."""
    profile = _load_profile()
    if not profile:
        return "edge_profile.json missing"
    health = profile.get("edge_profile_health", {})
    if not health.get("edge_profile_trusted"):
        return f"profile untrusted: {health.get('reason', 'unknown')}"
    nm = int(health.get("normal_council_approved_modern_trades", 0))
    if nm < 10:
        return f"normal_council_approved_modern_trades={nm} < 10 trust gate"
    return None


# ── T11: real_money_allowed remains False ────────────────────────────────────

def test_real_money_stays_locked() -> Optional[str]:
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        records = load_trades()
        buckets = classify_records(records)
        gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
        if gate.get("real_money_allowed") is not False:
            return f"real_money_allowed is not False: {gate.get('real_money_allowed')!r}"
    except Exception as exc:
        return f"gate check error: {exc}"
    return None


# ── T12: scale_allowed remains False ─────────────────────────────────────────

def test_scale_stays_locked() -> Optional[str]:
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        records = load_trades()
        buckets = classify_records(records)
        gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
        if gate.get("scale_allowed") is not False:
            return f"scale_allowed is not False: {gate.get('scale_allowed')!r}"
    except Exception as exc:
        return f"gate check error: {exc}"
    return None


# ── T13: Kelly disabled ───────────────────────────────────────────────────────

def test_kelly_disabled() -> Optional[str]:
    try:
        from config.trading_config import GLOBAL_FORCED_LEARNING_MODE
        if not GLOBAL_FORCED_LEARNING_MODE:
            return "GLOBAL_FORCED_LEARNING_MODE is False — Kelly may be enabled"
    except Exception as exc:
        return f"config import error: {exc}"
    return None


# ── T14: KXETH quarantine still active in paper_trader ───────────────────────

def test_kxeth_quarantine_in_paper_trader() -> Optional[str]:
    """paper_trader.py must still import and use QUARANTINED_TICKER_PREFIXES."""
    pt_path = ROOT / "brain" / "paper_trader.py"
    src = pt_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_import = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "trading_config" in (node.module or ""):
                names = [alias.name for alias in node.names]
                if "QUARANTINED_TICKER_PREFIXES" in names:
                    found_import = True
    if not found_import:
        return "QUARANTINED_TICKER_PREFIXES not imported in paper_trader.py"
    if "quarantined prefix block" not in src:
        return "quarantine guard phrase 'quarantined prefix block' missing from paper_trader.py"
    return None


# ── T15: QUARANTINED_TICKER_PREFIXES feeds _PROFILE_EXCLUDED_PREFIXES ─────────

def test_exclusion_uses_quarantine_config() -> Optional[str]:
    """
    build_edge_profile.py must import QUARANTINED_TICKER_PREFIXES (or fall back)
    and derive _PROFILE_EXCLUDED_PREFIXES from it.
    """
    src = BUILD_SCRIPT.read_text(encoding="utf-8") if BUILD_SCRIPT.exists() else ""
    if not src:
        return "build_edge_profile.py not found"
    if "QUARANTINED_TICKER_PREFIXES" not in src:
        return "QUARANTINED_TICKER_PREFIXES not referenced in build_edge_profile.py"
    if "_PROFILE_EXCLUDED_PREFIXES" not in src:
        return "_PROFILE_EXCLUDED_PREFIXES not defined"
    # Verify the runtime value matches config
    try:
        from config.trading_config import QUARANTINED_TICKER_PREFIXES
        # Import the build script's exclusion set without running main()
        import importlib.util
        spec = importlib.util.spec_from_file_location("bep", BUILD_SCRIPT)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        excluded = mod._PROFILE_EXCLUDED_PREFIXES
        for pfx in QUARANTINED_TICKER_PREFIXES:
            if str(pfx).upper() not in excluded:
                return (
                    f"QUARANTINED prefix '{pfx}' not in _PROFILE_EXCLUDED_PREFIXES={excluded}"
                )
    except Exception as exc:
        return f"runtime exclusion check error: {exc}"
    return None


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("build_edge_profile.py has _is_excluded_ticker helper",
         test_build_script_has_centralized_helper),
        ("main 1D loop uses clean_settled_for_profile",
         test_build_script_uses_filtered_source),
        ("by_ticker has no KXETH keys",
         test_by_ticker_no_kxeth),
        ("by_edge_bucket n matches profile_input_trades (KXETH excluded)",
         test_by_edge_bucket_kxeth_excluded),
        ("by_confidence_bucket 0.80-0.90 pnl > +6.20 (KXETH losses removed)",
         test_by_confidence_bucket_kxeth_excluded),
        ("by_market_type CRYPTO n matches profile_input_trades",
         test_by_market_type_kxeth_excluded),
        ("by_edge_price_bucket (2D table) still present and KXETH-free",
         test_2d_table_still_present),
        ("sweet-spot cell 0.05-0.10|0.80-0.90: n>=5, WR>=0.80, pnl>0",
         test_sweet_spot_cell_qualifies),
        ("poison cells 0.05-0.10|0.70-0.80 and 0.60-0.70 remain pnl<0",
         test_poison_cells_remain_negative),
        ("profile trusted after rebuild",
         test_profile_trusted),
        ("real_money_allowed=False",
         test_real_money_stays_locked),
        ("scale_allowed=False",
         test_scale_stays_locked),
        ("GLOBAL_FORCED_LEARNING_MODE=True (Kelly disabled)",
         test_kelly_disabled),
        ("paper_trader imports and uses QUARANTINED_TICKER_PREFIXES",
         test_kxeth_quarantine_in_paper_trader),
        ("QUARANTINED_TICKER_PREFIXES feeds _PROFILE_EXCLUDED_PREFIXES",
         test_exclusion_uses_quarantine_config),
    ]

    passed = 0
    failed = 0
    print("=" * 68)
    print("KXETH EXCLUSION TEST SUITE — Phase 9F")
    print("=" * 68)
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
        print("PROVEN_KXETH_EXCLUSION_OK")
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
