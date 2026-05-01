"""
tools/performance_report.py
----------------------------
Aggregate performance summary of paper trading activity.

Reads logs/paper_trades.jsonl and reports stats on SETTLED trades only.

Open-position reconciliation
-----------------------------
The log is append-only: when a trade settles, the original OPEN record stays
and a new SETTLED (or FORCED_CLOSE / VOID_LEGACY_DUPLICATE) record is appended
with the same (timestamp, ticker, action, size, entry_price).

A raw OPEN row is "active" only if no resolved record with a matching key
exists anywhere in the log.  The report breaks raw OPEN rows into:
  active  — no matching SETTLED / FORCED_CLOSE / VOID_LEGACY_DUPLICATE
  stale   — matched, i.e. already resolved by a later record

Excluded from win/loss stats:
  - status="SETTLED" with a trade key that also has FORCED_CLOSE or
    VOID_LEGACY_DUPLICATE terminal records
  - status="VOID_LEGACY_DUPLICATE"  (duplicate cleanup artifacts)
  - status="FORCED_CLOSE"           (mid-price exits, not event outcomes)
  - status="OPEN"                   (open or stale — not settled outcomes)
  - Records with no "status" field  (old-format pre-M13 test records)

Usage:
  python3 tools/performance_report.py
"""

import json
import sys
from pathlib import Path


# ─── PATHS ──────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).parent.parent
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
sys.path.insert(0, str(ROOT))

from config.trading_config import DATA_COLLECTION_MODE, GLOBAL_FORCED_LEARNING_MODE


# ─── HELPERS ────────────────────────────────────────────────────────────────

def load_trades() -> list:
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
    return float(rec.get("pnl") or rec.get("realized_pnl") or 0.0)


def get_size(rec: dict) -> float:
    return float(rec.get("size") or rec.get("entry_cost") or 0.0)


def get_clv(rec: dict):
    if rec.get("clv") is not None:
        return float(rec["clv"])
    if rec.get("entry_price") is None or rec.get("exit_price") is None:
        return None
    return round(float(rec["exit_price"]) - float(rec["entry_price"]), 4)


def _trade_key(rec: dict):
    """
    Stable identity key for an open/resolved trade pair.

    Uses (timestamp[:19], ticker, action, size_rounded, entry_price_rounded).
    Truncating timestamp to seconds guards against minor formatting differences
    between when the OPEN was written and when it was resolved.
    Returns None if required fields are absent.
    """
    ts     = str(rec.get("timestamp") or "")[:19]
    ticker = rec.get("ticker", "")
    action = rec.get("action", "")
    if not (ts and ticker and action):
        return None
    size  = round(float(rec["size"]),         6) if rec.get("size")         is not None else None
    ep    = round(float(rec["entry_price"]),  6) if rec.get("entry_price")  is not None else None
    return (ts, ticker, action, size, ep)


def classify_open_records(all_records: list) -> tuple[list, list]:
    """
    Split all status=="OPEN" records into (active, stale).

    A record is stale when any SETTLED / FORCED_CLOSE / VOID_LEGACY_DUPLICATE
    record in the log shares the same trade key.
    """
    _resolved_statuses = {"SETTLED", "FORCED_CLOSE", "VOID_LEGACY_DUPLICATE"}

    resolved_keys: set = set()
    for rec in all_records:
        if rec.get("status") in _resolved_statuses:
            k = _trade_key(rec)
            if k is not None:
                resolved_keys.add(k)

    active, stale = [], []
    for rec in all_records:
        if rec.get("status") != "OPEN":
            continue
        k = _trade_key(rec)
        if k is not None and k in resolved_keys:
            stale.append(rec)
        else:
            active.append(rec)

    return active, stale


def build_terminal_key_sets(all_records: list) -> tuple[set, set, set]:
    """
    Build terminal-state identity sets for conflict-safe performance stats.

    A clean SETTLED trade cannot share its trade key with FORCED_CLOSE or
    VOID_LEGACY_DUPLICATE.  The report is read-only and never rewrites logs.
    """
    settled_keys: set = set()
    forced_close_keys: set = set()
    void_keys: set = set()

    for rec in all_records:
        status = rec.get("status")
        if status not in {"SETTLED", "FORCED_CLOSE", "VOID_LEGACY_DUPLICATE"}:
            continue
        key = _trade_key(rec)
        if key is None:
            continue
        if status == "SETTLED":
            settled_keys.add(key)
        elif status == "FORCED_CLOSE":
            forced_close_keys.add(key)
        elif status == "VOID_LEGACY_DUPLICATE":
            void_keys.add(key)

    return settled_keys, forced_close_keys, void_keys


def classify_settled_records(
    all_records: list,
    settled_keys: set,
    forced_close_keys: set,
    void_keys: set,
) -> tuple[list, list]:
    """
    Split SETTLED rows into (clean, conflicted).

    Clean SETTLED rows are the only rows counted in performance metrics.
    Conflicted SETTLED rows are terminal-state collisions and are reported
    separately for validation visibility.
    """
    clean, conflicted = [], []

    for rec in all_records:
        if rec.get("status") != "SETTLED":
            continue
        key = _trade_key(rec)
        if (
            key is not None
            and key in settled_keys
            and key not in forced_close_keys
            and key not in void_keys
        ):
            clean.append(rec)
        else:
            conflicted.append(rec)

    return clean, conflicted


# ─── REPORT ─────────────────────────────────────────────────────────────────

def run() -> None:
    all_records = load_trades()

    total_lines  = len(all_records)
    void_count   = sum(1 for r in all_records
                       if r.get("status") == "VOID_LEGACY_DUPLICATE")
    forced_count = sum(1 for r in all_records
                       if r.get("status") == "FORCED_CLOSE")
    raw_open     = sum(1 for r in all_records if r.get("status") == "OPEN")
    no_status    = sum(1 for r in all_records if "status" not in r)

    active_opens, stale_opens = classify_open_records(all_records)
    active_exposure = sum(get_size(r) for r in active_opens)

    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted_settled = classify_settled_records(
        all_records,
        settled_keys,
        forced_close_keys,
        void_keys,
    )
    clean_data_collection = sum(1 for r in clean_settled if r.get("data_collection_override"))
    clean_bootstrap = sum(1 for r in clean_settled if r.get("bootstrap_provisional"))
    clean_normal = max(0, len(clean_settled) - clean_data_collection - clean_bootstrap)

    wins   = [r for r in clean_settled if get_pnl(r) > 0]
    losses = [r for r in clean_settled if get_pnl(r) < 0]
    pushes = [r for r in clean_settled if get_pnl(r) == 0]

    total_pnl     = sum(get_pnl(r) for r in clean_settled)
    gross_profit  = sum(get_pnl(r) for r in wins)
    gross_loss    = sum(get_pnl(r) for r in losses)
    total_wagered = sum(get_size(r) for r in clean_settled)

    win_rate      = len(wins) / len(clean_settled) * 100 if clean_settled else 0.0
    avg_win       = gross_profit / len(wins)   if wins   else 0.0
    avg_loss      = gross_loss   / len(losses) if losses else 0.0
    profit_factor = (gross_profit / abs(gross_loss)
                     if gross_loss != 0 else float("inf"))
    roi           = (total_pnl / total_wagered * 100) if total_wagered > 0 else 0.0

    conf_vals = [float(r["confidence"]) for r in clean_settled if "confidence" in r]
    edge_vals = [float(r["edge"])       for r in clean_settled if "edge"       in r]
    avg_conf  = sum(conf_vals) / len(conf_vals) if conf_vals else None
    avg_edge  = sum(edge_vals) / len(edge_vals) if edge_vals else None

    clv_vals = [v for v in (get_clv(r) for r in clean_settled) if v is not None]
    avg_clv = sum(clv_vals) / len(clv_vals) if clv_vals else None
    clv_positive = sum(1 for v in clv_vals if v > 0)
    clv_negative = sum(1 for v in clv_vals if v < 0)
    clv_flat = sum(1 for v in clv_vals if v == 0)

    forced_clv_vals = [
        v for v in (
            get_clv(r) for r in all_records if r.get("status") == "FORCED_CLOSE"
        )
        if v is not None
    ]
    forced_avg_clv = (
        sum(forced_clv_vals) / len(forced_clv_vals)
        if forced_clv_vals else None
    )

    # Per-ticker settled breakdown
    tickers: dict = {}
    for r in clean_settled:
        tk = r.get("ticker", "?")
        if tk not in tickers:
            tickers[tk] = {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
        tickers[tk]["count"] += 1
        tickers[tk]["pnl"]   += get_pnl(r)
        if get_pnl(r) > 0:
            tickers[tk]["wins"]   += 1
        elif get_pnl(r) < 0:
            tickers[tk]["losses"] += 1

    # ── Print ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("PAPER TRADING PERFORMANCE REPORT")
    print("=" * 66)
    print(f"  Log:            {TRADES_LOG}")
    print(f"  Total records:  {total_lines}")
    print(f"    SETTLED:      {len(clean_settled)}  (clean — counted in stats)")
    print(f"      conflicted: {len(conflicted_settled)}  (excluded — terminal-state conflict)")
    print(f"    OPEN (raw):   {raw_open}")
    print(f"      active:     {len(active_opens)}")
    print(f"      stale:      {len(stale_opens)}  (resolved by a later record)")
    print(f"    VOIDED:       {void_count}  (excluded — duplicate cleanup)")
    print(f"    FORCED_CLOSE: {forced_count}  (excluded — mid-price exits, not event outcomes)")
    if forced_avg_clv is not None:
        forced_pos = sum(1 for v in forced_clv_vals if v > 0)
        forced_neg = sum(1 for v in forced_clv_vals if v < 0)
        forced_flat = sum(1 for v in forced_clv_vals if v == 0)
        print(
            f"      CLV avg:    {forced_avg_clv:+.4f}  "
            f"(positive={forced_pos}, negative={forced_neg}, flat={forced_flat})"
        )
    print(f"    no-status:    {no_status}   (excluded — old format)")
    print()
    print("── SIZING MODE ─────────────────────────────────────────────")
    print(
        "  Kelly sizing:   "
        + ("DISABLED — calculated/logged for audit only" if GLOBAL_FORCED_LEARNING_MODE else "ENABLED")
    )
    print(
        "  Actual sizing:  "
        + ("forced minimum learning bet" if GLOBAL_FORCED_LEARNING_MODE else "Kelly/risk path")
    )
    print(
        "  Data collection:"
        + (" ENABLED — Council blocks may be logged as $5 data-collection rows"
           if DATA_COLLECTION_MODE else " DISABLED")
    )
    print(
        f"  Clean SETTLED classes: normal={clean_normal}  "
        f"data_collection={clean_data_collection}  bootstrap={clean_bootstrap}"
    )

    # ── Active open positions ────────────────────────────────────────────────
    print()
    print(f"── ACTIVE OPEN POSITIONS ({len(active_opens)}) "
          + "─" * (38 - len(str(len(active_opens)))))
    if active_opens:
        print(f"  {'Timestamp':<20} {'Ticker':<35} {'Act':<5} "
              f"{'Size':>7}  {'Entry':>7}  {'Conf':>5}  {'Edge':>6}")
        for r in sorted(active_opens, key=lambda x: x.get("timestamp", "")):
            ts    = str(r.get("timestamp", ""))[:19]
            tk    = r.get("ticker", "?")
            act   = r.get("action", "?")
            sz    = get_size(r)
            ep    = r.get("entry_price")
            ep_s  = f"{float(ep):.4f}" if ep is not None else "  n/a "
            conf  = r.get("confidence")
            conf_s = f"{float(conf):.3f}" if conf is not None else " n/a"
            edge  = r.get("edge")
            edge_s = f"{float(edge):.4f}" if edge is not None else "  n/a"
            print(f"  {ts:<20} {tk:<35} {act:<5} "
                  f"${sz:>6.2f}  {ep_s:>7}  {conf_s:>5}  {edge_s:>6}")
        print(f"  {'Active exposure':<60} ${active_exposure:.2f}")
    else:
        print("  (none — all positions resolved)")

    if not clean_settled:
        print()
        print("  No clean SETTLED trades to report.")
        print("=" * 66 + "\n")
        return

    # ── Settled performance ──────────────────────────────────────────────────
    print()
    print("── CLEAN SETTLED PERFORMANCE ───────────────────────────────")
    print(f"  Trades settled: {len(clean_settled)}")
    print(f"    Wins:         {len(wins)}")
    print(f"    Losses:       {len(losses)}")
    print(f"    Pushes:       {len(pushes)}")
    print(f"  Win rate:       {win_rate:.1f}%")
    print()
    print(f"  Total P&L:      ${total_pnl:+.2f}")
    print(f"  Gross profit:   ${gross_profit:.2f}")
    print(f"  Gross loss:     ${gross_loss:.2f}")
    pf_str = f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞ (no losses)"
    print(f"  Profit factor:  {pf_str}")
    print(f"  Avg win:        ${avg_win:+.2f}")
    print(f"  Avg loss:       ${avg_loss:+.2f}")
    print(f"  Total wagered:  ${total_wagered:.2f}")
    print(f"  ROI:            {roi:+.2f}%")
    if avg_conf is not None:
        print(f"  Avg confidence: {avg_conf:.3f}")
    if avg_edge is not None:
        print(f"  Avg edge:       {avg_edge:.4f}")
    if avg_clv is not None:
        print(f"  Avg CLV:        {avg_clv:+.4f}")
        print(
            f"  CLV dist:       positive={clv_positive}  "
            f"negative={clv_negative}  flat={clv_flat}"
        )

    if len(tickers) > 1:
        print()
        print("── PER-TICKER (settled) ────────────────────────────────────")
        for tk, info in sorted(tickers.items(),
                               key=lambda x: x[1]["pnl"], reverse=True):
            wr = info["wins"] / info["count"] * 100 if info["count"] else 0
            print(f"  {tk:<42}  n={info['count']:>3}  "
                  f"pnl=${info['pnl']:+7.2f}  wr={wr:.0f}%")

    print("=" * 66 + "\n")


# ─── ENTRY POINT ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()
