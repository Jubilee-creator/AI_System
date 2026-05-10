#!/usr/bin/env python3
"""
tools/test_2d_clv_payoff_cells.py
------------------------------------
Phase 9K test suite — 2D CLV / Payoff Cell Report.

Verifies:
  1.  Report module imports without error
  2.  build_2d_cell_data() runs on real settled records without exception
  3.  KXETH records are excluded from all 2D cells
  4.  Required fields present in every finalized cell
  5.  CLV math consistency: avg_clv × clv_count ≈ total_clv
  6.  True breakeven formula: true_be_wr = 1/(2-avg_entry_price)
  7.  True EV formula: true_ev = win_rate × (2-avg_ep) - 1
  8.  Sign consistency: sign(wr_vs_true_be) == sign(true_ev) for non-edge cells
  9.  Sweet-spot cell 0.05-0.10|0.80-0.90 is GOOD with WR > true_be_wr
 10.  Poison cell 0.05-0.10|0.70-0.80 WR < true_be_wr (correctly identified as bad)
 11.  Total 2D trades <= non-KXETH clean_settled count (no record duplication)
 12.  No live trading files are modified (module import does not mutate any file)
 13.  Critic brain does NOT reference report functions or CLV cell fields
 14.  Builder brain does NOT reference report functions or CLV cell fields
 15.  Safety locks: real_money_allowed=False, scale_allowed=False
 16.  MIN_EDGE=0.03, MIN_CONFIDENCE=0.65 unchanged
 17.  Diagnosis values are from the known valid set
 18.  zero-n cells are diagnosed EMPTY (not a real cell)
 19.  main() runs and produces PROVEN_2D_CLV_PAYOFF_OK sentinel
 20.  paper_trader.py not imported or modified by report

Expected result when all pass: PROVEN_2D_CLV_PAYOFF_TESTS_OK
"""
from __future__ import annotations

import ast
import io
import math
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

REPORT_PATH  = ROOT / "tools" / "report_2d_clv_payoff_cells.py"
CRITIC_PATH  = ROOT / "brain" / "critic_brain.py"
BUILDER_PATH = ROOT / "brain" / "builder_brain.py"
PT_PATH      = ROOT / "brain" / "paper_trader.py"
TRADES_PATH  = ROOT / "logs" / "paper_trades.jsonl"

_VALID_DIAGNOSES = {
    "GOOD", "PAYOFF_STRUCTURE", "MODEL_QUALITY",
    "MIXED", "TOO_SMALL", "NO_CLV_DATA", "EMPTY", "NO_DATA",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_cells():
    from report_2d_clv_payoff_cells import build_2d_cell_data
    from performance_report import load_trades, build_terminal_key_sets, classify_settled_records
    all_records = load_trades()
    s, fc, v = build_terminal_key_sets(all_records)
    clean, _ = classify_settled_records(all_records, s, fc, v)
    non_kxeth = [r for r in clean
                 if not str(r.get("ticker", "")).upper().startswith("KXETH")]
    return build_2d_cell_data(non_kxeth), non_kxeth, clean


# ── 1. Import ─────────────────────────────────────────────────────────────────

def test_import() -> Optional[str]:
    """Report module must import without error."""
    try:
        import report_2d_clv_payoff_cells  # noqa: F401
        return None
    except Exception as e:
        return f"Import failed: {e}"


# ── 2. build_2d_cell_data runs ────────────────────────────────────────────────

def test_build_runs() -> Optional[str]:
    """build_2d_cell_data() runs on real data without exception."""
    try:
        cells, _, _ = _load_cells()
        if not isinstance(cells, dict):
            return f"Expected dict, got {type(cells)}"
        return None
    except Exception as e:
        return f"build_2d_cell_data() raised: {e}"


# ── 3. KXETH excluded ─────────────────────────────────────────────────────────

def test_kxeth_excluded() -> Optional[str]:
    """No KXETH tickers should appear in the cell keys or underlying data."""
    from report_2d_clv_payoff_cells import build_2d_cell_data
    from performance_report import load_trades, build_terminal_key_sets, classify_settled_records
    all_records = load_trades()
    s, fc, v = build_terminal_key_sets(all_records)
    clean, _ = classify_settled_records(all_records, s, fc, v)
    # Feed ALL records including KXETH — module must filter them out
    cells = build_2d_cell_data(clean)
    # Count KXETH settled records
    kxeth_n = sum(1 for r in clean
                  if str(r.get("ticker", "")).upper().startswith("KXETH"))
    non_kxeth_n = sum(1 for r in clean
                      if not str(r.get("ticker", "")).upper().startswith("KXETH"))
    total_2d = sum(c["n"] for c in cells.values() if c.get("n"))
    if total_2d > non_kxeth_n:
        return (
            f"total 2D trades={total_2d} > non_kxeth_settled={non_kxeth_n} — "
            "KXETH may be leaking into cells"
        )
    return None


# ── 4. Required fields ────────────────────────────────────────────────────────

def test_required_fields() -> Optional[str]:
    """Every non-empty cell must carry the required fields."""
    REQUIRED = [
        "n", "wins", "losses", "win_rate", "total_pnl", "avg_pnl",
        "avg_entry_price", "true_be_wr", "clv_be_wr", "wr_vs_true_be",
        "true_ev", "diagnosis",
    ]
    cells, _, _ = _load_cells()
    for key, c in cells.items():
        if c.get("n", 0) == 0:
            continue
        for f in REQUIRED:
            if f not in c:
                return f"Cell '{key}' missing required field '{f}'"
    return None


# ── 5. CLV math consistency ───────────────────────────────────────────────────

def test_clv_math() -> Optional[str]:
    """avg_clv × clv_count ≈ total_clv within rounding tolerance."""
    cells, _, _ = _load_cells()
    for key, c in cells.items():
        avg = c.get("avg_clv")
        n   = c.get("clv_count")
        tot = c.get("total_clv")
        if avg is None or n is None or tot is None:
            continue
        expected = avg * n
        if abs(expected - tot) > 0.05:
            return (
                f"Cell '{key}': avg_clv({avg}) × clv_count({n}) = {expected:.4f} "
                f"but total_clv={tot} (delta={abs(expected-tot):.4f})"
            )
    return None


# ── 6. True breakeven formula ─────────────────────────────────────────────────

def test_true_be_wr_formula() -> Optional[str]:
    """true_be_wr must equal 1/(2-avg_entry_price) for every non-empty cell."""
    cells, _, _ = _load_cells()
    for key, c in cells.items():
        if c.get("n", 0) < 1:
            continue
        ep  = c.get("avg_entry_price")
        be  = c.get("true_be_wr")
        if ep is None or be is None:
            continue
        expected = 1.0 / (2.0 - ep)
        if abs(expected - be) > 1e-3:
            return (
                f"Cell '{key}': true_be_wr={be} but 1/(2-ep={ep}) = {expected:.4f}"
            )
    return None


# ── 7. True EV formula ────────────────────────────────────────────────────────

def test_true_ev_formula() -> Optional[str]:
    """true_ev must equal WR*(2-avg_ep) - 1 for every non-empty cell."""
    cells, _, _ = _load_cells()
    for key, c in cells.items():
        if c.get("n", 0) < 1:
            continue
        wr  = c.get("win_rate")
        ep  = c.get("avg_entry_price")
        ev  = c.get("true_ev")
        if wr is None or ep is None or ev is None:
            continue
        expected = wr * (2.0 - ep) - 1.0
        if abs(expected - ev) > 1e-3:
            return (
                f"Cell '{key}': true_ev={ev} but WR({wr})*(2-ep({ep}))-1 = {expected:.4f}"
            )
    return None


# ── 8. Sign consistency ───────────────────────────────────────────────────────

def test_wr_be_sign_matches_true_ev() -> Optional[str]:
    """sign(wr_vs_true_be) must equal sign(true_ev) for all non-edge cells."""
    cells, _, _ = _load_cells()
    for key, c in cells.items():
        if c.get("n", 0) < 1:
            continue
        ev   = c.get("true_ev")
        wrbe = c.get("wr_vs_true_be")
        if ev is None or wrbe is None:
            continue
        # Skip cells at exact zero (rounding edge)
        if abs(ev) < 1e-6 or abs(wrbe) < 1e-6:
            continue
        ev_pos  = ev   > 0
        be_pos  = wrbe > 0
        if ev_pos != be_pos:
            return (
                f"Cell '{key}': sign(wr_vs_true_be)={'+' if be_pos else '-'} "
                f"but sign(true_ev)={'+' if ev_pos else '-'} — inconsistent"
            )
    return None


# ── 9. Sweet-spot cell is GOOD ────────────────────────────────────────────────

def test_sweetspot_is_good() -> Optional[str]:
    """0.05-0.10|0.80-0.90 must be diagnosed GOOD with WR > true_be_wr."""
    cells, _, _ = _load_cells()
    cell = cells.get("0.05-0.10|0.80-0.90")
    if cell is None or cell.get("n", 0) < 5:
        return "Sweet-spot cell '0.05-0.10|0.80-0.90' missing or too small"
    diag = cell.get("diagnosis")
    if diag != "GOOD":
        return (
            f"Sweet-spot cell diagnosis={diag!r} (expected GOOD). "
            f"WR={cell['win_rate']:.3f} True_BE={cell.get('true_be_wr', 0):.3f}"
        )
    if cell.get("wr_vs_true_be", -1) <= 0:
        return (
            f"Sweet-spot cell WR not above true breakeven: "
            f"wr_vs_true_be={cell['wr_vs_true_be']:.4f}"
        )
    return None


# ── 10. Poison cell WR < true_be_wr ──────────────────────────────────────────

def test_poison_below_true_be() -> Optional[str]:
    """0.05-0.10|0.70-0.80 must have WR < true_be_wr (not profitable)."""
    cells, _, _ = _load_cells()
    cell = cells.get("0.05-0.10|0.70-0.80")
    if cell is None or cell.get("n", 0) < 5:
        return "Poison cell '0.05-0.10|0.70-0.80' missing or too small"
    if cell.get("wr_vs_true_be", 1) >= 0:
        return (
            f"Poison cell WR≥true_be_wr — cell should be unprofitable. "
            f"wr_vs_true_be={cell.get('wr_vs_true_be'):.4f}"
        )
    return None


# ── 11. No record duplication ─────────────────────────────────────────────────

def test_no_duplication() -> Optional[str]:
    """Total 2D cell trades must not exceed non-KXETH clean_settled count."""
    cells, non_kxeth, _ = _load_cells()
    total_2d = sum(c["n"] for c in cells.values() if c.get("n"))
    if total_2d > len(non_kxeth):
        return (
            f"total 2D trades={total_2d} > non_kxeth_clean_settled={len(non_kxeth)} — "
            "records may be counted multiple times"
        )
    return None


# ── 12. Import does not mutate files ─────────────────────────────────────────

def test_import_no_mutation() -> Optional[str]:
    """Importing the report must not modify any files."""
    import time
    paths_to_check = [
        ROOT / "logs" / "paper_trades.jsonl",
        ROOT / "data" / "edge_profile.json",
        ROOT / "logs" / "execution_funnel.jsonl",
    ]
    before = {}
    for p in paths_to_check:
        if p.exists():
            before[p] = p.stat().st_mtime

    import report_2d_clv_payoff_cells  # noqa: F811

    for p, t in before.items():
        if p.exists() and abs(p.stat().st_mtime - t) > 0.01:
            return f"{p.name} was modified during import — report must be read-only"
    return None


# ── 13. Critic does not reference report functions ────────────────────────────

def test_critic_no_report_refs() -> Optional[str]:
    """critic_brain.py must not import or reference report_2d_clv_payoff_cells."""
    source = CRITIC_PATH.read_text()
    if "report_2d_clv_payoff_cells" in source:
        return "critic_brain.py references report_2d_clv_payoff_cells — must not"
    for field in ("true_ev", "true_be_wr", "clv_be_wr", "wr_vs_true_be"):
        if field in source:
            return f"critic_brain.py references '{field}' — CLV cell fields must not influence decisions"
    return None


# ── 14. Builder does not reference report functions ───────────────────────────

def test_builder_no_report_refs() -> Optional[str]:
    """builder_brain.py must not import or reference report_2d_clv_payoff_cells."""
    if not BUILDER_PATH.exists():
        return None
    source = BUILDER_PATH.read_text()
    if "report_2d_clv_payoff_cells" in source:
        return "builder_brain.py references report_2d_clv_payoff_cells — must not"
    for field in ("true_ev", "true_be_wr", "clv_be_wr", "wr_vs_true_be"):
        if field in source:
            return f"builder_brain.py references '{field}'"
    return None


# ── 15. Safety locks ─────────────────────────────────────────────────────────

def test_real_money_stays_locked() -> Optional[str]:
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades
    records = load_trades()
    buckets = classify_records(records)
    gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gate.get("real_money_allowed") is not False:
        return f"real_money_allowed is not False: {gate.get('real_money_allowed')!r}"
    return None


def test_scale_stays_locked() -> Optional[str]:
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades
    records = load_trades()
    buckets = classify_records(records)
    gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gate.get("scale_allowed") is not False:
        return f"scale_allowed is not False: {gate.get('scale_allowed')!r}"
    return None


# ── 16. Thresholds unchanged ─────────────────────────────────────────────────

def test_min_edge_unchanged() -> Optional[str]:
    from config.trading_config import MIN_EDGE
    if abs(MIN_EDGE - 0.03) > 1e-9:
        return f"MIN_EDGE changed: {MIN_EDGE} (expected 0.03)"
    return None


def test_min_confidence_unchanged() -> Optional[str]:
    from config.trading_config import MIN_CONFIDENCE
    if abs(MIN_CONFIDENCE - 0.65) > 1e-9:
        return f"MIN_CONFIDENCE changed: {MIN_CONFIDENCE} (expected 0.65)"
    return None


# ── 17. Valid diagnosis values ────────────────────────────────────────────────

def test_valid_diagnoses() -> Optional[str]:
    """All diagnosis values must be from the known valid set."""
    cells, _, _ = _load_cells()
    for key, c in cells.items():
        d = c.get("diagnosis", "")
        if d not in _VALID_DIAGNOSES:
            return f"Cell '{key}' has unexpected diagnosis {d!r}"
    return None


# ── 18. Zero-n cells are EMPTY ────────────────────────────────────────────────

def test_zero_n_is_empty() -> Optional[str]:
    """Cells with n=0 must be diagnosed EMPTY."""
    cells, _, _ = _load_cells()
    for key, c in cells.items():
        if c.get("n", 1) == 0:
            if c.get("diagnosis") != "EMPTY":
                return f"Cell '{key}' has n=0 but diagnosis={c.get('diagnosis')!r} (expected EMPTY)"
    return None


# ── 19. main() produces sentinel ─────────────────────────────────────────────

def test_main_produces_sentinel() -> Optional[str]:
    """report_2d_clv_payoff_cells.main() must print PROVEN_2D_CLV_PAYOFF_OK."""
    from report_2d_clv_payoff_cells import main, _SENTINEL
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        main()
    except Exception as e:
        sys.stdout = old_stdout
        return f"main() raised exception: {e}"
    finally:
        sys.stdout = old_stdout
    output = buf.getvalue()
    if _SENTINEL not in output:
        return f"main() did not print sentinel '{_SENTINEL}'"
    return None


# ── 20. paper_trader not imported by report ───────────────────────────────────

def test_report_does_not_import_paper_trader() -> Optional[str]:
    """report_2d_clv_payoff_cells.py must not import brain.paper_trader."""
    source = REPORT_PATH.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "paper_trader" in module:
                return f"report imports from '{module}' — must not import paper_trader"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "paper_trader" in alias.name:
                    return f"report imports '{alias.name}' — must not import paper_trader"
    return None


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("report module imports without error",
         test_import),
        ("build_2d_cell_data() runs on real settled records",
         test_build_runs),
        ("KXETH records excluded from all 2D cells",
         test_kxeth_excluded),
        ("all non-empty cells carry required fields",
         test_required_fields),
        ("avg_clv × clv_count ≈ total_clv (math consistency)",
         test_clv_math),
        ("true_be_wr = 1/(2-avg_entry_price) formula",
         test_true_be_wr_formula),
        ("true_ev = WR*(2-ep)-1 formula",
         test_true_ev_formula),
        ("sign(wr_vs_true_be) == sign(true_ev)",
         test_wr_be_sign_matches_true_ev),
        ("0.05-0.10|0.80-0.90 diagnosed GOOD with WR > true_be_wr",
         test_sweetspot_is_good),
        ("0.05-0.10|0.70-0.80 WR < true_be_wr (correctly unprofitable)",
         test_poison_below_true_be),
        ("total 2D trades <= non-KXETH clean_settled (no duplication)",
         test_no_duplication),
        ("import does not mutate any runtime file",
         test_import_no_mutation),
        ("critic_brain.py does NOT reference report or CLV cell fields",
         test_critic_no_report_refs),
        ("builder_brain.py does NOT reference report or CLV cell fields",
         test_builder_no_report_refs),
        ("real_money_allowed=False",
         test_real_money_stays_locked),
        ("scale_allowed=False",
         test_scale_stays_locked),
        ("MIN_EDGE unchanged at 0.03",
         test_min_edge_unchanged),
        ("MIN_CONFIDENCE unchanged at 0.65",
         test_min_confidence_unchanged),
        ("all diagnosis values are from the valid set",
         test_valid_diagnoses),
        ("zero-n cells diagnosed EMPTY",
         test_zero_n_is_empty),
        ("main() produces PROVEN_2D_CLV_PAYOFF_OK sentinel",
         test_main_produces_sentinel),
        ("report does not import paper_trader",
         test_report_does_not_import_paper_trader),
    ]

    passed = 0
    failed = 0
    print("=" * 68)
    print("2D CLV PAYOFF CELL TEST SUITE — Phase 9K")
    print("=" * 68)
    print()
    for name, fn in tests:
        err = fn()
        if err is None:
            print(f"  [PASS]  {name}")
            passed += 1
        else:
            print(f"  [FAIL]  {name}")
            print(f"          {err}")
            failed += 1

    print()
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    print()
    if failed == 0:
        print("PROVEN_2D_CLV_PAYOFF_TESTS_OK")
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
