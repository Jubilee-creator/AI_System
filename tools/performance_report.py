#!/usr/bin/env python3
"""
tools/performance_report.py — Paper trading performance report

Reads logs/paper_trades.jsonl (read-only) and prints a full performance
breakdown.  Does not touch any trading, risk, or settlement logic.

Usage:
    python3 tools/performance_report.py
    python3 tools/performance_report.py --log path/to/custom.jsonl
"""

import json
import argparse
import os
import sys
from pathlib import Path
from typing import List, Dict, Any

# Locate the default log relative to this file's position (tools/ → project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = str(_PROJECT_ROOT / "logs" / "paper_trades.jsonl")


# ── Log loading ────────────────────────────────────────────────────────────────

def load_trades(log_path: str) -> List[Dict[str, Any]]:
    """
    Load all trade records from a JSONL file.

    Deduplicates by (ticker, timestamp) — last line wins — matching the same
    logic PaperTrader uses on startup.  Returns the deduplicated trade list.
    """
    if not os.path.exists(log_path):
        print(f"[ERROR] Log file not found: {log_path}")
        sys.exit(1)

    trades_by_key: dict = {}
    skipped = 0

    with open(log_path, "r") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                t = json.loads(raw)
                key = (t.get("ticker", ""), t.get("timestamp", ""))
                trades_by_key[key] = t
            except json.JSONDecodeError:
                skipped += 1

    if skipped:
        print(f"[WARN] Skipped {skipped} malformed line(s) in {log_path}")

    return list(trades_by_key.values())


# ── Statistics ─────────────────────────────────────────────────────────────────

def _avg(values: list) -> float:
    return sum(values) / len(values) if values else 0.0


def compute_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute all performance metrics from the deduplicated trade list."""

    total   = len(trades)
    open_t  = [t for t in trades if t.get("status") == "OPEN"]
    settled = [t for t in trades if t.get("status") == "SETTLED"]
    wins    = [t for t in settled if t.get("result") == "WIN"]
    losses  = [t for t in settled if t.get("result") == "LOSS"]

    n_settled = len(settled)
    n_wins    = len(wins)
    n_losses  = len(losses)
    win_rate  = n_wins / n_settled if n_settled else 0.0

    # P&L
    pnls        = [float(t.get("pnl", 0.0)) for t in settled]
    win_pnls    = [float(t.get("pnl", 0.0)) for t in wins]
    loss_pnls   = [abs(float(t.get("pnl", 0.0))) for t in losses]  # positive magnitude

    total_pnl = sum(pnls)
    avg_pnl   = _avg(pnls)
    avg_win   = _avg(win_pnls)
    avg_loss  = _avg(loss_pnls)

    # Expectancy: avg profit per trade if played many times
    # expectancy = (win_rate × avg_win) − ((1 − win_rate) × avg_loss)
    expectancy = (win_rate * avg_win) - ((1.0 - win_rate) * avg_loss)

    # Edge & confidence breakdown by outcome
    win_edges  = [float(t["edge"])      for t in wins   if t.get("edge")       is not None]
    loss_edges = [float(t["edge"])      for t in losses if t.get("edge")       is not None]
    win_confs  = [float(t["confidence"])for t in wins   if t.get("confidence") is not None]
    loss_confs = [float(t["confidence"])for t in losses if t.get("confidence") is not None]

    # Open exposure
    open_exposure = sum(float(t.get("size", 0.0)) for t in open_t)

    return {
        "total_trades":    total,
        "open_trades":     len(open_t),
        "open_exposure":   open_exposure,
        "settled_trades":  n_settled,
        "wins":            n_wins,
        "losses":          n_losses,
        "win_rate":        win_rate,
        "total_pnl":       total_pnl,
        "avg_pnl":         avg_pnl,
        "avg_win":         avg_win,
        "avg_loss":        avg_loss,
        "expectancy":      expectancy,
        "avg_edge_wins":   _avg(win_edges),
        "avg_edge_losses": _avg(loss_edges),
        "avg_conf_wins":   _avg(win_confs),
        "avg_conf_losses": _avg(loss_confs),
    }


# ── Report rendering ───────────────────────────────────────────────────────────

def print_report(stats: Dict[str, Any], log_path: str) -> None:
    s = stats
    n_settled = s["settled_trades"]

    def pct(v: float) -> str:
        return f"{v * 100:.1f}%"

    def dollar(v: float, sign: bool = False) -> str:
        return f"${v:+.2f}" if sign else f"${v:.2f}"

    print()
    print("=" * 46)
    print("  PERFORMANCE REPORT")
    print(f"  {log_path}")
    print("=" * 46)

    print(f"\n  {'Trades (total):':<28} {s['total_trades']}")
    print(f"  {'Open:':<28} {s['open_trades']}  (${s['open_exposure']:.2f} exposure)")
    print(f"  {'Settled:':<28} {n_settled}")
    print(f"  {'Wins:':<28} {s['wins']}")
    print(f"  {'Losses:':<28} {s['losses']}")

    print(f"\n  {'Win rate:':<28} {pct(s['win_rate'])}")
    print(f"  {'Total P&L:':<28} {dollar(s['total_pnl'], sign=True)}")
    print(f"  {'Avg P&L per settled trade:':<28} {dollar(s['avg_pnl'], sign=True)}")
    print(f"  {'Avg win size:':<28} {dollar(s['avg_win'])}")
    print(f"  {'Avg loss size:':<28} {dollar(s['avg_loss'])}")
    print(f"  {'Expectancy:':<28} {dollar(s['expectancy'], sign=True)}")

    print(f"\n  {'Avg edge (wins):':<28} {s['avg_edge_wins']:.4f}")
    print(f"  {'Avg edge (losses):':<28} {s['avg_edge_losses']:.4f}")
    print(f"  {'Avg confidence (wins):':<28} {pct(s['avg_conf_wins'])}")
    print(f"  {'Avg confidence (losses):':<28} {pct(s['avg_conf_losses'])}")

    # Verdict
    print()
    if n_settled == 0:
        verdict = "No settled trades yet."
    elif s["expectancy"] > 0:
        verdict = f"Positive expectancy — edge exists over {n_settled} settled trades."
    else:
        verdict = f"Negative expectancy over {n_settled} settled trades — review strategy."

    print(f"  Verdict: {verdict}")
    print("=" * 46)
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="performance_report.py",
        description="Paper trading performance report (read-only)"
    )
    parser.add_argument(
        "--log", default=DEFAULT_LOG, metavar="PATH",
        help=f"Path to JSONL trade log (default: {DEFAULT_LOG})"
    )
    args = parser.parse_args()

    trades = load_trades(args.log)
    stats  = compute_stats(trades)
    print_report(stats, args.log)


if __name__ == "__main__":
    main()
