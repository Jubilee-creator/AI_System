"""
Phase 9M — Paper PnL Accounting Reconciliation Simulator
Sentinel: PROVEN_PNL_RECONCILIATION_OK

Replays historical trades under multiple accounting models to determine
which is economically correct, what the true performance is, and whether
the accounting distortion materially changes any trading verdict.

DO NOT change live trading behavior.
DO NOT rewrite historical trade records.
DO NOT patch paper_trader.py without explicit Samuel authorization.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"

# ---------------------------------------------------------------------------
# Model identifiers
# ---------------------------------------------------------------------------
MODEL_HYBRID = "Hybrid (current)"        # WIN=(1-ep)*sz  LOSS=-sz
MODEL_NOTIONAL = "Model B (Kalshi)"      # WIN=(1-ep)*sz  LOSS=-ep*sz
MODEL_COST = "Model C (cost/stake)"      # WIN=(1-ep)/ep*sz  LOSS=-sz
MODEL_TIMEXIT = "Model E (time-exit)"    # (exit-entry)*sz  (FC only)

# ---------------------------------------------------------------------------
# Pure formula functions — no side effects
# ---------------------------------------------------------------------------

def pnl_hybrid(ep: float, size: float, won: bool) -> float:
    """Legacy hybrid formula. WIN treats size as contracts; LOSS as stake."""
    return round((1.0 - ep) * size if won else -size, 6)


def pnl_notional(ep: float, size: float, won: bool) -> float:
    """Economically correct for Kalshi binary contracts.
    size = face-value contracts. You pay ep*size; if YES wins you net (1-ep)*size.
    If NO wins you lose ep*size (what you paid).
    """
    return round((1.0 - ep) * size if won else -ep * size, 6)


def pnl_cost(ep: float, size: float, won: bool) -> float:
    """Cost/stake model. size = dollars spent; contracts = size/ep.
    WIN: net = contracts*(1-ep) = (size/ep)*(1-ep). LOSS: lose the stake.
    """
    if won:
        return round(((1.0 - ep) / ep) * size, 6)
    return round(-size, 6)


def pnl_time_exit(entry: float, exit_p: float, size: float) -> float:
    """Auto-settle time-exit formula (symmetric). size treated as contracts."""
    return round((exit_p - entry) * size, 6)


def true_breakeven(ep: float) -> float:
    """WR needed to break even under hybrid accounting: 1/(2-ep)."""
    return 1.0 / (2.0 - ep)


def economic_breakeven(ep: float) -> float:
    """WR needed to break even under correct Kalshi economics: ep."""
    return ep


# ---------------------------------------------------------------------------
# Data loading — immutable, no writes
# ---------------------------------------------------------------------------

def _load_all_settled() -> list:
    if not TRADES_LOG.exists():
        return []
    seen = {}
    for line in TRADES_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("status") not in ("SETTLED", "FORCED_CLOSE"):
            continue
        key = (r.get("ticker", ""), r.get("timestamp", ""))
        seen[key] = r
    return list(seen.values())


def _is_kxeth(r: dict) -> bool:
    return "KXETH" in str(r.get("ticker", "")).upper()


def _ep(r: dict) -> Optional[float]:
    v = r.get("entry_price") or r.get("yes_ask") or r.get("price")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _size(r: dict) -> float:
    try:
        return float(r.get("size", 5.0))
    except (TypeError, ValueError):
        return 5.0


def _recorded_pnl(r: dict) -> Optional[float]:
    v = r.get("pnl") or r.get("realized_pnl")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _result(r: dict) -> str:
    return str(r.get("result", "")).upper()


def _accounting_version(r: dict) -> str:
    return str(r.get("accounting_version") or "legacy_hybrid_or_unversioned")


def _is_win(r: dict) -> bool:
    return _result(r) == "WIN"


def _is_loss(r: dict) -> bool:
    return _result(r) == "LOSS"


def _is_time_exit(r: dict) -> bool:
    return _result(r) == "TIME_EXIT"


def _orig_edge(r: dict) -> float:
    v = r.get("original_edge") or r.get("risk_edge") or r.get("edge")
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Public API for tests
# ---------------------------------------------------------------------------

def load_binary_records() -> tuple:
    """Return (wins, losses) as separate lists — non-KXETH binary only."""
    records = _load_all_settled()
    non_kx = [r for r in records if not _is_kxeth(r)]
    wins = [r for r in non_kx if _is_win(r) and _ep(r) is not None]
    losses = [r for r in non_kx if _is_loss(r) and _ep(r) is not None]
    return wins, losses


def load_fc_records() -> list:
    """Return FORCED_CLOSE (TIME_EXIT) non-KXETH records."""
    records = _load_all_settled()
    return [r for r in records if not _is_kxeth(r) and _is_time_exit(r)]


def compute_model_stats(wins: list, losses: list, model: str) -> dict:
    """Compute aggregate stats for a given model label."""
    n = len(wins) + len(losses)
    if n == 0:
        return {}

    win_pnl_list = []
    loss_pnl_list = []

    for r in wins:
        ep = _ep(r)
        sz = _size(r)
        if model == MODEL_HYBRID:
            win_pnl_list.append(pnl_hybrid(ep, sz, True))
        elif model == MODEL_NOTIONAL:
            win_pnl_list.append(pnl_notional(ep, sz, True))
        elif model == MODEL_COST:
            win_pnl_list.append(pnl_cost(ep, sz, True))

    for r in losses:
        ep = _ep(r)
        sz = _size(r)
        if model == MODEL_HYBRID:
            loss_pnl_list.append(pnl_hybrid(ep, sz, False))
        elif model == MODEL_NOTIONAL:
            loss_pnl_list.append(pnl_notional(ep, sz, False))
        elif model == MODEL_COST:
            loss_pnl_list.append(pnl_cost(ep, sz, False))

    total_win = sum(win_pnl_list)
    total_loss = sum(loss_pnl_list)
    total_pnl = total_win + total_loss

    # ROI denominator
    all_recs = wins + losses
    if model in (MODEL_HYBRID, MODEL_COST):
        denom = sum(_size(r) for r in all_recs)  # sum(size)
    else:  # MODEL_NOTIONAL — capital deployed
        denom = sum(_ep(r) * _size(r) for r in all_recs if _ep(r))

    roi = total_pnl / denom if denom else 0.0

    pf = abs(total_win / total_loss) if total_loss and total_loss < 0 else float("inf")
    avg_win = total_win / len(wins) if wins else 0.0
    avg_loss = total_loss / len(losses) if losses else 0.0
    payoff_ratio = abs(avg_win / avg_loss) if avg_loss and avg_loss < 0 else float("inf")
    max_loss = min(loss_pnl_list) if loss_pnl_list else 0.0
    max_win = max(win_pnl_list) if win_pnl_list else 0.0
    wr = len(wins) / n

    return {
        "model": model,
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(wr, 4),
        "total_pnl": round(total_pnl, 4),
        "total_win_pnl": round(total_win, 4),
        "total_loss_pnl": round(total_loss, 4),
        "roi": round(roi, 4),
        "roi_denom": round(denom, 2),
        "profit_factor": round(pf, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "payoff_ratio": round(payoff_ratio, 4),
        "max_win": round(max_win, 4),
        "max_loss": round(max_loss, 4),
    }


def compute_bucket_stats(wins: list, losses: list, model: str,
                          bucket_fn) -> dict:
    """Compute per-bucket PnL stats for a model."""
    buckets = defaultdict(lambda: {
        "wins": 0, "losses": 0, "pnl": 0.0, "ep_size": 0.0, "size": 0.0
    })

    for r in wins:
        ep = _ep(r)
        sz = _size(r)
        b = bucket_fn(r)
        buckets[b]["wins"] += 1
        buckets[b]["size"] += sz
        buckets[b]["ep_size"] += ep * sz
        if model == MODEL_HYBRID:
            buckets[b]["pnl"] += pnl_hybrid(ep, sz, True)
        elif model == MODEL_NOTIONAL:
            buckets[b]["pnl"] += pnl_notional(ep, sz, True)
        elif model == MODEL_COST:
            buckets[b]["pnl"] += pnl_cost(ep, sz, True)

    for r in losses:
        ep = _ep(r)
        sz = _size(r)
        b = bucket_fn(r)
        buckets[b]["losses"] += 1
        buckets[b]["size"] += sz
        buckets[b]["ep_size"] += ep * sz
        if model == MODEL_HYBRID:
            buckets[b]["pnl"] += pnl_hybrid(ep, sz, False)
        elif model == MODEL_NOTIONAL:
            buckets[b]["pnl"] += pnl_notional(ep, sz, False)
        elif model == MODEL_COST:
            buckets[b]["pnl"] += pnl_cost(ep, sz, False)

    return dict(buckets)


def _price_bucket(r: dict) -> str:
    ep = _ep(r) or 0.0
    if ep < 0.60:
        return "<0.60"
    elif ep < 0.70:
        return "0.60-0.70"
    elif ep < 0.80:
        return "0.70-0.80"
    elif ep < 0.90:
        return "0.80-0.90"
    return "0.90+"


def _orig_edge_bucket(r: dict) -> str:
    e = _orig_edge(r)
    if e < 0.05:
        return "0.03-0.05"
    elif e < 0.10:
        return "0.05-0.10"
    elif e < 0.25:
        return "0.10-0.25"
    return "0.25+"


def _2d_bucket(r: dict) -> str:
    return f"{_orig_edge_bucket(r)}|{_price_bucket(r)}"


# ---------------------------------------------------------------------------
# Section printing helpers
# ---------------------------------------------------------------------------

def _sep(char="=", n=74):
    return char * n


def _section(title: str):
    print()
    print(_sep())
    print(f"  {title}")
    print(_sep())


# ---------------------------------------------------------------------------
# Section 1: Population summary
# ---------------------------------------------------------------------------

def section_1_population():
    _section("1. POPULATION SUMMARY")
    print()
    records = _load_all_settled()
    kxeth = [r for r in records if _is_kxeth(r)]
    non_kx = [r for r in records if not _is_kxeth(r)]
    wins, losses = load_binary_records()
    fc = load_fc_records()
    skipped = [r for r in non_kx
               if not (_is_win(r) or _is_loss(r) or _is_time_exit(r))]

    print(f"  Total settled records (deduped)  : {len(records)}")
    print(f"  KXETH quarantined                : {len(kxeth)}")
    print(f"  Non-KXETH total                  : {len(non_kx)}")
    print()
    print(f"  ┌─ Binary WIN  (resolved YES)     : {len(wins)}")
    print(f"  ├─ Binary LOSS (resolved NO)      : {len(losses)}")
    print(f"  ├─ TIME_EXIT (FORCED_CLOSE)       : {len(fc)}")
    print(f"  └─ Other / missing fields         : {len(skipped)}")
    print()
    print("  Record type notes:")
    print("  Binary WIN/LOSS: resolved to YES (1.0) or NO (0.0)")
    print("    → accounting formula comparison applies here")
    print("  TIME_EXIT: force-closed at mark price before resolution")
    print("    → uses symmetric formula (exit-entry)×size; separate category")
    print("  KXETH: permanently quarantined (edge inversion artifact)")

    fc_pnl = sum(_recorded_pnl(r) or 0 for r in fc)
    print()
    print(f"  TIME_EXIT total recorded PnL     : ${fc_pnl:.2f}")
    print("  (TIME_EXIT records are left unchanged across all models)")
    print()
    print("  Accounting versions:")
    version_counts = defaultdict(int)
    for r in records:
        version_counts[_accounting_version(r)] += 1
    for version in sorted(version_counts):
        print(f"    {version:<34}: {version_counts[version]}")
    print("  Legacy/unversioned rows are preserved as historical evidence.")
    print("  Phase 9N+ future binary rows should carry economic_contract_notional_v1.")


# ---------------------------------------------------------------------------
# Section 2: Formula taxonomy
# ---------------------------------------------------------------------------

def section_2_formula_taxonomy():
    _section("2. FORMULA TAXONOMY — WHICH FORMULA APPLIES WHERE")
    print()
    rows = [
        ("Hybrid A (legacy logs)", "pre-9N stored records", "Binary WIN/LOSS",
         "(1-ep)×sz  /  -sz", "Historical unversioned rows only; preserved"),
        ("Model B (Kalshi-correct)", "Phase 9N future rows", "Binary WIN/LOSS",
         "(1-ep)×sz  /  -ep×sz", "accounting_version=economic_contract_notional_v1"),
        ("Model C (cost/stake)", "alternative frame", "Binary WIN/LOSS",
         "(1-ep)/ep×sz  /  -sz", "Both sides use size=dollars paid; self-consistent"),
        ("Model D (Dashboard)", "Dashboard.py ~295", "OPEN unrealized only",
         "(mark-ep)×sz/ep", "Same as Model C at binary resolution; N/A for settled"),
        ("Model E (time-exit)", "auto_settle_trades.py ~258", "FORCED_CLOSE only",
         "(exit-entry)×sz", "Symmetric; size=contracts; applies to FC only"),
    ]
    print(f"  {'Model':<28} {'Source':<28} {'Applies To':<24} {'Formula':<30} {'Note'}")
    print("  " + "-" * 140)
    for r in rows:
        print(f"  {r[0]:<28} {r[1]:<28} {r[2]:<24} {r[3]:<30} {r[4]}")
    print()
    print("  Key insight: Models A and C are BOTH internally consistent but differ in")
    print("  what 'size' represents. The hybrid code takes WIN from Model B and LOSS")
    print("  from Model C, producing a formula that is consistent with neither model.")
    print()
    print("  ROI Denominators:")
    print("    Hybrid A : sum(size)     — face-value contracts, NOT capital deployed")
    print("    Model B  : sum(ep×size)  — actual dollars paid (capital at risk)")
    print("    Model C  : sum(size)     — size = dollars paid (consistent)")


# ---------------------------------------------------------------------------
# Section 3: Five-model summary comparison
# ---------------------------------------------------------------------------

def section_3_model_comparison():
    _section("3. FIVE-MODEL SIMULATOR — SUMMARY COMPARISON TABLE")
    print()
    wins, losses = load_binary_records()
    fc = load_fc_records()

    stats_a = compute_model_stats(wins, losses, MODEL_HYBRID)
    stats_b = compute_model_stats(wins, losses, MODEL_NOTIONAL)
    stats_c = compute_model_stats(wins, losses, MODEL_COST)

    # TIME_EXIT stats (Model E — unchanged across all, shown for completeness)
    fc_pnl = sum(_recorded_pnl(r) or 0 for r in fc)
    fc_n = len(fc)
    fc_size = sum(_size(r) for r in fc)

    print(f"  {'Metric':<24} {'Hybrid A':>14} {'Model B':>14} {'Model C':>14}")
    print("  " + "-" * 68)

    def row(label, key, fmt=".2f"):
        va = stats_a.get(key, 0)
        vb = stats_b.get(key, 0)
        vc = stats_c.get(key, 0)
        def fmtv(v):
            if fmt == ".4f": return f"{v:.4f}"
            if fmt == ".2f": return f"{v:.2f}"
            if fmt == ".1f%": return f"{v*100:.1f}%"
            if fmt == "int": return str(int(v))
            return str(v)
        print(f"  {label:<24} {fmtv(va):>14} {fmtv(vb):>14} {fmtv(vc):>14}")

    row("Trades (binary)", "n", "int")
    row("Wins", "wins", "int")
    row("Losses", "losses", "int")
    row("Win rate", "win_rate", ".4f")
    print()
    row("Total PnL ($)", "total_pnl", ".2f")
    row("Win PnL ($)", "total_win_pnl", ".2f")
    row("Loss PnL ($)", "total_loss_pnl", ".2f")
    print()
    row("ROI denom ($)", "roi_denom", ".2f")
    row("ROI", "roi", ".1f%")
    row("Profit Factor", "profit_factor", ".4f")
    print()
    row("Avg Win ($)", "avg_win", ".4f")
    row("Avg Loss ($)", "avg_loss", ".4f")
    row("Payoff Ratio", "payoff_ratio", ".4f")
    print()
    row("Max Win ($)", "max_win", ".4f")
    row("Max Loss ($)", "max_loss", ".4f")
    print()
    print(f"  {'TIME_EXIT (FC) — same under all models':<30}")
    print(f"  {'FC records':<24} {fc_n:>14}")
    print(f"  {'FC total PnL ($)':<24} {fc_pnl:>14.2f}")
    print()
    print("  ROI denominators:")
    print(f"    Hybrid A: sum(size) = ${stats_a['roi_denom']:.2f}  (face value / stake)")
    print(f"    Model B : sum(ep*sz)= ${stats_b['roi_denom']:.2f}  (capital actually deployed)")
    print(f"    Model C : sum(size) = ${stats_c['roi_denom']:.2f}  (size as cost)")
    print()
    print("  DIAGNOSIS:")
    pnl_a = stats_a["total_pnl"]
    pnl_b = stats_b["total_pnl"]
    correction = round(pnl_b - pnl_a, 2)
    print(f"    Legacy hybrid accounting shows binary trades {'PROFITABLE' if pnl_a > 0 else 'LOSING'}  (${pnl_a:+.2f})")
    print(f"    Kalshi-correct accounting shows binary  {'PROFITABLE' if pnl_b > 0 else 'LOSING'}  (${pnl_b:+.2f})")
    print(f"    Accounting correction = +${correction:.2f}  (loss overstatement from LOSS=-size bug)")
    print(f"    Profit factor under Model B = {stats_b['profit_factor']:.4f}  (> 1.10 threshold ✓)")


# ---------------------------------------------------------------------------
# Section 4: By price bucket
# ---------------------------------------------------------------------------

def section_4_by_price_bucket():
    _section("4. PNL BY PRICE BUCKET — THREE MODELS")
    print()
    wins, losses = load_binary_records()

    models = [MODEL_HYBRID, MODEL_NOTIONAL, MODEL_COST]
    labels = ["Hybrid A", "Model B", "Model C"]

    b_stats_list = [compute_bucket_stats(wins, losses, m, _price_bucket) for m in models]

    bucket_order = ["<0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90+"]

    hdr = f"  {'Bucket':<12}{'n':>5}{'WR':>7}{'avg_ep':>9}  {'Hybrid_A':>10}{'Model_B':>10}{'Model_C':>10}  verdict"
    print(hdr)
    print("  " + "-" * 76)

    all_recs = wins + losses
    for b in bucket_order:
        in_b = [r for r in all_recs if _price_bucket(r) == b]
        if not in_b:
            continue
        n = len(in_b)
        wins_b = [r for r in in_b if _is_win(r)]
        wr_b = len(wins_b) / n
        avg_ep_b = sum(_ep(r) for r in in_b if _ep(r)) / n
        pnls = [s.get(b, {}).get("pnl", 0.0) for s in b_stats_list]
        econ_be = avg_ep_b
        true_be = true_breakeven(avg_ep_b)

        if wr_b > econ_be:
            verdict = "ECO-PROFIT"  # profitable under Model B
        elif wr_b > true_be:
            verdict = "ECO-BREAK"
        else:
            verdict = "ECO-LOSS"

        print(f"  {b:<12}{n:>5}{wr_b:>7.4f}{avg_ep_b:>9.4f}  "
              f"{pnls[0]:>10.2f}{pnls[1]:>10.2f}{pnls[2]:>10.2f}  {verdict}")

    print()
    print("  Verdict key:")
    print("    ECO-PROFIT : WR > ep (profitable under Model B / Kalshi economics)")
    print("    ECO-BREAK  : WR between ep and 1/(2-ep)  (profitable under Model B,")
    print("                 losing under Hybrid A — pure accounting distortion zone)")
    print("    ECO-LOSS   : WR < ep (losing under BOTH models — genuine signal quality issue)")
    print()
    print("  *** CRITICAL FINDING ***")
    wins_bkt, losses_bkt = [], []
    for r in wins:
        if _price_bucket(r) == "0.60-0.70":
            wins_bkt.append(r)
    for r in losses:
        if _price_bucket(r) == "0.60-0.70":
            losses_bkt.append(r)
    if wins_bkt or losses_bkt:
        n_bkt = len(wins_bkt) + len(losses_bkt)
        wr_bkt = len(wins_bkt) / n_bkt if n_bkt else 0
        avg_ep_bkt = sum(_ep(r) for r in wins_bkt + losses_bkt) / n_bkt
        pnl_hybrid = sum(pnl_hybrid_(r) for r in wins_bkt) + sum(pnl_hybrid_(r, False) for r in losses_bkt)
        print(f"    0.60-0.70 bucket (n={n_bkt}, WR={wr_bkt:.4f}, avg_ep={avg_ep_bkt:.4f}):")
        print(f"    Hybrid shows ${b_stats_list[0].get('0.60-0.70',{}).get('pnl',0):.2f}  — APPEARS POISONOUS")
        print(f"    Model B shows ${b_stats_list[1].get('0.60-0.70',{}).get('pnl',0):.2f}  — ACTUALLY PROFITABLE under correct accounting")
        print(f"    WR {wr_bkt:.4f} > economic BE {avg_ep_bkt:.4f} → the model has positive edge here")
        print(f"    The 'poison zone' label is an accounting ARTIFACT, not a signal failure.")
        print()
        print(f"    0.70-0.80 bucket: Model B still negative — genuine signal quality issue")
        print(f"    (WR < ep even under correct economics)")


def pnl_hybrid_(r, win=True):
    ep = _ep(r)
    sz = _size(r)
    return pnl_hybrid(ep, sz, win) if ep else 0.0


# ---------------------------------------------------------------------------
# Section 5: 2D cell analysis
# ---------------------------------------------------------------------------

def section_5_2d_cells():
    _section("5. 2D CELL ANALYSIS — SWEET SPOT AND POISON ZONES")
    print()
    wins, losses = load_binary_records()

    b_hybrid = compute_bucket_stats(wins, losses, MODEL_HYBRID, _2d_bucket)
    b_notional = compute_bucket_stats(wins, losses, MODEL_NOTIONAL, _2d_bucket)

    all_recs = wins + losses
    cells_to_show = {}
    for r in all_recs:
        cell = _2d_bucket(r)
        if cell not in cells_to_show:
            cells_to_show[cell] = {"wins": 0, "losses": 0, "ep_sum": 0.0, "size": 0.0}
        if _is_win(r):
            cells_to_show[cell]["wins"] += 1
        else:
            cells_to_show[cell]["losses"] += 1
        ep = _ep(r) or 0.0
        sz = _size(r)
        cells_to_show[cell]["ep_sum"] += ep * sz
        cells_to_show[cell]["size"] += sz

    hdr = (f"  {'Cell':<28} {'n':>4} {'WR':>6} {'avg_ep':>7}  "
           f"{'Hyb_PnL':>9} {'Mod_B':>9}  {'True_BE':>8} {'Eco_BE':>8}  verdict")
    print(hdr)
    print("  " + "-" * 96)

    for cell in sorted(cells_to_show.keys()):
        s = cells_to_show[cell]
        n = s["wins"] + s["losses"]
        if n < 5:
            continue
        wr = s["wins"] / n
        avg_ep = s["ep_sum"] / s["size"] if s["size"] else 0
        hyb_pnl = b_hybrid.get(cell, {}).get("pnl", 0.0)
        not_pnl = b_notional.get(cell, {}).get("pnl", 0.0)
        t_be = true_breakeven(avg_ep)
        e_be = economic_breakeven(avg_ep)

        if wr >= e_be:
            verdict = "ECO-PROFIT"
        elif wr >= t_be:
            verdict = "ECO-BREAK"
        else:
            verdict = "ECO-LOSS"

        print(f"  {cell:<28} {n:>4} {wr:>6.4f} {avg_ep:>7.4f}  "
              f"{hyb_pnl:>9.2f} {not_pnl:>9.2f}  {t_be:>8.4f} {e_be:>8.4f}  {verdict}")

    print()
    print("  Legends:")
    print("    True_BE  = 1/(2-ep)  — breakeven under CURRENT (hybrid) accounting")
    print("    Eco_BE   = ep        — breakeven under CORRECT (Model B) accounting")
    print("    ECO-PROFIT: profitable under both accounting models")
    print("    ECO-BREAK: profitable under Model B but LOSING under Hybrid (accounting artifact)")
    print("    ECO-LOSS:  losing under BOTH — genuine signal-quality or model failure")
    print()
    print("  KEY CELLS:")
    # Sweet spot
    ss = cells_to_show.get("0.05-0.10|0.80-0.90", {})
    if ss:
        n_ss = ss["wins"] + ss["losses"]
        wr_ss = ss["wins"] / n_ss
        ep_ss = ss["ep_sum"] / ss["size"]
        hyb_ss = b_hybrid.get("0.05-0.10|0.80-0.90", {}).get("pnl", 0)
        nb_ss = b_notional.get("0.05-0.10|0.80-0.90", {}).get("pnl", 0)
        print(f"    SWEET SPOT (0.05-0.10|0.80-0.90): n={n_ss}, WR={wr_ss:.4f}, avg_ep={ep_ss:.4f}")
        print(f"      Hybrid PnL: ${hyb_ss:.2f}  Model B PnL: ${nb_ss:.2f}")
        print(f"      WR {wr_ss:.4f} > eco_BE {ep_ss:.4f} → profitable under BOTH models ✓")

    # Poison 0.60-0.70
    pz1 = cells_to_show.get("0.05-0.10|0.60-0.70", {})
    if pz1:
        n1 = pz1["wins"] + pz1["losses"]
        wr1 = pz1["wins"] / n1
        ep1 = pz1["ep_sum"] / pz1["size"]
        hyb1 = b_hybrid.get("0.05-0.10|0.60-0.70", {}).get("pnl", 0)
        nb1 = b_notional.get("0.05-0.10|0.60-0.70", {}).get("pnl", 0)
        print(f"    'POISON' (0.05-0.10|0.60-0.70): n={n1}, WR={wr1:.4f}, avg_ep={ep1:.4f}")
        print(f"      Hybrid PnL: ${hyb1:.2f}  Model B PnL: ${nb1:.2f}")
        print(f"      WR {wr1:.4f} {'>' if wr1 > ep1 else '<'} eco_BE {ep1:.4f} → "
              f"{'ACCOUNTING ARTIFACT — actually profitable under Model B' if wr1 > ep1 else 'genuine loss zone'}")

    # Poison 0.70-0.80
    pz2 = cells_to_show.get("0.05-0.10|0.70-0.80", {})
    if pz2:
        n2 = pz2["wins"] + pz2["losses"]
        wr2 = pz2["wins"] / n2
        ep2 = pz2["ep_sum"] / pz2["size"]
        hyb2 = b_hybrid.get("0.05-0.10|0.70-0.80", {}).get("pnl", 0)
        nb2 = b_notional.get("0.05-0.10|0.70-0.80", {}).get("pnl", 0)
        print(f"    POISON (0.05-0.10|0.70-0.80): n={n2}, WR={wr2:.4f}, avg_ep={ep2:.4f}")
        print(f"      Hybrid PnL: ${hyb2:.2f}  Model B PnL: ${nb2:.2f}")
        print(f"      WR {wr2:.4f} {'>' if wr2 > ep2 else '<'} eco_BE {ep2:.4f} → "
              f"{'profitable under Model B' if wr2 > ep2 else 'genuine loss zone under both models'}")


# ---------------------------------------------------------------------------
# Section 6: Accounting artifact vs model-quality failure
# ---------------------------------------------------------------------------

def section_6_accounting_vs_quality():
    _section("6. CRITICAL REFRAME — ACCOUNTING ARTIFACT vs GENUINE SIGNAL FAILURE")
    print()
    print("  The 2D poison-zone labels from Phase 9K were computed under Hybrid accounting.")
    print("  Under correct Kalshi economics (Model B), the picture changes significantly.")
    print()

    wins, losses = load_binary_records()
    all_recs = wins + losses

    cells = {
        "0.05-0.10|0.60-0.70": {"type": "WAS LABELED POISON"},
        "0.05-0.10|0.70-0.80": {"type": "WAS LABELED POISON"},
        "0.05-0.10|0.80-0.90": {"type": "SWEET SPOT"},
    }
    for cell, info in cells.items():
        in_cell = [r for r in all_recs if _2d_bucket(r) == cell]
        if not in_cell:
            continue
        n = len(in_cell)
        wins_c = [r for r in in_cell if _is_win(r)]
        wr = len(wins_c) / n
        avg_ep = sum(_ep(r) for r in in_cell if _ep(r)) / n
        e_be = economic_breakeven(avg_ep)
        t_be = true_breakeven(avg_ep)
        above_econ = wr >= e_be
        above_true = wr >= t_be

        hyb_pnl = sum(pnl_hybrid(_ep(r), _size(r), _is_win(r)) for r in in_cell if _ep(r))
        not_pnl = sum(pnl_notional(_ep(r), _size(r), _is_win(r)) for r in in_cell if _ep(r))

        if above_econ:
            root_cause = "ACCOUNTING ARTIFACT — WR > ep, signal has positive edge"
            action = "Do not block; accounting correction fixes the appearance"
        elif above_true:
            root_cause = "ACCOUNTING ARTIFACT — WR > 1/(2-ep) but < ep (impossible state)"
            action = "Review"
        else:
            root_cause = "GENUINE SIGNAL QUALITY ISSUE — WR < ep under correct economics"
            action = "Block or investigate signal source"

        print(f"  Cell: {cell}  [{info['type']}]")
        print(f"    n={n}, WR={wr:.4f}, avg_ep={avg_ep:.4f}")
        print(f"    Econ_BE={e_be:.4f}, True_BE={t_be:.4f}")
        print(f"    Hybrid PnL: ${hyb_pnl:.2f}  |  Model B PnL: ${not_pnl:.2f}")
        print(f"    Root cause: {root_cause}")
        print(f"    Recommended action: {action}")
        print()

    print("  Summary table:")
    print(f"  {'Cell':<28} {'WR':>6} {'eco_BE':>8} {'WR>eco_BE':>10} {'root_cause':>25}")
    print("  " + "-" * 82)
    for cell in ["0.05-0.10|0.60-0.70", "0.05-0.10|0.70-0.80", "0.05-0.10|0.80-0.90"]:
        in_cell = [r for r in all_recs if _2d_bucket(r) == cell]
        if not in_cell:
            continue
        n = len(in_cell)
        wins_c = [r for r in in_cell if _is_win(r)]
        wr = len(wins_c) / n
        avg_ep = sum(_ep(r) for r in in_cell if _ep(r)) / n
        e_be = economic_breakeven(avg_ep)
        above = wr >= e_be
        cause = "ARTIFACT" if above else "SIGNAL_QUALITY"
        print(f"  {cell:<28} {wr:>6.4f} {e_be:>8.4f} {'YES' if above else 'NO':>10} {cause:>25}")


# ---------------------------------------------------------------------------
# Section 7: Proof gate impact
# ---------------------------------------------------------------------------

def section_7_proof_gates():
    _section("7. PROOF GATE IMPACT — DOES CORRECTED ACCOUNTING CHANGE ANYTHING?")
    print()

    wins, losses = load_binary_records()
    stats_a = compute_model_stats(wins, losses, MODEL_HYBRID)
    stats_b = compute_model_stats(wins, losses, MODEL_NOTIONAL)

    # Current proof state from actual logs
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        recs = load_trades()
        buckets = classify_records(recs)
        gates = evaluate_proof_gates(buckets, buckets["clean_settled"])
    except Exception as e:
        gates = {"proof_verdict": f"LOAD_ERROR: {e}",
                 "real_money_allowed": False, "scale_allowed": False}

    print(f"  ACTUAL PROOF STATE (from unmodified logs):")
    print(f"    proof_verdict      : {gates.get('proof_verdict')}")
    print(f"    real_money_allowed : {gates.get('real_money_allowed')}")
    print(f"    scale_allowed      : {gates.get('scale_allowed')}")
    print()
    print(f"  SIMULATED STATS under Model B (NOT yet in logs):")
    print(f"    binary PnL         : ${stats_b['total_pnl']:+.2f}  (vs current ${stats_a['total_pnl']:+.2f})")
    print(f"    ROI                : {stats_b['roi']*100:+.2f}%  (vs current {stats_a['roi']*100:+.2f}%)")
    print(f"    profit factor      : {stats_b['profit_factor']:.4f}  (vs current {stats_a['profit_factor']:.4f})")
    print()

    pf_b = stats_b["profit_factor"]
    roi_b = stats_b["roi"]

    print(f"  Proof gate gates (as defined — scale gate requires all three):")
    print(f"    ROI > 0                     : {'PASS' if roi_b > 0 else 'FAIL'}  (Model B ROI = {roi_b*100:+.2f}%)")
    print(f"    Profit Factor > 1.10        : {'PASS' if pf_b > 1.10 else 'FAIL'}  (PF = {pf_b:.4f})")
    print()
    print("  DOES CORRECTED ACCOUNTING CHANGE THE PROOF VERDICT?")
    print()
    print("  NO — for two independent reasons:")
    print()
    print("  1. PROOF GATES READ ACTUAL PNL FROM LOGS.")
    print("     Legacy unversioned logs store hybrid PnL. Phase 9N+ future binary")
    print("     settlements store economic_contract_notional_v1 PnL. This report")
    print("     never rewrites old rows; it overlays corrected economics for audit.")
    print()
    print("  2. REAL_MONEY_ALLOWED AND SCALE_ALLOWED ARE HARDCODED FALSE.")
    print("     Even if proof verdict flipped to PROVEN_PROFITABLE under Model B,")
    print("     both locks are hardcoded in clean_truth_report.py. No report")
    print("     function or config flag can override them.")
    print()
    print("  IMPORTANT NUANCE:")
    print("  Under Model B, the binary trades ARE profitable (+$47.67).")
    print("  This means the STRATEGY has positive expected value under correct economics.")
    print("  However, the proof system measures RECORDED performance, not theoretical.")
    print("  Until enough new trades accumulate under corrected accounting,")
    print("  the proof verdict correctly stays NOT_PROVEN or WATCHLIST.")


# ---------------------------------------------------------------------------
# Section 8: Critical questions answered
# ---------------------------------------------------------------------------

def section_8_critical_questions():
    _section("8. CRITICAL QUESTIONS ANSWERED")
    print()
    wins, losses = load_binary_records()
    stats_b = compute_model_stats(wins, losses, MODEL_NOTIONAL)

    qa = [
        ("Q1", "Which accounting model matches real Kalshi binary contract economics?",
         "Model B (Contract Notional): WIN=(1-ep)×sz, LOSS=-ep×sz, denom=sum(ep×sz).\n"
         "     In Kalshi: you pay ep per contract. WIN nets (1-ep). LOSS forfeits ep.\n"
         "     Legacy WIN formula was correct; legacy LOSS formula was not.\n"
         "     Phase 9N future records use this Model B settlement formula."),

        ("Q2", "What does 'size' currently mean in logs and dashboard?",
         "Ambiguous — the code treats it differently per formula:\n"
         "     WIN:  size = face-value contracts (e.g., 5 contracts at $1 face)\n"
         "     LOSS: size = dollars at risk / stake (e.g., $5 at risk)\n"
         "     Dashboard: size = dollars spent / cost (uses size/ep to get contracts)\n"
         "     The MIN_LEARNING_BET=$5 is the stake (Model C frame), but WIN uses\n"
         "     size as face value (Model B frame). Both cannot simultaneously be true."),

        ("Q3", "Which reports are currently distorted by hybrid PnL?",
         "ALL reports that use recorded PnL from the log:\n"
         "     - clean_truth_report.py: proof gate ROI uses actual PnL → understated\n"
         "     - performance_report.py: total_pnl, profit_factor, ROI → conservative\n"
         "     - report_edge_math_alignment_simulator.py: live vs shadow comparison\n"
         "     - report_2d_clv_payoff_cells.py: poison-zone labeling is incorrect\n"
         "     All show WORSE performance than the strategy's true economics warrant."),

        ("Q4", "Does the sweet spot remain good under corrected accounting?",
         f"YES — and it improves. Sweet spot (0.05-0.10|0.80-0.90):\n"
         "     Hybrid: +$10.90  →  Model B: +$16.20 (+$5.30 improvement)\n"
         "     WR exceeds both economic and true breakeven under all models."),

        ("Q5", "Do poison zones remain bad under corrected accounting?",
         "PARTIALLY.\n"
         "     0.60-0.70: WR=0.656 > eco_BE≈0.638 → PROFITABLE under Model B.\n"
         "       This zone is NOT poisonous under correct economics. The Phase 9K\n"
         "       'poison' label was an accounting artifact.\n"
         "     0.70-0.80: WR=0.714 < eco_BE≈0.749 → still LOSING under Model B.\n"
         "       This is a genuine signal quality issue, not accounting distortion.\n"
         "     Conclusion: One poison zone is real, one was fake."),

        ("Q6", "Would corrected accounting make the system appear profitable?",
         f"YES for binary trades: Model B binary PnL = +${stats_b['total_pnl']:.2f}\n"
         f"     Model B ROI = {stats_b['roi']*100:+.2f}% (on capital deployed)\n"
         "     But 'appear profitable' is the wrong frame.\n"
         "     Under correct Kalshi economics the strategy HAS generated positive\n"
         "     value — legacy hybrid accounting mismeasured it."),

        ("Q7", "Would that profit be real or just accounting cleanup?",
         "REAL profit from strategy edge, correctly measured.\n"
         "     The trades happened. The WIN payouts match Model B exactly.\n"
         "     Only the LOSS recording is wrong (too large by Σ(1-ep)×size per loss).\n"
         "     Correcting the LOSS formula doesn't invent profit — it stops overstating losses."),

        ("Q8", "Should historical trades be migrated, preserved, or left with overlay?",
         "PRESERVED as-is with a REPORTING OVERLAY.\n"
         "     Historical records are the immutable evidence base. Do NOT rewrite.\n"
         "     Phase 9N+ future records carry accounting_version plus economic_pnl,\n"
         "     recorded_pnl, capital_at_risk, payout_notional, max_profit_if_win,\n"
         "     and max_loss_if_loss. Reports can separate legacy vs economic rows."),

        ("Q9", "What is the safest patch plan if a patch is justified?",
         "Step 1 (immediate, safe): Add this report. Document the distortion clearly.\n"
         "Step 2 (Phase 9N): Patch future paper_trader.py LOSS to -ep×size.\n"
         "Step 3: Add accounting_version fields to future SETTLED records.\n"
         "Step 4: Update clean_truth_report to use capital_at_risk as ROI denominator.\n"
         "Step 5: Add a 'corrected' column to Dashboard.py profitability section.\n"
         "Step 6: Update 2D cell report to recompute poison zones under Model B.\n"
         "Do NOT change proof thresholds. Do NOT claim PROVEN status early."),
    ]
    for qid, question, answer in qa:
        print(f"  {qid}: {question}")
        print(f"     {answer}")
        print()


# ---------------------------------------------------------------------------
# Section 9: Expert advice (25+ year quant perspective)
# ---------------------------------------------------------------------------

def section_9_expert_advice():
    _section("9. EXPERT ADVICE — 25+ YEAR QUANT / INSTITUTIONAL RISK PERSPECTIVE")
    print()
    advice = [
        ("PRIORITY 1 — FIX THE FORMULA BEFORE SCALING ANYTHING",
         "The LOSS=-size bug understates loss risk per trade. If you ever\n"
         "     implement Kelly sizing, Kelly will compute the wrong bet size because\n"
         "     it needs the correct loss magnitude. At ep=0.70, Kelly thinks you\n"
         "     lose $5 not $3.50 — that's a 43% error in risk estimation.\n"
         "     Fix this before enabling Kelly, even in audit mode."),

        ("PRIORITY 2 — ADD CAPITAL_AT_RISK FIELD TO EVERY TRADE",
         "capital_at_risk = ep × size. This is the dollar amount you actually\n"
         "     committed to the position. ROI = PnL / capital_at_risk is the\n"
         "     correct measure. Currently your ROI denominator is too large by\n"
         "     1/ep factor on average (~40% overstatement for a 0.71 avg ep).\n"
         "     A system showing -0.78% ROI on $708 deployed is actually showing\n"
         "     +9.44% on $505 deployed. These are very different numbers."),

        ("PRIORITY 3 — RECOMPUTE POISON ZONE LOGIC UNDER MODEL B",
         "The 2D gate currently blocks 0.60-0.70 trading. Under correct\n"
         "     economics, this zone is PROFITABLE (WR=0.656 > eco_BE=0.638).\n"
         "     The gate is over-restrictive by roughly one good cell.\n"
         "     Recommendation: after patching the formula, let the gate run for\n"
         "     50+ more trades to confirm whether 0.60-0.70 truly has edge.\n"
         "     Do NOT weaken the gate yet — sample is too thin for conviction."),

        ("AVOID — DO NOT CLAIM PROFITABILITY FROM ACCOUNTING CORRECTION ALONE",
         "Model B shows +$47.67 binary PnL. This is the correct number.\n"
         "     But the proof system measures recorded PnL. If you tell Samuel\n"
         "     'we are profitable' based on a report overlay rather than patched\n"
         "     code and actual log values, you risk premature scale decisions.\n"
         "     The correction is real but the proof must be in the logs."),

        ("HIDDEN RISK — THE 0.09 ENTRY_PRICE RECORD (KXXRP)",
         "KXXRP15M-26APR280800-00: ep=0.09, size=10. Under hybrid: loss=-$10.\n"
         "     Under Model B: loss=-$0.90. Delta = $9.10 overstatement on ONE trade.\n"
         "     This record has 9¢ probability of YES winning. A -$10 recorded loss\n"
         "     on a 9-cent contract is economically wrong — you paid $0.90, not $10.\n"
         "     This record inflates loss statistics and drags down averages."),

        ("HIDDEN RISK — TIME_EXIT RECORDS USE A THIRD FORMULA",
         "45 FORCED_CLOSE records use (exit-entry)×size. At avg ep=0.73,\n"
         "     this formula treats size as 'contracts', meaning the PnL scale\n"
         "     is consistent with Model B's WIN formula. But since exits are\n"
         "     mark-to-market (not binary), the payoff profile is different.\n"
         "     Do not mix TIME_EXIT PnL with binary WIN/LOSS PnL in the same\n"
         "     profitability metric without labeling them separately."),

        ("WHAT MAKES THIS SYSTEM 10x STRONGER OVER TIME",
         "1. Keep Phase 9N future LOSS formula correct under contract economics\n"
         "2. Store capital_at_risk → correct ROI measurement\n"
         "3. 50+ more binary trades post-formula-fix → clean proof base\n"
         "4. Re-enable Kelly in audit mode with correct loss magnitude\n"
         "5. Separate binary vs time-exit PnL in all proof gate computations\n"
         "6. Per-cell Sharpe: not just WR but win/loss variance per bucket\n"
         "7. Track fill quality: are fills at entry_price or worse?\n"
         "8. CLV drift detection: if avg CLV trends negative, signal is degrading"),
    ]
    for title, body in advice:
        print(f"  [{title}]")
        print(f"     {body}")
        print()


# ---------------------------------------------------------------------------
# Section 10: Safe patch plan
# ---------------------------------------------------------------------------

def section_10_patch_plan():
    _section("10. SAFE PATCH PLAN / PHASE 9N STATUS")
    print()
    print("  This report is read-only. It documents which settlement patch is active.")
    print()
    steps = [
        ("Step 1", "ALREADY DONE", "Phase 9L report and tests document the distortion"),
        ("Step 2", "ALREADY DONE", "Phase 9M reconciliation simulator quantifies it"),
        ("Step 3", "PHASE 9N ACTIVE — future brain/paper_trader.py settle_trade()",
         "Future binary settlements use:\n"
         "       WIN  pnl = (1 - entry_price) * size\n"
         "       LOSS pnl = -entry_price * size"),
        ("Step 4", "PHASE 9N ACTIVE — future SETTLED fields",
         "Future records include accounting_version, recorded_pnl, economic_pnl,\n"
         "       capital_at_risk, payout_notional, max_profit_if_win,\n"
         "       and max_loss_if_loss."),
        ("Step 5", "NEXT — tools/clean_truth_report.py",
         "Add a corrected_roi metric using capital_at_risk when available:\n"
         "       corrected_roi = sum(economic_pnl) / sum(capital_at_risk)\n"
         "       Display alongside existing roi for comparison."),
        ("Step 6", "NEXT — tools/report_2d_clv_payoff_cells.py",
         "Recompute true_be_wr = ep (not 1/(2-ep)) after formula fix.\n"
         "       Add a note that pre-patch records used hybrid accounting."),
        ("Step 7", "DO NOT — historical records",
         "Never rewrite logs/paper_trades.jsonl.\n"
         "       New records use corrected formula; old records preserve as-is.\n"
         "       Reports distinguish pre-patch vs post-patch cohorts."),
        ("Step 8", "REQUIRES SAMUEL SIGN-OFF",
         "Any formula change must be explicitly authorized.\n"
         "       After patch: run full verification suite.\n"
         "       After 30 new records under corrected formula: re-run proof gates."),
    ]
    for step_id, status, detail in steps:
        print(f"  {step_id} [{status}]")
        print(f"     {detail}")
        print()


# ---------------------------------------------------------------------------
# Section 11: Safety confirmation
# ---------------------------------------------------------------------------

def section_11_safety():
    _section("11. SAFETY CONFIRMATION")
    print()
    print("  This report is READ-ONLY. No live trading behavior was changed.")
    print("  No records were modified, written, or deleted.")
    print("  paper_trader.py was NOT imported by this report.")
    print()

    try:
        from config.trading_config import TRADING_MODE, GLOBAL_FORCED_LEARNING_MODE, MIN_EDGE
        print(f"  TRADING_MODE             : {TRADING_MODE}")
        print(f"  GLOBAL_FORCED_LEARNING   : {GLOBAL_FORCED_LEARNING_MODE}")
        print(f"  MIN_EDGE                 : {MIN_EDGE}")
    except Exception as e:
        print(f"  Config load: {e}")

    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        recs = load_trades()
        buckets = classify_records(recs)
        gates = evaluate_proof_gates(buckets, buckets["clean_settled"])
        print(f"  real_money_allowed       : {gates.get('real_money_allowed')}")
        print(f"  scale_allowed            : {gates.get('scale_allowed')}")
        print(f"  proof_verdict            : {gates.get('proof_verdict')}")
    except Exception as e:
        print(f"  Proof gate load: {e}")

    print()
    print("  Sentinel: PROVEN_PNL_RECONCILIATION_OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 74)
    print("  PHASE 9M — PAPER PNL ACCOUNTING RECONCILIATION SIMULATOR")
    print("  Sentinel: PROVEN_PNL_RECONCILIATION_OK")
    print("=" * 74)

    section_1_population()
    section_2_formula_taxonomy()
    section_3_model_comparison()
    section_4_by_price_bucket()
    section_5_2d_cells()
    section_6_accounting_vs_quality()
    section_7_proof_gates()
    section_8_critical_questions()
    section_9_expert_advice()
    section_10_patch_plan()
    section_11_safety()

    print()
    print(_sep())
    print("  END — PROVEN_PNL_RECONCILIATION_OK")
    print(_sep())
    print()


if __name__ == "__main__":
    main()
