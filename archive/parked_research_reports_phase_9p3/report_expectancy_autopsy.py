#!/usr/bin/env python3
"""
tools/report_expectancy_autopsy.py
-----------------------------------
Read-only expectancy autopsy report.
Finds exactly why the system is mathematically losing.

Does NOT modify any config, threshold, execution path, or log file.

Data sources (printed at runtime):
  logs/paper_trades.jsonl
  data/edge_profile.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import (
    build_terminal_key_sets,
    classify_open_records,
    classify_settled_records,
    get_clv,
    get_pnl,
    get_size,
    is_side_coverage_record,
    load_trades,
)
from tools.clean_truth_report import is_time_exit, row_quality_group

TRADES_LOG   = ROOT / "logs" / "paper_trades.jsonl"
PROFILE_PATH = ROOT / "data" / "edge_profile.json"

SEP  = "=" * 80
SEP2 = "-" * 80


# ─── primitive helpers ───────────────────────────────────────────────────────

def _af(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _avg(vals: List[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return (s[mid - 1] + s[mid]) / 2.0 if n % 2 == 0 else s[mid]


def _pct(v: Optional[float], d: int = 1) -> str:
    return f"{v * 100:+.{d}f}%" if v is not None else "n/a"


def _num(v: Optional[float], d: int = 4) -> str:
    return f"{v:+.{d}f}" if v is not None else "n/a"


def _ratio(v: Optional[float], d: int = 2) -> str:
    return f"{v:.{d}f}" if v is not None else "n/a"


def _money(v: Optional[float]) -> str:
    return f"${v:+.2f}" if v is not None else "n/a"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── field extractors ────────────────────────────────────────────────────────

def get_model_prob(r: dict) -> Optional[float]:
    """Priority: model_probability → original_confidence → confidence."""
    for field in ("model_probability", "original_confidence", "confidence"):
        v = _af(r.get(field))
        if v is not None:
            return v
    return None


def get_spread(r: dict) -> Optional[float]:
    for field in ("market_spread", "spread"):
        v = _af(r.get(field))
        if v is not None:
            return v
    return None


def get_volume(r: dict) -> Optional[float]:
    for field in ("market_volume", "volume", "liquidity"):
        v = _af(r.get(field))
        if v is not None:
            return v
    return None


# ─── bucket functions ────────────────────────────────────────────────────────

def ticker_prefix(ticker: str) -> str:
    """Extract leading alpha prefix, stopping before the first digit or date."""
    t = str(ticker or "").upper().strip()
    if not t:
        return "UNKNOWN"
    m = re.match(r"^([A-Z]+(?:-[A-Z]+)*)", t)
    return m.group(1) if m else t[:8]


def entry_price_bucket(ep: Optional[float]) -> str:
    if ep is None:
        return "unknown"
    if ep < 0.20:  return "0.00-0.20"
    if ep < 0.35:  return "0.20-0.35"
    if ep < 0.50:  return "0.35-0.50"
    if ep < 0.60:  return "0.50-0.60"
    if ep < 0.70:  return "0.60-0.70"
    if ep < 0.80:  return "0.70-0.80"
    return                "0.80-1.00"


ENTRY_PRICE_ORDER = [
    "0.00-0.20", "0.20-0.35", "0.35-0.50",
    "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-1.00", "unknown",
]


def prob_bucket(p: Optional[float]) -> str:
    if p is None:
        return "unknown"
    if p < 0.50:  return "<0.50"
    if p < 0.60:  return "0.50-0.60"
    if p < 0.65:  return "0.60-0.65"
    if p < 0.70:  return "0.65-0.70"
    if p < 0.75:  return "0.70-0.75"
    if p < 0.80:  return "0.75-0.80"
    if p < 0.90:  return "0.80-0.90"
    return               "0.90-1.00"


PROB_ORDER = [
    "<0.50", "0.50-0.60", "0.60-0.65", "0.65-0.70",
    "0.70-0.75", "0.75-0.80", "0.80-0.90", "0.90-1.00", "unknown",
]


def edge_bucket_fn(e: Optional[float]) -> str:
    if e is None:
        return "unknown"
    if e < 0.00:  return "<0%"
    if e < 0.03:  return "0-3%"
    if e < 0.05:  return "3-5%"
    if e < 0.08:  return "5-8%"
    if e < 0.12:  return "8-12%"
    return               "12%+"


EDGE_ORDER = ["<0%", "0-3%", "3-5%", "5-8%", "8-12%", "12%+", "unknown"]


def spread_bucket(s: Optional[float]) -> str:
    if s is None:
        return "unknown"
    if s <= 0.01:  return "0-1¢"
    if s <= 0.02:  return "1-2¢"
    if s <= 0.03:  return "2-3¢"
    if s <= 0.05:  return "3-5¢"
    return               ">5¢"


SPREAD_ORDER = ["0-1¢", "1-2¢", "2-3¢", "3-5¢", ">5¢", "unknown"]


def volume_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 500:     return "<500"
    if v < 1000:    return "500-1k"
    if v < 5000:    return "1k-5k"
    if v < 20000:   return "5k-20k"
    return                 ">=20k"


VOLUME_ORDER = ["<500", "500-1k", "1k-5k", "5k-20k", ">=20k", "unknown"]


# ─── metrics ─────────────────────────────────────────────────────────────────

class Metrics:
    """Aggregate performance metrics for a group of trade records."""

    def __init__(self, rows: List[dict]):
        self.rows = rows
        self.n = len(rows)

        wins   = [r for r in rows if get_pnl(r) > 0]
        losses = [r for r in rows if get_pnl(r) < 0]
        pushes = [r for r in rows if get_pnl(r) == 0]

        self.win_count  = len(wins)
        self.loss_count = len(losses)
        self.push_count = len(pushes)
        self.total_pnl     = sum(get_pnl(r) for r in rows)
        self.total_wagered = sum(get_size(r) for r in rows)
        self.gross_wins    = sum(get_pnl(r) for r in wins)
        self.gross_losses  = sum(get_pnl(r) for r in losses)

        wl = self.win_count + self.loss_count
        self.win_rate = self.win_count / wl if wl else None
        self.roi      = self.total_pnl / self.total_wagered if self.total_wagered else None
        self.avg_win  = self.gross_wins  / self.win_count  if self.win_count  else None
        self.avg_loss = self.gross_losses / self.loss_count if self.loss_count else None
        self.avg_pnl  = self.total_pnl / self.n if self.n else None

        if self.gross_losses < 0:
            self.profit_factor: Optional[float] = self.gross_wins / abs(self.gross_losses)
        elif self.gross_wins > 0:
            self.profit_factor = float("inf")
        else:
            self.profit_factor = None

        clv_vals = [v for v in (get_clv(r) for r in rows) if v is not None]
        self.avg_clv    = _avg(clv_vals)
        self.median_clv = _median(clv_vals)
        self.clv_count  = len(clv_vals)

        prob_vals = [v for v in (get_model_prob(r) for r in rows) if v is not None]
        self.avg_prob = _avg(prob_vals)

        # Breakeven win rate and gap
        if self.avg_win is not None and self.avg_loss is not None and self.avg_loss < 0:
            al = abs(self.avg_loss)
            self.breakeven_wr: Optional[float] = al / (self.avg_win + al)
            self.wr_vs_be: Optional[float] = (self.win_rate or 0.0) - self.breakeven_wr
        else:
            self.breakeven_wr = None
            self.wr_vs_be = None

        # Calibration: realized win rate vs predicted probability
        outcomes = []
        for r in rows:
            p = get_pnl(r)
            if p > 0:
                outcomes.append(1.0)
            elif p < 0:
                outcomes.append(0.0)
        self.realized_wr = sum(outcomes) / len(outcomes) if outcomes else None
        self.calibration_gap = (
            (self.realized_wr - self.avg_prob)
            if self.realized_wr is not None and self.avg_prob is not None
            else None
        )

    def _pf_str(self) -> str:
        if self.profit_factor is None:
            return "n/a"
        if self.profit_factor == float("inf"):
            return "∞"
        return f"{self.profit_factor:.2f}"

    def verdict(self) -> str:
        if self.n < 3:
            return "TOO_SMALL"
        roi_ok = self.roi is not None and self.roi > 0
        pf_ok  = (self.profit_factor is not None
                  and (self.profit_factor == float("inf") or self.profit_factor > 1.0))
        clv_ok = self.avg_clv is None or self.avg_clv >= 0.0
        clearly_losing = (
            self.roi is not None and self.roi < 0
            and self.profit_factor is not None
            and self.profit_factor != float("inf")
            and self.profit_factor < 1.0
        )
        if roi_ok and pf_ok and clv_ok and self.n >= 5:
            return "POSSIBLE_EDGE"
        if clearly_losing:
            return "POISON"
        return "WATCHLIST"


def group_by(rows: List[dict], key_fn) -> Dict[str, List[dict]]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    return dict(groups)


def _ts_key(r: dict) -> str:
    return str(r.get("settled_at") or r.get("timestamp") or "")


def equity_curve(rows_sorted: List[dict]) -> List[float]:
    curve = [0.0]
    cum = 0.0
    for r in rows_sorted:
        cum += get_pnl(r)
        curve.append(cum)
    return curve


def max_drawdown(curve: List[float]) -> float:
    if len(curve) < 2:
        return 0.0
    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd


def worst_window(rows_sorted: List[dict], w: int) -> Optional[float]:
    if len(rows_sorted) < w:
        return None
    pnls = [get_pnl(r) for r in rows_sorted]
    return min(sum(pnls[i:i + w]) for i in range(len(pnls) - w + 1))


def longest_losing_streak(rows_sorted: List[dict]) -> Tuple[int, int, int]:
    """Returns (streak_length, start_idx, end_idx)."""
    max_len, cur_len, cur_start = 0, 0, 0
    best_start, best_end = 0, 0
    for i, r in enumerate(rows_sorted):
        if get_pnl(r) < 0:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
            if cur_len > max_len:
                max_len = cur_len
                best_start = cur_start
                best_end = i
        else:
            cur_len = 0
    return max_len, best_start, best_end


def _print_metrics_row(
    label: str,
    m: Metrics,
    show_be: bool = False,
    indent: int = 2,
) -> None:
    pad = " " * indent
    line = (
        f"{pad}{label:<22}  n={m.n:>3}  W={m.win_count:>3} L={m.loss_count:>3} P={m.push_count:>2}"
        f"  WR={_pct(m.win_rate):>7}"
        f"  ROI={_pct(m.roi):>8}"
        f"  PF={m._pf_str():>5}"
        f"  CLV={_num(m.avg_clv, 4):>9}"
        f"  PnL={_money(m.total_pnl):>9}"
    )
    if show_be and m.breakeven_wr is not None:
        line += (
            f"  BE={_pct(m.breakeven_wr):>7}"
            f"  WR-BE={_pct(m.wr_vs_be):>8}"
        )
    line += f"  [{m.verdict()}]"
    print(line)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main() -> None:
    print(SEP)
    print("EXPECTANCY AUTOPSY REPORT")
    print(f"  generated_at: {_now_utc()}")
    print()
    print(f"  Data sources:")
    print(f"    {TRADES_LOG}")
    print(f"    {PROFILE_PATH}")
    print(SEP)

    # ── load & classify ───────────────────────────────────────────────────────
    all_records = load_trades()
    if not all_records:
        print("  [ERROR] No records found in paper_trades.jsonl.")
        return

    settled_keys, forced_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted = classify_settled_records(
        all_records, settled_keys, forced_keys, void_keys,
    )
    active_opens, stale_opens = classify_open_records(all_records)

    time_exits  = [r for r in all_records if is_time_exit(r)]
    forced_other = [
        r for r in all_records
        if r.get("status") == "FORCED_CLOSE" and not is_time_exit(r)
    ]
    voided    = [r for r in all_records if r.get("status") == "VOID_LEGACY_DUPLICATE"]
    side_cov  = [r for r in all_records if is_side_coverage_record(r)]

    # Subsets of clean_settled
    modern_rows = [r for r in clean_settled if row_quality_group(r) == "MODERN_FULL_METADATA"]
    normal_rows = [
        r for r in clean_settled
        if not r.get("data_collection_override") and not r.get("bootstrap_provisional")
    ]
    dc_rows  = [r for r in clean_settled if r.get("data_collection_override")]
    bp_rows  = [r for r in clean_settled if r.get("bootstrap_provisional")]
    era_rows = [r for r in clean_settled if r.get("bootstrap_era_council_allow")]
    legacy_rows = [r for r in clean_settled if row_quality_group(r) == "LEGACY_EDGE_ONLY"]

    # Chronological order for equity curve / streaks
    cs_sorted = sorted(clean_settled, key=_ts_key)

    # ── SECTION 1: SUMMARY ───────────────────────────────────────────────────
    print()
    print("SECTION 1: SYSTEM AUTOPSY SUMMARY")
    print(SEP2)

    raw_settled  = sum(1 for r in all_records if r.get("status") == "SETTLED")
    raw_open     = sum(1 for r in all_records if r.get("status") == "OPEN")
    raw_forced   = sum(1 for r in all_records if r.get("status") == "FORCED_CLOSE")
    raw_void     = sum(1 for r in all_records if r.get("status") == "VOID_LEGACY_DUPLICATE")
    raw_no_status = sum(1 for r in all_records if "status" not in r)

    print(f"  Total records in log:          {len(all_records)}")
    print(f"    SETTLED (raw):               {raw_settled}")
    print(f"      clean (used in stats):     {len(clean_settled)}")
    print(f"      conflicted (excluded):     {len(conflicted)}")
    print(f"    OPEN (raw):                  {raw_open}")
    print(f"      active:                    {len(active_opens)}")
    print(f"      stale/ghost:               {len(stale_opens)}")
    print(f"    FORCED_CLOSE:                {raw_forced}")
    print(f"      TIME_EXIT:                 {len(time_exits)}")
    print(f"      other forced:              {len(forced_other)}")
    print(f"    VOID_LEGACY_DUPLICATE:       {raw_void}")
    print(f"    no-status (old format):      {raw_no_status}")
    print(f"    side_coverage (excluded):    {len(side_cov)}")
    print()
    print(f"  Clean-settled breakdown:")
    print(f"    normal (incl era_allow):     {len(normal_rows)}")
    print(f"    data_collection_override:    {len(dc_rows)}  [excluded from normal proof]")
    print(f"    bootstrap_provisional:       {len(bp_rows)}  [excluded from normal proof]")
    print(f"    bootstrap_era_allow:         {len(era_rows)}  [counts as normal]")
    print(f"    legacy_edge_only:            {len(legacy_rows)}  [quarantined]")
    print(f"    modern_full_metadata:        {len(modern_rows)}")
    print()

    m_all = Metrics(clean_settled)
    print(f"  OVERALL PERFORMANCE (clean settled n={m_all.n}):")
    print(f"    Wins / Losses / Pushes:      {m_all.win_count} / {m_all.loss_count} / {m_all.push_count}")
    print(f"    Win rate:                    {_pct(m_all.win_rate)}")
    print(f"    Total PnL:                   {_money(m_all.total_pnl)}")
    print(f"    Total wagered:               ${m_all.total_wagered:.2f}")
    print(f"    ROI:                         {_pct(m_all.roi)}")
    print(f"    Avg PnL per trade:           {_money(m_all.avg_pnl)}")
    pf_s = m_all._pf_str()
    print(f"    Profit factor:               {pf_s}")
    print(f"    Avg win:                     {_money(m_all.avg_win)}")
    print(f"    Avg loss:                    {_money(m_all.avg_loss)}")
    if m_all.breakeven_wr is not None:
        print(f"    Breakeven win rate:          {_pct(m_all.breakeven_wr)}")
        print(f"    WR vs breakeven:             {_pct(m_all.wr_vs_be)}")
    print(f"    Avg CLV:                     {_num(m_all.avg_clv)}")
    print(f"    Median CLV:                  {_num(m_all.median_clv)}")
    print(f"    CLV available for:           {m_all.clv_count}/{m_all.n} rows")

    curve    = equity_curve(cs_sorted)
    max_dd   = max_drawdown(curve)
    streak_n, streak_s, streak_e = longest_losing_streak(cs_sorted)
    print()
    print(f"    Max drawdown (settled):      ${max_dd:.2f}")
    print(f"    Longest losing streak:       {streak_n}")
    for w in (5, 10, 20):
        wv = worst_window(cs_sorted, w)
        if wv is not None:
            print(f"    Worst {w:>2}-trade window:        {_money(wv)}")

    # ── SECTION 2: PROFITABILITY GATES ───────────────────────────────────────
    print()
    print("SECTION 2: PROFITABILITY GATES")
    print(SEP2)

    m_mod = Metrics(modern_rows)

    def gate(ok: bool, lbl: str, detail: str) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {lbl:<45}  {detail}")

    roi_ok  = m_mod.roi is not None and m_mod.roi > 0
    pf_ok   = (m_mod.profit_factor is not None
               and (m_mod.profit_factor == float("inf") or m_mod.profit_factor > 1.10))
    clv_ok  = m_mod.avg_clv is not None and m_mod.avg_clv > 0
    n_ok    = len(modern_rows) >= 30
    cs_ok   = m_all.roi is not None and m_all.roi > 0
    dd_ok   = max_dd < 50.0

    gate(roi_ok, "Modern ROI > 0%",
         f"current {_pct(m_mod.roi)} (need > 0%)")
    gate(pf_ok,  "Modern profit factor > 1.10",
         f"current {m_mod._pf_str()} (need > 1.10)")
    gate(clv_ok, "Modern avg CLV > 0",
         f"current {_num(m_mod.avg_clv)} (need > 0)")
    gate(n_ok,   "Modern full-metadata >= 30",
         f"current {len(modern_rows)} (need >= 30)")
    gate(cs_ok,  "Clean-settled ROI > 0%",
         f"current {_pct(m_all.roi)} (need > 0%)")
    gate(dd_ok,  "Max drawdown < $50",
         f"current ${max_dd:.2f}")

    gates_pass = sum([roi_ok, pf_ok, clv_ok, n_ok, cs_ok])
    print()
    if gates_pass == 0:
        print("  VERDICT: ZERO performance gates pass. System is mathematically losing on all metrics.")
    elif gates_pass == 5:
        print("  VERDICT: All gates pass — review drawdown and streak before considering scaling.")
    else:
        print(f"  VERDICT: {gates_pass}/5 gates pass. WATCHLIST only. Do not scale.")

    # ── SECTION 3: PREFIX PERFORMANCE ────────────────────────────────────────
    print()
    print("SECTION 3: PREFIX PERFORMANCE")
    print(SEP2)
    print("  Metrics from clean SETTLED rows only. TE = time exits (shown for context).")
    print()

    # Build prefix maps
    all_for_prefix = clean_settled + time_exits
    prefix_all    = group_by(all_for_prefix, lambda r: ticker_prefix(r.get("ticker", "")))
    prefix_settled_map: Dict[str, List] = {}
    prefix_te_map:      Dict[str, List] = {}
    for pfx, rows in prefix_all.items():
        prefix_settled_map[pfx] = [r for r in rows if r.get("status") == "SETTLED"]
        prefix_te_map[pfx]      = [r for r in rows if is_time_exit(r)]

    sorted_prefixes = sorted(
        prefix_all.keys(),
        key=lambda p: len(prefix_settled_map.get(p, [])),
        reverse=True,
    )

    hdr = (
        f"  {'Prefix':<14}  {'total':>5}  {'settled':>7}  "
        f"{'W':>3}  {'L':>3}  {'TE':>3}  "
        f"{'WR':>7}  {'ROI':>8}  {'PF':>5}  "
        f"{'CLV':>9}  {'PnL':>9}  Verdict"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for pfx in sorted_prefixes:
        settled_r = prefix_settled_map.get(pfx, [])
        te_r      = prefix_te_map.get(pfx, [])
        m = Metrics(settled_r) if settled_r else Metrics([])
        total_r = prefix_all.get(pfx, [])
        print(
            f"  {pfx:<14}  {len(total_r):>5}  {len(settled_r):>7}  "
            f"{m.win_count:>3}  {m.loss_count:>3}  {len(te_r):>3}  "
            f"{_pct(m.win_rate):>7}  {_pct(m.roi):>8}  {m._pf_str():>5}  "
            f"{_num(m.avg_clv, 4):>9}  {_money(m.total_pnl):>9}  {m.verdict()}"
        )

    # ── SECTION 4: ENTRY PRICE BUCKETS ───────────────────────────────────────
    print()
    print("SECTION 4: ENTRY PRICE BUCKETS")
    print(SEP2)
    print("  entry_price = yes_ask for BET_YES, no_ask for BET_NO.")
    print("  Breakeven WR = abs(avg_loss) / (avg_win + abs(avg_loss)).")
    print("  WR-BE < 0 means the system must win more often but does not.")
    print()

    ep_groups = group_by(clean_settled, lambda r: entry_price_bucket(_af(r.get("entry_price"))))
    hdr = (
        f"  {'Bucket':<12}  {'n':>4}  {'W':>3}  {'L':>3}  "
        f"{'WR':>7}  {'BE_WR':>7}  {'WR-BE':>8}  "
        f"{'ROI':>8}  {'PF':>5}  {'CLV':>9}  {'PnL':>9}  Verdict"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for bkt in ENTRY_PRICE_ORDER:
        rows = ep_groups.get(bkt, [])
        if not rows:
            continue
        m = Metrics(rows)
        print(
            f"  {bkt:<12}  {m.n:>4}  {m.win_count:>3}  {m.loss_count:>3}  "
            f"{_pct(m.win_rate):>7}  {_pct(m.breakeven_wr):>7}  {_pct(m.wr_vs_be):>8}  "
            f"{_pct(m.roi):>8}  {m._pf_str():>5}  {_num(m.avg_clv, 4):>9}  "
            f"{_money(m.total_pnl):>9}  {m.verdict()}"
        )

    # ── SECTION 5: CALIBRATION AUTOPSY ───────────────────────────────────────
    print()
    print("SECTION 5: CALIBRATION AUTOPSY")
    print(SEP2)
    print("  Predicted prob = model_probability → original_confidence → confidence.")
    print("  Calibration gap = realized_WR - avg_predicted. Negative = overconfident.")
    print()

    missing_prob = sum(1 for r in clean_settled if get_model_prob(r) is None)
    if missing_prob:
        print(f"  [NOTE] {missing_prob}/{len(clean_settled)} rows missing model probability.")
        print()

    prob_groups = group_by(clean_settled, lambda r: prob_bucket(get_model_prob(r)))
    hdr = (
        f"  {'Bucket':<12}  {'n':>4}  {'AvgPred':>8}  {'RealWR':>7}  {'CalGap':>8}  "
        f"{'ROI':>8}  {'PF':>5}  {'CLV':>9}  Verdict"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for bkt in PROB_ORDER:
        rows = prob_groups.get(bkt, [])
        if not rows:
            continue
        m = Metrics(rows)
        print(
            f"  {bkt:<12}  {m.n:>4}  {_pct(m.avg_prob, 1):>8}  {_pct(m.realized_wr, 1):>7}"
            f"  {_pct(m.calibration_gap, 1):>8}  {_pct(m.roi):>8}  {m._pf_str():>5}"
            f"  {_num(m.avg_clv, 4):>9}  {m.verdict()}"
        )

    # ── SECTION 6: EDGE BUCKET AUTOPSY ───────────────────────────────────────
    print()
    print("SECTION 6: EDGE BUCKET AUTOPSY")
    print(SEP2)

    edge_columns = [
        ("original_edge",  "original_edge  (pre-council, used for danger guard)"),
        ("adjusted_edge",  "adjusted_edge  (post-council, used for bet sizing)"),
        ("risk_edge",      "risk_edge      (sent to risk manager)"),
    ]
    for col, label in edge_columns:
        present = sum(1 for r in clean_settled if _af(r.get(col)) is not None)
        if present == 0:
            print(f"  [MISSING] {col} — not present in any clean settled row.")
            continue
        print()
        print(f"  By {label}  ({present}/{len(clean_settled)} rows have value):")
        print(
            f"  {'Bucket':<8}  {'n':>4}  {'W':>3}  {'L':>3}  "
            f"{'WR':>7}  {'ROI':>8}  {'PF':>5}  {'CLV':>9}  Verdict"
        )
        grps = group_by(clean_settled, lambda r, c=col: edge_bucket_fn(_af(r.get(c))))
        for bkt in EDGE_ORDER:
            rows = grps.get(bkt, [])
            if not rows:
                continue
            m = Metrics(rows)
            print(
                f"  {bkt:<8}  {m.n:>4}  {m.win_count:>3}  {m.loss_count:>3}  "
                f"{_pct(m.win_rate):>7}  {_pct(m.roi):>8}  {m._pf_str():>5}"
                f"  {_num(m.avg_clv, 4):>9}  {m.verdict()}"
            )

    # ── SECTION 7: CLV AUTOPSY ────────────────────────────────────────────────
    print()
    print("SECTION 7: CLV AUTOPSY")
    print(SEP2)

    clv_present = sum(1 for r in clean_settled if get_clv(r) is not None)
    if clv_present == 0:
        print("  [MISSING] No CLV values. entry_price or exit_price absent in all records.")
    else:
        pos_clv  = [r for r in clean_settled if (get_clv(r) or 0.0) > 0]
        neg_clv  = [r for r in clean_settled if (get_clv(r) or 0.0) < 0]
        zero_clv = [r for r in clean_settled if get_clv(r) is not None and get_clv(r) == 0.0]
        no_clv   = [r for r in clean_settled if get_clv(r) is None]

        print(f"  CLV available for {clv_present}/{len(clean_settled)} rows.")
        print(f"  CLV > 0: {len(pos_clv)}  |  CLV < 0: {len(neg_clv)}  |  CLV = 0: {len(zero_clv)}  |  missing: {len(no_clv)}")
        print()

        if pos_clv:
            _print_metrics_row("CLV > 0 group", Metrics(pos_clv))
        if neg_clv:
            _print_metrics_row("CLV < 0 group", Metrics(neg_clv))

        # CLV by prefix
        print()
        print("  CLV by ticker prefix:")
        for pfx in sorted_prefixes:
            rows_p = [r for r in prefix_settled_map.get(pfx, []) if get_clv(r) is not None]
            if not rows_p:
                continue
            clvs = [get_clv(r) for r in rows_p]   # type: ignore[arg-type]
            avg_c = _avg(clvs)   # type: ignore[arg-type]
            med_c = _median(clvs)  # type: ignore[arg-type]
            pct_pos = sum(1 for c in clvs if c > 0) / len(clvs) * 100
            print(
                f"    {pfx:<14}  n={len(clvs):>3}  avg={_num(avg_c, 4):>9}"
                f"  median={_num(med_c, 4):>9}  pct_positive={pct_pos:.0f}%"
            )

        # CLV by entry price bucket
        print()
        print("  CLV by entry price bucket:")
        for bkt in ENTRY_PRICE_ORDER:
            rows_ep = ep_groups.get(bkt, [])
            clvs = [get_clv(r) for r in rows_ep if get_clv(r) is not None]
            if not clvs:
                continue
            avg_c = _avg(clvs)   # type: ignore[arg-type]
            med_c = _median(clvs)  # type: ignore[arg-type]
            worst5 = [round(c, 4) for c in sorted(clvs)[:5]]
            print(
                f"    {bkt:<12}  n={len(clvs):>3}  avg={_num(avg_c, 4):>9}"
                f"  median={_num(med_c, 4):>9}  worst5={worst5}"
            )

        # CLV by probability bucket
        print()
        print("  CLV by model probability bucket:")
        for bkt in PROB_ORDER:
            rows_pb = prob_groups.get(bkt, [])
            clvs = [get_clv(r) for r in rows_pb if get_clv(r) is not None]
            if not clvs:
                continue
            avg_c = _avg(clvs)   # type: ignore[arg-type]
            med_c = _median(clvs)  # type: ignore[arg-type]
            print(
                f"    {bkt:<12}  n={len(clvs):>3}  avg={_num(avg_c, 4):>9}"
                f"  median={_num(med_c, 4):>9}"
            )

        # Worst 10 CLV trades
        all_clv_rows = sorted(
            [(get_clv(r), r) for r in clean_settled if get_clv(r) is not None],
            key=lambda x: x[0],  # type: ignore[arg-type]
        )
        if all_clv_rows:
            print()
            print("  Worst 10 CLV trades:")
            for clv_val, r in all_clv_rows[:10]:
                ep_b   = entry_price_bucket(_af(r.get("entry_price")))
                pb_b   = prob_bucket(get_model_prob(r))
                action = r.get("action", "?")
                result = r.get("result", "?")
                print(
                    f"    {r.get('ticker', '?'):<38}  {action:<7}  "
                    f"CLV={_num(clv_val, 4):>9}  entry={ep_b}  prob={pb_b}  result={result}"
                )

    # ── SECTION 8: MARKET QUALITY AUTOPSY ────────────────────────────────────
    print()
    print("SECTION 8: MARKET QUALITY AUTOPSY")
    print(SEP2)

    spread_pres = sum(1 for r in clean_settled if get_spread(r) is not None)
    vol_pres    = sum(1 for r in clean_settled if get_volume(r) is not None)
    mq_pres     = sum(1 for r in clean_settled if r.get("market_quality_passed") is not None)

    print(
        f"  Field coverage: spread={spread_pres}/{len(clean_settled)}"
        f"  volume={vol_pres}/{len(clean_settled)}"
        f"  market_quality_passed={mq_pres}/{len(clean_settled)}"
    )
    print()

    if spread_pres > 0:
        print("  Performance by spread bucket:")
        print(
            f"  {'Bucket':<8}  {'n':>4}  {'WR':>7}  {'ROI':>8}"
            f"  {'PF':>5}  {'CLV':>9}  Verdict"
        )
        sg = group_by(clean_settled, lambda r: spread_bucket(get_spread(r)))
        for bkt in SPREAD_ORDER:
            rows = sg.get(bkt, [])
            if not rows:
                continue
            m = Metrics(rows)
            print(
                f"  {bkt:<8}  {m.n:>4}  {_pct(m.win_rate):>7}  {_pct(m.roi):>8}"
                f"  {m._pf_str():>5}  {_num(m.avg_clv, 4):>9}  {m.verdict()}"
            )
        print()

    if vol_pres > 0:
        print("  Performance by volume bucket (market_volume → volume → liquidity):")
        print(
            f"  {'Bucket':<8}  {'n':>4}  {'WR':>7}  {'ROI':>8}"
            f"  {'PF':>5}  {'CLV':>9}  Verdict"
        )
        vg = group_by(clean_settled, lambda r: volume_bucket(get_volume(r)))
        for bkt in VOLUME_ORDER:
            rows = vg.get(bkt, [])
            if not rows:
                continue
            m = Metrics(rows)
            print(
                f"  {bkt:<8}  {m.n:>4}  {_pct(m.win_rate):>7}  {_pct(m.roi):>8}"
                f"  {m._pf_str():>5}  {_num(m.avg_clv, 4):>9}  {m.verdict()}"
            )
        print()

    if mq_pres > 0:
        print("  Performance by market_quality_passed:")
        mqg = group_by(clean_settled, lambda r: str(r.get("market_quality_passed", "unknown")))
        for mq_val, rows in sorted(mqg.items()):
            m = Metrics(rows)
            print(
                f"  market_quality_passed={mq_val:<8}  n={m.n:>3}  "
                f"WR={_pct(m.win_rate):>7}  ROI={_pct(m.roi):>8}"
                f"  PF={m._pf_str():>5}  CLV={_num(m.avg_clv, 4):>9}"
            )

    if spread_pres == 0 and vol_pres == 0:
        print("  [MISSING] No spread or volume fields found in clean settled rows.")

    # ── SECTION 9: TIME EXIT CONTAMINATION ───────────────────────────────────
    print()
    print("SECTION 9: TIME EXIT CONTAMINATION")
    print(SEP2)

    print(f"  TIME_EXIT records (FORCED_CLOSE + TIME_EXIT result): {len(time_exits)}")
    if time_exits:
        m_te = Metrics(time_exits)
        print(
            f"  TIME_EXIT PnL total:   {_money(m_te.total_pnl)}"
            f"  ROI: {_pct(m_te.roi)}"
            f"  WR: {_pct(m_te.win_rate)}"
            f"  PF: {m_te._pf_str()}"
            f"  avg CLV: {_num(m_te.avg_clv)}"
        )
        print()

    m_with_te = Metrics(clean_settled + time_exits)
    print(f"  Performance comparison (clean settled = {len(clean_settled)}, w/ TIME_EXIT = {len(clean_settled + time_exits)}):")
    print(f"  {'Metric':<22}  {'With TE':>10}  {'Without TE':>12}")
    print(f"  {'-'*22}  {'-'*10}  {'-'*12}")
    print(f"  {'n':<22}  {m_with_te.n:>10}  {m_all.n:>12}")
    print(f"  {'Win rate':<22}  {_pct(m_with_te.win_rate):>10}  {_pct(m_all.win_rate):>12}")
    print(f"  {'ROI':<22}  {_pct(m_with_te.roi):>10}  {_pct(m_all.roi):>12}")
    print(f"  {'Profit factor':<22}  {m_with_te._pf_str():>10}  {m_all._pf_str():>12}")
    print(f"  {'Avg CLV':<22}  {_num(m_with_te.avg_clv):>10}  {_num(m_all.avg_clv):>12}")
    print(f"  {'Total PnL':<22}  {_money(m_with_te.total_pnl):>10}  {_money(m_all.total_pnl):>12}")

    if time_exits and m_te.total_pnl < 0:
        te_pct = abs(m_te.total_pnl) / (abs(m_all.total_pnl) + 0.01) * 100
        if te_pct > 10:
            print()
            print(f"  [WARNING] TIME_EXIT PnL represents {te_pct:.0f}% of clean-settled loss magnitude.")
            print("            Time exits are not event-outcome proof but represent real economic loss.")
    elif time_exits and m_te.total_pnl > 0:
        print()
        print("  [NOTE] TIME_EXIT trades are net positive — closing at a price above entry.")

    # ── SECTION 10: HIGH-EDGE DANGER ─────────────────────────────────────────
    print()
    print("SECTION 10: HIGH-EDGE DANGER / SUSPICIOUS EDGE")
    print(SEP2)
    print("  EDGE_DANGER_GUARD blocks new entries at original_edge >= 0.08.")
    print("  This section audits already-settled rows that had original_edge >= 0.08 at entry.")
    print()

    he_present = sum(1 for r in clean_settled if _af(r.get("original_edge")) is not None)
    if he_present == 0:
        print("  [MISSING] original_edge not present — cannot audit high-edge bucket.")
    else:
        high_edge = [r for r in clean_settled if (_af(r.get("original_edge")) or 0.0) >= 0.08]
        norm_edge = [r for r in clean_settled if (_af(r.get("original_edge")) or 0.0) <  0.08]

        print(f"  original_edge >= 0.08:  {len(high_edge)} settled rows")
        print(f"  original_edge < 0.08:   {len(norm_edge)} settled rows")

        if high_edge:
            m_he = Metrics(high_edge)
            print(
                f"  High-edge → ROI={_pct(m_he.roi)}  WR={_pct(m_he.win_rate)}"
                f"  PF={m_he._pf_str()}  CLV={_num(m_he.avg_clv)}  PnL={_money(m_he.total_pnl)}"
            )
            if m_he.roi is not None and m_he.roi < 0:
                print("  [CONFIRMED] High-edge bucket is NEGATIVE. Guard is correctly blocking these.")
                print("              Inverted edge: high predicted edge correlates with actual losses.")
            else:
                print("  [NOTE] High-edge bucket is non-negative. Sample may be too small to conclude.")

        if norm_edge:
            m_ne = Metrics(norm_edge)
            print(
                f"  Normal-edge <0.08 → ROI={_pct(m_ne.roi)}  WR={_pct(m_ne.win_rate)}"
                f"  PF={m_ne._pf_str()}  CLV={_num(m_ne.avg_clv)}"
            )
            if m_ne.roi is not None and m_ne.roi < 0:
                print("  [NOTE] Even normal-edge bucket is negative. Problem is not only high-edge.")

    # ── SECTION 11: STALE PROFILE / DATA FRESHNESS ───────────────────────────
    print()
    print("SECTION 11: STALE PROFILE / DATA FRESHNESS")
    print(SEP2)

    profile: dict = {}
    if PROFILE_PATH.exists():
        try:
            profile = json.loads(PROFILE_PATH.read_text())
            print(f"  Edge profile loaded: {PROFILE_PATH}")
        except Exception as exc:
            print(f"  Edge profile load error: {exc}")
    else:
        print(f"  [MISSING] {PROFILE_PATH} not found.")

    if profile:
        built_at = profile.get("built_at")
        print(f"  built_at:                  {built_at or 'n/a'}")
        print(f"  clean_settled_trades:      {profile.get('clean_settled_trades', 'n/a')}")
        print(f"  modern_full_metadata:      {profile.get('modern_full_metadata_trades', 'n/a')}")
        print(f"  normal_council_approved:   {profile.get('normal_council_approved_modern_trades', 'n/a')}")
        print(f"  edge_profile_trusted:      {profile.get('edge_profile_trusted', 'n/a')}")

        if built_at:
            try:
                bdt = datetime.fromisoformat(str(built_at).replace("Z", "+00:00"))
                age_h = (datetime.now(timezone.utc) - bdt).total_seconds() / 3600
                stale = age_h > 48
                print(f"  profile_age_hours:         {age_h:.1f}h  ({'STALE' if stale else 'fresh'})")
                if stale:
                    print("  [ACTION] Run: python3 tools/build_edge_profile.py")
            except Exception:
                pass

    # Performance split by edge_profile_trusted at time of entry
    trusted_rows   = [r for r in clean_settled if r.get("edge_profile_trusted") is True]
    untrusted_rows = [r for r in clean_settled if r.get("edge_profile_trusted") is False]
    unknown_ep     = [r for r in clean_settled if r.get("edge_profile_trusted") is None]

    print()
    print(f"  Settled trades by profile trust at entry time:")
    print(f"    edge_profile_trusted=True:   {len(trusted_rows)}")
    print(f"    edge_profile_trusted=False:  {len(untrusted_rows)}")
    print(f"    edge_profile_trusted=None:   {len(unknown_ep)} (field absent — old records)")

    if trusted_rows:
        m_tr = Metrics(trusted_rows)
        print(
            f"  Trusted-at-entry:    ROI={_pct(m_tr.roi):>8}  PF={m_tr._pf_str():>5}"
            f"  CLV={_num(m_tr.avg_clv)}  n={len(trusted_rows)}"
        )
    if untrusted_rows:
        m_un = Metrics(untrusted_rows)
        print(
            f"  Untrusted-at-entry:  ROI={_pct(m_un.roi):>8}  PF={m_un._pf_str():>5}"
            f"  CLV={_num(m_un.avg_clv)}  n={len(untrusted_rows)}"
        )
    if trusted_rows and untrusted_rows:
        m_tr2 = Metrics(trusted_rows)
        m_un2 = Metrics(untrusted_rows)
        if (m_tr2.roi is not None and m_un2.roi is not None
                and abs(m_tr2.roi - m_un2.roi) > 0.05):
            print()
            print("  [NOTE] Material ROI gap between trusted/untrusted profile states.")
            print("         Trades taken while profile was untrusted may have lower signal quality.")

    # ── SECTION 12: DRAWDOWN / LOSING STREAK ─────────────────────────────────
    print()
    print("SECTION 12: DRAWDOWN / LOSING STREAK")
    print(SEP2)

    print(f"  Sorted by settled_at → timestamp. N={len(cs_sorted)}")
    print()
    print(f"  Max drawdown:              ${max_dd:.2f}")
    print(f"  Longest losing streak:     {streak_n}")

    if curve and len(curve) > 1:
        print(f"  Equity curve peak:         {_money(max(curve))}")
        print(f"  Equity curve trough:       {_money(min(curve))}")
        print(f"  Final equity (settled):    {_money(curve[-1])}")

    print()
    for w in (5, 10, 20):
        wv = worst_window(cs_sorted, w)
        if wv is not None:
            print(f"  Worst {w:>2}-trade window:      {_money(wv)}")

    if streak_n > 0 and cs_sorted:
        streak_rows = cs_sorted[streak_s: streak_e + 1]
        streak_pnl  = sum(get_pnl(r) for r in streak_rows)
        ts0 = str(streak_rows[0].get("settled_at") or streak_rows[0].get("timestamp") or "?")[:19]
        ts1 = str(streak_rows[-1].get("settled_at") or streak_rows[-1].get("timestamp") or "?")[:19]
        print()
        print(f"  Longest streak details: {streak_n} losses  PnL={_money(streak_pnl)}")
        print(f"    Period: {ts0} → {ts1}")
        print("    Tickers:", ", ".join(r.get("ticker", "?") for r in streak_rows))

    # Show last 20 trades equity walk
    if len(cs_sorted) >= 5:
        recent = cs_sorted[-20:]
        print()
        print(f"  Last {len(recent)} settled trades (chronological):")
        running = sum(get_pnl(r) for r in cs_sorted[:-len(recent)])
        for r in recent:
            p = get_pnl(r)
            running += p
            res  = r.get("result", "?")
            tk   = r.get("ticker", "?")[:30]
            clv  = get_clv(r)
            ts   = str(r.get("settled_at") or r.get("timestamp") or "?")[:19]
            print(
                f"    {ts}  {tk:<30}  {res:<4}  PnL={_money(p):>8}"
                f"  CLV={_num(clv, 4) if clv is not None else '   n/a':>9}"
                f"  equity={_money(running):>9}"
            )

    # ── SECTION 13: FINAL VERDICT ─────────────────────────────────────────────
    print()
    print(SEP)
    print("SECTION 13: FINAL VERDICT")
    print(SEP)

    # Collect poison and edge candidates across scopes
    poison_list: List[Tuple[str, str, float, str]] = []
    edge_list:   List[Tuple[str, str, float, str]] = []

    def _collect(scope: str, label: str, rows: List[dict]) -> None:
        if not rows:
            return
        m = Metrics(rows)
        v = m.verdict()
        if v == "POISON":
            poison_list.append((scope, label, m.total_pnl, _pct(m.roi)))
        elif v == "POSSIBLE_EDGE":
            edge_list.append((scope, label, m.total_pnl, _pct(m.roi)))

    for pfx in sorted_prefixes:
        _collect("prefix", pfx, prefix_settled_map.get(pfx, []))
    for bkt in ENTRY_PRICE_ORDER:
        _collect("entry_price", bkt, ep_groups.get(bkt, []))
    for bkt in PROB_ORDER:
        _collect("prob_bucket", bkt, prob_groups.get(bkt, []))
    for bkt in EDGE_ORDER:
        grp = group_by(clean_settled, lambda r, c="original_edge": edge_bucket_fn(_af(r.get(c))))
        _collect("edge_bucket", bkt, grp.get(bkt, []))

    poison_sorted = sorted(poison_list, key=lambda x: x[2])  # worst PnL first
    edge_sorted   = sorted(edge_list,   key=lambda x: -x[2])  # best PnL first

    print()
    print("  POISON BUCKETS (worst performing, at least n=3):")
    if poison_sorted:
        for i, (scope, lbl, pnl, roi) in enumerate(poison_sorted[:3], 1):
            print(f"    #{i}: {scope}={lbl:<26}  PnL={_money(pnl):>9}  ROI={roi}  ← QUARANTINE CANDIDATE")
    else:
        print("    No single bucket clearly flagged POISON (need n>=3, ROI<0, PF<1).")
        print("    Losses appear distributed — no dominant single poison pocket.")

    print()
    print("  POSSIBLE EDGE BUCKETS (ROI>0, PF>1.0, CLV>=0, n>=5):")
    if edge_sorted:
        for scope, lbl, pnl, roi in edge_sorted[:3]:
            print(f"    {scope}={lbl:<26}  PnL={_money(pnl):>9}  ROI={roi}")
            print(f"      ↑ WARNING: Sample likely too small. Do NOT trade based on this alone.")
    else:
        print("    NONE. No bucket currently passes all three positive gates.")

    print()
    print("  ROOT CAUSE ANALYSIS:")
    if m_all.win_rate is not None and m_all.breakeven_wr is not None:
        gap = m_all.wr_vs_be or 0.0
        if gap < 0:
            print(f"    Structural shortfall: win rate {_pct(m_all.win_rate)} < breakeven {_pct(m_all.breakeven_wr)}  (gap={_pct(gap)})")
            print(f"    The system must win {_pct(m_all.breakeven_wr)} of trades to break even. It wins {_pct(m_all.win_rate)}.")
        else:
            print(f"    Win rate {_pct(m_all.win_rate)} exceeds breakeven {_pct(m_all.breakeven_wr)} — but system still loses.")
            print(f"    Problem is payout asymmetry: avg_win {_money(m_all.avg_win)} < |avg_loss| {_money(abs(m_all.avg_loss or 0))}")
    if m_all.avg_clv is not None and m_all.avg_clv < 0:
        print(f"    Negative CLV ({_num(m_all.avg_clv)}): exit price is on average below entry price.")
        print(f"    This means the system is consistently buying on the expensive side of the market.")
    if m_all.calibration_gap is not None and m_all.calibration_gap < -0.05:
        print(
            f"    Overconfident: predicted {_pct(m_all.avg_prob)} avg probability,"
            f" realized {_pct(m_all.realized_wr)} win rate."
        )

    print()
    print("  WHAT TO QUARANTINE FIRST:")
    if poison_sorted:
        _, lbl, pnl, roi = poison_sorted[0]
        print(f"    → {lbl} bucket  ({roi} ROI, {_money(pnl)} PnL)")
        print(f"      Investigate what makes this setup consistently lose before touching anything else.")
    else:
        print("    → No dominant poison bucket. Review calibration section and entry price distribution.")
        print("      The problem may be systemic (wrong model, wrong market, wrong breakeven math).")

    print()
    print("  WHAT NOT TO TOUCH:")
    print("    - Safety locks (TRADING_MODE, real_money_allowed, scale_allowed)")
    print("    - MIN_EDGE = 0.03, MIN_CONFIDENCE = 0.65  (do not lower)")
    print("    - GLOBAL_FORCED_LEARNING_MODE = True  (Kelly correctly disabled)")
    print("    - Proof gate thresholds in evaluate_proof_gates()")
    print("    - EDGE_DANGER_BLOCK_HIGH_EDGE = True  (high-edge bucket confirmed dangerous)")
    print("    - classify_records() / row_quality_group() classification logic")
    print("    - The trade log (paper_trades.jsonl) — append-only, do not truncate")

    print()
    print("  MISSING DATA / GAPS:")
    missing_items: List[str] = []
    clv_missing = len(clean_settled) - clv_present
    if clv_missing > len(clean_settled) // 4:
        missing_items.append(f"CLV: {clv_missing}/{len(clean_settled)} rows have no exit_price or clv field")
    if missing_prob > len(clean_settled) // 4:
        missing_items.append(f"model_probability: {missing_prob}/{len(clean_settled)} rows missing")
    mq_missing = len(clean_settled) - mq_pres if mq_pres > 0 else len(clean_settled)
    if mq_missing > len(clean_settled) // 4:
        missing_items.append(f"market_quality_passed: {mq_missing}/{len(clean_settled)} rows missing")
    if not missing_items:
        missing_items.append("No critical field gaps found in the clean settled sample.")
    for item in missing_items:
        print(f"    - {item}")

    print()
    print("  CURRENTLY PROVEN TRADABLE SETUPS: NONE")
    print("  All performance gates fail. System is at WATCHLIST — data collecting, not proven.")
    print()
    print(SEP)
    print("  DO NOT SCALE UNTIL A CLEAN SETUP PASSES ROI, PF, CLV, SAMPLE, AND DRAWDOWN GATES.")
    print(SEP)
    print()


if __name__ == "__main__":
    main()
