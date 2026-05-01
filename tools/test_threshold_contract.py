#!/usr/bin/env python3
"""
tools/test_threshold_contract.py
----------------------------------
Verifies that the threshold contract is:
  1. Centralized in config/trading_config.py
  2. Imported (not duplicated) by critic_brain.py
  3. Absent as a hardcoded local constant in market_scanner.py
  4. Present as threshold metadata on generated paper trade records
  5. Consistent: bootstrap ALLOW still works end-to-end

Uses temp log files. Never touches logs/paper_trades.jsonl.

Expected result: PROVEN_THRESHOLD_CONTRACT_OK
"""
from __future__ import annotations

import sys
import importlib
import tempfile
import ast
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── 1. Static config contract check ──────────────────────────────────────────

def test_config_exports_required_constants() -> str | None:
    """All threshold constants must be importable from config."""
    required = [
        "MIN_EDGE",
        "MIN_CONFIDENCE",
        "BOOTSTRAP_MIN_EDGE",
        "BOOTSTRAP_MIN_CONFIDENCE",
        "BOOTSTRAP_CONFIDENCE_ADJUSTMENT",
        "BOOTSTRAP_ALLOW_ENABLED",
        "BOOTSTRAP_PROVISIONAL_MODE",
        "DATA_COLLECTION_OVERRIDE_ENABLED",
        "GLOBAL_FORCED_LEARNING_MODE",
        "EDGE_DANGER_HIGH_EDGE_MIN",
    ]
    from config import trading_config as cfg
    missing = [name for name in required if not hasattr(cfg, name)]
    if missing:
        return f"config missing constants: {missing}"
    return None


def test_config_values_are_sane() -> str | None:
    """Threshold values must be in sensible ranges."""
    from config.trading_config import (
        MIN_EDGE, MIN_CONFIDENCE, BOOTSTRAP_MIN_EDGE, BOOTSTRAP_MIN_CONFIDENCE,
        BOOTSTRAP_CONFIDENCE_ADJUSTMENT,
    )
    errors = []
    if not (0 < MIN_EDGE < 0.20):
        errors.append(f"MIN_EDGE={MIN_EDGE} out of expected range (0, 0.20)")
    if not (0.5 < MIN_CONFIDENCE < 1.0):
        errors.append(f"MIN_CONFIDENCE={MIN_CONFIDENCE} out of expected range (0.5, 1.0)")
    if not (MIN_EDGE <= BOOTSTRAP_MIN_EDGE):
        errors.append(
            f"BOOTSTRAP_MIN_EDGE={BOOTSTRAP_MIN_EDGE} should be >= MIN_EDGE={MIN_EDGE}"
        )
    if not (0.5 < BOOTSTRAP_MIN_CONFIDENCE <= MIN_CONFIDENCE + 0.001):
        errors.append(
            f"BOOTSTRAP_MIN_CONFIDENCE={BOOTSTRAP_MIN_CONFIDENCE} unexpected vs MIN_CONFIDENCE={MIN_CONFIDENCE}"
        )
    if not (-0.20 <= BOOTSTRAP_CONFIDENCE_ADJUSTMENT <= 0.0):
        errors.append(
            f"BOOTSTRAP_CONFIDENCE_ADJUSTMENT={BOOTSTRAP_CONFIDENCE_ADJUSTMENT} should be <= 0"
        )
    return "; ".join(errors) if errors else None


# ── 2. Critic brain does NOT define its own threshold constants ───────────────

def test_critic_has_no_local_min_edge() -> str | None:
    """critic_brain.py must not define MIN_NORMAL_EDGE or a local BOOTSTRAP_CONFIDENCE_ADJUSTMENT."""
    source = (ROOT / "brain" / "critic_brain.py").read_text()
    tree = ast.parse(source)
    forbidden_names = {"MIN_NORMAL_EDGE", "BOOTSTRAP_CONFIDENCE_ADJUSTMENT"}
    local_assigns = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in forbidden_names:
                    local_assigns.add(target.id)
    if local_assigns:
        return (
            f"critic_brain.py still defines local threshold constants: {local_assigns}. "
            "These should be imported from config."
        )
    return None


def test_critic_imports_min_edge_from_config() -> str | None:
    """critic_brain.py must import MIN_EDGE from config."""
    source = (ROOT / "brain" / "critic_brain.py").read_text()
    if "MIN_EDGE" not in source:
        return "critic_brain.py does not reference MIN_EDGE at all"
    if "from config.trading_config import" not in source:
        return "critic_brain.py missing config import block"
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "config.trading_config":
                names = [alias.name for alias in node.names]
                if "MIN_EDGE" in names:
                    return None
    return "critic_brain.py does not import MIN_EDGE from config.trading_config"


# ── 3. Market scanner has no local MIN_EDGE constant ─────────────────────────

def test_scanner_has_no_local_min_edge() -> str | None:
    """market_scanner.py must not define a local MIN_EDGE = <number> constant."""
    source = (ROOT / "brain" / "market_scanner.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "MIN_EDGE":
                    return (
                        "market_scanner.py still defines a local MIN_EDGE constant. "
                        "The scanner does not apply an independent edge filter; "
                        "this value was misleading and should have been removed."
                    )
    return None


# ── 4. Paper trade records contain threshold metadata ─────────────────────────

def _market():
    from engine.edge_calculator import MarketData
    return MarketData(
        ticker="KXBTCD-15M-THRESHOLD-CONTRACT-TEST",
        yes_price=0.45,
        no_price=0.55,
        yes_bid=0.44,
        yes_ask=0.46,
        no_bid=0.54,
        no_ask=0.56,
        volume_24h=5000,
        spread=0.02,
        liquidity=2000,
        fee_rate=0.01,
        time_to_expiry=0.25,
        venue="kalshi",
    )


def test_trade_record_contains_threshold_metadata() -> str | None:
    """A paper trade opened via bootstrap_era_allow must carry threshold metadata fields."""
    import brain.paper_trader as _pt_mod

    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        tmp_log = Path(fh.name)

    def _fake_council(signal: dict) -> dict:
        return {
            "final_decision": "ALLOW",
            "final_confidence": signal.get("confidence", 0.67),
            "reason": "test threshold contract",
            "net_confidence_adjustment": -0.05,
            "edge_profile_health": {"edge_profile_trusted": False, "reason": "test"},
            "bootstrap_provisional": False,
            "bootstrap_era_allow": True,
        }

    try:
        with (
            patch.object(_pt_mod, "DATA_COLLECTION_OVERRIDE_ENABLED", False),
            patch.object(_pt_mod, "GLOBAL_FORCED_LEARNING_MODE", True),
            patch.object(_pt_mod, "EDGE_DANGER_BLOCK_HIGH_EDGE", False),
            patch.object(_pt_mod, "MAX_CONCURRENT_OPEN_TRADES", 99),
        ):
            from brain.paper_trader import PaperTrader
            trader = PaperTrader(bankroll=500.0)
            trader.open_trades = []
            trader.logger.log_file = str(tmp_log)
            trader.enable()

            with (
                patch("brain.decision_council.decide_signal", side_effect=_fake_council),
                patch.object(trader.risk_manager, "check_trade", return_value=(True, None)),
                patch.object(trader.risk_manager, "add_position"),
            ):
                result = trader.process_signal(
                    _market(),
                    estimated_prob=0.67,
                    strategy="SIGNAL",
                    intended_action="BET_YES",
                )
    finally:
        tmp_log.unlink(missing_ok=True)

    if result is None:
        return "trade was blocked — cannot check threshold metadata"

    required_fields = [
        "min_edge_at_entry",
        "min_confidence_at_entry",
        "bootstrap_min_edge_at_entry",
        "bootstrap_min_confidence_at_entry",
        "bootstrap_allow_enabled_at_entry",
    ]
    missing = [f for f in required_fields if f not in result]
    if missing:
        return f"trade record missing threshold metadata fields: {missing}"

    from config.trading_config import (
        MIN_EDGE, MIN_CONFIDENCE, BOOTSTRAP_MIN_EDGE,
        BOOTSTRAP_MIN_CONFIDENCE, BOOTSTRAP_ALLOW_ENABLED,
    )
    mismatches = []
    if result["min_edge_at_entry"] != MIN_EDGE:
        mismatches.append(f"min_edge_at_entry={result['min_edge_at_entry']} != config MIN_EDGE={MIN_EDGE}")
    if result["min_confidence_at_entry"] != MIN_CONFIDENCE:
        mismatches.append(f"min_confidence_at_entry={result['min_confidence_at_entry']} != MIN_CONFIDENCE={MIN_CONFIDENCE}")
    if result["bootstrap_min_edge_at_entry"] != BOOTSTRAP_MIN_EDGE:
        mismatches.append(f"bootstrap_min_edge_at_entry={result['bootstrap_min_edge_at_entry']} != BOOTSTRAP_MIN_EDGE={BOOTSTRAP_MIN_EDGE}")
    if result["bootstrap_min_confidence_at_entry"] != BOOTSTRAP_MIN_CONFIDENCE:
        mismatches.append(f"bootstrap_min_confidence_at_entry={result['bootstrap_min_confidence_at_entry']} != BOOTSTRAP_MIN_CONFIDENCE={BOOTSTRAP_MIN_CONFIDENCE}")
    if result["bootstrap_allow_enabled_at_entry"] != BOOTSTRAP_ALLOW_ENABLED:
        mismatches.append(f"bootstrap_allow_enabled_at_entry={result['bootstrap_allow_enabled_at_entry']} != BOOTSTRAP_ALLOW_ENABLED={BOOTSTRAP_ALLOW_ENABLED}")
    if mismatches:
        return "threshold metadata value mismatches: " + "; ".join(mismatches)

    return None


# ── 5. Bootstrap allow end-to-end ─────────────────────────────────────────────

def test_bootstrap_allow_still_works() -> str | None:
    """bootstrap_era_allow path must still fire after threshold contract changes."""
    import brain.critic_brain as _cb_mod
    from brain.critic_brain import critique_signal

    signal = {
        "confidence": 0.67,
        "edge": 0.21,
        "ticker": "KXBTCD-15M-THRESHOLD-CONTRACT-TEST",
        "strategy": "SIGNAL",
        "action": "BET_YES",
    }
    fake_profile = {"generated_at": "2026-01-01T00:00:00Z", "profiles": {}}
    untrusted_health = {
        "edge_profile_trusted": False,
        "reason": "test isolation",
        "normal_council_approved_modern_trades": 0,
    }

    with (
        patch.object(_cb_mod, "BOOTSTRAP_ALLOW_ENABLED", True),
        patch("brain.critic_brain.load_edge_profile", return_value=fake_profile),
        patch("brain.critic_brain.edge_profile_health", return_value=untrusted_health),
    ):
        result = critique_signal(signal)

    if result.get("decision") == "ALLOW" and result.get("bootstrap_era_allow") is True:
        return None
    return (
        f"bootstrap_era_allow path broken after threshold contract changes: "
        f"decision={result.get('decision')} bootstrap_era_allow={result.get('bootstrap_era_allow')}"
    )


# ── 6. DC override confirmed off ─────────────────────────────────────────────

def test_dc_override_is_off() -> str | None:
    """DATA_COLLECTION_OVERRIDE_ENABLED must be False (Phase 6B-2 requirement)."""
    from config.trading_config import DATA_COLLECTION_OVERRIDE_ENABLED
    if DATA_COLLECTION_OVERRIDE_ENABLED:
        return "DATA_COLLECTION_OVERRIDE_ENABLED is True — trust deadlock is still active!"
    return None


def test_bootstrap_allow_is_on() -> str | None:
    """BOOTSTRAP_ALLOW_ENABLED must be True (Phase 6B-2 requirement)."""
    from config.trading_config import BOOTSTRAP_ALLOW_ENABLED
    if not BOOTSTRAP_ALLOW_ENABLED:
        return "BOOTSTRAP_ALLOW_ENABLED is False — bootstrap proof path is blocked!"
    return None


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("config exports required constants",       test_config_exports_required_constants),
        ("config values are sane",                  test_config_values_are_sane),
        ("critic has no local MIN_NORMAL_EDGE",      test_critic_has_no_local_min_edge),
        ("critic imports MIN_EDGE from config",      test_critic_imports_min_edge_from_config),
        ("scanner has no local MIN_EDGE constant",   test_scanner_has_no_local_min_edge),
        ("trade record has threshold metadata",      test_trade_record_contains_threshold_metadata),
        ("bootstrap allow end-to-end",               test_bootstrap_allow_still_works),
        ("DC override is off",                       test_dc_override_is_off),
        ("bootstrap allow is on",                    test_bootstrap_allow_is_on),
    ]

    fails: list[str] = []

    print("=" * 70)
    print("TEST: THRESHOLD CONTRACT INTEGRITY")
    print("=" * 70)

    for name, fn in tests:
        err = fn()
        if err is None:
            print(f"[PASS] {name}")
        else:
            print(f"[FAIL] {name}")
            print(f"       {err}")
            fails.append(f"{name}: {err}")

    from config.trading_config import (
        MIN_EDGE, MIN_CONFIDENCE, BOOTSTRAP_MIN_EDGE, BOOTSTRAP_MIN_CONFIDENCE,
        BOOTSTRAP_CONFIDENCE_ADJUSTMENT, BOOTSTRAP_ALLOW_ENABLED,
        DATA_COLLECTION_OVERRIDE_ENABLED,
    )
    print()
    print("THRESHOLD CONTRACT STATE:")
    print(f"  MIN_EDGE                      = {MIN_EDGE:.3f} ({MIN_EDGE*100:.1f}%)")
    print(f"  MIN_CONFIDENCE                = {MIN_CONFIDENCE:.2f} ({MIN_CONFIDENCE*100:.0f}%)")
    print(f"  BOOTSTRAP_MIN_EDGE            = {BOOTSTRAP_MIN_EDGE:.3f} ({BOOTSTRAP_MIN_EDGE*100:.1f}%)")
    print(f"  BOOTSTRAP_MIN_CONFIDENCE      = {BOOTSTRAP_MIN_CONFIDENCE:.2f} ({BOOTSTRAP_MIN_CONFIDENCE*100:.0f}%)")
    print(f"  BOOTSTRAP_CONFIDENCE_ADJ      = {BOOTSTRAP_CONFIDENCE_ADJUSTMENT:+.3f}")
    print(f"  BOOTSTRAP_ALLOW_ENABLED       = {BOOTSTRAP_ALLOW_ENABLED}")
    print(f"  DATA_COLLECTION_OVERRIDE      = {DATA_COLLECTION_OVERRIDE_ENABLED}")
    print()

    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
        print("RESULT: FAILED")
        sys.exit(1)
    else:
        print("RESULT: PROVEN_THRESHOLD_CONTRACT_OK")


if __name__ == "__main__":
    main()
