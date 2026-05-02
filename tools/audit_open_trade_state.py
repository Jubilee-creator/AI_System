#!/usr/bin/env python3
"""
tools/audit_open_trade_state.py
---------------------------------
Read-only audit of the open-trade state in paper_trades.jsonl.

Reports:
  - Total raw OPEN rows in the log
  - Active (unresolved) OPENs: OPEN rows with no later SETTLED/FORCED_CLOSE
  - Ghost (stale) OPENs: OPEN rows that already have a settlement on disk
  - Current cap and whether flow control would block
  - Examples of active and ghost trades

This tool never writes, modifies, or settles anything.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "paper_trades.jsonl"

MAX_CONCURRENT_OPEN_TRADES = 3  # must match brain/paper_trader.py


def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    except ValueError:
        return None


def audit() -> None:
    if not LOG_PATH.exists():
        print(f"[AUDIT] Log not found: {LOG_PATH}")
        sys.exit(0)

    # ── Read all rows ─────────────────────────────────────────────────────────
    all_rows: list[dict] = []
    raw_open_rows: list[dict] = []
    for line in LOG_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        all_rows.append(row)
        if str(row.get("status", "")).upper() == "OPEN":
            raw_open_rows.append(row)

    # ── Build latest-status map using (ticker, timestamp) key ────────────────
    latest_status: dict[tuple, str] = {}
    for row in all_rows:
        key = (row.get("ticker", ""), row.get("timestamp", ""))
        latest_status[key] = str(row.get("status", "")).upper()

    # ── Classify each raw OPEN row ────────────────────────────────────────────
    active: list[dict] = []
    ghost: list[dict] = []
    for row in raw_open_rows:
        key = (row.get("ticker", ""), row.get("timestamp", ""))
        final = latest_status.get(key, "OPEN")
        if final == "OPEN":
            active.append(row)
        else:
            ghost.append({"row": row, "final_status": final})

    now = datetime.now(timezone.utc)
    cap_full = len(active) >= MAX_CONCURRENT_OPEN_TRADES

    # ── Print report ──────────────────────────────────────────────────────────
    print("=" * 70)
    print("OPEN-TRADE STATE AUDIT")
    print("=" * 70)
    print(f"  Log file          : {LOG_PATH}")
    print(f"  Total rows        : {len(all_rows)}")
    print(f"  Raw OPEN rows     : {len(raw_open_rows)}")
    print(f"  Active (unresolved): {len(active)}")
    print(f"  Ghost (stale)     : {len(ghost)}")
    print(f"  Cap               : MAX_CONCURRENT_OPEN_TRADES = {MAX_CONCURRENT_OPEN_TRADES}")
    cap_status = "FULL — would block new entries" if cap_full else "NOT full — new entries allowed"
    print(f"  Cap status        : {cap_status}")
    print()

    if active:
        print("ACTIVE TRADES (counting against cap):")
        for t in active:
            ts = _parse_ts(t.get("timestamp_utc") or t.get("timestamp"))
            age = f"{(now - ts).total_seconds() / 60:.1f}min" if ts else "age=?"
            print(
                f"  {t.get('ticker','?'):30s}  action={t.get('action','?'):8s}"
                f"  size=${float(t.get('size', 0)):.2f}  age={age}"
            )
        print()

    if ghost:
        print("GHOST TRADES (settled on disk, NOT yet pruned from in-memory list):")
        for g in ghost:
            t = g["row"]
            final = g["final_status"]
            ts = _parse_ts(t.get("timestamp_utc") or t.get("timestamp"))
            age = f"{(now - ts).total_seconds() / 60:.1f}min" if ts else "age=?"
            print(
                f"  {t.get('ticker','?'):30s}  action={t.get('action','?'):8s}"
                f"  size=${float(t.get('size', 0)):.2f}  age={age}"
                f"  final_status={final}"
            )
        print()

    if ghost and not active:
        print("NOTE: All OPEN rows are ghost/stale. In-memory cap is 0 after restart.")
        print("      If Dashboard still shows cap_full, it needs a restart to re-sync.")
        print("      After Phase 6G patch: _sync_open_trades_from_log() runs before")
        print("      each cap check, so restart is no longer required.")
        print()

    verdict = "OK — cap not blocking" if not cap_full else "WARN — cap at limit"
    print(f"RESULT: {verdict}")


if __name__ == "__main__":
    audit()
