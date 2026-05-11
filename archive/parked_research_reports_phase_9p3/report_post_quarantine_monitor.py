#!/usr/bin/env python3
"""
tools/report_post_quarantine_monitor.py
-----------------------------------------
Read-only post-quarantine performance monitor.

Separates OLD pre-quarantine history from NEW post-quarantine behavior
to answer: "After KXETH quarantine, are fresh non-KXETH trades improving,
or is the system still mathematically losing?"

Activation timestamp is detected automatically from the first
BLOCKED_QUARANTINE entry in logs/execution_funnel.jsonl.

Does NOT modify any config, threshold, log, or execution path.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import (
    build_terminal_key_sets,
    classify_open_records,
    classify_settled_records,
    load_trades,
    get_clv,
    get_pnl,
    get_size,
    is_side_coverage_record,
)
from tools.clean_truth_report import (
    evaluate_proof_gates,
    classify_records,
    is_time_exit,
    row_quality_group,
)
from config.trading_config import (
    QUARANTINED_TICKER_PREFIXES,
    MIN_EDGE,
    MIN_CONFIDENCE,
    GLOBAL_FORCED_LEARNING_MODE,
)

# Import shared helpers from autopsy to avoid duplication.
from tools.report_expectancy_autopsy import (
    Metrics,
    group_by,
    ticker_prefix,
    entry_price_bucket,
    equity_curve,
    max_drawdown,
    longest_losing_streak,
    _af,
    _avg,
    _pct,
    _num,
    _money,
    _now_utc,
    get_model_prob,
    ENTRY_PRICE_ORDER,
    EDGE_ORDER,
    PROB_ORDER,
    edge_bucket_fn,
    prob_bucket,
    SEP,
    SEP2,
)

TRADES_LOG   = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG   = ROOT / "logs" / "execution_funnel.jsonl"
PROFILE_PATH = ROOT / "data" / "edge_profile.json"
CONFIG_PATH  = ROOT / "config" / "trading_config.py"

# Proof gate thresholds (must match evaluate_proof_gates)
PROOF_N   = 30
PROOF_ROI = 0.0
PROOF_PF  = 1.10
PROOF_CLV = 0.0


# ─── helpers ─────────────────────────────────────────────────────────────────

def _open_ts(r: dict) -> str:
    """Trade open timestamp — NOT settlement time."""
    return str(r.get("timestamp_utc") or r.get("timestamp") or "")


def _settled_ts(r: dict) -> str:
    return str(r.get("settled_at") or r.get("timestamp_utc") or r.get("timestamp") or "")


def is_quarantined_row(r: dict) -> bool:
    """True if this trade's ticker is in QUARANTINED_TICKER_PREFIXES (mirrors paper_trader logic)."""
    t = str(r.get("ticker") or "").upper()
    return any(t.startswith(pfx.upper()) for pfx in QUARANTINED_TICKER_PREFIXES)


def _load_funnel_rows() -> List[dict]:
    if not FUNNEL_LOG.exists():
        return []
    rows = []
    for line in FUNNEL_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def _load_profile() -> dict:
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text())
    except Exception:
        return {}


def _gate_check(ok: bool, lbl: str, val: str) -> None:
    print(f"    [{'PASS' if ok else 'FAIL'}]  {lbl:<40}  {val}")


def _quarantine_rec(m: Metrics) -> str:
    if m.n < 3:
        return "TOO_SMALL"
    roi = m.roi if m.roi is not None else 0.0
    pf  = 0.0 if m.profit_factor is None else (999.0 if m.profit_factor == float("inf") else m.profit_factor)
    if roi <= -0.25 and pf < 0.50:
        return "HARD_QUARANTINE"
    if roi < 0.0 and pf < 1.0:
        return "WATCHLIST"
    return "KEEP_RESEARCH"


def sim_verdict(m: Metrics) -> str:
    if m.n == 0:
        return "NO_SAMPLE"
    if m.n < 3:
        return "TOO_SMALL"
    roi_ok = m.roi is not None and m.roi > PROOF_ROI
    pf_ok  = (
        m.profit_factor is not None
        and (m.profit_factor == float("inf") or m.profit_factor > PROOF_PF)
    )
    clv_ok = m.avg_clv is not None and m.avg_clv > PROOF_CLV
    n_ok   = m.n >= PROOF_N
    clearly_losing = (
        m.roi is not None and m.roi < 0
        and m.profit_factor is not None
        and m.profit_factor != float("inf")
        and m.profit_factor < 1.0
    )
    if roi_ok and pf_ok and clv_ok and n_ok:
        return "PROVEN_EDGE"
    if roi_ok and pf_ok and clv_ok and not n_ok:
        return "WATCHLIST"
    if roi_ok and not pf_ok and clv_ok:
        return "WATCHLIST"
    if clearly_losing:
        return "POISON"
    return "WATCHLIST"


# ─── Activation timestamp detection ──────────────────────────────────────────

def _detect_activation(funnel_rows: List[dict]) -> Optional[str]:
    """
    Return ISO-format activation timestamp as a string.
    Priority: first BLOCKED_QUARANTINE entry in execution_funnel.jsonl.
    Fallback: file mtime of config/trading_config.py.
    """
    bq_rows = [r for r in funnel_rows if r.get("final_reason") == "BLOCKED_QUARANTINE"]
    if bq_rows:
        first = min(bq_rows, key=lambda r: r.get("timestamp_utc", ""))
        return first.get("timestamp_utc", "")

    # Fallback: file mtime of trading_config.py (when quarantine was added)
    if CONFIG_PATH.exists():
        try:
            mtime_ts = datetime.fromtimestamp(os.path.getmtime(str(CONFIG_PATH)), tz=timezone.utc)
            return mtime_ts.isoformat()
        except Exception:
            pass
    return None


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main() -> None:
    print(SEP)
    print("POST-QUARANTINE PERFORMANCE MONITOR")
    print(f"  generated_at: {_now_utc()}")
    print()
    print(f"  Data sources:")
    print(f"    {TRADES_LOG}")
    print(f"    {FUNNEL_LOG}")
    print(SEP)

    # ── Load all data ────────────────────────────────────────────────────────
    all_records  = load_trades() or []
    funnel_rows  = _load_funnel_rows()
    profile      = _load_profile()

    settled_keys, forced_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted = classify_settled_records(all_records, settled_keys, forced_keys, void_keys)
    active_opens, stale_opens = classify_open_records(all_records)

    # Proof gates
    try:
        proof_buckets = classify_records(all_records)
        proof_gate    = evaluate_proof_gates(proof_buckets, proof_buckets["clean_settled"])
    except Exception as exc:
        proof_gate = {"verdict": "UNKNOWN", "real_money_allowed": False, "scale_allowed": False,
                      "error": str(exc)}

    # Quarantine activation timestamp
    activation_ts = _detect_activation(funnel_rows)

    if activation_ts:
        print(f"  Quarantine activation detected: {activation_ts}")
        method = ("execution_funnel.jsonl (first BLOCKED_QUARANTINE entry)"
                  if any(r.get("final_reason") == "BLOCKED_QUARANTINE" for r in funnel_rows)
                  else "config/trading_config.py file mtime (fallback)")
        print(f"  Detection method: {method}")
    else:
        print("  [WARN] Quarantine activation timestamp could not be determined.")
        print("         No BLOCKED_QUARANTINE entries found and config mtime unavailable.")
        print("         Pre/post split will not be performed.")

    # Split clean_settled by trade OPEN timestamp
    if activation_ts:
        pre_settled  = [r for r in clean_settled if _open_ts(r) < activation_ts]
        post_settled = [r for r in clean_settled if _open_ts(r) >= activation_ts]
    else:
        pre_settled  = clean_settled
        post_settled = []

    pre_kxeth    = [r for r in pre_settled  if is_quarantined_row(r)]
    pre_nonkxeth = [r for r in pre_settled  if not is_quarantined_row(r)]
    post_kxeth   = [r for r in post_settled if is_quarantined_row(r)]  # should be 0
    post_nonkxeth= [r for r in post_settled if not is_quarantined_row(r)]

    bq_all = [r for r in funnel_rows if r.get("final_reason") == "BLOCKED_QUARANTINE"]
    post_funnel = [r for r in funnel_rows if activation_ts and r.get("timestamp_utc", "") >= activation_ts]

    # ── SECTION 1: QUARANTINE STATUS ─────────────────────────────────────────
    print()
    print(SEP)
    print("SECTION 1: QUARANTINE STATUS")
    print(SEP)

    if QUARANTINED_TICKER_PREFIXES:
        print(f"  Active quarantined prefixes: {QUARANTINED_TICKER_PREFIXES}")
        print(f"  KXETH quarantine active:    YES")
        print(f"  Enforcement file:           brain/paper_trader.py → process_signal()")
        print()
        print(f"  BLOCKED_QUARANTINE total (all time):    {len(bq_all)}")
        if bq_all:
            sample_tickers = list(dict.fromkeys(r.get("ticker", "?") for r in bq_all[:20]))[:8]
            print(f"  Sample blocked tickers: {sample_tickers}")
            ticker_ctr = Counter(r.get("ticker", "?") for r in bq_all)
            print(f"  Top 5 quarantine-blocked tickers:")
            for tk, cnt in ticker_ctr.most_common(5):
                print(f"    {tk:<45}  blocked {cnt:3d} times")
    else:
        print("  [WARN] QUARANTINED_TICKER_PREFIXES is empty — quarantine not active!")

    print()
    safe_prefixes = ["KXBTCD", "KXBTC", "KXBTC15M", "KXSOL", "KXXRP", "KXDOGE"]
    print("  Confirmed NOT quarantined:")
    for pfx in safe_prefixes:
        blocked = any(pfx.upper().startswith(qp.upper()) for qp in QUARANTINED_TICKER_PREFIXES)
        status = "BLOCKED ⚠" if blocked else "not quarantined ✓"
        print(f"    {pfx:<14}  {status}")

    # ── SECTION 2: SYSTEM SAFETY SNAPSHOT ────────────────────────────────────
    print()
    print(SEP)
    print("SECTION 2: SYSTEM SAFETY SNAPSHOT")
    print(SEP)

    ep_health     = (proof_gate.get("edge_profile_health") or {}) if isinstance(proof_gate, dict) else {}
    ep_trusted    = ep_health.get("edge_profile_trusted", profile.get("edge_profile_trusted", "n/a"))
    proof_verdict = proof_gate.get("verdict", "UNKNOWN") if isinstance(proof_gate, dict) else "UNKNOWN"
    scale_allowed = proof_gate.get("scale_allowed", False) if isinstance(proof_gate, dict) else False
    rm_allowed    = proof_gate.get("real_money_allowed", False) if isinstance(proof_gate, dict) else False

    def sl(ok: bool, lbl: str, val: str) -> None:
        mark = "OK  " if ok else "WARN"
        print(f"  [{mark}] {lbl:<30} {val}")

    sl(False,               "proof_verdict",       proof_verdict)
    sl(bool(ep_trusted),    "edge_profile_trusted", str(ep_trusted))
    sl(not rm_allowed,      "real_money_allowed",   "NO" if not rm_allowed else "YES ⚠")
    sl(not scale_allowed,   "scale_allowed",        "NO" if not scale_allowed else "YES ⚠")
    sl(True,                "kelly_execution",      "DISABLED" if GLOBAL_FORCED_LEARNING_MODE else "ENABLED ⚠")
    sl(True,                "min_edge",             f"{MIN_EDGE:.3f}")
    sl(True,                "min_confidence",       f"{MIN_CONFIDENCE:.2f}")
    sl(len(active_opens) == 0, "open_count",        str(len(active_opens)))
    sl(True,                "quarantine_active",    f"YES — {QUARANTINED_TICKER_PREFIXES}")

    print()
    print(f"  clean_settled total: {len(clean_settled)}")
    print(f"  pre_quarantine:      {len(pre_settled)}")
    print(f"  post_quarantine:     {len(post_settled)}")
    if post_kxeth:
        print(f"  [WARN] {len(post_kxeth)} KXETH trades appear post-quarantine — investigation needed.")

    # ── SECTION 3: PRE vs POST QUARANTINE SPLIT ───────────────────────────────
    print()
    print(SEP)
    print("SECTION 3: PRE vs POST QUARANTINE TRADE SPLIT")
    print(SEP)

    if not activation_ts:
        print("  [WARN] Cannot split — activation timestamp unknown.")
    else:
        print(f"  Activation cutoff: {activation_ts}")
        print()

    def _split_stats(rows: List[dict], label: str) -> None:
        if not rows:
            print(f"  {label}: n=0 — NO DATA")
            return
        m        = Metrics(rows)
        kxeth_n  = sum(1 for r in rows if is_quarantined_row(r))
        nonk_n   = len(rows) - kxeth_n
        dc_n     = sum(1 for r in rows if r.get("data_collection_override"))
        bp_n     = sum(1 for r in rows if r.get("bootstrap_provisional"))
        proof_n  = len(rows) - dc_n - bp_n
        rs       = sorted(rows, key=_settled_ts)
        dd       = max_drawdown(equity_curve(rs))
        ls, _, _ = longest_losing_streak(rs)
        print(f"  {label}")
        print(f"    n={m.n}  W={m.win_count}  L={m.loss_count}")
        print(f"    KXETH trades: {kxeth_n}  non-KXETH: {nonk_n}")
        print(f"    proof-clean (excl dc+bootstrap_provisional): {proof_n}")
        print(f"    win rate:      {_pct(m.win_rate)}")
        print(f"    ROI:           {_pct(m.roi)}")
        print(f"    profit factor: {m._pf_str()}")
        print(f"    avg CLV:       {_num(m.avg_clv)}")
        print(f"    total PnL:     {_money(m.total_pnl)}")
        print(f"    max drawdown:  ${dd:.2f}")
        print(f"    losing streak: {ls}")

    _split_stats(pre_settled, "PRE-QUARANTINE (all clean settled before activation):")
    print()
    if not post_settled:
        print("  POST-QUARANTINE:")
        print("    NO POST-QUARANTINE SETTLED SAMPLE YET.")
        print("    All settled trades pre-date quarantine activation.")
        print(f"    Latest settled open-time: {max((_open_ts(r) for r in clean_settled), default='none')[:19]}")
        print(f"    Quarantine activated at:  {(activation_ts or 'unknown')[:19]}")
        print("    Collect data — check back after non-KXETH trades settle.")
    else:
        _split_stats(post_settled, "POST-QUARANTINE (clean settled after activation):")

    print()
    print("  Non-KXETH subset of pre-quarantine (KXETH removed from history):")
    _split_stats(pre_nonkxeth, "PRE-QUARANTINE, non-KXETH only:")

    # ── SECTION 4: POST-QUARANTINE NON-KXETH ONLY ────────────────────────────
    print()
    print(SEP)
    print("SECTION 4: POST-QUARANTINE NON-KXETH PERFORMANCE")
    print(SEP)
    print("  Trades opened AND settled after quarantine activation, excluding KXETH.")
    print()

    if not post_nonkxeth:
        print("  n=0  —  NO_SAMPLE")
        print()
        print("  VERDICT: NO_SAMPLE")
        print("  Post-quarantine settled sample does not exist yet.")
        print("  This is the expected state immediately after quarantine activation.")
        print("  Check back after non-KXETH markets open and settle.")
    else:
        m_pq = Metrics(post_nonkxeth)
        dc_pq   = sum(1 for r in post_nonkxeth if r.get("data_collection_override"))
        bp_pq   = sum(1 for r in post_nonkxeth if r.get("bootstrap_provisional"))
        proof_n = m_pq.n - dc_pq - bp_pq
        pq_sorted = sorted(post_nonkxeth, key=_settled_ts)
        dd_pq = max_drawdown(equity_curve(pq_sorted))
        ls_pq, _, _ = longest_losing_streak(pq_sorted)

        print(f"  n={m_pq.n}  W={m_pq.win_count}  L={m_pq.loss_count}")
        print(f"  proof-clean n: {proof_n}  (dc={dc_pq}  bp={bp_pq})")
        print(f"  win rate:      {_pct(m_pq.win_rate)}")
        print(f"  breakeven WR:  {_pct(m_pq.breakeven_wr)}")
        print(f"  ROI:           {_pct(m_pq.roi)}")
        print(f"  profit factor: {m_pq._pf_str()}")
        print(f"  avg CLV:       {_num(m_pq.avg_clv)}")
        print(f"  total PnL:     {_money(m_pq.total_pnl)}")
        print(f"  max drawdown:  ${dd_pq:.2f}")
        print(f"  losing streak: {ls_pq}")
        print()
        print("  PROOF GATE CHECK:")
        _gate_check(proof_n >= PROOF_N,         f"proof-clean n >= {PROOF_N}",  f"current {proof_n}")
        _gate_check((m_pq.roi or 0) > 0,        "ROI > 0%",                     f"current {_pct(m_pq.roi)}")
        _gate_check((m_pq.profit_factor or 0) > PROOF_PF, f"PF > {PROOF_PF}",  f"current {m_pq._pf_str()}")
        _gate_check((m_pq.avg_clv or 0) > 0,    "Avg CLV > 0",                  f"current {_num(m_pq.avg_clv)}")
        _gate_check(dc_pq == 0,                  "No dc_override contamination", f"dc={dc_pq}")
        _gate_check(bp_pq == 0,                  "No bootstrap_provisional",     f"bp={bp_pq}")
        print()
        v = sim_verdict(m_pq)
        print(f"  VERDICT: {v}")
        if v == "PROVEN_EDGE":
            print("  ⚠  All gates pass — real_money_allowed and scale_allowed remain hardcoded False.")

    # ── SECTION 5: POST-QUARANTINE BLOCKER MIX ───────────────────────────────
    print()
    print(SEP)
    print("SECTION 5: POST-QUARANTINE BLOCKER MIX (execution funnel)")
    print(SEP)

    if not funnel_rows:
        print("  [MISSING] execution_funnel.jsonl not found or empty.")
    elif not activation_ts:
        print("  [WARN] Cannot show post-quarantine mix — activation timestamp unknown.")
    elif not post_funnel:
        print("  No execution funnel rows recorded after activation timestamp.")
        print("  Dashboard may need a scan cycle to populate post-quarantine data.")
    else:
        reason_ctr = Counter(r.get("final_reason", "UNKNOWN") for r in post_funnel)
        total_post = len(post_funnel)

        print(f"  Total funnel rows after activation: {total_post}")
        print(f"  (activation: {activation_ts[:19]})")
        print()
        print(f"  {'Reason':<40}  {'Count':>6}  {'%':>6}")
        print(f"  {'-'*40}  {'-'*6}  {'-'*6}")
        for reason, count in reason_ctr.most_common():
            pct = 100.0 * count / total_post if total_post else 0.0
            marker = "  ← quarantine working" if reason == "BLOCKED_QUARANTINE" else ""
            print(f"  {reason:<40}  {count:>6}  {pct:>5.1f}%{marker}")

        print()
        bq_post = [r for r in post_funnel if r.get("final_reason") == "BLOCKED_QUARANTINE"]
        if bq_post:
            print(f"  BLOCKED_QUARANTINE details ({len(bq_post)} rows):")
            bq_tickers = Counter(r.get("ticker", "?") for r in bq_post)
            for tk, cnt in bq_tickers.most_common(5):
                print(f"    {tk:<45}  {cnt:3d}  blocked")
            all_non_kxeth_after = [
                r for r in bq_post
                if not ticker_prefix(r.get("ticker", "")).startswith("KXETH")
            ]
            if all_non_kxeth_after:
                print(f"\n  [WARN] {len(all_non_kxeth_after)} BLOCKED_QUARANTINE rows have non-KXETH tickers!")
                print("         Investigate — may be a false positive in the quarantine check.")
            else:
                print(f"\n  [OK] All {len(bq_post)} BLOCKED_QUARANTINE rows are correctly KXETH tickers.")
        else:
            print("  No BLOCKED_QUARANTINE rows in post-activation funnel yet.")

    # ── SECTION 6: REMAINING POISON WATCHLIST ────────────────────────────────
    print()
    print(SEP)
    print("SECTION 6: REMAINING POISON WATCHLIST (non-KXETH, pre-quarantine data)")
    print(SEP)
    print("  Analysis of pre-quarantine non-KXETH trades to flag next investigation targets.")
    print("  Do NOT quarantine any prefix unless n>=30, ROI clearly negative, PF<1.0, CLV negative.")
    print()

    base_rows = pre_nonkxeth  # non-KXETH pre-quarantine for poison analysis

    if not base_rows:
        print("  [MISSING] No pre-quarantine non-KXETH rows available.")
    else:
        total_loss_abs = abs(Metrics(base_rows).total_pnl) or 1.0

        # Prefix ranking
        print("  By prefix (non-KXETH only, ranked by PnL loss):")
        pfx_groups = group_by(base_rows, lambda r: ticker_prefix(r.get("ticker", "")))
        pfx_stats = []
        for pfx, rows in pfx_groups.items():
            m        = Metrics(rows)
            loss_pct = abs(m.total_pnl) / total_loss_abs * 100 if (m.total_pnl or 0) < 0 else 0.0
            pfx_stats.append((pfx, m, loss_pct))
        pfx_stats.sort(key=lambda x: x[1].total_pnl)

        hdr = (
            f"  {'Prefix':<14}  {'n':>4}  {'WR':>7}  {'ROI':>8}  "
            f"{'PF':>5}  {'CLV':>9}  {'PnL':>9}  {'LossCont%':>9}  Rec"
        )
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for pfx, m, loss_pct in pfx_stats:
            rec      = _quarantine_rec(m)
            loss_str = f"{loss_pct:.0f}%" if loss_pct > 0 else "   —"
            print(
                f"  {pfx:<14}  {m.n:>4}  {_pct(m.win_rate):>7}  {_pct(m.roi):>8}  "
                f"{m._pf_str():>5}  {_num(m.avg_clv, 4):>9}  {_money(m.total_pnl):>9}"
                f"  {loss_str:>9}  {rec}"
            )

        # Entry price ranking
        print()
        print("  By entry price bucket (non-KXETH only, ranked by PnL loss):")
        ep_groups = group_by(base_rows, lambda r: entry_price_bucket(_af(r.get("entry_price"))))
        ep_stats = []
        for bkt in ENTRY_PRICE_ORDER:
            rows = ep_groups.get(bkt, [])
            if not rows:
                continue
            m        = Metrics(rows)
            loss_pct = abs(m.total_pnl) / total_loss_abs * 100 if (m.total_pnl or 0) < 0 else 0.0
            ep_stats.append((bkt, m, loss_pct))
        ep_stats.sort(key=lambda x: x[1].total_pnl)

        hdr2 = (
            f"  {'Bucket':<12}  {'n':>4}  {'WR':>7}  {'BE_WR':>7}  {'WR-BE':>7}  "
            f"{'ROI':>8}  {'PF':>5}  {'CLV':>9}  {'PnL':>9}  {'LossCont%':>9}  Rec"
        )
        print(hdr2)
        print("  " + "-" * (len(hdr2) - 2))
        for bkt, m, loss_pct in ep_stats:
            rec      = _quarantine_rec(m)
            loss_str = f"{loss_pct:.0f}%" if loss_pct > 0 else "   —"
            wr_be    = (m.win_rate or 0.0) - (m.breakeven_wr or 0.0)
            print(
                f"  {bkt:<12}  {m.n:>4}  {_pct(m.win_rate):>7}  {_pct(m.breakeven_wr):>7}"
                f"  {_pct(wr_be):>7}  {_pct(m.roi):>8}  {m._pf_str():>5}"
                f"  {_num(m.avg_clv, 4):>9}  {_money(m.total_pnl):>9}  {loss_str:>9}  {rec}"
            )

        # Confidence / probability ranking
        print()
        print("  By model probability bucket (non-KXETH only):")
        prob_groups = group_by(base_rows, lambda r: prob_bucket(get_model_prob(r)))
        prob_stats = []
        for bkt in PROB_ORDER:
            rows = prob_groups.get(bkt, [])
            if not rows:
                continue
            m        = Metrics(rows)
            loss_pct = abs(m.total_pnl) / total_loss_abs * 100 if (m.total_pnl or 0) < 0 else 0.0
            prob_stats.append((bkt, m, loss_pct))
        prob_stats.sort(key=lambda x: x[1].total_pnl)

        hdr3 = (
            f"  {'Bucket':<12}  {'n':>4}  {'AvgProb':>8}  {'RealWR':>7}  "
            f"{'ROI':>8}  {'PF':>5}  {'CLV':>9}  {'PnL':>9}  Rec"
        )
        print(hdr3)
        print("  " + "-" * (len(hdr3) - 2))
        for bkt, m, loss_pct in prob_stats:
            rec = _quarantine_rec(m)
            print(
                f"  {bkt:<12}  {m.n:>4}  {_pct(m.avg_prob, 1):>8}  {_pct(m.realized_wr, 1):>7}"
                f"  {_pct(m.roi):>8}  {m._pf_str():>5}  {_num(m.avg_clv, 4):>9}"
                f"  {_money(m.total_pnl):>9}  {rec}"
            )

        print()
        print("  QUARANTINE READINESS CHECK (non-KXETH prefixes):")
        quarantine_ready = [
            (pfx, m) for pfx, m, _ in pfx_stats
            if m.n >= 30 and (m.roi or 0) < -0.20 and (m.profit_factor or 1.0) < 0.5
        ]
        if quarantine_ready:
            for pfx, m in quarantine_ready:
                print(f"    [INVESTIGATE]  {pfx}: n={m.n}, ROI={_pct(m.roi)}, PF={m._pf_str()}")
                print(f"    Run report_quarantine_simulation.py before adding to QUARANTINED_TICKER_PREFIXES.")
        else:
            print("    No non-KXETH prefix meets the hard quarantine threshold (n>=30, ROI<=-20%, PF<0.5).")
            print("    Insufficient post-quarantine data to confirm or rule out new poison pockets.")

    # ── SECTION 7: NEXT ACTION RECOMMENDATION ────────────────────────────────
    print()
    print(SEP)
    print("SECTION 7: NEXT ACTION RECOMMENDATION")
    print(SEP)
    print()

    post_settled_n = len(post_settled)
    post_nonkxeth_n = len(post_nonkxeth)
    bq_count = len(bq_all)

    # Determine verdict
    if post_nonkxeth_n == 0:
        action = "WAIT_AND_COLLECT"
    else:
        m_pq_check = Metrics(post_nonkxeth)
        if (m_pq_check.roi or 0) > 0 and (m_pq_check.profit_factor or 0) > PROOF_PF and (m_pq_check.avg_clv or 0) > 0 and m_pq_check.n >= PROOF_N:
            action = "PROTECT_CLEAN_SETUP"
        elif any(
            m.n >= 30 and (m.roi or 0) < -0.20 and (m.profit_factor or 1.0) < 0.5
            for _, m, _ in pfx_stats
        ) if base_rows else False:
            action = "INVESTIGATE_NEXT_POISON"
        else:
            action = "WAIT_AND_COLLECT"

    print(f"  ACTION: {action}")
    print()

    if action == "WAIT_AND_COLLECT":
        print("  WHY: Post-quarantine settled sample is zero.")
        print(f"       KXETH quarantine is active and blocking ({bq_count} signals blocked so far).")
        print(f"       Non-KXETH signals are flowing through the remaining filters.")
        print()
        print("  WHAT TO WATCH:")
        print("    1. Run this report again after non-KXETH trades settle.")
        print("    2. Target: collect 30+ proof-clean non-KXETH settled trades.")
        print("    3. When available, Section 4 will show post-quarantine ROI/PF/CLV.")
        print()
        print("  HOW LONG:")
        print("    Depends on market availability and filter pass rate.")
        print("    Current post-quarantine blocker mix shows which filters are active.")
        print("    If BLOCKED_MARKET_QUALITY + BLOCKED_COUNCIL dominate → normal operation.")
        print("    If BLOCKED_MIN_EDGE dominates post-quarantine → check if edge model calibrated.")

    elif action == "PROTECT_CLEAN_SETUP":
        print("  WHY: Post-quarantine non-KXETH setup passes all proof gates.")
        print("  CONFIRM: All 4 gates (n>=30, ROI>0, PF>1.10, CLV>0) must pass.")
        print("  DO NOT scale or enable real money — those are hardcoded locked.")
        print("  DO: Run report_quarantine_simulation.py to confirm the setup is not fragile.")

    elif action == "INVESTIGATE_NEXT_POISON":
        print("  WHY: A new poison bucket has reached n>=30 with strong negative metrics.")
        print("  DO: Run report_quarantine_simulation.py with the candidate prefix before patching.")
        print("  DO NOT: Add to QUARANTINED_TICKER_PREFIXES without simulation confirmation.")

    print()
    print(SEP)
    print(
        "  Current phase: collect fresh non-KXETH evidence. Do not scale. "
        "Do not enable real money.\n"
        "  Do not add another quarantine until post-quarantine data proves it."
    )
    print(SEP)
    print()


if __name__ == "__main__":
    main()
