#!/usr/bin/env python3
"""
tools/audit_execution_blockers.py
-----------------------------------
Read-only diagnostic report for execution_funnel.jsonl.

Shows, for the current (or most recent) Dashboard session:
  - Blocker counts by final_reason
  - Phase 6G health: cap_already_full distribution
  - BLOCKED_RISK breakdown with risk_block_reason
  - BLOCKED_COUNCIL breakdown with council_reason
  - Top blocked tickers
  - TRADE_OPENED rows (if any)

Usage:
  python3 tools/audit_execution_blockers.py           # most recent session
  python3 tools/audit_execution_blockers.py --all     # all sessions today
  python3 tools/audit_execution_blockers.py --session dashboard_run_20260502_092819
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "execution_funnel.jsonl"


def _load_rows() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    rows = []
    for line in LOG_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _pick_session(rows: list[dict], session_arg: str | None, all_today: bool) -> tuple[list[dict], str]:
    today = _today_str()
    if session_arg:
        filtered = [r for r in rows if r.get("run_id") == session_arg]
        return filtered, session_arg
    if all_today:
        filtered = [r for r in rows if (r.get("run_id") or "").find(today) != -1]
        return filtered, f"all sessions on {today}"
    # Most recent run_id
    run_ids = [r.get("run_id") for r in rows if r.get("run_id")]
    if not run_ids:
        return rows, "ALL"
    latest = max(set(run_ids), key=lambda rid: rid or "")
    filtered = [r for r in rows if r.get("run_id") == latest]
    return filtered, latest


def report(rows: list[dict], label: str) -> None:
    print("=" * 70)
    print(f"EXECUTION FUNNEL BLOCKER AUDIT — {label}")
    print("=" * 70)
    print(f"  Total rows in window : {len(rows)}")
    print()

    if not rows:
        print("  (no rows)")
        return

    # ── Phase 6G health ───────────────────────────────────────────────────────
    cap_dist = Counter(r.get("cap_already_full") for r in rows)
    print("PHASE 6G HEALTH (cap_already_full distribution):")
    for val, count in sorted(cap_dist.items(), key=lambda x: str(x[0])):
        marker = "  ⚠  GHOST-OPEN DEADLOCK ACTIVE" if val is True else ""
        print(f"  cap_full={val!r:8s}: {count:5d}{marker}")
    print()

    # ── Blocker counts ────────────────────────────────────────────────────────
    reasons = Counter(r.get("final_reason", "UNKNOWN") for r in rows)
    print("BLOCKER COUNTS:")
    total = len(rows)
    for reason, count in reasons.most_common():
        pct = 100.0 * count / total if total else 0.0
        print(f"  {reason:<40s} {count:5d}  ({pct:.1f}%)")
    print()

    # ── BLOCKED_RISK details ──────────────────────────────────────────────────
    br_rows = [r for r in rows if r.get("final_reason") == "BLOCKED_RISK"]
    if br_rows:
        print(f"BLOCKED_RISK ({len(br_rows)} rows) — risk_block_reason breakdown:")
        reason_ctr: Counter = Counter()
        reason_examples: dict[str, str] = {}
        for r in br_rows:
            raw_reason = r.get("risk_block_reason") or "NULL (trace too short — reason unknown)"
            # Normalise long dynamic reasons to a short key for grouping
            if raw_reason.startswith("Edge ") and "below minimum" in raw_reason:
                key = "Edge below minimum (post-council adjustment)"
            elif raw_reason.startswith("Effective daily risk") or "open_exposure" in raw_reason:
                key = "Effective daily risk exceeds limit"
            elif raw_reason.startswith("Daily loss limit"):
                key = "Daily loss limit hit"
            elif raw_reason.startswith("Max open positions"):
                key = "Max open positions reached"
            elif raw_reason.startswith("Cooldown"):
                key = "Cooldown active"
            elif raw_reason.startswith("Max trades"):
                key = "Max trades per day"
            elif raw_reason.startswith("NULL"):
                key = raw_reason
            else:
                key = raw_reason[:80]
            reason_ctr[key] += 1
            if key not in reason_examples:
                reason_examples[key] = r.get("ticker", "?")
        for key, count in reason_ctr.most_common():
            print(f"  [{count:3d}]  {key}")
            print(f"         example ticker: {reason_examples[key]}")
        print()

    # ── BLOCKED_COUNCIL details ───────────────────────────────────────────────
    bc_rows = [r for r in rows if r.get("final_reason") == "BLOCKED_COUNCIL"]
    if bc_rows:
        print(f"BLOCKED_COUNCIL ({len(bc_rows)} rows) — council_reason breakdown:")
        creason_ctr: Counter = Counter()
        creason_ex: dict[str, str] = {}
        for r in bc_rows:
            raw = r.get("council_reason") or "NULL (trace too short — reason unknown)"
            key = raw[:100]
            creason_ctr[key] += 1
            if key not in creason_ex:
                creason_ex[key] = r.get("ticker", "?")
        for key, count in creason_ctr.most_common():
            print(f"  [{count:3d}]  {key}")
            print(f"         example ticker: {creason_ex[key]}")
        print()

    # ── Top blocked tickers ───────────────────────────────────────────────────
    blocked = [r for r in rows if not r.get("paper_trade_opened")]
    ticker_ctr: Counter = Counter(r.get("ticker", "?") for r in blocked)
    print(f"TOP 10 BLOCKED TICKERS (across all block reasons):")
    for ticker, count in ticker_ctr.most_common(10):
        print(f"  {ticker:<45s} {count:4d}")
    print()

    # ── Trades opened ─────────────────────────────────────────────────────────
    opened = [r for r in rows if r.get("paper_trade_opened") or r.get("final_reason") == "TRADE_OPENED"]
    print(f"TRADES OPENED: {len(opened)}")
    for r in opened[:5]:
        print(
            f"  {r.get('ticker','?'):40s}  action={r.get('scanner_action','?'):8s}"
            f"  edge={r.get('edge')}"
        )
    if len(opened) > 5:
        print(f"  ... and {len(opened)-5} more")
    print()

    # ── Verdict ───────────────────────────────────────────────────────────────
    cap_blocked = cap_dist.get(True, 0)
    if cap_blocked > 0:
        print(f"VERDICT: ⚠  Phase 6G ghost-open issue STILL ACTIVE ({cap_blocked} cap_full=True rows). Restart Dashboard.")
    else:
        print("VERDICT: Phase 6G confirmed — cap_full=False on all rows in this window.")
    print()


def main() -> None:
    args = sys.argv[1:]
    all_today = "--all" in args
    session_arg = None
    if "--session" in args:
        idx = args.index("--session")
        if idx + 1 < len(args):
            session_arg = args[idx + 1]

    rows = _load_rows()
    subset, label = _pick_session(rows, session_arg, all_today)
    report(subset, label)


if __name__ == "__main__":
    main()
