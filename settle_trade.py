#!/usr/bin/env python3
"""
settle_trade.py - Manually settle paper trades when markets resolve
Usage: Edit the trade_id, outcome, and final_price below, then run
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from brain.paper_trader import PaperTrader

# Initialize trader
trader = PaperTrader()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EDIT THESE VALUES ↓
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TRADE_ID = "trade_20260414_005842_986671"  # Get this from logs/paper_trades.jsonl
OUTCOME = "win"  # "win" or "loss"
FINAL_MARKET_PRICE = 0.68  # What the market closed at (for CLV calculation)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"SETTLING TRADE: {TRADE_ID}")
    print(f"{'='*60}\n")
    
    trader.settle_trade(
        trade_id=TRADE_ID,
        outcome=OUTCOME,
        final_market_price=FINAL_MARKET_PRICE
    )
    
    print(f"\n{'='*60}")
    print("UPDATED STATS")
    print(f"{'='*60}")
    trader.print_stats()