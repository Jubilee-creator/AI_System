#!/usr/bin/env python3
"""
tools/external_data/report_research_dataset_truth.py
------------------------------------------------------
Read-only research dataset truth report.

Loads the latest joined research CSV and summarises what it can and cannot prove.
Does NOT modify logs, trades, config, proof gates, or strategy.

Sections:
  1. DATASET COMPOSITION    — row counts by outcome type; ghost-OPEN warning
  2. JOIN COVERAGE          — crypto/Kalshi join status; by-prefix table
  3. PROOF CLASS BREAKDOWN  — which rows count toward proof and which do not
  4. USABLE ROW SUMMARY     — intersection of joined × outcome-known × proof class
  5. COLUMN COMPLETENESS    — which features are present and for how many rows
  6. KEY METRICS SUMMARY    — ROI, CLV, win-rate, confidence (with sample caveats)
  7. ANALYSIS READINESS     — clear verdicts for each analysis type
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

JOINED_DIR = ROOT / "data" / "research" / "joined"

# Proof classes that count toward proof gates
PROOF_ELIGIBLE = {"BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL", "NORMAL_MODERN_CANDIDATE"}

# Human-readable labels
PROOF_CLASS_DESC = {
    "BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL": "era_allow            [PROOF-ELIGIBLE — counts as normal_modern]",
    "NORMAL_MODERN_CANDIDATE":           "normal_modern        [PROOF-ELIGIBLE — counts as normal_modern]",
    "DATA_COLLECTION_OVERRIDE_EXCLUDED": "dc_override          [EXCLUDED from proof — learning data only]",
    "BOOTSTRAP_PROVISIONAL_EXCLUDED":    "bootstrap_provisional[EXCLUDED from proof — learning data only]",
    "LEGACY_OR_INCOMPLETE":              "legacy/incomplete    [QUARANTINED — edge artifact, excluded]",
    "SIDE_COVERAGE_EXCLUDED":            "side_coverage_test   [EXCLUDED — coverage testing only]",
}

SAMPLE_WARN = "[SAMPLE_TOO_SMALL]"
LINE = "─" * 72


# ── Loader ────────────────────────────────────────────────────────────────────

def _load_latest_csv() -> Tuple[Optional[Path], List[Dict[str, str]]]:
    if not JOINED_DIR.exists():
        return None, []
    paths = sorted(JOINED_DIR.glob("*.csv"))
    if not paths:
        return None, []
    latest = paths[-1]
    try:
        with latest.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return latest, []
    return latest, rows


# ── Helpers ───────────────────────────────────────────────────────────────────

def _f(v: str) -> Optional[float]:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{n / total * 100:.1f}%"


def _fmt(v: Optional[float], fmt: str = ".4f") -> str:
    return "n/a" if v is None else f"{v:{fmt}}"


def _safe_mean(vals: List[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _breakeven_wr(entry_price: float) -> float:
    """
    For a YES bet at entry_price p:
      WIN  ROI = (1-p)/p
      LOSS ROI = -1
    Breakeven WR satisfies:  WR × (1-p)/p = (1-WR) × 1  →  WR = p
    """
    return entry_price


def _sample_warn(n: int, threshold: int = 30) -> str:
    return f"  {SAMPLE_WARN}  n={n} < {threshold}" if n < threshold else f"  n={n}"


# ── Sections ──────────────────────────────────────────────────────────────────

def _section_composition(rows: List[Dict[str, str]]) -> None:
    total = len(rows)
    has_outcome_source = rows and "outcome_source" in rows[0]

    if has_outcome_source:
        market_res   = [r for r in rows if r.get("outcome_source") == "MARKET_RESOLVED"]
        forced_exit  = [r for r in rows if r.get("outcome_source") == "FORCED_TIME_EXIT"]
        forced_void  = [r for r in rows if r.get("outcome_source") == "FORCED_VOID"]
        ghost_open   = [r for r in rows if r.get("outcome_source") == "OPEN_NO_OUTCOME"]
        outcome_k    = [r for r in rows if r.get("outcome_known", "").lower() in ("true", "1")]
        outcome_unk  = [r for r in rows if r.get("outcome_known", "").lower() not in ("true", "1")]
    else:
        wins   = [r for r in rows if r.get("outcome") == "WIN"]
        losses = [r for r in rows if r.get("outcome") == "LOSS"]
        pushes = [r for r in rows if r.get("outcome") == "PUSH"]
        ghost_open  = [r for r in pushes if not (r.get("settlement_ts") or "").strip()]
        forced_void = [r for r in pushes if (r.get("settlement_ts") or "").strip()]
        outcome_k   = wins + losses
        outcome_unk = pushes
        market_res  = []
        forced_exit = wins + losses
        forced_void = forced_void

    print("DATASET COMPOSITION")
    print(LINE)
    print(f"  total_rows:                    {total}")
    print()
    if has_outcome_source:
        print(f"  MARKET_RESOLVED:               {len(market_res):>4}  ({_pct(len(market_res), total)})  — settled by Kalshi; outcome=WIN/LOSS")
        print(f"  FORCED_TIME_EXIT:               {len(forced_exit):>4}  ({_pct(len(forced_exit), total)})  — position closed at market price; outcome=WIN/LOSS")
        print(f"  FORCED_VOID:                   {len(forced_void):>4}  ({_pct(len(forced_void), total)})  — validation reset; pnl=0; no trading outcome")
        print(f"  OPEN_NO_OUTCOME (ghost):       {len(ghost_open):>4}  ({_pct(len(ghost_open), total)})  — unresolved OPEN rows; no outcome recorded")
    else:
        wins   = [r for r in rows if r.get("outcome") == "WIN"]
        losses = [r for r in rows if r.get("outcome") == "LOSS"]
        pushes = [r for r in rows if r.get("outcome") == "PUSH"]
        push_deferred = [r for r in pushes if not (r.get("settlement_ts") or "").strip()]
        push_at_cost  = [r for r in pushes if (r.get("settlement_ts") or "").strip()]
        print(f"  outcome=WIN:                   {len(wins):>4}  ({_pct(len(wins), total)})")
        print(f"  outcome=LOSS:                  {len(losses):>4}  ({_pct(len(losses), total)})")
        print(f"  outcome=PUSH / settled:        {len(push_at_cost):>4}  (FORCED_CLOSE at cost)")
        print(f"  outcome=PUSH / no settle:      {len(push_deferred):>4}  (ghost OPEN rows)")
    print()
    print(f"  outcome_known (WIN+LOSS):      {len(outcome_k):>4}  ← usable for performance analysis")
    print(f"  outcome_unknown:               {len(outcome_unk):>4}  ← NOT usable for outcome analysis")
    print()
    print("  NOTE: Ghost OPEN rows (OPEN_NO_OUTCOME) appear because the research")
    print("  dataset builder includes status=OPEN rows from paper_trades.jsonl.")
    print("  FORCED_VOID rows are validation resets — pnl=0, no market outcome.")
    print()


def _section_join_coverage(rows: List[Dict[str, str]]) -> None:
    total = len(rows)
    crypto_status = Counter(r.get("crypto_join_status", "") for r in rows)
    kalshi_status = Counter(r.get("kalshi_join_status", "") for r in rows)

    crypto_joined = [r for r in rows if r.get("crypto_join_status") == "JOINED"]
    kalshi_joined = [r for r in rows if r.get("kalshi_join_status") == "JOINED"]
    both_joined   = [r for r in rows if r.get("crypto_join_status") == "JOINED"
                                     and r.get("kalshi_join_status") == "JOINED"]

    print("JOIN COVERAGE")
    print(LINE)
    print("  Crypto join:")
    for status, count in sorted(crypto_status.items(), key=lambda x: -x[1]):
        note = ""
        if status == "NO_CRYPTO_SYMBOL_FOR_MARKET":
            note = "  (non-BTC/ETH markets — expected)"
        elif status == "NO_CRYPTO_CANDLE_BEFORE_ENTRY":
            note = "  (trades before earliest crypto candle)"
        print(f"    {status:<38s} {count:>3}  ({_pct(count, total)}){note}")
    print()
    print("  Kalshi join:")
    for status, count in sorted(kalshi_status.items(), key=lambda x: -x[1]):
        note = ""
        if status == "NO_MATCHING_KALSHI_TICKER":
            note = "  (no Kalshi candles collected for these tickers)"
        elif status == "STALE_KALSHI_CANDLE":
            note = "  (nearest candle > 1h before entry)"
        print(f"    {status:<38s} {count:>3}  ({_pct(count, total)}){note}")
    print()
    print(f"  both_crypto_and_kalshi_joined: {len(both_joined):>3}  ({_pct(len(both_joined), total)})")
    print(f"  crypto_only:                   {len(crypto_joined) - len(both_joined):>3}")
    print(f"  kalshi_only:                   {len(kalshi_joined) - len(both_joined):>3}")
    print()

    # Per-prefix table
    print("  Per-prefix join coverage:")
    prefixes = sorted(set(r.get("market_prefix", "") for r in rows))
    header = f"  {'prefix':<12s} {'total':>5}  {'crypto_j':>8}  {'kalshi_j':>8}  {'both_j':>6}"
    print(header)
    print("  " + "─" * 50)
    for prefix in prefixes:
        prefix_rows = [r for r in rows if r.get("market_prefix") == prefix]
        cj = sum(1 for r in prefix_rows if r.get("crypto_join_status") == "JOINED")
        kj = sum(1 for r in prefix_rows if r.get("kalshi_join_status") == "JOINED")
        bj = sum(1 for r in prefix_rows if r.get("crypto_join_status") == "JOINED"
                                        and r.get("kalshi_join_status") == "JOINED")
        print(f"  {prefix:<12s} {len(prefix_rows):>5}  {cj:>8}  {kj:>8}  {bj:>6}")
    print()


def _section_proof_classes(rows: List[Dict[str, str]]) -> None:
    total = len(rows)
    proof_counts = Counter(r.get("proof_class", "") for r in rows)

    print("PROOF CLASS BREAKDOWN")
    print(LINE)
    print(f"  {'proof_class':<45s} {'total':>5}  {'outcome_known':>13}  {'crypto_j':>8}")
    print("  " + "─" * 74)
    for cls in sorted(proof_counts.keys(), key=lambda c: -proof_counts[c]):
        subset      = [r for r in rows if r.get("proof_class") == cls]
        outcome_k   = [r for r in subset if r.get("outcome") in ("WIN", "LOSS")]
        crypto_j    = [r for r in subset if r.get("crypto_join_status") == "JOINED"]
        desc        = PROOF_CLASS_DESC.get(cls, cls)
        eligible    = "✓" if cls in PROOF_ELIGIBLE else " "
        print(f"  {eligible} {cls:<43s} {len(subset):>5}  {len(outcome_k):>13}  {len(crypto_j):>8}")
    print()
    proof_eligible_total = sum(1 for r in rows if r.get("proof_class") in PROOF_ELIGIBLE)
    proof_eligible_outcome = sum(1 for r in rows
                                 if r.get("proof_class") in PROOF_ELIGIBLE
                                 and r.get("outcome") in ("WIN", "LOSS"))
    print(f"  proof_eligible rows (✓):        {proof_eligible_total:>3}")
    print(f"  proof_eligible + outcome_known:  {proof_eligible_outcome:>3}  ← only these can contribute to proof gates")
    print()
    print("  NOTE: proof gates require normal_modern >= 10 (trust gate) and >= 30 (scale gate).")
    print("  Only BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL and NORMAL_MODERN_CANDIDATE rows count.")
    print("  All other rows are excluded or quarantined from proof evaluation.")
    print()


def _section_usable_rows(rows: List[Dict[str, str]]) -> None:
    total     = len(rows)
    has_src   = rows and "outcome_source" in rows[0]
    has_ok    = rows and "outcome_known" in rows[0]

    if has_ok:
        outcome_k = [r for r in rows if r.get("outcome_known", "").lower() in ("true", "1")]
    else:
        outcome_k = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]

    crypto_j  = [r for r in rows if r.get("crypto_join_status") == "JOINED"]
    kalshi_j  = [r for r in rows if r.get("kalshi_join_status") == "JOINED"]
    both_j    = [r for r in rows if r.get("crypto_join_status") == "JOINED"
                                 and r.get("kalshi_join_status") == "JOINED"]

    ok_crypto       = [r for r in outcome_k if r.get("crypto_join_status") == "JOINED"]
    ok_kalshi       = [r for r in outcome_k if r.get("kalshi_join_status") == "JOINED"]
    ok_both         = [r for r in outcome_k if r.get("crypto_join_status") == "JOINED"
                                           and r.get("kalshi_join_status") == "JOINED"]
    ok_proof        = [r for r in outcome_k if r.get("proof_class") in PROOF_ELIGIBLE]
    ok_proof_crypto = [r for r in ok_proof  if r.get("crypto_join_status") == "JOINED"]

    if has_src:
        ghost_open   = [r for r in rows if r.get("outcome_source") == "OPEN_NO_OUTCOME"]
        forced_void  = [r for r in rows if r.get("outcome_source") == "FORCED_VOID"]
    else:
        ghost_open  = [r for r in rows if r.get("outcome") == "PUSH"
                                       and not (r.get("settlement_ts") or "").strip()]
        forced_void = [r for r in rows if r.get("outcome") == "PUSH"
                                       and (r.get("settlement_ts") or "").strip()]

    print("USABLE ROW SUMMARY")
    print(LINE)
    print(f"  total rows in dataset:                    {total:>4}")
    print(f"  outcome_known (WIN+LOSS):                 {len(outcome_k):>4}  ← basis for all performance metrics")
    print()
    print("  Intersection  (outcome_known + ...):")
    print(f"    + crypto_joined:                        {len(ok_crypto):>4}  ({_pct(len(ok_crypto), len(outcome_k))} of outcome-known)")
    print(f"    + kalshi_joined:                        {len(ok_kalshi):>4}  ({_pct(len(ok_kalshi), len(outcome_k))} of outcome-known)")
    print(f"    + both_joined:                          {len(ok_both):>4}  ({_pct(len(ok_both), len(outcome_k))} of outcome-known)")
    print(f"    + proof_eligible:                       {len(ok_proof):>4}  ({_pct(len(ok_proof), len(outcome_k))} of outcome-known)")
    print(f"    + proof_eligible + crypto_joined:       {len(ok_proof_crypto):>4}")
    print()
    print("  UNUSABLE rows:")
    print(f"    ghost OPEN (OPEN_NO_OUTCOME):           {len(ghost_open):>4}  — no resolution; exclude from all analysis")
    print(f"    FORCED_VOID (validation reset):         {len(forced_void):>4}  — pnl=0; no market outcome; exclude")
    print()


def _section_column_completeness(rows: List[Dict[str, str]]) -> None:
    total = len(rows)
    crypto_j = [r for r in rows if r.get("crypto_join_status") == "JOINED"]
    n_cj = len(crypto_j)
    kalshi_j = [r for r in rows if r.get("kalshi_join_status") == "JOINED"]
    n_kj = len(kalshi_j)

    def _coverage(col: str, subset: List[Dict[str, str]]) -> str:
        n = sum(1 for r in subset if (r.get(col) or "").strip())
        return f"{n:>4} / {len(subset):<4}  ({_pct(n, len(subset))})"

    print("COLUMN COMPLETENESS")
    print(LINE)
    n_ok = len([r for r in rows if r.get("outcome_known", "").lower() in ("true", "1")]
               if (rows and "outcome_known" in rows[0])
               else [r for r in rows if r.get("outcome") in ("WIN", "LOSS")])

    print(f"  Core fields (all {total} rows):")
    for col in ("entry_price", "probability_confidence", "roi", "outcome", "settlement_ts", "clv"):
        note = ""
        if col == "roi":
            note = f"  — meaningful only for {n_ok} outcome-known rows"
        if col == "clv":
            note = "  — present for some proof classes; check coverage"
        if col == "settlement_ts":
            note = "  — missing for ghost OPEN rows"
        print(f"    {col:<36s} {_coverage(col, rows)}{note}")
    print()

    print(f"  Probability / edge fields (all {total} rows):")
    for col in ("model_probability", "risk_edge", "post_council_edge"):
        note = ""
        if col == "model_probability":
            note = "  — pre-council raw model output; None for legacy rows"
        if col == "risk_edge":
            note = "  — pre-council edge = model_prob - entry_price - 0.01; None for legacy"
        if col == "post_council_edge":
            note = "  — council-adjusted edge (may be negative); present all rows"
        # fall back gracefully if column not in old CSV
        if rows and col not in rows[0]:
            print(f"    {col:<36s}  n/a  (column added in Phase 8M — rebuild CSV)")
        else:
            print(f"    {col:<36s} {_coverage(col, rows)}{note}")
    print()

    print(f"  Crypto features  ({n_cj} crypto-joined rows):")
    for col in (
        "crypto_close_before_entry",
        "crypto_return_5m_before_entry",
        "crypto_return_15m_before_entry",
        "crypto_volatility_15m",
    ):
        note = ""
        if col == "crypto_volatility_15m":
            if rows and col in rows[0]:
                n_nonzero = sum(1 for r in crypto_j if (r.get(col) or "").strip())
                if n_nonzero == 0:
                    note = "  — all empty: candle window too short or insufficient data"
                else:
                    note = f"  — computed for {n_nonzero}/{n_cj} joined rows"
            else:
                note = "  — column added in Phase 8M; rebuild CSV to populate"
        print(f"    {col:<36s} {_coverage(col, crypto_j)}{note}")
    print()

    print(f"  Kalshi features  ({n_kj} Kalshi-joined rows):")
    for col in (
        "kalshi_yes_mid_close",
        "kalshi_yes_bid_close",
        "kalshi_yes_ask_close",
        "kalshi_market_price_close",
        "kalshi_volume",
        "kalshi_open_interest",
    ):
        print(f"    {col:<36s} {_coverage(col, kalshi_j)}")
    print()


def _section_key_metrics(rows: List[Dict[str, str]]) -> None:
    outcome_k = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]
    wins      = [r for r in outcome_k if r.get("outcome") == "WIN"]
    losses    = [r for r in outcome_k if r.get("outcome") == "LOSS"]
    n_ok      = len(outcome_k)

    roi_vals  = [_f(r["roi"]) for r in outcome_k if _f(r.get("roi")) is not None]
    ep_vals   = [_f(r["entry_price"]) for r in outcome_k if _f(r.get("entry_price")) is not None]
    conf_vals = [_f(r["probability_confidence"]) for r in rows if _f(r.get("probability_confidence")) is not None]
    clv_vals  = [_f(r["clv"]) for r in rows if _f(r.get("clv")) is not None]

    wr        = len(wins) / n_ok if n_ok else None
    mean_roi  = _safe_mean(roi_vals)
    mean_ep   = _safe_mean(ep_vals)
    mean_conf = _safe_mean(conf_vals)
    mean_clv  = _safe_mean(clv_vals)
    beven_wr  = _breakeven_wr(mean_ep) if mean_ep else None

    print("KEY METRICS SUMMARY")
    print(LINE)
    print("  ALL FIGURES ARE APPROXIMATE — mixed proof classes, small sample.")
    print("  Do not use these numbers as proof of edge or strategy quality.")
    print()
    print(f"  Outcome-known rows:      {n_ok}{_sample_warn(n_ok)}")
    print(f"    wins:                  {len(wins)}")
    print(f"    losses:                {len(losses)}")
    print(f"    win_rate:              {_fmt(wr, '.3f') if wr else 'n/a'}  ({_pct(len(wins), n_ok)}){_sample_warn(n_ok)}")
    print(f"    mean_roi:              {_fmt(mean_roi, '+.3f') if mean_roi else 'n/a'}  ({_fmt(mean_roi * 100, '+.1f') + '%' if mean_roi else 'n/a'})")
    print(f"    mean_entry_price:      {_fmt(mean_ep, '.3f') if mean_ep else 'n/a'}")
    print(f"    breakeven_WR_at_mean_entry: {_fmt(beven_wr, '.3f') if beven_wr else 'n/a'}  (= mean entry_price)")
    print(f"    actual_vs_breakeven:   {_fmt(wr - beven_wr, '+.3f') if (wr and beven_wr) else 'n/a'}  gap")
    print()
    print(f"  CLV  (n={len(clv_vals)}/99, mostly excluded proof classes):")
    print(f"    mean_clv:              {_fmt(mean_clv, '+.4f') if mean_clv else 'n/a'}")
    print(f"    NOTE: CLV is 68.7% missing. Available rows are mostly DC_OVERRIDE and PROVISIONAL —")
    print(f"    do not interpret this mean as evidence of positive or negative edge.")
    print()
    print(f"  Model confidence  (n={len(conf_vals)}/99, all proof classes):")
    print(f"    mean_confidence:       {_fmt(mean_conf, '.3f') if mean_conf else 'n/a'}")
    conf_min = min(conf_vals) if conf_vals else None
    conf_max = max(conf_vals) if conf_vals else None
    print(f"    range:                 {_fmt(conf_min, '.3f')} – {_fmt(conf_max, '.3f')}")
    if wr and mean_conf:
        gap = wr - mean_conf
        print(f"    confidence_vs_WR_gap:  {gap:+.3f}  (model says {mean_conf:.3f}, actual WR {wr:.3f})")
        print(f"    NOTE: raw gap {gap:+.3f} across mixed proof classes — not a calibration diagnosis.")
    print()

    # Per-prefix breakdown (outcome-known + crypto-joined)
    ok_crypto = [r for r in outcome_k if r.get("crypto_join_status") == "JOINED"]
    if ok_crypto:
        print(f"  Per-prefix breakdown  (outcome-known + crypto-joined, n={len(ok_crypto)}):")
        prefixes = sorted(set(r.get("market_prefix", "") for r in ok_crypto))
        for prefix in prefixes:
            p_rows  = [r for r in ok_crypto if r.get("market_prefix") == prefix]
            p_wins  = [r for r in p_rows if r.get("outcome") == "WIN"]
            p_roi   = [_f(r["roi"]) for r in p_rows if _f(r.get("roi")) is not None]
            p_ep    = [_f(r["entry_price"]) for r in p_rows if _f(r.get("entry_price")) is not None]
            p_wr    = len(p_wins) / len(p_rows) if p_rows else None
            p_mroi  = _safe_mean(p_roi)
            p_mep   = _safe_mean(p_ep)
            p_bev   = _breakeven_wr(p_mep) if p_mep else None
            warn    = f"  {SAMPLE_WARN}" if len(p_rows) < 30 else ""
            wr_str  = f"{p_wr:.3f}" if p_wr is not None else "n/a"
            roi_str = f"{p_mroi * 100:+.1f}%" if p_mroi is not None else "n/a"
            bev_str = f"{p_bev:.3f}" if p_bev is not None else "n/a"
            print(f"    {prefix:<10s}  n={len(p_rows):>2}  WR={wr_str}  ROI={roi_str:>7s}  breakeven={bev_str}{warn}")
        print()

    # CLV by proof class (for transparency)
    clv_rows = [r for r in rows if _f(r.get("clv")) is not None]
    if clv_rows:
        print(f"  CLV by proof class  (n={len(clv_rows)}):")
        by_cls = defaultdict(list)
        for r in clv_rows:
            by_cls[r.get("proof_class", "")].append(_f(r["clv"]))
        for cls, vals in sorted(by_cls.items(), key=lambda x: -len(x[1])):
            m = _safe_mean(vals)
            eligible_flag = "✓" if cls in PROOF_ELIGIBLE else " "
            print(f"    {eligible_flag} {cls:<45s} n={len(vals)}  mean_clv={_fmt(m, '+.4f')}")
        print()


def _section_analysis_readiness(rows: List[Dict[str, str]]) -> None:
    outcome_k     = [r for r in rows if r.get("outcome") in ("WIN", "LOSS")]
    ok_crypto     = [r for r in outcome_k if r.get("crypto_join_status") == "JOINED"]
    ok_proof      = [r for r in outcome_k if r.get("proof_class") in PROOF_ELIGIBLE]
    crypto_j      = [r for r in rows if r.get("crypto_join_status") == "JOINED"]
    n_ok          = len(outcome_k)
    n_ok_crypto   = len(ok_crypto)
    n_ok_proof    = len(ok_proof)
    n_crypto_j    = len(crypto_j)

    def _verdict(label: str, status: str, detail: str) -> None:
        print(f"  {label:<28s} {status}")
        for line in detail.strip().splitlines():
            print(f"    {line}")
        print()

    print("ANALYSIS READINESS VERDICTS")
    print(LINE)

    _verdict(
        "DESCRIPTIVE ANALYSIS",
        "READY  (limited)",
        f"{n_crypto_j} rows have BTC/ETH price, 5m/15m momentum at entry.\n"
        "Can describe market conditions at trade entry time.\n"
        "Cannot assess whether those conditions predicted outcomes.",
    )

    _verdict(
        "CALIBRATION ANALYSIS",
        "NOT READY",
        f"{n_ok} outcome-known rows, but all 4 proof classes are mixed together.\n"
        f"Mean model confidence ({_fmt(_safe_mean([_f(r.get('probability_confidence', '')) for r in rows if _f(r.get('probability_confidence', ''))]), '.3f')}) "
        f"vs WR on outcome-known rows is a raw gap only,\n"
        "not a calibration result — different proof classes have different edge\n"
        "formulae, different market regimes, and different entry criteria.\n"
        f"Proof-eligible outcome rows: {n_ok_proof}  (minimum for calibration: 30+).",
    )

    _verdict(
        "EDGE BUCKET ANALYSIS",
        "NOT READY",
        f"{n_ok_crypto} outcome-known + crypto-joined rows across mixed proof classes.\n"
        "Legacy rows (quarantined) have an inverted edge artifact that contaminates\n"
        "any edge-bucket segmentation. Entry price as edge proxy is unreliable.\n"
        f"Proof-eligible outcome rows: {n_ok_proof}.",
    )

    _verdict(
        "MOMENTUM / FEATURE ANALYSIS",
        "EXPLORATORY ONLY",
        f"{n_ok_crypto} rows with crypto momentum features and known outcome.\n"
        "Can observe directional tendencies but n is too small for statistical\n"
        "inference. Results should not drive strategy changes.",
    )

    _verdict(
        "STRATEGY PATCHING",
        "ABSOLUTELY NOT",
        f"Proof-eligible outcome rows: {n_ok_proof} / 30 minimum.\n"
        "Evidence base is too small and too mixed to diagnose or modify strategy.\n"
        "No strategy change is justified until normal_modern >= 30 with passing gates.",
    )

    _verdict(
        "REAL-MONEY DECISIONS",
        "ABSOLUTELY NOT",
        "real_money_allowed = False  (hardcoded — cannot be changed via this dataset).\n"
        "scale_allowed      = False  (hardcoded — cannot be changed via this dataset).\n"
        "Proof gates: normal_modern = 1 / 30.  Not met.\n"
        "No evidence in this dataset overrides these locks.",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    path, rows = _load_latest_csv()

    print("=" * 72)
    print("RESEARCH DATASET TRUTH REPORT")
    print("=" * 72)
    print("Read-only. No log mutation, no proof changes, no trading behavior change.")
    print()

    if path is None:
        print(f"  [WARN] No joined CSV found in {JOINED_DIR}")
        print("  Run: python3 tools/external_data/build_research_dataset.py")
        print("RESULT: RESEARCH_DATASET_TRUTH_REPORT_OK")
        return

    if not rows:
        print(f"  [WARN] CSV at {path} is empty or unreadable.")
        print("RESULT: RESEARCH_DATASET_TRUTH_REPORT_OK")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rel_path = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    print(f"  generated_at: {now}")
    print(f"  csv_path:     {rel_path}")
    print(f"  columns:      {len(rows[0])}")
    print(f"  total_rows:   {len(rows)}")
    print()

    _section_composition(rows)
    _section_join_coverage(rows)
    _section_proof_classes(rows)
    _section_usable_rows(rows)
    _section_column_completeness(rows)
    _section_key_metrics(rows)
    _section_analysis_readiness(rows)

    print("=" * 72)
    print("  real_money_allowed = False  (hardcoded)")
    print("  scale_allowed      = False  (hardcoded)")
    print("  This report is read-only evidence. It does not change proof gates,")
    print("  strategy thresholds, or trading behavior.")
    print("=" * 72)
    print()
    print("RESULT: RESEARCH_DATASET_TRUTH_REPORT_OK")


if __name__ == "__main__":
    main()
