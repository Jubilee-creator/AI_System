#!/usr/bin/env python3
"""
tools/test_bet_no_handoff.py
----------------------------
Controlled paper-only BET_NO handoff test.

This uses PaperTrader.process_signal() but redirects trade logging to a temp
file and replaces the risk manager with an in-memory allow-only stub. It must
not append synthetic rows to logs/paper_trades.jsonl or mutate risk state.
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain.paper_trader import PaperTrader  # noqa: E402
from engine.edge_calculator import MarketData  # noqa: E402
from logs.trade_logger import TradeLogger  # noqa: E402


TEST_NAME = "BET_NO_HANDOFF_TEST"


class InMemoryRiskManager:
    """Minimal allow-only risk manager for isolated handoff testing."""

    def __init__(self) -> None:
        self.total_exposure = 0.0
        self.open_positions = 0
        self.checks: List[Dict[str, Any]] = []

    def check_trade(
        self,
        ticker: str,
        action: str,
        size: float,
        confidence: float,
        edge: float,
        strategy: str,
        learning_trade: bool = False,
    ) -> tuple:
        self.checks.append({
            "ticker": ticker,
            "action": action,
            "size": size,
            "confidence": confidence,
            "edge": edge,
            "strategy": strategy,
            "learning_trade": learning_trade,
        })
        return True, ""

    def add_position(self, size: float) -> None:
        self.open_positions += 1
        self.total_exposure += size


class SyntheticTradeLogger(TradeLogger):
    """TradeLogger that tags every temp row as non-proof synthetic output."""

    def log_trade(self, trade) -> None:
        if isinstance(trade, dict):
            trade["synthetic_test"] = True
            trade["test_name"] = TEST_NAME
            trade["proof_eligible"] = False
        super().log_trade(trade)


def build_market(ticker: str) -> MarketData:
    return MarketData(
        ticker=ticker,
        yes_price=0.66,
        no_price=0.67,
        yes_bid=0.65,
        yes_ask=0.66,
        no_bid=0.66,
        no_ask=0.67,
        volume_24h=10000,
        spread=0.01,
        liquidity=1000,
        fee_rate=0.01,
        time_to_expiry=1.0,
        venue="synthetic_test",
    )


def isolated_trader(temp_trade_log: Path) -> PaperTrader:
    trader = PaperTrader(
        bankroll=500.0,
        min_edge=0.03,
        min_confidence=0.65,
        max_bet_size=50.0,
        kelly_fraction=0.25,
    )
    trader.logger = SyntheticTradeLogger(str(temp_trade_log))
    trader.risk_manager = InMemoryRiskManager()
    trader.open_trades = []
    trader.trade_history = []
    trader.total_trades = 0
    trader.settled_trades = 0
    trader.total_pnl = 0.0
    trader.wins = 0
    trader.losses = 0
    trader.enable()
    return trader


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def run_case(
    trader: PaperTrader,
    name: str,
    ticker: str,
    intended_action: str,
    estimated_prob: float,
) -> Dict[str, Any]:
    before_count = len(read_jsonl(Path(trader.logger.log_file)))
    trade = trader.process_signal(
        market_data=build_market(ticker),
        estimated_prob=estimated_prob,
        strategy="SIGNAL_SYNTHETIC",
        intended_action=intended_action,
    )
    after_rows = read_jsonl(Path(trader.logger.log_file))
    logged_trade = after_rows[-1] if len(after_rows) > before_count else None
    return {
        "case": name,
        "input_intended_action": intended_action,
        "input_estimated_prob": estimated_prob,
        "opened": bool(trade),
        "returned_action": trade.get("action") if trade else None,
        "returned_scanner_action": trade.get("scanner_action") if trade else None,
        "returned_intended_action": trade.get("intended_action") if trade else None,
        "returned_executed_action": trade.get("executed_action") if trade else None,
        "returned_handoff_action_mismatch": trade.get("handoff_action_mismatch") if trade else None,
        "logged_action": logged_trade.get("action") if logged_trade else None,
        "logged_scanner_action": logged_trade.get("scanner_action") if logged_trade else None,
        "logged_intended_action": logged_trade.get("intended_action") if logged_trade else None,
        "logged_executed_action": logged_trade.get("executed_action") if logged_trade else None,
        "logged_handoff_action_mismatch": logged_trade.get("handoff_action_mismatch") if logged_trade else None,
        "logged_synthetic_test": logged_trade.get("synthetic_test") if logged_trade else None,
        "logged_test_name": logged_trade.get("test_name") if logged_trade else None,
        "logged_proof_eligible": logged_trade.get("proof_eligible") if logged_trade else None,
    }


def main() -> int:
    normal_log = ROOT / "logs" / "paper_trades.jsonl"
    normal_before_size = normal_log.stat().st_size if normal_log.exists() else 0

    with tempfile.TemporaryDirectory(prefix="bet_no_handoff_") as tmp:
        temp_trade_log = Path(tmp) / "paper_trades.synthetic.jsonl"
        trader = isolated_trader(temp_trade_log)

        results = [
            run_case(
                trader=trader,
                name="scanner_bet_no_side_confidence",
                ticker="SYNTH-BETNO-SIDECONF",
                intended_action="BET_NO",
                estimated_prob=0.72,
            ),
            run_case(
                trader=trader,
                name="scanner_bet_yes_control",
                ticker="SYNTH-BETYES-CONTROL",
                intended_action="BET_YES",
                estimated_prob=0.72,
            ),
        ]

        normal_after_size = normal_log.stat().st_size if normal_log.exists() else 0
        normal_log_untouched = normal_before_size == normal_after_size
        temp_rows = read_jsonl(temp_trade_log)

        print("=" * 86)
        print("BET_NO HANDOFF CONTROLLED TEST")
        print("=" * 86)
        print(f"normal paper log: {normal_log}")
        print(f"normal log untouched: {normal_log_untouched}")
        print(f"synthetic temp rows: {len(temp_rows)}")
        print()
        for result in results:
            print(result["case"])
            print("-" * len(result["case"]))
            for key in sorted(result):
                if key != "case":
                    print(f"{key}: {result[key]}")
            print()

        bet_no_case = results[0]
        if (
            bet_no_case["opened"]
            and bet_no_case["returned_scanner_action"] == "BET_NO"
            and bet_no_case["returned_executed_action"] == "BET_YES"
            and bet_no_case["returned_handoff_action_mismatch"] is True
        ):
            verdict = "PROVEN_BROKEN"
            reason = (
                "PaperTrader accepted intended_action=BET_NO but re-derived "
                "execution side from estimated_prob=0.72 and opened BET_YES."
            )
        elif (
            bet_no_case["opened"]
            and bet_no_case["returned_executed_action"] == "BET_NO"
            and bet_no_case["returned_handoff_action_mismatch"] is False
        ):
            verdict = "PROVEN_OK"
            reason = "PaperTrader opened BET_NO while preserving handoff action."
        else:
            verdict = "STILL_UNPROVEN"
            reason = "The synthetic BET_NO case did not open, so conversion could not be measured."

        print("VERDICT")
        print("-------")
        print(verdict)
        print(reason)

        if not normal_log_untouched:
            print("[ERROR] normal paper trade log changed during synthetic test")
            return 2
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
