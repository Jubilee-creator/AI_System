#!/usr/bin/env python3
"""
Controlled non-live test for SIDE_BALANCED_RESEARCH shadow selection.

No PaperTrader import, no normal logs, no execution.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import brain.side_coverage_queue as queue  # noqa: E402


def _opportunity(action: str, ticker: str, edge: float = 0.04) -> dict:
    return {
        "action": action,
        "ticker": ticker,
        "confidence": 0.72,
        "edge": edge,
        "price_yes": 0.31,
        "price_no": 0.67,
        "no_bid": 0.66,
        "no_ask": 0.67,
    }


def main() -> None:
    original_enabled = queue.SIDE_BALANCED_RESEARCH_ENABLED
    original_execute = queue.SIDE_BALANCED_RESEARCH_EXECUTE
    original_proof = queue.SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE
    try:
        disabled = queue.select_shadow_candidate(
            [_opportunity("BET_NO", "SYNTH-NO")],
            run_id="test_run",
            scan_id="test_scan",
            open_count=0,
            max_open_trades=3,
        )

        queue.SIDE_BALANCED_RESEARCH_ENABLED = True
        queue.SIDE_BALANCED_RESEARCH_EXECUTE = False
        queue.SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE = False
        selected = queue.select_shadow_candidate(
            [
                _opportunity("PASS", "SYNTH-PASS"),
                _opportunity("BET_YES", "SYNTH-YES"),
                _opportunity("BET_NO", "SYNTH-NO-1"),
                _opportunity("BET_NO", "SYNTH-NO-2"),
            ],
            run_id="test_run",
            scan_id="test_scan",
            open_count=1,
            max_open_trades=3,
        )
        no_available = queue.select_shadow_candidate(
            [_opportunity("PASS", "SYNTH-PASS"), _opportunity("BET_YES", "SYNTH-YES")],
            run_id="test_run",
            scan_id="test_scan",
            open_count=1,
            max_open_trades=3,
        )

        checks = [
            ("disabled_reason", disabled.get("final_reason") == "SIDE_COVERAGE_DISABLED"),
            ("selected_ticker", selected.get("ticker") == "SYNTH-NO-1"),
            ("selected_action", selected.get("scanner_action") == "BET_NO"),
            ("shadow_only", selected.get("shadow_only") is True),
            ("would_execute_false", selected.get("would_execute") is False),
            ("proof_eligible_false", selected.get("proof_eligible") is False),
            ("normal_strategy_false", selected.get("normal_strategy_trade") is False),
            ("no_pass_synthesis", no_available.get("final_reason") == "SIDE_COVERAGE_NO_BET_NO_AVAILABLE"),
        ]
        passed = all(ok for _, ok in checks)

        print("=" * 86)
        print("SIDE COVERAGE SHADOW CONTROLLED TEST")
        print("=" * 86)
        for name, ok in checks:
            print(f"{name}: {'PASS' if ok else 'FAIL'}")
        print()
        print("VERDICT")
        print("-------")
        print("PROVEN_SHADOW_OK" if passed else "FAILED")
        if not passed:
            raise SystemExit(1)
    finally:
        queue.SIDE_BALANCED_RESEARCH_ENABLED = original_enabled
        queue.SIDE_BALANCED_RESEARCH_EXECUTE = original_execute
        queue.SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE = original_proof


if __name__ == "__main__":
    main()

