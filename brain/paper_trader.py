"""
brain/paper_trader.py
---------------------
Paper trading system with risk management integration

UPDATED: Phase 4, Step 4
CHANGES: Integrated RiskManager for safety checks
PRESERVED: All existing paper trading logic
"""

import os
import json
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from pathlib import Path

# Add parent directory to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import risk manager (PHASE 4 ADDITION)
from brain.risk_manager import RiskManager

# Import dependencies
from logs.trade_logger import TradeLogger
from engine.edge_calculator import MarketData


# ═══════════════════════════════════════════════════════════════
# PAPER TRADER
# ═══════════════════════════════════════════════════════════════

class PaperTrader:
    """
    Paper trading system with full risk management.
    
    PHASE 4 UPDATE: Now integrates RiskManager for safety checks.
    All trades must pass risk checks before execution.
    
    Usage:
        trader = PaperTrader(bankroll=500.0)
        trader.enable()
        
        # Process signal
        result = trader.process_signal(
            market_data=market,
            estimated_prob=0.68,
            strategy="SIGNAL"
        )
    """
    
    def __init__(
        self,
        bankroll: float = 500.0,
        min_edge: float = 0.03,
        min_confidence: float = 0.65,
        max_bet_size: float = 50.0,
        kelly_fraction: float = 0.25
    ):
        """
        Initialize paper trader.
        
        Args:
            bankroll: Starting capital
            min_edge: Minimum edge required (after fees)
            min_confidence: Minimum probability required
            max_bet_size: Maximum bet size in dollars
            kelly_fraction: Kelly multiplier (0.25 = quarter Kelly)
        """
        self.bankroll = bankroll
        
        # PHASE 4: Initialize risk manager
        self.risk_manager = RiskManager(bankroll=bankroll)
        
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.max_bet_size = max_bet_size
        self.kelly_fraction = kelly_fraction
        
        # State
        self.enabled = False
        self.total_trades = 0
        self.settled_trades = 0
        self.total_pnl = 0.0
        self.wins = 0
        self.losses = 0
        
        # Trade tracking
        self.open_trades = []
        self.trade_history = []
        
        # Logger
        self.logger = TradeLogger()

        # Rebuild counters and open trades from persisted log
        self._load_state_from_trade_log()

        print(f"[PAPER] Initialized PaperTrader")
        print(f"  Bankroll: ${self.bankroll:.2f}")
        print(f"  Min edge: {self.min_edge*100:.1f}%")
        print(f"  Min confidence: {self.min_confidence*100:.1f}%")
        print(f"  Max bet: ${self.max_bet_size:.2f}")
        print(f"  Kelly fraction: {self.kelly_fraction}")
    
    
    def _load_state_from_trade_log(self) -> None:
        """
        Rebuild in-memory state from logs/paper_trades.jsonl on startup.

        The log may contain two entries per trade (OPEN then SETTLED).
        Deduplication key: (ticker, timestamp) — last line seen wins, so
        a SETTLED entry always supersedes its corresponding OPEN entry.

        Read-only: never writes to the log during this call.
        Safe to call on missing or malformed log files.
        """
        log_path = self.logger.log_file

        if not os.path.exists(log_path):
            print("[PAPER] No trade log found — starting clean")
            return

        trades_by_key: dict = {}
        lines_skipped = 0

        try:
            with open(log_path, "r") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        trade = json.loads(raw)
                        key = (trade.get("ticker", ""), trade.get("timestamp", ""))
                        trades_by_key[key] = trade
                    except Exception:
                        lines_skipped += 1
                        continue
        except Exception as exc:
            print(f"[PAPER] Warning: could not read trade log: {exc}")
            return

        if lines_skipped:
            print(f"[PAPER] Warning: skipped {lines_skipped} malformed line(s) in trade log")

        for trade in trades_by_key.values():
            status = trade.get("status", "")
            result = trade.get("result")
            pnl    = float(trade.get("pnl") or 0.0)

            if status == "OPEN":
                self.open_trades.append(trade)
                self.total_trades += 1
            elif status == "SETTLED":
                self.trade_history.append(trade)
                self.total_trades  += 1
                self.settled_trades += 1
                self.total_pnl     += pnl
                if result == "WIN":
                    self.wins += 1
                elif result == "LOSS":
                    self.losses += 1

        if self.total_trades > 0:
            print(
                f"[PAPER] State rebuilt: {self.total_trades} total | "
                f"{self.settled_trades} settled | "
                f"{len(self.open_trades)} open | "
                f"P&L=${self.total_pnl:.2f}"
            )


    def enable(self) -> None:
        """Enable paper trading - will log all signals."""
        self.enabled = True
        print(f"[PAPER] ENABLED - Will log all signals to paper_trades.jsonl")
    
    
    def disable(self) -> None:
        """Disable paper trading."""
        self.enabled = False
        print(f"[PAPER] DISABLED")
    
    
    def _calculate_kelly_size(
        self,
        prob: float,
        price: float
    ) -> float:
        """
        Calculate Kelly bet size.
        
        Args:
            prob: Win probability
            price: Entry price
        
        Returns:
            Bet size in dollars
        """
        if prob < self.min_confidence:
            return 0.0
        
        # Payout multiplier (profit per dollar risked)
        b = (1.0 - price) / price
        
        # Kelly formula: f* = (bp - q) / b
        q = 1.0 - prob
        f_star = (b * prob - q) / b
        f_star = max(0.0, f_star)
        
        # Apply Kelly fraction (conservative)
        f = self.kelly_fraction * f_star
        
        # Convert to dollar amount
        bet_size = f * self.bankroll
        
        # Apply caps
        bet_size = min(bet_size, self.max_bet_size)
        bet_size = min(bet_size, self.bankroll * 0.10)  # Never >10% of bankroll
        bet_size = max(bet_size, 0.0)
        
        return round(bet_size, 2)
    
    
    def process_signal(
        self,
        market_data: MarketData,
        estimated_prob: float,
        strategy: str = "SIGNAL"
    ) -> Optional[Dict[str, Any]]:
        """
        Process trading signal through risk manager, then execute if approved.
        
        PHASE 4 UPDATE: Now checks risk manager before execution.
        DEBUG: Added debug prints to trace execution flow.
        
        Args:
            market_data: Market data object
            estimated_prob: Model probability of YES outcome
            strategy: Strategy label (SIGNAL, ARB, TREND)
        
        Returns:
            Trade dict if executed, None if skipped/blocked
        """
        
        # DEBUG 1: Check if enabled
        if not self.enabled:
            print(f"[PAPER_DEBUG] blocked: trader disabled")
            return None
        
        # DEBUG 2: Check minimum confidence
        if estimated_prob < self.min_confidence and strategy != "ARB":
            print(f"[PAPER_DEBUG] blocked: below min confidence | estimated_prob={estimated_prob:.3f} min_confidence={self.min_confidence:.3f} strategy={strategy}")
            return None
        
        # Determine action and price
        if estimated_prob >= 0.5:
            action = "BET_YES"
            price = market_data.yes_price
        else:
            action = "BET_NO"
            price = market_data.no_price
            estimated_prob = 1.0 - estimated_prob  # Flip for NO side
        
        # ARB override
        if strategy == "ARB":
            action = "ARB"
            # For ARB, estimated_prob is near 1.0
            estimated_prob = 0.99
        
        # Calculate edge (after fees)
        edge = estimated_prob - price - 0.01  # 1¢ fee estimate
        
        # DEBUG 3: Show calculated values
        print(f"[PAPER_DEBUG] action={action} price={price:.3f} estimated_prob={estimated_prob:.3f} edge={edge:.4f} min_edge={self.min_edge:.4f}")
        
        # DEBUG 4: Check minimum edge
        if edge < self.min_edge and strategy != "ARB":
            print(f"[PAPER_DEBUG] blocked: below min edge | edge={edge:.4f} min_edge={self.min_edge:.4f} strategy={strategy}")
            return None
        
        # Calculate bet size
        bet_size = self._calculate_kelly_size(estimated_prob, price)
        
        # DEBUG 5: Show bet size calculation
        print(f"[PAPER_DEBUG] bet_size={bet_size:.2f} bankroll={self.bankroll:.2f} max_bet_size={self.max_bet_size:.2f}")
        
        # DEBUG 6: Check bet size is positive
        if bet_size <= 0:
            print(f"[PAPER_DEBUG] blocked: bet size <= 0")
            return None
        
        # DEBUG 7: About to call risk manager
        print(f"[PAPER_DEBUG] sending to risk manager")
        
        # ═══════════════════════════════════════════════════════
        # PHASE 4: RISK MANAGER CHECK
        # ═══════════════════════════════════════════════════════
        
        approved, block_reason = self.risk_manager.check_trade(
            ticker=market_data.ticker,
            action=action,
            size=bet_size,
            confidence=estimated_prob,
            edge=edge,
            strategy=strategy
        )
        
        if not approved:
            print(f"[PAPER] ❌ Trade blocked by risk manager: {block_reason}")
            print(f"  Ticker: {market_data.ticker}")
            print(f"  Action: {action}")
            print(f"  Size: ${bet_size:.2f}")
            return None
        
        # ═══════════════════════════════════════════════════════
        # TRADE APPROVED - EXECUTE
        # ═══════════════════════════════════════════════════════
        
        # Create trade record
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticker": market_data.ticker,
            "action": action,
            "strategy": strategy,
            "entry_price": price,
            "size": bet_size,
            "confidence": estimated_prob,
            "edge": edge,
            "status": "OPEN",
            "result": None,
            "pnl": 0.0,
            "exit_price": None,
            "settled_at": None
        }
        
        # Log to file
        self.logger.log_trade(trade)
        
        # Track internally
        self.open_trades.append(trade)
        self.total_trades += 1
        
        # PHASE 4: Track position with risk manager
        self.risk_manager.add_position(bet_size)
        
        # DEBUG 9: Trade created successfully
        print(f"[PAPER_DEBUG] trade created successfully")
        
        print(f"[PAPER] ✅ Trade opened: {action} {market_data.ticker} ${bet_size:.2f} @ {price:.2f}")
        
        return trade
    
    
    def settle_trade(
        self,
        ticker: str,
        outcome: str  # "YES" or "NO"
    ) -> Optional[Dict[str, Any]]:
        """
        Settle a trade when outcome is known.
        
        PHASE 4 UPDATE: Now records result with risk manager.
        
        Args:
            ticker: Market ticker
            outcome: Final outcome ("YES" or "NO")
        
        Returns:
            Settled trade dict or None
        """
        
        # Find open trade
        trade = None
        for t in self.open_trades:
            if t["ticker"] == ticker and t["status"] == "OPEN":
                trade = t
                break
        
        if not trade:
            print(f"[PAPER] No open trade found for {ticker}")
            return None
        
        # Determine win/loss
        won = False
        if trade["action"] == "BET_YES" and outcome == "YES":
            won = True
        elif trade["action"] == "BET_NO" and outcome == "NO":
            won = True
        elif trade["action"] == "ARB":
            won = True  # ARB always wins (bought both sides)
        
        # Calculate P&L
        if won:
            # Profit = (1 - entry_price) * size
            pnl = (1.0 - trade["entry_price"]) * trade["size"]
            result = "WIN"
            self.wins += 1
        else:
            # Loss = -size
            pnl = -trade["size"]
            result = "LOSS"
            self.losses += 1
        
        # Update trade record
        trade["status"] = "SETTLED"
        trade["result"] = result
        trade["pnl"] = round(pnl, 2)
        trade["exit_price"] = 1.0 if won else 0.0
        trade["settled_at"] = datetime.now(timezone.utc).isoformat()
        
        # Update totals
        self.total_pnl += pnl
        self.settled_trades += 1
        
        # ═══════════════════════════════════════════════════════
        # PHASE 4: UPDATE RISK MANAGER
        # ═══════════════════════════════════════════════════════
        
        self.risk_manager.close_position(trade["size"])
        self.risk_manager.record_result(
            pnl=pnl,
            result=result,
            size=trade["size"],
            ticker=trade["ticker"]
        )
        
        # Move to history
        self.open_trades.remove(trade)
        self.trade_history.append(trade)
        
        # Update log
        self.logger.log_trade(trade)
        
        print(f"[PAPER] 💰 Trade settled: {ticker} → {result} (${pnl:+.2f})")
        
        return trade
    
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get paper trading statistics.
        
        Returns:
            Dict with all stats
        """
        
        # Win rate
        win_rate = self.wins / self.settled_trades if self.settled_trades > 0 else 0.0
        
        # Average edge (from settled trades)
        edges = [t["edge"] for t in self.trade_history if "edge" in t]
        avg_edge = sum(edges) / len(edges) if edges else 0.0
        
        # Average EV
        avg_ev = self.total_pnl / self.settled_trades if self.settled_trades > 0 else 0.0
        
        # CLV (closing line value - simplified)
        avg_clv = avg_edge  # Simplified - real CLV needs closing prices
        
        # Sharpe ratio (simplified)
        if self.settled_trades > 1:
            pnls = [t["pnl"] for t in self.trade_history]
            import statistics
            mean_pnl = statistics.mean(pnls)
            std_pnl = statistics.stdev(pnls)
            sharpe = (mean_pnl / std_pnl) if std_pnl > 0 else 0.0
        else:
            sharpe = 0.0
        
        # Verdicts
        is_profitable = self.total_pnl > 0
        has_edge = avg_edge > 0
        beats_closing = avg_clv > 0
        
        return {
            "total_trades": self.total_trades,
            "settled_trades": self.settled_trades,
            "open_trades": len(self.open_trades),
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(win_rate, 3),
            "total_pnl": round(self.total_pnl, 2),
            "avg_edge": round(avg_edge, 4),
            "avg_ev": round(avg_ev, 3),
            "avg_clv": round(avg_clv, 3),
            "sharpe": round(sharpe, 2),
            "is_profitable": is_profitable,
            "has_edge": has_edge,
            "beats_closing": beats_closing,
            "bankroll": self.bankroll,
            "current_balance": round(self.bankroll + self.total_pnl, 2)
        }
    
    
    def get_risk_status(self) -> Dict[str, Any]:
        """
        Get risk manager status.
        
        PHASE 4 ADDITION: Expose risk manager state.
        
        Returns:
            Risk manager status dict
        """
        return self.risk_manager.get_status()
    
    
    def print_stats(self) -> None:
        """Print statistics summary."""
        stats = self.get_stats()
        
        print("\n" + "="*60)
        print("PAPER TRADING STATS")
        print("="*60)
        print(f"Trades: {stats['settled_trades']} settled, {stats['open_trades']} open")
        print(f"Win rate: {stats['win_rate']:.1%}")
        print(f"P&L: ${stats['total_pnl']:+.2f}")
        print(f"Avg edge: {stats['avg_edge']:.3f}")
        print(f"Avg EV: ${stats['avg_ev']:+.3f}")
        print(f"Sharpe: {stats['sharpe']:.2f}")
        print(f"Balance: ${stats['current_balance']:.2f}")
        print("="*60 + "\n")


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "="*60)
    print("PAPER TRADER TEST (with Risk Manager)")
    print("="*60)
    
    # Initialize
    trader = PaperTrader(bankroll=500.0)
    trader.enable()
    
    # Create test market
    market = MarketData(
        ticker="TEST-001",
        yes_price=0.45,
        no_price=0.55,
        yes_bid=0.44,
        yes_ask=0.46,
        no_bid=0.54,
        no_ask=0.56,
        volume_24h=1000,
        spread=0.02,
        liquidity=500,
        fee_rate=0.01,
        time_to_expiry=24.0,
        venue="kalshi"
    )
    
    print("\n[TEST 1] Process valid signal (should pass risk check)")
    trade1 = trader.process_signal(market, estimated_prob=0.68, strategy="SIGNAL")
    
    print("\n[TEST 2] Settle trade as WIN")
    if trade1:
        trader.settle_trade("TEST-001", outcome="YES")
    
    print("\n[TEST 3] Trigger loss streak")
    for i in range(3):
        market_loss = MarketData(
            ticker=f"TEST-LOSS-{i}",
            yes_price=0.45, no_price=0.55,
            yes_bid=0.44, yes_ask=0.46,
            no_bid=0.54, no_ask=0.56,
            volume_24h=1000, spread=0.02,
            liquidity=500, fee_rate=0.01,
            time_to_expiry=24.0, venue="kalshi"
        )
        trade_loss = trader.process_signal(market_loss, estimated_prob=0.65, strategy="SIGNAL")
        if trade_loss:
            trader.settle_trade(f"TEST-LOSS-{i}", outcome="NO")  # Force loss
    
    print("\n[TEST 4] Try to trade during cooldown (should be blocked)")
    market3 = MarketData(
        ticker="TEST-003",
        yes_price=0.45, no_price=0.55,
        yes_bid=0.44, yes_ask=0.46,
        no_bid=0.54, no_ask=0.56,
        volume_24h=1000, spread=0.02,
        liquidity=500, fee_rate=0.01,
        time_to_expiry=24.0, venue="kalshi"
    )
    trade3 = trader.process_signal(market3, estimated_prob=0.70, strategy="SIGNAL")
    
    print("\n[TEST 5] Stats")
    trader.print_stats()
    
    print("\n[TEST 6] Risk manager status")
    print(trader.risk_manager.get_limits_summary())
    
    print("\n" + "="*60)
    print("✅ All tests complete")
    print("="*60 + "\n")
