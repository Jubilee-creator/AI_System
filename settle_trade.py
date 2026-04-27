#!/usr/bin/env python3
"""
settle_trade.py — CLI to manually settle paper trades when markets resolve

Usage:
  python3 settle_trade.py --list
      List all currently open paper trades (rebuilt from paper_trades.jsonl)

  python3 settle_trade.py --ticker TICKER --outcome YES
  python3 settle_trade.py --ticker TICKER --outcome NO
      Settle the named open trade with the given market resolution.
      YES / NO = what the market resolved to.
      The trader infers WIN/LOSS from your original bet direction.
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from brain.paper_trader import PaperTrader


def cmd_list(trader: PaperTrader) -> None:
    """Print all open trades loaded from the trade log."""
    open_trades = trader.open_trades
    if not open_trades:
        print("[settle_trade] No open paper trades found in logs/paper_trades.jsonl")
        return

    print(f"\nOpen paper trades ({len(open_trades)}):")
    print(f"  {'TICKER':<38} {'ACTION':<9} {'SIZE':>7}  {'PRICE':>7}  {'EDGE':>7}  OPENED")
    print(f"  {'-'*38} {'-'*9} {'-'*7}  {'-'*7}  {'-'*7}  {'-'*19}")
    for t in open_trades:
        print(
            f"  {t.get('ticker', '?'):<38} "
            f"{t.get('action', '?'):<9} "
            f"${t.get('size', 0):>6.2f}  "
            f"{t.get('entry_price', 0):>7.4f}  "
            f"{t.get('edge', 0):>+7.4f}  "
            f"{str(t.get('timestamp', '?'))[:19]}"
        )
    print()


def cmd_settle(trader: PaperTrader, ticker: str, outcome: str) -> None:
    """Verify the ticker is open, then settle it."""
    open_tickers = [t.get("ticker") for t in trader.open_trades]

    if ticker not in open_tickers:
        print(f"[settle_trade] ERROR: no open trade found for ticker '{ticker}'")
        if open_tickers:
            print(f"  Open tickers: {', '.join(open_tickers)}")
        else:
            print("  No open trades at all. Run --list to check.")
        sys.exit(1)

    print(f"\nSettling: {ticker}  →  outcome={outcome}")

    result = trader.settle_trade(ticker, outcome)

    if result is None:
        print(f"[settle_trade] ERROR: settle_trade() returned None — trade was not settled.")
        sys.exit(1)

    print(f"  Status : {result['status']}")
    print(f"  Result : {result['result']}")
    print(f"  P&L    : ${result['pnl']:+.2f}")
    print(f"\n  Remaining open trades : {len(trader.open_trades)}")
    print(f"  Total P&L             : ${trader.total_pnl:+.2f}")
    print(f"  Settled               : {trader.settled_trades}  "
          f"(wins={trader.wins}  losses={trader.losses})")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="settle_trade.py",
        description="Manually settle paper trades when markets resolve"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--list", action="store_true",
        help="List all currently open paper trades"
    )
    group.add_argument(
        "--ticker", metavar="TICKER",
        help="Exact ticker of the open trade to settle"
    )
    parser.add_argument(
        "--outcome", choices=["YES", "NO"],
        help="Market resolution: YES or NO  (required with --ticker)"
    )
    args = parser.parse_args()

    if args.ticker and not args.outcome:
        parser.error("--outcome YES|NO is required when using --ticker")

    # PaperTrader.__init__ calls _load_state_from_trade_log() automatically,
    # so open_trades is populated before we do anything else.
    trader = PaperTrader()

    if args.list:
        cmd_list(trader)
    else:
        cmd_settle(trader, args.ticker, args.outcome)


if __name__ == "__main__":
    main()
