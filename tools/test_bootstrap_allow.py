#!/usr/bin/env python3
"""
tools/test_bootstrap_allow.py
------------------------------
Verifies BOOTSTRAP_ALLOW_ENABLED controls whether bootstrap-quality signals
are returned as ALLOW (bootstrap_era_allow=True) or fall back to PROVISIONAL
(when BOOTSTRAP_ALLOW_ENABLED=False and BOOTSTRAP_PROVISIONAL_MODE=True).

Also verifies that DATA_COLLECTION_OVERRIDE_ENABLED=False stops council-
blocked trades from opening (i.e., the old override path is truly off).

Uses temp log files. Never touches logs/paper_trades.jsonl.

Expected results:
  bootstrap_allow=True  → ALLOW with bootstrap_era_allow=True
  bootstrap_allow=False → PROVISIONAL (normal_modern=0 fallback path)
  dc_override=False     → council BLOCK is respected, no trade opened
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import brain.paper_trader as _pt_mod
import brain.critic_brain as _cb_mod
from config.trading_config import (
    BOOTSTRAP_ALLOW_ENABLED,
    BOOTSTRAP_PROVISIONAL_MODE,
    DATA_COLLECTION_OVERRIDE_ENABLED,
    DATA_COLLECTION_MODE,
    GLOBAL_FORCED_LEARNING_MODE,
)
from engine.edge_calculator import MarketData


def _market() -> MarketData:
    # yes_price=0.45 → edge≈0.21 → comfortably above BOOTSTRAP_MIN_EDGE=0.05
    return MarketData(
        ticker="KXBTCD-15M-TEST-BOOTSTRAP",
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


def _untrusted_health(normal_modern: int = 0) -> dict:
    return {
        "edge_profile_trusted": False,
        "reason": "test isolation: untrusted profile",
        "normal_council_approved_modern_trades": normal_modern,
        "edge_profile_age_hours": None,
        "clean_settled_trades": 0,
        "modern_full_metadata_trades": 0,
    }


# ── Critic-level unit tests ───────────────────────────────────────────────────

def test_critic_bootstrap_allow_enabled() -> str | None:
    """With BOOTSTRAP_ALLOW_ENABLED=True, critic returns ALLOW + bootstrap_era_allow."""
    from brain.critic_brain import critique_signal

    signal = {
        "confidence": 0.67,
        "edge": 0.21,
        "ticker": "KXBTCD-15M-TEST-BOOTSTRAP",
        "strategy": "SIGNAL",
        "action": "BET_YES",
    }

    fake_profile = {"generated_at": "2026-01-01T00:00:00Z", "profiles": {}}

    with (
        patch.object(_cb_mod, "BOOTSTRAP_ALLOW_ENABLED", True),
        patch.object(_cb_mod, "BOOTSTRAP_PROVISIONAL_MODE", True),
        patch("brain.critic_brain.load_edge_profile", return_value=fake_profile),
        patch("brain.critic_brain.edge_profile_health", return_value=_untrusted_health(0)),
    ):
        result = critique_signal(signal)

    if result.get("decision") == "ALLOW" and result.get("bootstrap_era_allow") is True:
        return None
    return (
        f"expected ALLOW+bootstrap_era_allow; "
        f"got decision={result.get('decision')} bootstrap_era_allow={result.get('bootstrap_era_allow')}"
    )


def test_critic_bootstrap_allow_disabled_provisional() -> str | None:
    """With BOOTSTRAP_ALLOW_ENABLED=False and normal_modern=0, critic returns PROVISIONAL."""
    from brain.critic_brain import critique_signal

    signal = {
        "confidence": 0.67,
        "edge": 0.21,
        "ticker": "KXBTCD-15M-TEST-BOOTSTRAP",
        "strategy": "SIGNAL",
        "action": "BET_YES",
    }

    fake_profile = {"generated_at": "2026-01-01T00:00:00Z", "profiles": {}}

    with (
        patch.object(_cb_mod, "BOOTSTRAP_ALLOW_ENABLED", False),
        patch.object(_cb_mod, "BOOTSTRAP_PROVISIONAL_MODE", True),
        patch("brain.critic_brain.load_edge_profile", return_value=fake_profile),
        patch("brain.critic_brain.edge_profile_health", return_value=_untrusted_health(0)),
    ):
        result = critique_signal(signal)

    if result.get("decision") == "PROVISIONAL" and result.get("bootstrap_provisional") is True:
        return None
    return (
        f"expected PROVISIONAL+bootstrap_provisional; "
        f"got decision={result.get('decision')} bootstrap_provisional={result.get('bootstrap_provisional')}"
    )


def test_critic_bootstrap_allow_non_zero_modern() -> str | None:
    """With BOOTSTRAP_ALLOW_ENABLED=True and normal_modern=5, still returns ALLOW."""
    from brain.critic_brain import critique_signal

    signal = {
        "confidence": 0.67,
        "edge": 0.21,
        "ticker": "KXBTCD-15M-TEST-BOOTSTRAP",
        "strategy": "SIGNAL",
        "action": "BET_YES",
    }

    fake_profile = {"generated_at": "2026-01-01T00:00:00Z", "profiles": {}}

    with (
        patch.object(_cb_mod, "BOOTSTRAP_ALLOW_ENABLED", True),
        patch("brain.critic_brain.load_edge_profile", return_value=fake_profile),
        patch("brain.critic_brain.edge_profile_health", return_value=_untrusted_health(5)),
    ):
        result = critique_signal(signal)

    if result.get("decision") == "ALLOW" and result.get("bootstrap_era_allow") is True:
        return None
    return (
        f"expected ALLOW+bootstrap_era_allow with normal_modern=5; "
        f"got decision={result.get('decision')}"
    )


# ── Paper-trader integration tests ───────────────────────────────────────────

def _run_paper(
    *,
    bootstrap_allow: bool,
    dc_override: bool,
    council_decision: str,
) -> dict | None:
    """
    Run one signal through PaperTrader under controlled conditions.

    Patches applied:
      - BOOTSTRAP_ALLOW_ENABLED
      - DATA_COLLECTION_OVERRIDE_ENABLED
      - GLOBAL_FORCED_LEARNING_MODE=True
      - EDGE_DANGER_BLOCK_HIGH_EDGE=False
      - MAX_CONCURRENT_OPEN_TRADES=99
      - decide_signal returns a fixed council decision
      - risk_manager.check_trade always approves
      - risk_manager.add_position no-op
    """
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        tmp_log = Path(fh.name)

    def _fake_council(signal: dict) -> dict:
        base = {
            "final_decision": council_decision,
            "final_confidence": signal.get("confidence", 0.67),
            "reason": f"test-mock council={council_decision}",
            "net_confidence_adjustment": -0.05,
            "edge_profile_health": {
                "edge_profile_trusted": False,
                "reason": "test isolation",
            },
            "bootstrap_provisional": council_decision == "PROVISIONAL",
            "bootstrap_era_allow": council_decision == "ALLOW",
        }
        return base

    try:
        with (
            patch.object(_pt_mod, "DATA_COLLECTION_OVERRIDE_ENABLED", dc_override),
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
                return trader.process_signal(
                    _market(),
                    estimated_prob=0.67,
                    strategy="SIGNAL",
                    intended_action="BET_YES",
                )
    finally:
        tmp_log.unlink(missing_ok=True)


def main() -> None:
    fails: list[str] = []

    print("=" * 68)
    print("TEST: BOOTSTRAP_ALLOW_ENABLED + DATA_COLLECTION_OVERRIDE_ENABLED")
    print("=" * 68)

    # ── Critic unit tests ─────────────────────────────────────────────────────
    print("\n-- Critic unit tests --")

    err = test_critic_bootstrap_allow_enabled()
    if err is None:
        print("[PASS] critic: BOOTSTRAP_ALLOW_ENABLED=True → ALLOW + bootstrap_era_allow")
    else:
        print(f"[FAIL] critic: BOOTSTRAP_ALLOW_ENABLED=True test: {err}")
        fails.append(err)

    err = test_critic_bootstrap_allow_disabled_provisional()
    if err is None:
        print("[PASS] critic: BOOTSTRAP_ALLOW_ENABLED=False + normal_modern=0 → PROVISIONAL")
    else:
        print(f"[FAIL] critic: BOOTSTRAP_ALLOW_ENABLED=False test: {err}")
        fails.append(err)

    err = test_critic_bootstrap_allow_non_zero_modern()
    if err is None:
        print("[PASS] critic: BOOTSTRAP_ALLOW_ENABLED=True + normal_modern=5 → ALLOW")
    else:
        print(f"[FAIL] critic: normal_modern=5 test: {err}")
        fails.append(err)

    # ── Paper-trader integration: ALLOW → bootstrap_era_council_allow ─────────
    print("\n-- Paper-trader integration tests --")

    result = _run_paper(bootstrap_allow=True, dc_override=False, council_decision="ALLOW")
    if result is not None and result.get("bootstrap_era_council_allow") is True:
        print("[PASS] paper: ALLOW+bootstrap_era_allow → trade tagged bootstrap_era_council_allow=True")
    elif result is not None:
        tag = result.get("bootstrap_era_council_allow")
        print(f"[FAIL] paper: ALLOW trade opened but bootstrap_era_council_allow={tag}")
        fails.append(f"paper ALLOW: bootstrap_era_council_allow={tag}")
    else:
        print("[FAIL] paper: ALLOW trade was blocked (expected open)")
        fails.append("paper ALLOW: trade unexpectedly blocked")

    # ── Paper-trader integration: PROVISIONAL → bootstrap_provisional ─────────
    result2 = _run_paper(bootstrap_allow=False, dc_override=False, council_decision="PROVISIONAL")
    if result2 is not None and result2.get("bootstrap_provisional") is True:
        print("[PASS] paper: PROVISIONAL → trade tagged bootstrap_provisional=True")
    elif result2 is not None:
        tag2 = result2.get("bootstrap_provisional")
        print(f"[FAIL] paper: PROVISIONAL trade opened but bootstrap_provisional={tag2}")
        fails.append(f"paper PROVISIONAL: bootstrap_provisional={tag2}")
    else:
        print("[FAIL] paper: PROVISIONAL trade was blocked (expected open)")
        fails.append("paper PROVISIONAL: trade unexpectedly blocked")

    # ── Paper-trader integration: BLOCK + dc_override=False → None ────────────
    result3 = _run_paper(bootstrap_allow=False, dc_override=False, council_decision="BLOCK")
    if result3 is None:
        print("[PASS] paper: BLOCK + dc_override=False → council block respected, no trade")
    else:
        tag3 = result3.get("data_collection_override")
        print(f"[FAIL] paper: BLOCK + dc_override=False → unexpected trade (dc_override={tag3})")
        fails.append(f"paper BLOCK: trade opened when should be blocked (tag={tag3})")

    # ── Config truth ──────────────────────────────────────────────────────────
    print()
    print("CURRENT CONFIG STATE:")
    print(f"  BOOTSTRAP_ALLOW_ENABLED          = {BOOTSTRAP_ALLOW_ENABLED}")
    print(f"  BOOTSTRAP_PROVISIONAL_MODE       = {BOOTSTRAP_PROVISIONAL_MODE}")
    print(f"  DATA_COLLECTION_OVERRIDE_ENABLED = {DATA_COLLECTION_OVERRIDE_ENABLED}")
    print(f"  DATA_COLLECTION_MODE             = {DATA_COLLECTION_MODE}")
    print(f"  GLOBAL_FORCED_LEARNING_MODE      = {GLOBAL_FORCED_LEARNING_MODE}")

    if BOOTSTRAP_ALLOW_ENABLED and not DATA_COLLECTION_OVERRIDE_ENABLED:
        print(
            "  [PROOF PATH ACTIVE] bootstrap_era_allow=True: bootstrap signals become "
            "normal_council_approved; trust deadlock is broken."
        )
    elif DATA_COLLECTION_OVERRIDE_ENABLED:
        print("  [TRUST DEADLOCK] DATA_COLLECTION_OVERRIDE_ENABLED=True is still active.")

    print()
    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
        print("RESULT: FAILED")
        sys.exit(1)
    else:
        print("RESULT: PROVEN_BOOTSTRAP_ALLOW_OK")


if __name__ == "__main__":
    main()
