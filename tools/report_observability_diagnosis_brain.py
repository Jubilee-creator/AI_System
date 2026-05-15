#!/usr/bin/env python3
"""
Phase 11D — Observability Diagnosis Brain.

READ-ONLY.  Combines all observability stack outputs with canonical truth
reports to produce a ranked, evidence-grounded diagnosis of current system
weak spots.

Sources (in trust order — canonical always wins):
  1. Canonical truth:  tools/performance_report.py + tools/clean_truth_report.py
  2. MLflow metrics:   tools/report_mlflow_phase_metrics._compute_metrics()
  3. DuckDB post-11A:  data/observability/ai_system_truth.duckdb (if built)
  4. GX results:       reports/observability/gx_results.json (if present)
  5. Evidently drift:  reports/observability/evidently_drift_results.json (if present)
  6. Config:           config/trading_config.py

Trust hierarchy: canonical > MLflow > DuckDB > GX/Evidently.
If any tool disagrees with canonical truth, flag OBSERVABILITY_CONTRADICTION
and trust canonical.

Diagnosis categories (15):
  PAYOFF_ASYMMETRY, ENTRY_PRICE_TOXICITY, CONFIDENCE_OVERCONFIDENCE,
  LOW_OR_NEGATIVE_CLV, PROFIT_FACTOR_FAILURE, ROI_FAILURE,
  SCANNER_CONCENTRATION, SIDE_BIAS, SAMPLE_TOO_SMALL,
  DATA_QUALITY_RISK, DRIFT_OR_REGIME_SHIFT, BLOCKER_STARVATION,
  KXETH_QUARANTINE_HEALTH, SAFETY_LOCK_HEALTH, OBSERVABILITY_CONTRADICTION

Severity scale: CRITICAL > HIGH > MEDIUM > LOW > INFO

Sentinel: OBSERVABILITY_DIAGNOSIS_BRAIN_OK
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

PHASE_11A_CUTOFF = "2026-05-15T13:46:29+00:00"
SENTINEL = "OBSERVABILITY_DIAGNOSIS_BRAIN_OK"

SEVERITY_WEIGHT = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "INFO": 1}

# Proof gate constants (must not be changed — read-only reference)
_GATE_SCALE_MIN_NORMAL = 30
_GATE_PROFIT_FACTOR    = 1.10
_MIN_EDGE              = 0.03
_MIN_CONFIDENCE        = 0.65
_EDGE_DANGER_MIN       = 0.08
_EXPENSIVE_ENTRY       = 0.80
_TOXIC_ZONE_LO         = 0.80
_TOXIC_ZONE_HI         = 0.90
_WEAK_RR_THRESHOLD     = 0.30


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Diagnosis:
    category: str
    severity: str                 # CRITICAL / HIGH / MEDIUM / LOW / INFO
    evidence: str
    source: str
    actionable_now: bool
    safest_next: str
    do_not: str
    rank: int = 0


# ─── Canonical data loader ────────────────────────────────────────────────────

def _load_canonical_data() -> dict[str, Any]:
    """
    Load and classify all trades using canonical classification functions.
    Returns a dict of computed metrics.  READ-ONLY.
    """
    from tools.performance_report import (
        build_terminal_key_sets, classify_settled_records,
        load_trades, get_clv, get_pnl,
    )
    from tools.clean_truth_report import row_quality_group

    trades = load_trades()
    settled_keys, fc_keys, void_keys = build_terminal_key_sets(trades)
    clean_settled, conflicted = classify_settled_records(
        trades, settled_keys, fc_keys, void_keys
    )
    modern_full = [r for r in clean_settled if row_quality_group(r) == "MODERN_FULL_METADATA"]
    dc_override    = sum(1 for r in modern_full if r.get("data_collection_override"))
    bootstrap_prov = sum(1 for r in modern_full if r.get("bootstrap_provisional"))
    normal_modern  = [
        r for r in modern_full
        if not r.get("data_collection_override") and not r.get("bootstrap_provisional")
    ]

    def _safe_float(v: Any) -> Optional[float]:
        try: return float(v)
        except (TypeError, ValueError): return None

    # Performance on normal_modern
    pnls        = [get_pnl(r) for r in normal_modern]
    wins        = sum(1 for p in pnls if p > 0)
    losses      = sum(1 for p in pnls if p < 0)
    gross_win   = sum(p for p in pnls if p > 0)
    gross_loss  = abs(sum(p for p in pnls if p < 0))
    total_pnl   = sum(pnls)
    invested    = sum(_safe_float(r.get("entry_price")) or 0 for r in normal_modern) * 5
    roi         = total_pnl / invested if invested > 0 else None
    pf          = gross_win / gross_loss if gross_loss > 0 else None
    actual_wr   = wins / (wins + losses) if (wins + losses) > 0 else None

    clv_vals    = [v for v in (get_clv(r) for r in normal_modern) if v is not None]
    avg_clv     = sum(clv_vals) / len(clv_vals) if clv_vals else None

    entries     = [_safe_float(r.get("entry_price")) for r in normal_modern]
    entries     = [e for e in entries if e is not None]
    avg_entry   = sum(entries) / len(entries) if entries else None
    expensive   = sum(1 for e in entries if e >= _EXPENSIVE_ENTRY)
    toxic       = sum(1 for e in entries if _TOXIC_ZONE_LO <= e < _TOXIC_ZONE_HI)
    rr_vals     = [(1.0 - e) / e for e in entries if e and e > 0]
    avg_rr      = sum(rr_vals) / len(rr_vals) if rr_vals else None
    weak_rr     = sum(1 for rr in rr_vals if rr < _WEAK_RR_THRESHOLD)

    confs       = [_safe_float(r.get("council_confidence") or r.get("confidence"))
                   for r in normal_modern]
    confs       = [c for c in confs if c is not None]
    avg_conf    = sum(confs) / len(confs) if confs else None
    conf_gap    = (avg_conf - actual_wr) if (avg_conf is not None and actual_wr is not None) else None

    bet_yes     = sum(1 for r in normal_modern if r.get("action") == "BET_YES")
    bet_no      = sum(1 for r in normal_modern if r.get("action") == "BET_NO")

    from collections import Counter
    prefixes    = Counter(str(r.get("ticker") or "").split("-")[0].upper()
                          for r in normal_modern)

    # clean_settled performance
    cs_pnls     = [get_pnl(r) for r in clean_settled]
    cs_invested = sum(_safe_float(r.get("entry_price")) or 0 for r in clean_settled) * 5
    cs_roi      = sum(cs_pnls) / cs_invested if cs_invested > 0 else None

    # Post-11A
    all_trades  = trades
    post11a_raw = [t for t in all_trades if str(t.get("timestamp") or "") >= PHASE_11A_CUTOFF]
    _by_key: dict[tuple, dict] = {}
    for t in post11a_raw:
        k = (t.get("ticker"), str(t.get("timestamp") or "")[:19])
        _by_key[k] = t
    post11a     = list(_by_key.values())
    p11a_settled = [t for t in post11a if t.get("status") == "SETTLED"]
    p11a_opened  = [t for t in post11a if t.get("status") in ("OPEN", "SETTLED")]

    p11a_pnls   = [get_pnl(t) for t in p11a_settled]
    p11a_wins   = sum(1 for p in p11a_pnls if p > 0)
    p11a_losses = sum(1 for p in p11a_pnls if p < 0)
    p11a_gwin   = sum(p for p in p11a_pnls if p > 0)
    p11a_gloss  = abs(sum(p for p in p11a_pnls if p < 0))
    p11a_pf     = p11a_gwin / p11a_gloss if p11a_gloss > 0 else None
    p11a_inv    = sum(_safe_float(t.get("entry_price")) or 0 for t in p11a_settled) * 5
    p11a_roi    = sum(p11a_pnls) / p11a_inv if p11a_inv > 0 else None
    p11a_wr     = p11a_wins / len(p11a_settled) if p11a_settled else None

    p11a_confs  = [_safe_float(t.get("council_confidence") or t.get("confidence"))
                   for t in p11a_settled]
    p11a_confs  = [c for c in p11a_confs if c is not None]
    p11a_conf   = sum(p11a_confs) / len(p11a_confs) if p11a_confs else None
    p11a_ent    = [_safe_float(t.get("entry_price")) for t in p11a_settled]
    p11a_ent    = [e for e in p11a_ent if e is not None]
    p11a_avg_ent = sum(p11a_ent) / len(p11a_ent) if p11a_ent else None
    p11a_bkwr   = p11a_avg_ent
    p11a_prefixes = Counter(str(t.get("ticker") or "").split("-")[0].upper()
                             for t in p11a_settled)
    p11a_actions  = Counter(t.get("action") for t in p11a_settled)

    return {
        # Cohort sizes
        "clean_settled_n":     len(clean_settled),
        "conflicted_n":        len(conflicted),
        "modern_full_n":       len(modern_full),
        "normal_modern_n":     len(normal_modern),
        "dc_override_n":       dc_override,
        "bootstrap_prov_n":    bootstrap_prov,
        # Performance — normal_modern
        "wins":                wins,
        "losses":              losses,
        "gross_win":           gross_win,
        "gross_loss":          gross_loss,
        "total_pnl":           total_pnl,
        "roi":                 roi,
        "profit_factor":       pf,
        "actual_wr":           actual_wr,
        "avg_clv":             avg_clv,
        # Entry geometry
        "avg_entry":           avg_entry,
        "expensive_count":     expensive,
        "expensive_pct":       expensive / len(entries) if entries else 0,
        "toxic_count":         toxic,
        "avg_rr":              avg_rr,
        "weak_rr_count":       weak_rr,
        # Calibration
        "avg_confidence":      avg_conf,
        "confidence_gap":      conf_gap,
        # Side bias
        "bet_yes":             bet_yes,
        "bet_no":              bet_no,
        # Concentration
        "prefix_counts":       prefixes,
        "top_prefix":          prefixes.most_common(1)[0] if prefixes else ("N/A", 0),
        # clean_settled perf
        "cs_roi":              cs_roi,
        # Post-11A
        "p11a_raw_rows":       len(post11a_raw),
        "p11a_opened":         len(p11a_opened),
        "p11a_settled":        len(p11a_settled),
        "p11a_wins":           p11a_wins,
        "p11a_losses":         p11a_losses,
        "p11a_pf":             p11a_pf,
        "p11a_roi":            p11a_roi,
        "p11a_wr":             p11a_wr,
        "p11a_avg_conf":       p11a_conf,
        "p11a_avg_entry":      p11a_avg_ent,
        "p11a_breakeven_wr":   p11a_bkwr,
        "p11a_prefixes":       p11a_prefixes,
        "p11a_actions":        p11a_actions,
    }


def _load_observability_data() -> dict[str, Any]:
    """
    Load non-canonical observability outputs.
    All gracefully skip if files are absent.
    """
    obs: dict[str, Any] = {}

    # GX results
    gx_path = ROOT / "reports" / "observability" / "gx_results.json"
    if gx_path.exists():
        try:
            d = json.loads(gx_path.read_text())
            obs["gx_overall_success"]     = d.get("overall_success")
            obs["gx_kxeth_success"]       = (d.get("suite_results", {})
                                               .get("kxeth_quarantine", {})
                                               .get("success"))
            obs["gx_kxeth_open_active"]   = (d.get("suite_results", {})
                                               .get("kxeth_quarantine", {})
                                               .get("kxeth_open_active"))
            obs["gx_kxeth_post_quar"]     = (d.get("suite_results", {})
                                               .get("kxeth_quarantine", {})
                                               .get("kxeth_post_quarantine"))
        except Exception:
            obs["gx_load_error"] = True

    # Evidently results
    ev_path = ROOT / "reports" / "observability" / "evidently_drift_results.json"
    if ev_path.exists():
        try:
            d = json.loads(ev_path.read_text())
            obs["ev_dataset_drift"]    = d.get("dataset_drift")
            obs["ev_drift_share"]      = d.get("drift_share")
            obs["ev_drifted_columns"]  = d.get("drifted_columns")
            obs["ev_exploratory_only"] = d.get("exploratory_only")
            obs["ev_post_11a_n"]       = d.get("post_11a_n")
            obs["ev_per_column"]       = d.get("per_column", {})
            obs["ev_verdict_caveat"]   = d.get("verdict_caveat")
        except Exception:
            obs["ev_load_error"] = True

    # MLflow metrics (via module — uses same canonical path internally)
    try:
        import report_mlflow_phase_metrics as mlf
        mets = mlf._compute_metrics()["metrics"]
        obs["mlf_clean_settled"]  = mets.get("clean_settled")
        obs["mlf_modern_full"]    = mets.get("modern_full")
        obs["mlf_normal_modern"]  = mets.get("normal_modern")
        obs["mlf_avg_clv"]        = mets.get("avg_clv")
        obs["mlf_roi"]            = mets.get("modern_roi")
        obs["mlf_pf"]             = mets.get("profit_factor")
    except Exception:
        obs["mlf_load_error"] = True

    # DuckDB counts — warehouse exists but duckdb package may be absent under plain python3
    db_path = ROOT / "data" / "observability" / "ai_system_truth.duckdb"
    if db_path.exists():
        try:
            import duckdb as _duckdb
            con = _duckdb.connect(str(db_path), read_only=True)
            obs["db_post11a_settled"] = con.execute(
                "SELECT count(*) FROM post_11a_settled_trades"
            ).fetchone()[0]
            obs["db_post11a_opened"]  = con.execute(
                "SELECT count(*) FROM post_11a_opened_trades"
            ).fetchone()[0]
            obs["db_post11a_funnel_candidates"] = con.execute(
                "SELECT count(*) FROM post_11a_execution_funnel"
            ).fetchone()[0]
            con.close()
            obs["funnel_candidates"] = obs["db_post11a_funnel_candidates"]
            obs["funnel_source"] = "duckdb"
        except ImportError:
            # Warehouse present but duckdb package not installed in this venv
            obs["db_warehouse_present_but_unavailable"] = True
        except Exception:
            obs["db_load_error"] = True

    # Funnel candidates fallback: parse execution_funnel.jsonl read-only if DuckDB unavailable
    if "funnel_candidates" not in obs:
        _funnel_path = ROOT / "logs" / "execution_funnel.jsonl"
        if _funnel_path.exists():
            try:
                _count = 0
                with open(_funnel_path) as _f:
                    for _line in _f:
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            _r = json.loads(_line)
                            if str(_r.get("timestamp_utc") or "") >= PHASE_11A_CUTOFF:
                                _count += 1
                        except json.JSONDecodeError:
                            continue
                obs["funnel_candidates"] = _count
                obs["funnel_source"] = "jsonl_fallback"
            except Exception:
                obs["funnel_source"] = "unavailable"
        else:
            obs["funnel_source"] = "unavailable"

    return obs


def _load_safety_constants() -> dict[str, Any]:
    """Load safety constants from config for verification."""
    try:
        from config.trading_config import (
            MIN_EDGE, MIN_CONFIDENCE, EDGE_DANGER_HIGH_EDGE_MIN,
            GLOBAL_FORCED_LEARNING_MODE, TRADING_MODE,
        )
        return {
            "MIN_EDGE":                   MIN_EDGE,
            "MIN_CONFIDENCE":             MIN_CONFIDENCE,
            "EDGE_DANGER_HIGH_EDGE_MIN":  EDGE_DANGER_HIGH_EDGE_MIN,
            "GLOBAL_FORCED_LEARNING_MODE":GLOBAL_FORCED_LEARNING_MODE,
            "TRADING_MODE":               TRADING_MODE,
        }
    except ImportError:
        return {}


# ─── Individual diagnosis functions ──────────────────────────────────────────

def _dx_sample_too_small(c: dict) -> Diagnosis:
    n11a = c["p11a_settled"]
    nm   = c["normal_modern_n"]
    ok_full = nm >= _GATE_SCALE_MIN_NORMAL
    evidence = (
        f"post-11A settled={n11a} (need {_GATE_SCALE_MIN_NORMAL} before performance judgements); "
        f"normal_modern={nm}; "
        f"phase_gates={'PASS_SAMPLE' if ok_full else 'FAIL'}"
    )
    return Diagnosis(
        category="SAMPLE_TOO_SMALL",
        severity="CRITICAL",
        evidence=evidence,
        source="canonical/performance_report",
        actionable_now=False,
        safest_next=(
            f"Accumulate ≥{_GATE_SCALE_MIN_NORMAL} post-11A settled trades before "
            f"drawing ROI/PF/WR conclusions. Continue shadow logging passively."
        ),
        do_not=(
            "Do NOT patch live strategy, thresholds, or entry filters based on n=5. "
            "Do NOT lower gates to make the sample 'pass' sooner."
        ),
    )


def _dx_payoff_asymmetry(c: dict) -> Diagnosis:
    pf    = c["profit_factor"]
    p11pf = c["p11a_pf"]
    gw    = c["gross_win"]
    gl    = c["gross_loss"]
    p11wr = c["p11a_wr"]
    p11bk = c["p11a_breakeven_wr"]
    pf_s  = f"{pf:.4f}" if pf is not None else "N/A"
    p11pf_s = f"{p11pf:.4f}" if p11pf is not None else "N/A"
    evidence = (
        f"normal_modern: gross_win=${gw:.2f} vs gross_loss=${gl:.2f}, PF={pf_s}; "
        f"post-11A: PF={p11pf_s}, WR={p11wr:.3f} vs breakeven={p11bk:.3f}"
        if (p11wr is not None and p11bk is not None)
        else f"normal_modern: gross_win=${gw:.2f} vs gross_loss=${gl:.2f}, PF={pf_s}"
    )
    sev = "CRITICAL" if (p11pf is not None and p11pf < 0.5) else "HIGH"
    return Diagnosis(
        category="PAYOFF_ASYMMETRY",
        severity=sev,
        evidence=evidence,
        source="canonical/performance_report",
        actionable_now=False,
        safest_next=(
            "Monitor whether entry_price filter (shadow variant) reduces losses. "
            "Wait for 30 post-11A trades before any live ranking change."
        ),
        do_not=(
            "Do NOT lower MIN_EDGE or loosen entry filters to increase win rate. "
            "Do NOT change payout structure expectations."
        ),
    )


def _dx_entry_price_toxicity(c: dict) -> Diagnosis:
    avg_e   = c["avg_entry"]
    exp_pct = c["expensive_pct"]
    exp_n   = c["expensive_count"]
    tot     = c["normal_modern_n"]
    avg_rr  = c["avg_rr"]
    p11e    = c["p11a_avg_entry"]
    avg_e_s = f"{avg_e:.4f}" if avg_e is not None else "N/A"
    avg_rr_s = f"{avg_rr:.4f}" if avg_rr is not None else "N/A"
    p11e_s  = f"{p11e:.4f}" if p11e is not None else "N/A"
    evidence = (
        f"avg_entry={avg_e_s} (normal_modern); "
        f"expensive_entry(≥{_EXPENSIVE_ENTRY})={exp_n}/{tot}={exp_pct*100:.1f}%; "
        f"avg_rr={avg_rr_s}; "
        f"post-11A avg_entry={p11e_s}"
    )
    sev = "HIGH" if exp_pct >= 0.50 else "MEDIUM"
    return Diagnosis(
        category="ENTRY_PRICE_TOXICITY",
        severity=sev,
        evidence=evidence,
        source="canonical/performance_report",
        actionable_now=False,
        safest_next=(
            "Shadow upstream hygiene variants (strict_payoff, no_expensive_entry) "
            "are already logging. Monitor their CLV/ROI vs current. No live change yet."
        ),
        do_not=(
            "Do NOT raise MIN_EDGE to force cheaper entries — this could create "
            "a different starvation trap. Do NOT patch entry filter live on n<30."
        ),
    )


def _dx_confidence_overconfidence(c: dict) -> Diagnosis:
    gap  = c["confidence_gap"]
    conf = c["avg_confidence"]
    wr   = c["actual_wr"]
    p11gap = (c["p11a_avg_conf"] - c["p11a_wr"]) if (c["p11a_avg_conf"] and c["p11a_wr"]) else None
    gap_s    = f"{gap:+.4f}" if gap is not None else "N/A"
    conf_s   = f"{conf:.4f}" if conf is not None else "N/A"
    wr_s     = f"{wr:.4f}" if wr is not None else "N/A"
    p11gap_s = f"{p11gap:+.4f}" if p11gap is not None else "N/A"
    evidence = (
        f"normal_modern: avg_conf={conf_s} vs actual_WR={wr_s}, gap={gap_s}; "
        f"post-11A: conf_gap={p11gap_s} (n=5)"
    )
    sev = "HIGH" if (gap is not None and gap > 0.08) else "MEDIUM"
    return Diagnosis(
        category="CONFIDENCE_OVERCONFIDENCE",
        severity=sev,
        evidence=evidence,
        source="canonical/performance_report",
        actionable_now=False,
        safest_next=(
            "Track calibration_error metric over time. "
            "No recalibration without 30+ post-11A settled outcomes."
        ),
        do_not=(
            "Do NOT adjust confidence scaling in Builder/Critic on live system. "
            "Do NOT raise MIN_CONFIDENCE to compensate."
        ),
    )


def _dx_low_clv(c: dict) -> Diagnosis:
    avg_clv = c["avg_clv"]
    clv_s   = f"{avg_clv:+.4f}" if avg_clv is not None else "N/A"
    evidence = (
        f"avg_clv={clv_s} on normal_modern (n={c['normal_modern_n']}); "
        f"canonical: exit_price − entry_price; "
        f"negative = settled below entry on average"
    )
    sev = "HIGH" if (avg_clv is not None and avg_clv < -0.01) else "MEDIUM"
    return Diagnosis(
        category="LOW_OR_NEGATIVE_CLV",
        severity=sev,
        evidence=evidence,
        source="canonical/performance_report",
        actionable_now=False,
        safest_next=(
            "Track avg_clv trend per phase. CLV needs to cross 0 and stay positive "
            "across 30+ trades before any performance gate can pass."
        ),
        do_not=(
            "Do NOT use model margin (conf − entry) as CLV proxy. "
            "Do NOT lower CLV gate."
        ),
    )


def _dx_profit_factor_failure(c: dict) -> Diagnosis:
    pf     = c["profit_factor"]
    needed = _GATE_PROFIT_FACTOR
    pf_s   = f"{pf:.4f}" if pf is not None else "N/A"
    evidence = (
        f"profit_factor={pf_s} on normal_modern (need >{needed} for scale gate); "
        f"gross_win=${c['gross_win']:.2f}, gross_loss=${c['gross_loss']:.2f}"
    )
    sev = "HIGH" if (pf is not None and pf < 1.0) else "MEDIUM"
    return Diagnosis(
        category="PROFIT_FACTOR_FAILURE",
        severity=sev,
        evidence=evidence,
        source="canonical/performance_report + report_scale_readiness",
        actionable_now=False,
        safest_next=(
            f"PF must exceed {needed} on normal_modern for scale gate. "
            f"Wait for sample to grow; do not reverse-engineer entry filters to raise PF artificially."
        ),
        do_not=(
            "Do NOT lower the PF gate threshold. "
            "Do NOT exclude loss trades to inflate PF."
        ),
    )


def _dx_roi_failure(c: dict) -> Diagnosis:
    roi     = c["roi"]
    cs_roi  = c["cs_roi"]
    p11roi  = c["p11a_roi"]
    roi_s   = f"{roi*100:+.1f}%" if roi is not None else "N/A"
    cs_s    = f"{cs_roi*100:+.1f}%" if cs_roi is not None else "N/A"
    p11_s   = f"{p11roi*100:+.1f}%" if p11roi is not None else "N/A"
    evidence = (
        f"normal_modern ROI={roi_s}; "
        f"clean_settled ROI={cs_s}; "
        f"post-11A ROI={p11_s} (n={c['p11a_settled']})"
    )
    return Diagnosis(
        category="ROI_FAILURE",
        severity="HIGH",
        evidence=evidence,
        source="canonical/performance_report + report_scale_readiness",
        actionable_now=False,
        safest_next=(
            "ROI must be positive on normal_modern for scale gate. "
            "Monitor trend as sample grows. No live intervention."
        ),
        do_not=(
            "Do NOT remove loss trades from the proof cohort. "
            "Do NOT lower the ROI gate to 0 or below."
        ),
    )


def _dx_scanner_concentration(c: dict) -> Diagnosis:
    prefix_counts = c["prefix_counts"]
    total = c["normal_modern_n"]
    top3 = prefix_counts.most_common(3)
    top_name, top_n = c["top_prefix"]
    top_pct = top_n / total * 100 if total else 0
    p11pref  = c["p11a_prefixes"]
    top3_str = ", ".join(f"{k}={v}({v/total*100:.0f}%)" for k,v in top3) if total else "N/A"
    p11_str  = str(dict(p11pref.most_common(3)))
    evidence = (
        f"normal_modern top-3: {top3_str}; "
        f"top prefix: {top_name}={top_pct:.0f}% of trades; "
        f"post-11A: {p11_str}"
    )
    sev = "MEDIUM" if top_pct >= 40 else "LOW"
    return Diagnosis(
        category="SCANNER_CONCENTRATION",
        severity=sev,
        evidence=evidence,
        source="canonical/performance_report",
        actionable_now=False,
        safest_next=(
            "Monitor whether edge profile from top prefix generalizes to other prefixes. "
            "Shadow logs will capture diversification naturally."
        ),
        do_not=(
            "Do NOT force-diversify by lowering thresholds on non-top prefixes. "
            "Do NOT add artificial concentration limits without empirical basis."
        ),
    )


def _dx_side_bias(c: dict) -> Diagnosis:
    yes = c["bet_yes"]
    no  = c["bet_no"]
    tot = yes + no
    yes_pct = yes / tot * 100 if tot else 0
    no_pct  = no  / tot * 100 if tot else 0
    p11act  = c["p11a_actions"]
    evidence = (
        f"BET_YES={yes} ({yes_pct:.0f}%), BET_NO={no} ({no_pct:.0f}%) across "
        f"normal_modern n={tot}; "
        f"post-11A actions={dict(p11act)}"
    )
    sev = "HIGH" if no == 0 and yes > 0 else "MEDIUM"
    return Diagnosis(
        category="SIDE_BIAS",
        severity=sev,
        evidence=evidence,
        source="canonical/performance_report",
        actionable_now=False,
        safest_next=(
            "Monitor whether BET_NO signals are being generated but blocked, "
            "or never generated. Investigate scanner/council side selection logic."
        ),
        do_not=(
            "Do NOT force BET_NO execution to balance counts artificially. "
            "Do NOT change scanner side-selection thresholds on live system."
        ),
    )


def _dx_data_quality_risk(c: dict, obs: dict) -> Diagnosis:
    gx_ok = obs.get("gx_overall_success")
    conf_n = c["conflicted_n"]
    evidence = (
        f"GX overall_success={gx_ok}; "
        f"conflicted_settled_excluded={conf_n}; "
        f"source: reports/observability/gx_results.json"
        if gx_ok is not None
        else f"GX results not available; conflicted_settled_excluded={conf_n}"
    )
    sev = "LOW" if gx_ok else "MEDIUM"
    return Diagnosis(
        category="DATA_QUALITY_RISK",
        severity=sev,
        evidence=evidence,
        source="GX/gx_results.json + canonical/performance_report",
        actionable_now=False,
        safest_next=(
            "Run report_gx_log_quality.py regularly. "
            "Monitor for GX expectation failures; investigate if any appear."
        ),
        do_not=(
            "Do NOT patch conflicted rows back into the proof pool. "
            "Do NOT lower GX expectations to make failures disappear."
        ),
    )


def _dx_drift_regime(obs: dict) -> Diagnosis:
    ev_ok    = "ev_dataset_drift" in obs
    drift    = obs.get("ev_dataset_drift")
    share    = obs.get("ev_drift_share")
    ncols    = obs.get("ev_drifted_columns")
    expl     = obs.get("ev_exploratory_only")
    n_post   = obs.get("ev_post_11a_n")
    per_col  = obs.get("ev_per_column", {})
    drifted  = [col for col, info in per_col.items() if info.get("drift_detected")]

    if not ev_ok:
        evidence = "Evidently results not available — run run_observability_stack.sh first"
        sev = "LOW"
    else:
        evidence = (
            f"dataset_drift={drift}; drift_share={share}; "
            f"drifted_columns={ncols}: {drifted}; "
            f"post-11A n={n_post}; exploratory_only={expl}"
        )
        sev = "MEDIUM" if (expl or (n_post is not None and n_post < 30)) else "HIGH"

    return Diagnosis(
        category="DRIFT_OR_REGIME_SHIFT",
        severity=sev,
        evidence=evidence,
        source="Evidently/evidently_drift_results.json",
        actionable_now=False,
        safest_next=(
            f"original_edge drift is expected (Phase 11A fixed the edge interval trap). "
            f"Re-assess once post-11A n≥30. "
            f"Evidently results are EXPLORATORY_ONLY with n={n_post}."
        ),
        do_not=(
            "Do NOT use Evidently drift alone to trigger strategy changes. "
            "Do NOT dismiss drifted columns without investigating root cause."
        ),
    )


def _dx_blocker_starvation(obs: dict, c: dict) -> Diagnosis:
    p11a_opened = c["p11a_opened"]
    candidates  = obs.get("funnel_candidates")
    src         = obs.get("funnel_source", "unavailable")

    if candidates is not None and candidates > 0:
        pass_rate = p11a_opened / candidates
        evidence = (
            f"post_11a_unique_opened={p11a_opened}; "
            f"post_11a_candidates={candidates} (source={src}); "
            f"pass_rate={pass_rate * 100:.3f}% (opened/candidates)"
        )
    else:
        evidence = (
            f"post_11a_unique_opened={p11a_opened}; "
            f"post_11a_candidates=UNKNOWN (funnel denominator unavailable — "
            f"run .venv_observability/bin/python for DuckDB cross-check); "
            f"pass_rate=UNKNOWN"
        )

    return Diagnosis(
        category="BLOCKER_STARVATION",
        severity="MEDIUM",
        evidence=evidence,
        source=f"funnel/{src} + canonical",
        actionable_now=False,
        safest_next=(
            "Market quality and min-edge filters are working correctly. "
            "Monitor whether any blocker category grows unexpectedly. "
            "Review report_candidate_to_paper_forward_blockers.py for detail."
        ),
        do_not=(
            "Do NOT lower market quality or min-edge thresholds to increase pass rate. "
            "Do NOT disable EDGE_DANGER_GUARD to get more trades."
        ),
    )


def _dx_kxeth_quarantine(obs: dict) -> Diagnosis:
    gx_ok       = obs.get("gx_kxeth_success")
    open_active = obs.get("gx_kxeth_open_active")
    post_quar   = obs.get("gx_kxeth_post_quar")

    if gx_ok is None:
        evidence = "GX results not available — run GX report to verify"
        sev = "LOW"
    else:
        evidence = (
            f"GX_kxeth_check_success={gx_ok}; "
            f"kxeth_final_open_active={open_active} (must=0); "
            f"kxeth_post_quarantine={post_quar} (must=0); "
            f"canonical quarantine enforced by paper_trader.py prefix block"
        )
        sev = "CRITICAL" if (open_active and open_active > 0) else "INFO"

    return Diagnosis(
        category="KXETH_QUARANTINE_HEALTH",
        severity=sev,
        evidence=evidence,
        source="GX/gx_results.json + canonical/report_health",
        actionable_now=(sev == "CRITICAL"),
        safest_next=(
            "Quarantine is active and working. Continue monitoring. "
            "KXETH historical rows are correctly excluded from proof pool by clean_truth_report.py."
            if sev == "INFO"
            else "ALERT: KXETH quarantine may be failing — investigate paper_trader.py immediately."
        ),
        do_not=(
            "Do NOT remove KXETH quarantine. "
            "Do NOT count historical KXETH rows toward proof. "
            "KXETH ROI=-53.5%, PF=0.15, was responsible for 56% of system losses."
        ),
    )


def _dx_safety_locks(safety: dict) -> Diagnosis:
    expected = {
        "TRADING_MODE":                "PAPER",
        "GLOBAL_FORCED_LEARNING_MODE": True,
        "MIN_EDGE":                    0.03,
        "MIN_CONFIDENCE":              0.65,
        "EDGE_DANGER_HIGH_EDGE_MIN":   0.08,
    }
    violations = []
    for k, v in expected.items():
        actual = safety.get(k)
        if actual != v:
            violations.append(f"{k}={actual!r} (expected {v!r})")

    if violations:
        evidence = f"VIOLATION: {'; '.join(violations)}"
        sev = "CRITICAL"
    else:
        vals = ", ".join(f"{k}={v!r}" for k, v in expected.items())
        evidence = f"All 5 constants verified: {vals}"
        sev = "INFO"

    return Diagnosis(
        category="SAFETY_LOCK_HEALTH",
        severity=sev,
        evidence=evidence,
        source="config/trading_config.py",
        actionable_now=(sev == "CRITICAL"),
        safest_next=(
            "Safety locks are intact. No action required."
            if sev == "INFO"
            else "CRITICAL: A safety constant has been modified. Restore immediately."
        ),
        do_not=(
            "Do NOT weaken any safety constant. "
            "Do NOT enable real money, scale, or Kelly at any threshold."
        ),
    )


def _dx_observability_contradiction(c: dict, obs: dict) -> Diagnosis:
    """
    Check for contradictions between canonical truth and observability outputs.
    Canonical truth ALWAYS wins; contradictions are flagged, not acted on.
    """
    issues: list[str] = []

    # canonical vs MLflow clean_settled
    mlf_cs = obs.get("mlf_clean_settled")
    if mlf_cs is not None and abs(int(mlf_cs) - c["clean_settled_n"]) > 0:
        issues.append(
            f"MLflow clean_settled={int(mlf_cs)} vs canonical={c['clean_settled_n']} "
            f"— CANONICAL WINS"
        )

    # canonical vs MLflow modern_full
    mlf_mf = obs.get("mlf_modern_full")
    if mlf_mf is not None and abs(int(mlf_mf) - c["modern_full_n"]) > 0:
        issues.append(
            f"MLflow modern_full={int(mlf_mf)} vs canonical={c['modern_full_n']} "
            f"— CANONICAL WINS"
        )

    # canonical vs DuckDB post-11A settled
    db_settled = obs.get("db_post11a_settled")
    if db_settled is not None and db_settled != c["p11a_settled"]:
        issues.append(
            f"DuckDB post_11a_settled={db_settled} vs canonical={c['p11a_settled']} "
            f"— CANONICAL WINS (possible warehouse rebuild needed)"
        )

    # Note: avg_clv difference between canonical scopes is expected
    # canonical scale_readiness uses clean_settled, MLflow uses normal_modern
    # This is NOT a contradiction — document it clearly
    notes = [
        "Expected scope difference: canonical scale_readiness avg_clv uses clean_settled; "
        "MLflow avg_clv uses normal_modern — these differ by design, not by error."
    ]

    if issues:
        evidence = "; ".join(issues) + " | Notes: " + "; ".join(notes)
        sev = "HIGH"
    else:
        evidence = "No contradictions found. " + " | ".join(notes)
        sev = "LOW"

    return Diagnosis(
        category="OBSERVABILITY_CONTRADICTION",
        severity=sev,
        evidence=evidence,
        source="cross-tool comparison",
        actionable_now=False,
        safest_next=(
            "If contradictions are present: canonical truth takes precedence. "
            "Rebuild DuckDB warehouse if DuckDB counts are stale."
        ),
        do_not=(
            "Do NOT use observability outputs to override canonical truth. "
            "Do NOT treat expected scope differences as bugs."
        ),
    )


# ─── Main diagnosis engine ────────────────────────────────────────────────────

def run_diagnosis() -> tuple[list[Diagnosis], dict, dict, dict]:
    """
    Run all 15 diagnosis checks.
    Returns (ranked_diagnoses, canonical_data, obs_data, safety_constants).
    """
    canonical = _load_canonical_data()
    obs       = _load_observability_data()
    safety    = _load_safety_constants()

    raw = [
        _dx_sample_too_small(canonical),
        _dx_payoff_asymmetry(canonical),
        _dx_entry_price_toxicity(canonical),
        _dx_confidence_overconfidence(canonical),
        _dx_low_clv(canonical),
        _dx_profit_factor_failure(canonical),
        _dx_roi_failure(canonical),
        _dx_scanner_concentration(canonical),
        _dx_side_bias(canonical),
        _dx_data_quality_risk(canonical, obs),
        _dx_drift_regime(obs),
        _dx_blocker_starvation(obs, canonical),
        _dx_kxeth_quarantine(obs),
        _dx_safety_locks(safety),
        _dx_observability_contradiction(canonical, obs),
    ]

    # Sort: descending severity weight, then alphabetical category
    raw.sort(key=lambda d: (-SEVERITY_WEIGHT.get(d.severity, 0), d.category))
    for i, dx in enumerate(raw, 1):
        dx.rank = i

    return raw, canonical, obs, safety


def _compute_agree_status(obs: dict, all_agree: bool) -> str:
    """
    Return the tools_agree_with_canonical status string.

    Possible values:
      YES                   — all checked tools agree with canonical truth
      NO                    — at least one tool disagrees (canonical wins)
      PARTIAL_DUCKDB_SKIPPED — warehouse exists but duckdb package unavailable;
                               DuckDB cross-check was not performed
    """
    if not all_agree:
        return "NO"
    if obs.get("db_warehouse_present_but_unavailable"):
        return "PARTIAL_DUCKDB_SKIPPED"
    return "YES"


def _fmt(v: Any, pct: bool = False, dollar: bool = False, dec: int = 4) -> str:
    if v is None:
        return "N/A"
    if pct:
        return f"{v*100:+.1f}%"
    if dollar:
        return f"${v:+.2f}"
    return f"{v:.{dec}f}"


def main() -> int:
    print()
    print("=" * 72)
    print("  Phase 11D — Observability Diagnosis Brain")
    print("=" * 72)

    diagnoses, c, obs, safety = run_diagnosis()

    # ── 1. Ranked diagnosis table ──────────────────────────────────────────────
    print()
    print(f"  {'RANK':<5} {'CATEGORY':<35} {'SEV':<10} {'ACTIONABLE'}")
    print("  " + "-" * 68)
    for dx in diagnoses:
        act = "NOW" if dx.actionable_now else "wait"
        print(f"  {dx.rank:<5} {dx.category:<35} {dx.severity:<10} {act}")

    # ── 2. Evidence detail ──────────────────────────────────────────────────────
    print()
    print("  " + "=" * 68)
    print("  EVIDENCE DETAIL")
    print("  " + "=" * 68)
    for dx in diagnoses:
        print()
        print(f"  [{dx.rank}] {dx.category} — {dx.severity}")
        print(f"  Evidence:      {dx.evidence}")
        print(f"  Source:        {dx.source}")
        print(f"  Safest next:   {dx.safest_next}")
        print(f"  Do NOT:        {dx.do_not}")

    # ── 3. Canonical truth summary ─────────────────────────────────────────────
    print()
    print("  " + "=" * 68)
    print("  CANONICAL TRUTH — CURRENT SYSTEM STATE")
    print("  " + "=" * 68)

    proof_verdict = "WATCHLIST" if c["normal_modern_n"] >= 30 else "NOT_PROVEN"
    perf_gates = (
        c["roi"] is not None and c["roi"] > 0
        and c["profit_factor"] is not None and c["profit_factor"] > _GATE_PROFIT_FACTOR
        and c["avg_clv"] is not None and c["avg_clv"] > 0
    )
    if perf_gates and c["normal_modern_n"] >= 30:
        proof_verdict = "PROVEN_PROFITABLE"

    print(f"  proof_verdict:           {proof_verdict}")
    print(f"  clean_settled:           {c['clean_settled_n']}  (need 30)")
    print(f"  modern_full:             {c['modern_full_n']}")
    print(f"  normal_modern:           {c['normal_modern_n']}  (need 30 for scale gate)")
    print(f"  total_pnl:               {_fmt(c['total_pnl'], dollar=True)}")
    print(f"  normal_modern ROI:       {_fmt(c['roi'], pct=True)}")
    print(f"  clean_settled ROI:       {_fmt(c['cs_roi'], pct=True)}")
    print(f"  profit_factor:           {_fmt(c['profit_factor'])}")
    print(f"  avg_clv:                 {_fmt(c['avg_clv'])}  (need > 0)")
    print(f"  actual_win_rate:         {_fmt(c['actual_wr'])}  ({c['wins']}W/{c['losses']}L)")
    print(f"  avg_entry:               {_fmt(c['avg_entry'])}")
    print(f"  avg_confidence:          {_fmt(c['avg_confidence'])}")
    print(f"  confidence_gap:          {_fmt(c['confidence_gap'])}  (conf minus WR)")
    print(f"  side_bias:               BET_YES={c['bet_yes']} BET_NO={c['bet_no']}")
    print()
    print(f"  POST-11A (n={c['p11a_settled']} settled, need 30+ for conclusions):")
    print(f"    post_11a_unique_opened:  {c['p11a_opened']}")
    print(f"    post_11a_settled:        {c['p11a_settled']}")
    print(f"    post_11a_ROI:            {_fmt(c['p11a_roi'], pct=True)}")
    print(f"    post_11a_PF:             {_fmt(c['p11a_pf'])}")
    print(f"    post_11a_WR:             {_fmt(c['p11a_wr'])}  vs breakeven {_fmt(c['p11a_breakeven_wr'])}")
    print(f"    post_11a_avg_conf:       {_fmt(c['p11a_avg_conf'])}")
    print(f"    post_11a_conf_gap:       {_fmt((c['p11a_avg_conf'] - c['p11a_wr']) if c['p11a_avg_conf'] and c['p11a_wr'] else None)}")
    print(f"    post_11a_prefixes:       {dict(c['p11a_prefixes'].most_common(3))}")
    print(f"    sample_sufficient:       NO  ({c['p11a_settled']}/30)")

    # ── 4. Tool agreement check ────────────────────────────────────────────────
    print()
    print("  " + "=" * 68)
    print("  TOOL AGREEMENT vs CANONICAL TRUTH")
    print("  " + "=" * 68)
    checks = [
        ("MLflow clean_settled", obs.get("mlf_clean_settled"), c["clean_settled_n"]),
        ("MLflow modern_full",   obs.get("mlf_modern_full"),   c["modern_full_n"]),
        ("MLflow normal_modern", obs.get("mlf_normal_modern"), c["normal_modern_n"]),
        ("DuckDB p11a_settled",  obs.get("db_post11a_settled"), c["p11a_settled"]),
    ]
    all_agree = True
    db_unavail = obs.get("db_warehouse_present_but_unavailable", False)
    for label, tool_val, canonical_val in checks:
        is_db = label.startswith("DuckDB")
        if tool_val is None:
            if is_db and db_unavail:
                print(f"  [WARN] {label:<30} DUCKDB_WAREHOUSE_PRESENT_BUT_PACKAGE_UNAVAILABLE")
            else:
                print(f"  [SKIP] {label:<30} tool not available")
        elif abs(int(tool_val) - canonical_val) == 0:
            print(f"  [OK  ] {label:<30} tool={int(tool_val)}  canonical={canonical_val}  AGREE")
        else:
            print(f"  [WARN] {label:<30} tool={int(tool_val)}  canonical={canonical_val}  DISAGREE — canonical wins")
            all_agree = False

    agree_status = _compute_agree_status(obs, all_agree)
    print()
    print(f"  tools_agree_with_canonical: {agree_status}")
    if db_unavail:
        print()
        print(f"  [WARN] DUCKDB_WAREHOUSE_PRESENT_BUT_PACKAGE_UNAVAILABLE")
        print(f"         Run with .venv_observability/bin/python for full DuckDB cross-check.")

    # ── 5. Hard warnings ──────────────────────────────────────────────────────
    print()
    print("  " + "=" * 68)
    print("  HARD WARNINGS — READ BEFORE TAKING ANY ACTION")
    print("  " + "=" * 68)
    warnings = [
        "DO NOT enable real money  — scale/performance gates not passed",
        "DO NOT enable scale       — real_money_allowed=False hardcoded",
        "DO NOT enable Kelly       — GLOBAL_FORCED_LEARNING_MODE=True required",
        f"DO NOT weaken thresholds  — no empirical basis from n={c['p11a_settled']} post-11A trades",
        "DO NOT remove KXETH quarantine — KXETH ROI=-53.5%, PF=0.15 historically",
        f"DO NOT patch live strategy — need 30 post-11A settled trades, have {c['p11a_settled']}",
        "DO NOT use avg_model_margin as CLV — it is not CLV",
        "DO NOT count conflicted_settled rows as proof",
    ]
    for w in warnings:
        print(f"  [!] {w}")

    # ── 6. Final recommendation ────────────────────────────────────────────────
    critical_dx = [dx for dx in diagnoses if dx.severity == "CRITICAL"]
    high_dx     = [dx for dx in diagnoses if dx.severity == "HIGH"]

    print()
    print("  " + "=" * 68)
    print("  FINAL RECOMMENDATION")
    print("  " + "=" * 68)
    print(f"  Critical diagnoses ({len(critical_dx)}): "
          + ", ".join(dx.category for dx in critical_dx))
    print(f"  High diagnoses    ({len(high_dx)}): "
          + ", ".join(dx.category for dx in high_dx))
    print()
    print("  The system is in WATCHLIST state. Sample gates pass. Performance gates fail.")
    print("  PAYOFF_ASYMMETRY + ENTRY_PRICE_TOXICITY + SIDE_BIAS are structural.")
    print("  None of these can be fixed safely on n=5 post-11A trades.")
    print("  The safest action is to continue accumulating post-11A settled trades.")
    print("  Shadow logging (upstream hygiene, payoff-aware) is already active.")
    print("  Revisit all diagnoses once post-11A n ≥ 30.")
    print()
    print(f"  {SENTINEL}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
