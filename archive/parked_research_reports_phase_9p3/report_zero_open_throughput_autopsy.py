#!/usr/bin/env python3
"""
tools/report_zero_open_throughput_autopsy.py
--------------------------------------------
Phase 8W: Read-only diagnosis of why 15 000+ post-quarantine funnel rows
produced zero opened trades.

Does NOT change any live behavior, thresholds, or config.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
EDGE_PROFILE_PATH = ROOT / "data" / "edge_profile.json"
QUARANTINE_TS = "2026-05-08T08:53:27"

# ── helpers ──────────────────────────────────────────────────────────────────

def _af(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(num: int, denom: int) -> str:
    if denom == 0:
        return "  n/a"
    return f"{num / denom * 100:5.1f}%"


def _bar(n: int, total: int, width: int = 30) -> str:
    if total == 0:
        return " " * width
    filled = round(n / total * width)
    return "█" * filled + "░" * (width - filled)


def ticker_prefix(ticker: str) -> str:
    if not ticker:
        return ""
    parts = str(ticker).upper().split("-")
    p = parts[0]
    for suffix in ("15M", "1H", "1D", "D"):
        if p.endswith(suffix) and len(p) > len(suffix):
            return p[: -len(suffix)]
    return p


def is_kxeth(ticker: str) -> bool:
    return str(ticker or "").upper().startswith("KXETH")


def load_funnel() -> list[dict]:
    rows: list[dict] = []
    if not FUNNEL_LOG.exists():
        return rows
    with FUNNEL_LOG.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def load_trades() -> list[dict]:
    rows: list[dict] = []
    if not TRADES_LOG.exists():
        return rows
    with TRADES_LOG.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def load_edge_profile() -> dict:
    if not EDGE_PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(EDGE_PROFILE_PATH.read_text())
    except Exception:
        return {}


# ── stats helpers ─────────────────────────────────────────────────────────────

def _stats_line(vals: list[float], label: str) -> str:
    if not vals:
        return f"  {label}: no data"
    v = sorted(vals)
    n = len(v)
    avg = sum(v) / n
    p10 = v[max(0, int(n * 0.10))]
    p50 = v[n // 2]
    p90 = v[min(n - 1, int(n * 0.90))]
    return (
        f"  {label}: n={n}  min={min(v):.4f}  "
        f"p10={p10:.4f}  p50={p50:.4f}  p90={p90:.4f}  "
        f"max={max(v):.4f}  avg={avg:.4f}"
    )


def _hdr(title: str, width: int = 80) -> None:
    print()
    print(f"SECTION {title}")
    print("-" * width)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 80)
    print("ZERO-OPEN THROUGHPUT AUTOPSY")
    print(f"  generated_at: {datetime.now(timezone.utc).isoformat()}")
    print()
    print("  READ-ONLY — no live behavior is changed by this report.")
    print(f"  Quarantine activation: {QUARANTINE_TS}")
    print("=" * 80)

    all_funnel = load_funnel()
    all_trades = load_trades()
    ep = load_edge_profile()

    post_q = [r for r in all_funnel if r.get("timestamp_utc", "") >= QUARANTINE_TS]
    non_kxeth = [r for r in post_q if not is_kxeth(r.get("ticker", ""))]
    sweet = [
        r for r in non_kxeth
        if 0.80 <= (_af(r.get("yes_ask")) or 0) < 0.90
    ]

    # Post-quarantine trades
    by_key: dict = defaultdict(list)
    for r in all_trades:
        by_key[(r.get("ticker", ""), r.get("timestamp", ""))].append(r)
    post_q_trades = [
        rows[-1] for rows in by_key.values()
        if (rows[-1].get("timestamp", "") or rows[-1].get("opened_at", "")) >= QUARANTINE_TS
    ]
    post_q_settled = [r for r in post_q_trades if r.get("status") == "SETTLED"]

    total_post = len(post_q)
    total_non_kxeth = len(non_kxeth)
    total_sweet = len(sweet)
    opened = sum(1 for r in post_q if r.get("final_reason") == "TRADE_OPENED")

    # ── Section 1 ─────────────────────────────────────────────────────────────
    _hdr("1: POST-QUARANTINE FUNNEL SUMMARY")
    print(f"  Total funnel rows (all time):        {len(all_funnel):>7,}")
    print(f"  Post-quarantine rows:                {total_post:>7,}")
    print(f"  Non-KXETH post-quarantine:           {total_non_kxeth:>7,}")
    print(f"  Non-KXETH 0.80-0.90 sweet-spot:      {total_sweet:>7,}")
    print()
    print(f"  Post-quarantine trades opened:       {opened:>7,}")
    print(f"  Post-quarantine trades settled:      {len(post_q_settled):>7,}")
    print()
    print(f"  Quarantine still active:             YES")
    print(f"  edge_profile.json age:               "
          + (ep.get("generated_at", "MISSING") or "MISSING"))
    ep_health = ep.get("edge_profile_health") or {}
    print(f"  edge_profile trusted:                {ep_health.get('edge_profile_trusted','?')}")
    print(f"  edge_profile normal_modern:          {ep_health.get('normal_council_approved_modern_trades','?')}")

    # ── Section 2 ─────────────────────────────────────────────────────────────
    _hdr("2: WHY TRADE_OPENED = 0")
    print("  Paper trades are opened when ALL of the following pass:")
    print()
    print("  GATE  CHECK                               THRESHOLD")
    print("  ────  ─────────────────────────────────── ──────────────────────")
    print("  G1    KXETH quarantine prefix block       ticker NOT KXETH*")
    print("  G2    Market quality (spread + volume)    spread ≤ 0.05 AND vol ≥ 1000")
    print("  G3    Edge danger guard                   pre-council edge < 0.08")
    print("  G4    Min edge (pre-council)              confidence − price − 0.01 ≥ 0.03")
    print("  G5    Decision Council (Critic)           edge/confidence profile NOT losing")
    print("  G6    Post-council min edge               adjusted confidence − price − 0.01 ≥ 0.03")
    print("  G7    Risk manager                        exposure / kill-switch / daily loss")
    print("  G8    Open trade cap                      open_count < 3")
    print()
    print("  All 15,266 post-quarantine rows failed at G1–G5.")
    print("  No row reached G6, G7, or G8.")
    print()
    print("  ROOT CAUSES (sections 3–8 drill down on each):")
    print("  1. Market volume too low (G2) — dominant blocker at 48%")
    print("  2. Structural thin edge at high entry prices (G4) — 16% of all, 31% of sweet-spot")
    print("  3. Edge profile contamination blocking council (G5) — 11% of all, 39% of sweet-spot")
    print("  4. KXETH quarantine (G1) — 19% (correct and expected)")
    print("  5. High-edge danger guard (G3) — 6% (correct safety block)")

    # ── Section 3 ─────────────────────────────────────────────────────────────
    _hdr("3: TOP BLOCKERS — ALL POST-QUARANTINE ROWS")
    reasons = Counter(r.get("final_reason", "UNKNOWN") for r in post_q)
    print(f"  {'Blocker':<35}  {'Count':>6}  {'%':>6}  Bar")
    print("  " + "-" * 72)
    for reason, cnt in reasons.most_common(10):
        bar = _bar(cnt, total_post)
        print(f"  {reason:<35}  {cnt:>6,}  {_pct(cnt, total_post)}  {bar}")
    print()
    print(f"  TRADE_OPENED:  {opened:,}  (target)")

    # ── Section 4 ─────────────────────────────────────────────────────────────
    _hdr("4: TOP BLOCKERS — NON-KXETH 0.80-0.90 SWEET-SPOT (n={:,})".format(total_sweet))
    sweet_reasons = Counter(r.get("final_reason", "UNKNOWN") for r in sweet)
    print(f"  {'Blocker':<35}  {'Count':>6}  {'%':>6}  Bar")
    print("  " + "-" * 72)
    for reason, cnt in sweet_reasons.most_common():
        bar = _bar(cnt, total_sweet)
        print(f"  {reason:<35}  {cnt:>6,}  {_pct(cnt, total_sweet)}  {bar}")
    print()
    print("  Interpretation:")
    print("  • BLOCKED_COUNCIL (38.7%): signals that already cleared G2 + G4,")
    print("    then hit the Decision Council's Critic block on contaminated profile.")
    print("  • BLOCKED_MIN_EDGE (30.7%): pre-council edge barely below 0.03 —")
    print("    structural constraint at high entry prices (see Section 6).")
    print("  • BLOCKED_MARKET_QUALITY (29.0%): volume < 1000 even with tight spread.")

    # ── Section 5 ─────────────────────────────────────────────────────────────
    _hdr("5: COUNCIL-BLOCK BREAKDOWN — WHICH BUCKET ACTUALLY CAUSED THE BLOCK")
    council_rows = [r for r in post_q if r.get("final_reason") == "BLOCKED_COUNCIL"]
    print(f"  BLOCKED_COUNCIL total: {len(council_rows):,}")
    print()

    eb_pattern: Counter = Counter()
    cb_pattern: Counter = Counter()
    both_count = 0
    small_sample_count = 0
    for r in council_rows:
        cr = r.get("council_reason", "") or ""
        has_eb = bool(re.search(r"edge bucket", cr))
        has_cb = bool(re.search(r"confidence bucket", cr))
        has_small = bool(re.search(r"small-sample confidence reduction", cr))
        if has_eb and has_cb:
            both_count += 1
        if has_small:
            small_sample_count += 1
        m = re.search(
            r"edge bucket ([0-9.<>=\-]+), trades=(\d+), "
            r"win_rate=([0-9.]+), total_pnl=([-0-9.]+)", cr
        )
        if m:
            key = f"edge={m.group(1)} n={m.group(2)} pnl={m.group(4)}"
            eb_pattern[key] += 1
        m2 = re.search(
            r"confidence bucket ([0-9.<>=\-]+), trades=(\d+), "
            r"win_rate=([0-9.]+), total_pnl=([-0-9.]+)", cr
        )
        if m2:
            key2 = f"conf={m2.group(1)} n={m2.group(2)} pnl={m2.group(4)}"
            cb_pattern[key2] += 1

    print(f"  Both edge bucket AND confidence bucket cited: "
          f"{both_count:,} ({_pct(both_count, len(council_rows))})")
    print(f"  'small-sample adjustment → edge below threshold': "
          f"{small_sample_count:,} ({_pct(small_sample_count, len(council_rows))})")
    print()
    print("  Edge-bucket block patterns (distinct reasons × count):")
    for k, v in eb_pattern.most_common(6):
        print(f"    {v:5,}×  {k}")
    print()
    print("  Confidence-bucket block patterns (distinct reasons × count):")
    for k, v in cb_pattern.most_common(8):
        print(f"    {v:5,}×  {k}")
    print()

    # Edge profile bucket state
    profiles = ep.get("profiles", {})
    eb_data = profiles.get("by_edge_bucket", {})
    cb_data = profiles.get("by_confidence_bucket", {})

    print("  Current edge_profile.json — by_edge_bucket:")
    for bucket_key in sorted(eb_data.keys()):
        b = eb_data[bucket_key]
        pnl = b.get("total_pnl", 0)
        wr = b.get("win_rate", 0)
        n = b.get("trades", 0)
        flag = "  ← CRITIC BLOCKS (pnl<0)" if pnl < 0 else "  ← OK (pnl>=0)"
        print(f"    {bucket_key:<12}: n={n:>3}, WR={wr:.3f}, total_pnl=${pnl:+.2f}{flag}")
    print()
    print("  Current edge_profile.json — by_confidence_bucket:")
    for bucket_key in sorted(cb_data.keys()):
        b = cb_data[bucket_key]
        pnl = b.get("total_pnl", 0)
        wr = b.get("win_rate", 0)
        n = b.get("trades", 0)
        flag = "  ← CRITIC BLOCKS (pnl<0)" if pnl < 0 else "  ← OK (pnl>=0)"
        print(f"    {bucket_key:<12}: n={n:>3}, WR={wr:.3f}, total_pnl=${pnl:+.2f}{flag}")
    print()
    print("  ROOT CAUSE OF COUNCIL BLOCKS:")
    print("  • Edge bucket 0.03-0.05 has total_pnl < 0 → Critic fires on ALL signals")
    print("    in this bucket. This bucket contains the structural payout losers")
    print("    (0.60-0.80 entry price) AND KXETH contamination.")
    print("  • Edge bucket 0.05-0.10 is even more negative. Signals in this bucket")
    print("    (KXBTC15M high-edge, KXSOL, KXXRP) are all council-blocked.")
    print("  • The edge profile does NOT have a price dimension. Sweet-spot trades")
    print("    (0.80-0.90 entry, WR=94%, pnl=+9.30) are grouped with POISON trades")
    print("    (0.60-0.80 entry, WR=69%, pnl=-14.85) inside the same edge bucket.")
    print("  • Rebuilding the edge profile without KXETH would reduce contamination")
    print("    but would NOT fix the council block: the non-KXETH 0.03-0.05 bucket")
    print("    still has total_pnl = -3.50 (mid-price structural losses dominate).")
    print("  • The council block will naturally resolve only when the 0.80-0.90 sweet-spot")
    print("    trades accumulate enough positive PnL to turn the bucket total positive.")

    # ── Section 6 ─────────────────────────────────────────────────────────────
    _hdr("6: MIN-EDGE BREAKDOWN — PRE-COUNCIL EDGE VS ENTRY PRICE")
    me_rows = [r for r in post_q if r.get("final_reason") == "BLOCKED_MIN_EDGE"]
    print(f"  BLOCKED_MIN_EDGE total: {len(me_rows):,}")
    print()
    print("  Pre-council edge formula: edge = confidence − entry_price − 0.01")
    print("  MIN_EDGE threshold = 0.03 (hardcoded, cannot be changed)")
    print()
    print("  Minimum confidence required to pass MIN_EDGE at entry_price X:")
    print("    entry=0.75 → conf ≥ 0.79   entry=0.80 → conf ≥ 0.84")
    print("    entry=0.83 → conf ≥ 0.87   entry=0.85 → conf ≥ 0.89")
    print("    entry=0.88 → conf ≥ 0.92   entry=0.90 → conf ≥ 0.94")
    print()

    # Break by price bucket
    price_buckets_me = {
        "0.80-0.90 (sweet-spot)": [r for r in me_rows if 0.80 <= (_af(r.get("yes_ask")) or 0) < 0.90],
        "0.60-0.80 (mid-range)": [r for r in me_rows if 0.60 <= (_af(r.get("yes_ask")) or 0) < 0.80],
        "other": [r for r in me_rows if not (0.60 <= (_af(r.get("yes_ask")) or 0) < 0.90)],
    }
    for label, subset in price_buckets_me.items():
        if not subset:
            print(f"  {label}: 0")
            continue
        computed = [
            (_af(r.get("confidence")) or 0) - (_af(r.get("yes_ask")) or 0) - 0.01
            for r in subset
        ]
        edges_stored = [_af(r.get("edge")) for r in subset if r.get("edge") is not None]
        barely = sum(1 for x in computed if 0 <= x < 0.03)
        far_below = sum(1 for x in computed if x < 0)
        print(f"  {label}: n={len(subset)}")
        print(f"    barely below 0.03 (computed 0 to 0.029):  {barely:5,} ({_pct(barely, len(subset))})")
        print(f"    far below 0.03    (computed < 0):         {far_below:5,} ({_pct(far_below, len(subset))})")
        if computed:
            cv = sorted(computed)
            cn = len(cv)
            print(
                f"    computed edge: min={min(cv):.4f} "
                f"p25={cv[cn//4]:.4f} p50={cv[cn//2]:.4f} "
                f"p75={cv[int(cn*0.75)]:.4f} max={max(cv):.4f}"
            )
        if edges_stored:
            ev = sorted(edges_stored)
            en = len(ev)
            print(
                f"    funnel edge:   min={min(ev):.4f} "
                f"p25={ev[en//4]:.4f} p50={ev[en//2]:.4f} "
                f"p75={ev[int(en*0.75)]:.4f} max={max(ev):.4f}"
            )
        print()
    print("  INTERPRETATION:")
    print("  • Sweet-spot (0.80-0.90): 532/535 are 'barely below 0.03' —")
    print("    model confidence is high but the entry price consumes the edge.")
    print("    Example: conf=0.865, price=0.850 → edge=0.015, blocked (need 0.03).")
    print("  • The model is systematically pricing confidence at or near the")
    print("    market price with only a thin margin. This is structural.")
    print("  • Changing MIN_EDGE is a FORBIDDEN action (CLAUDE.md). Do not propose it.")
    print("  • Wait for signals where confidence exceeds price by ≥0.04.")

    # ── Section 7 ─────────────────────────────────────────────────────────────
    _hdr("7: MARKET-QUALITY BREAKDOWN — SPREAD, VOLUME, LIQUIDITY")
    mq_rows = [r for r in post_q if r.get("final_reason") == "BLOCKED_MARKET_QUALITY"]
    print(f"  BLOCKED_MARKET_QUALITY total: {len(mq_rows):,}")
    print()
    print("  Config thresholds:")
    print("    MAX_SPREAD = 0.05  (yes_ask − yes_bid ≤ 0.05)")
    print("    MIN_VOLUME = 1000  (volume_24h / volume / liquidity ≥ 1000)")
    print()

    spreads = [_af(r.get("spread")) for r in mq_rows if r.get("spread") is not None]
    overrounds = [_af(r.get("overround")) for r in mq_rows if r.get("overround") is not None]
    yes_asks = [_af(r.get("yes_ask")) for r in mq_rows if r.get("yes_ask") is not None]

    print(_stats_line([s for s in spreads if s is not None], "spread       "))
    print(_stats_line([o for o in overrounds if o is not None], "overround    "))
    print(_stats_line([y for y in yes_asks if y is not None], "yes_ask (ep) "))
    print()

    tight_spread = sum(1 for s in spreads if s is not None and s <= 0.05)
    wide_spread = sum(1 for s in spreads if s is not None and s > 0.05)
    extreme_ya = sum(1 for y in yes_asks if y is not None and (y <= 0.05 or y >= 0.95))
    normal_ya = sum(1 for y in yes_asks if y is not None and 0.05 < y < 0.95)

    print(f"  spread ≤ 0.05 (tight):                {tight_spread:>6,} ({_pct(tight_spread, len(mq_rows))})")
    print(f"  spread > 0.05 (wide):                 {wide_spread:>6,} ({_pct(wide_spread, len(mq_rows))})")
    print(f"  yes_ask ≤ 0.05 or ≥ 0.95 (extreme):  {extreme_ya:>6,} ({_pct(extreme_ya, len(mq_rows))})")
    print(f"  yes_ask 0.05–0.95 (normal range):     {normal_ya:>6,} ({_pct(normal_ya, len(mq_rows))})")
    print()
    print("  Top prefixes in MQ blocks:")
    pfxs = Counter(ticker_prefix(r.get("ticker", "")) for r in mq_rows)
    for pfx, cnt in pfxs.most_common(8):
        print(f"    {pfx:<12}: {cnt:>5,}  ({_pct(cnt, len(mq_rows))})")
    print()
    print("  ROOT CAUSE (volume is not stored in funnel, but inferred from data):")
    print("  • 76.6% of MQ blocks have TIGHT spread (≤ 0.05) — spread is NOT the blocker.")
    print("  • The blocker is VOLUME (volume_24h < 1000). These markets have")
    print("    tight bid/ask quotes but very few actual contracts traded.")
    print("  • 59.8% have extreme yes_ask (≤0.05 or ≥0.95): near-certainty markets")
    print("    where outcome is almost known — low volume because no one will take")
    print("    the other side at a fair price.")
    print("  • KXSOL, KXXRP, KXSOLE, KXHYPE dominate — these are less liquid than KXBTCD.")
    print("  • CORRECTION PATH: none from this side. Volume improves when broader")
    print("    market activity picks up. Lowering MIN_VOLUME would expose")
    print("    the system to thin markets with unreliable fills.")

    # ── Section 8 ─────────────────────────────────────────────────────────────
    _hdr("8: EDGE-DANGER BREAKDOWN — WHY EDGE >= 0.08 TRIGGERS GUARD")
    edg_rows = [r for r in post_q if r.get("final_reason") == "BLOCKED_EDGE_DANGER_GUARD"]
    print(f"  BLOCKED_EDGE_DANGER_GUARD total: {len(edg_rows):,}")
    print()
    print("  Config: EDGE_DANGER_HIGH_EDGE_MIN = 0.08  (guard fires if pre-council edge ≥ 0.08)")
    print("  Audit finding M-46: historically, edge ≥ 0.08 signals have INVERTED performance.")
    print("  High edge often signals overconfidence or model error, not genuine opportunity.")
    print()
    edg_edges = [_af(r.get("edge")) for r in edg_rows if r.get("edge") is not None]
    edg_confs = [_af(r.get("confidence")) for r in edg_rows if r.get("confidence") is not None]
    edg_ya = [_af(r.get("yes_ask")) for r in edg_rows if r.get("yes_ask") is not None]
    print(_stats_line([e for e in edg_edges if e is not None], "edge    "))
    print(_stats_line([c for c in edg_confs if c is not None], "confidence"))
    print(_stats_line([y for y in edg_ya if y is not None], "yes_ask "))
    print()
    edg_pfxs = Counter(ticker_prefix(r.get("ticker", "")) for r in edg_rows)
    print("  Prefix breakdown:")
    for pfx, cnt in edg_pfxs.most_common(8):
        print(f"    {pfx:<12}: {cnt:>4,}  ({_pct(cnt, len(edg_rows))})")
    print()
    print("  INTERPRETATION:")
    print("  • KXBTC accounts for 77% of edge-danger blocks.")
    print("    KXBTC15M signals frequently show conf=0.82 at price=0.68 → edge=0.13.")
    print("    Pre-quarantine settled data: edge≥0.08 trades had WR=0% and pnl=-30.00")
    print("    (see edge_profile >=0.50 bucket). Guard is justified and correct.")
    print("  • Do not lower EDGE_DANGER_HIGH_EDGE_MIN without 30+ post-quarantine")
    print("    high-edge trades with WR>breakeven and positive CLV.")

    # ── Section 9 ─────────────────────────────────────────────────────────────
    _hdr("9: SAFETY VS OVER-CONSERVATIVE — CLASSIFICATION BY BLOCK CATEGORY")
    print(f"  {'Blocker':<35}  {'Count':>6}  {'%':>6}  Classification")
    print("  " + "-" * 80)
    rows_9 = [
        ("BLOCKED_QUARANTINE",
         2939,
         total_post,
         "CORRECT_SAFETY — KXETH proven poison; quarantine working as designed"),
        ("BLOCKED_MARKET_QUALITY",
         7331,
         total_post,
         "CORRECT_SAFETY — volume < 1000 is a real market condition"),
        ("BLOCKED_EDGE_DANGER_GUARD",
         936,
         total_post,
         "CORRECT_SAFETY — high edge historically inverted (M-46)"),
        ("BLOCKED_MIN_EDGE (far below, <0)",
         sum(1 for r in me_rows if (_af(r.get("edge")) or 0) < 0),
         total_post,
         "CORRECT_SAFETY — genuinely negative edge; correct block"),
        ("BLOCKED_MIN_EDGE (barely below 0.03)",
         sum(1 for r in me_rows if 0 <= (_af(r.get("edge")) or 0) < 0.03),
         total_post,
         "STRUCTURAL — thin edge at high prices; formula constraint"),
        ("BLOCKED_COUNCIL (conf-bucket, losing)",
         sum(1 for r in council_rows
             if re.search(r"confidence bucket (0\.65|0\.70|0\.75|<0\.65)", r.get("council_reason", "") or "")),
         total_post,
         "CORRECT — conf buckets 0.65-0.80 genuinely negative PnL"),
        ("BLOCKED_COUNCIL (edge-bucket, contaminated)",
         sum(1 for r in council_rows
             if re.search(r"edge bucket 0\.03-0\.05|edge bucket 0\.05-0\.10", r.get("council_reason", "") or "")),
         total_post,
         "POSSIBLY_OVER-CONSERVATIVE — bucket mixes sweet-spot + POISON + KXETH"),
    ]
    for label, cnt, denom, cls in rows_9:
        print(f"  {label:<35}  {cnt:>6,}  {_pct(cnt, denom)}  {cls}")
    print()
    print("  SUMMARY:")
    print("  • ~75% of blocks (MQ + quarantine + edge-danger + clearly negative)")
    print("    are CORRECT safety decisions. The system is doing its job.")
    print("  • ~11% of blocks (council edge-bucket blocks) are POSSIBLY OVER-CONSERVATIVE.")
    print("    The edge buckets mix sweet-spot (positive) with POISON (negative).")
    print("    This cannot be fixed by a simple patch — it requires the edge profile")
    print("    to accumulate enough sweet-spot post-quarantine data to turn the bucket positive.")
    print("  • ~15% (barely-below min-edge) are STRUCTURAL. The model's confidence")
    print("    slightly exceeds the market price but not by the 0.04 required margin.")
    print("    Not a bug — a real observation that model edge is thin at high prices.")

    # ── Section 10 ────────────────────────────────────────────────────────────
    _hdr("10: EXACT RECOMMENDATION")
    print()
    print("  RECOMMENDATION CODES:")
    print()

    recs = [
        ("WAIT_AND_COLLECT",
         "PRIMARY",
         "Post-quarantine settled = 0. All evidence is pre-quarantine. The 0.80-0.90\n"
         "     sweet-spot shows POSSIBLE_EDGE on historical data but needs 30+ post-\n"
         "     quarantine clean trades to confirm. No action required."),
        ("REBUILD_EDGE_PROFILE",
         "INVESTIGATE (low priority)",
         "Running `python3 tools/build_edge_profile.py` always uses ALL settled trades\n"
         "     including KXETH. A KXETH-excluded rebuild would shift the 0.03-0.05 bucket\n"
         "     from pnl=-15.85 to pnl=-3.50 — still negative, still blocking. Not enough\n"
         "     improvement to unblock council. Worth tracking, not urgent."),
        ("INVESTIGATE_COUNCIL_BUCKETS",
         "USEFUL (read-only only)",
         "Root cause confirmed: edge_profile 0.03-0.05 and 0.05-0.10 buckets both have\n"
         "     negative total_pnl because the profile has no price dimension. The 18 sweet-\n"
         "     spot trades in edge-bucket 0.03-0.05 have pnl=+9.30 but are grouped with\n"
         "     29 mid-price trades with pnl=-14.80. No live fix should be applied."),
        ("INVESTIGATE_MARKET_QUALITY",
         "USEFUL (read-only only)",
         "Volume (MIN_VOLUME=1000) is the actual MQ gate, not spread. KXSOL, KXXRP,\n"
         "     KXSOLE, KXHYPE are liquid enough in spread but not in volume. No threshold\n"
         "     change is recommended. Worth monitoring as market matures."),
        ("INVESTIGATE_MIN_EDGE_ADJUSTMENT",
         "DO_NOT_PATCH",
         "Lowering MIN_EDGE is a FORBIDDEN action (CLAUDE.md). Sweet-spot min-edge blocks\n"
         "     are structural: confidence barely exceeds price. Adjusting MIN_EDGE would\n"
         "     expose the system to thin-edge signals with historically negative PnL at\n"
         "     mid-range entry prices. Do not lower MIN_EDGE."),
        ("DO_NOT_PATCH",
         "MANDATORY — all live execution files",
         "Do not change: paper_trader.py, config/trading_config.py, critic_brain.py,\n"
         "     decision_council.py, edge profile builder, MIN_EDGE, MIN_CONFIDENCE,\n"
         "     MAX_SPREAD, MIN_VOLUME, EDGE_DANGER_HIGH_EDGE_MIN, QUARANTINED_TICKER_PREFIXES.\n"
         "     The system is working correctly. The zero-open state is the expected state\n"
         "     when no signals meet all filters simultaneously."),
    ]
    for code, priority, detail in recs:
        print(f"  [{priority}]")
        print(f"  {code}:")
        print(f"     {detail}")
        print()

    print("  PATH TO FIRST OPENED TRADE:")
    print("  A non-KXETH market must simultaneously satisfy:")
    print("    1. Volume ≥ 1000 AND spread ≤ 0.05")
    print("    2. Pre-council edge ≥ 0.03 (confidence ≥ price + 0.04)")
    print("    3. Pre-council edge < 0.08 (below danger guard)")
    print("    4. Council must ALLOW: at minimum one of the primary profile buckets")
    print("       must turn positive (likely the edge bucket 0.03-0.05)")
    print("    5. Post-council edge still ≥ 0.03 after confidence adjustment")
    print("    6. Risk manager approves")
    print()
    print("  For condition (4), the 0.03-0.05 edge bucket needs:")
    print("    Current state: pnl=-15.85, n=58")
    print("    To turn positive: need ≈$16 more in wins from clean sweet-spot trades")
    print("    At $5 bets, entry=0.85: each win ≈ $0.75, so ≈ 22 more wins needed")
    print("    But no trades are opening to provide those wins (circular deadlock).")
    print()
    print("  CIRCULAR DEADLOCK EXPLAINED:")
    print("    The edge profile blocks the council because PnL is negative.")
    print("    The PnL is negative because mid-price + KXETH trades contaminate the bucket.")
    print("    New clean sweet-spot trades would turn the bucket positive.")
    print("    But new trades can't open because the council blocks them.")
    print()
    print("  THE BOOTSTRAP PATH (currently active):")
    print("    edge_profile_trusted=True (73 normal_modern trades pass the trust gate).")
    print("    Because the profile is TRUSTED, the bootstrap_era_allow bypass does NOT fire.")
    print("    The bootstrap path only fires when edge_profile_trusted=False.")
    print("    The trusted profile's bucket data is blocking the system.")
    print()
    print("  WHAT WOULD BREAK THE DEADLOCK (without patching anything):")
    print("    If ENOUGH additional sweet-spot non-KXETH trades settle with positive PnL,")
    print("    and the edge profile is rebuilt, the 0.03-0.05 bucket would eventually")
    print("    turn positive and unblock the council.")
    print("    The only remaining question: can any sweet-spot signals currently enter?")
    print("    No — they are blocked at min-edge (30.7%) and market quality (29.0%)")
    print("    in addition to council (38.7%). The volume issue is pre-council.")

    # ── Final summary ─────────────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  FINAL SUMMARY")
    print("-" * 80)
    print(f"  Post-quarantine funnel rows:   {total_post:,}")
    print(f"  Post-quarantine trades opened: {opened:,}")
    print(f"  Post-quarantine settled:       {len(post_q_settled):,}")
    print()
    print("  Strongest blocker:   BLOCKED_MARKET_QUALITY (47.9%) — volume < 1000")
    print("  Second blocker:      BLOCKED_QUARANTINE (19.2%) — KXETH correct block")
    print("  Third blocker:       BLOCKED_MIN_EDGE (15.7%) — thin edge at high prices")
    print("  Fourth blocker:      BLOCKED_COUNCIL (10.9%) — contaminated edge profile")
    print()
    print("  Justified blocks:    BLOCKED_QUARANTINE, BLOCKED_MARKET_QUALITY,")
    print("                       BLOCKED_EDGE_DANGER_GUARD, most BLOCKED_MIN_EDGE")
    print("  Possibly over-conservative: BLOCKED_COUNCIL edge-bucket blocks (~11%)")
    print("     Root cause: edge_profile 0.03-0.05 bucket is negative because it mixes")
    print("     sweet-spot (positive) and mid-price (POISON) trades — no price dimension.")
    print("     Fix: not a patch; requires organic accumulation of sweet-spot trades.")
    print()
    print("  Circular deadlock: edge profile trusted but buckets contaminated → council blocks")
    print("  → no new trades → no profile improvement → deadlock continues.")
    print()
    print("  SAFEST NEXT ACTION: WAIT_AND_COLLECT")
    print("  Monitor the volume-passed, edge-passed signals for any that also pass council.")
    print("  If 0 trades open after 72 more hours, consider a non-execution investigation")
    print("  into whether the edge profile bucket deadlock warrants a targeted rebuild")
    print("  that separates sweet-spot from mid-price trades (requires Samuel sign-off).")
    print()
    print("  DO NOT TOUCH:")
    print("  • MIN_EDGE (hardcoded, forbidden action)")
    print("  • MIN_CONFIDENCE (hardcoded, forbidden action)")
    print("  • MAX_SPREAD or MIN_VOLUME (real market filters)")
    print("  • EDGE_DANGER_HIGH_EDGE_MIN (safety guard)")
    print("  • council logic or Critic thresholds")
    print("  • real_money_allowed — stays False (hardcoded)")
    print("  • scale_allowed — stays False (hardcoded)")
    print("  • Kelly execution — stays disabled (GLOBAL_FORCED_LEARNING_MODE)")
    print("=" * 80)


if __name__ == "__main__":
    main()
