"""
tools/cleanup_legacy_duplicates.py
-----------------------------------
Safe cleanup of legacy duplicate OPEN trades in paper_trades.jsonl.

A "duplicate" is any ticker that has more OPEN records than SETTLED records,
meaning net open count > 1.  This happened before M13 added duplicate-guard
logic in paper_trader.py.

Usage:
  python3 tools/cleanup_legacy_duplicates.py --dry-run    # inspect only
  python3 tools/cleanup_legacy_duplicates.py --execute    # rewrite log + risk_state

Safety guarantees:
  - Always creates logs/paper_trades.jsonl.bak.<timestamp> before touching anything
  - Preserves daily_pnl, weekly_pnl, trades_today, loss_streak in risk_state.json
  - Earliest OPEN record per ticker is kept; extras are VOIDED (not deleted)
  - VOIDED records are marked status="VOID_LEGACY_DUPLICATE" with pnl=0.0
  - Bad JSON lines are preserved as-is (not touched)
  - If net open count is exactly 1 for every ticker, no changes are made
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

def load_all_lines() -> list[tuple[int, str, dict | None]]:
    """
    Load every line from paper_trades.jsonl.
    Returns list of (line_index, raw_text, parsed_or_None).
    Unparseable lines are included with parsed=None so they survive rewrite.
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


def find_duplicates(lines: list[tuple[int, str, dict | None]]) -> dict:
    """
    Analyse net open count per ticker.

    Returns a dict keyed by ticker:
      {
        "opens":    [(line_idx, record), ...],   # all OPEN records, oldest first
        "settled":  int,                          # number of SETTLED records
        "net_open": int,                          # opens - settled
      }

    Only tickers with net_open > 1 are included (those are the duplicates).
    """
    open_records:    dict[str, list[tuple[int, dict]]] = {}
    settled_counts:  dict[str, int] = {}

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
        settled = settled_counts.get(ticker, 0)
        net_open = len(opens) - settled
        if net_open > 1:
            # Sort by timestamp so we keep the earliest one
            opens_sorted = sorted(opens, key=lambda x: x[1].get("timestamp", ""))
            duplicates[ticker] = {
                "opens":    opens_sorted,
                "settled":  settled,
                "net_open": net_open,
            }

    return duplicates


def make_void_record(rec: dict, now_iso: str) -> dict:
    """Return a copy of rec with VOID_LEGACY_DUPLICATE fields applied."""
    voided = dict(rec)
    voided["status"]          = "VOID_LEGACY_DUPLICATE"
    voided["result"]          = "VOID"
    voided["pnl"]             = 0.0
    voided["exit_price"]      = None
    voided["settled_at"]      = now_iso
    voided["cleanup_reason"]  = "pre-M13 duplicate ticker legacy cleanup"
    return voided


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


def rebuild_risk_state(lines: list[tuple[int, str, dict | None]]) -> None:
    """
    Recount open_positions and total_exposure from the (already-updated) lines,
    then merge-write into risk_state.json, preserving all accounting fields.
    """
    real_open_count = 0
    real_open_exposure = 0.0

    for _, _, rec in lines:
        if rec is None:
            continue
        if rec.get("status") != "OPEN":
            continue
        ticker = rec.get("ticker", "")
        if _is_test_ticker(ticker):
            continue
        size = float(rec.get("size") or rec.get("entry_cost") or 0.0)
        real_open_count    += 1
        real_open_exposure += size

    existing = load_risk_state()
    updates  = dict(existing)
    updates["open_positions"]    = real_open_count
    updates["total_exposure"]    = round(real_open_exposure, 2)
    updates["open_risk_exposure"] = round(real_open_exposure, 2)
    updates["last_updated"]      = datetime.now(timezone.utc).isoformat()

    RISK_STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(RISK_STATE, "w") as f:
        json.dump(updates, f, indent=2)

    preserved = {k: updates[k] for k in
                 ["daily_pnl", "weekly_pnl", "trades_today", "loss_streak"]
                 if k in updates}
    if preserved:
        parts = ", ".join(f"{k}={v}" for k, v in preserved.items())
        print(f"  [CLEANUP] Preserved risk fields: {parts}")

    print(f"  [CLEANUP] risk_state.json → open_positions={real_open_count}, "
          f"total_exposure=${real_open_exposure:.2f}")


def _is_test_ticker(ticker: str) -> bool:
    t = ticker.upper()
    if t.startswith("TEST"):
        return True
    if t in {"BTCUSD-UP", "BTCUSD-DOWN", "ETHD-UP", "UNKNOWN"}:
        return True
    return False


# ─── MAIN LOGIC ─────────────────────────────────────────────────────────────

def run(execute: bool) -> None:
    mode = "EXECUTE" if execute else "DRY-RUN"
    now_iso = datetime.now(timezone.utc).isoformat()

    print("\n" + "=" * 66)
    print(f"LEGACY DUPLICATE CLEANUP  [{mode}]")
    print("=" * 66)

    if not TRADES_LOG.exists():
        print(f"  [WARN] Trade log not found: {TRADES_LOG}")
        print("  Nothing to do.\n")
        return

    lines = load_all_lines()
    print(f"  Log:          {TRADES_LOG}")
    print(f"  Total lines:  {len(lines)}")

    duplicates = find_duplicates(lines)

    if not duplicates:
        print("\n  No duplicate OPEN positions found.  Nothing to do.")
        print("=" * 66 + "\n")
        return

    # ── Report what we found ────────────────────────────────────────────────
    total_voided     = 0
    total_exposure_before = 0.0
    total_exposure_after  = 0.0
    void_line_indices: set[int] = set()

    print(f"\n  Found {len(duplicates)} ticker(s) with duplicate OPEN positions:\n")

    for ticker, info in duplicates.items():
        opens    = info["opens"]      # sorted oldest-first
        settled  = info["settled"]
        net_open = info["net_open"]

        # keeps[0..settled-1] are already matched to settled records → keep all
        # The first unsettled open is at index `settled` → keep that one
        keep_idx   = settled          # index in opens list to keep
        void_start = settled + 1      # everything from here is voided

        keep_record  = opens[keep_idx]
        void_records = opens[void_start:]

        keep_size  = float(keep_record[1].get("size") or keep_record[1].get("entry_cost") or 0.0)
        void_sizes = [float(r[1].get("size") or r[1].get("entry_cost") or 0.0)
                      for _, r in void_records]
        void_exposure = sum(void_sizes)

        total_exposure_before += keep_size + void_exposure
        total_exposure_after  += keep_size
        total_voided          += len(void_records)

        for line_idx, _ in void_records:
            void_line_indices.add(line_idx)

        print(f"  Ticker: {ticker}")
        print(f"    total OPEN records : {len(opens)}")
        print(f"    settled records    : {settled}")
        print(f"    net open           : {net_open}  (should be 1)")
        print(f"    KEEP  line {keep_record[0]:>4}  "
              f"ts={keep_record[1].get('timestamp','?')[:19]}  "
              f"${keep_size:.2f}")
        for line_idx, r in void_records:
            sz = float(r.get("size") or r.get("entry_cost") or 0.0)
            print(f"    VOID  line {line_idx:>4}  "
                  f"ts={r.get('timestamp','?')[:19]}  "
                  f"${sz:.2f}")
        print()

    print(f"  Summary:")
    print(f"    Tickers affected  : {len(duplicates)}")
    print(f"    Records to void   : {total_voided}")
    print(f"    Exposure before   : ${total_exposure_before:.2f}")
    print(f"    Exposure after    : ${total_exposure_after:.2f}")
    print(f"    Exposure freed    : ${total_exposure_before - total_exposure_after:.2f}")

    if not execute:
        print(f"\n  [DRY-RUN] No changes written.  Re-run with --execute to apply.")
        print("=" * 66 + "\n")
        return

    # ── Backup ──────────────────────────────────────────────────────────────
    ts_tag = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = TRADES_LOG.with_suffix(f".jsonl.bak.{ts_tag}")
    shutil.copy2(TRADES_LOG, backup)
    print(f"\n  Backup: {backup}")

    # ── Rewrite log ─────────────────────────────────────────────────────────
    new_lines: list[str] = []
    for idx, raw, rec in lines:
        if idx in void_line_indices and rec is not None:
            voided = make_void_record(rec, now_iso)
            new_lines.append(json.dumps(voided))
        else:
            new_lines.append(raw)

    with open(TRADES_LOG, "w") as f:
        for line in new_lines:
            f.write(line + "\n")

    print(f"  Rewrote {len(new_lines)} lines → {TRADES_LOG}")

    # ── Rebuild risk state ───────────────────────────────────────────────────
    # Re-parse the new file so rebuild_risk_state sees the VOID records
    updated_lines = load_all_lines()
    rebuild_risk_state(updated_lines)

    print(f"\n  Done.  {total_voided} duplicate(s) voided, "
          f"risk_state.json updated.")
    print("=" * 66 + "\n")


# ─── ENTRY POINT ────────────────────────────────────────────────────────────

USAGE = """
cleanup_legacy_duplicates.py — safe legacy duplicate OPEN trade cleanup

Usage:
  python3 tools/cleanup_legacy_duplicates.py --dry-run    # inspect only
  python3 tools/cleanup_legacy_duplicates.py --execute    # apply changes
"""

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("--dry-run", "--execute"):
        print(USAGE)
        sys.exit(1)

    execute = sys.argv[1] == "--execute"
    run(execute=execute)
