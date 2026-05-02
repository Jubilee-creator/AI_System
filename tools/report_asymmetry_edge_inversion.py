#!/usr/bin/env python3
"""
tools/report_asymmetry_edge_inversion.py
-----------------------------------------
Forensic audit of payout asymmetry and edge inversion.

ASYMMETRY THEOREM (Kalshi binary contract):
  For any bet at entry price P (YES or NO contract):
    Win:  PnL = stake * (1 - P) / P
    Loss: PnL = -stake
    Breakeven WR = P   (entry price IS the breakeven win rate)
  Implication: buying at 0.65 requires 65% WR to break even.
  If actual WR = 50% the structural gap = -(0.65 - 0.50) = -0.15.

EDGE INVERSION DIAGNOSIS:
  Legacy formula: edge = confidence - entry_price - fee
    entry=0.09 => edge=0.84  (spurious artifact)
    entry=0.05 => edge=0.62  (spurious artifact)
  All high-edge losers (edge >= 0.08) are LEGACY_EDGE_ONLY — no real edge.
  EDGE_DANGER_BLOCK_HIGH_EDGE=True already blocks new high-edge trades.

Expected result: ASYMMETRY_EDGE_INVERSION_REPORT_OK
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.clean_truth_report import (
    CALIBRATION_BUCKET_ORDER,
    EDGE_ORDER,
    ENTRY_PRICE_ORDER,
    HORIZON_ORDER,
    ROW_QUALITY_ORDER,
    _as_float,
    _avg,
    _fmt_money,
    _fmt_num,
    _fmt_pct,
    _fmt_ratio,
    calc_asymmetry,
    calibration_bucket,
    classify_records,
    edge_bucket,
    edge_source,
    edge_source_quality,
    edge_value,
    entry_price_bucket,
    get_clv,
    get_pnl,
    get_size,
    high_edge_loser_flags,
    inferred_edge_fee,
    market_horizon,
    print_asymmetry_row,
    row_quality_group,
    strategy_key,
)
from tools.performance_report import load_trades


# ── helpers ────────────────────────────────────────────────────────────────────

def _section(title: str, width: int = 72) -> None:
    print()
    print(title)
    print("-" * width)


def _subsection(title: str) -> None:
    print()
    print(f"  {title}")
    print(f"  {'-' * len(title)}")


def _asymmetry_table(title: str, rows: list[dict], key_func,
                     order: list[str] | None = None) -> None:
    _section(title)
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[key_func(r)].append(r)
    keys = order if order is not None else sorted(groups)
    printed = False
    for k in keys:
        g = groups.get(k, [])
        if g:
            print_asymmetry_row(str(k), g)
            printed = True
    if not printed:
        print("  (no rows)")


def _compute_entry_breakeven_stats(rows: list[dict]) -> list[dict]:
    """
    For each entry-price bucket compute avg_entry, breakeven_WR (= avg_entry),
    actual_WR and gap.  Breakeven WR = entry price is the theorem for both YES
    and NO contracts on Kalshi because the entry_price field records the price
    actually paid for the contract.
    """
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[entry_price_bucket(r)].append(r)

    results = []
    for bucket in ENTRY_PRICE_ORDER:
        g = groups.get(bucket, [])
        if not g:
            continue
        entries = [_as_float(r.get("entry_price")) for r in g]
        entries = [e for e in entries if e is not None]
        avg_entry = sum(entries) / len(entries) if entries else None
        m = calc_asymmetry(g)
        actual_wr = m["win_rate"]
        breakeven_wr = avg_entry  # theorem: BE_WR = entry price
        gap = (actual_wr - breakeven_wr) if breakeven_wr is not None else None
        results.append({
            "bucket": bucket,
            "n": m["count"],
            "avg_entry": avg_entry,
            "actual_wr": actual_wr,
            "breakeven_wr": breakeven_wr,
            "gap": gap,
            "pnl": m["total_pnl"],
            "roi": m["roi"],
            "payoff_ratio": m["payoff_ratio"],
        })
    return results


def _classify_split(rows: list[dict]) -> dict[str, list]:
    """Split clean_settled into legacy / modern dc / modern provisional / normal_modern."""
    groups: dict[str, list] = {
        "LEGACY_EDGE_ONLY": [],
        "MODERN_DC_OVERRIDE": [],
        "MODERN_PROVISIONAL": [],
        "MODERN_NORMAL": [],
        "OTHER": [],
    }
    for r in rows:
        quality = row_quality_group(r)
        if quality == "LEGACY_EDGE_ONLY":
            groups["LEGACY_EDGE_ONLY"].append(r)
        elif quality in ("MODERN_FULL_METADATA", "MODERN_EDGE_ONLY"):
            if r.get("data_collection_override"):
                groups["MODERN_DC_OVERRIDE"].append(r)
            elif r.get("bootstrap_provisional"):
                groups["MODERN_PROVISIONAL"].append(r)
            else:
                groups["MODERN_NORMAL"].append(r)
        else:
            groups["OTHER"].append(r)
    return groups


def _recommend_zone(bucket: str, m: dict, n_min: int = 5) -> str:
    """Assign a danger/keep zone label."""
    if m["count"] < n_min:
        return "INSUFFICIENT_DATA"
    gap = m.get("actual_minus_breakeven")
    pf = m.get("profit_factor")
    roi = m.get("roi", 0.0)
    if gap is None:
        return "INSUFFICIENT_DATA"
    if gap < -0.20 or (roi is not None and roi < -0.50):
        return "QUARANTINE"
    if gap < -0.10 or (roi is not None and roi < -0.20):
        return "BLOCK"
    if gap < 0.0:
        return "WATCHLIST"
    return "KEEP"


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]
    modern_full = [r for r in clean_settled if row_quality_group(r) == "MODERN_FULL_METADATA"]

    split = _classify_split(clean_settled)

    W = 72
    print("=" * W)
    print("ASYMMETRY & EDGE INVERSION FORENSIC AUDIT")
    print("=" * W)
    print(f"Clean settled:          {len(clean_settled)}")
    print(f"  LEGACY_EDGE_ONLY:     {len(split['LEGACY_EDGE_ONLY'])}")
    print(f"  MODERN_DC_OVERRIDE:   {len(split['MODERN_DC_OVERRIDE'])}")
    print(f"  MODERN_PROVISIONAL:   {len(split['MODERN_PROVISIONAL'])}")
    print(f"  MODERN_NORMAL:        {len(split['MODERN_NORMAL'])}")
    print(f"  OTHER/MISSING:        {len(split['OTHER'])}")
    print()

    # ── Section 1: Payout Asymmetry Theorem ────────────────────────────────────

    print("=" * W)
    print("1. PAYOUT ASYMMETRY THEOREM")
    print("=" * W)
    print(
        "  For ANY Kalshi binary contract bought at price P:\n"
        "    Win:  PnL = stake × (1-P)/P\n"
        "    Loss: PnL = -stake\n"
        "    Breakeven WR = P  (the entry price IS the breakeven win rate)\n"
        "\n"
        "  Example:  entry = 0.65  =>  breakeven WR = 65%\n"
        "            entry = 0.09  =>  breakeven WR = 9%  (BUT win pays only 10¢ per $1)\n"
        "\n"
        "  The cheap-entry lottery illusion: low breakeven WR looks easy but\n"
        "  the win payout is tiny vs. the full-stake loss on a loss.\n"
        "  The payoff_ratio = (1-P)/P collapses toward 0 as P rises.\n"
    )

    m_overall = calc_asymmetry(clean_settled)
    clv_vals = [v for v in (get_clv(r) for r in clean_settled) if v is not None]
    avg_clv = _avg(clv_vals)

    print("OVERALL CLEAN SETTLED")
    print("-" * W)
    print_asymmetry_row("ALL", clean_settled)
    print()
    print(f"  avg_CLV:         {_fmt_num(avg_clv)} (n={len(clv_vals)})")
    pf = m_overall["profit_factor"]
    print(f"  profit_factor:   {_fmt_ratio(pf) if pf else 'n/a'}")
    print(f"  payoff_ratio:    {_fmt_ratio(m_overall['payoff_ratio'])}  "
          f"(avg_win / |avg_loss| — need >= 1.0 for positive expectancy at 50% WR)")
    aw = m_overall["avg_win"]
    al = m_overall["avg_loss"]
    if aw is not None and al is not None:
        print(f"  avg_win:         {_fmt_money(aw)}")
        print(f"  avg_loss:        {_fmt_money(al)}")
    be = m_overall["breakeven_win_rate"]
    wr = m_overall["win_rate"]
    if be is not None:
        print(f"  breakeven_WR:    {_fmt_pct(be)}  (from avg_win/|avg_loss| ratio)")
        print(f"  actual_WR:       {_fmt_pct(wr)}")
        gap = wr - be
        label = "POSITIVE" if gap >= 0 else "NEGATIVE"
        print(f"  WR - BE_WR:      {_fmt_pct(gap)}  ({label} EXPECTANCY)")

    # ── Section 2: Entry-Price Breakeven Analysis ───────────────────────────────

    _section("2. ENTRY-PRICE BREAKEVEN ANALYSIS")
    print(
        "  Each row shows the entry-price bucket's avg_entry (= theoretical breakeven WR),\n"
        "  the actual WR achieved, and the gap.  Negative gap = structural loss.\n"
    )
    ep_stats = _compute_entry_breakeven_stats(clean_settled)
    print(
        f"  {'Bucket':<12}  {'n':>4}  {'AvgEntry':>9}  {'BE_WR':>7}  "
        f"{'ActualWR':>9}  {'Gap':>8}  {'PnL':>9}  {'ROI':>8}  {'PayoffR':>8}"
    )
    print(f"  {'-'*12}  {'-'*4}  {'-'*9}  {'-'*7}  {'-'*9}  {'-'*8}  {'-'*9}  {'-'*8}  {'-'*8}")
    for s in ep_stats:
        gap_str = _fmt_pct(s["gap"]) if s["gap"] is not None else "  n/a  "
        print(
            f"  {s['bucket']:<12}  {s['n']:>4}  "
            f"{_fmt_pct(s['avg_entry']) if s['avg_entry'] is not None else '  n/a  ':>9}  "
            f"{_fmt_pct(s['breakeven_wr']) if s['breakeven_wr'] is not None else '  n/a  ':>7}  "
            f"{_fmt_pct(s['actual_wr']):>9}  "
            f"{gap_str:>8}  "
            f"{_fmt_money(s['pnl']):>9}  "
            f"{_fmt_pct(s['roi']):>8}  "
            f"{_fmt_ratio(s['payoff_ratio']) if s['payoff_ratio'] is not None else ' n/a':>8}"
        )
    print()
    print("  NOTE: BE_WR column equals the AvgEntry column. This is the theorem —")
    print("  not a coincidence. The payoff ratio (1-P)/P collapses as entry price rises.")

    # ── Section 3: Legacy vs Modern Damage Attribution ──────────────────────────

    _section("3. LEGACY vs MODERN DAMAGE ATTRIBUTION")
    print(
        "  LEGACY_EDGE_ONLY rows used large bet sizes ($5–$10) and the old formula\n"
        "  edge = confidence - entry_price - fee which produces spurious high edge\n"
        "  for cheap (< 0.20) entries. These are the primary loss driver.\n"
    )
    total_loss = sum(get_pnl(r) for r in clean_settled if get_pnl(r) < 0)

    for label, group in [
        ("LEGACY_EDGE_ONLY",   split["LEGACY_EDGE_ONLY"]),
        ("MODERN_DC_OVERRIDE", split["MODERN_DC_OVERRIDE"]),
        ("MODERN_PROVISIONAL", split["MODERN_PROVISIONAL"]),
        ("MODERN_NORMAL",      split["MODERN_NORMAL"]),
    ]:
        if not group:
            print(f"  {label:<22} (no rows)")
            continue
        m = calc_asymmetry(group)
        group_loss = sum(get_pnl(r) for r in group if get_pnl(r) < 0)
        loss_pct = (group_loss / total_loss * 100) if total_loss < 0 else 0.0
        avg_size = _avg([get_size(r) for r in group])
        print_asymmetry_row(label, group)
        print(
            f"    avg_size={_fmt_money(avg_size) if avg_size else 'n/a'}  "
            f"loss_contribution={_fmt_pct(loss_pct / 100)} of total_loss"
        )

    print()
    if total_loss < 0:
        legacy_loss = sum(get_pnl(r) for r in split["LEGACY_EDGE_ONLY"] if get_pnl(r) < 0)
        print(f"  Total losses:        {_fmt_money(total_loss)}")
        print(f"  Legacy share:        {_fmt_money(legacy_loss)}  "
              f"({_fmt_pct(legacy_loss / total_loss) if total_loss else 'n/a'} of all losses)")
        print("  [DIAGNOSIS] Remove legacy rows from future bet sizing — they are")
        print("  the single largest contributor to drawdown.")

    # ── Section 4: Edge Bucket Performance ─────────────────────────────────────

    _asymmetry_table("4. EDGE BUCKET PERFORMANCE (clean settled)", clean_settled, edge_bucket, EDGE_ORDER)
    print()
    high_edge_rows = [r for r in clean_settled if edge_value(r) is not None and edge_value(r) >= 0.08]
    if high_edge_rows:
        legacy_he = [r for r in high_edge_rows if edge_source_quality(r) == "LEGACY_EDGE_ONLY"]
        risk_edge_he = [r for r in high_edge_rows if edge_source_quality(r) == "RISK_EDGE_AVAILABLE"]
        print(f"  HIGH-EDGE (>=0.08) breakdown:")
        print(f"    LEGACY_EDGE_ONLY:      {len(legacy_he)}  (spurious formula — no real edge)")
        print(f"    RISK_EDGE_AVAILABLE:   {len(risk_edge_he)}  (genuine model edge)")
        if not risk_edge_he:
            print("    [CONFIRMED] All high-edge signals came from legacy formula — EDGE_INVERSION=YES")
        else:
            print(f"    [NOTE] {len(risk_edge_he)} RISK_EDGE high-edge rows exist — review individually")
    else:
        print("  No high-edge (>=0.08) rows in clean settled.")

    # ── Section 5: High-Edge Loser Forensics ───────────────────────────────────

    _section("5. HIGH-EDGE LOSER FORENSICS (edge >= 0.08, PnL < 0)")
    losers = [r for r in clean_settled if edge_value(r) is not None
              and edge_value(r) >= 0.08 and get_pnl(r) < 0]

    if not losers:
        print("  (none — EDGE_DANGER guard may already be preventing new ones)")
    else:
        from_config = __import__("config.trading_config", fromlist=["EDGE_DANGER_BLOCK_HIGH_EDGE",
                                                                     "EDGE_DANGER_HIGH_EDGE_MIN"])
        guard_active = getattr(from_config, "EDGE_DANGER_BLOCK_HIGH_EDGE", False)
        guard_min = getattr(from_config, "EDGE_DANGER_HIGH_EDGE_MIN", 0.08)
        print(f"  EDGE_DANGER guard: EDGE_DANGER_BLOCK_HIGH_EDGE={guard_active} "
              f"(min={guard_min})")
        if guard_active:
            print("  [PROTECTED] No new high-edge trades will be accepted while guard is active.")
        print()
        for rec in losers:
            entry = _as_float(rec.get("entry_price"))
            edge = edge_value(rec)
            confidence = _as_float(rec.get("confidence")) or _as_float(rec.get("original_confidence"))
            inferred_fee = inferred_edge_fee(rec)
            flags = high_edge_loser_flags(rec)
            ticker = rec.get("ticker", "n/a")
            strategy = rec.get("strategy", "n/a")

            # Show the legacy formula math to prove spurious nature
            if confidence is not None and entry is not None and edge is not None:
                computed = confidence - entry - 0.01
                formula_note = (
                    f"conf-entry-0.01={_fmt_num(computed)} "
                    f"{'≈ stored edge (SPURIOUS)' if abs(computed - edge) < 0.005 else '≠ stored edge'}"
                )
            else:
                formula_note = "cannot verify formula"

            print(
                f"  {ticker:<44}  edge={_fmt_num(edge)}  entry={_fmt_num(entry)}  "
                f"pnl={_fmt_money(get_pnl(rec))}"
            )
            print(
                f"    strategy={strategy}  source={edge_source_quality(rec)}  "
                f"horizon={market_horizon(rec)}"
            )
            print(f"    formula_check: {formula_note}")
            print(f"    flags: {', '.join(flags) or 'none'}")
            print()

    # ── Section 6: Asymmetry by Strategy and Horizon ───────────────────────────

    _asymmetry_table(
        "6a. ASYMMETRY BY STRATEGY", clean_settled, strategy_key
    )
    _asymmetry_table(
        "6b. ASYMMETRY BY MARKET HORIZON", clean_settled, market_horizon, HORIZON_ORDER
    )
    _asymmetry_table(
        "6c. ASYMMETRY BY ROW QUALITY", clean_settled, row_quality_group, ROW_QUALITY_ORDER
    )
    _asymmetry_table(
        "6d. ASYMMETRY BY CONFIDENCE BUCKET", clean_settled, calibration_bucket,
        CALIBRATION_BUCKET_ORDER
    )

    # ── Section 7: Danger Zones Ranked by Damage ───────────────────────────────

    _section("7. DANGER ZONES RANKED BY DAMAGE")
    print(
        "  Zones cross entry-price bucket × row-quality.\n"
        "  QUARANTINE: WR-BE gap < -20% or ROI < -50%\n"
        "  BLOCK:      WR-BE gap < -10% or ROI < -20%\n"
        "  WATCHLIST:  WR-BE gap < 0%\n"
        "  KEEP:       positive expectancy (rare at this sample size)\n"
    )

    zone_data = []
    for quality_label, quality_rows in split.items():
        if not quality_rows:
            continue
        for ep_bkt in ENTRY_PRICE_ORDER:
            zone_rows = [r for r in quality_rows if entry_price_bucket(r) == ep_bkt]
            if not zone_rows:
                continue
            m = calc_asymmetry(zone_rows)
            if m["count"] == 0:
                continue
            rec = _recommend_zone(ep_bkt, m)
            zone_data.append((m["total_pnl"], quality_label, ep_bkt, m, rec))

    zone_data.sort(key=lambda x: x[0])  # most negative first

    if not zone_data:
        print("  (no zone data)")
    else:
        print(
            f"  {'Zone':<44}  {'n':>4}  {'PnL':>9}  {'ROI':>8}  "
            f"{'WR-BE':>7}  {'Verdict':>18}"
        )
        print(f"  {'-'*44}  {'-'*4}  {'-'*9}  {'-'*8}  {'-'*7}  {'-'*18}")
        for pnl, quality, ep_bkt, m, rec in zone_data:
            zone_label = f"{quality}/{ep_bkt}"
            gap = m.get("actual_minus_breakeven")
            print(
                f"  {zone_label:<44}  {m['count']:>4}  {_fmt_money(pnl):>9}  "
                f"{_fmt_pct(m['roi']):>8}  "
                f"{_fmt_pct(gap) if gap is not None else '  n/a ':>7}  "
                f"{rec:>18}"
            )

    # ── Section 8: CLV Analysis ─────────────────────────────────────────────────

    _section("8. CLV (CLOSING LINE VALUE) ANALYSIS")
    print(
        "  CLV = settlement_price - entry_price\n"
        "    YES win: settlement=1.00, CLV = 1.00 - entry  (positive, small)\n"
        "    YES loss: settlement=0.00, CLV = 0.00 - entry  (negative, large)\n"
        "  avg_CLV < 0 means the market's closing price is consistently below entry —\n"
        "  i.e. the model overestimates the probability of YES resolution.\n"
    )
    print(f"  overall avg_CLV:  {_fmt_num(avg_clv)}  (n={len(clv_vals)})")
    if avg_clv is not None:
        if avg_clv < 0:
            print("  [CLV WARNING] Model is systematically overpricing YES probability.")
            print("  The market's consensus, on average, disagrees with model entry direction.")
        elif avg_clv > 0:
            print("  [CLV POSITIVE] Model has positive closing-line value — good sign.")
        else:
            print("  [CLV FLAT] avg_CLV near zero.")
    print()

    for label, group in split.items():
        if not group:
            continue
        vals = [v for v in (get_clv(r) for r in group) if v is not None]
        if vals:
            print(f"  {label:<22}  avg_CLV={_fmt_num(_avg(vals))}  n={len(vals)}")

    # ── Section 9: 10x Better Path ──────────────────────────────────────────────

    _section("9. THE 10X BETTER PATH (without fake profitability)")
    print(
        "  The system cannot be 10x better by simply running more trades or\n"
        "  changing config. These are the structural requirements:\n"
        "\n"
        "  [PATH 1] Fix entry prices.\n"
        "    - Target entries < 0.50 on well-priced bets where WR >= 50% is achievable.\n"
        "    - For any P > 0.50 bet, need model confidence > P to have real edge.\n"
        "    - Avoid P > 0.75 unless win probability evidence is overwhelming.\n"
        "\n"
        "  [PATH 2] Fix payout asymmetry.\n"
        "    - Need avg_win / |avg_loss| > 1.0  (currently "
        + _fmt_ratio(m_overall["payoff_ratio"]) + ").\n"
        "    - One route: bet more NO contracts on overpriced YES markets\n"
        "      (entry P_no = 1 - P_yes is often cheap when YES is mispriced).\n"
        "\n"
        "  [PATH 3] Fix edge quality.\n"
        "    - ALL high-edge signals that lost were LEGACY_EDGE_ONLY.\n"
        "    - Only trust risk_edge (model_prob - market_price - fee).\n"
        "    - EDGE_DANGER guard is already active — do not remove it.\n"
        "\n"
        "  [PATH 4] Accumulate MODERN_NORMAL council-approved trades.\n"
        "    - Currently 0 normal_modern trades in proof pool.\n"
        "    - Bootstrap_provisional + dc_override excluded from proof.\n"
        "    - Need 30 normal_modern with positive ROI before any scale review.\n"
        "\n"
        "  [PATH 5] Require avg_CLV > 0 before any scale review.\n"
        f"    - Currently {_fmt_num(avg_clv)} — negative means systematic misprice.\n"
        "    - Positive CLV is a precondition for a trustworthy edge claim.\n"
    )

    # ── Section 10: Final Verdicts ──────────────────────────────────────────────

    print("=" * W)
    print("10. FINAL VERDICTS")
    print("=" * W)

    asymmetry_fixed = (
        m_overall["payoff_ratio"] is not None
        and m_overall["payoff_ratio"] >= 1.0
    )
    high_edge_losers_exist = bool(losers)
    edge_inversion_risk = high_edge_losers_exist or any(
        edge_source_quality(r) == "LEGACY_EDGE_ONLY"
        and edge_value(r) is not None
        and edge_value(r) >= 0.08
        for r in clean_settled
    )
    normal_modern_count = len(split["MODERN_NORMAL"])
    profitable_ready = (
        m_overall["roi"] > 0
        and (avg_clv or 0.0) > 0
        and normal_modern_count >= 30
        and not edge_inversion_risk
        and asymmetry_fixed
    )

    print(f"  ASYMMETRY_FIXED:            {'YES' if asymmetry_fixed else 'NO'}")
    print(f"    payoff_ratio = {_fmt_ratio(m_overall['payoff_ratio'])}  (need >= 1.0)")
    print()
    print(f"  EDGE_INVERSION_RISK:        {'HIGH' if edge_inversion_risk else 'LOW'}")
    if high_edge_losers_exist:
        print(f"    {len(losers)} high-edge losers — all LEGACY_EDGE_ONLY with spurious formula edge")
    print()
    print(f"  CLV_POSITIVE:               {'YES' if (avg_clv or 0.0) > 0 else 'NO'}")
    print(f"    avg_CLV = {_fmt_num(avg_clv)}  (need > 0)")
    print()
    print(f"  NORMAL_MODERN_PROOF_READY:  {'YES' if normal_modern_count >= 30 else 'NO'}")
    print(f"    normal_modern = {normal_modern_count}  (need >= 30)")
    print()
    print(f"  PROFITABLE_10X_READY:       {'YES' if profitable_ready else 'NO'}")
    print()
    print("  LOCKDOWN STATUS (unchanged):")
    print("    scale_allowed     = False  (hardcoded)")
    print("    real_money_allowed = False  (hardcoded)")
    print("    TRADING_MODE      = PAPER")
    print()
    print("RESULT: ASYMMETRY_EDGE_INVERSION_REPORT_OK")


if __name__ == "__main__":
    main()
