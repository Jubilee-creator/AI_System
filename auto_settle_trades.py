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
import json
import os
import subprocess
from datetime import datetime, timezone
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
MAX_TRADE_DURATION_SECONDS = 7200  # 2 hours


# ── Optional notifications ────────────────────────────────────────────────────

def _load_monitor_env() -> None:
    """
    Load local monitor notification credentials when auto_settle_trades.py is
    run directly or before tools/auto_monitor.sh sources .env.monitor.
    """
    env_path = Path(__file__).parent / ".env.monitor"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:
        print(f"[WARN] notification env load failed: {exc}")


def _notify(title: str, message: str) -> None:
    """
    Best-effort operator notification. Never raises into settlement flow.
    Uses the same Pushover env vars and macOS notification pattern as
    tools/auto_monitor.sh.
    """
    try:
        _load_monitor_env()

        user_key = os.environ.get("PUSHOVER_USER_KEY", "")
        api_token = os.environ.get("PUSHOVER_API_TOKEN", "")
        if user_key and api_token:
            result = subprocess.run([
                "curl",
                "-sS",
                "--fail",
                "https://api.pushover.net/1/messages.json",
                "-F", f"token={api_token}",
                "-F", f"user={user_key}",
                "-F", f"title={title}",
                "-F", f"message={message}",
            ], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                print("[NOTIFY] Pushover alert sent")
            else:
                err = (result.stderr or result.stdout or "").strip()
                print(f"[WARN] pushover failed: {err}")
        else:
            print("[NOTIFY] Pushover skipped: PUSHOVER_USER_KEY/PUSHOVER_API_TOKEN not set")
    except Exception as exc:
        print(f"[WARN] pushover notification failed: {exc}")

    try:
        script = f'''
ObjC.import("Cocoa");
var notification = $.NSUserNotification.alloc.init;
notification.setTitle({json.dumps(title)});
notification.setInformativeText({json.dumps(message)});
$.NSUserNotificationCenter.defaultUserNotificationCenter.deliverNotification(notification);
'''
        subprocess.run([
            "osascript",
            "-l", "JavaScript",
            "-e", script,
        ], check=False)
    except Exception as exc:
        print(f"[WARN] macOS notification failed: {exc}")


def _edge_for_alert(trade: dict):
    return trade.get("risk_edge", trade.get("edge"))


def _format_trade_alert(trade: dict) -> str:
    return "\n".join([
        f"Ticker: {trade.get('ticker', 'UNKNOWN')}",
        f"Result: {trade.get('result', 'UNKNOWN')}",
        f"PnL: ${float(trade.get('pnl') or 0.0):+.2f}",
        f"Entry: {trade.get('entry_price', 'n/a')}",
        f"Exit: {trade.get('exit_price', 'n/a')}",
        f"Edge: {_edge_for_alert(trade) if _edge_for_alert(trade) is not None else 'n/a'}",
        f"CLV: {trade.get('clv', 'n/a')}",
        f"Status: {trade.get('status', 'UNKNOWN')}",
    ])


def _send_trade_settlement_alert(trade: dict) -> None:
    _notify("AI_System Trade Settled", _format_trade_alert(trade))


def _send_auto_settle_summary_alert(settled: int, forced_closed: int,
                                    pnl_delta: float, remaining_open: int) -> None:
    if settled + forced_closed <= 0:
        return
    message = "\n".join([
        "Auto-settle complete:",
        f"Settled: {settled}",
        f"Time exits: {forced_closed}",
        f"PnL change: ${pnl_delta:+.2f}",
        f"Remaining open: {remaining_open}",
    ])
    _notify("AI_System Auto-Settle", message)


# ── Kalshi API helper ──────────────────────────────────────────────────────────

def _to_prob(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, int):
        return round(value / 100.0, 6) if 0 <= value <= 100 else None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= v <= 1.0:
        return round(v, 6)
    if 1.0 < v <= 100.0:
        return round(v / 100.0, 6)
    return None


def fetch_market(ticker: str) -> Optional[dict]:
    data = kalshi_get(f"/markets/{ticker}", silent=False)
    if not data:
        return None
    return data.get("market") or data


def extract_market_resolution(market: Optional[dict]) -> Tuple[bool, Optional[str]]:
    if not market:
        return False, None

    status: str = (market.get("status") or "").strip().lower()
    result: str = (market.get("result") or "").strip().lower()

    if status in _RESOLVED_STATUSES and result in _TRADEABLE_RESULTS:
        return True, result.upper()  # "YES" or "NO"

    return False, None


def fetch_market_resolution(ticker: str) -> Tuple[bool, Optional[str]]:
    """
    Query Kalshi for a single market and extract its settlement outcome.

    Returns:
        (is_resolved, outcome)
        is_resolved: True only when the market has a definitive YES or NO result.
        outcome:     "YES" or "NO" if resolved; None otherwise.

    Will not raise — returns (False, None) on any API or parsing error.
    """
    return extract_market_resolution(fetch_market(ticker))


def _parse_timestamp(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _trade_age_seconds(trade: dict, now: datetime) -> Optional[float]:
    opened_at = _parse_timestamp(str(trade.get("timestamp") or ""))
    if opened_at is None:
        return None
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return (now - opened_at).total_seconds()


def _current_exit_price(trade: dict, market: Optional[dict]) -> Optional[float]:
    if not market:
        return None

    action = trade.get("action", "")
    if action == "BET_YES":
        for field in ("yes_bid_dollars", "yes_bid", "last_price", "last_price_dollars"):
            price = _to_prob(market.get(field))
            if price is not None:
                return price
        no_ask = _to_prob(market.get("no_ask_dollars") or market.get("no_ask"))
        if no_ask is not None:
            return round(1.0 - no_ask, 6)
    elif action == "BET_NO":
        for field in ("no_bid_dollars", "no_bid"):
            price = _to_prob(market.get(field))
            if price is not None:
                return price
        yes_ask = _to_prob(market.get("yes_ask_dollars") or market.get("yes_ask"))
        if yes_ask is not None:
            return round(1.0 - yes_ask, 6)

    return None


def _fallback_exit_price(trade: dict) -> float:
    for field in ("last_price", "current_price", "mark_price", "exit_price"):
        price = _to_prob(trade.get(field))
        if price is not None:
            return price
    return float(trade.get("entry_price", 0.0))


def _compute_time_exit_pnl(trade: dict, exit_price: float) -> float:
    entry = float(trade.get("entry_price", 0.0))
    size = float(trade.get("size", 0.0))
    return round((exit_price - entry) * size, 2)


def _compute_clv(trade: dict, exit_price: float) -> float:
    return round(float(exit_price) - float(trade.get("entry_price", 0.0)), 4)


def _format_clv_label(clv: float) -> str:
    if clv > 0:
        return "favorable"
    if clv < 0:
        return "unfavorable"
    return "flat"


def _time_exit_risk_result(pnl: float) -> str:
    if abs(pnl) < 0.01:
        return "NEUTRAL"
    return "WIN" if pnl > 0 else "LOSS"


def _append_forced_close_record(trader: PaperTrader, trade: dict, exit_price: float,
                                pnl: float, duration_seconds: float) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    # Preserve all entry-time metadata from the original OPEN row, then
    # override only terminal outcome fields for the appended TIME_EXIT row.
    closed = dict(trade)
    closed["status"] = "FORCED_CLOSE"
    closed["result"] = "TIME_EXIT"
    closed["pnl"] = pnl
    closed["exit_price"] = exit_price
    closed["clv"] = _compute_clv(trade, exit_price)
    closed["settled_at"] = now_iso
    closed["duration_seconds"] = round(duration_seconds, 2)
    closed["reason"] = "TIME_EXIT"
    closed["cleanup_reason"] = "TIME_EXIT"

    trader.logger.log_trade(closed)

    if trade in trader.open_trades:
        trader.open_trades.remove(trade)
    trader.trade_history.append(closed)

    trader.risk_manager.close_position(float(trade.get("size", 0.0)))
    risk_result = _time_exit_risk_result(pnl)
    if risk_result == "NEUTRAL":
        print("    [TIME_EXIT] Neutral exit — not counted as loss streak")
    trader.risk_manager.record_result(
        pnl=pnl,
        result=risk_result,
        size=float(trade.get("size", 0.0)),
        ticker=str(trade.get("ticker", "")),
    )
    return closed


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
    forced_closed = 0
    errors    = 0
    pnl_delta = 0.0
    now = datetime.now(timezone.utc)

    for trade in open_trades:
        ticker = trade.get("ticker", "?")
        action = trade.get("action", "?")
        size   = float(trade.get("size", 0.0))
        checked += 1

        print(f"  [{checked}/{total_open}] {ticker}")
        print(f"    Action : {action}  Entry : {trade.get('entry_price', '?')}  Size : ${size:.2f}")

        market = fetch_market(ticker)
        is_resolved, outcome = extract_market_resolution(market)

        if not is_resolved:
            duration_seconds = _trade_age_seconds(trade, now)
            if duration_seconds is None:
                print(f"    Status : UNRESOLVED — skipping (unknown age)")
                skipped += 1
                print()
                continue

            if duration_seconds < MAX_TRADE_DURATION_SECONDS:
                age_min = duration_seconds / 60.0
                print(f"    Status : UNRESOLVED — skipping (age={age_min:.1f}min)")
                skipped += 1
                print()
                continue

            exit_price = _current_exit_price(trade, market)
            if exit_price is None:
                exit_price = _fallback_exit_price(trade)
                print("    [TIME_EXIT] API failed — using fallback exit price")

            time_exit_pnl = _compute_time_exit_pnl(trade, exit_price)
            time_exit_clv = _compute_clv(trade, exit_price)
            age_min = duration_seconds / 60.0
            print(f"    Status : TIME-EXIT due  age={age_min:.1f}min")
            print(f"    Exit   : price={exit_price:.4f}  P&L=${time_exit_pnl:+.2f}")
            print(f"    [CLV] value={time_exit_clv:+.4f} ({_format_clv_label(time_exit_clv)})")

            if dry_run:
                print("    [DRY-RUN] Would append FORCED_CLOSE reason=TIME_EXIT")
            else:
                closed = _append_forced_close_record(
                    trader=trader,
                    trade=trade,
                    exit_price=exit_price,
                    pnl=time_exit_pnl,
                    duration_seconds=duration_seconds,
                )
                _send_trade_settlement_alert(closed)
                print("    Forced : FORCED_CLOSE  reason=TIME_EXIT")

            forced_closed += 1
            pnl_delta += time_exit_pnl
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
                _send_trade_settlement_alert(result)
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
    print(f"  Forced time exits  : {forced_closed}")
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
        _send_auto_settle_summary_alert(
            settled=settled,
            forced_closed=forced_closed,
            pnl_delta=pnl_delta,
            remaining_open=len(trader.open_trades),
        )

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
