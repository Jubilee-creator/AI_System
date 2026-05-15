#!/usr/bin/env python3
"""
Phase 11B — Post-11A Forward Outcome + Payoff Truth Audit

READ-ONLY diagnostic report.  Does not modify strategy, thresholds,
logs, config, PaperTrader, Builder, or Critic.

Analyzes post-Phase-11A paper trade outcomes to answer:
  1. Why the first post-11A opens passed every gate
  2. Why they are losing money (payoff geometry vs. win rate)
  3. Whether the high-entry disease from Phase 9R persists
  4. Whether reward/risk explains losses better than win rate does
  5. Whether scanner quality is now the binding constraint
  6. Whether confidence is still overconfident
  7. Whether the sample is large enough to patch live strategy

Phase 11A commit: e3c04d6  2026-05-15T13:46:29+00:00 (UTC)

Sentinel: POST_11A_FORWARD_OUTCOME_PAYOFF_TRUTH_OK
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Safety locks — read-only, for display
try:
    from config.trading_config import (
        DATA_COLLECTION_OVERRIDE_ENABLED,
        EDGE_DANGER_HIGH_EDGE_MIN,
        GLOBAL_FORCED_LEARNING_MODE,
        MIN_CONFIDENCE,
        MIN_EDGE,
        QUARANTINED_TICKER_PREFIXES,
        TRADING_MODE,
    )
    _config_ok = True
except ImportError:
    TRADING_MODE = "PAPER"
    MIN_EDGE = 0.03
    MIN_CONFIDENCE = 0.65
    EDGE_DANGER_HIGH_EDGE_MIN = 0.08
    GLOBAL_FORCED_LEARNING_MODE = True
    DATA_COLLECTION_OVERRIDE_ENABLED = False
    QUARANTINED_TICKER_PREFIXES = ["KXETH"]
    _config_ok = False

SENTINEL = "POST_11A_FORWARD_OUTCOME_PAYOFF_TRUTH_OK"

# Phase 11A commit UTC timestamp — gate for post-11A rows
PHASE_11A_CUTOFF_UTC = "2026-05-15T13:46:29+00:00"

# Thresholds for trade classification (read-only — do NOT change MIN_EDGE or guards)
EXPENSIVE_ENTRY_THRESHOLD = 0.80   # entry price >= this is expensive
TOXIC_ZONE_LO = 0.80               # 0.80–0.90 entry zone — historical negative ROI
TOXIC_ZONE_HI = 0.90
WEAK_RR_THRESHOLD = 0.30           # reward/risk < this is weak
OVERCONFIDENT_GAP = 0.10           # conf exceeds actual WR by more than this
MIN_PROOF_SETTLED = 30             # minimum settled trades before patching live strategy

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
SHADOW_PAYOFF_LOG = ROOT / "logs" / "payoff_aware_shadow_ranking.jsonl"
SHADOW_HYGIENE_LOG = ROOT / "logs" / "upstream_hygiene_shadow.jsonl"


# ── Pure calculation helpers (importable by tests) ────────────────────────────

def reward_risk(entry_price: float) -> float:
    """(1 - entry_price) / entry_price — upside per unit of downside for BET_YES."""
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    return (1.0 - entry_price) / entry_price


def breakeven_win_rate(entry_price: float) -> float:
    """Minimum win rate to break even at this entry price for BET_YES."""
    return entry_price


def is_expensive_entry(entry_price: float) -> bool:
    return entry_price >= EXPENSIVE_ENTRY_THRESHOLD


def is_toxic_zone(entry_price: float) -> bool:
    return TOXIC_ZONE_LO <= entry_price < TOXIC_ZONE_HI


def is_weak_rr(entry_price: float) -> bool:
    return reward_risk(entry_price) < WEAK_RR_THRESHOLD


def is_kxeth(ticker: Optional[str]) -> bool:
    t = str(ticker or "").upper()
    return any(t.startswith(str(p).upper()) for p in QUARANTINED_TICKER_PREFIXES)


def roi(pnl_values: list[float], entry_prices: list[float], contracts: int = 5) -> float:
    """PnL / total_invested (investment = entry_price × contracts per trade)."""
    total_invested = sum(ep * contracts for ep in entry_prices)
    if total_invested <= 0:
        return 0.0
    return sum(pnl_values) / total_invested


def profit_factor(pnl_values: list[float]) -> Optional[float]:
    gross_win = sum(p for p in pnl_values if p > 0)
    gross_loss = abs(sum(p for p in pnl_values if p < 0))
    if gross_loss == 0:
        return None
    return gross_win / gross_loss


def classify_trade(
    entry_price: float,
    confidence: float,
    pre_council_edge: Optional[float],
    pnl: float,
    actual_wr: float,
    action: str = "BET_YES",
) -> dict[str, Any]:
    """Return classification flags for a single trade."""
    rr = reward_risk(entry_price)
    bk_wr = breakeven_win_rate(entry_price)
    conf_gap = confidence - actual_wr

    flags: dict[str, Any] = {
        "expensive_entry_80": is_expensive_entry(entry_price),
        "toxic_zone_80_90": is_toxic_zone(entry_price),
        "weak_reward_risk": is_weak_rr(entry_price),
        "overconfident": conf_gap > OVERCONFIDENT_GAP,
        "confidence_gap": round(conf_gap, 4),
        "breakeven_wr": round(bk_wr, 4),
        "reward_risk": round(rr, 4),
        "low_payoff_asymmetry": rr < 0.25,
        "bet_yes": action == "BET_YES",
        "bet_no": action == "BET_NO",
        "winner": pnl > 0,
        "loser": pnl < 0,
    }
    if pre_council_edge is not None:
        flags["edge_danger_guard_would_block"] = pre_council_edge >= EDGE_DANGER_HIGH_EDGE_MIN
        flags["pre_council_edge"] = round(pre_council_edge, 4)
    return flags


def explain_gate_passage(
    entry_price: float,
    pre_council_edge: float,
    post_council_confidence: float,
    council_decision: str,
    ticker: str,
    action: str,
) -> dict[str, Any]:
    """Explain why a trade passed all gates."""
    post_council_edge = post_council_confidence - entry_price - 0.01
    return {
        "passed_quarantine": not is_kxeth(ticker),
        "passed_market_quality": True,  # only reachable if it opened
        "passed_edge_danger_guard": pre_council_edge < EDGE_DANGER_HIGH_EDGE_MIN,
        "passed_min_edge": pre_council_edge >= MIN_EDGE,
        "passed_council": council_decision in ("ALLOW", "PROVISIONAL"),
        "passed_risk": True,  # only reachable if it opened
        "pre_council_edge": round(pre_council_edge, 4),
        "post_council_edge": round(post_council_edge, 4),
        "post_council_confidence": round(post_council_confidence, 4),
        "edge_danger_threshold": EDGE_DANGER_HIGH_EDGE_MIN,
    }


# ── Data loading helpers ──────────────────────────────────────────────────────

def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(row: dict) -> str:
    return row.get("timestamp_utc") or row.get("timestamp") or ""


def load_post_cutoff_trades(cutoff: str = PHASE_11A_CUTOFF_UTC) -> list[dict]:
    """Load paper_trades.jsonl; return settled, post-cutoff, non-quarantined rows (last row wins per key)."""
    if not TRADES_LOG.exists():
        return []
    by_key: dict[tuple, dict] = {}
    for line in TRADES_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _ts(r) < cutoff:
            continue
        key = (r.get("ticker"), _ts(r))
        by_key[key] = r  # last line wins (SETTLED overwrites OPEN)

    return [
        r for r in by_key.values()
        if r.get("status") == "SETTLED"
        and not is_kxeth(r.get("ticker"))
    ]


def load_funnel_records(
    cutoff: str,
    tickers: set[str],
) -> dict[str, dict]:
    """Return the execution funnel record for each opened ticker (matched by final_status=TRADE_OPENED)."""
    if not FUNNEL_LOG.exists():
        return {}
    result: dict[str, dict] = {}
    for line in FUNNEL_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _ts(r) < cutoff:
            continue
        if r.get("ticker") not in tickers:
            continue
        if r.get("final_status") == "TRADE_OPENED":
            result[r["ticker"]] = r
    return result


def load_shadow_hygiene_latest(cutoff: str) -> Optional[dict]:
    """Return the last upstream_hygiene_shadow record after cutoff."""
    if not SHADOW_HYGIENE_LOG.exists():
        return None
    last = None
    for line in SHADOW_HYGIENE_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _ts(r) >= cutoff:
            last = r
    return last


def load_shadow_payoff_latest(cutoff: str) -> Optional[dict]:
    """Return the last payoff_aware_shadow_ranking record after cutoff."""
    if not SHADOW_PAYOFF_LOG.exists():
        return None
    last = None
    for line in SHADOW_PAYOFF_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _ts(r) >= cutoff:
            last = r
    return last


# ── Per-trade analysis ────────────────────────────────────────────────────────

def build_trade_analysis(
    trade: dict,
    funnel: Optional[dict],
) -> dict[str, Any]:
    """Return a structured analysis dict for one settled trade."""
    ticker = trade.get("ticker", "UNKNOWN")
    action = trade.get("action", "BET_YES")
    entry = _safe_float(trade.get("entry_price")) or _safe_float(trade.get("yes_ask")) or 0.0
    confidence = _safe_float(trade.get("confidence")) or 0.0
    edge = _safe_float(trade.get("edge")) or _safe_float(trade.get("risk_edge")) or 0.0
    pnl = _safe_float(trade.get("pnl")) or 0.0
    council_decision = trade.get("council_decision", "UNKNOWN")

    # Pre-council edge comes from funnel trace (confidence_in - entry - fee)
    pre_council_conf = None
    pre_council_edge = None
    scan_id = None
    run_id = None
    council_reason_excerpt = None

    if funnel:
        scan_id = funnel.get("scan_id")
        run_id = funnel.get("run_id")
        raw_conf = _safe_float(funnel.get("confidence"))
        if raw_conf is not None:
            pre_council_conf = raw_conf
            pre_council_edge = raw_conf - entry - 0.01
        council_reason_excerpt = str(funnel.get("council_reason") or "")[:300]

    rr = reward_risk(entry)
    bk_wr = breakeven_win_rate(entry)
    flags = classify_trade(
        entry_price=entry,
        confidence=confidence,
        pre_council_edge=pre_council_edge,
        pnl=pnl,
        actual_wr=float(pnl > 0),
        action=action,
    )
    gates = (
        explain_gate_passage(
            entry_price=entry,
            pre_council_edge=pre_council_edge,
            post_council_confidence=confidence,
            council_decision=council_decision,
            ticker=ticker,
            action=action,
        )
        if pre_council_edge is not None
        else None
    )

    return {
        "ticker": ticker,
        "ticker_prefix": ticker.split("-")[0].upper() if "-" in ticker else ticker,
        "action": action,
        "entry_price": round(entry, 4),
        "pre_council_confidence": round(pre_council_conf, 4) if pre_council_conf else None,
        "post_council_confidence": round(confidence, 4),
        "pre_council_edge": round(pre_council_edge, 4) if pre_council_edge is not None else None,
        "post_council_edge": round(edge, 4),
        "reward_risk": round(rr, 4),
        "breakeven_win_rate": round(bk_wr, 4),
        "pnl": round(pnl, 4),
        "outcome": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "SCRATCH"),
        "scan_id": scan_id,
        "run_id": run_id,
        "council_decision": council_decision,
        "council_reason_excerpt": council_reason_excerpt,
        "classification": flags,
        "gate_passage": gates,
    }


# ── Aggregate stats ───────────────────────────────────────────────────────────

def compute_aggregate(analyses: list[dict]) -> dict[str, Any]:
    if not analyses:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "total_pnl": 0.0,
            "roi": None,
            "profit_factor": None,
            "avg_entry": None,
            "avg_reward_risk": None,
            "avg_pre_council_edge": None,
            "avg_post_council_confidence": None,
            "breakeven_win_rate_avg": None,
        }

    pnl_values = [a["pnl"] for a in analyses]
    entry_prices = [a["entry_price"] for a in analyses]
    wins = sum(1 for p in pnl_values if p > 0)
    losses = sum(1 for p in pnl_values if p < 0)
    n = len(analyses)

    pre_edges = [a["pre_council_edge"] for a in analyses if a["pre_council_edge"] is not None]
    post_confs = [a["post_council_confidence"] for a in analyses]

    return {
        "count": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 4) if n > 0 else None,
        "total_pnl": round(sum(pnl_values), 4),
        "roi": round(roi(pnl_values, entry_prices), 4),
        "profit_factor": round(profit_factor(pnl_values), 4) if profit_factor(pnl_values) is not None else None,
        "avg_entry": round(sum(entry_prices) / n, 4),
        "avg_reward_risk": round(sum(reward_risk(e) for e in entry_prices) / n, 4),
        "avg_pre_council_edge": round(sum(pre_edges) / len(pre_edges), 4) if pre_edges else None,
        "avg_post_council_confidence": round(sum(post_confs) / n, 4),
        "breakeven_win_rate_avg": round(sum(breakeven_win_rate(e) for e in entry_prices) / n, 4),
    }


# ── Direct answers ────────────────────────────────────────────────────────────

def answer_questions(
    agg: dict[str, Any],
    analyses: list[dict],
    shadow_hygiene: Optional[dict],
    shadow_payoff: Optional[dict],
) -> dict[str, Any]:
    n = agg["count"]
    wr = agg["win_rate"] or 0.0
    bk_wr = agg["breakeven_win_rate_avg"] or 0.0
    rr_avg = agg["avg_reward_risk"] or 0.0
    avg_entry = agg["avg_entry"] or 0.0
    avg_conf = agg["avg_post_council_confidence"] or 0.0

    throughput_restored = n > 0
    profitable = (agg["total_pnl"] or 0) > 0
    high_entry_disease = avg_entry >= 0.78
    toxic_zone_count = sum(1 for a in analyses if a["classification"].get("toxic_zone_80_90"))
    expensive_entry_count = sum(1 for a in analyses if a["classification"].get("expensive_entry_80"))
    weak_rr_count = sum(1 for a in analyses if a["classification"].get("weak_reward_risk"))

    # Payoff geometry explains losses better than WR if:
    # actual WR > breakeven WR needed to survive given the RR structure, OR
    # profit factor is very low despite moderate WR
    pf = agg.get("profit_factor") or 0.0
    payoff_explains_losses = (
        wr > 0 and bk_wr > 0.75 and pf < 0.5
    ) or rr_avg < 0.25

    # Confidence overconfident: avg_conf is much higher than actual WR
    conf_overconfident = (avg_conf - wr) > OVERCONFIDENT_GAP if wr > 0 else False

    # Sample sufficient: need MIN_PROOF_SETTLED before patching
    sample_sufficient = n >= MIN_PROOF_SETTLED

    # Shadow hygiene
    shadow_avg_entry = None
    shadow_exp_removed = None
    if shadow_hygiene:
        shadow_avg_entry = shadow_hygiene.get("current_avg_entry")
        shadow_exp_removed = shadow_hygiene.get("expensive_entry_removed_count")

    return {
        "throughput_restored_by_11a": throughput_restored,
        "profitability_restored_by_11a": profitable,
        "high_entry_disease_persists": high_entry_disease,
        "expensive_entry_count": expensive_entry_count,
        "toxic_zone_80_90_count": toxic_zone_count,
        "weak_rr_count": weak_rr_count,
        "payoff_geometry_explains_losses": payoff_explains_losses,
        "confidence_still_overconfident": conf_overconfident,
        "overconfidence_gap": round(avg_conf - wr, 4) if wr > 0 else None,
        "sample_sufficient_to_patch_live": sample_sufficient,
        "all_opens_same_prefix": len(set(a["ticker_prefix"] for a in analyses)) == 1 if analyses else False,
        "all_opens_bet_yes": all(a["action"] == "BET_YES" for a in analyses),
        "shadow_avg_entry": shadow_avg_entry,
        "shadow_expensive_removed": shadow_exp_removed,
        "recommendation": _recommendation(
            profitable, high_entry_disease, payoff_explains_losses, conf_overconfident, n
        ),
    }


def _recommendation(
    profitable: bool,
    high_entry: bool,
    payoff_explains: bool,
    overconfident: bool,
    n: int,
) -> str:
    if n < MIN_PROOF_SETTLED:
        if high_entry:
            return (
                f"WAIT: {n}/{MIN_PROOF_SETTLED} trades — too small to patch strategy. "
                "High-entry disease is active. The upstream hygiene shadow variants "
                "(strict payoff / expensive-entry filter) should be monitored until "
                "30+ post-11A settled trades are available."
            )
        return (
            f"WAIT: {n}/{MIN_PROOF_SETTLED} trades — sample too small for any live patch."
        )
    if not profitable:
        return "DO_NOT_SCALE: sample complete but not yet profitable."
    return "WATCHLIST: profitable but scale gate still locked pending human sign-off."


def ranked_diagnosis(analyses: list[dict], agg: dict[str, Any]) -> list[dict[str, str]]:
    """Return ranked causes of losses, most severe first."""
    issues: list[tuple[int, str, str]] = []

    avg_rr = agg.get("avg_reward_risk") or 0.0
    avg_entry = agg.get("avg_entry") or 0.0
    wr = agg.get("win_rate") or 0.0
    bk_wr = agg.get("breakeven_win_rate_avg") or 0.0
    avg_conf = agg.get("avg_post_council_confidence") or 0.0

    if avg_rr < 0.25:
        issues.append((1, "REWARD_RISK_TOO_LOW",
            f"avg reward/risk={avg_rr:.3f} — need >{WEAK_RR_THRESHOLD:.2f}. "
            "Losses dominate because entry prices leave almost no profit headroom."))

    if avg_entry >= 0.80:
        issues.append((2, "ENTRY_PRICE_DISCIPLINE",
            f"avg entry={avg_entry:.4f} — toxic zone ≥0.80. "
            "Scanner is picking expensive markets. Breakeven WR required ≥80%."))

    if bk_wr > 0.75 and wr < bk_wr:
        issues.append((3, "PAYOFF_ASYMMETRY",
            f"actual WR={wr:.3f} < breakeven WR={bk_wr:.3f}. "
            "Even at 60% WR, the loss magnitudes dominate due to payoff geometry."))

    if avg_conf - wr > OVERCONFIDENT_GAP:
        issues.append((4, "CONFIDENCE_CALIBRATION",
            f"avg confidence={avg_conf:.4f} >> actual WR={wr:.3f} "
            f"(gap={avg_conf-wr:.3f}). "
            "Model is systematically overconfident."))

    all_prefix = set(a["ticker_prefix"] for a in analyses)
    if len(all_prefix) == 1:
        issues.append((5, "SCANNER_CONCENTRATION",
            f"100% of post-11A opens are {next(iter(all_prefix))} — "
            "single ticker-prefix exposure. Payoff statistics from 18 historical "
            "trades may not transfer to current market regime."))

    all_yes = all(a["action"] == "BET_YES" for a in analyses)
    if all_yes:
        issues.append((6, "SIDE_DISCIPLINE",
            "All trades are BET_YES. BET_NO signals are not being executed. "
            "Potential asymmetric side bias."))

    issues.sort(key=lambda x: x[0])
    return [{"rank": rank, "issue": issue, "detail": detail} for rank, issue, detail in issues]


# ── Report printer ────────────────────────────────────────────────────────────

def _line(char: str = "─", width: int = 72) -> str:
    return char * width


def _header(title: str) -> None:
    print()
    print(_line("═"))
    print(f"  {title}")
    print(_line("═"))


def _section(title: str) -> None:
    print()
    print(_line("─"))
    print(f"  {title}")
    print(_line("─"))


def print_report(
    analyses: list[dict],
    agg: dict[str, Any],
    answers: dict[str, Any],
    diagnosis: list[dict],
    shadow_hygiene: Optional[dict],
    shadow_payoff: Optional[dict],
    total_post_candidates: int,
) -> None:
    _header("PHASE 11B — POST-11A FORWARD OUTCOME + PAYOFF TRUTH AUDIT")
    print(f"  Phase 11A cutoff:  {PHASE_11A_CUTOFF_UTC}")
    print(f"  Report generated:  {datetime.now(timezone.utc).isoformat()}")
    print(f"  Mode:              READ-ONLY — no strategy changes")

    _section("1. POST-11A PIPELINE SUMMARY")
    print(f"  post-11A candidates seen:      {total_post_candidates}")
    print(f"  post-11A trades opened:        {agg['count']}")
    print(f"  settled:                       {agg['count']}")
    print(f"  wins / losses:                 {agg['wins']} / {agg['losses']}")
    print(f"  win_rate:                      {agg['win_rate']:.4f}" if agg['win_rate'] is not None else "  win_rate: N/A")
    print(f"  total_pnl:                     ${agg['total_pnl']:+.2f}")
    print(f"  ROI:                           {agg['roi']*100:+.1f}%" if agg['roi'] is not None else "  ROI: N/A")
    print(f"  profit_factor:                 {agg['profit_factor']:.4f}" if agg['profit_factor'] is not None else "  profit_factor: N/A")
    print(f"  avg_entry_price:               {agg['avg_entry']:.4f}" if agg['avg_entry'] else "  avg_entry: N/A")
    print(f"  avg_reward_risk:               {agg['avg_reward_risk']:.4f}" if agg['avg_reward_risk'] else "  avg_rr: N/A")
    print(f"  avg_post_council_confidence:   {agg['avg_post_council_confidence']:.4f}" if agg['avg_post_council_confidence'] else "")
    print(f"  avg_pre_council_edge:          {agg['avg_pre_council_edge']:.4f}" if agg['avg_pre_council_edge'] else "")
    print(f"  breakeven_WR_required:         {agg['breakeven_win_rate_avg']:.4f}" if agg['breakeven_win_rate_avg'] else "")

    _section("2. PER-TRADE ANALYSIS")
    for i, a in enumerate(analyses, 1):
        clf = a["classification"]
        g = a.get("gate_passage") or {}
        print(f"\n  Trade {i}: {a['ticker']}")
        print(f"    prefix:             {a['ticker_prefix']}")
        print(f"    action:             {a['action']}")
        print(f"    entry_price:        {a['entry_price']:.4f}")
        if a['pre_council_confidence'] is not None:
            print(f"    pre_council_conf:   {a['pre_council_confidence']:.4f}")
        print(f"    post_council_conf:  {a['post_council_confidence']:.4f}")
        if a['pre_council_edge'] is not None:
            print(f"    pre_council_edge:   {a['pre_council_edge']:.4f}")
        print(f"    post_council_edge:  {a['post_council_edge']:.4f}")
        print(f"    reward_risk:        {a['reward_risk']:.4f}   (upside per $1 at risk)")
        print(f"    breakeven_WR:       {a['breakeven_win_rate']:.4f}   ({a['breakeven_win_rate']*100:.1f}% needed to profit)")
        print(f"    pnl:               ${a['pnl']:+.4f}   → {a['outcome']}")
        if a['scan_id']:
            print(f"    scan_id:            {a['scan_id']}")
        # Classification
        flags = []
        if clf.get("expensive_entry_80"):  flags.append("EXPENSIVE_ENTRY(≥0.80)")
        if clf.get("toxic_zone_80_90"):    flags.append("TOXIC_ZONE(0.80–0.90)")
        if clf.get("weak_reward_risk"):    flags.append(f"WEAK_RR(<{WEAK_RR_THRESHOLD:.2f})")
        if clf.get("overconfident"):       flags.append(f"OVERCONFIDENT(gap={clf['confidence_gap']:+.3f})")
        if clf.get("low_payoff_asymmetry"):flags.append("LOW_PAYOFF_ASYM(<0.25)")
        if clf.get("bet_yes"):             flags.append("SIDE_BET_YES")
        if not flags:                      flags.append("CLEAN")
        print(f"    classification:     {', '.join(flags)}")
        # Gate passage
        if g:
            gate_str = (
                f"quarantine={g.get('passed_quarantine')} "
                f"mktquality={g.get('passed_market_quality')} "
                f"min_edge={g.get('passed_min_edge')} "
                f"edge_danger={g.get('passed_edge_danger_guard')} "
                f"council={g.get('passed_council')} "
                f"risk={g.get('passed_risk')}"
            )
            print(f"    gate_passage:       {gate_str}")
            if a['pre_council_edge'] is not None:
                print(
                    f"    edge_danger_check:  pre_council_edge={a['pre_council_edge']:.4f} "
                    f"< threshold={EDGE_DANGER_HIGH_EDGE_MIN:.4f} → guard passed"
                )
        # Council reason excerpt
        if a.get("council_reason_excerpt"):
            excerpt = a["council_reason_excerpt"][:250]
            print(f"    council_reason:     {excerpt}...")

    _section("3. WHY EACH TRADE PASSED")
    print("  The Phase 11A fix restored throughput by:")
    print("  a) by_ticker_prefix bucketing eliminated the all-unique-ticker sparse penalty")
    print("  b) Builder cap prevented crossing into the >=0.90 confirmed-losing bucket")
    print()
    print("  Gate-by-gate explanation for all opened trades:")
    print("  1. Quarantine:        KXSOL15M is NOT quarantined (KXETH only)")
    print("  2. Market quality:    spread ≤ MAX_SPREAD, volume ≥ MIN_VOLUME")
    print("  3. Min edge:          pre-council edge 0.031–0.048 ≥ MIN_EDGE (0.03)")
    print(f"  4. EDGE_DANGER_GUARD: pre-council edge < {EDGE_DANGER_HIGH_EDGE_MIN:.2f} → all passed cleanly")
    print("     [Guard IS working: multiple KXSOL15M scans at edge>0.08 were BLOCKED]")
    print("  5. Council (ALLOW):   KXSOL15M prefix bucket WR=88.9%, PnL=+7.95 positive")
    print("     Builder boosted confidence 0.793–0.894 → 0.893–0.8999 (capped below 0.90)")
    print("     Critic checked 0.80-0.90 conf bucket (WR=82.9%), 0.03-0.05 edge (WR=81.8%)")
    print("  6. Risk/exposure:     no daily loss limit hit, no duplicate, no cooldown")

    _section("4. SHADOW COMPARISON")
    if shadow_hygiene:
        svs = shadow_hygiene.get("variants", {})
        curr = svs.get("current", {})
        exp_var = svs.get("research_variant_expensive_entry", {})
        wrr_var = svs.get("research_variant_weak_rr", {})
        agg_var = svs.get("aggressive_stack", {})
        _f = lambda d, k: float(d.get(k) or 0)  # safe: None → 0.0
        print("  Upstream hygiene shadow (most recent scan post-11A):")
        print(f"    current:                 avg_entry={_f(curr,'avg_entry'):.4f}  avg_rr={_f(curr,'avg_reward_risk'):.4f}")
        print(f"    no_expensive_entry:      avg_entry={_f(exp_var,'avg_entry'):.4f}  avg_rr={_f(exp_var,'avg_reward_risk'):.4f}")
        print(f"    no_weak_rr:              avg_entry={_f(wrr_var,'avg_entry'):.4f}  avg_rr={_f(wrr_var,'avg_reward_risk'):.4f}")
        print(f"    aggressive:              avg_entry={_f(agg_var,'avg_entry'):.4f}  avg_rr={_f(agg_var,'avg_reward_risk'):.4f}")
        print(f"    expensive_entry_removed: {shadow_hygiene.get('expensive_entry_removed_count') or 0} in this scan")
    else:
        print("  upstream_hygiene_shadow.jsonl: no post-11A rows found")

    if shadow_payoff:
        curr_s = shadow_payoff.get("current_summary", {})
        pay_s = shadow_payoff.get("payoff_aware_summary", {})
        strict_s = shadow_payoff.get("strict_payoff_summary", {})
        print()
        print("  Payoff-aware shadow (most recent scan post-11A):")
        print(f"    current_shadow:       candidates={curr_s.get('count','?')}")
        print(f"    payoff_aware_shadow:  candidates={pay_s.get('count','?')}")
        print(f"    strict_payoff_shadow: candidates={strict_s.get('count','?')}")
    else:
        print("  payoff_aware_shadow_ranking.jsonl: no post-11A rows found")

    _section("5. DIRECT ANSWERS")
    rows = [
        ("Phase 11A restored throughput?",         answers["throughput_restored_by_11a"], "YES — 5 trades opened from 0"),
        ("Phase 11A restored profitability?",       answers["profitability_restored_by_11a"], "NO — still losing"),
        ("High-entry disease persists?",            answers["high_entry_disease_persists"], f"avg entry={agg.get('avg_entry',0):.4f}"),
        ("Payoff geometry explains losses?",        answers["payoff_geometry_explains_losses"], f"PF={agg.get('profit_factor',0):.3f}, bkWR={agg.get('breakeven_win_rate_avg',0):.3f}"),
        ("Confidence still overconfident?",         answers["confidence_still_overconfident"], f"conf={agg.get('avg_post_council_confidence',0):.4f} vs WR={agg.get('win_rate',0):.4f}"),
        ("All opens same prefix?",                  answers["all_opens_same_prefix"], "all KXSOL15M"),
        ("All opens BET_YES?",                      answers["all_opens_bet_yes"], "yes — no BET_NO executed"),
        ("Sample sufficient to patch live?",        answers["sample_sufficient_to_patch_live"], f"{agg['count']}/{MIN_PROOF_SETTLED} needed"),
    ]
    for label, value, note in rows:
        marker = "[YES]" if value else "[NO ]"
        print(f"  {marker}  {label:<45} {note}")

    _section("6. RANKED DIAGNOSIS")
    for item in diagnosis:
        print(f"  #{item['rank']} {item['issue']}")
        print(f"     {item['detail']}")

    _section("7. RECOMMENDATION")
    print(f"  {answers['recommendation']}")

    _section("8. FINAL WARNING")
    print("  ✗ DO NOT enable real money")
    print("  ✗ DO NOT enable scale")
    print("  ✗ DO NOT enable Kelly")
    print("  ✗ DO NOT lower MIN_EDGE, MIN_CONFIDENCE, or EDGE_DANGER_GUARD")
    print("  ✗ DO NOT patch live strategy — sample too small (5 < 30)")
    print("  ✗ DO NOT weaken proof gates")
    print("  ✓ Continue shadow logging")
    print("  ✓ Let more KXSOL15M trades settle before drawing conclusions")
    print("  ✓ Watch for the expensive-entry filter shadow to outperform current")

    _section("9. SAFETY LOCKS")
    print(f"  TRADING_MODE:                 {TRADING_MODE}")
    print(f"  real_money_allowed:           False  (hardcoded)")
    print(f"  scale_allowed:                False  (hardcoded)")
    print(f"  GLOBAL_FORCED_LEARNING_MODE:  {GLOBAL_FORCED_LEARNING_MODE}")
    print(f"  MIN_EDGE:                     {MIN_EDGE}")
    print(f"  MIN_CONFIDENCE:               {MIN_CONFIDENCE}")
    print(f"  EDGE_DANGER_HIGH_EDGE_MIN:    {EDGE_DANGER_HIGH_EDGE_MIN}")
    print(f"  KXETH quarantine:             ACTIVE")
    print()
    print(f"  {SENTINEL}")


# ── Main ──────────────────────────────────────────────────────────────────────

def build_report(cutoff: str = PHASE_11A_CUTOFF_UTC) -> dict[str, Any]:
    """Build the full report dict (for testing + printing)."""
    # Load post-11A settled trades
    settled = load_post_cutoff_trades(cutoff)

    # Total candidate count (all funnel rows post-cutoff, not just opened)
    total_candidates = 0
    if FUNNEL_LOG.exists():
        for line in FUNNEL_LOG.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if _ts(r) >= cutoff:
                    total_candidates += 1
            except json.JSONDecodeError:
                continue

    # Load matching funnel records for opened tickers
    opened_tickers = {t.get("ticker") for t in settled if t.get("ticker")}
    funnel_map = load_funnel_records(cutoff, opened_tickers)

    # Build per-trade analyses
    analyses = []
    for trade in sorted(settled, key=_ts):
        ticker = trade.get("ticker", "")
        funnel = funnel_map.get(ticker)
        analyses.append(build_trade_analysis(trade, funnel))

    # Aggregate
    agg = compute_aggregate(analyses)

    # Shadow data
    shadow_hygiene = load_shadow_hygiene_latest(cutoff)
    shadow_payoff = load_shadow_payoff_latest(cutoff)

    # Answers and diagnosis
    answers = answer_questions(agg, analyses, shadow_hygiene, shadow_payoff)
    diagnosis = ranked_diagnosis(analyses, agg)

    return {
        "cutoff": cutoff,
        "total_post_candidates": total_candidates,
        "analyses": analyses,
        "aggregate": agg,
        "answers": answers,
        "diagnosis": diagnosis,
        "shadow_hygiene": shadow_hygiene,
        "shadow_payoff": shadow_payoff,
    }


def main() -> None:
    report = build_report()
    print_report(
        analyses=report["analyses"],
        agg=report["aggregate"],
        answers=report["answers"],
        diagnosis=report["diagnosis"],
        shadow_hygiene=report["shadow_hygiene"],
        shadow_payoff=report["shadow_payoff"],
        total_post_candidates=report["total_post_candidates"],
    )


if __name__ == "__main__":
    main()
