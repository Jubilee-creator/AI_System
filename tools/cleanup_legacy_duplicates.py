"""
tools/cleanup_legacy_duplicates.py
-----------------------------------
Safe cleanup of legacy duplicate OPEN trades in logs/paper_trades.jsonl.

A "duplicate" exists when a ticker has more than 1 OPEN record.  This happened
before M13 added duplicate-guard logic in paper_trader.py.

Usage:
  python3 tools/cleanup_legacy_duplicates.py --dry-run    # inspect only
  python3 tools/cleanup_legacy_duplicates.py --execute    # rewrite log + risk_state

Safety guarantees:
  - Creates logs/paper_trades.jsonl.bak.<timestamp> before any write
  - Earliest OPEN record per ticker is kept; later ones are VOIDED (not deleted)
  - VOID records get: status="VOID_LEGACY_DUPLICATE", result="VOID", pnl=0.0,
    exit_price=null, settled_at=<now UTC>, cleanup_reason=<label>
  - risk_state.json is rebuilt from remaining OPEN non-TEST trades
  - Accounting fields (daily_pnl, weekly_pnl, trades_today, loss_streak) preserved
  - Bad-JSON lines are never touched
"""

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── PATHS ──────────────────────────────────────────────────────────────────

ROOT        = Path(__file__).parent.parent
TRADES_LOG  = ROOT / "logs" / "paper_trades.jsonl"
RISK_STATE  = ROOT / "data" / "risk_state.json"


# ─── HELPERS ────────────────────────────────────────────────────────────────

def load_all_lines() -> list:
    """
    Return list of (line_idx, raw_text, parsed_or_None) for every non-blank
    line in paper_trades.jsonl.  Unparseable lines have parsed=None and are
    preserved verbatim during rewrite.
    """
    if not TRADES_LOG.exists():
        return []
    result = []
    with open(TRADES_LOG) as f:
        for i, line in enumerate(f):
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            result.append((i, raw, parsed))
    return result


def find_duplicates(lines: list) -> dict:
    """
    Return dict of tickers that have net OPEN count > 1.

    For each such ticker:
      "opens"    — list of (line_idx, record) sorted oldest-first
      "settled"  — count of SETTLED records for this ticker
      "net_open" — len(opens) - settled
    """
    open_records   = {}   # ticker -> [(line_idx, record), ...]
    settled_counts = {}   # ticker -> int

    for idx, raw, rec in lines:
        if rec is None:
            continue
        ticker = rec.get("ticker", "")
        if not ticker:
            continue
        status = rec.get("status", "")
        if status == "OPEN":
            open_records.setdefault(ticker, []).append((idx, rec))
        elif status == "SETTLED":
            settled_counts[ticker] = settled_counts.get(ticker, 0) + 1

    duplicates = {}
    for ticker, opens in open_records.items():
        settled  = settled_counts.get(ticker, 0)
        net_open = len(opens) - settled
        if net_open > 1:
            opens_sorted = sorted(opens, key=lambda x: x[1].get("timestamp", ""))
            duplicates[ticker] = {
                "opens":    opens_sorted,
                "settled":  settled,
                "net_open": net_open,
            }
    return duplicates


def make_void_record(rec: dict, now_iso: str) -> dict:
    """Return a copy of rec with all VOID fields applied."""
    voided = dict(rec)
    voided["status"]         = "VOID_LEGACY_DUPLICATE"
    voided["result"]         = "VOID"
    voided["pnl"]            = 0.0
    voided["exit_price"]     = None
    voided["settled_at"]     = now_iso
    voided["cleanup_reason"] = "pre-M13 duplicate ticker legacy cleanup"
    return voided


def _is_test_ticker(ticker: str) -> bool:
    t = ticker.upper()
    if t.startswith("TEST"):
        return True
    if t in {"BTCUSD-UP", "BTCUSD-DOWN", "ETHD-UP", "UNKNOWN"}:
        return True
    return False


# ─── RISK STATE ─────────────────────────────────────────────────────────────

def load_risk_state() -> dict:
    if not RISK_STATE.exists():
        return {}
    try:
        with open(RISK_STATE) as f:
            return json.load(f)
    except Exception as e:
        print(f"  [WARN] Could not read {RISK_STATE}: {e}")
        return {}


def rebuild_risk_state(lines: list) -> None:
    """
    Count real OPEN paper trades from lines, then merge-write risk_state.json.
    Accounting fields (daily_pnl, weekly_pnl, trades_today, loss_streak)
    are always preserved from the existing file.

    Inclusion criteria (ALL must be true):
      status == "OPEN"
      is_paper == True
      not a test ticker

    Explicitly excluded:
      status == "VOID_LEGACY_DUPLICATE"
      status == "SETTLED"
      is_paper != True
    """
    real_open_count    = 0
    real_open_exposure = 0.0
    counted_trades     = []

    for _, _, rec in lines:
        if rec is None:
            continue
        if rec.get("status") != "OPEN":
            continue
        if rec.get("is_paper") is not True:
            continue
        ticker = rec.get("ticker", "")
        if _is_test_ticker(ticker):
            continue
        size = float(rec.get("size") or rec.get("entry_cost") or 0.0)
        real_open_count    += 1
        real_open_exposure += size
        counted_trades.append((ticker, size))

    print(f"  [REBUILD] Counting {real_open_count} valid OPEN paper trade(s):")
    for tk, sz in counted_trades:
        print(f"    {tk}  ${sz:.2f}")
    if not counted_trades:
        print("    (none)")

    existing = load_risk_state()
    merged   = dict(existing)
    merged["open_positions"]     = real_open_count
    merged["total_exposure"]     = round(real_open_exposure, 2)
    merged["open_risk_exposure"] = round(real_open_exposure, 2)
    merged["last_updated"]       = datetime.now(timezone.utc).isoformat()

    preserved = {k: merged[k] for k in
                 ["daily_pnl", "weekly_pnl", "trades_today", "loss_streak"]
                 if k in merged}
    if preserved:
        parts = ", ".join(f"{k}={v}" for k, v in preserved.items())
        print(f"  [CLEANUP] Preserved: {parts}")

    RISK_STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(RISK_STATE, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"  [CLEANUP] risk_state.json → open_positions={real_open_count}, "
          f"total_exposure=${real_open_exposure:.2f}")


# ─── MAIN ───────────────────────────────────────────────────────────────────

def run(execute: bool) -> None:
    mode    = "EXECUTE" if execute else "DRY-RUN"
    now_iso = datetime.now(timezone.utc).isoformat()

    print("\n" + "=" * 66)
    print(f"LEGACY DUPLICATE CLEANUP  [{mode}]")
    print("=" * 66)

    if not TRADES_LOG.exists():
        print(f"  [WARN] Trade log not found: {TRADES_LOG}")
        print("  Nothing to do.\n")
        return

    lines = load_all_lines()
    print(f"  Log:         {TRADES_LOG}")
    print(f"  Total lines: {len(lines)}")

    duplicates = find_duplicates(lines)

    if not duplicates:
        print("\n  No duplicate OPEN positions found.  Nothing to do.")
        print("=" * 66 + "\n")
        return

    # ── Analyse what needs to change ─────────────────────────────────────────
    total_voided          = 0
    total_exposure_before = 0.0
    total_exposure_after  = 0.0
    void_line_indices     = set()

    print(f"\n  Found {len(duplicates)} ticker(s) with duplicate OPEN positions:\n")

    for ticker, info in duplicates.items():
        opens    = info["opens"]    # sorted oldest-first
        settled  = info["settled"]
        net_open = info["net_open"]

        # Positions at indices 0..settled-1 already match settled records.
        # Index `settled` is the first still-open one — we keep that.
        # Everything from settled+1 onward is a duplicate → VOID.
        keep_idx   = settled
        void_start = settled + 1

        keep_record  = opens[keep_idx]
        void_records = opens[void_start:]

        keep_size  = float(keep_record[1].get("size") or
                           keep_record[1].get("entry_cost") or 0.0)
        void_sizes = [float(r.get("size") or r.get("entry_cost") or 0.0)
                      for _, r in void_records]

        total_exposure_before += keep_size + sum(void_sizes)
        total_exposure_after  += keep_size
        total_voided          += len(void_records)

        for line_idx, _ in void_records:
            void_line_indices.add(line_idx)

        print(f"  Ticker: {ticker}")
        print(f"    OPEN records   : {len(opens)}")
        print(f"    SETTLED records: {settled}")
        print(f"    Net open       : {net_open}  (1 is correct)")
        ts_keep = keep_record[1].get("timestamp", "?")[:19]
        print(f"    KEEP  line {keep_record[0]:>4}  ts={ts_keep}  ${keep_size:.2f}")
        for (line_idx, r), sz in zip(void_records, void_sizes):
            ts_v = r.get("timestamp", "?")[:19]
            print(f"    VOID  line {line_idx:>4}  ts={ts_v}  ${sz:.2f}")
        print()

    print(f"  Summary:")
    print(f"    Tickers affected : {len(duplicates)}")
    print(f"    Records to void  : {total_voided}")
    print(f"    Exposure before  : ${total_exposure_before:.2f}")
    print(f"    Exposure after   : ${total_exposure_after:.2f}")
    print(f"    Exposure freed   : "
          f"${total_exposure_before - total_exposure_after:.2f}")

    if not execute:
        print(f"\n  [DRY-RUN] No changes written.  "
              f"Re-run with --execute to apply.")
        print("=" * 66 + "\n")
        return

    # ── Backup ───────────────────────────────────────────────────────────────
    ts_tag = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = TRADES_LOG.parent / f"paper_trades.jsonl.bak.{ts_tag}"
    shutil.copy2(TRADES_LOG, backup)
    print(f"\n  Backup: {backup.name}")

    # ── Rewrite log ──────────────────────────────────────────────────────────
    new_lines = []
    for idx, raw, rec in lines:
        if idx in void_line_indices and rec is not None:
            new_lines.append(json.dumps(make_void_record(rec, now_iso)))
        else:
            new_lines.append(raw)

    with open(TRADES_LOG, "w") as f:
        f.write("\n".join(new_lines) + "\n")

    print(f"  Rewrote {len(new_lines)} lines → {TRADES_LOG.name}")

    # ── Rebuild risk state ───────────────────────────────────────────────────
    rebuild_risk_state(load_all_lines())

    print(f"\n  Done.  {total_voided} duplicate(s) voided, "
          f"risk_state.json updated.")
    print("=" * 66 + "\n")


# ─── ENTRY POINT ────────────────────────────────────────────────────────────

USAGE = """
cleanup_legacy_duplicates.py — legacy duplicate OPEN trade cleanup

Usage:
  python3 tools/cleanup_legacy_duplicates.py --dry-run    # inspect only
  python3 tools/cleanup_legacy_duplicates.py --execute    # apply changes
"""

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("--dry-run", "--execute"):
        print(USAGE)
        sys.exit(1)
    run(execute=(sys.argv[1] == "--execute"))
