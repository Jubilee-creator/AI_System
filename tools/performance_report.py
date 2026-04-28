"""
tools/performance_report.py
----------------------------
Performance summary of paper trading activity.

Reads logs/paper_trades.jsonl and reports aggregate stats on SETTLED trades.

Excludes:
  - VOID_LEGACY_DUPLICATE records (cleanup artifacts, not real trades)
  - OPEN records (not yet resolved)
  - Old-format records with no status field (pre-M13 test artifacts)

Usage:
  python3 tools/performance_report.py
"""

import json
from pathlib import Path


# ─── PATHS ──────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).parent.parent
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"


# ─── HELPERS ────────────────────────────────────────────────────────────────

def load_trades() -> list[dict]:
    """Load all parseable records from paper_trades.jsonl."""
    if not TRADES_LOG.exists():
        return []
    records = []
    with open(TRADES_LOG) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  [WARN] Line {i} skipped (bad JSON): {e}")
    return records


def get_pnl(rec: dict) -> float:
    """Extract P&L, handling field name variants."""
    return float(rec.get("pnl") or rec.get("realized_pnl") or 0.0)


def get_size(rec: dict) -> float:
    return float(rec.get("size") or rec.get("entry_cost") or 0.0)


# ─── REPORT ─────────────────────────────────────────────────────────────────

def run() -> None:
    all_records = load_trades()

    total_lines  = len(all_records)
    void_count   = sum(1 for r in all_records
                       if r.get("status") == "VOID_LEGACY_DUPLICATE")
    open_count   = sum(1 for r in all_records if r.get("status") == "OPEN")
    no_status    = sum(1 for r in all_records if "status" not in r)

    settled = [r for r in all_records if r.get("status") == "SETTLED"]

    wins   = [r for r in settled if get_pnl(r) > 0]
    losses = [r for r in settled if get_pnl(r) < 0]
    pushes = [r for r in settled if get_pnl(r) == 0]

    total_pnl     = sum(get_pnl(r) for r in settled)
    gross_profit  = sum(get_pnl(r) for r in wins)
    gross_loss    = sum(get_pnl(r) for r in losses)   # negative
    total_wagered = sum(get_size(r) for r in settled)

    win_rate = len(wins) / len(settled) * 100 if settled else 0.0
    avg_win  = gross_profit / len(wins)   if wins   else 0.0
    avg_loss = gross_loss   / len(losses) if losses else 0.0
    profit_factor = (gross_profit / abs(gross_loss)
                     if gross_loss != 0 else float("inf"))

    roi = (total_pnl / total_wagered * 100) if total_wagered > 0 else 0.0

    # Confidence and edge averages
    conf_vals = [float(r["confidence"]) for r in settled if "confidence" in r]
    edge_vals = [float(r["edge"])       for r in settled if "edge"       in r]
    avg_conf  = sum(conf_vals) / len(conf_vals) if conf_vals else None
    avg_edge  = sum(edge_vals) / len(edge_vals) if edge_vals else None

    # Per-ticker breakdown
    tickers: dict[str, dict] = {}
    for r in settled:
        tk = r.get("ticker", "?")
        if tk not in tickers:
            tickers[tk] = {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
        tickers[tk]["count"]  += 1
        tickers[tk]["pnl"]    += get_pnl(r)
        if get_pnl(r) > 0:
            tickers[tk]["wins"]   += 1
        elif get_pnl(r) < 0:
            tickers[tk]["losses"] += 1

    # ── Print ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("PAPER TRADING PERFORMANCE REPORT")
    print("=" * 66)
    print(f"  Log:              {TRADES_LOG}")
    print(f"  Total records:    {total_lines}")
    print(f"    SETTLED:        {len(settled)}")
    print(f"    OPEN:           {open_count}")
    print(f"    VOIDED:         {void_count}  (excluded from stats)")
    print(f"    no-status:      {no_status}   (old format, excluded)")

    if not settled:
        print("\n  No SETTLED trades to report.")
        print("=" * 66 + "\n")
        return

    print()
    print("── OVERALL ─────────────────────────────────────────────────")
    print(f"  Trades (settled): {len(settled)}")
    print(f"    Wins:           {len(wins)}")
    print(f"    Losses:         {len(losses)}")
    print(f"    Pushes:         {len(pushes)}")
    print(f"  Win rate:         {win_rate:.1f}%")
    print()
    print(f"  Total P&L:        ${total_pnl:+.2f}")
    print(f"  Gross profit:     ${gross_profit:.2f}")
    print(f"  Gross loss:       ${gross_loss:.2f}")
    print(f"  Profit factor:    {profit_factor:.2f}"
          + ("  (no losses)" if gross_loss == 0 else ""))
    print(f"  Avg win:          ${avg_win:+.2f}")
    print(f"  Avg loss:         ${avg_loss:+.2f}")
    print(f"  Total wagered:    ${total_wagered:.2f}")
    print(f"  ROI:              {roi:+.2f}%")
    if avg_conf is not None:
        print(f"  Avg confidence:   {avg_conf:.3f}")
    if avg_edge is not None:
        print(f"  Avg edge:         {avg_edge:.4f}")

    if len(tickers) > 1:
        print()
        print("── PER-TICKER BREAKDOWN ────────────────────────────────────")
        sorted_tickers = sorted(tickers.items(), key=lambda x: x[1]["pnl"], reverse=True)
        for tk, info in sorted_tickers:
            wr = info["wins"] / info["count"] * 100 if info["count"] else 0
            print(f"  {tk:<40}  "
                  f"n={info['count']:>3}  "
                  f"pnl=${info['pnl']:+6.2f}  "
                  f"wr={wr:.0f}%")

    print("=" * 66 + "\n")


# ─── ENTRY POINT ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
