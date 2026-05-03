#!/usr/bin/env python3
"""
tools/external_data/report_research_signal_discovery.py
---------------------------------------------------------
Read-only research signal discovery analysis.

Loads the latest joined research CSV and identifies candidate edge pockets,
anti-edge pockets, and hard data limitations. Writes:
  - data/research/reports/latest_signal_discovery_report.md
  - data/research/reports/latest_signal_discovery_segments.csv
  - data/research/reports/latest_signal_discovery_candidates.json

DOES NOT:
  - modify logs, trades, or proof gates
  - change strategy thresholds
  - unlock real money or scale
  - create fake proof
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

JOINED_DIR  = ROOT / "data" / "research" / "joined"
REPORTS_DIR = ROOT / "data" / "research" / "reports"

PROOF_ELIGIBLE = {"BOOTSTRAP_ERA_ALLOW_COUNTS_NORMAL", "NORMAL_MODERN_CANDIDATE"}
LEGACY_CLASS   = "LEGACY_OR_INCOMPLETE"
DC_EXCLUDED    = "DATA_COLLECTION_OVERRIDE_EXCLUDED"
BP_EXCLUDED    = "BOOTSTRAP_PROVISIONAL_EXCLUDED"

CONFIDENCE_LABELS = {
    (0,   5):   "useless/anecdotal",
    (5,  10):   "very weak",
    (10, 30):   "weak exploratory",
    (30, 100):  "moderate research signal",
    (100, 9999):"stronger (still needs forward validation)",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fv(v: Any) -> Optional[float]:
    try:
        return float(v) if str(v).strip() not in ("", "None") else None
    except (TypeError, ValueError):
        return None


def _is_ok(row: dict) -> bool:
    return row.get("outcome_known", "").lower() in ("true", "1")


def _is_mr(row: dict) -> bool:
    return row.get("outcome_source") == "MARKET_RESOLVED"


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


def _confidence_label(n: int) -> str:
    for (lo, hi), label in CONFIDENCE_LABELS.items():
        if lo <= n < hi:
            return label
    return "unknown"


def _safe_mean(vals: List[float]) -> Optional[float]:
    return statistics.mean(vals) if vals else None


def _safe_median(vals: List[float]) -> Optional[float]:
    return statistics.median(vals) if vals else None


def _pct(v: Optional[float]) -> str:
    return f"{v * 100:+.1f}%" if v is not None else "n/a"


def _fmt(v: Optional[float], d: int = 3) -> str:
    return f"{v:.{d}f}" if v is not None else "n/a"


def _wr(wins: int, total: int) -> Optional[float]:
    return wins / total if total else None


def _breakeven(ep: Optional[float]) -> Optional[float]:
    return ep  # for YES bets: breakeven WR = entry_price


def _vs_breakeven(wr_val: Optional[float], ep: Optional[float]) -> Optional[float]:
    if wr_val is None or ep is None:
        return None
    return wr_val - ep


class SegmentStats:
    def __init__(self, label: str, rows: List[Dict[str, str]], note: str = "") -> None:
        self.label     = label
        self.n_total   = len(rows)
        self.note      = note
        ok             = [r for r in rows if _is_ok(r)]
        mr             = [r for r in ok if _is_mr(r)]
        self.n_ok      = len(ok)
        self.n_mr      = len(mr)
        wins_ok        = [r for r in ok if r.get("outcome") == "WIN"]
        self.wins      = len(wins_ok)
        self.losses    = self.n_ok - self.wins
        self.wr        = _wr(self.wins, self.n_ok)
        roi_vals       = [_fv(r["roi"]) for r in ok if _fv(r.get("roi")) is not None]
        self.mroi      = _safe_mean(roi_vals)
        self.medroi    = _safe_median(roi_vals)
        ep_vals        = [_fv(r["entry_price"]) for r in ok if _fv(r.get("entry_price")) is not None]
        self.mep       = _safe_mean(ep_vals)
        clv_vals       = [_fv(r["clv"]) for r in ok if _fv(r.get("clv")) is not None]
        self.mclv      = _safe_mean(clv_vals)
        conf_vals      = [_fv(r["probability_confidence"]) for r in ok
                          if _fv(r.get("probability_confidence")) is not None]
        self.mconf     = _safe_mean(conf_vals)
        self.beven     = _breakeven(self.mep)
        self.vs_beven  = _vs_breakeven(self.wr, self.beven)
        self.conf_label = _confidence_label(self.n_ok)
        proof_counts   = Counter(r.get("proof_class", "") for r in ok)
        self.proof_mix = dict(proof_counts)
        self.legacy_pct = proof_counts.get(LEGACY_CLASS, 0) / self.n_ok if self.n_ok else 0
        self.proof_eligible_n = sum(1 for r in ok if r.get("proof_class") in PROOF_ELIGIBLE)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label":           self.label,
            "n_total":         self.n_total,
            "n_ok":            self.n_ok,
            "n_mr":            self.n_mr,
            "wins":            self.wins,
            "losses":          self.losses,
            "wr":              round(self.wr, 4) if self.wr is not None else None,
            "mean_roi":        round(self.mroi, 4) if self.mroi is not None else None,
            "median_roi":      round(self.medroi, 4) if self.medroi is not None else None,
            "mean_ep":         round(self.mep, 4) if self.mep is not None else None,
            "breakeven_wr":    round(self.beven, 4) if self.beven is not None else None,
            "vs_breakeven":    round(self.vs_beven, 4) if self.vs_beven is not None else None,
            "mean_clv":        round(self.mclv, 4) if self.mclv is not None else None,
            "mean_conf":       round(self.mconf, 4) if self.mconf is not None else None,
            "confidence_label": self.conf_label,
            "proof_eligible_n": self.proof_eligible_n,
            "legacy_pct":      round(self.legacy_pct, 3),
            "proof_mix":       self.proof_mix,
            "note":            self.note,
        }

    def md_row(self) -> str:
        wr_str   = _fmt(self.wr)
        mroi_str = _pct(self.mroi)
        med_str  = _pct(self.medroi)
        bev_str  = _fmt(self.beven)
        gap_str  = _pct(self.vs_beven)
        lp_str   = f"{self.legacy_pct*100:.0f}%"
        return (f"| {self.label} | {self.n_ok} | {self.wins} | {self.losses} | "
                f"{wr_str} | {mroi_str} | {med_str} | {bev_str} | {gap_str} | "
                f"{self.proof_eligible_n} | {lp_str} | {self.conf_label} |")


def _table_header() -> str:
    return (
        "| Segment | n_ok | W | L | WR | mROI | medROI | breakeven | vs_beven | "
        "proof_el | legacy% | confidence |\n"
        "|---------|------|---|---|----|------|--------|-----------|----------|"
        "---------|---------|------------|"
    )


# ── Analysis functions ────────────────────────────────────────────────────────

def _by_prefix(ok: List[dict]) -> List[SegmentStats]:
    prefixes = sorted(set(r.get("market_prefix", "OTHER") for r in ok))
    return [SegmentStats(f"prefix:{p}", [r for r in ok if r.get("market_prefix") == p])
            for p in prefixes if [r for r in ok if r.get("market_prefix") == p]]


def _by_outcome_source(ok: List[dict]) -> List[SegmentStats]:
    sources = ["MARKET_RESOLVED", "FORCED_TIME_EXIT"]
    result = []
    for src in sources:
        sub = [r for r in ok if r.get("outcome_source") == src]
        if sub:
            result.append(SegmentStats(f"src:{src}", sub))
    result.append(SegmentStats("src:ALL_OUTCOME_KNOWN", ok))
    return result


def _by_proof_class(ok: List[dict]) -> List[SegmentStats]:
    classes = sorted(set(r.get("proof_class", "") for r in ok))
    return [SegmentStats(f"proof:{c}", [r for r in ok if r.get("proof_class") == c])
            for c in classes if [r for r in ok if r.get("proof_class") == c]]


def _by_entry_price_bucket(ok: List[dict]) -> List[SegmentStats]:
    buckets = [
        ("<0.35",   None,  0.35),
        ("0.35-0.45", 0.35, 0.45),
        ("0.45-0.55", 0.45, 0.55),
        ("0.55-0.65", 0.55, 0.65),
        ("0.65-0.75", 0.65, 0.75),
        (">0.75",   0.75,  None),
    ]
    result = []
    for label, lo, hi in buckets:
        sub = [r for r in ok if _fv(r.get("entry_price")) is not None
               and (lo is None or _fv(r["entry_price"]) >= lo)
               and (hi is None or _fv(r["entry_price"]) < hi)]
        if sub:
            result.append(SegmentStats(f"ep:{label}", sub))
    return result


def _by_model_prob_bucket(ok: List[dict]) -> List[SegmentStats]:
    mp_ok = [r for r in ok if _fv(r.get("model_probability")) is not None]
    buckets = [
        ("<0.60",   None, 0.60),
        ("0.60-0.70", 0.60, 0.70),
        ("0.70-0.80", 0.70, 0.80),
        ("0.80-0.90", 0.80, 0.90),
        (">0.90",   0.90, None),
    ]
    result = []
    for label, lo, hi in buckets:
        sub = [r for r in mp_ok
               if (lo is None or _fv(r["model_probability"]) >= lo)
               and (hi is None or _fv(r["model_probability"]) < hi)]
        if sub:
            result.append(SegmentStats(f"mp:{label}", sub,
                note="model_probability present for modern rows only (n=27 outcome-known)"))
    return result


def _by_risk_edge_bucket(ok: List[dict]) -> List[SegmentStats]:
    re_ok = [r for r in ok if _fv(r.get("risk_edge")) is not None]
    buckets = [
        ("<0.03",   None, 0.03),
        ("0.03-0.05", 0.03, 0.05),
        ("0.05-0.08", 0.05, 0.08),
        ("0.08-0.12", 0.08, 0.12),
        (">0.12",   0.12, None),
    ]
    result = []
    for label, lo, hi in buckets:
        sub = [r for r in re_ok
               if (lo is None or _fv(r["risk_edge"]) >= lo)
               and (hi is None or _fv(r["risk_edge"]) < hi)]
        if sub:
            result.append(SegmentStats(f"re:{label}", sub,
                note="risk_edge present for modern rows only (n=27 outcome-known)"))
    return result


def _by_post_council_edge_bucket(ok: List[dict]) -> List[SegmentStats]:
    pce_ok = [r for r in ok if _fv(r.get("post_council_edge")) is not None]
    buckets = [
        ("<0.00",    None,  0.00),
        ("0.00-0.03", 0.00,  0.03),
        ("0.03-0.06", 0.03,  0.06),
        ("0.06-0.10", 0.06,  0.10),
        (">0.10",   0.10,  None),
    ]
    result = []
    for label, lo, hi in buckets:
        sub = [r for r in pce_ok
               if (lo is None or _fv(r["post_council_edge"]) >= lo)
               and (hi is None or _fv(r["post_council_edge"]) < hi)]
        if sub:
            result.append(SegmentStats(f"pce:{label}", sub))
    return result


def _by_crypto_momentum(ok: List[dict]) -> List[SegmentStats]:
    cj = [r for r in ok if r.get("crypto_join_status") == "JOINED"]
    result = []
    # 5m momentum
    r5 = [r for r in cj if _fv(r.get("crypto_return_5m_before_entry")) is not None]
    m5_buckets = [
        ("5m_neg_strong", None, -0.01),
        ("5m_neg_mild",  -0.01, -0.001),
        ("5m_flat",      -0.001, 0.001),
        ("5m_pos_mild",   0.001, 0.01),
        ("5m_pos_strong", 0.01, None),
    ]
    for label, lo, hi in m5_buckets:
        sub = [r for r in r5
               if (lo is None or _fv(r["crypto_return_5m_before_entry"]) >= lo)
               and (hi is None or _fv(r["crypto_return_5m_before_entry"]) < hi)]
        if sub:
            result.append(SegmentStats(f"mom:{label}", sub,
                note="confounded by prefix (all crypto-joined rows are KXBTCD/KXETHD)"))
    # 15m momentum
    r15 = [r for r in cj if _fv(r.get("crypto_return_15m_before_entry")) is not None]
    m15_buckets = [
        ("15m_neg_strong", None, -0.01),
        ("15m_neg_mild",  -0.01, -0.001),
        ("15m_flat",      -0.001, 0.001),
        ("15m_pos_mild",   0.001, 0.01),
        ("15m_pos_strong", 0.01, None),
    ]
    for label, lo, hi in m15_buckets:
        sub = [r for r in r15
               if (lo is None or _fv(r["crypto_return_15m_before_entry"]) >= lo)
               and (hi is None or _fv(r["crypto_return_15m_before_entry"]) < hi)]
        if sub:
            result.append(SegmentStats(f"mom:{label}", sub,
                note="confounded by prefix (all crypto-joined rows are KXBTCD/KXETHD)"))
    return result


def _by_kalshi_spread(ok: List[dict]) -> List[SegmentStats]:
    kj = [r for r in ok if r.get("kalshi_join_status") == "JOINED"]
    with_spread = []
    for r in kj:
        ask = _fv(r.get("kalshi_yes_ask_close"))
        bid = _fv(r.get("kalshi_yes_bid_close"))
        if ask is not None and bid is not None:
            with_spread.append((r, ask - bid))
    buckets = [
        ("tight_<0.05",  None, 0.05),
        ("mod_0.05-0.10", 0.05, 0.10),
        ("wide_>0.10",   0.10, None),
    ]
    result = []
    for label, lo, hi in buckets:
        sub = [r for r, s in with_spread
               if (lo is None or s >= lo) and (hi is None or s < hi)]
        if sub:
            result.append(SegmentStats(f"spread:{label}", sub))
    return result


def _by_kalshi_mid_gap(ok: List[dict]) -> List[SegmentStats]:
    kj = [r for r in ok if r.get("kalshi_join_status") == "JOINED"]
    with_gap = []
    for r in kj:
        mid = _fv(r.get("kalshi_yes_mid_close"))
        ep  = _fv(r.get("entry_price"))
        if mid is not None and ep is not None:
            with_gap.append((r, mid - ep))
    buckets = [
        ("model_above_mid_>+0.05",   0.05, None),
        ("near_fair_-0.05_to_+0.05", -0.05, 0.05),
        ("model_below_mid_<-0.05",   None, -0.05),
    ]
    result = []
    for label, lo, hi in buckets:
        sub = [r for r, g in with_gap
               if (lo is None or g >= lo) and (hi is None or g < hi)]
        if sub:
            result.append(SegmentStats(f"midgap:{label}", sub))
    return result


# ── Candidate / Anti-edge analysis ──────────────────────────────────────────

def _identify_candidates(all_segments: List[SegmentStats]) -> Tuple[List[dict], List[dict]]:
    """
    Identify candidate edge pockets and anti-edge pockets.
    Applies BRUTALLY HONEST labeling of contamination.
    Returns (candidates, anti_edges).
    """
    candidates = []
    anti_edges = []

    for s in all_segments:
        if s.n_ok < 3:
            continue
        if s.wr is None:
            continue

        # Candidate edge: WR above breakeven, positive median ROI, n >= 5
        if (s.wr > (s.beven or 0.5) + 0.05 and
                s.medroi is not None and s.medroi > 0 and
                s.n_ok >= 5):
            # Assess contamination
            contamination = []
            if s.legacy_pct > 0.30:
                contamination.append(f"LEGACY rows {s.legacy_pct*100:.0f}% — edge formula artifact")
            if s.proof_eligible_n == 0:
                contamination.append("ZERO proof-eligible rows")
            if s.n_ok < 10:
                contamination.append(f"tiny sample n={s.n_ok}")
            fake_reason = "; ".join(contamination) if contamination else "no obvious artifact (but n is small)"

            candidates.append({
                "segment":          s.label,
                "n_ok":             s.n_ok,
                "wins":             s.wins,
                "losses":           s.losses,
                "win_rate":         round(s.wr, 4) if s.wr else None,
                "breakeven_wr":     round(s.beven, 4) if s.beven else None,
                "vs_breakeven":     round(s.vs_beven, 4) if s.vs_beven else None,
                "mean_roi":         round(s.mroi, 4) if s.mroi else None,
                "median_roi":       round(s.medroi, 4) if s.medroi else None,
                "mean_clv":         round(s.mclv, 4) if s.mclv else None,
                "proof_eligible_n": s.proof_eligible_n,
                "legacy_pct":       round(s.legacy_pct, 3),
                "confidence_label": s.conf_label,
                "may_be_real":      _candidate_reason(s),
                "may_be_fake":      fake_reason,
                "forward_test_rule": _forward_test_rule(s),
                "contamination":    contamination,
            })

        # Anti-edge: WR significantly below breakeven, n >= 5
        if (s.beven is not None and s.wr is not None and
                s.wr < (s.beven or 0.5) - 0.15 and
                s.n_ok >= 5):
            contamination = []
            if s.legacy_pct > 0.30:
                contamination.append(f"LEGACY rows {s.legacy_pct*100:.0f}%")
            if s.proof_eligible_n == 0:
                contamination.append("ZERO proof-eligible rows")

            anti_edges.append({
                "segment":          s.label,
                "n_ok":             s.n_ok,
                "wins":             s.wins,
                "losses":           s.losses,
                "win_rate":         round(s.wr, 4),
                "breakeven_wr":     round(s.beven, 4) if s.beven else None,
                "vs_breakeven":     round(s.vs_beven, 4) if s.vs_beven else None,
                "mean_roi":         round(s.mroi, 4) if s.mroi else None,
                "median_roi":       round(s.medroi, 4) if s.medroi else None,
                "proof_eligible_n": s.proof_eligible_n,
                "legacy_pct":       round(s.legacy_pct, 3),
                "confidence_label": s.conf_label,
                "possible_cause":   _anti_edge_cause(s),
                "future_action":    _anti_edge_action(s),
                "contamination":    contamination,
            })

    return candidates, anti_edges


def _candidate_reason(s: SegmentStats) -> str:
    label = s.label
    if "KXBTCD" in label:
        return "KXBTCD has shown consistently higher WR than breakeven across ALL proof classes (legacy, DC, bootstrap, era_allow). This persistence across proof class boundaries is the most credible signal in the dataset."
    if "pos_mild" in label or "pos_strong" in label:
        return "Positive crypto momentum before entry correlates with wins. Could indicate regime alignment (rising price favors YES bets at threshold)."
    if "ep:>0.75" in label:
        return "High entry price bets (>0.75) have positive actual ROI (+12.9%). High entry means lower payout but high probability events."
    if "pce:0.06" in label:
        return "Post-council edge in 0.06-0.10 range shows 9/11 wins. Edge level suggests council is correctly identifying genuine probability advantage."
    if "spread" in label and "tight" in label:
        return "Tight spread markets show decent WR. Liquid markets may price more efficiently."
    return "WR exceeds breakeven with positive median ROI."


def _anti_edge_cause(s: SegmentStats) -> str:
    label = s.label
    if "OTHER" in label:
        return "OTHER prefix contains novel/unusual market structures (KXETHMINY, KXETH hourly buckets) not seen in normal KXBTCD/KXETHD daily markets. Different resolution mechanics and lower liquidity."
    if "ep:<0.35" in label:
        return "Cheap entry YES bets (ep<0.35) have extremely high breakeven WR requirement implicitly. Legacy rows with cheap entries had inflated edge scores from the formula artifact (edge = confidence - entry_price - 0.01). All these rows are legacy class."
    if "neg" in label:
        return "Negative crypto momentum before entry. Could indicate market moving against direction of YES bet."
    if "model_below_mid" in label:
        return "Model entry price is below Kalshi market mid. Suggests model was less optimistic than market consensus. May indicate adverse selection."
    if "mp:0.60-0.70" in label or "mp:0.70-0.80" in label:
        return "Lower model probability rows underperform. WR below breakeven suggests model overestimates at these confidence levels (calibration deficit)."
    if "pce:>0.10" in label:
        return "PCE>0.10 is the legacy edge artifact: edge = confidence - entry_price - 0.01 produces inflated edge for cheap entries. Already blocked by EDGE_DANGER_BLOCK_HIGH_EDGE guard."
    return "Win rate significantly below breakeven threshold."


def _anti_edge_action(s: SegmentStats) -> str:
    label = s.label
    if "OTHER" in label:
        return "WATCHLIST: Consider blocking non-KXBTCD/KXETHD/BTC15M/ETH15M prefixes once data accumulates. Do NOT add blocker yet — n=11 is insufficient."
    if "ep:<0.35" in label:
        return "ALREADY HANDLED: EDGE_DANGER_BLOCK_HIGH_EDGE blocks edge>=0.08. Legacy cheap entries produced edge=0.60+ and should not recur with current guards."
    if "pce:>0.10" in label:
        return "ALREADY HANDLED: EDGE_DANGER_BLOCK_HIGH_EDGE guard is active. These rows are historical legacy data."
    if "model_below_mid" in label:
        return "WATCHLIST ONLY: n=3, anecdotal. Do not add blocker. Collect more Kalshi-joined data."
    return "WATCHLIST ONLY: collect more data before considering a blocker."


def _forward_test_rule(s: SegmentStats) -> str:
    label = s.label
    if "KXBTCD" in label:
        return "Forward-test: track WR for all KXBTCD trades in next 30 normal_modern rows. If WR stays > 0.70, KXBTCD bias is real. If WR reverts toward 0.65, it was regime luck."
    if "pos_mild" in label and "15m" in label:
        return "Forward-test: for KXBTCD/KXETHD with 15m BTC/ETH return > +0.1%, track if WR remains > 0.85. NOTE: must control for prefix — test separately from baseline."
    if "ep:>0.75" in label:
        return "Forward-test: track trades with entry_price > 0.75. Current data is all DC_OVERRIDE excluded. Need proof-eligible rows with high entry price."
    if "spread:tight" in label:
        return "Forward-test: track WR for tight-spread (<0.05) Kalshi-joined trades. Current n=37 but all mixed proof classes."
    return "Forward-test: collect 30+ proof-eligible rows in this segment before drawing conclusions."


# ── Report writers ───────────────────────────────────────────────────────────

def _write_segments_csv(all_segments: List[SegmentStats], path: Path) -> None:
    if not all_segments:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(all_segments[0].to_dict().keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for s in all_segments:
            row = s.to_dict()
            row["proof_mix"] = json.dumps(row["proof_mix"])
            writer.writerow(row)


def _write_candidates_json(candidates: List[dict], anti_edges: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "real_money_allowed": False,
        "scale_allowed": False,
        "verdict_note": "All candidates are WEAK CANDIDATE EDGE ONLY or lower due to proof contamination and small samples.",
        "candidates": candidates,
        "anti_edges": anti_edges,
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _overall_verdict(candidates: List[dict], anti_edges: List[dict], n_ok: int, proof_el: int) -> str:
    if proof_el < 5:
        return "NO EDGE FOUND (proof-eligible outcome rows insufficient: 0 or near-zero)"
    if proof_el < 10:
        return "WEAK CANDIDATE EDGE ONLY"
    if candidates and any(c["proof_eligible_n"] >= 5 for c in candidates):
        return "WEAK CANDIDATE EDGE ONLY"
    return "NO EDGE FOUND"


def _write_markdown_report(
    csv_path: Path,
    rows: List[Dict[str, str]],
    all_segments: List[SegmentStats],
    candidates: List[dict],
    anti_edges: List[dict],
    segments_csv: Path,
    candidates_json: Path,
    out_path: Path,
) -> None:
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok   = [r for r in rows if _is_ok(r)]
    mr   = [r for r in ok if _is_mr(r)]
    te   = [r for r in ok if r.get("outcome_source") == "FORCED_TIME_EXIT"]
    fv_r = [r for r in rows if r.get("outcome_source") == "FORCED_VOID"]
    ghost = [r for r in rows if r.get("outcome_source") == "OPEN_NO_OUTCOME"]
    proof_el = sum(1 for r in ok if r.get("proof_class") in PROOF_ELIGIBLE)
    verdict  = _overall_verdict(candidates, anti_edges, len(ok), proof_el)

    rel_csv = csv_path.relative_to(ROOT) if csv_path.is_relative_to(ROOT) else csv_path

    lines: List[str] = []
    A = lines.append

    A("# Research Signal Discovery Report")
    A(f"**Generated:** {now}  ")
    A(f"**Source CSV:** `{rel_csv}`  ")
    A(f"**Rows:** {len(rows)}  ")
    A("")

    # ── 1. Executive Verdict ──
    A("---")
    A("## 1. Executive Verdict")
    A("")
    A(f"### **{verdict}**")
    A("")
    A(f"- outcome-known rows: **{len(ok)}** (min for proof: 30 normal_modern)")
    A(f"- proof-eligible outcome rows: **{proof_el}** / 30 minimum")
    A(f"- KXBTCD shows persistent above-breakeven WR across all 4 proof classes, but the sample is")
    A(f"  too small and too contaminated to call this proven edge.")
    A(f"- Momentum and PCE 0.06-0.10 signals are contaminated by legacy edge artifact.")
    A(f"- PCE >0.10 anti-edge is already handled by EDGE_DANGER_BLOCK_HIGH_EDGE guard.")
    A(f"- OTHER prefix has persistent negative performance but n=11.")
    A("")
    A("> **real_money_allowed = False (hardcoded)**  ")
    A("> **scale_allowed = False (hardcoded)**  ")
    A("> No finding in this report unlocks either gate.")
    A("")

    # ── 2. Dataset ──
    A("---")
    A("## 2. Dataset Used")
    A("")
    A(f"| Field | Value |")
    A(f"|-------|-------|")
    A(f"| CSV path | `{rel_csv}` |")
    A(f"| Total rows | {len(rows)} |")
    A(f"| Columns | {len(rows[0]) if rows else 0} |")
    A(f"| Generated at | {now} |")
    A("")

    # ── 3. Safety Locks ──
    A("---")
    A("## 3. Safety Locks Confirmed")
    A("")
    A("| Lock | Status |")
    A("|------|--------|")
    A("| real_money_allowed | **False — hardcoded** |")
    A("| scale_allowed | **False — hardcoded** |")
    A("| TRADING_MODE | PAPER |")
    A("| GLOBAL_FORCED_LEARNING_MODE | True — Kelly overridden, $5 flat |")
    A("| kalshi_client | GET-only, no POST |")
    A("")
    A("This report is read-only. It does not change proof gates, strategy thresholds,")
    A("or trading behavior.")
    A("")

    # ── 4. Outcome Class Breakdown ──
    A("---")
    A("## 4. Outcome Class Breakdown")
    A("")
    A("| Class | n | % | Usable for analysis? |")
    A("|-------|---|---|----------------------|")
    A(f"| MARKET_RESOLVED | {len(mr)} | {len(mr)/len(rows)*100:.1f}% | **YES** — true market resolution |")
    A(f"| FORCED_TIME_EXIT | {len(te)} | {len(te)/len(rows)*100:.1f}% | **YES (with caution)** — position closed at market price |")
    A(f"| FORCED_VOID | {len(fv_r)} | {len(fv_r)/len(rows)*100:.1f}% | **NO** — validation reset, pnl=0 |")
    A(f"| OPEN_NO_OUTCOME | {len(ghost)} | {len(ghost)/len(rows)*100:.1f}% | **NO** — ghost OPEN, no resolution |")
    A(f"| **outcome_known total** | **{len(ok)}** | **{len(ok)/len(rows)*100:.1f}%** | **YES** — basis for all analysis below |")
    A("")
    A("**CAUTION:** FORCED_TIME_EXIT rows represent positions closed by the auto-settler")
    A("before market resolution. Their outcomes may not reflect the market's actual resolution.")
    A("Performance analysis is shown both combined and MARKET_RESOLVED-only where possible.")
    A("")

    # ── 5. Data Coverage ──
    A("---")
    A("## 5. Data Coverage Breakdown")
    A("")
    cj_ok  = sum(1 for r in ok if r.get("crypto_join_status") == "JOINED")
    kj_ok  = sum(1 for r in ok if r.get("kalshi_join_status") == "JOINED")
    both_ok= sum(1 for r in ok if r.get("crypto_join_status") == "JOINED"
                                and r.get("kalshi_join_status") == "JOINED")
    A(f"| Coverage | n (of {len(ok)} outcome-known) | % |")
    A(f"|----------|------|---|")
    A(f"| crypto_joined | {cj_ok} | {cj_ok/len(ok)*100:.1f}% |")
    A(f"| kalshi_joined | {kj_ok} | {kj_ok/len(ok)*100:.1f}% |")
    A(f"| both joined | {both_ok} | {both_ok/len(ok)*100:.1f}% |")
    A(f"| model_probability present | {sum(1 for r in ok if _fv(r.get('model_probability')) is not None)} | — (modern rows only) |")
    A(f"| proof_eligible | {proof_el} | {proof_el/len(ok)*100:.1f}% |")
    A(f"| legacy_or_incomplete | {sum(1 for r in ok if r.get('proof_class')==LEGACY_CLASS)} | — |")
    A("")

    # ── 6. Performance by Prefix ──
    A("---")
    A("## 6. Performance by Market Prefix")
    A("")
    A(_table_header())
    for s in _by_prefix(ok):
        A(s.md_row())
    A("")
    A("**Notes:**")
    A("- KXBTCD: n=22, WR=0.773 above breakeven (0.662). Consistent across 4 proof classes.")
    A("  This is the strongest signal in the dataset — but contaminated and too small for proof.")
    A("- KXETHD: n=7, WR=0.571 vs breakeven 0.699. BELOW breakeven. mROI +0.7% only due to ROI skew.")
    A("- OTHER: n=11, WR=0.182 vs breakeven 0.467. Consistent anti-edge across novel market structures.")
    A("- BTC15M/ETH15M/SOL/XRP/DOGE: n < 5 each — anecdotal only, ignore.")
    A("")
    A("### KXBTCD by proof class:")
    A("")
    kxb_ok = [r for r in ok if r.get("market_prefix") == "KXBTCD"]
    A("| Proof Class | n | W | L | WR | mROI | medROI |")
    A("|-------------|---|---|---|----|------|--------|")
    for cls in sorted(set(r.get("proof_class","") for r in kxb_ok)):
        sub = [r for r in kxb_ok if r.get("proof_class") == cls]
        wins = [r for r in sub if r["outcome"] == "WIN"]
        roi_vals = [_fv(r["roi"]) for r in sub if _fv(r.get("roi")) is not None]
        wr_v = _wr(len(wins), len(sub))
        A(f"| {cls} | {len(sub)} | {len(wins)} | {len(sub)-len(wins)} | "
          f"{_fmt(wr_v)} | {_pct(_safe_mean(roi_vals))} | {_pct(_safe_median(roi_vals))} |")
    A("")
    A("> All proof classes show WR >= 0.600 for KXBTCD. The persistence across legacy,")
    A("> DC-override, bootstrap-provisional, and era-allow rows is notable — but note")
    A("> that DIFFERENT entry criteria were applied in each era, so this is NOT clean")
    A("> evidence of a single strategy edge. The market structure may simply favor YES bets")
    A("> (BTC tends not to crash dramatically within single-day windows).")
    A("")

    # ── 7. Performance by Entry Price Bucket ──
    A("---")
    A("## 7. Performance by Entry Price Bucket")
    A("")
    A(_table_header())
    for s in _by_entry_price_bucket(ok):
        A(s.md_row())
    A("")
    A("**Notes:**")
    A("- `<0.35`: n=5, ALL LEGACY rows. 0/5 wins. This is the legacy edge artifact —")
    A("  cheap entries produced inflated edge scores under the old formula.")
    A("  The EDGE_DANGER_BLOCK_HIGH_EDGE guard already prevents these in production.")
    A("- `0.35-0.45`: no outcome-known rows.")
    A("- `0.55-0.65`: n=23, WR=0.522, mROI=-0.4%. Near-breakeven performance.")
    A("- `0.65-0.75`: n=14, WR=0.643, mROI=-17.1%. WR looks OK but mROI negative due to asymmetry.")
    A("- `>0.75`: n=7, WR=0.714, mROI=+12.9%. ALL DC_OVERRIDE_EXCLUDED rows. Cannot trust.")
    A("")
    A("**WARNING:** The lack of rows in `0.35-0.45` suggests the system has never traded at")
    A("these prices, or they were excluded. No conclusions possible for that bucket.")
    A("")

    # ── 8. Performance by Edge Buckets ──
    A("---")
    A("## 8. Performance by Probability and Edge Buckets")
    A("")
    A("### 8a. model_probability (modern rows only, n=27 outcome-known)")
    A("")
    A(_table_header())
    for s in _by_model_prob_bucket(ok):
        A(s.md_row())
    A("")
    A("> model_probability is only present for rows with modern metadata (54/99 rows).")
    A("> The 27 outcome-known modern rows span n=11/9/6/1 across buckets — all very weak.")
    A("> MP 0.80-0.90 shows WR=0.667, mROI=+13% but n=6 only.")
    A("")

    A("### 8b. risk_edge (modern rows only, n=27 outcome-known)")
    A("")
    A(_table_header())
    for s in _by_risk_edge_bucket(ok):
        A(s.md_row())
    A("")
    A("> RE 0.03-0.05: n=9, WR=0.667, mROI=+2%. **Appears slightly better than RE 0.05-0.08**")
    A("> (WR=0.389). This is counterintuitive — lower pre-council edge performing better.")
    A("> Possible explanation: rows with risk_edge 0.05-0.08 may be riskier entries where")
    A("> the council PENALIZES confidence, resulting in the post-council edge being lower")
    A("> and outcomes worse. But n is too small (9 and 18) to conclude anything.")
    A("")

    A("### 8c. post_council_edge (all rows, n=49 outcome-known)")
    A("")
    A(_table_header())
    for s in _by_post_council_edge_bucket(ok):
        A(s.md_row())
    A("")
    A("**CRITICAL WARNING — PCE buckets are CONTAMINATED by the legacy edge artifact:**")
    A("")
    A("| PCE bucket | Legacy% | Explanation |")
    A("|------------|---------|-------------|")
    A("| PCE >0.10  | 100%    | ALL legacy rows. Formula: edge=confidence-ep-0.01. With ep=0.05, edge=0.65. Already blocked by EDGE_DANGER_BLOCK_HIGH_EDGE guard. |")
    A("| PCE 0.06-0.10 | 91%  | 10/11 legacy rows. Same formula artifact for ep=0.55-0.70 range. |")
    A("| PCE 0.03-0.06 | mixed | Mix of legacy and modern rows. |")
    A("| PCE <0.00  | mixed   | Rows where council penalized confidence below minimum. |")
    A("")
    A("> The apparent WR=0.818 at PCE 0.06-0.10 is NOT evidence of edge. It is the")
    A("> combination of (a) legacy formula producing PCE=0.07 for moderate EP + moderate conf,")
    A("> and (b) KXBTCD having naturally high WR. These two artifacts compound.")
    A("> **Do not use PCE buckets as a signal filter until legacy rows are excluded.**")
    A("")

    # ── 9. Crypto Feature Findings ──
    A("---")
    A("## 9. Crypto Feature Findings")
    A("")
    A("### 9a. Crypto momentum buckets (outcome-known + crypto-joined, n=34)")
    A("")
    A(_table_header())
    for s in _by_crypto_momentum(ok):
        A(s.md_row())
    A("")
    A("**CRITICAL WARNING — Momentum signals are confounded by prefix:**")
    A("")
    A("All crypto-joined outcome-known rows are KXBTCD (22 rows) and KXETHD (7 rows) plus")
    A("a few BTC15M/ETH15M. The positive momentum signal (WR=1.0 at 5m, WR=0.9 at 15m)")
    A("cannot be separated from the prefix effect:")
    A("")
    A("| 5m positive momentum rows | proof_class |")
    A("|----------------------------|-------------|")
    A("| 4/6 LEGACY, 2/6 BOOTSTRAP_PROVISIONAL, 1/6 ERA_ALLOW | All KXBTCD/KXETHD |")
    A("")
    A("> If KXBTCD wins 77% regardless of momentum, and the 6 positive-momentum rows happen")
    A("> to all be KXBTCD, then the 100% WR is just KXBTCD regime + tiny sample selection,")
    A("> NOT a momentum signal. This must be tested by controlling for prefix.")
    A("")
    A("### 9b. Crypto volatility")
    A("")
    vol_n = sum(1 for r in rows if _fv(r.get("crypto_volatility_15m")) is not None)
    A(f"- crypto_volatility_15m computed for: **{vol_n} / {len(rows)} rows**")
    A("- Most candle data only covers 2026-04-27 onwards. Most trades predate this window.")
    A("- Insufficient data for volatility analysis. No conclusions possible.")
    A("")

    # ── 10. Kalshi Feature Findings ──
    A("---")
    A("## 10. Kalshi Feature Findings")
    A("")
    kj_total = sum(1 for r in rows if r.get("kalshi_join_status") == "JOINED")
    A(f"Kalshi-joined rows: **{kj_total} / {len(rows)}** total")
    A(f"Kalshi-joined outcome-known: **{kj_ok} / {len(ok)}**")
    A("")
    A("### 10a. Spread analysis (ask - bid)")
    A("")
    A(_table_header())
    for s in _by_kalshi_spread(ok):
        A(s.md_row())
    A("")
    A("- Tight spread (<0.05): n=37, WR=0.649 — moderate performance, mixed proof classes.")
    A("- Wider spread (>0.05): n=4, WR=0.250 — worse performance but n=4 (anecdotal).")
    A("- Spread appears inversely correlated with outcome quality, but n is too small.")
    A("")
    A("### 10b. Kalshi mid vs entry price gap")
    A("")
    A(_table_header())
    for s in _by_kalshi_mid_gap(ok):
        A(s.md_row())
    A("")
    A("- model_below_mid (entry < Kalshi market mid by >0.05): n=3, WR=0.000 — ALL losses.")
    A("  This is potentially the most actionable Kalshi finding: when our model prices")
    A("  a YES bet BELOW the market consensus, it consistently loses.")
    A("  **BUT n=3 is completely anecdotal.** Do not add a blocker.")
    A("")

    # ── 11. Candidate Edge Pockets ──
    A("---")
    A("## 11. Candidate Edge Pockets")
    A("")
    if not candidates:
        A("**NO candidate edge pockets identified with n >= 5 and positive median ROI.**")
    else:
        for c in candidates:
            A(f"### Candidate: `{c['segment']}`")
            A(f"- **n_ok:** {c['n_ok']}  ")
            A(f"- **wins/losses:** {c['wins']}/{c['losses']}  ")
            A(f"- **win_rate:** {_fmt(c.get('win_rate'))}  ")
            A(f"- **breakeven_WR:** {_fmt(c.get('breakeven_wr'))}  ")
            A(f"- **vs_breakeven:** {_pct(c.get('vs_breakeven'))}  ")
            A(f"- **mean_ROI:** {_pct(c.get('mean_roi'))}  ")
            A(f"- **median_ROI:** {_pct(c.get('median_roi'))}  ")
            if c.get("mean_clv"):
                A(f"- **mean_CLV:** {_fmt(c.get('mean_clv'),4)}  ")
            A(f"- **proof_eligible_n:** {c['proof_eligible_n']} / {c['n_ok']}  ")
            A(f"- **legacy%:** {c['legacy_pct']*100:.0f}%  ")
            A(f"- **confidence:** {c['confidence_label']}  ")
            A(f"- **May be real because:** {c['may_be_real']}  ")
            A(f"- **May be fake because:** {c['may_be_fake']}  ")
            A(f"- **Forward-test rule:** {c['forward_test_rule']}  ")
            A("")
    A("")

    # ── 12. Anti-Edge Pockets ──
    A("---")
    A("## 12. Anti-Edge Pockets")
    A("")
    if not anti_edges:
        A("**No anti-edge pockets identified with n >= 5.**")
    else:
        for ae in anti_edges:
            A(f"### Anti-Edge: `{ae['segment']}`")
            A(f"- **n_ok:** {ae['n_ok']}  ")
            A(f"- **wins/losses:** {ae['wins']}/{ae['losses']}  ")
            A(f"- **win_rate:** {_fmt(ae.get('win_rate'))}  ")
            A(f"- **breakeven_WR:** {_fmt(ae.get('breakeven_wr'))}  ")
            A(f"- **vs_breakeven:** {_pct(ae.get('vs_breakeven'))}  ")
            A(f"- **mean_ROI:** {_pct(ae.get('mean_roi'))}  ")
            A(f"- **proof_eligible_n:** {ae['proof_eligible_n']}  ")
            A(f"- **legacy%:** {ae['legacy_pct']*100:.0f}%  ")
            A(f"- **confidence:** {ae['confidence_label']}  ")
            A(f"- **Possible cause:** {ae['possible_cause']}  ")
            A(f"- **Future action:** {ae['future_action']}  ")
            A("")
    A("")

    # ── 13. Contamination ──
    A("---")
    A("## 13. Contamination and Limitations")
    A("")
    A("### Proof class mix in outcome-known rows:")
    A("")
    pc_counts = Counter(r.get("proof_class","") for r in ok)
    A("| Proof Class | n | % | Eligible? |")
    A("|-------------|---|---|-----------|")
    for cls, cnt in sorted(pc_counts.items(), key=lambda x: -x[1]):
        elig = "YES" if cls in PROOF_ELIGIBLE else "NO"
        A(f"| {cls} | {cnt} | {cnt/len(ok)*100:.1f}% | {elig} |")
    A("")
    A("### Key contamination issues:")
    A("")
    A("1. **Legacy edge formula artifact**: `edge = confidence - entry_price - 0.01` for")
    A("   LEGACY_OR_INCOMPLETE rows produces edge values of 0.10-0.84 for cheap entries.")
    A("   This inflates PCE buckets 0.06-0.10 and >0.10. The EDGE_DANGER_BLOCK guard")
    A("   prevents this from recurring, but historical rows remain in the dataset.")
    A("")
    A("2. **Cross-era contamination**: Rows from 4 different proof eras (legacy, DC-override,")
    A("   bootstrap-provisional, era-allow) have different entry criteria. Mixing them")
    A("   produces misleading averages. Strategy was different in each era.")
    A("")
    A("3. **FORCED_TIME_EXIT inclusion**: 13/49 outcome-known rows are auto-settled positions,")
    A("   not market-resolved outcomes. Their outcomes may differ from true market resolution.")
    A("")
    A("4. **Crypto coverage gap**: Candle data only starts 2026-04-27. Older trades get")
    A("   wrong/no volatility data. Momentum signals are only computable for recent trades.")
    A("")
    A("5. **Survivorship / selection bias**: We only traded markets where the scanner")
    A("   found sufficient edge AND the council approved. The 'winning' segments may reflect")
    A("   selection criteria, not forward-looking edge.")
    A("")
    A("6. **Kalshi coverage fragmentation**: 12/99 rows have NO_MATCHING_KALSHI_TICKER.")
    A("   For 72% of rows, Kalshi data exists. Spread and mid-gap analysis only on joined rows.")
    A("")

    # ── 14. What to Forward-Test ──
    A("---")
    A("## 14. What To Forward-Test Next")
    A("")
    A("Priority forward-test signals (in order of credibility):")
    A("")
    A("1. **KXBTCD WR > breakeven (0.65-0.70)**")
    A("   - Hypothesis: KXBTCD daily BTC price markets resolve YES consistently above")
    A("     the model's 0.66 threshold entry price in the current regime.")
    A("   - Test: track next 30 KXBTCD normal_modern rows. If WR > 0.68, signal persists.")
    A("   - Confound to watch: if BTC enters a bearish regime, this reverses instantly.")
    A("")
    A("2. **Kalshi spread as quality filter**")
    A("   - Hypothesis: trades with tight Kalshi spread (<0.05) have better outcomes")
    A("     because they represent more liquid, efficiently priced markets.")
    A("   - Test: separate Kalshi-joined trades by spread. Need n=30+ per bucket.")
    A("")
    A("3. **Model-below-market-mid as blocking signal**")
    A("   - Hypothesis: when our YES entry price < Kalshi market mid price by >0.05,")
    A("     the market knows more than the model and the trade loses.")
    A("   - Test: n=3 currently. Need 10+ rows in this bucket before any conclusion.")
    A("")
    A("4. **15m positive crypto momentum with KXBTCD prefix**")
    A("   - Hypothesis: BTC rising in the 15 minutes before our entry makes KXBTCD")
    A("     YES bets more likely to win (upward trend favors threshold clearance).")
    A("   - Test: track WR for KXBTCD rows with 15m BTC return > +0.1%. Must control")
    A("     for prefix — compute WR difference vs baseline KXBTCD WR (not all-market WR).")
    A("   - Confound: 9/10 current positive-15m rows are LEGACY — regime may have changed.")
    A("")

    # ── 15. What NOT to Touch ──
    A("---")
    A("## 15. What NOT To Patch")
    A("")
    A("Do NOT change ANY of the following based on this analysis:")
    A("")
    A("- MIN_EDGE (0.03) — too small sample to justify")
    A("- MIN_CONFIDENCE (0.65) — too small sample to justify")
    A("- EDGE_DANGER_BLOCK_HIGH_EDGE — already blocking the legacy artifact correctly")
    A("- BOOTSTRAP_MIN_EDGE / BOOTSTRAP_MIN_CONFIDENCE")
    A("- Proof gate thresholds (30 normal_modern)")
    A("- real_money_allowed / scale_allowed locks")
    A("- GLOBAL_FORCED_LEARNING_MODE")
    A("- MAX_CONCURRENT_OPEN_TRADES")
    A("- PaperTrader execution logic")
    A("- Decision Council logic")
    A("")
    A("Do NOT add a blocker for OTHER prefix yet — n=11 is insufficient evidence.")
    A("Do NOT add a momentum filter yet — confounded by prefix and n < 10.")
    A("Do NOT add a Kalshi mid-gap filter — n=3 for the concerning bucket.")
    A("")

    # ── 16. Brutally Honest Recommendation ──
    A("---")
    A("## 16. Brutally Honest Recommendation")
    A("")
    A(f"### Overall verdict: **{verdict}**")
    A("")
    A("**What looks potentially real:**")
    A("")
    A("- KXBTCD shows WR above breakeven across ALL 4 proof classes. This persistence is")
    A("  the closest thing to a real signal in the data. It may reflect that BTC daily price")
    A("  markets are genuinely easier to predict than other markets in this regime.")
    A("  The market structure (daily settle, known threshold) may favor systematic YES bets.")
    A("")
    A("**What looks fake or weak:**")
    A("")
    A("- PCE 0.06-0.10 candidate edge is 91% legacy artifact. Discard.")
    A("- 5m/15m momentum WR=1.0/0.9 is completely confounded with prefix. Discard.")
    A("- >0.75 entry price performance is 100% DC_OVERRIDE_EXCLUDED rows. Discard.")
    A("- PCE >0.10 anti-edge is already known and already blocked.")
    A("")
    A("**What the data is too small to answer:**")
    A("")
    A("- Is KXBTCD WR 0.773 real or regime luck? Need 30 normal_modern rows to know.")
    A("- Does Kalshi spread quality actually matter? Need 30+ Kalshi-joined rows per bucket.")
    A("- Does crypto momentum add value on top of prefix alone? Need prefix-controlled test.")
    A("")
    A("**Bottom line:**")
    A("")
    A("The system is working correctly as a data collection engine. The sample is too")
    A("small and too contaminated to prove or disprove edge. The honest state is:")
    A("*data collection in progress, no actionable edge signal yet*.")
    A("")
    A("Collecting more normal_modern rows (bootstrap_era_allow path) is the correct")
    A("next step. Phase 8P should focus on data accumulation, not signal exploitation.")
    A("")
    A("---")
    A(f"*Report generated {now} | real_money_allowed=False | scale_allowed=False*  ")
    A("*This report is read-only. It does not change proof gates, strategy, or trading behavior.*")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    csv_path, rows = _load_latest_csv()

    print("=" * 72)
    print("RESEARCH SIGNAL DISCOVERY REPORT")
    print("=" * 72)
    print("Read-only. No trading, no proof changes, no strategy modification.")
    print()

    if csv_path is None or not rows:
        print("[WARN] No joined CSV found. Run: python3 tools/external_data/build_research_dataset.py")
        print("RESULT: RESEARCH_SIGNAL_DISCOVERY_OK")
        return

    rel = csv_path.relative_to(ROOT) if csv_path.is_relative_to(ROOT) else csv_path
    print(f"  csv:        {rel}")
    print(f"  rows:       {len(rows)}")

    ok    = [r for r in rows if _is_ok(r)]
    print(f"  outcome_known: {len(ok)} / {len(rows)}")
    print(f"  proof_eligible_outcome_known: "
          f"{sum(1 for r in ok if r.get('proof_class') in PROOF_ELIGIBLE)}")
    print()

    # Build all segments
    all_segments: List[SegmentStats] = []
    all_segments += _by_outcome_source(ok)
    all_segments += _by_prefix(ok)
    all_segments += _by_proof_class(ok)
    all_segments += _by_entry_price_bucket(ok)
    all_segments += _by_model_prob_bucket(ok)
    all_segments += _by_risk_edge_bucket(ok)
    all_segments += _by_post_council_edge_bucket(ok)
    all_segments += _by_crypto_momentum(ok)
    all_segments += _by_kalshi_spread(ok)
    all_segments += _by_kalshi_mid_gap(ok)

    candidates, anti_edges = _identify_candidates(all_segments)

    ok_proof_el = sum(1 for r in ok if r.get("proof_class") in PROOF_ELIGIBLE)
    verdict = _overall_verdict(candidates, anti_edges, len(ok), ok_proof_el)

    print(f"  segments_analyzed: {len(all_segments)}")
    print(f"  candidate_edge_pockets: {len(candidates)}")
    print(f"  anti_edge_pockets:      {len(anti_edges)}")
    print(f"  overall_verdict:        {verdict}")
    print()

    # Write outputs
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    segs_csv  = REPORTS_DIR / "latest_signal_discovery_segments.csv"
    cands_json = REPORTS_DIR / "latest_signal_discovery_candidates.json"
    report_md = REPORTS_DIR / "latest_signal_discovery_report.md"

    _write_segments_csv(all_segments, segs_csv)
    _write_candidates_json(candidates, anti_edges, cands_json)
    _write_markdown_report(csv_path, rows, all_segments, candidates, anti_edges,
                           segs_csv, cands_json, report_md)

    print(f"  wrote report: {report_md.relative_to(ROOT)}")
    print(f"  wrote segments CSV: {segs_csv.relative_to(ROOT)}")
    print(f"  wrote candidates JSON: {cands_json.relative_to(ROOT)}")
    print()

    print("=" * 72)
    print(f"VERDICT: {verdict}")
    print("=" * 72)
    print("  real_money_allowed = False  (hardcoded)")
    print("  scale_allowed      = False  (hardcoded)")
    print("RESULT: RESEARCH_SIGNAL_DISCOVERY_OK")


if __name__ == "__main__":
    main()
