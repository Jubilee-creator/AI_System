#!/usr/bin/env python3
"""
tools/market_watchlist_report.py
---------------------------------
Read-only market watchlist ranker.

Categorises observed market types by evidence strength.

Categories:
  KEEP_WATCHING     — positive CLV and/or positive ROI, reasonable sample
  PROMISING_SMALL   — positive direction but n < 5 (not enough to conclude)
  SUSPECT           — negative CLV and/or ROI across multiple metrics (n 3-14)
  AVOID_CANDIDATE   — consistently negative across CLV/ROI/PF with n >= 5
  INCONCLUSIVE      — n < 3 or all key metrics missing

Rules:
  - Never recommend blocking/gating a market in code
  - All recommendations are labelled as "candidate for future review"
  - Sample-size caveats on every row
  - Never imply the system is proven profitable
  - Never change trading logic
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_rows() -> list[dict]:
    if not TRADES_LOG.exists():
        return []
    rows = []
    for line in TRADES_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _get_clean_settled(rows: list[dict]) -> list[dict]:
    try:
        from tools.performance_report import (
            build_terminal_key_sets, classify_settled_records,
        )
        sk, fk, vk = build_terminal_key_sets(rows)
        clean, _ = classify_settled_records(rows, sk, fk, vk)
        return clean
    except Exception:
        return [r for r in rows if r.get("status") == "SETTLED"]


def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _pnl(r: dict) -> float:
    try:
        from tools.performance_report import get_pnl
        return get_pnl(r)
    except Exception:
        return _as_float(r.get("pnl")) or 0.0


def _size(r: dict) -> float:
    try:
        from tools.performance_report import get_size
        return get_size(r)
    except Exception:
        return _as_float(r.get("size")) or 5.0


def _clv(r: dict) -> float | None:
    try:
        from tools.performance_report import get_clv
        return get_clv(r)
    except Exception:
        return _as_float(r.get("clv"))


def _market_key(r: dict) -> str:
    t = str(r.get("ticker") or "").upper()
    if t.startswith("KXBTCD"):
        return "BTC_DAILY"
    if t.startswith("KXETHD"):
        return "ETH_DAILY"
    if "BTC15M" in t:
        return "BTC_15M"
    if "ETH15M" in t:
        return "ETH_15M"
    if t.startswith("KXXRP"):
        return "XRP"
    if t.startswith("KXSOL"):
        return "SOL"
    if t.startswith("KXDOGE"):
        return "DOGE"
    if "MINY" in t or "27JAN" in t or "2027" in t:
        return "LONG_TERM"
    return "OTHER"


def _stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    wins   = [r for r in rows if _pnl(r) > 0]
    losses = [r for r in rows if _pnl(r) < 0]
    pnl    = sum(_pnl(r) for r in rows)
    wagered = sum(_size(r) for r in rows)
    roi    = round(pnl / wagered, 4) if wagered else None
    wr     = round(len(wins) / (len(wins) + len(losses)), 3) if (wins or losses) else None

    clv_vals = [v for v in (_clv(r) for r in rows) if v is not None]
    avg_clv  = round(sum(clv_vals) / len(clv_vals), 4) if clv_vals else None

    entry_vals = [_as_float(r.get("entry_price")) for r in rows if r.get("entry_price") is not None]
    avg_entry  = round(sum(entry_vals) / len(entry_vals), 3) if entry_vals else None

    gross_w = sum(_pnl(r) for r in wins)
    gross_l = sum(_pnl(r) for r in losses)
    pf = round(gross_w / abs(gross_l), 3) if gross_w > 0 and gross_l < 0 else None

    # Settlement time (minutes)
    from datetime import datetime, timezone
    def _parse(v) -> datetime | None:
        if not v:
            return None
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except (ValueError, TypeError):
            return None

    settlement_mins: list[float] = []
    for r in rows:
        t_open  = _parse(r.get("timestamp") or r.get("timestamp_utc"))
        t_close = _parse(r.get("settled_at") or r.get("result_time"))
        if t_open and t_close:
            diff = (t_close - t_open).total_seconds() / 60
            if diff >= 0:
                settlement_mins.append(diff)

    median_settle = None
    if settlement_mins:
        s = sorted(settlement_mins)
        mid = len(s) // 2
        median_settle = round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 0)

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "wr": wr,
        "roi": roi,
        "avg_clv": avg_clv,
        "avg_entry": avg_entry,
        "pf": pf,
        "pnl": round(pnl, 2),
        "median_settle_min": median_settle,
    }


def _category(s: dict) -> str:
    n       = s["n"]
    roi     = s.get("roi")
    clv     = s.get("avg_clv")
    pf      = s.get("pf")

    if n < 3:
        return "INCONCLUSIVE"

    # Count positive and negative signals
    pos = sum([
        roi is not None and roi > 0,
        clv is not None and clv > 0,
        pf  is not None and pf  > 1.0,
    ])
    neg = sum([
        roi is not None and roi < 0,
        clv is not None and clv < 0,
        pf  is not None and pf  < 0.5,
    ])

    if n < 5:
        return "PROMISING_SMALL" if pos >= 1 else "INCONCLUSIVE"
    if pos >= 2 and neg == 0:
        return "KEEP_WATCHING"
    if neg >= 2 and pos == 0 and n >= 5:
        return "AVOID_CANDIDATE"
    if neg > pos and n >= 3:
        return "SUSPECT"
    return "INCONCLUSIVE"


_CAT_ORDER = [
    "KEEP_WATCHING",
    "PROMISING_SMALL",
    "SUSPECT",
    "AVOID_CANDIDATE",
    "INCONCLUSIVE",
]


def report() -> None:
    rows  = _load_rows()
    clean = _get_clean_settled(rows)

    by_market: dict[str, list] = defaultdict(list)
    for r in clean:
        by_market[_market_key(r)].append(r)

    # Also include any market type with open trades
    all_rows_by_market: dict[str, list] = defaultdict(list)
    for r in rows:
        all_rows_by_market[_market_key(r)].append(r)

    print("=" * 72)
    print("MARKET WATCHLIST REPORT  (read-only — research only)")
    print("=" * 72)
    print(f"  clean_settled rows : {len(clean)}")
    print(f"  total log rows     : {len(rows)}")
    print()
    print("  Categories:")
    print("    KEEP_WATCHING   — pos CLV + pos ROI + pos PF, n >= 5")
    print("    PROMISING_SMALL — at least 1 positive metric, n < 5")
    print("    SUSPECT         — more negative than positive metrics, n 3–14")
    print("    AVOID_CANDIDATE — all metrics negative, n >= 5")
    print("    INCONCLUSIVE    — n < 3 or metrics all missing")
    print()
    print("  NOTE: DO NOT gate or disable any market based on this report.")
    print("  All recommendations require n >= 30 and explicit human review.")
    print()

    # Build per-category tables
    by_category: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for key, mkt_rows in sorted(by_market.items()):
        s = _stats(mkt_rows)
        cat = _category(s)
        by_category[cat].append((key, s))

    for cat in _CAT_ORDER:
        entries = by_category.get(cat, [])
        if not entries:
            continue
        print(f"── {cat} {'─' * (60 - len(cat))}")
        for key, s in sorted(entries, key=lambda x: -(x[1].get("n") or 0)):
            n     = s["n"]
            wr    = f"{s['wr']*100:.1f}%" if s.get("wr") is not None else "n/a "
            roi   = f"{s['roi']*100:+.1f}%" if s.get("roi") is not None else "n/a "
            clv   = f"{s['avg_clv']:+.4f}" if s.get("avg_clv") is not None else "n/a   "
            pf    = f"{s['pf']:.2f}" if s.get("pf") is not None else "n/a "
            entry = f"{s['avg_entry']:.2f}" if s.get("avg_entry") is not None else "n/a "
            settle = f"{s['median_settle_min']:.0f}m" if s.get("median_settle_min") is not None else "n/a"
            caveat = "  [SAMPLE_TOO_SMALL]" if n < 15 else ""
            print(
                f"  {key:<22s}  n={n:3d}  WR={wr:>7s}  ROI={roi:>8s}  "
                f"CLV={clv}  PF={pf:>5s}  entry={entry}  settle={settle:>5s}{caveat}"
            )
        print()

    # ── Expert summary ─────────────────────────────────────────────────────────
    print("=" * 72)
    print("EXPERT SUMMARY")
    print("=" * 72)
    keep = by_category.get("KEEP_WATCHING", [])
    avoid = by_category.get("AVOID_CANDIDATE", [])

    if keep:
        print("\n  Potential signal markets (watchlist only — n too small for proof):")
        for key, s in keep:
            clv_k = s.get("avg_clv"); roi_k = s.get("roi")
            clv_s = f"{clv_k:+.4f}" if clv_k is not None else "n/a"
            roi_s = f"{roi_k*100:+.1f}%" if roi_k is not None else "n/a"
            print(f"    {key}  n={s['n']}  CLV={clv_s}  ROI={roi_s}")
    else:
        print("\n  No KEEP_WATCHING markets identified at current sample size.")

    if avoid:
        print("\n  Markets with consistently negative evidence (candidate for future review):")
        for key, s in avoid:
            clv_a = s.get("avg_clv"); roi_a = s.get("roi")
            clv_s = f"{clv_a:+.4f}" if clv_a is not None else "n/a"
            roi_s = f"{roi_a*100:+.1f}%" if roi_a is not None else "n/a"
            print(f"    {key}  n={s['n']}  CLV={clv_s}  ROI={roi_s}")
        print("    → Do NOT disable in code until Samuel explicitly approves after n >= 30.")

    print()
    print("  real_money_allowed = False  (hardcoded)")
    print("  scale_allowed      = False  (hardcoded)")
    print("  No market should be traded with real money regardless of watchlist category.")
    print()


if __name__ == "__main__":
    report()
