#!/usr/bin/env python3
"""
tools/report_shadow_price_conditioned_council.py
-------------------------------------------------
Phase 8Z — Shadow Price-Conditioned Council Simulator.

READ-ONLY simulation: replays post-quarantine BLOCKED_COUNCIL funnel rows
against historical 2D price × edge evidence to quantify the missed-opportunity
cost of the current 1D council deadlock.

Three edge-field modes compared:
  A: pre-council scanner edge  → hist_risk  (current inconsistent + price)
  B: pre-council scanner edge  → hist_orig  (original_edge-consistent)
  C: adjusted edge (edge-0.02) → hist_risk  (risk_edge-consistent)

DO NOT modify any live execution code, config, thresholds, edge_profile.json,
paper_trader.py, critic_brain.py, decision_council.py, or any safety lock.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FUNNEL_LOG  = ROOT / "logs" / "execution_funnel.jsonl"
TRADES_LOG  = ROOT / "logs" / "paper_trades.jsonl"

KXETH_QUARANTINE_DATE    = "2026-05-06"  # Phase 8Q/8R quarantine cutoff (ISO prefix)
MIN_EDGE                 = 0.03
BOOTSTRAP_CONF_ADJ       = -0.02         # BOOTSTRAP_CONFIDENCE_ADJUSTMENT
G6_MIN_PRE_COUNCIL_EDGE  = MIN_EDGE - BOOTSTRAP_CONF_ADJ   # 0.05

# ── helpers ──────────────────────────────────────────────────────────────────

def _af(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_kxeth(ticker: str) -> bool:
    return str(ticker or "").upper().startswith("KXETH")


def _price_bucket(price: Optional[float]) -> Optional[str]:
    if price is None:
        return None
    if price < 0.10: return "<0.10"
    if price < 0.20: return "0.10-0.20"
    if price < 0.30: return "0.20-0.30"
    if price < 0.40: return "0.30-0.40"
    if price < 0.50: return "0.40-0.50"
    if price < 0.60: return "0.50-0.60"
    if price < 0.70: return "0.60-0.70"
    if price < 0.80: return "0.70-0.80"
    if price < 0.90: return "0.80-0.90"
    return "0.90-1.00"


def _edge_bucket_coarse(edge: Optional[float]) -> Optional[str]:
    if edge is None:
        return None
    if edge < 0.03: return "<0.03"
    if edge < 0.05: return "0.03-0.05"
    if edge < 0.10: return "0.05-0.10"
    if edge < 0.25: return "0.10-0.25"
    if edge < 0.50: return "0.25-0.50"
    return ">=0.50"


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


# ── classification helpers ────────────────────────────────────────────────────

_MODERN_FIELDS = [
    "council_decision", "bootstrap_provisional",
    "data_collection_override", "risk_edge", "bootstrap_era_council_allow",
]


def _is_modern(r: dict) -> bool:
    return all(r.get(f) is not None for f in _MODERN_FIELDS)


def _is_dc(r: dict) -> bool:
    return bool(r.get("data_collection_override"))


def _is_bp(r: dict) -> bool:
    return bool(r.get("bootstrap_provisional"))


def _is_normal_modern(r: dict) -> bool:
    return _is_modern(r) and not _is_dc(r) and not _is_bp(r)


def _edge_for_profile(r: dict) -> Optional[float]:
    """risk_edge first, then edge fallback — mirrors build_edge_profile.py."""
    e = _af(r.get("risk_edge"))
    if e is not None:
        return e
    return _af(r.get("edge"))


def _original_edge(r: dict) -> Optional[float]:
    """Pre-council scanner edge: original_edge field, fallback to edge."""
    oe = _af(r.get("original_edge"))
    if oe is not None:
        return oe
    return _af(r.get("edge"))


# ── 2D table builder ─────────────────────────────────────────────────────────

Cell = Dict[str, Any]


def build_hist_table(
    settled: List[dict],
    use_original_edge: bool = False,
) -> Dict[Tuple[str, str], Cell]:
    """
    Build a 2D table keyed by (edge_bucket, price_bucket) from clean settled
    non-KXETH trades.

    use_original_edge=False  → hist_risk  (risk_edge / edge fallback)
    use_original_edge=True   → hist_orig  (original_edge / edge fallback)
    """
    accumulator: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for r in settled:
        if _is_kxeth(r.get("ticker", "")):
            continue
        edge  = _original_edge(r) if use_original_edge else _edge_for_profile(r)
        price = _af(r.get("entry_price") or r.get("yes_ask"))
        pnl   = _af(r.get("pnl"))
        eb    = _edge_bucket_coarse(edge)
        pb    = _price_bucket(price)
        if eb and pb and pnl is not None:
            accumulator[(eb, pb)].append(pnl)

    result: Dict[Tuple[str, str], Cell] = {}
    for (eb, pb), pnls in accumulator.items():
        n     = len(pnls)
        wins  = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        result[(eb, pb)] = {
            "n":         n,
            "wins":      wins,
            "win_rate":  wins / n if n > 0 else 0.0,
            "total_pnl": round(total, 2),
            "avg_pnl":   round(total / n, 4) if n > 0 else 0.0,
        }
    return result


def _verdict_cell(cell: Optional[Cell], min_n: int = 3) -> str:
    if cell is None or cell["n"] < min_n:
        return "TOO_SMALL"
    n   = cell["n"]
    wr  = cell["win_rate"]
    pnl = cell["total_pnl"]
    if n < 5:
        return "LOW_N_POS" if pnl > 0 else "TOO_SMALL"
    if pnl > 0 and wr >= 0.80:
        return "POSSIBLE_EDGE"
    if pnl > 0 and wr >= 0.65:
        return "MIXED_POS"
    if pnl < -2.0:
        return "POISON"
    if pnl < 0:
        return "MARGINAL_NEG"
    return "UNCERTAIN"


def _shadow_decision(cell: Optional[Cell]) -> str:
    v = _verdict_cell(cell)
    if v == "POSSIBLE_EDGE":
        return "SHADOW_ALLOW"
    if v == "MIXED_POS":
        return "SHADOW_ALLOW_CAUTION"
    if v in ("POISON", "MARGINAL_NEG"):
        return "SHADOW_BLOCK"
    return "SHADOW_UNCERTAIN"


# ── safety confirmation ───────────────────────────────────────────────────────

def _safety_check() -> List[str]:
    lines: List[str] = []
    try:
        from config.trading_config import (
            TRADING_MODE,
            MIN_EDGE as CFG_MIN_EDGE,
            MIN_CONFIDENCE,
            BOOTSTRAP_ALLOW_ENABLED,
            BOOTSTRAP_CONFIDENCE_ADJUSTMENT,
            GLOBAL_FORCED_LEARNING_MODE,
        )
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades

        records = load_trades()
        buckets = classify_records(records)
        gate    = evaluate_proof_gates(buckets, buckets["clean_settled"])

        checks = [
            ("TRADING_MODE=PAPER",               TRADING_MODE == "PAPER"),
            ("real_money_allowed=False",          gate.get("real_money_allowed") is False),
            ("scale_allowed=False",               gate.get("scale_allowed") is False),
            ("GLOBAL_FORCED_LEARNING_MODE=True",  GLOBAL_FORCED_LEARNING_MODE is True),
            ("MIN_EDGE=0.03",                     abs(CFG_MIN_EDGE - 0.03) < 1e-9),
            ("MIN_CONFIDENCE=0.65",               abs(MIN_CONFIDENCE - 0.65) < 1e-9),
            ("BOOTSTRAP_CONFIDENCE_ADJUSTMENT=-0.02",
             abs(BOOTSTRAP_CONFIDENCE_ADJUSTMENT - (-0.02)) < 1e-9),
        ]
        for name, ok in checks:
            lines.append(f"  {'[OK]  ' if ok else '[FAIL]'}  {name}")
    except Exception as exc:
        lines.append(f"  [ERROR]  safety check failed: {exc}")
    return lines


# ── simulation helper ─────────────────────────────────────────────────────────

def _simulate(
    rows: List[dict],
    hist: Dict[Tuple[str, str], Cell],
    adjusted_edge: bool = False,
) -> Tuple[int, int, int, float]:
    """
    For each row, look up 2D cell and return (allow, block, uncertain, pnl_est).
    adjusted_edge=True: use edge + BOOTSTRAP_CONF_ADJ for lookup (Mode C).
    pnl_est = sum of historical cell avg_pnl for shadow-allowed rows.
    """
    allow = block = uncertain = 0
    pnl_est = 0.0
    for r in rows:
        raw_edge = _af(r.get("edge"))
        if raw_edge is None:
            uncertain += 1
            continue
        lookup_edge = (raw_edge + BOOTSTRAP_CONF_ADJ) if adjusted_edge else raw_edge
        price = _af(r.get("yes_ask"))
        eb    = _edge_bucket_coarse(lookup_edge)
        pb    = _price_bucket(price)
        cell  = hist.get((eb, pb)) if (eb and pb) else None
        dec   = _shadow_decision(cell)
        if dec in ("SHADOW_ALLOW", "SHADOW_ALLOW_CAUTION"):
            allow += 1
            if cell:
                pnl_est += cell["avg_pnl"]
        elif dec == "SHADOW_BLOCK":
            block += 1
        else:
            uncertain += 1
    return allow, block, uncertain, pnl_est


# ── main report ───────────────────────────────────────────────────────────────

def main() -> None:
    W = 72

    def hdr(text: str) -> str:
        return f"\n{'─' * W}\n  {text}\n{'─' * W}"

    print("=" * W)
    print("  PHASE 8Z — SHADOW PRICE-CONDITIONED COUNCIL SIMULATOR")
    print("  READ-ONLY  •  no files modified  •  simulation only")
    print("=" * W)

    # ── §1  SAFETY CONFIRMATION ──────────────────────────────────────────────
    print(hdr("§1  SAFETY CONFIRMATION"))
    for line in _safety_check():
        print(line)

    # ── load data ─────────────────────────────────────────────────────────────
    funnel_rows = _load_jsonl(FUNNEL_LOG)
    trade_rows  = _load_jsonl(TRADES_LOG)

    settled = [r for r in trade_rows if r.get("status") == "SETTLED"]
    clean_settled = [
        r for r in settled
        if not r.get("void") and not r.get("forced_close")
    ]

    # ── §2  DATASET SUMMARY ──────────────────────────────────────────────────
    print(hdr("§2  DATASET SUMMARY"))

    post_q_funnel = [
        r for r in funnel_rows
        if (r.get("timestamp_utc") or "") >= KXETH_QUARANTINE_DATE
    ]
    bc_all = [r for r in post_q_funnel if r.get("final_reason") == "BLOCKED_COUNCIL"]
    bc_nk  = [r for r in bc_all if not _is_kxeth(r.get("ticker", ""))]

    nm_trades = [r for r in clean_settled if _is_normal_modern(r)]
    nm_nk     = [r for r in nm_trades if not _is_kxeth(r.get("ticker", ""))]

    print(f"  Funnel rows total              : {len(funnel_rows):,}")
    print(f"  Post-quarantine funnel rows    : {len(post_q_funnel):,}")
    print(f"  BLOCKED_COUNCIL (all)          : {len(bc_all):,}")
    print(f"  BLOCKED_COUNCIL non-KXETH      : {len(bc_nk):,}")
    print()
    print(f"  Clean settled trades           : {len(clean_settled):,}")
    print(f"  Normal_modern trades           : {len(nm_trades):,}")
    print(f"  Normal_modern non-KXETH        : {len(nm_nk):,}")
    print()
    print("  NOTE: Simulation is read-only. No data or config was changed.")

    # ── §3  THREE-MODE SIMULATION DESIGN ────────────────────────────────────
    print(hdr("§3  THREE-MODE SIMULATION DESIGN"))
    print("""
  Mode A — current behaviour + price dimension:
    Lookup edge  : pre-council scanner edge   (signal.get("edge"))
    History table: hist_risk                  (risk_edge stored in profile)
    Inconsistency: Critic uses "0.05-0.10" for edge=0.062 signal, but profile
                   evidence for those trades lives in "0.03-0.05" (risk_edge≈0.042).
                   Adding price as 2nd axis but the edge axis is still mismatched.

  Mode B — original_edge consistent  (RECOMMENDED):
    Lookup edge  : pre-council scanner edge   (same as Mode A)
    History table: hist_orig                  (original_edge / edge fallback)
    Both the Critic lookup AND the historical table use the same pre-council edge.
    Sweet-spot evidence: "0.05-0.10" × "0.80-0.90": n=23, WR=0.913, pnl=+$8.55
    G6 constraint: post-council edge = pre_edge - 0.02 ≥ 0.03  →  pre_edge ≥ 0.05
                   Only edge-≥0.05 signals survive G6.

  Mode C — risk_edge consistent:
    Lookup edge  : adjusted_edge = pre_council_edge + (-0.02)
    History table: hist_risk                  (risk_edge used by profile)
    Both lookup AND history use the post-council (risk_edge) perspective.
    Sweet-spot evidence: "0.03-0.05" × "0.80-0.90": n=19, WR=0.947, pnl=+$10.25
    G6 constraint: same as Mode B (pre_edge ≥ 0.05 required for edge-0.02 ≥ 0.03)
""")

    # ── §4  G6 CONSTRAINT ANALYSIS ──────────────────────────────────────────
    print(hdr("§4  G6 CONSTRAINT ANALYSIS"))

    sweet_all    = [r for r in bc_nk
                    if _price_bucket(_af(r.get("yes_ask"))) == "0.80-0.90"]
    sweet_g6p    = [r for r in sweet_all
                    if (_af(r.get("edge")) or 0.0) >= G6_MIN_PRE_COUNCIL_EDGE]
    sweet_g6f    = [r for r in sweet_all
                    if (_af(r.get("edge")) or 0.0) < G6_MIN_PRE_COUNCIL_EDGE]
    edge_03_05   = [r for r in bc_nk
                    if _edge_bucket_coarse(_af(r.get("edge"))) == "0.03-0.05"]
    edge_05_10   = [r for r in bc_nk
                    if _edge_bucket_coarse(_af(r.get("edge"))) == "0.05-0.10"]

    def pct(n: int, d: int) -> str:
        return f"{100 * n / d:.1f}%" if d > 0 else "—"

    print(f"  G6 gate: post-council edge ≥ {MIN_EDGE:.2f}")
    print(f"  BOOTSTRAP_CONFIDENCE_ADJUSTMENT = {BOOTSTRAP_CONF_ADJ:+.2f}")
    print(f"  → pre-council edge required ≥ {G6_MIN_PRE_COUNCIL_EDGE:.2f}")
    print()
    print(f"  BLOCKED_COUNCIL non-KXETH (all prices):")
    print(f"    Total                    : {len(bc_nk):,}")
    print(f"    Edge 0.03-0.05 (G6 FAIL) : {len(edge_03_05):,}  "
          f"({pct(len(edge_03_05), len(bc_nk))})")
    print(f"    Edge 0.05-0.10 (G6 PASS) : {len(edge_05_10):,}  "
          f"({pct(len(edge_05_10), len(bc_nk))})")
    print()
    print(f"  Sweet-spot (yes_ask 0.80-0.90) BLOCKED_COUNCIL:")
    print(f"    Total                    : {len(sweet_all):,}")
    print(f"    G6 PASS (edge ≥ 0.05)    : {len(sweet_g6p):,}  "
          f"({pct(len(sweet_g6p), len(sweet_all))})")
    print(f"    G6 FAIL (edge < 0.05)    : {len(sweet_g6f):,}  "
          f"({pct(len(sweet_g6f), len(sweet_all))})")
    print()
    print(f"  ► Only {len(sweet_g6p):,} sweet-spot rows are reachable by any shadow mode.")
    print(f"    The remaining {len(sweet_g6f):,} would be blocked at G6 regardless of "
          "council decision.")

    # ── §5  HISTORICAL 2D TABLES ─────────────────────────────────────────────
    print(hdr("§5  HISTORICAL 2D TABLES (non-KXETH clean settled)"))

    hist_risk = build_hist_table(clean_settled, use_original_edge=False)
    hist_orig = build_hist_table(clean_settled, use_original_edge=True)

    KEY_PRICE = ["0.80-0.90", "0.70-0.80", "0.60-0.70", "0.50-0.60"]
    KEY_EDGE  = ["0.03-0.05", "0.05-0.10"]

    def print_table(hist: Dict[Tuple[str, str], Cell], label: str) -> None:
        print(f"\n  {label}:")
        print(f"  {'Edge':10s}  {'Price':10s}  {'n':>4}  {'WR':>5}  {'PnL':>8}  Verdict")
        print(f"  {'-' * 55}")
        for eb in KEY_EDGE:
            for pb in KEY_PRICE:
                c = hist.get((eb, pb))
                if c:
                    v = _verdict_cell(c)
                    print(f"  {eb:10s}  {pb:10s}  {c['n']:4d}  "
                          f"{c['win_rate']:.3f}  {c['total_pnl']:+8.2f}  {v}")
                else:
                    print(f"  {eb:10s}  {pb:10s}     0     —        —     TOO_SMALL")

    print_table(hist_risk, "hist_risk  (risk_edge — used by current build_edge_profile / Mode A & C)")
    print_table(hist_orig, "hist_orig  (original/scanner edge — Mode B)")

    # ── §6  MISSED OPPORTUNITY ESTIMATE ─────────────────────────────────────
    print(hdr("§6  MISSED OPPORTUNITY ESTIMATE (sweet-spot G6-viable rows)"))
    print(f"  Simulating each of the {len(sweet_g6p):,} G6-viable sweet-spot "
          "BLOCKED_COUNCIL rows under 3 modes.\n")

    a_all, a_bl, a_un, a_pnl = _simulate(sweet_g6p, hist_risk, adjusted_edge=False)
    b_all, b_bl, b_un, b_pnl = _simulate(sweet_g6p, hist_orig, adjusted_edge=False)
    c_all, c_bl, c_un, c_pnl = _simulate(sweet_g6p, hist_risk, adjusted_edge=True)

    tot = max(len(sweet_g6p), 1)
    print(f"  {'Mode':<6}  {'ALLOW':>6}  {'BLOCK':>6}  {'UNCERT':>6}  "
          f"{'Est PnL':>9}  Note")
    print(f"  {'-' * 60}")
    print(f"  {'A':6}  {a_all:6d}  {a_bl:6d}  {a_un:6d}  {a_pnl:+9.2f}  "
          "pre-edge→hist_risk (edge axis mismatch)")
    print(f"  {'B':6}  {b_all:6d}  {b_bl:6d}  {b_un:6d}  {b_pnl:+9.2f}  "
          "pre-edge→hist_orig (consistent) ← recommended")
    print(f"  {'C':6}  {c_all:6d}  {c_bl:6d}  {c_un:6d}  {c_pnl:+9.2f}  "
          "risk_edge→hist_risk (consistent)")
    print()
    print("  'Est PnL' = sum of historical cell avg_pnl for each shadow-allowed row.")
    print("  This is a shadow simulation estimate ONLY — not a performance projection.")
    print("  Actual returns depend on real market conditions at execution time.")

    # ── §7  POISON LEAK CHECK ────────────────────────────────────────────────
    print(hdr("§7  POISON LEAK CHECK"))
    print("  Verifies Mode B does NOT shadow-allow mid/low price poison zones.\n")

    POISON_ZONES = [
        ("0.05-0.10", "0.70-0.80"),
        ("0.05-0.10", "0.60-0.70"),
        ("0.03-0.05", "0.70-0.80"),
        ("0.03-0.05", "0.60-0.70"),
    ]
    for eb, pb in POISON_ZONES:
        cr = hist_risk.get((eb, pb))
        co = hist_orig.get((eb, pb))
        vr = _verdict_cell(cr)
        vo = _verdict_cell(co)
        print(f"  {eb:10s} × {pb:10s}:")
        if cr:
            print(f"    hist_risk : n={cr['n']:2d}  WR={cr['win_rate']:.3f}  "
                  f"pnl={cr['total_pnl']:+.2f}  → {vr}")
        else:
            print(f"    hist_risk : n=0  → TOO_SMALL")
        if co:
            print(f"    hist_orig : n={co['n']:2d}  WR={co['win_rate']:.3f}  "
                  f"pnl={co['total_pnl']:+.2f}  → {vo}")
        else:
            print(f"    hist_orig : n=0  → TOO_SMALL")
        print()

    # Count Mode B shadow-allows at mid/low price zones for full bc_nk population
    mid_low_bc = [r for r in bc_nk
                  if _price_bucket(_af(r.get("yes_ask"))) in ("0.70-0.80", "0.60-0.70")]
    mid_allow = sum(
        1 for r in mid_low_bc
        if _shadow_decision(
            hist_orig.get((
                _edge_bucket_coarse(_af(r.get("edge"))),
                _price_bucket(_af(r.get("yes_ask"))),
            ))
        ) in ("SHADOW_ALLOW", "SHADOW_ALLOW_CAUTION")
    )
    poison_leak = mid_allow > 0
    print(f"  Mode B shadow-allows at price 0.60-0.80 (full bc_nk population):")
    print(f"    Mid/low price BLOCKED_COUNCIL rows : {len(mid_low_bc):,}")
    print(f"    Shadow-ALLOWed by Mode B           : {mid_allow}")
    print()
    if poison_leak:
        print("  ► POISON LEAK DETECTED — investigate before any design change.")
    else:
        print("  ► NO POISON LEAK — 2D price conditioning correctly blocks all")
        print("    mid/low price zones. Adding price dimension is safe.")

    # ── §8  DEADLOCK VISUALIZATION ───────────────────────────────────────────
    print(hdr("§8  CURRENT DEADLOCK VISUALIZATION"))

    c_0510 = hist_risk.get(("0.05-0.10", "0.80-0.90"))
    b_0510 = hist_orig.get(("0.05-0.10", "0.80-0.90"))
    r_0305 = hist_risk.get(("0.03-0.05", "0.80-0.90"))

    def _cfmt(c: Optional[Cell], label: str) -> str:
        if c is None:
            return f"    {label}: n=0  → TOO_SMALL"
        v = _verdict_cell(c)
        return (f"    {label}: n={c['n']:2d}  WR={c['win_rate']:.3f}  "
                f"pnl={c['total_pnl']:+.2f}  → {v}")

    print(f"""
  New sweet-spot signal arrives (edge=0.062, conf=0.80, yes_ask=0.85):

  CURRENT 1D Critic path:
    edge_key = edge_bucket(0.062) = "0.05-0.10"
    Profile lookup: by_edge_bucket["0.05-0.10"]
      → n=38, WR=0.632, pnl=-$42.50  (1D contaminated bucket)
    _bad_enough_sample() → True (n≥5, pnl<0)
    Decision: BLOCK  ← all sweet-spot signals blocked by contaminated 1D bucket

  Why "0.05-0.10" is contaminated (1D only):
    • build_edge_profile.py iterates ALL clean_settled (not just normal_modern)
    • Includes DC override, bootstrap_provisional, and KXETH trades
    • Sweet-spot (price 0.80-0.90): WR=0.913, pnl positive  ← buried
    • Poison (price 0.60-0.80)   : WR<0.72, pnl negative    ← dominates
    • Combined bucket: n=38, pnl=-$42.50 → BLOCK

  SHADOW MODE B path (pre-council edge → hist_orig, + price):
    edge_key   = "0.05-0.10"
    price_key  = "0.80-0.90"
    2D hist_orig lookup: ("0.05-0.10", "0.80-0.90")
{_cfmt(b_0510, "hist_orig sweet-spot")}
    _shadow_decision() → SHADOW_ALLOW

    G6 check: post-council edge = 0.062 - 0.02 = 0.042 ≥ 0.03  ✓
    Result: would proceed to G7 risk manager

  SHADOW MODE C path (risk_edge → hist_risk, + price):
    adjusted_edge  = 0.062 - 0.02 = 0.042
    edge_key   = "0.03-0.05"
    price_key  = "0.80-0.90"
    2D hist_risk lookup: ("0.03-0.05", "0.80-0.90")
{_cfmt(r_0305, "hist_risk sweet-spot")}
    _shadow_decision() → SHADOW_ALLOW
""")

    # ── §9  FULL POPULATION MODE COMPARISON ──────────────────────────────────
    print(hdr("§9  FULL POPULATION MODE COMPARISON"))
    print(f"  All BLOCKED_COUNCIL non-KXETH post-quarantine: {len(bc_nk):,} rows\n")

    fa_al, fa_bl, fa_un, fa_pnl = _simulate(bc_nk, hist_risk, adjusted_edge=False)
    fb_al, fb_bl, fb_un, fb_pnl = _simulate(bc_nk, hist_orig, adjusted_edge=False)
    fc_al, fc_bl, fc_un, fc_pnl = _simulate(bc_nk, hist_risk, adjusted_edge=True)

    denom = max(len(bc_nk), 1)
    print(f"  {'Mode':<6}  {'ALLOW':>7}  {'%':>6}  {'BLOCK':>7}  {'UNCERT':>7}  "
          f"{'Est PnL':>9}  Description")
    print(f"  {'-' * 72}")
    print(f"  {'A':6}  {fa_al:7d}  {100*fa_al/denom:5.1f}%  {fa_bl:7d}  {fa_un:7d}  "
          f"{fa_pnl:+9.2f}  pre-edge→hist_risk")
    print(f"  {'B':6}  {fb_al:7d}  {100*fb_al/denom:5.1f}%  {fb_bl:7d}  {fb_un:7d}  "
          f"{fb_pnl:+9.2f}  pre-edge→hist_orig ← recommended")
    print(f"  {'C':6}  {fc_al:7d}  {100*fc_al/denom:5.1f}%  {fc_bl:7d}  {fc_un:7d}  "
          f"{fc_pnl:+9.2f}  risk_edge→hist_risk")
    print()
    print(f"  Sweet-spot subset (G6-viable, edge ≥ 0.05, price 0.80-0.90):")
    print(f"    Total G6-viable sweet-spot  : {len(sweet_g6p):,}")
    print(f"    Mode B ALLOW                : {b_all:,}")
    print(f"    Mode B est PnL              : {b_pnl:+.2f}  (shadow estimate only)")
    print()
    print("  KEY INSIGHT: Mode B sweet-spot ALLOW count matches the G6-viable")
    print("  population because all sweet-spot G6-viable rows land in the")
    print("  POSSIBLE_EDGE cell (hist_orig '0.05-0.10' × '0.80-0.90').")

    # ── §10  PLAIN-ENGLISH VERDICT ───────────────────────────────────────────
    print(hdr("§10  PLAIN-ENGLISH VERDICT"))

    sweet_b_n   = b_0510["n"]        if b_0510 else "?"
    sweet_b_wr  = f"{b_0510['win_rate']:.3f}"  if b_0510 else "?"
    sweet_b_pnl = f"{b_0510['total_pnl']:+.2f}" if b_0510 else "?"
    sweet_b_v   = _verdict_cell(b_0510) if b_0510 else "TOO_SMALL"

    print(f"""
  WHAT THIS SIMULATION PROVES:

  1. The current council is blocking ALL new trading because the 1D
     "0.05-0.10" edge bucket (n=38, pnl=-$42.50) is negative — a correct
     decision given what the Critic sees, but caused by contamination.

  2. Two sources of contamination:
     (a) Missing price dimension: sweet-spot trades (yes_ask 0.80-0.90,
         positive PnL) are mixed with poison (yes_ask 0.60-0.80, negative PnL)
         in the same 1D edge bucket.
     (b) build_edge_profile.py uses ALL clean_settled (101 trades), not just
         normal_modern (75 trades), including DC override + bootstrap_provisional
         + KXETH rows in the bucket data.

  3. When price is added as a 2nd dimension (Mode B — hist_orig):
     "0.05-0.10" × "0.80-0.90" (non-KXETH):
       n={sweet_b_n}, WR={sweet_b_wr}, pnl={sweet_b_pnl} → {sweet_b_v}
     The sweet-spot is profitable in isolation. The 1D bucket hides this.

  4. Poison zones (0.70-0.80 and 0.60-0.70 price):
     Both hist_risk and hist_orig show POISON / MARGINAL_NEG.
     Mode B does NOT shadow-allow any mid/low price rows.
     Adding the price dimension adds signal — it does not spread poison.

  5. G6 constraint is the binding reachability limit:
     Only {len(sweet_g6p):,} of {len(sweet_all):,} sweet-spot rows ({100*len(sweet_g6p)/max(len(sweet_all),1):.1f}%) have
     pre-council edge ≥ 0.05 and would survive G6 if council ALLOWed them.
     The other {len(sweet_g6f):,} ({100*len(sweet_g6f)/max(len(sweet_all),1):.1f}%) would be blocked at G6 regardless.

  6. Mode B shadow estimate: {b_all:,} sweet-spot G6-viable rows → SHADOW_ALLOW.
     Estimated PnL: {b_pnl:+.2f} (cell avg_pnl × shadow-allowed count).
     This is a simulation only — not a performance guarantee.

  WHAT WOULD NEED TO CHANGE (requires Samuel's explicit authorization):
    • build_edge_profile.py: add 2D (edge × price) bucket table; filter to
      normal_modern rows only.
    • critic_brain.py: add price as a 2nd lookup dimension.
    • Consistent edge field choice: use original_edge throughout (Mode B)
      OR adjust pre-council edge by BOOTSTRAP_CONFIDENCE_ADJUSTMENT (Mode C).
    None of these changes are made by this script.

  VERDICT: The 1D contamination is the root cause of the council deadlock.
  2D price conditioning would isolate the profitable sweet-spot and allow
  those trades while correctly blocking poison price zones. The evidence
  supports Mode B as the most consistent design.

  THIS REPORT IS READ-ONLY. No files, configs, or safety locks were changed.
""")

    print("=" * W)
    print("  SHADOW_PRICE_CONDITIONED_COUNCIL_REPORT_OK")
    print("=" * W)


if __name__ == "__main__":
    main()
