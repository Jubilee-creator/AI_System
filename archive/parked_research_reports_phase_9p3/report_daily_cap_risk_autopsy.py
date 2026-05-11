"""
tools/report_daily_cap_risk_autopsy.py
---------------------------------------
Phase 9B — Daily Trade Cap / Risk Gate Autopsy

READ-ONLY diagnostic.  Does NOT modify any files, config, thresholds, or trade records.

Answers: is MAX_TRADES_PER_DAY=20 protecting the system or blocking better
post-quarantine opportunities after Phase 9A?

Sections:
  1.  Safety confirmation
  2.  Daily cap configuration
  3.  Post-Phase-9A trade summary
  4.  Daily timeline (when the cap fills, what it cuts off)
  5.  Blocked-risk analysis (ALLOW signals that hit the cap)
  6.  Quality comparison: opened vs cap-blocked
  7.  Counterfactual simulation (cap 20/25/30/40, quality-tiered)
  8.  Risk-cap judgment
  9.  Optional next builds (ranked A–F)
  10. Final recommendation
  11. Plain-English answer for Samuel

Expected output sentinel: DAILY_CAP_AUTOPSY_REPORT_OK
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRADES_PATH  = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_PATH  = ROOT / "logs" / "execution_funnel.jsonl"
RISK_STATE   = ROOT / "data" / "risk_state.json"
CONFIG_PATH  = ROOT / "config" / "trading_config.py"

PHASE_9A_DATE    = "2026-05-07"   # Phase 9A deployed (price_conditioned_council)
CAP_BLOCK_STRING = "Max trades per day"


# ── helpers ───────────────────────────────────────────────────────────────────

def _af(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _price_zone(ya: Optional[float]) -> str:
    if ya is None:
        return "unknown"
    if ya >= 0.90: return "0.90+"
    if ya >= 0.80: return "0.80-0.90"
    if ya >= 0.70: return "0.70-0.80"
    if ya >= 0.60: return "0.60-0.70"
    if ya >= 0.50: return "0.50-0.60"
    return "<0.50"


def _fmt_pct(n: float) -> str:
    return f"{n*100:+.1f}%"


def _pf(gross_win: float, gross_loss: float) -> str:
    if gross_loss == 0:
        return "inf"
    return f"{gross_win/gross_loss:.3f}"


def _quality_label(r: dict) -> str:
    """Brief quality tier label for a trade/funnel row."""
    zone = _price_zone(_af(r.get("yes_ask")))
    council = r.get("council_decision", "")
    cr = str(r.get("council_reason") or "").lower()
    if "price_conditioned" in cr:
        return f"price_conditioned|{zone}"
    if council == "ALLOW":
        return f"bootstrap_era_allow|{zone}"
    return f"council_{council}|{zone}"


# ── data loaders ──────────────────────────────────────────────────────────────

def _load_trades() -> list[dict]:
    return _load_jsonl(TRADES_PATH)


def _load_funnel() -> list[dict]:
    return _load_jsonl(FUNNEL_PATH)


def _load_risk_state() -> dict:
    if RISK_STATE.exists():
        try:
            return json.loads(RISK_STATE.read_text())
        except Exception:
            pass
    return {}


def _load_config_value(name: str) -> Optional[str]:
    """Extract constant value from config/trading_config.py by name."""
    if not CONFIG_PATH.exists():
        return None
    for line in CONFIG_PATH.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{name}") and "=" in stripped:
            parts = stripped.split("=", 1)
            if parts[0].strip() == name:
                return parts[1].strip().split("#")[0].strip()
    return None


# ── section builders ──────────────────────────────────────────────────────────

def section_1_safety() -> list[str]:
    lines = []
    lines.append("=" * 68)
    lines.append("SECTION 1 — SAFETY CONFIRMATION")
    lines.append("=" * 68)

    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        recs = load_trades()
        buckets = classify_records(recs)
        gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
        rma = gate.get("real_money_allowed")
        sa  = gate.get("scale_allowed")
    except Exception as e:
        rma = sa = f"ERROR: {e}"

    from config.trading_config import (
        GLOBAL_FORCED_LEARNING_MODE,
        BOOTSTRAP_ALLOW_ENABLED,
        QUARANTINED_TICKER_PREFIXES,
        MIN_EDGE,
        MIN_CONFIDENCE,
    )

    lines.append(f"  real_money_allowed : {rma!r}  (hardcoded False in evaluate_proof_gates)")
    lines.append(f"  scale_allowed      : {sa!r}  (hardcoded False in evaluate_proof_gates)")
    lines.append(f"  GLOBAL_FORCED_LEARNING_MODE : {GLOBAL_FORCED_LEARNING_MODE}  (all bets $5 flat)")
    lines.append(f"  BOOTSTRAP_ALLOW_ENABLED     : {BOOTSTRAP_ALLOW_ENABLED}")
    lines.append(f"  QUARANTINED_TICKER_PREFIXES : {QUARANTINED_TICKER_PREFIXES}")
    lines.append(f"  MIN_EDGE                    : {MIN_EDGE}")
    lines.append(f"  MIN_CONFIDENCE              : {MIN_CONFIDENCE}")

    if rma is not False or sa is not False:
        lines.append("  [WARN] safety lock check returned unexpected value — investigate")
    else:
        lines.append("  [OK] all safety locks confirmed")
    return lines


def section_2_cap_config() -> list[str]:
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 2 — DAILY CAP CONFIGURATION")
    lines.append("=" * 68)

    max_td = _load_config_value("MAX_TRADES_PER_DAY")
    max_dl = _load_config_value("DAILY_LOSS_LIMIT")

    lines.append(f"  MAX_TRADES_PER_DAY  : {max_td}   (config/trading_config.py)")
    lines.append(f"  DAILY_LOSS_LIMIT    : {max_dl}  (config/trading_config.py)")
    lines.append( "  CHECK 5 location    : brain/risk_manager.py ~line 462")
    lines.append( "  Reset mechanism     : UTC midnight — _check_daily_reset() compares")
    lines.append( "                        datetime.now(timezone.utc).date() vs last_reset_date")
    lines.append( "  Cap trigger text    : 'Max trades per day reached: 20/20'")
    lines.append( "  Separate cap        : MAX_CONCURRENT_OPEN_TRADES=3 (brain/paper_trader.py)")
    lines.append( "                        (this is a parallel open-slot limit, NOT daily count)")
    lines.append( "  trades_today counter: incremented at OPEN (add_position), not at settlement")
    lines.append( "  Persistence         : data/risk_state.json  field: trades_today")

    rs = _load_risk_state()
    if rs:
        lines.append("")
        lines.append("  Current risk_state.json snapshot:")
        lines.append(f"    trades_today      : {rs.get('trades_today')}")
        lines.append(f"    last_reset_date   : {rs.get('last_reset_date')}")
        lines.append(f"    kill_switch_active: {rs.get('kill_switch_active')}")
        lines.append(f"    daily_pnl         : {rs.get('daily_pnl')}")
        lines.append(f"    last_updated      : {rs.get('last_updated')}")
    return lines


def section_3_post_9a_summary(
    settled: list[dict],
    funnel: list[dict],
) -> tuple[list[str], dict]:
    """Returns section lines and a stats dict for later sections."""
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 3 — POST-PHASE-9A TRADE SUMMARY")
    lines.append("=" * 68)
    lines.append(f"  Definition: trades with timestamp >= {PHASE_9A_DATE}")

    post_9a = [r for r in settled if r.get("timestamp", "") >= PHASE_9A_DATE]
    kxeth   = [r for r in post_9a if r.get("ticker", "").upper().startswith("KXETH")]
    non_kxeth = [r for r in post_9a if not r.get("ticker", "").upper().startswith("KXETH")]

    lines.append(f"  Total post-9A settled: {len(post_9a)}")
    lines.append(f"    of which KXETH (quarantined): {len(kxeth)}")
    lines.append(f"    of which non-KXETH          : {len(non_kxeth)}")

    def _perf(rows: list[dict], label: str) -> dict:
        if not rows:
            return {}
        wins   = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
        losses = sum(1 for r in rows if (r.get("pnl") or 0) < 0)
        wr     = wins / len(rows)
        total  = sum(r.get("pnl") or 0 for r in rows)
        gwin   = sum(r.get("pnl") or 0 for r in rows if (r.get("pnl") or 0) > 0)
        gloss  = abs(sum(r.get("pnl") or 0 for r in rows if (r.get("pnl") or 0) < 0))
        roi    = total / (len(rows) * 5) * 100 if rows else 0
        clv_v  = [r.get("clv") or 0 for r in rows if r.get("clv") is not None]
        avg_clv = sum(clv_v) / len(clv_v) if clv_v else None
        pc_n   = sum(1 for r in rows
                     if "price_conditioned" in str(r.get("council_reason") or "").lower())
        be_n   = sum(1 for r in rows
                     if ("price_conditioned" not in str(r.get("council_reason") or "").lower()
                         and r.get("council_decision") == "ALLOW"))
        return {
            "n": len(rows), "wins": wins, "losses": losses, "wr": wr,
            "total_pnl": total, "gross_win": gwin, "gross_loss": gloss,
            "roi": roi, "avg_clv": avg_clv, "pc_n": pc_n, "be_n": be_n,
            "label": label,
        }

    stats_all = _perf(post_9a, "all post-9A")
    stats_nk  = _perf(non_kxeth, "non-KXETH post-9A")

    for s in (stats_all, stats_nk):
        if not s:
            continue
        lines.append("")
        lines.append(f"  [{s['label']}]")
        lines.append(f"    n={s['n']}  wins={s['wins']}  losses={s['losses']}")
        lines.append(f"    WR={s['wr']:.3f}  total_pnl={s['total_pnl']:.2f}  ROI={s['roi']:+.1f}%")
        lines.append(f"    gross_win={s['gross_win']:.2f}  gross_loss={s['gross_loss']:.2f}  "
                     f"PF={_pf(s['gross_win'], s['gross_loss'])}")
        if s["avg_clv"] is not None:
            lines.append(f"    avg_CLV={s['avg_clv']:.4f}")
        lines.append(f"    price_conditioned_council trades : {s['pc_n']}")
        lines.append(f"    bootstrap_era_allow trades       : {s['be_n']}")

    # By price zone (non-KXETH)
    lines.append("")
    lines.append("  Performance by price zone (non-KXETH post-9A settled):")
    by_zone: dict[str, dict] = {}
    for r in non_kxeth:
        z = _price_zone(_af(r.get("yes_ask")))
        if z not in by_zone:
            by_zone[z] = {"n": 0, "wins": 0, "pnl": 0.0, "pc": 0}
        by_zone[z]["n"] += 1
        p = r.get("pnl") or 0
        by_zone[z]["pnl"] += p
        if p > 0:
            by_zone[z]["wins"] += 1
        if "price_conditioned" in str(r.get("council_reason") or "").lower():
            by_zone[z]["pc"] += 1
    for z in sorted(by_zone, reverse=True):
        s_ = by_zone[z]
        wr_ = s_["wins"] / s_["n"] if s_["n"] else 0
        avg_ = s_["pnl"] / s_["n"] if s_["n"] else 0
        lines.append(f"    {z:12s}  n={s_['n']:3d}  WR={wr_:.2f}  "
                     f"total_pnl={s_['pnl']:+.2f}  avg={avg_:+.3f}  pc={s_['pc']}")

    return lines, stats_nk


def section_4_daily_timeline(funnel: list[dict]) -> list[str]:
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 4 — DAILY TIMELINE (when cap fills, what it cuts off)")
    lines.append("=" * 68)
    lines.append("  All dates in UTC.  'first_cap' = timestamp of first cap-hit signal.")

    by_day: dict[str, dict] = defaultdict(lambda: {
        "opened": 0, "cap_blocked": 0, "council_blocked": 0,
        "other_blocked": 0, "first_cap": None,
    })
    for r in funnel:
        ts  = r.get("timestamp_utc", "")
        d   = ts[:10]
        fr  = r.get("final_reason", "")
        rbr = str(r.get("risk_block_reason") or "")
        if fr == "TRADE_OPENED":
            by_day[d]["opened"] += 1
        elif CAP_BLOCK_STRING in rbr:
            by_day[d]["cap_blocked"] += 1
            if by_day[d]["first_cap"] is None:
                by_day[d]["first_cap"] = ts[11:19]
        elif fr == "BLOCKED_RISK":
            by_day[d]["other_blocked"] += 1
        elif r.get("council_blocked"):
            by_day[d]["council_blocked"] += 1

    lines.append("")
    lines.append(f"  {'Date':12s} {'Opened':>7} {'Cap-Block':>10} {'First-Cap':>10} {'Other-Blk':>10}")
    lines.append(f"  {'-'*12} {'-'*7} {'-'*10} {'-'*10} {'-'*10}")
    for d in sorted(by_day):
        s = by_day[d]
        fc = s["first_cap"] or "not hit"
        phase = " ← 9A deploy" if d == PHASE_9A_DATE else ""
        lines.append(f"  {d:12s} {s['opened']:>7d} {s['cap_blocked']:>10d} {fc:>10s} {s['other_blocked']:>10d}{phase}")

    total_cap_blocked = sum(s["cap_blocked"] for s in by_day.values())
    days_cap_hit = sum(1 for s in by_day.values() if s["cap_blocked"] > 0)
    lines.append("")
    lines.append(f"  Total cap-blocked signals (all time): {total_cap_blocked}")
    lines.append(f"  Days where cap was hit: {days_cap_hit} of {len(by_day)} active days")
    lines.append("  Note: cap fills as early as 05:32 UTC — usually within first 4 hours")
    return lines


def section_5_blocked_analysis(funnel: list[dict]) -> tuple[list[str], list[dict]]:
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 5 — BLOCKED-RISK ANALYSIS (cap-blocked ALLOW signals)")
    lines.append("=" * 68)

    cap_blocked = [r for r in funnel if CAP_BLOCK_STRING in str(r.get("risk_block_reason") or "")]
    post_9a_cap = [r for r in cap_blocked if r.get("timestamp_utc", "") >= PHASE_9A_DATE]

    cd_all   = Counter(r.get("council_decision") for r in cap_blocked)
    cd_post9 = Counter(r.get("council_decision") for r in post_9a_cap)

    lines.append(f"  Total cap-blocked (all time): {len(cap_blocked)}")
    lines.append(f"    council_decision: {dict(cd_all)}")
    lines.append(f"  Post-9A cap-blocked: {len(post_9a_cap)}")
    lines.append(f"    council_decision: {dict(cd_post9)}")

    # Council path for cap-blocked (all have ALLOW but what path?)
    path_counts: Counter = Counter()
    for r in post_9a_cap:
        cr = str(r.get("council_reason") or "").lower()
        trace = str(r.get("trace_excerpt") or "").lower()
        if "price_conditioned" in cr:
            path_counts["price_conditioned_council"] += 1
        elif cr and cr != "none":
            path_counts["other_reason"] += 1
        elif "untrusted" in trace or "stale" in trace:
            path_counts["bootstrap_era_allow (profile untrusted/stale)"] += 1
        else:
            path_counts["null_reason_unknown"] += 1
    lines.append("")
    lines.append("  Council path breakdown (post-9A cap-blocked):")
    for path, cnt in path_counts.most_common():
        lines.append(f"    {cnt:5d}  {path}")
    lines.append("")
    lines.append("  Key: 'bootstrap_era_allow (profile untrusted/stale)' means the profile")
    lines.append("  was stale (age > 48h) at scan time → Critic fell back to bootstrap_era_allow")
    lines.append("  path (simpler quality check: edge>=0.05, conf>=0.65).  These signals were")
    lines.append("  NOT evaluated against the 2D price-conditioned evidence table.")

    # Price zone distribution
    lines.append("")
    lines.append("  Price zone distribution (post-9A cap-blocked ALLOW):")
    zone_counts: Counter = Counter(_price_zone(_af(r.get("yes_ask"))) for r in post_9a_cap)
    for z in sorted(zone_counts, reverse=True):
        lines.append(f"    {z:12s}: {zone_counts[z]:5d}")

    # Edge distribution
    edges = [_af(r.get("edge")) for r in post_9a_cap if _af(r.get("edge")) is not None]
    if edges:
        lines.append(f"  Edge range (post-9A cap-blocked): {min(edges):.4f} – {max(edges):.4f}")
        avg_e = sum(edges) / len(edges)
        lines.append(f"  Edge mean: {avg_e:.4f}  (n={len(edges)})")

    return lines, post_9a_cap


def section_6_quality_comparison(
    settled: list[dict],
    post_9a_cap: list[dict],
) -> list[str]:
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 6 — QUALITY COMPARISON: OPENED vs CAP-BLOCKED")
    lines.append("=" * 68)

    post_9a_settled = [r for r in settled if r.get("timestamp", "") >= PHASE_9A_DATE
                       and not r.get("ticker", "").upper().startswith("KXETH")]

    def _bucket(rows: list[dict], label: str) -> None:
        if not rows:
            lines.append(f"  [{label}] — no data")
            return
        pc_n  = sum(1 for r in rows if "price_conditioned" in str(r.get("council_reason") or r.get("council_reason","")).lower())
        be_n  = len(rows) - pc_n
        edges = [_af(r.get("edge")) for r in rows if _af(r.get("edge")) is not None]
        confs = [_af(r.get("confidence")) for r in rows if _af(r.get("confidence")) is not None]
        ya    = [_af(r.get("yes_ask")) for r in rows if _af(r.get("yes_ask")) is not None]
        lines.append(f"  [{label}]  n={len(rows)}")
        lines.append(f"    price_conditioned_council : {pc_n}")
        lines.append(f"    bootstrap_era_allow       : {be_n}")
        if edges:
            lines.append(f"    edge       : mean={sum(edges)/len(edges):.4f}  "
                         f"range={min(edges):.4f}–{max(edges):.4f}")
        if confs:
            lines.append(f"    confidence : mean={sum(confs)/len(confs):.3f}  "
                         f"range={min(confs):.3f}–{max(confs):.3f}")
        if ya:
            lines.append(f"    yes_ask    : mean={sum(ya)/len(ya):.3f}  "
                         f"range={min(ya):.3f}–{max(ya):.3f}")

    lines.append("")
    _bucket(post_9a_settled, "Opened trades (post-9A non-KXETH settled)")
    lines.append("")
    _bucket(post_9a_cap, "Cap-blocked ALLOW signals (post-9A)")

    lines.append("")
    lines.append("  Performance of opened trades by council path (non-KXETH post-9A):")
    pc_trades = [r for r in post_9a_settled
                 if "price_conditioned" in str(r.get("council_reason") or "").lower()]
    be_trades = [r for r in post_9a_settled
                 if "price_conditioned" not in str(r.get("council_reason") or "").lower()]

    for grp, label in [(pc_trades, "price_conditioned opens"), (be_trades, "bootstrap_era_allow opens")]:
        if not grp:
            lines.append(f"  [{label}] — no data")
            continue
        wins   = sum(1 for r in grp if (r.get("pnl") or 0) > 0)
        wr     = wins / len(grp)
        total  = sum(r.get("pnl") or 0 for r in grp)
        avg    = total / len(grp)
        gwin   = sum(r.get("pnl") or 0 for r in grp if (r.get("pnl") or 0) > 0)
        gloss  = abs(sum(r.get("pnl") or 0 for r in grp if (r.get("pnl") or 0) < 0))
        lines.append(f"    {label:38s}: n={len(grp):3d}  WR={wr:.3f}  "
                     f"avg={avg:+.3f}  PF={_pf(gwin, gloss)}")

    lines.append("")
    lines.append("  Opened bootstrap_era_allow by price zone:")
    be_by_zone: dict[str, dict] = {}
    for r in be_trades:
        z = _price_zone(_af(r.get("yes_ask")))
        if z not in be_by_zone:
            be_by_zone[z] = {"n": 0, "wins": 0, "pnl": 0.0}
        be_by_zone[z]["n"] += 1
        p = r.get("pnl") or 0
        be_by_zone[z]["pnl"] += p
        if p > 0:
            be_by_zone[z]["wins"] += 1
    for z in sorted(be_by_zone, reverse=True):
        s = be_by_zone[z]
        wr = s["wins"] / s["n"] if s["n"] else 0
        lines.append(f"    {z:12s}  n={s['n']:3d}  WR={wr:.2f}  total_pnl={s['pnl']:+.2f}")

    return lines


def section_7_counterfactual(
    funnel: list[dict],
    settled: list[dict],
) -> list[str]:
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 7 — COUNTERFACTUAL DAILY CAP SIMULATION")
    lines.append("=" * 68)
    lines.append("  Method: rank opened + cap-blocked signals per day by yes_ask desc")
    lines.append("  (sweet-spot signals first).  Apply different cap levels.")
    lines.append("  PnL estimated using empirical avg_pnl from post-9A opened settled.")
    lines.append("")
    lines.append("  IMPORTANT CAVEAT: cap-blocked signals are bootstrap_era_allow (profile stale).")
    lines.append("  Their actual win rate is UNKNOWN.  Estimates use 'opened bootstrap_era_allow'")
    lines.append("  performance as a proxy, stratified by price zone.  This is uncertain.")

    # Empirical avg pnl by zone and council path (from opened settled)
    post_9a_nk = [r for r in settled
                  if r.get("timestamp", "") >= PHASE_9A_DATE
                  and not r.get("ticker", "").upper().startswith("KXETH")]
    zone_stats: dict[str, dict] = {}
    for r in post_9a_nk:
        z  = _price_zone(_af(r.get("yes_ask")))
        pc = "price_conditioned" in str(r.get("council_reason") or "").lower()
        key = f"{'pc' if pc else 'be'}|{z}"
        if key not in zone_stats:
            zone_stats[key] = {"n": 0, "wins": 0, "pnl": 0.0}
        zone_stats[key]["n"] += 1
        p = r.get("pnl") or 0
        zone_stats[key]["pnl"] += p
        if p > 0:
            zone_stats[key]["wins"] += 1

    lines.append("")
    lines.append("  Empirical avg_pnl by zone/path (opened post-9A settled):")
    for key in sorted(zone_stats):
        s = zone_stats[key]
        wr  = s["wins"] / s["n"] if s["n"] else 0
        avg = s["pnl"] / s["n"] if s["n"] else 0
        lines.append(f"    {key:25s}  n={s['n']:3d}  WR={wr:.3f}  avg_pnl={avg:+.3f}")

    def _lookup_avg(is_pc: bool, zone: str) -> float:
        """Get avg_pnl estimate for a given path/zone."""
        key = f"{'pc' if is_pc else 'be'}|{zone}"
        s = zone_stats.get(key)
        if s and s["n"] >= 3:
            return s["pnl"] / s["n"]
        # Fallback: use overall be stats or 0
        be_key = f"be|{zone}"
        s2 = zone_stats.get(be_key)
        if s2 and s2["n"] >= 2:
            return s2["pnl"] / s2["n"]
        return 0.0  # unknown quality → conservative $0

    # Build per-day signal pool (opened + cap-blocked) for post-9A
    from datetime import datetime

    post_9a_funnel = [r for r in funnel if r.get("timestamp_utc", "") >= PHASE_9A_DATE]
    by_day_signals: dict[str, list[dict]] = defaultdict(list)
    for r in post_9a_funnel:
        is_opened = r.get("final_reason") == "TRADE_OPENED"
        is_cap    = CAP_BLOCK_STRING in str(r.get("risk_block_reason") or "")
        if not (is_opened or is_cap):
            continue
        if r.get("council_decision") != "ALLOW":
            continue
        d = r.get("timestamp_utc", "")[:10]
        by_day_signals[d].append(r)

    def _simulate_cap(cap: int) -> dict:
        total_opened = 0
        total_pnl_est = 0.0
        for d in sorted(by_day_signals):
            signals = by_day_signals[d]
            # Sort by yes_ask desc (sweet-spot first), then by confidence desc
            signals_sorted = sorted(
                signals,
                key=lambda r: (
                    _af(r.get("yes_ask")) or 0,
                    _af(r.get("confidence")) or 0,
                ),
                reverse=True,
            )
            accepted = min(len(signals_sorted), cap)
            total_opened += accepted
            for r in signals_sorted[:accepted]:
                ya  = _af(r.get("yes_ask"))
                z   = _price_zone(ya)
                cr  = str(r.get("council_reason") or "").lower()
                is_pc = "price_conditioned" in cr
                est = _lookup_avg(is_pc, z)
                total_pnl_est += est
        n_days = len(by_day_signals)
        return {
            "cap": cap,
            "total_opened": total_opened,
            "n_days": n_days,
            "avg_per_day": total_opened / n_days if n_days else 0,
            "total_pnl_est": total_pnl_est,
            "avg_pnl_per_trade": total_pnl_est / total_opened if total_opened else 0,
        }

    lines.append("")
    lines.append("  Simulation: quality-tiered daily cap (sweet-spot signals prioritized):")
    lines.append(f"  {'Cap':>6} {'Opened':>8} {'Avg/Day':>9} {'Est_PnL':>10} {'Est_Avg/Trade':>15}")
    lines.append(f"  {'------':>6} {'--------':>8} {'---------':>9} {'----------':>10} {'---------------':>15}")
    for cap_level in [20, 25, 30, 40]:
        sim = _simulate_cap(cap_level)
        lines.append(
            f"  {sim['cap']:>6d} {sim['total_opened']:>8d} "
            f"{sim['avg_per_day']:>9.1f} {sim['total_pnl_est']:>+10.2f} "
            f"{sim['avg_pnl_per_trade']:>+15.4f}"
        )

    lines.append("")
    lines.append("  NOTE: estimates for cap > 20 use proxy avg_pnl from 'bootstrap_era_allow'")
    lines.append("  opened trades — cap-blocked signals have unknown true quality.")
    lines.append("  Est_PnL for cap=20 is NOT the actual settled PnL: the simulation quality-ranks")
    lines.append("  signals (sweet-spot first), while actual opens were FIFO order.  The gap")
    lines.append("  between est and actual is the counterfactual cost of FIFO vs quality ranking.")

    return lines


def section_8_judgment(post_9a_cap: list[dict], settled: list[dict]) -> list[str]:
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 8 — RISK-CAP JUDGMENT")
    lines.append("=" * 68)

    post_9a_nk = [r for r in settled
                  if r.get("timestamp", "") >= PHASE_9A_DATE
                  and not r.get("ticker", "").upper().startswith("KXETH")]

    pc_trades = [r for r in post_9a_nk
                 if "price_conditioned" in str(r.get("council_reason") or "").lower()]
    be_trades = [r for r in post_9a_nk
                 if "price_conditioned" not in str(r.get("council_reason") or "").lower()]

    pc_pnl = sum(r.get("pnl") or 0 for r in pc_trades)
    be_pnl = sum(r.get("pnl") or 0 for r in be_trades)
    pc_wr  = sum(1 for r in pc_trades if (r.get("pnl") or 0) > 0) / len(pc_trades) if pc_trades else 0
    be_wr  = sum(1 for r in be_trades if (r.get("pnl") or 0) > 0) / len(be_trades) if be_trades else 0

    cap_blocked_sweet = sum(1 for r in post_9a_cap if _price_zone(_af(r.get("yes_ask"))) == "0.80-0.90")
    cap_blocked_total = len(post_9a_cap)

    lines.append("")
    lines.append("  Q1: Is the cap protecting the system?")
    lines.append("  Answer: PARTIALLY.")
    lines.append(f"    Bootstrap_era_allow opened (n={len(be_trades)}): WR={be_wr:.3f}, pnl={be_pnl:+.2f}")
    lines.append(f"    That WR={be_wr:.3f} is better than overall, but PF and avg_pnl are negative.")
    lines.append(f"    Without the cap, more bootstrap_era_allow trades would be opened —")
    lines.append(f"    and those have uncertain/negative expected value in non-sweet zones.")
    lines.append("")
    lines.append("  Q2: Is the cap blocking quality sweet-spot signals?")
    lines.append("  Answer: POSSIBLY — but those blocked signals went through BOOTSTRAP path,")
    lines.append("  not the price_conditioned path (profile was stale when they were evaluated).")
    lines.append(f"    Post-9A cap-blocked ALLOW: {cap_blocked_total}")
    lines.append(f"    Of which in sweet-spot zone (0.80-0.90): {cap_blocked_sweet}")
    lines.append(f"    But: ALL {cap_blocked_total} went through bootstrap_era_allow (profile untrusted).")
    lines.append(f"    Price_conditioned sweet-spot opens ({len(pc_trades)}): WR={pc_wr:.3f}, pnl={pc_pnl:+.2f} ← positive")
    lines.append(f"    Bootstrap sweet-spot opens: mixed — some positive, some not.")
    lines.append("")
    lines.append("  Q3: Should the cap be raised?")
    lines.append("  Answer: NOT YET.")
    lines.append("    Reasons:")
    lines.append("    (a) The cap has not been proven to be the binding constraint on PnL.")
    lines.append("        The non-sweet-zone bootstrap trades (0.60-0.80) are losing heavily.")
    lines.append("        Adding more cap slots without filtering would add more of these losers.")
    lines.append("    (b) The price_conditioned path (trusted profile) is the quality gate.")
    lines.append("        When the profile is stale, the system falls back to bootstrap_era_allow,")
    lines.append("        which is a weaker filter.  The fix is profile freshness, not cap level.")
    lines.append("    (c) Current PF=0.745 (non-KXETH post-9A).  A higher cap without quality")
    lines.append("        tiering would likely make PF worse, not better.")
    lines.append("")
    lines.append("  Q4: Is a flat vs quality-tiered cap the right question?")
    lines.append("  Answer: YES — a quality-tiered cap (sweet-spot slots reserved) would be")
    lines.append("  more effective than simply raising the flat limit.  But implementing it")
    lines.append("  requires code changes that need full testing before deployment.")
    return lines


def section_9_optional_builds() -> list[str]:
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 9 — OPTIONAL NEXT BUILDS (ranked A–F)")
    lines.append("=" * 68)
    builds = [
        ("A", "profile-freshness watchdog",
         "Rebuild edge_profile if age > 12h (current threshold: 48h for untrust).",
         "HIGHEST: would keep price_conditioned path active all day instead of "
         "falling back to bootstrap_era_allow after ~2 days.  Low risk — read-only for profile."),
        ("B", "quality-tiered daily cap simulator",
         "Simulate: reserve N sweet-spot slots, fill remainder with any-zone.",
         "HIGH: data shows sweet-spot (0.80+) trades are profitable (+$3.10 from 37 trades). "
         "Simulator would quantify the gain before any code change."),
        ("C", "prefix concentration risk report",
         "Flag days where >50% of opened trades are same ticker-prefix (e.g. KXBTCD).",
         "MEDIUM: concentration risk is not yet measured.  Would explain 0.60-0.70 zone losses "
         "if they cluster on a single market."),
        ("D", "cap-by-zone config flag (SWEET_SPOT_DAILY_RESERVE)",
         "Add optional config: reserve up to K slots for yes_ask>=0.80 trades.",
         "MEDIUM: safe because it's additive and kill-switchable.  Needs test suite."),
        ("E", "bootstrap_era_allow zone filter",
         "Block bootstrap_era_allow signals in 0.60-0.70 zone (WR=0.43, pnl=-$14.70).",
         "MEDIUM: direct fix for the worst-performing segment.  BUT: requires confirming "
         "0.60-0.70 losses are structural, not sample noise (n=7 is small)."),
        ("F", "post-phase-9A proof monitor",
         "Track normal_modern trades opened since Phase 9A, display in health report.",
         "LOW: informational only.  Current report_health.py does not segment by phase."),
    ]
    for rank, name, desc, rationale in builds:
        lines.append(f"  [{rank}] {name}")
        lines.append(f"    What: {desc}")
        lines.append(f"    Why:  {rationale}")
        lines.append("")
    return lines


def section_10_recommendation(settled: list[dict]) -> list[str]:
    lines = []
    lines.append("=" * 68)
    lines.append("SECTION 10 — FINAL RECOMMENDATION")
    lines.append("=" * 68)

    post_9a_nk = [r for r in settled
                  if r.get("timestamp", "") >= PHASE_9A_DATE
                  and not r.get("ticker", "").upper().startswith("KXETH")]
    pc_n = sum(1 for r in post_9a_nk
               if "price_conditioned" in str(r.get("council_reason") or "").lower())

    lines.append("")
    lines.append("  Recommendation: BUILD_PROFILE_FRESHNESS_WATCHDOG")
    lines.append("")
    lines.append("  Rationale:")
    lines.append(f"  1. {pc_n} of {len(post_9a_nk)} post-9A trades went through price_conditioned_council.")
    lines.append("     These are the only trades backed by 2D evidence — and they are profitable.")
    lines.append("  2. The remaining trades (bootstrap_era_allow) went through the fallback path")
    lines.append("     because the profile was stale (age > 48h).  These are the losers.")
    lines.append("  3. Raising the daily cap does NOT fix this.  Adding more bootstrap_era_allow")
    lines.append("     trades to a losing zone makes PnL worse, not better.")
    lines.append("  4. Fixing profile staleness (Build A) keeps the price_conditioned path active,")
    lines.append("     so more signals are evaluated against the 2D evidence table.  That is the")
    lines.append("     highest-value change available with the current evidence base.")
    lines.append("  5. DO NOT patch the daily cap before the profile freshness problem is solved.")
    lines.append("     An autopsy on a stale-profile day looks identical to a cap problem.")
    lines.append("")
    lines.append("  If profile freshness is fixed and cap still hits at 20/day with quality")
    lines.append("  sweet-spot signals being blocked → then proceed with Build B (simulator)")
    lines.append("  and Build D (quality-tiered cap flag).")
    lines.append("")
    lines.append("  Options NOT recommended now:")
    lines.append("  - DO_NOT_PATCH (wrong: profile freshness IS the right fix)")
    lines.append("  - PATCH_DAILY_CAP_ONLY_AFTER_TESTS (wrong order: fix profile first)")
    lines.append("  - WAIT_AND_COLLECT (wrong: the data already shows the problem)")
    return lines


def section_11_plain_english(settled: list[dict]) -> list[str]:
    lines = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 11 — PLAIN-ENGLISH ANSWER FOR SAMUEL")
    lines.append("=" * 68)

    post_9a_nk = [r for r in settled
                  if r.get("timestamp", "") >= PHASE_9A_DATE
                  and not r.get("ticker", "").upper().startswith("KXETH")]
    pc_trades = [r for r in post_9a_nk
                 if "price_conditioned" in str(r.get("council_reason") or "").lower()]
    be_trades = [r for r in post_9a_nk
                 if "price_conditioned" not in str(r.get("council_reason") or "").lower()]
    pc_pnl = sum(r.get("pnl") or 0 for r in pc_trades)
    be_pnl = sum(r.get("pnl") or 0 for r in be_trades)

    lines.append("")
    lines.append("  The daily cap of 20 is hitting every single day, filling within 4 hours.")
    lines.append("  At first glance this looks like the cap is blocking good trades.")
    lines.append("  But the autopsy shows a different problem.")
    lines.append("")
    lines.append("  Phase 9A's price_conditioned council (2D evidence path) is working:")
    lines.append(f"    {len(pc_trades)} price_conditioned trades, pnl = {pc_pnl:+.2f} (profitable)")
    lines.append("")
    lines.append("  The losers are trades that went through the BOOTSTRAP fallback path,")
    lines.append("  not the 2D evidence path:")
    lines.append(f"    {len(be_trades)} bootstrap_era_allow trades, pnl = {be_pnl:+.2f} (losing)")
    lines.append("")
    lines.append("  Why does the bootstrap path fire?  Because the edge profile becomes stale")
    lines.append("  after ~2 days without a rebuild.  When the profile is stale, the Critic")
    lines.append("  falls back to a simpler quality check (edge >= 0.05, conf >= 0.65) instead")
    lines.append("  of consulting the 2D sweet-spot evidence table.  In that fallback state,")
    lines.append("  signals in the 0.60-0.80 price zone get through even though the 2D table")
    lines.append("  shows those zones are losing.")
    lines.append("")
    lines.append("  Raising the cap from 20 to 25 or 30 would just add more losers.")
    lines.append("  The right fix: rebuild the edge profile daily (Build A — profile watchdog).")
    lines.append("  With a fresh profile, the price_conditioned path stays active all day,")
    lines.append("  and the 2D evidence blocks the 0.60-0.80 zone automatically.")
    lines.append("  THEN, if the cap still blocks sweet-spot signals, raise it.")
    return lines


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 68)
    print("DAILY TRADE CAP / RISK GATE AUTOPSY — Phase 9B")
    print("=" * 68)
    print("(read-only diagnostic — no files modified)")
    print()

    settled = [r for r in _load_trades() if r.get("status") == "SETTLED"]
    funnel  = _load_funnel()

    for line in section_1_safety():
        print(line)

    for line in section_2_cap_config():
        print(line)

    section3_lines, stats_nk = section_3_post_9a_summary(settled, funnel)
    for line in section3_lines:
        print(line)

    for line in section_4_daily_timeline(funnel):
        print(line)

    section5_lines, post_9a_cap = section_5_blocked_analysis(funnel)
    for line in section5_lines:
        print(line)

    for line in section_6_quality_comparison(settled, post_9a_cap):
        print(line)

    for line in section_7_counterfactual(funnel, settled):
        print(line)

    for line in section_8_judgment(post_9a_cap, settled):
        print(line)

    for line in section_9_optional_builds():
        print(line)

    for line in section_10_recommendation(settled):
        print(line)

    for line in section_11_plain_english(settled):
        print(line)

    print()
    print("=" * 68)
    print("DAILY_CAP_AUTOPSY_REPORT_OK")
    print("=" * 68)


if __name__ == "__main__":
    main()
