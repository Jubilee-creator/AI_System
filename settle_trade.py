#!/usr/bin/env python3
"""
settle_trade.py - Manually settle paper trades when markets resolve
Usage: Set TICKER and OUTCOME below, then run:
  python3 settle_trade.py

TICKER  — the market ticker exactly as logged in paper_trades.jsonl
OUTCOME — the market resolution: "YES" or "NO"
          (the trader figures out win/loss from your bet direction)
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from brain.paper_trader import PaperTrader

# Initialize trader (reloads open trades from paper_trades.jsonl)
trader = PaperTrader()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EDIT THESE VALUES ↓
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TICKER  = "KXBTCUSD-23DEC1-T12345"  # Copy exact ticker from paper_trades.jsonl
OUTCOME = "YES"                      # "YES" or "NO" — what the market resolved to

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"SETTLING TRADE: {TICKER}  →  outcome={OUTCOME}")
    print(f"{'='*60}\n")

    result = trader.settle_trade(TICKER, OUTCOME)

    if result:
        print(f"\n{'='*60}")
        print("UPDATED STATS")
        print(f"{'='*60}")
        trader.print_stats()
    else:
        print(f"[ERROR] No open trade found for ticker: {TICKER}")
        print("Check paper_trades.jsonl for the exact ticker string.")
