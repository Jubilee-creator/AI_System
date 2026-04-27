#!/usr/bin/env python3
"""
auto_settle_trades.py — Automatically settle resolved paper trades from Kalshi

Usage:
  python3 auto_settle_trades.py --dry-run
      Show which open trades would be settled, without writing anything.

  python3 auto_settle_trades.py --execute
      Fetch each open trade's market from Kalshi; settle those with a known
      YES/NO result.  Only writes to the log when settlement actually occurs.

Rules enforced:
  - Never guesses outcomes.
  - Skips markets that are not yet resolved (no valid result field).
  - Skips "void" markets (cancelled) without settling them.
  - Does not touch strategy, edge, confidence, or risk thresholds.
"""

import sys
import argparse
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from brain.paper_trader import PaperTrader
from brokers.kalshi_client import kalshi_get


# ── Kalshi resolution constants ────────────────────────────────────────────────
# These are the status values Kalshi uses once a market has been determined.
# "determined" = outcome known, settlement pending
# "settled"    = payouts complete
_RESOLVED_STATUSES: frozenset[str] = frozenset({"determined", "settled", "finalized"})

# Only these result values map to a paper-trade outcome.
# "void" (market cancelled) is intentionally excluded — we never settle voids.
_TRADEABLE_RESULTS: frozenset[str] = frozenset({"yes", "no"})


# ── Kalshi API helper ──────────────────────────────────────────────────────────

def fetch_market_resolution(ticker: str) -> Tuple[bool, Optional[str]]:
    """
    Query Kalshi for a single market and extract its settlement outcome.

    Returns:
        (is_resolved, outcome)
        is_resolved: True only when the market has a definitive YES or NO result.
        outcome:     "YES" or "NO" if resolved; None otherwise.

    Will not raise — returns (False, None) on any API or parsing error.
    """
    data = kalshi_get(f"/markets/{ticker}", silent=False)
    if not data:
        return False, None

    # Kalshi /markets/{ticker} wraps the object under "market"
    market = data.get("market") or data

    status: str = (market.get("status") or "").strip().lower()
    result: str = (market.get("result") or "").strip().lower()

    # Must have both a resolved status AND a tradeable result (yes/no)
    if status in _RESOLVED_STATUSES and result in _TRADEABLE_RESULTS:
        return True, result.upper()  # "YES" or "NO"

    return False, None


# ── Core logic ─────────────────────────────────────────────────────────────────

def _compute_expected_pnl(trade: dict, outcome: str) -> float:
    """Return expected P&L for a trade given an outcome (dry-run use only)."""
    action = trade.get("action", "")
    entry  = float(trade.get("entry_price", 0.5))
    size   = float(trade.get("size", 0.0))

    won = (
        (action == "BET_YES" and outcome == "YES") or
        (action == "BET_NO"  and outcome == "NO")  or
        (action == "ARB")
    )
    return round((1.0 - entry) * size if won else -size, 2)


def run(dry_run: bool) -> None:
    mode_label = "DRY-RUN" if dry_run else "EXECUTE"
    print(f"\n[AUTO-SETTLE] Mode: {mode_label}")
    print("=" * 60)

    trader = PaperTrader()

    # Snapshot the open-trade list before we begin — it shrinks during execute
    open_trades = list(trader.open_trades)
    total_open  = len(open_trades)

    if not open_trades:
        print("[AUTO-SETTLE] No open paper trades found in logs/paper_trades.jsonl")
        return

    print(f"\n[AUTO-SETTLE] {total_open} open trade(s) — querying Kalshi...\n")

    checked   = 0
    skipped   = 0
    settled   = 0
    errors    = 0
    pnl_delta = 0.0

    for trade in open_trades:
        ticker = trade.get("ticker", "?")
        action = trade.get("action", "?")
        size   = float(trade.get("size", 0.0))
        checked += 1

        print(f"  [{checked}/{total_open}] {ticker}")
        print(f"    Action : {action}  Entry : {trade.get('entry_price', '?')}  Size : ${size:.2f}")

        is_resolved, outcome = fetch_market_resolution(ticker)

        if not is_resolved:
            print(f"    Status : UNRESOLVED — skipping")
            skipped += 1
            print()
            continue

        print(f"    Status : RESOLVED  →  outcome={outcome}")

        if dry_run:
            expected_pnl = _compute_expected_pnl(trade, outcome)
            result_label = "WIN" if expected_pnl >= 0 else "LOSS"
            print(f"    [DRY-RUN] Would settle as {result_label}  "
                  f"Expected P&L: ${expected_pnl:+.2f}")
            settled   += 1
            pnl_delta += expected_pnl
        else:
            result = trader.settle_trade(ticker, outcome)
            if result is None:
                print(f"    [ERROR] settle_trade() returned None — skipping")
                errors += 1
            else:
                trade_pnl  = float(result.get("pnl", 0.0))
                pnl_delta += trade_pnl
                settled   += 1
                print(f"    Settled : {result['result']}  P&L : ${trade_pnl:+.2f}")

        print()

    # ── Reconcile risk state with actual open trades ───────────────────────────
    if not dry_run:
        trader.risk_manager.rebuild_from_trade_log(trader.open_trades)

    # ── Summary ────────────────────────────────────────────────────────────────
    print("=" * 60)
    print(f"[AUTO-SETTLE] {mode_label} SUMMARY")
    print(f"  Trades checked     : {checked}")
    print(f"  Unresolved skipped : {skipped}")
    print(f"  Settled            : {settled}")
    if errors:
        print(f"  Errors             : {errors}")
    print(f"  P&L change         : ${pnl_delta:+.2f}")

    if not dry_run:
        remaining_exposure = sum(
            float(t.get("size", 0.0)) for t in trader.open_trades
        )
        stats = trader.get_stats()
        print(f"  Remaining open     : {len(trader.open_trades)}")
        print(f"  Open exposure      : ${remaining_exposure:.2f}")
        print(f"  Total P&L          : ${trader.total_pnl:+.2f}")
        print(f"  Total settled      : {stats['settled_trades']}"
              f"  (wins={stats['wins']}  losses={stats['losses']})")

    print()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="auto_settle_trades.py",
        description="Automatically settle resolved Kalshi paper trades"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be settled without writing to the trade log"
    )
    group.add_argument(
        "--execute", action="store_true",
        help="Fetch each open trade from Kalshi and settle resolved ones"
    )
    args = parser.parse_args()

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
