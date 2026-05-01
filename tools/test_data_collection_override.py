#!/usr/bin/env python3
"""
tools/test_data_collection_override.py
---------------------------------------
Verifies that DATA_COLLECTION_OVERRIDE_ENABLED controls whether
council-blocked trades are forced open ($5 data_collection_override) or
truly stopped at the council gate.

Uses temp log files. Never touches logs/paper_trades.jsonl.

Expected results:
  override_enabled=True  → trade opens with data_collection_override=True
  override_enabled=False → council BLOCK respected, no trade opened (None)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import brain.paper_trader as _pt_mod
from config.trading_config import (
    DATA_COLLECTION_MODE,
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
)
from engine.edge_calculator import MarketData


def _market() -> MarketData:
    # yes_price=0.45 → estimated_prob=0.67 → edge=0.21
    # edge danger guard is patched off so we can reach the council gate cleanly
    return MarketData(
        ticker="KXBTCD-15M-TEST-OVERRIDE",
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


def _blocking_council(signal: dict) -> dict:
    """Mock council that always returns BLOCK (simulates edge-profile untrusted)."""
    return {
        "final_decision": "BLOCK",
        "final_confidence": signal.get("confidence", 0.65),
        "reason": "EDGE_PROFILE_UNTRUSTED — test mock",
        "net_confidence_adjustment": 0.0,
        "edge_profile_health": {
            "edge_profile_trusted": False,
            "reason": "test isolation",
        },
    }


def _run(override_enabled: bool) -> dict | None:
    """
    Run one signal through PaperTrader in isolated test context.

    Patches applied (all reversed after the call):
      - DATA_COLLECTION_OVERRIDE_ENABLED — the flag under test
      - GLOBAL_FORCED_LEARNING_MODE=True — keep $5 sizing active
      - EDGE_DANGER_BLOCK_HIGH_EDGE=False — reach council cleanly
      - MAX_CONCURRENT_OPEN_TRADES=99 — ignore real open-trades state
      - decide_signal — always returns BLOCK
      - risk_manager.check_trade — always approves (isolate risk layer)
      - risk_manager.add_position — no-op

    open_trades is cleared after PaperTrader init so production log state
    does not affect the MAX_CONCURRENT_OPEN_TRADES check.
    """
    with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as fh:
        tmp_log = Path(fh.name)

    try:
        with (
            patch.object(_pt_mod, "DATA_COLLECTION_OVERRIDE_ENABLED", override_enabled),
            patch.object(_pt_mod, "GLOBAL_FORCED_LEARNING_MODE", True),
            patch.object(_pt_mod, "EDGE_DANGER_BLOCK_HIGH_EDGE", False),
            patch.object(_pt_mod, "MAX_CONCURRENT_OPEN_TRADES", 99),
        ):
            from brain.paper_trader import PaperTrader

            trader = PaperTrader(bankroll=500.0)
            trader.open_trades = []  # isolate from real log state
            trader.logger.log_file = str(tmp_log)
            trader.enable()

            with (
                patch("brain.decision_council.decide_signal", side_effect=_blocking_council),
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

    print("=" * 62)
    print("TEST: DATA_COLLECTION_OVERRIDE_ENABLED flag behavior")
    print("=" * 62)

    # ── Test 1: override=True → trade opens tagged data_collection_override ──
    result = _run(override_enabled=True)
    if result is not None and result.get("data_collection_override") is True:
        print("[PASS] override_enabled=True  → $5 data_collection_override trade opened")
    elif result is not None:
        tag = result.get("data_collection_override")
        print(f"[FAIL] override_enabled=True  → trade opened but data_collection_override={tag}")
        fails.append(f"override=True: missing data_collection_override tag (got {tag})")
    else:
        print("[FAIL] override_enabled=True  → no trade opened (expected override trade)")
        fails.append("override=True: trade was blocked when it should have fired")

    # ── Test 2: override=False → council BLOCK is respected ──────────────────
    result2 = _run(override_enabled=False)
    if result2 is None:
        print("[PASS] override_enabled=False → council BLOCK respected, no trade opened")
    else:
        tag2 = result2.get("data_collection_override")
        print(f"[FAIL] override_enabled=False → unexpected trade opened (data_collection_override={tag2})")
        fails.append(f"override=False: trade opened when council block should have held (tag={tag2})")

    # ── Config truth ──────────────────────────────────────────────────────────
    print()
    print("CURRENT CONFIG STATE:")
    print(f"  DATA_COLLECTION_OVERRIDE_ENABLED = {DATA_COLLECTION_OVERRIDE_ENABLED}")
    print(f"  DATA_COLLECTION_MODE             = {DATA_COLLECTION_MODE}")
    print(f"  GLOBAL_FORCED_LEARNING_MODE      = {GLOBAL_FORCED_LEARNING_MODE}")
    if DATA_COLLECTION_OVERRIDE_ENABLED and DATA_COLLECTION_MODE:
        print(
            "  [TRUST DEADLOCK] override is active — "
            "normal_council_approved_modern stays 0"
        )
    elif not DATA_COLLECTION_OVERRIDE_ENABLED:
        print(
            "  [NORMAL PROOF PATH] override is off — "
            "council BLOCKs stay blocked; normal proof can accumulate"
        )

    print()
    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
        print("RESULT: FAILED")
        sys.exit(1)
    else:
        print("RESULT: PROVEN_OVERRIDE_OK")


if __name__ == "__main__":
    main()
