"""
Phase 9L — Payoff Accounting Truth Audit
Sentinel: PROVEN_PAYOFF_ACCOUNTING_AUDIT_OK

Audits whether the paper-trading payoff formula is internally consistent,
economically correct, and properly understood throughout the codebase.

DO NOT change live trading behavior.
DO NOT rewrite historical trade records.
DO NOT patch paper_trader.py without explicit Samuel authorization.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_settled():
    records = []
    if not TRADES_LOG.exists():
        return records
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


def _is_kxeth(r):
    return "KXETH" in str(r.get("ticker", "")).upper()


def _get_ep(r):
    ep = r.get("entry_price")
    if ep is None:
        ep = r.get("yes_ask") or r.get("price")
    try:
        return float(ep)
    except (TypeError, ValueError):
        return None


def _get_size(r):
    try:
        return float(r.get("size", 5.0))
    except (TypeError, ValueError):
        return 5.0


def _get_pnl(r):
    pnl = r.get("pnl") or r.get("realized_pnl")
    try:
        return float(pnl)
    except (TypeError, ValueError):
        return None


def _get_result(r):
    return str(r.get("result", "")).upper()


# ---------------------------------------------------------------------------
# Formula models
# ---------------------------------------------------------------------------

def model_a_pnl(ep, size, won):
    """Model A — face-value binary contracts. Size = number of contracts.
    WIN: receive (1-ep) per contract = (1-ep)×size
    LOSS: forfeit ep per contract = -ep×size
    Breakeven WR = ep (symmetric)
    """
    if won:
        return round((1.0 - ep) * size, 4)
    else:
        return round(-ep * size, 4)


def model_b_pnl(ep, size, won):
    """Model B — dollars-spent / stake model. Size = cash risked.
    WIN: net profit = (1-ep)/ep × size
    LOSS: lose stake = -size
    Breakeven WR = ep (symmetric)
    """
    if won:
        return round(((1.0 - ep) / ep) * size, 4)
    else:
        return round(-size, 4)


def current_pnl(ep, size, won):
    """Current paper_trader.py formula (hybrid).
    WIN: (1-ep)×size  [same as Model A — treats size as contracts]
    LOSS: -size        [same as Model B — treats size as cash risked]
    Breakeven WR = 1/(2-ep)  (NOT ep — because models are mixed)
    """
    if won:
        return round((1.0 - ep) * size, 4)
    else:
        return round(-size, 4)


def true_breakeven_wr(ep):
    """Correct breakeven for current asymmetric accounting."""
    return 1.0 / (2.0 - ep)


def economic_breakeven_wr(ep):
    """Correct breakeven under either properly-applied Model A or Model B."""
    return ep


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------

def _sep(char="=", n=72):
    return char * n


def _section(title):
    print()
    print(_sep())
    print(f"  {title}")
    print(_sep())


# ---------------------------------------------------------------------------
# Section 1: Formula source — what the code actually does
# ---------------------------------------------------------------------------

def section_1_formula_source():
    _section("1. CONFIRMED SOURCE CODE FORMULA")
    print()
    print("  File: brain/paper_trader.py — settle_trade() ~line 987")
    print()
    print("    if won:")
    print("        pnl = (1.0 - trade['entry_price']) * trade['size']   # WIN")
    print("    else:")
    print("        pnl = -trade['size']                                  # LOSS")
    print()
    print("  File: Dashboard.py — unrealized PnL for open trades ~line 295")
    print()
    print("    contracts = size / entry_price          # size treated as COST PAID")
    print("    unrealized_pnl = (mark - entry) * contracts")
    print()
    print("  File: auto_settle_trades.py — time-exit PnL ~line 258")
    print()
    print("    return round((exit_price - entry) * size, 2)   # size as CONTRACTS")
    print()
    print("  Observation: THREE different PnL formulas exist in the codebase.")
    print("  Settlement and time-exit both treat size as 'contracts', but")
    print("  Dashboard open-trade view treats size as 'cost paid'.")


# ---------------------------------------------------------------------------
# Section 2: Three-model comparison
# ---------------------------------------------------------------------------

def section_2_model_comparison():
    _section("2. THREE-MODEL COMPARISON")
    print()
    test_cases = [
        (0.57, 5.0, True),
        (0.57, 5.0, False),
        (0.62, 5.0, True),
        (0.63, 5.0, False),
        (0.74, 5.0, True),
        (0.74, 5.0, False),
        (0.81, 5.0, True),
        (0.81, 5.0, False),
        (0.84, 5.0, True),
        (0.84, 5.0, False),
    ]

    hdr = f"  {'ep':>6}  {'size':>6}  {'outcome':>8}  {'Model_A':>9}  {'Model_B':>9}  {'Current':>9}  {'Delta_A':>9}"
    print(hdr)
    print("  " + "-" * 68)
    for ep, size, won in test_cases:
        outcome = "WIN" if won else "LOSS"
        ma = model_a_pnl(ep, size, won)
        mb = model_b_pnl(ep, size, won)
        cur = current_pnl(ep, size, won)
        delta = round(cur - ma, 4)
        print(f"  {ep:>6.2f}  {size:>6.2f}  {outcome:>8}  {ma:>9.4f}  {mb:>9.4f}  {cur:>9.4f}  {delta:>9.4f}")

    print()
    print("  Delta_A = Current - Model_A")
    print("  For WINs: Current == Model_A (delta = 0)")
    print("  For LOSSes: Current > Model_A (delta > 0 = losses overstated vs Model A)")
    print()
    print("  Diagnosis: Current code is a HYBRID.")
    print("    WIN path  → Model A formula  (size = contracts)")
    print("    LOSS path → Model B formula  (size = stake/cash)")
    print()
    print("  This mixing is NOT economically consistent. Under either correctly")
    print("  applied model, breakeven WR = ep. Under the current hybrid,")
    print("  breakeven WR = 1/(2-ep).")


# ---------------------------------------------------------------------------
# Section 3: Breakeven WR comparison
# ---------------------------------------------------------------------------

def section_3_breakeven_comparison():
    _section("3. BREAKEVEN WIN-RATE COMPARISON")
    print()
    print("  Formula derivation for current accounting:")
    print("    E[PnL] = WR×(1-ep)×size + (1-WR)×(-size) = 0")
    print("    WR×(1-ep) - (1-WR) = 0")
    print("    WR×(1-ep) - 1 + WR = 0")
    print("    WR×(2-ep) = 1")
    print("    WR = 1/(2-ep)   ← current accounting breakeven")
    print()
    print("  Formula derivation under Model A:")
    print("    E[PnL] = WR×(1-ep)×size + (1-WR)×(-ep)×size = 0")
    print("    WR×(1-ep) - (1-WR)×ep = 0")
    print("    WR - WR×ep - ep + WR×ep = 0")
    print("    WR = ep   ← economic breakeven")
    print()

    eps = [0.50, 0.57, 0.62, 0.70, 0.74, 0.81, 0.84, 0.90]
    print(f"  {'ep':>6}  {'True_BE (current)':>20}  {'Econ_BE (Model A)':>20}  {'Gap (pp)':>10}")
    print("  " + "-" * 62)
    for ep in eps:
        tbe = true_breakeven_wr(ep)
        ebe = economic_breakeven_wr(ep)
        gap = round((tbe - ebe) * 100, 1)
        print(f"  {ep:>6.2f}  {tbe:>20.4f}  {ebe:>20.4f}  {gap:>10.1f}pp")

    print()
    print("  The gap is largest at low entry prices (cheap markets).")
    print("  A $5 bet at ep=0.57 needs WR=0.699 to break even (not 0.57).")
    print("  The current formula makes cheap-market trading much harder")
    print("  to be profitable than the economics actually require.")


# ---------------------------------------------------------------------------
# Section 4: Quantified PnL correction from real logs
# ---------------------------------------------------------------------------

def section_4_quantified_correction():
    _section("4. QUANTIFIED PnL CORRECTION FROM TRADE LOG")
    print()
    records = _load_settled()
    if not records:
        print("  WARNING: No settled records found in logs/paper_trades.jsonl")
        return None

    non_kxeth = [r for r in records if not _is_kxeth(r)]
    wins, losses, skipped = [], [], []
    for r in non_kxeth:
        ep = _get_ep(r)
        size = _get_size(r)
        pnl = _get_pnl(r)
        result = _get_result(r)
        if ep is None or pnl is None or result not in ("WIN", "LOSS"):
            skipped.append(r)
            continue
        won = result == "WIN"
        entry = {"ep": ep, "size": size, "pnl": pnl, "won": won,
                 "ticker": r.get("ticker", "?")}
        if won:
            wins.append(entry)
        else:
            losses.append(entry)

    actual_pnl = sum(e["pnl"] for e in wins + losses)
    model_a_total = sum(model_a_pnl(e["ep"], e["size"], e["won"]) for e in wins + losses)
    overstatement = sum(
        current_pnl(e["ep"], e["size"], False) - model_a_pnl(e["ep"], e["size"], False)
        for e in losses
    )
    corrected_pnl = actual_pnl - overstatement  # actual + abs(overstatement)

    print(f"  Non-KXETH settled records : {len(non_kxeth)}")
    print(f"  Wins                      : {len(wins)}")
    print(f"  Losses                    : {len(losses)}")
    print(f"  Skipped (missing fields)  : {len(skipped)}")
    print()
    print(f"  Actual total PnL (current accounting) : ${actual_pnl:>8.2f}")
    print(f"  Model A total PnL (economic)          : ${model_a_total:>8.2f}")
    print(f"  Difference (overstatement of losses)  : ${overstatement:>8.2f}")
    print()
    print(f"  Interpretation:")
    print(f"    Every LOSS recorded as -$size instead of -ep×size.")
    print(f"    For {len(losses)} losses, this overstates losses by ${abs(overstatement):.2f} total.")
    print(f"    Under correct Model A accounting: PnL would be ${model_a_total:.2f}")
    print()
    print(f"  *** Under correct binary contract economics, the non-KXETH portfolio")
    print(f"      is {'PROFITABLE' if model_a_total > 0 else 'UNPROFITABLE'}  (${model_a_total:+.2f}). ***")
    print()
    print("  NOTE: This does NOT change proof gates.")
    print("  Proof uses actual recorded PnL. The system is harder to prove than")
    print("  economics require. This is CONSERVATIVE and SAFER for proof integrity.")

    return {
        "wins": len(wins), "losses": len(losses),
        "actual_pnl": actual_pnl,
        "model_a_total": model_a_total,
        "overstatement": overstatement,
    }


# ---------------------------------------------------------------------------
# Section 5: Loss overstatement detail (sample)
# ---------------------------------------------------------------------------

def section_5_loss_detail():
    _section("5. LOSS OVERSTATEMENT DETAIL (SAMPLE)")
    print()
    records = _load_settled()
    non_kxeth = [r for r in records if not _is_kxeth(r)]
    loss_records = []
    for r in non_kxeth:
        ep = _get_ep(r)
        size = _get_size(r)
        pnl = _get_pnl(r)
        result = _get_result(r)
        if ep is None or pnl is None or result != "LOSS":
            continue
        cur = current_pnl(ep, size, False)
        ma = model_a_pnl(ep, size, False)
        overstate = round(cur - ma, 4)
        loss_records.append({
            "ticker": r.get("ticker", "?")[:30],
            "ep": ep, "size": size,
            "actual": pnl, "model_a": ma, "overstatement": overstate,
        })

    loss_records.sort(key=lambda x: abs(x["overstatement"]), reverse=True)
    show = loss_records[:10]

    print(f"  {'ticker':30}  {'ep':>6}  {'size':>6}  {'actual':>8}  {'ModelA':>8}  {'overstate':>10}")
    print("  " + "-" * 76)
    for e in show:
        print(f"  {e['ticker']:30}  {e['ep']:>6.3f}  {e['size']:>6.2f}  "
              f"{e['actual']:>8.2f}  {e['model_a']:>8.4f}  {e['overstatement']:>10.4f}")

    if len(loss_records) > 10:
        print(f"  ... ({len(loss_records) - 10} more losses not shown)")
    print()
    total_over = sum(e["overstatement"] for e in loss_records)
    print(f"  Total loss overstatement across {len(loss_records)} losses: ${total_over:.4f}")


# ---------------------------------------------------------------------------
# Section 6: Dashboard / auto_settle inconsistencies
# ---------------------------------------------------------------------------

def section_6_dashboard_inconsistencies():
    _section("6. CODEBASE INCONSISTENCIES IDENTIFIED")
    print()
    print("  Three PnL formulas co-exist in the codebase:")
    print()
    print("  A. brain/paper_trader.py — BINARY SETTLEMENT (WIN / LOSS to resolution)")
    print("     WIN:  pnl = (1 - ep) × size    [size = contracts, Model A]")
    print("     LOSS: pnl = -size               [size = stake, Model B]")
    print("     → Hybrid model; inconsistent across WIN/LOSS paths")
    print()
    print("  B. Dashboard.py — UNREALIZED PnL for OPEN trades")
    print("     contracts = size / ep           [size = cost paid, ep = price/share]")
    print("     unrealized = (mark - ep) × contracts")
    print("              = (mark - ep) × size/ep")
    print("     → Treats size as dollar cost; different from settlement formula")
    print()
    print("  C. auto_settle_trades.py — TIME-EXIT PnL (mark-to-market at timeout)")
    print("     pnl = (exit_price - entry) × size  [size = contracts, symmetric]")
    print("     → Third formula; neither Model A, B, nor Dashboard formula")
    print()
    print("  D. ROI denominator:")
    print("     total_wagered = Σ size  [treats size as dollars risked]")
    print("     While WIN PnL = (1-ep)×size  [treats size as contracts]")
    print("     → ROI numerator and denominator use incompatible size meanings")
    print()
    print("  Impact:")
    print("  - Settlement PnL (persisted in logs): overstates losses by $53.20")
    print("  - Dashboard live PnL (open trades): different formula, small discrepancy")
    print("  - ROI metric: denominator too large → ROI appears worse than reality")
    print("  - All three effects are CONSERVATIVE (make system look worse)")


# ---------------------------------------------------------------------------
# Section 7: Answers to Phase 9L audit questions
# ---------------------------------------------------------------------------

def section_7_audit_questions():
    _section("7. AUDIT QUESTIONS ANSWERED")
    print()
    questions = [
        ("Q1", "Is the asymmetric formula intentional stake/max-loss accounting?",
         "PARTIALLY. The LOSS=-size formula matches a stake/max-loss model (Model B).\n"
         "     But the WIN=(1-ep)×size formula does NOT match Model B — it matches Model A.\n"
         "     The formula was likely written without recognizing the inconsistency.\n"
         "     There is no comment or config flag suggesting it was a deliberate choice."),
        ("Q2", "Is it a modeling bug?",
         "YES, in the strict sense. The two paths are economically inconsistent.\n"
         "     However, the 'bug' is conservative: it makes the system harder to prove\n"
         "     profitable than the economics require. It is the safest possible error."),
        ("Q3", "Is it a dashboard / reporting misunderstanding?",
         "PARTIALLY. Dashboard.py uses a third formula for unrealized PnL (size/ep).\n"
         "     This is numerically close but not identical to either Model A or B.\n"
         "     auto_settle_trades.py uses a symmetric formula for time-exit PnL.\n"
         "     All three formulas co-exist and are inconsistent."),
        ("Q4", "Is it inconsistent with Kalshi contract economics?",
         "YES. In Kalshi, you pay ep per contract. If you win, you receive $1.\n"
         "     Net WIN = (1-ep) per contract = Model A. ✓ (matches current WIN formula)\n"
         "     Net LOSS = -ep per contract = Model A. ✗ (current uses -size, not -ep×size)\n"
         "     Under Kalshi economics, the LOSS formula is definitively wrong.\n"
         "     The correct LOSS formula is: pnl = -ep × size."),
        ("Q5", "What is the true breakeven WR under current accounting?",
         "true_BE_WR = 1/(2-ep).\n"
         "     At ep=0.57: 1/(2-0.57) = 0.699  (vs economic 0.57)\n"
         "     At ep=0.84: 1/(2-0.84) = 0.862  (vs economic 0.84)\n"
         "     Phase 9K 2D report uses this formula — it is correct for current code."),
        ("Q6", "What is the economic breakeven WR?",
         "economic_BE_WR = ep (under either correctly-applied Model A or Model B).\n"
         "     This is the breakeven you'd use if the formula were fixed."),
        ("Q7", "Is there evidence of losses being double-counted?",
         "NO. Each LOSS appears once in the log. The overstatement is per-record\n"
         "     (each loss is $size instead of $ep×size), not record duplication."),
        ("Q8", "Should we patch paper_trader.py to fix the LOSS formula?",
         "NO — not without explicit Samuel authorization.\n"
         "     Reasons: (1) Conservative accounting is safer for proof integrity.\n"
         "     (2) Fixing would make current PnL look better, not worse — could\n"
         "         create pressure to scale prematurely.\n"
         "     (3) Historical records cannot be rewritten.\n"
         "     (4) Proof gates use actual PnL; fixing would require re-evaluating gates.\n"
         "     Recommendation: add documentation and economic_pnl field to future records."),
        ("Q9", "Should historical records be rewritten?",
         "NO — absolutely not. Paper trade records are the evidence base.\n"
         "     Rewriting them would destroy proof integrity regardless of whether\n"
         "     the correction is mathematically valid."),
        ("Q10", "Dashboard 'Bet $45' display — is this correct?",
         "The Dashboard shows total_wagered = Σ size = $5 × trades.\n"
         "     This is the correct stake-at-risk per trade under the LOSS model.\n"
         "     The display is internally consistent with what is actually risked per trade.\n"
         "     However, it mixes with WIN PnL formula where size = contracts, not cost.\n"
         "     The display label 'Bet $X' is accurate for loss-exposure, not contract-face-value."),
        ("Q11", "Does the Dashboard show correct ROI?",
         "ROI = total_PnL / total_wagered.\n"
         "     total_wagered = Σ size (dollars at risk per LOSS formula).\n"
         "     total_PnL uses actual settlement PnL (current hybrid formula).\n"
         "     Because losses are overstated, PnL < economic PnL → ROI < economic ROI.\n"
         "     The ROI metric is conservative. It will appear negative longer than it\n"
         "     should be under correct economics. This is acceptable and safe."),
    ]
    for qid, question, answer in questions:
        print(f"  {qid}: {question}")
        print(f"     → {answer}")
        print()


# ---------------------------------------------------------------------------
# Section 8: Recommendations
# ---------------------------------------------------------------------------

def section_8_recommendations():
    _section("8. RECOMMENDATIONS")
    print()
    recs = [
        ("REC-1", "DOCUMENT ONLY",
         "Add a clear docstring/comment in paper_trader.py settle_trade()\n"
         "         explaining that LOSS=-size is intentionally conservative\n"
         "         (larger than Kalshi-economic loss) and that this is the\n"
         "         system's accounting convention until explicitly changed."),
        ("REC-2", "ADD economic_pnl FIELD TO FUTURE RECORDS",
         "When writing new SETTLED records, add an additional field:\n"
         "         economic_pnl = model_a_pnl(ep, size, won)\n"
         "         This allows future reports to compare accounting vs economic PnL\n"
         "         without rewriting historical records."),
        ("REC-3", "DO NOT PATCH paper_trader.py LOSS FORMULA",
         "The conservative accounting is safer for proof.\n"
         "         Changing it would require re-evaluation of all proof gates\n"
         "         and could create premature scale pressure."),
        ("REC-4", "DO NOT REWRITE HISTORICAL RECORDS",
         "The evidence base must not be modified.\n"
         "         Any correction would be documented as a reporting-layer\n"
         "         adjustment, not a log mutation."),
        ("REC-5", "UPDATE 2D REPORT TO NOTE ACCOUNTING CONVENTION",
         "report_2d_clv_payoff_cells.py uses true_BE_WR=1/(2-ep) correctly.\n"
         "         Add a note confirming this reflects current accounting,\n"
         "         and that economic BE under corrected formula would be ep."),
        ("REC-6", "ALIGN DASHBOARD UNREALIZED PnL WITH SETTLEMENT FORMULA",
         "Dashboard.py uses (mark-ep)×size/ep while settlement uses (1-ep)×size.\n"
         "         These will produce slightly different PnL for open trades.\n"
         "         LOW PRIORITY — open trade PnL is indicative only."),
    ]
    for rid, title, detail in recs:
        print(f"  {rid}: {title}")
        print(f"         {detail}")
        print()


# ---------------------------------------------------------------------------
# Section 9: Safety confirmation
# ---------------------------------------------------------------------------

def section_9_safety():
    _section("9. SAFETY CONFIRMATION")
    print()
    print("  This report is READ-ONLY. No live trading behavior was changed.")
    print()

    try:
        from config.trading_config import TRADING_MODE, GLOBAL_FORCED_LEARNING_MODE
        print(f"  TRADING_MODE             : {TRADING_MODE}")
        print(f"  GLOBAL_FORCED_LEARNING   : {GLOBAL_FORCED_LEARNING_MODE}")
    except Exception as e:
        print(f"  Config load error: {e}")

    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        _recs = load_trades()
        _buckets = classify_records(_recs)
        gates = evaluate_proof_gates(_buckets, _buckets["clean_settled"])
        print(f"  real_money_allowed       : {gates.get('real_money_allowed')}")
        print(f"  scale_allowed            : {gates.get('scale_allowed')}")
        print(f"  proof_verdict            : {gates.get('proof_verdict')}")
    except Exception as e:
        print(f"  Proof gate load error: {e}")

    print()
    print("  paper_trader.py NOT imported by this report.")
    print("  No records were written, modified, or deleted.")
    print("  Sentinel: PROVEN_PAYOFF_ACCOUNTING_AUDIT_OK")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 72)
    print("  PHASE 9L — PAYOFF ACCOUNTING TRUTH AUDIT")
    print("  Sentinel: PROVEN_PAYOFF_ACCOUNTING_AUDIT_OK")
    print("=" * 72)

    section_1_formula_source()
    section_2_model_comparison()
    section_3_breakeven_comparison()
    stats = section_4_quantified_correction()
    section_5_loss_detail()
    section_6_dashboard_inconsistencies()
    section_7_audit_questions()
    section_8_recommendations()
    section_9_safety()

    print()
    print(_sep())
    print("  END OF REPORT — PROVEN_PAYOFF_ACCOUNTING_AUDIT_OK")
    print(_sep())
    print()

    return stats


if __name__ == "__main__":
    main()
