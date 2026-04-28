#!/usr/bin/env python3
"""
settle_trade.py
---------------
Manually settle a paper trade when its Kalshi market resolves.

Usage:
  python3 settle_trade.py TICKER OUTCOME
  python3 settle_trade.py --list
  python3 settle_trade.py --help

Arguments:
  TICKER    Kalshi market ticker (e.g. KXBTCMAXMON-BTC-26APR30-8000000)
  OUTCOME   YES  — market resolved YES  (BET_YES wins, BET_NO loses)
            NO   — market resolved NO   (BET_NO wins, BET_YES loses)
            (case-insensitive)

Options:
  --list    Print all currently OPEN paper trades and exit
  --help    Show this message and exit

Examples:
  python3 settle_trade.py KXBTCMAXMON-BTC-26APR30-8000000 YES
  python3 settle_trade.py KXETHD-26APR28-2700 NO
  python3 settle_trade.py --list

How it works:
  1. Reads logs/paper_trades.jsonl to find currently OPEN trades
     (tickers whose OPEN record count exceeds their SETTLED record count).
  2. Calls paper_trader.settle_trade(ticker, outcome) which:
       - Calculates P&L: WIN → (1 - entry_price) × size
                         LOSS → −size
       - Appends a SETTLED record to paper_trades.jsonl
       - Updates risk_state.json:  open_positions−1, total_exposure−size,
                                   daily_pnl += pnl, loss_streak tracking
  3. Prints the settlement result and updated cumulative stats.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from brain.paper_trader import PaperTrader

USAGE = """\
settle_trade.py — Paper trade settlement tool

Usage:
  python3 settle_trade.py TICKER OUTCOME
  python3 settle_trade.py --list
  python3 settle_trade.py --help

Arguments:
  TICKER    Market ticker  (e.g. KXBTCMAXMON-BTC-26APR30-8000000)
  OUTCOME   YES or NO  (case-insensitive)

Options:
  --list    Show all currently OPEN paper trades
  --help    Show this message

Examples:
  python3 settle_trade.py KXBTCMAXMON-BTC-26APR30-8000000 YES
  python3 settle_trade.py KXETHD-26APR28-2700 NO
  python3 settle_trade.py --list
"""


# ─── COMMAND: list ───────────────────────────────────────────────

def cmd_list(trader: PaperTrader) -> None:
    """Print all currently OPEN paper trades reconstructed from the log."""
    trader.load_open_trades_from_log()
    open_trades = trader.open_trades

    print("\n" + "=" * 66)
    print("OPEN PAPER TRADES")
    print("=" * 66)

    if not open_trades:
        print("  (none — no unreconciled OPEN trades in logs/paper_trades.jsonl)")
    else:
        print(f"  {'Timestamp':<20} {'Ticker':<36} {'Action':<8} {'Size':>7}  {'Entry':>6}  Conf")
        print(f"  {'-'*19} {'-'*35} {'-'*7} {'-'*7}  {'-'*6}  ----")
        for t in open_trades:
            ts = (t.get("timestamp") or "")[:19]
            print(
                f"  {ts:<20} {t['ticker']:<36} "
                f"{t.get('action', '?'):<8} "
                f"${t.get('size', 0):6.2f}  "
                f"{t.get('entry_price', 0):6.3f}  "
                f"{t.get('confidence', 0):.2f}"
            )

    print("=" * 66)
    print(f"  Total OPEN: {len(open_trades)}")
    print("=" * 66 + "\n")


# ─── COMMAND: settle ─────────────────────────────────────────────

def cmd_settle(trader: PaperTrader, ticker: str, outcome: str) -> None:
    """
    Reconstruct open trades from the log file, then settle the given ticker.

    Steps:
      1. load_open_trades_from_log() → populates trader.open_trades from file
      2. Verify the ticker is present and OPEN
      3. settle_trade(ticker, outcome) →
           - calculates P&L
           - appends SETTLED record to paper_trades.jsonl
           - calls risk_manager.close_position() and record_result()
             → risk_state.json updated: open_positions, total_exposure,
               daily_pnl, weekly_pnl, loss_streak, cooldown if triggered
    """
    # Step 1: reconstruct open trades from persistent log
    trader.load_open_trades_from_log()

    # Step 2: verify ticker is open
    open_match = [
        t for t in trader.open_trades
        if t.get("ticker") == ticker and t.get("status") == "OPEN"
    ]

    if not open_match:
        print(f"\n[ERROR] No OPEN trade found for ticker: {ticker}")
        all_open = [t["ticker"] for t in trader.open_trades]
        if all_open:
            print("  Currently OPEN tickers:")
            for t_name in sorted(all_open):
                print(f"    {t_name}")
        else:
            print("  No open trades found in logs/paper_trades.jsonl")
        print("\n  Tip: run  python3 settle_trade.py --list  to see open trades.\n")
        sys.exit(1)

    # Step 3: settle — paper_trader handles P&L, logging, and risk state
    settled = trader.settle_trade(ticker=ticker, outcome=outcome)

    if not settled:
        print(f"\n[ERROR] Settlement returned None for {ticker} (unexpected).\n")
        sys.exit(1)

    # Print result
    result_label = settled["result"]
    pnl          = settled["pnl"]

    print("\n" + "=" * 62)
    print("TRADE SETTLED")
    print("=" * 62)
    print(f"  Ticker:      {settled['ticker']}")
    print(f"  Action:      {settled['action']}")
    print(f"  Outcome:     {outcome}")
    print(f"  Result:      {result_label}")
    print(f"  Entry price: {settled['entry_price']:.3f}")
    print(f"  Exit price:  {settled['exit_price']:.3f}")
    print(f"  Size:        ${settled['size']:.2f}")
    print(f"  P&L:         ${pnl:+.2f}")
    print(f"  Settled at:  {(settled.get('settled_at') or '')[:19]}")
    print("=" * 62)

    # Updated cumulative stats
    stats = trader.get_stats()
    rm    = trader.risk_manager.get_status()
    print(f"\n  Cumulative realized P&L:  ${stats['total_pnl']:+.2f}")
    print(f"  Settled trades:           {stats['settled_trades']}")
    if stats["settled_trades"] > 0:
        print(f"  Win rate:                 {stats['win_rate']:.1%}")
    else:
        print(f"  Win rate:                 --")
    print(f"  Open positions (risk):    {rm['open_positions']}")
    print(f"  Total exposure (risk):    ${rm['total_exposure']:.2f}")
    print(f"  Daily P&L (risk):         ${rm['daily_pnl']:+.2f}")
    print(f"  Loss streak:              {rm['loss_streak']}")
    if rm.get("cooldown_active"):
        print(f"  ⚠️  Cooldown active:      {rm['cooldown_remaining_minutes']:.0f} min remaining")
    print()


# ─── ENTRY POINT ─────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        print(USAGE)
        sys.exit(0)

    if "--list" in args:
        trader = PaperTrader()
        cmd_list(trader)
        sys.exit(0)

    if len(args) != 2:
        print(f"[ERROR] Expected 2 arguments: TICKER OUTCOME\n")
        print(USAGE)
        sys.exit(1)

    ticker_arg  = args[0]
    outcome_arg = args[1].upper()

    if outcome_arg not in ("YES", "NO"):
        print(f"[ERROR] OUTCOME must be YES or NO  (got: {args[1]!r})\n")
        sys.exit(1)

    trader = PaperTrader()
    cmd_settle(trader, ticker_arg, outcome_arg)
