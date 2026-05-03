#!/usr/bin/env python3
"""
tools/candidate_edge_pocket_report.py
--------------------------------------
Read-only candidate edge pocket analysis.

Identifies possible "edge pockets" by grouping clean_settled trades
by edge bucket, confidence, entry price, market type, and spread.
Computes WR / ROI / CLV / PF per pocket with strict sample-size warnings.

This is RESEARCH ONLY. Do NOT change strategy or thresholds based on this.
All buckets with n < 15 are flagged SAMPLE_TOO_SMALL and cannot be used
as proof of positive or negative edge.

Never writes, modifies, or settles anything.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRADES_LOG  = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG  = ROOT / "logs" / "execution_funnel.jsonl"
PROOF_WARN  = 15    # minimum n to report without SAMPLE_TOO_SMALL warning
PROOF_MIN   = 30    # minimum n to draw any strategic conclusion


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _get_clean_settled(all_rows: list[dict]) -> list[dict]:
    try:
        from tools.performance_report import (
            build_terminal_key_sets, classify_settled_records,
        )
        settled_keys, forced_keys, void_keys = build_terminal_key_sets(all_rows)
        clean, _ = classify_settled_records(all_rows, settled_keys, forced_keys, void_keys)
        return clean
    except Exception:
        return [r for r in all_rows if r.get("status") == "SETTLED"]


# ── Classification helpers ────────────────────────────────────────────────────

def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _edge_value(r: dict) -> float | None:
    v = _as_float(r.get("risk_edge"))
    return v if v is not None else _as_float(r.get("edge"))


def _edge_bucket(r: dict) -> str:
    e = _edge_value(r)
    if e is None:
        return "unknown"
    if e < 0.00:
        return "<0.00"
    if e < 0.03:
        return "0.00-0.029"
    if e < 0.05:
        return "0.03-0.049"
    if e < 0.08:
        return "0.05-0.079"
    return ">=0.08"


def _conf_bucket(r: dict) -> str:
    c = _as_float(r.get("confidence"))
    if c is None:
        return "unknown"
    if c < 0.60:
        return "<0.60"
    if c < 0.70:
        return "0.60-0.69"
    if c < 0.80:
        return "0.70-0.79"
    if c < 0.90:
        return "0.80-0.89"
    return ">=0.90"


def _entry_bucket(r: dict) -> str:
    p = _as_float(r.get("entry_price"))
    if p is None:
        return "unknown"
    if p < 0.40:
        return "<0.40"
    if p < 0.60:
        return "0.40-0.59"
    if p < 0.80:
        return "0.60-0.79"
    return ">=0.80"


def _horizon(r: dict) -> str:
    ticker = str(r.get("ticker") or "").upper()
    if "15M" in ticker:
        return "15M_INTRADAY"
    if any(t in ticker for t in ("BTCD", "ETHD", "XRPD", "DOGED", "SOLD")):
        return "DAILY_CRYPTO"
    if "MINY" in ticker or "27JAN" in ticker:
        return "LONG_TERM"
    return "OTHER"


def _market_prefix(r: dict) -> str:
    ticker = str(r.get("ticker") or "")
    if ticker.startswith("KXBTCD"):
        return "KXBTCD (BTC daily)"
    if ticker.startswith("KXETHD"):
        return "KXETHD (ETH daily)"
    if ticker.startswith("KXBTC15M") or ticker.startswith("KXBTCMINY15M"):
        return "BTC15M"
    if ticker.startswith("KXETH15M"):
        return "ETH15M"
    if ticker.startswith("KXXRP"):
        return "XRP"
    if ticker.startswith("KXSOL"):
        return "SOL"
    if ticker.startswith("KXDOGE"):
        return "DOGE"
    if "MINY" in ticker:
        return "LONG_TERM"
    return "OTHER"


def _spread_bucket(r: dict) -> str:
    # Try spread field first (Phase 6H added this to funnel; paper_trades may have it too)
    s = _as_float(r.get("spread"))
    if s is None:
        yb = _as_float(r.get("yes_bid"))
        ya = _as_float(r.get("yes_ask"))
        if yb is not None and ya is not None:
            s = round(ya - yb, 4)
    if s is None:
        return "unknown"
    if s <= 0.01:
        return "<=0.01"
    if s <= 0.03:
        return "0.01-0.03"
    if s <= 0.05:
        return "0.03-0.05"
    return ">0.05"


def _is_modern(r: dict) -> bool:
    return (
        r.get("risk_edge") is not None
        and r.get("model_probability") is not None
        and any(r.get(k) is not None for k in
                ("yes_bid", "yes_ask", "no_bid", "no_ask", "price_yes", "price_no"))
    )


# ── Metrics ───────────────────────────────────────────────────────────────────

def _get_pnl(r: dict) -> float:
    try:
        from tools.performance_report import get_pnl
        return get_pnl(r)
    except Exception:
        return _as_float(r.get("pnl")) or 0.0


def _get_size(r: dict) -> float:
    try:
        from tools.performance_report import get_size
        return get_size(r)
    except Exception:
        return _as_float(r.get("size")) or 5.0


def _get_clv(r: dict) -> float | None:
    try:
        from tools.performance_report import get_clv
        return get_clv(r)
    except Exception:
        return _as_float(r.get("clv"))


def _metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    wins   = [r for r in rows if _get_pnl(r) > 0]
    losses = [r for r in rows if _get_pnl(r) < 0]
    pnl    = sum(_get_pnl(r) for r in rows)
    wagered = sum(_get_size(r) for r in rows)
    clv_vals = [v for v in (_get_clv(r) for r in rows) if v is not None]
    avg_clv = round(sum(clv_vals) / len(clv_vals), 4) if clv_vals else None
    roi = round(pnl / wagered, 4) if wagered else None
    win_rate = round(len(wins) / (len(wins) + len(losses)), 3) if (wins or losses) else None
    gross_wins  = sum(_get_pnl(r) for r in wins)
    gross_loss  = sum(_get_pnl(r) for r in losses)
    pf = round(gross_wins / abs(gross_loss), 3) if gross_wins > 0 and gross_loss < 0 else None
    avg_entry = _as_float(sum(_as_float(r.get("entry_price")) or 0 for r in rows) / len(rows))

    flags = []
    if len(rows) < PROOF_WARN:
        flags.append("SAMPLE_TOO_SMALL")
    if avg_clv is not None and avg_clv > 0 and len(rows) >= 3:
        flags.append("POSITIVE_CLV")
    if avg_clv is not None and avg_clv < -0.05:
        flags.append("NEGATIVE_CLV")
    if roi is not None and roi > 0 and len(rows) >= PROOF_MIN:
        flags.append("POSITIVE_ROI_PROVEN")
    elif roi is not None and roi > 0:
        flags.append("POSITIVE_ROI_UNPROVEN")
    elif roi is not None and roi < 0:
        flags.append("NEGATIVE_ROI")
    if pf is not None and pf < 0.5:
        flags.append("BAD_PAYOUT")

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "wr": win_rate,
        "pnl": round(pnl, 2),
        "roi": roi,
        "pf": pf,
        "avg_clv": avg_clv,
        "avg_entry": round(avg_entry, 3) if avg_entry else None,
        "flags": flags,
    }


def _fmt_row(m: dict) -> str:
    wr  = f"{m['wr']*100:.1f}%" if m.get("wr") is not None else "n/a"
    roi = f"{m['roi']*100:+.1f}%" if m.get("roi") is not None else "n/a"
    clv = f"{m['avg_clv']:+.4f}" if m.get("avg_clv") is not None else "n/a"
    pf  = f"{m['pf']:.2f}" if m.get("pf") is not None else "n/a"
    flags = "  [" + ", ".join(m.get("flags", [])) + "]" if m.get("flags") else ""
    return (
        f"  n={m['n']:3d}  W={m['wins']:2d}  L={m['losses']:2d}  "
        f"WR={wr:>7s}  ROI={roi:>8s}  CLV={clv}  PF={pf:>5s}{flags}"
    )


def _print_group(label: str, by_key: dict, key_order: list[str] | None = None) -> None:
    print(f"\n{label}")
    print("─" * 72)
    keys = key_order if key_order else sorted(by_key.keys())
    for key in keys:
        if key not in by_key:
            continue
        m = _metrics(by_key[key])
        if m["n"] == 0:
            continue
        print(f"  {str(key):<28s} {_fmt_row(m)}")


# ── Main report ───────────────────────────────────────────────────────────────

def report() -> None:
    all_rows = _load_rows(TRADES_LOG)
    clean = _get_clean_settled(all_rows)
    modern = [r for r in clean if _is_modern(r)]
    normal = [r for r in modern if not r.get("data_collection_override") and not r.get("bootstrap_provisional")]

    print("=" * 72)
    print("CANDIDATE EDGE POCKET REPORT  (research only — read-only)")
    print("=" * 72)
    print(f"  Total rows         : {len(all_rows)}")
    print(f"  clean_settled      : {len(clean)}")
    print(f"  modern_full        : {len(modern)}")
    print(f"  normal_modern      : {len(normal)}  ← primary research sample")
    print()
    print("  SAMPLE SIZE WARNING: Any bucket with n < 15 is SAMPLE_TOO_SMALL.")
    print("  Any bucket with n < 30 cannot be used as proof of edge.")
    print("  No strategy changes should follow from this report.")
    print("  'POSITIVE_CLV' is watchlist-only — not proof until n >= 30.")
    print()

    # ── ALL clean settled ──────────────────────────────────────────────────────
    by_edge   = defaultdict(list)
    by_conf   = defaultdict(list)
    by_entry  = defaultdict(list)
    by_horiz  = defaultdict(list)
    by_market = defaultdict(list)
    by_spread = defaultdict(list)

    for r in clean:
        by_edge[_edge_bucket(r)].append(r)
        by_conf[_conf_bucket(r)].append(r)
        by_entry[_entry_bucket(r)].append(r)
        by_horiz[_horizon(r)].append(r)
        by_market[_market_prefix(r)].append(r)
        by_spread[_spread_bucket(r)].append(r)

    _print_group("EDGE BUCKET (clean_settled, all eras)",
                 by_edge, ["<0.00", "0.00-0.029", "0.03-0.049", "0.05-0.079", ">=0.08", "unknown"])

    _print_group("CONFIDENCE BUCKET (clean_settled, all eras)",
                 by_conf, ["<0.60", "0.60-0.69", "0.70-0.79", "0.80-0.89", ">=0.90", "unknown"])

    _print_group("ENTRY PRICE BUCKET (clean_settled, all eras)",
                 by_entry, ["<0.40", "0.40-0.59", "0.60-0.79", ">=0.80", "unknown"])

    _print_group("MARKET HORIZON (clean_settled, all eras)",
                 by_horiz, ["15M_INTRADAY", "DAILY_CRYPTO", "LONG_TERM", "OTHER"])

    _print_group("MARKET PREFIX (clean_settled, all eras)",
                 by_market)

    _print_group("SPREAD BUCKET (clean_settled — unknown=pre-6H rows without spread data)",
                 by_spread, ["<=0.01", "0.01-0.03", "0.03-0.05", ">0.05", "unknown"])

    # ── Modern full metadata only ──────────────────────────────────────────────
    print()
    print("=" * 72)
    print("MODERN FULL-METADATA ONLY  (19 rows — cleaner signal)")
    print("=" * 72)
    m_by_edge   = defaultdict(list)
    m_by_conf   = defaultdict(list)
    m_by_market = defaultdict(list)
    for r in modern:
        m_by_edge[_edge_bucket(r)].append(r)
        m_by_conf[_conf_bucket(r)].append(r)
        m_by_market[_market_prefix(r)].append(r)

    _print_group("EDGE BUCKET (modern_full only)",
                 m_by_edge, ["<0.00", "0.03-0.049", "0.05-0.079", ">=0.08", "unknown"])
    _print_group("CONFIDENCE BUCKET (modern_full only)",
                 m_by_conf, ["<0.60", "0.60-0.69", "0.70-0.79", "0.80-0.89", ">=0.90", "unknown"])
    _print_group("MARKET PREFIX (modern_full only)",
                 m_by_market)

    # ── Normal modern only ─────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print(f"NORMAL_MODERN ONLY  ({len(normal)} rows — proof-eligible subset)")
    print("=" * 72)
    if len(normal) < 3:
        print(f"  Insufficient data: {len(normal)} rows. At least 3 required for any table.")
    else:
        nm_by_market = defaultdict(list)
        nm_by_edge   = defaultdict(list)
        for r in normal:
            nm_by_market[_market_prefix(r)].append(r)
            nm_by_edge[_edge_bucket(r)].append(r)
        _print_group("EDGE BUCKET (normal_modern only)", nm_by_edge,
                     ["<0.00", "0.03-0.049", "0.05-0.079", ">=0.08", "unknown"])
        _print_group("MARKET PREFIX (normal_modern only)", nm_by_market)

    # ── Summary hypotheses ─────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("CANDIDATE POCKETS  (watchlist only — do not trade more based on this)")
    print("=" * 72)
    candidates = []
    bad_buckets = []
    for label, rows_in_bucket in [
        ("KXBTCD (BTC daily)",  by_market.get("KXBTCD (BTC daily)", [])),
        ("edge 0.03-0.049",     by_edge.get("0.03-0.049", [])),
        ("conf 0.80-0.89",      by_conf.get("0.80-0.89", [])),
        ("spread <=0.01",       by_spread.get("<=0.01", [])),
    ]:
        if not rows_in_bucket:
            continue
        m = _metrics(rows_in_bucket)
        clv = m.get("avg_clv")
        roi = m.get("roi")
        if clv is not None and clv > 0:
            candidates.append((label, m))
        elif roi is not None and roi < -0.20:
            bad_buckets.append((label, m))

    if candidates:
        print("\n  Pockets with positive CLV (watchlist — not proof):")
        for label, m in candidates:
            print(f"    {label:<30s} n={m['n']:3d}  CLV={m['avg_clv']:+.4f}  ROI={m['roi']*100:+.1f}%  {'[SAMPLE_TOO_SMALL]' if m['n'] < PROOF_WARN else ''}")
    else:
        print("\n  No pockets with positive CLV found in clean_settled data.")

    if bad_buckets:
        print("\n  Buckets with ROI < -20% (candidate for future filter review):")
        for label, m in bad_buckets:
            raw_clv = m.get("avg_clv")
            clv_str = f"{raw_clv:+.4f}" if raw_clv is not None else "n/a"
            print(f"    {label:<30s} n={m['n']:3d}  CLV={clv_str:>8s}  ROI={m['roi']*100:+.1f}%")

    print()
    print("  FORBIDDEN: Do not patch strategy, thresholds, or config based on this report.")
    print("  All positive pockets require n >= 30 AND CLV > 0 AND ROI > 0 AND PF > 1.10")
    print("  before any gate review. Current system is in data-collection phase only.")
    print()


if __name__ == "__main__":
    report()
