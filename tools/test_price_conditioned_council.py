#!/usr/bin/env python3
"""
tools/test_price_conditioned_council.py
----------------------------------------
Phase 9A test suite — Price-Conditioned Council.

Verifies:
  1. Config exports PRICE_CONDITIONED_COUNCIL_ENABLED, MIN_N, MIN_WR
  2. edge_profile.json contains by_edge_price_bucket (requires rebuilt profile)
  3. Sweet-spot 2D cell ("0.05-0.10|0.80-0.90") is positive in rebuilt profile
  4. Poison 2D cell ("0.05-0.10|0.70-0.80") is negative in rebuilt profile
  5. 2D table is built from normal_modern non-KXETH trades only
  6. critique_signal with sweet-spot yes_ask → ALLOW (2D override fires)
  7. critique_signal with poison yes_ask → BLOCK (2D path does not override)
  8. KXETH still blocked at paper_trader quarantine (before council)
  9. Safety locks remain intact: real_money_allowed=False, scale_allowed=False
 10. MIN_EDGE=0.03, MIN_CONFIDENCE=0.65 unchanged

NOTE: Tests 2–7 require the profile to be rebuilt first:
  python3 tools/build_edge_profile.py

Expected result when all pass: PROVEN_PRICE_CONDITIONED_COUNCIL_OK
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text())
    except Exception:
        return {}


# ── 1. Config flags ───────────────────────────────────────────────────────────

def test_config_has_price_conditioned_enabled() -> Optional[str]:
    """PRICE_CONDITIONED_COUNCIL_ENABLED must be importable and True."""
    try:
        from config.trading_config import PRICE_CONDITIONED_COUNCIL_ENABLED
    except ImportError:
        return "PRICE_CONDITIONED_COUNCIL_ENABLED not found in config.trading_config"
    if not isinstance(PRICE_CONDITIONED_COUNCIL_ENABLED, bool):
        return f"PRICE_CONDITIONED_COUNCIL_ENABLED is not bool: {type(PRICE_CONDITIONED_COUNCIL_ENABLED)}"
    if not PRICE_CONDITIONED_COUNCIL_ENABLED:
        return "PRICE_CONDITIONED_COUNCIL_ENABLED is False — feature is disabled"
    return None


def test_config_has_min_n_and_wr() -> Optional[str]:
    """PRICE_CONDITIONED_MIN_N=5 and PRICE_CONDITIONED_MIN_WR=0.80."""
    try:
        from config.trading_config import PRICE_CONDITIONED_MIN_N, PRICE_CONDITIONED_MIN_WR
    except ImportError as e:
        return f"config import error: {e}"
    if PRICE_CONDITIONED_MIN_N != 5:
        return f"PRICE_CONDITIONED_MIN_N={PRICE_CONDITIONED_MIN_N} (expected 5)"
    if abs(PRICE_CONDITIONED_MIN_WR - 0.80) > 1e-9:
        return f"PRICE_CONDITIONED_MIN_WR={PRICE_CONDITIONED_MIN_WR} (expected 0.80)"
    return None


# ── 2. Profile structure ──────────────────────────────────────────────────────

def test_profile_has_2d_table() -> Optional[str]:
    """by_edge_price_bucket must exist in edge_profile.json profiles."""
    profile = _load_profile()
    if not profile:
        return "data/edge_profile.json missing or empty"
    if "by_edge_price_bucket" not in profile.get("profiles", {}):
        return (
            "by_edge_price_bucket not found in edge_profile.json — "
            "run: python3 tools/build_edge_profile.py"
        )
    table = profile["profiles"]["by_edge_price_bucket"]
    if not isinstance(table, dict) or len(table) == 0:
        return "by_edge_price_bucket is empty"
    return None


# ── 3. Sweet-spot cell positive ───────────────────────────────────────────────

def test_sweet_spot_cell_positive() -> Optional[str]:
    """2D cell '0.05-0.10|0.80-0.90' must be n>=5, WR>=0.80, pnl>0."""
    profile = _load_profile()
    table = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    cell_key = "0.05-0.10|0.80-0.90"
    cell = table.get(cell_key)
    if cell is None:
        return (
            f"cell '{cell_key}' not found in by_edge_price_bucket — "
            "rebuild profile first"
        )
    n   = int(cell.get("trades", 0))
    wr  = float(cell.get("win_rate", 0.0))
    pnl = float(cell.get("total_pnl", 0.0))
    if n < 5:
        return f"sweet-spot cell n={n} < 5 (insufficient evidence)"
    if wr < 0.80:
        return f"sweet-spot cell WR={wr:.3f} < 0.80"
    if pnl <= 0:
        return f"sweet-spot cell total_pnl={pnl:.2f} <= 0"
    return None


# ── 4. Poison cell negative ───────────────────────────────────────────────────

def test_poison_cell_negative() -> Optional[str]:
    """2D cell '0.05-0.10|0.70-0.80' must have total_pnl < 0."""
    profile = _load_profile()
    table = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    cell_key = "0.05-0.10|0.70-0.80"
    cell = table.get(cell_key)
    if cell is None:
        return f"cell '{cell_key}' not found — rebuild profile first"
    pnl = float(cell.get("total_pnl", 0.0))
    n   = int(cell.get("trades", 0))
    if n < 3:
        return f"poison cell too small to confirm: n={n}"
    if pnl >= 0:
        return f"poison cell total_pnl={pnl:.2f} >= 0 — expected negative"
    return None


# ── 5. 2D table uses normal_modern only ──────────────────────────────────────

def test_2d_table_uses_normal_modern_only() -> Optional[str]:
    """
    2D cell trade count must be <= normal_modern non-KXETH count.
    If all_clean_settled were used the counts would be higher.
    Computes normal_modern directly using the same criteria as build_edge_profile.py.
    """
    import json as _json

    TRADES_PATH = ROOT / "logs" / "paper_trades.jsonl"
    if not TRADES_PATH.exists():
        return "logs/paper_trades.jsonl not found"

    # Replicate the normal_modern_for_2d filter from build_edge_profile.py
    _MODERN_KEYS = [
        "council_decision", "bootstrap_provisional",
        "data_collection_override", "risk_edge", "bootstrap_era_council_allow",
    ]

    def _is_nm(r: dict) -> bool:
        if any(r.get(k) is None for k in _MODERN_KEYS):
            return False
        if r.get("data_collection_override"):
            return False
        if r.get("bootstrap_provisional"):
            return False
        return True

    records: list = []
    with TRADES_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(_json.loads(line))
            except Exception:
                pass

    settled = [r for r in records if r.get("status") == "SETTLED"]
    nm_nk = [
        r for r in settled
        if _is_nm(r) and not str(r.get("ticker", "")).upper().startswith("KXETH")
    ]
    nm_count = len(nm_nk)

    profile = _load_profile()
    table = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    total_2d = sum(int(c.get("trades", 0)) for c in table.values())

    cell = table.get("0.05-0.10|0.80-0.90")
    if cell is None:
        return "sweet-spot cell missing — rebuild profile first"
    cell_n = int(cell.get("trades", 0))

    # Each normal_modern trade appears in at most one 2D cell, so total_2d <= nm_count
    if total_2d > nm_count:
        return (
            f"total 2D table trades={total_2d} > normal_modern_non_kxeth={nm_count} — "
            "2D table may include non-normal trades"
        )
    return None


# ── 6. Critic allows sweet-spot via 2D override ──────────────────────────────

def test_critic_allows_sweet_spot_with_price() -> Optional[str]:
    """
    critique_signal with yes_ask=0.85, edge=0.062, conf=0.80 should return
    ALLOW (price-conditioned override fires).
    Requires profile rebuilt with by_edge_price_bucket.
    """
    from brain.critic_brain import critique_signal

    signal = {
        "confidence": 0.80,
        "edge": 0.062,
        "yes_ask": 0.85,
        "ticker": "KXBTCD-TEST",
        "strategy": "SIGNAL",
        "market_type": "CRYPTO",
        "action": "BET_YES",
    }
    result = critique_signal(signal)
    decision = str(result.get("decision", "")).upper()
    if decision != "ALLOW":
        return (
            f"Expected ALLOW for sweet-spot signal, got {decision!r}. "
            f"Reason: {result.get('reason', '')[:200]}"
        )
    reason = str(result.get("reason", ""))
    if "price_conditioned" not in reason.lower():
        return (
            f"ALLOW was returned but reason does not mention price_conditioned. "
            f"Reason: {reason[:200]}"
        )
    return None


# ── 7. Critic blocks poison via 1D (2D does not override) ────────────────────

def test_critic_blocks_poison_price_zone() -> Optional[str]:
    """
    critique_signal with yes_ask=0.75 (poison zone) should NOT be allowed
    via the 2D override — 2D cell is poison → falls through to 1D → BLOCK.
    """
    from brain.critic_brain import critique_signal

    signal = {
        "confidence": 0.80,
        "edge": 0.062,
        "yes_ask": 0.75,  # poison price zone 0.70-0.80
        "ticker": "KXBTCD-TEST",
        "strategy": "SIGNAL",
        "market_type": "CRYPTO",
        "action": "BET_YES",
    }
    result = critique_signal(signal)
    decision = str(result.get("decision", "")).upper()
    if decision == "ALLOW" and "price_conditioned" in str(result.get("reason", "")).lower():
        return (
            "2D path allowed a poison-zone signal — this must NOT happen. "
            f"Reason: {result.get('reason', '')[:200]}"
        )
    # Expected: BLOCK (1D edge bucket contaminated AND no 2D override for poison cell)
    if decision != "BLOCK":
        return f"Expected BLOCK for poison-zone signal, got {decision!r}"
    return None


# ── 8. KXETH quarantine still wins (paper_trader level) ──────────────────────

def test_paper_trader_imports_quarantine() -> Optional[str]:
    """paper_trader.py must still import QUARANTINED_TICKER_PREFIXES."""
    pt_path = ROOT / "brain" / "paper_trader.py"
    source = pt_path.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "trading_config" in (node.module or ""):
                names = [alias.name for alias in node.names]
                if "QUARANTINED_TICKER_PREFIXES" in names:
                    return None
    return "QUARANTINED_TICKER_PREFIXES not found in paper_trader.py imports"


def test_kxeth_quarantine_phrase_in_paper_trader() -> Optional[str]:
    """Quarantine guard phrase must still be present in paper_trader.py."""
    pt_path = ROOT / "brain" / "paper_trader.py"
    source = pt_path.read_text()
    if "quarantined prefix block" not in source:
        return "quarantine guard phrase 'quarantined prefix block' missing from paper_trader.py"
    return None


# ── 9. Safety locks intact ────────────────────────────────────────────────────

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


# ── 10. Thresholds unchanged ──────────────────────────────────────────────────

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
        ("config exports PRICE_CONDITIONED_COUNCIL_ENABLED=True",
         test_config_has_price_conditioned_enabled),
        ("config exports PRICE_CONDITIONED_MIN_N=5, MIN_WR=0.80",
         test_config_has_min_n_and_wr),
        ("edge_profile.json has by_edge_price_bucket",
         test_profile_has_2d_table),
        ("sweet-spot cell '0.05-0.10|0.80-0.90' n>=5, WR>=0.80, pnl>0",
         test_sweet_spot_cell_positive),
        ("poison cell '0.05-0.10|0.70-0.80' pnl<0",
         test_poison_cell_negative),
        ("2D table trade counts bounded by normal_modern non-KXETH",
         test_2d_table_uses_normal_modern_only),
        ("Critic ALLOWs sweet-spot signal via price-conditioned override",
         test_critic_allows_sweet_spot_with_price),
        ("Critic BLOCKs poison-zone signal (no 2D override for poison)",
         test_critic_blocks_poison_price_zone),
        ("paper_trader imports QUARANTINED_TICKER_PREFIXES",
         test_paper_trader_imports_quarantine),
        ("quarantine guard phrase present in paper_trader.py",
         test_kxeth_quarantine_phrase_in_paper_trader),
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
    print("PRICE-CONDITIONED COUNCIL TEST SUITE — Phase 9A")
    print("=" * 68)
    print("NOTE: Tests 3–7 require: python3 tools/build_edge_profile.py first")
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
        print("PROVEN_PRICE_CONDITIONED_COUNCIL_OK")
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
