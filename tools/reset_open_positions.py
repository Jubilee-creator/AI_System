"""
tools/reset_open_positions.py
------------------------------
Validation reset: force-close all OPEN paper trades in paper_trades.jsonl.

Use this when entering a validation phase and needing a clean slate — all
positions are closed with pnl=0.0 so no phantom exposure carries forward.

Usage:
  python3 tools/reset_open_positions.py --dry-run    # inspect only
  python3 tools/reset_open_positions.py --execute    # rewrite log + risk_state

Targets (ALL must be true):
  status == "OPEN"
  is_paper == True

Converts each target to:
  status        = "FORCED_CLOSE"
  result        = "VOID"
  pnl           = 0.0
  exit_price    = null
  settled_at    = current UTC timestamp
  cleanup_reason = "validation reset"

Safety guarantees:
  - Creates logs/paper_trades.jsonl.bak.<timestamp> before any write
  - No records are deleted; targets are rewritten in-place
  - Bad-JSON lines are preserved verbatim
  - risk_state.json updated to open_positions=0, total_exposure=0
  - Accounting fields (daily_pnl, weekly_pnl, trades_today, loss_streak) preserved
"""

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── PATHS ──────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).parent.parent
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
RISK_STATE = ROOT / "data" / "risk_state.json"


# ─── HELPERS ────────────────────────────────────────────────────────────────

def load_all_lines() -> list:
    """
    Return list of (line_idx, raw_text, parsed_or_None) for every non-blank
    line.  Unparseable lines have parsed=None and survive rewrite unchanged.
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


def make_forced_close(rec: dict, now_iso: str) -> dict:
    """Return a copy of rec converted to a FORCED_CLOSE record."""
    closed = dict(rec)
    closed["status"]         = "FORCED_CLOSE"
    closed["result"]         = "VOID"
    closed["pnl"]            = 0.0
    closed["exit_price"]     = None
    closed["settled_at"]     = now_iso
    closed["cleanup_reason"] = "validation reset"
    return closed


def is_open_paper(rec: dict) -> bool:
    return rec.get("status") == "OPEN" and rec.get("is_paper") is True


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


def zero_risk_state() -> None:
    """
    Set open_positions and total_exposure to 0 in risk_state.json.
    Preserves daily_pnl, weekly_pnl, trades_today, loss_streak.
    """
    existing = load_risk_state()
    merged   = dict(existing)
    merged["open_positions"]     = 0
    merged["total_exposure"]     = 0.0
    merged["open_risk_exposure"] = 0.0
    merged["last_updated"]       = datetime.now(timezone.utc).isoformat()

    preserved = {k: merged[k] for k in
                 ["daily_pnl", "weekly_pnl", "trades_today", "loss_streak"]
                 if k in merged}
    if preserved:
        parts = ", ".join(f"{k}={v}" for k, v in preserved.items())
        print(f"  [RESET] Preserved: {parts}")

    RISK_STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(RISK_STATE, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"  [RESET] risk_state.json → open_positions=0, total_exposure=0.0")


# ─── MAIN ───────────────────────────────────────────────────────────────────

def run(execute: bool) -> None:
    mode    = "EXECUTE" if execute else "DRY-RUN"
    now_iso = datetime.now(timezone.utc).isoformat()

    print("\n" + "=" * 66)
    print(f"VALIDATION RESET — FORCE-CLOSE OPEN PAPER POSITIONS  [{mode}]")
    print("=" * 66)

    if not TRADES_LOG.exists():
        print(f"  [WARN] Trade log not found: {TRADES_LOG}")
        print("  Nothing to do.\n")
        return

    lines = load_all_lines()
    print(f"  Log:         {TRADES_LOG}")
    print(f"  Total lines: {len(lines)}")

    targets = [(idx, raw, rec) for idx, raw, rec in lines
               if rec is not None and is_open_paper(rec)]

    if not targets:
        print("\n  No OPEN paper positions found.  Nothing to do.")
        print("=" * 66 + "\n")
        return

    total_exposure = sum(
        float(rec.get("size") or rec.get("entry_cost") or 0.0)
        for _, _, rec in targets
    )

    print(f"\n  OPEN paper positions to force-close: {len(targets)}\n")
    for _, _, rec in targets:
        ts   = (rec.get("timestamp") or "")[:19]
        tk   = rec.get("ticker", "?")
        sz   = float(rec.get("size") or rec.get("entry_cost") or 0.0)
        act  = rec.get("action", "?")
        print(f"    {ts}  {tk:<38}  {act:<6}  ${sz:.2f}")

    print(f"\n  Total exposure to release: ${total_exposure:.2f}")

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
    target_indices = {idx for idx, _, _ in targets}
    new_lines = []
    for idx, raw, rec in lines:
        if idx in target_indices and rec is not None:
            new_lines.append(json.dumps(make_forced_close(rec, now_iso)))
        else:
            new_lines.append(raw)

    with open(TRADES_LOG, "w") as f:
        f.write("\n".join(new_lines) + "\n")

    print(f"  Rewrote {len(new_lines)} lines → {TRADES_LOG.name}")

    # ── Zero risk state ──────────────────────────────────────────────────────
    zero_risk_state()

    print(f"\n  Done.  {len(targets)} position(s) force-closed, "
          f"${total_exposure:.2f} exposure released.")
    print("=" * 66 + "\n")


# ─── ENTRY POINT ────────────────────────────────────────────────────────────

USAGE = """
reset_open_positions.py — validation reset: force-close all OPEN paper trades

Usage:
  python3 tools/reset_open_positions.py --dry-run    # inspect only
  python3 tools/reset_open_positions.py --execute    # apply changes
"""

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("--dry-run", "--execute"):
        print(USAGE)
        sys.exit(1)
    run(execute=(sys.argv[1] == "--execute"))
