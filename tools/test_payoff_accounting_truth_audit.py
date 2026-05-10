"""
Phase 9L — Payoff Accounting Truth Audit Tests
Sentinel: PROVEN_PAYOFF_ACCOUNTING_AUDIT_TESTS_OK
"""

import ast
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = []
FAIL = []


def ok(name):
    PASS.append(name)
    print(f"  PASS  {name}")


def fail(name, msg=""):
    FAIL.append(name)
    print(f"  FAIL  {name}  {msg}")


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------

def test_import():
    try:
        import tools.report_payoff_accounting_truth_audit as rp
        ok("import_clean")
        return rp
    except Exception as e:
        fail("import_clean", str(e))
        return None


# ---------------------------------------------------------------------------
# Formula correctness
# ---------------------------------------------------------------------------

def test_current_formula(rp):
    # WIN: (1-ep)*size
    v = rp.current_pnl(0.57, 5.0, True)
    if abs(v - (1 - 0.57) * 5.0) < 1e-9:
        ok("current_win_formula")
    else:
        fail("current_win_formula", f"got {v}")

    # LOSS: -size
    v = rp.current_pnl(0.57, 5.0, False)
    if abs(v - (-5.0)) < 1e-9:
        ok("current_loss_formula")
    else:
        fail("current_loss_formula", f"got {v}")


def test_model_a_formula(rp):
    # WIN: (1-ep)*size
    v = rp.model_a_pnl(0.57, 5.0, True)
    if abs(v - (1 - 0.57) * 5.0) < 1e-9:
        ok("model_a_win")
    else:
        fail("model_a_win", f"got {v}")

    # LOSS: -ep*size
    v = rp.model_a_pnl(0.57, 5.0, False)
    if abs(v - (-0.57 * 5.0)) < 1e-9:
        ok("model_a_loss")
    else:
        fail("model_a_loss", f"got {v}")


def test_model_b_formula(rp):
    # WIN: (1-ep)/ep * size
    v = rp.model_b_pnl(0.57, 5.0, True)
    expected = ((1 - 0.57) / 0.57) * 5.0
    if abs(v - expected) < 1e-3:
        ok("model_b_win")
    else:
        fail("model_b_win", f"got {v}, expected {expected}")

    # LOSS: -size
    v = rp.model_b_pnl(0.57, 5.0, False)
    if abs(v - (-5.0)) < 1e-9:
        ok("model_b_loss")
    else:
        fail("model_b_loss", f"got {v}")


def test_current_win_equals_model_a_win(rp):
    """WIN formulas are identical across current and Model A."""
    for ep in [0.50, 0.57, 0.62, 0.74, 0.81, 0.84, 0.90]:
        cur = rp.current_pnl(ep, 5.0, True)
        ma = rp.model_a_pnl(ep, 5.0, True)
        if abs(cur - ma) > 1e-9:
            fail("current_win_equals_model_a", f"ep={ep}: cur={cur}, ma={ma}")
            return
    ok("current_win_equals_model_a")


def test_current_loss_equals_model_b_loss(rp):
    """LOSS formulas are identical across current and Model B."""
    for ep in [0.50, 0.57, 0.62, 0.74, 0.81, 0.84, 0.90]:
        cur = rp.current_pnl(ep, 5.0, False)
        mb = rp.model_b_pnl(ep, 5.0, False)
        if abs(cur - mb) > 1e-9:
            fail("current_loss_equals_model_b", f"ep={ep}: cur={cur}, mb={mb}")
            return
    ok("current_loss_equals_model_b")


# ---------------------------------------------------------------------------
# Breakeven WR
# ---------------------------------------------------------------------------

def test_true_breakeven_formula(rp):
    """true_BE_WR = 1/(2-ep): confirmed mathematically."""
    for ep in [0.50, 0.57, 0.62, 0.70, 0.74, 0.81, 0.84]:
        tbe = rp.true_breakeven_wr(ep)
        expected = 1.0 / (2.0 - ep)
        if abs(tbe - expected) > 1e-9:
            fail("true_breakeven_formula", f"ep={ep}: got {tbe}, expected {expected}")
            return
    ok("true_breakeven_formula")


def test_breakeven_is_zero_ev(rp):
    """At true_BE_WR, E[PnL] == 0 under current accounting."""
    for ep in [0.57, 0.62, 0.74, 0.81, 0.84]:
        wr = rp.true_breakeven_wr(ep)
        size = 5.0
        ev = wr * rp.current_pnl(ep, size, True) + (1 - wr) * rp.current_pnl(ep, size, False)
        if abs(ev) > 1e-9:
            fail("breakeven_is_zero_ev", f"ep={ep}, wr={wr}, ev={ev}")
            return
    ok("breakeven_is_zero_ev")


def test_economic_breakeven_equals_ep(rp):
    """Under Model A, breakeven WR = ep."""
    for ep in [0.57, 0.62, 0.74, 0.81, 0.84]:
        wr = rp.economic_breakeven_wr(ep)
        size = 5.0
        ev = wr * rp.model_a_pnl(ep, size, True) + (1 - wr) * rp.model_a_pnl(ep, size, False)
        if abs(ev) > 1e-9:
            fail("economic_breakeven_model_a", f"ep={ep}, wr={wr}, ev={ev}")
            return
    ok("economic_breakeven_model_a")


def test_true_be_exceeds_economic_be(rp):
    """true_BE_WR > economic_BE_WR for all valid ep in (0,1)."""
    for ep in [0.50, 0.57, 0.62, 0.70, 0.74, 0.81, 0.84]:
        tbe = rp.true_breakeven_wr(ep)
        ebe = rp.economic_breakeven_wr(ep)
        if not (tbe > ebe):
            fail("true_be_exceeds_economic", f"ep={ep}: tbe={tbe}, ebe={ebe}")
            return
    ok("true_be_exceeds_economic")


# ---------------------------------------------------------------------------
# Loss overstatement direction
# ---------------------------------------------------------------------------

def test_loss_overstatement_positive(rp):
    """Current LOSS formula always records a larger loss than Model A (conservative)."""
    for ep in [0.50, 0.57, 0.62, 0.70, 0.74, 0.81, 0.84, 0.90]:
        cur = rp.current_pnl(ep, 5.0, False)  # negative
        ma = rp.model_a_pnl(ep, 5.0, False)   # also negative but smaller magnitude
        overstate = cur - ma
        if not (overstate < 0):  # cur is more negative (worse), so cur-ma < 0
            fail("loss_overstatement_positive", f"ep={ep}: cur={cur}, ma={ma}")
            return
    ok("loss_overstatement_positive")


def test_win_no_overstatement(rp):
    """WIN formula is identical; no overstatement for wins."""
    for ep in [0.50, 0.57, 0.62, 0.74, 0.84]:
        cur = rp.current_pnl(ep, 5.0, True)
        ma = rp.model_a_pnl(ep, 5.0, True)
        if abs(cur - ma) > 1e-9:
            fail("win_no_overstatement", f"ep={ep}: cur={cur}, ma={ma}")
            return
    ok("win_no_overstatement")


# ---------------------------------------------------------------------------
# Live log verification (uses actual trade log)
# ---------------------------------------------------------------------------

def test_actual_pnl_matches_current_formula(rp):
    """Spot-check: recorded PnL == current_pnl() for settled trades."""
    log = ROOT / "logs" / "paper_trades.jsonl"
    if not log.exists():
        print("  SKIP  actual_pnl_matches (no log file)")
        return

    seen = {}
    for line in log.read_text().splitlines():
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

    mismatches = 0
    checked = 0
    for r in seen.values():
        ep_raw = r.get("entry_price") or r.get("yes_ask") or r.get("price")
        pnl_raw = r.get("pnl") or r.get("realized_pnl")
        result = str(r.get("result", "")).upper()
        if ep_raw is None or pnl_raw is None or result not in ("WIN", "LOSS"):
            continue
        try:
            ep = float(ep_raw)
            actual = float(pnl_raw)
            size = float(r.get("size", 5.0))
        except (TypeError, ValueError):
            continue
        won = result == "WIN"
        expected = rp.current_pnl(ep, size, won)
        if abs(actual - expected) > 0.02:
            mismatches += 1
        checked += 1

    if checked == 0:
        print("  SKIP  actual_pnl_matches (no valid records)")
        return
    if mismatches == 0:
        ok(f"actual_pnl_matches ({checked} records verified)")
    else:
        fail("actual_pnl_matches", f"{mismatches}/{checked} mismatches (may include time-exit records)")


def test_model_a_corrected_pnl_better_than_actual(rp):
    """Model A total PnL > actual total PnL (since we overstate losses)."""
    log = ROOT / "logs" / "paper_trades.jsonl"
    if not log.exists():
        print("  SKIP  model_a_corrected (no log file)")
        return

    seen = {}
    for line in log.read_text().splitlines():
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

    actual_total = 0.0
    model_a_total = 0.0
    for r in seen.values():
        if "KXETH" in str(r.get("ticker", "")).upper():
            continue
        ep_raw = r.get("entry_price") or r.get("yes_ask") or r.get("price")
        pnl_raw = r.get("pnl") or r.get("realized_pnl")
        result = str(r.get("result", "")).upper()
        if ep_raw is None or pnl_raw is None or result not in ("WIN", "LOSS"):
            continue
        try:
            ep = float(ep_raw)
            actual = float(pnl_raw)
            size = float(r.get("size", 5.0))
        except (TypeError, ValueError):
            continue
        won = result == "WIN"
        actual_total += actual
        model_a_total += rp.model_a_pnl(ep, size, won)

    if model_a_total > actual_total:
        ok(f"model_a_better_than_actual (actual={actual_total:.2f}, modelA={model_a_total:.2f})")
    else:
        fail("model_a_better_than_actual",
             f"actual={actual_total:.2f}, modelA={model_a_total:.2f}")


# ---------------------------------------------------------------------------
# Report runs end-to-end
# ---------------------------------------------------------------------------

def test_report_runs():
    import io, contextlib
    try:
        import tools.report_payoff_accounting_truth_audit as rp
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rp.main()
        out = buf.getvalue()
        if "PROVEN_PAYOFF_ACCOUNTING_AUDIT_OK" in out:
            ok("report_runs_with_sentinel")
        else:
            fail("report_runs_with_sentinel", "sentinel missing from output")
    except Exception as e:
        fail("report_runs_with_sentinel", str(e))


# ---------------------------------------------------------------------------
# Architecture: report does not import paper_trader
# ---------------------------------------------------------------------------

def test_no_paper_trader_import():
    report_path = ROOT / "tools" / "report_payoff_accounting_truth_audit.py"
    if not report_path.exists():
        fail("no_paper_trader_import", "report file not found")
        return
    src = report_path.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        fail("no_paper_trader_import", f"parse error: {e}")
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "paper_trader" in alias.name:
                    fail("no_paper_trader_import", f"imports {alias.name}")
                    return
        if isinstance(node, ast.ImportFrom):
            if node.module and "paper_trader" in node.module:
                fail("no_paper_trader_import", f"from {node.module} import ...")
                return
    ok("no_paper_trader_import")


# ---------------------------------------------------------------------------
# AST: no Critic/Builder
# ---------------------------------------------------------------------------

def test_no_critic_builder_in_report():
    report_path = ROOT / "tools" / "report_payoff_accounting_truth_audit.py"
    if not report_path.exists():
        fail("no_critic_builder", "report file not found")
        return
    src = report_path.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        fail("no_critic_builder", f"parse error: {e}")
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ("Critic", "Builder"):
            fail("no_critic_builder", f"found reference to {node.id}")
            return
    ok("no_critic_builder")


# ---------------------------------------------------------------------------
# Safety locks unchanged
# ---------------------------------------------------------------------------

def test_safety_locks():
    try:
        from config.trading_config import (
            TRADING_MODE,
            GLOBAL_FORCED_LEARNING_MODE,
            MIN_EDGE,
            MIN_CONFIDENCE,
        )
        assert TRADING_MODE == "PAPER", f"TRADING_MODE={TRADING_MODE}"
        assert GLOBAL_FORCED_LEARNING_MODE is True
        assert MIN_EDGE >= 0.03
        assert MIN_CONFIDENCE >= 0.65
        ok("safety_locks_unchanged")
    except AssertionError as e:
        fail("safety_locks_unchanged", str(e))
    except Exception as e:
        fail("safety_locks_unchanged", str(e))


def test_real_money_still_locked():
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        records = load_trades()
        buckets = classify_records(records)
        gates = evaluate_proof_gates(buckets, buckets["clean_settled"])
        assert gates.get("real_money_allowed") is False
        assert gates.get("scale_allowed") is False
        ok("real_money_scale_locked")
    except AssertionError as e:
        fail("real_money_scale_locked", str(e))
    except Exception as e:
        fail("real_money_scale_locked", str(e))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    print()
    print("=" * 60)
    print("  PHASE 9L — PAYOFF ACCOUNTING AUDIT TESTS")
    print("  Sentinel: PROVEN_PAYOFF_ACCOUNTING_AUDIT_TESTS_OK")
    print("=" * 60)
    print()

    rp = test_import()

    if rp:
        test_current_formula(rp)
        test_model_a_formula(rp)
        test_model_b_formula(rp)
        test_current_win_equals_model_a_win(rp)
        test_current_loss_equals_model_b_loss(rp)
        test_true_breakeven_formula(rp)
        test_breakeven_is_zero_ev(rp)
        test_economic_breakeven_equals_ep(rp)
        test_true_be_exceeds_economic_be(rp)
        test_loss_overstatement_positive(rp)
        test_win_no_overstatement(rp)
        test_actual_pnl_matches_current_formula(rp)
        test_model_a_corrected_pnl_better_than_actual(rp)

    test_report_runs()
    test_no_paper_trader_import()
    test_no_critic_builder_in_report()
    test_safety_locks()
    test_real_money_still_locked()

    print()
    total = len(PASS) + len(FAIL)
    print(f"  Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"  FAILED: {', '.join(FAIL)}")
        print()
        print("  Sentinel NOT reached.")
        sys.exit(1)
    else:
        print()
        print("  Sentinel: PROVEN_PAYOFF_ACCOUNTING_AUDIT_TESTS_OK")
        print()


if __name__ == "__main__":
    main()
