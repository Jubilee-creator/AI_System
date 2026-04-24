"""
analytics/performance_analyzer.py
----------------------------------
Paper trade performance analyzer.

Data source : logs/paper_trades.jsonl
Output      : analytics/performance_report.json + terminal summary

Run:
    python3 analytics/performance_analyzer.py
    python3 analytics/performance_analyzer.py --log path/to/paper_trades.jsonl
"""

import json
import sys
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any


# ── paths ──────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
DEFAULT_LOG   = ROOT / "logs" / "paper_trades.jsonl"
DEFAULT_OUT   = ROOT / "analytics" / "performance_report.json"


# ═══════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════

def load_settled_trades(log_path: Path) -> List[Dict[str, Any]]:
    """
    Read paper_trades.jsonl and return only settled records.

    Each trade is logged twice (OPEN then SETTLED); we keep only the
    final SETTLED copy identified by status == 'SETTLED' or a non-null
    result field ('WIN' / 'LOSS').
    """
    if not log_path.exists():
        return []

    seen: Dict[tuple, Dict] = {}  # (ticker, open_timestamp) → record
    with open(log_path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue

            status = rec.get("status", "")
            result = rec.get("result")
            is_settled = status == "SETTLED" or result in ("WIN", "LOSS")
            if not is_settled:
                continue

            key = (rec.get("ticker", ""), rec.get("timestamp", ""))
            # Last write wins; if we already have a settled copy, keep it
            existing = seen.get(key)
            if existing is None or status == "SETTLED":
                seen[key] = rec

    trades = list(seen.values())
    trades.sort(key=lambda t: t.get("timestamp", ""))
    return trades


# ═══════════════════════════════════════════════════════════════════
# CORE METRICS
# ═══════════════════════════════════════════════════════════════════

def compute_core(trades: List[Dict]) -> Dict[str, Any]:
    if not trades:
        return {
            "settled_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "total_pnl": 0.0,
            "avg_win": None,
            "avg_loss": None,
            "expectancy": None,
            "profit_factor": None,
        }

    wins   = [t for t in trades if t.get("result") == "WIN"]
    losses = [t for t in trades if t.get("result") == "LOSS"]

    total_pnl   = sum(t.get("pnl", 0.0) for t in trades)
    gross_win   = sum(t.get("pnl", 0.0) for t in wins)
    gross_loss  = abs(sum(t.get("pnl", 0.0) for t in losses))

    win_rate    = len(wins) / len(trades)
    avg_win     = gross_win  / len(wins)   if wins   else 0.0
    avg_loss    = gross_loss / len(losses) if losses else 0.0
    expectancy  = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    return {
        "settled_trades": len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(win_rate, 4),
        "total_pnl":      round(total_pnl, 2),
        "avg_win":        round(avg_win, 2),
        "avg_loss":       round(avg_loss, 2),
        "expectancy":     round(expectancy, 3),
        "profit_factor":  round(profit_factor, 3) if profit_factor is not None else None,
    }


# ═══════════════════════════════════════════════════════════════════
# BUCKET ANALYSIS
# ═══════════════════════════════════════════════════════════════════

EDGE_BUCKETS = [
    ("0.00–0.01", 0.00, 0.01),
    ("0.01–0.03", 0.01, 0.03),
    ("0.03–0.05", 0.03, 0.05),
    ("0.05+",     0.05, float("inf")),
]

CONF_BUCKETS = [
    ("0.60–0.70", 0.60, 0.70),
    ("0.70–0.80", 0.70, 0.80),
    ("0.80–0.90", 0.80, 0.90),
    ("0.90+",     0.90, float("inf")),
]


def _bucket_stats(subset: List[Dict]) -> Dict[str, Any]:
    if not subset:
        return {"count": 0, "win_rate": None, "total_pnl": 0.0, "avg_pnl": 0.0}
    wins      = sum(1 for t in subset if t.get("result") == "WIN")
    total_pnl = sum(t.get("pnl", 0.0) for t in subset)
    return {
        "count":     len(subset),
        "win_rate":  round(wins / len(subset), 4),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl":   round(total_pnl / len(subset), 3),
    }


def bucket_by(trades: List[Dict], field: str, buckets: list) -> Dict[str, Any]:
    result = {}
    for label, lo, hi in buckets:
        subset = [t for t in trades if lo <= t.get(field, 0.0) < hi]
        result[label] = _bucket_stats(subset)
    return result


# ═══════════════════════════════════════════════════════════════════
# REPORT ASSEMBLY
# ═══════════════════════════════════════════════════════════════════

def build_report(trades: List[Dict]) -> Dict[str, Any]:
    return {
        "generated_at":       datetime.utcnow().isoformat() + "Z",
        "source_trades":      len(trades),
        "core":               compute_core(trades),
        "by_edge":            bucket_by(trades, "edge",       EDGE_BUCKETS),
        "by_confidence":      bucket_by(trades, "confidence", CONF_BUCKETS),
    }


# ═══════════════════════════════════════════════════════════════════
# TERMINAL PRINTER
# ═══════════════════════════════════════════════════════════════════

def _pct(v):
    return f"{v*100:.1f}%" if v is not None else "—"

def _dollar(v):
    if v is None:
        return "—"
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

def _val(v, fmt=".3f"):
    return format(v, fmt) if v is not None else "—"


def print_report(report: Dict) -> None:
    W = 58
    c = report["core"]
    sep = "─" * W

    print()
    print("═" * W)
    print("  PAPER TRADE PERFORMANCE REPORT")
    print(f"  {report['generated_at'][:19].replace('T',' ')} UTC")
    print("═" * W)

    if c["settled_trades"] == 0:
        print("\n  No settled trades found in log.\n")
        print("═" * W)
        return

    # ── core ──
    print(f"\n  {'CORE METRICS':}")
    print(f"  {sep}")
    print(f"  {'Settled trades':<28} {c['settled_trades']}")
    print(f"  {'Wins / Losses':<28} {c['wins']} / {c['losses']}")
    print(f"  {'Win rate':<28} {_pct(c['win_rate'])}")
    print(f"  {'Total P&L':<28} {_dollar(c['total_pnl'])}")
    print(f"  {'Avg win':<28} {_dollar(c['avg_win'])}")
    print(f"  {'Avg loss':<28} {_dollar(c['avg_loss'])}")
    print(f"  {'Expectancy / trade':<28} {_dollar(c['expectancy'])}")
    print(f"  {'Profit factor':<28} {_val(c['profit_factor'])}")

    # ── edge buckets ──
    print(f"\n  {'BY EDGE BUCKET':}")
    print(f"  {sep}")
    hdr = f"  {'Bucket':<14}{'Trades':>7}{'Win%':>8}{'Total P&L':>12}{'Avg P&L':>10}"
    print(hdr)
    print(f"  {sep}")
    for label, stats in report["by_edge"].items():
        if stats["count"] == 0:
            print(f"  {label:<14}{'—':>7}")
            continue
        print(
            f"  {label:<14}"
            f"{stats['count']:>7}"
            f"{_pct(stats['win_rate']):>8}"
            f"{_dollar(stats['total_pnl']):>12}"
            f"{_dollar(stats['avg_pnl']):>10}"
        )

    # ── confidence buckets ──
    print(f"\n  {'BY CONFIDENCE BUCKET':}")
    print(f"  {sep}")
    print(hdr)
    print(f"  {sep}")
    for label, stats in report["by_confidence"].items():
        if stats["count"] == 0:
            print(f"  {label:<14}{'—':>7}")
            continue
        print(
            f"  {label:<14}"
            f"{stats['count']:>7}"
            f"{_pct(stats['win_rate']):>8}"
            f"{_dollar(stats['total_pnl']):>12}"
            f"{_dollar(stats['avg_pnl']):>10}"
        )

    print()
    print("═" * W)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Paper trade performance analyzer")
    parser.add_argument("--log", default=str(DEFAULT_LOG),
                        help="Path to paper_trades.jsonl")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="Path for JSON output report")
    args = parser.parse_args()

    log_path = Path(args.log)
    out_path = Path(args.out)

    trades = load_settled_trades(log_path)
    report = build_report(trades)

    print_report(report)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  JSON report → {out_path}\n")


if __name__ == "__main__":
    main()
