"""
Phase 9M — PnL Accounting Reconciliation Simulator Tests
Sentinel: PROVEN_PNL_ACCOUNTING_RECONCILIATION_TESTS_OK

Read-only tests for tools/report_pnl_accounting_reconciliation.py.
These tests must not rewrite historical logs or change live trading behavior.
"""

import ast
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = []
FAIL = []

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
GUARDED_FILES = [
    ROOT / "logs" / "paper_trades.jsonl",
    ROOT / "brain" / "paper_trader.py",
    ROOT / "brain" / "critic_brain.py",
    ROOT / "brain" / "builder_brain.py",
]


def ok(name):
    PASS.append(name)
    print(f"  PASS  {name}")


def fail(name, msg=""):
    FAIL.append(name)
    print(f"  FAIL  {name}  {msg}")


def file_hash(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def guarded_hashes() -> dict:
    return {str(path): file_hash(path) for path in GUARDED_FILES}


def load_settled_records() -> List[dict]:
    if not TRADES_LOG.exists():
        return []
    seen = {}
    for line in TRADES_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status") not in ("SETTLED", "FORCED_CLOSE"):
            continue
        key = (rec.get("ticker", ""), rec.get("timestamp", ""))
        seen[key] = rec
    return list(seen.values())


def recorded_pnl(rec: dict) -> Optional[float]:
    raw = rec.get("pnl")
    if raw is None:
        raw = rec.get("realized_pnl")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def ep(rec: dict) -> Optional[float]:
    raw = rec.get("entry_price")
    if raw is None:
        raw = rec.get("yes_ask") or rec.get("price")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def size(rec: dict) -> float:
    try:
        return float(rec.get("size", 5.0))
    except (TypeError, ValueError):
        return 5.0


def is_kxeth(rec: dict) -> bool:
    return "KXETH" in str(rec.get("ticker", "")).upper()


def test_import():
    try:
        import tools.report_pnl_accounting_reconciliation as rp
        ok("import_clean")
        return rp
    except Exception as exc:
        fail("import_clean", str(exc))
        return None


def test_formula_correctness(rp):
    cases = [
        (0.57, 5.0),
        (0.68, 10.0),
        (0.84, 5.0),
    ]
    for price, trade_size in cases:
        expected_win = round((1.0 - price) * trade_size, 6)
        expected_notional_loss = round(-price * trade_size, 6)
        expected_cost_win = round(((1.0 - price) / price) * trade_size, 6)

        if rp.pnl_hybrid(price, trade_size, True) != expected_win:
            fail("hybrid_win_formula", f"ep={price}")
            return
        if rp.pnl_hybrid(price, trade_size, False) != round(-trade_size, 6):
            fail("hybrid_loss_formula", f"ep={price}")
            return
        if rp.pnl_notional(price, trade_size, True) != expected_win:
            fail("model_b_win_formula", f"ep={price}")
            return
        if rp.pnl_notional(price, trade_size, False) != expected_notional_loss:
            fail("model_b_loss_formula", f"ep={price}")
            return
        if rp.pnl_cost(price, trade_size, True) != expected_cost_win:
            fail("model_c_win_formula", f"ep={price}")
            return
        if rp.pnl_cost(price, trade_size, False) != round(-trade_size, 6):
            fail("model_c_loss_formula", f"ep={price}")
            return

    if rp.pnl_time_exit(0.70, 0.64, 5.0) != round((0.64 - 0.70) * 5.0, 6):
        fail("time_exit_formula", "unexpected time-exit formula")
        return
    ok("formula_correctness")


def test_report_runs_read_only(rp):
    before = guarded_hashes()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rp.main()
        out = buf.getvalue()
    except Exception as exc:
        fail("report_runs_read_only", str(exc))
        return
    after = guarded_hashes()
    if before != after:
        fail("report_runs_read_only", "guarded file hash changed")
        return
    required = [
        "PROVEN_PNL_RECONCILIATION_OK",
        "Model B",
        "real_money_allowed       : False",
        "scale_allowed            : False",
    ]
    missing = [token for token in required if token not in out]
    if missing:
        fail("report_runs_read_only", f"missing output token(s): {missing}")
        return
    ok("report_runs_read_only")


def test_no_live_behavior_imports():
    report_path = ROOT / "tools" / "report_pnl_accounting_reconciliation.py"
    try:
        tree = ast.parse(report_path.read_text())
    except SyntaxError as exc:
        fail("no_live_behavior_imports", str(exc))
        return

    forbidden_modules = (
        "brain.paper_trader",
        "paper_trader",
        "brain.critic_brain",
        "critic_brain",
        "brain.builder_brain",
        "builder_brain",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    fail("no_live_behavior_imports", f"imports {alias.name}")
                    return
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in forbidden_modules:
                fail("no_live_behavior_imports", f"from {module} import ...")
                return
    ok("no_live_behavior_imports")


def test_no_critic_builder_paper_trader_mutation(rp):
    before = guarded_hashes()
    rp.compute_model_stats(*rp.load_binary_records(), rp.MODEL_NOTIONAL)
    after = guarded_hashes()
    if before == after:
        ok("no_critic_builder_paper_trader_mutation")
    else:
        fail("no_critic_builder_paper_trader_mutation", "guarded file hash changed")


def test_hybrid_reproduces_stored_pnl(rp):
    mismatches = []
    checked = 0
    for rec in load_settled_records():
        result = str(rec.get("result", "")).upper()
        if (
            is_kxeth(rec)
            or result not in ("WIN", "LOSS")
            or rp._accounting_version(rec) != "legacy_hybrid_or_unversioned"
        ):
            continue
        price = ep(rec)
        actual = recorded_pnl(rec)
        if price is None or actual is None:
            continue
        expected = rp.pnl_hybrid(price, size(rec), result == "WIN")
        if abs(actual - expected) > 0.02:
            mismatches.append((rec.get("ticker"), actual, expected))
        checked += 1

    if checked == 0:
        fail("hybrid_reproduces_stored_pnl", "no non-KXETH binary records checked")
        return
    if mismatches:
        fail("hybrid_reproduces_stored_pnl", f"{len(mismatches)}/{checked} mismatch")
        return
    ok(f"hybrid_reproduces_stored_pnl ({checked} records)")


def test_corrected_model_does_not_overwrite_stored_pnl(rp):
    losses = [
        rec for rec in load_settled_records()
        if not is_kxeth(rec)
        and str(rec.get("result", "")).upper() == "LOSS"
        and rp._accounting_version(rec) == "legacy_hybrid_or_unversioned"
        and ep(rec) is not None
        and recorded_pnl(rec) is not None
    ]
    if not losses:
        fail("corrected_model_does_not_overwrite_stored_pnl", "no losses available")
        return

    before_hash = file_hash(TRADES_LOG)
    changed_by_overlay = 0
    for rec in losses:
        model_b = rp.pnl_notional(ep(rec), size(rec), False)
        if abs(recorded_pnl(rec) - model_b) > 0.02:
            changed_by_overlay += 1

    rp.compute_model_stats(*rp.load_binary_records(), rp.MODEL_NOTIONAL)
    after_hash = file_hash(TRADES_LOG)

    if before_hash != after_hash:
        fail("corrected_model_does_not_overwrite_stored_pnl", "log hash changed")
        return
    if changed_by_overlay == 0:
        fail("corrected_model_does_not_overwrite_stored_pnl", "stored losses already equal Model B")
        return
    ok("corrected_model_does_not_overwrite_stored_pnl")


def test_model_stats_match_expected_direction(rp):
    wins, losses = rp.load_binary_records()
    hybrid = rp.compute_model_stats(wins, losses, rp.MODEL_HYBRID)
    notional = rp.compute_model_stats(wins, losses, rp.MODEL_NOTIONAL)
    cost = rp.compute_model_stats(wins, losses, rp.MODEL_COST)

    if hybrid["total_pnl"] >= notional["total_pnl"]:
        fail("model_stats_direction", "Model B should improve over hybrid")
        return
    if cost["total_pnl"] <= hybrid["total_pnl"]:
        fail("model_stats_direction", "Model C should improve over hybrid")
        return
    if notional["profit_factor"] <= 1.10:
        fail("model_stats_direction", "Model B PF should exceed 1.10 in current sample")
        return
    if not (hybrid["wins"] == notional["wins"] == cost["wins"]):
        fail("model_stats_direction", "model win counts diverged")
        return
    ok("model_stats_direction")


def test_kxeth_quarantine(rp):
    all_records = load_settled_records()
    kxeth = [rec for rec in all_records if is_kxeth(rec)]
    wins, losses = rp.load_binary_records()
    included = [rec for rec in wins + losses if is_kxeth(rec)]

    if not kxeth:
        fail("kxeth_quarantine", "no KXETH records found to verify quarantine")
        return
    if included:
        fail("kxeth_quarantine", f"{len(included)} KXETH records included")
        return
    ok(f"kxeth_quarantine ({len(kxeth)} KXETH records excluded)")


def test_real_money_and_scale_locked():
    try:
        from tools.clean_truth_report import classify_records, evaluate_proof_gates
        from tools.performance_report import load_trades

        records = load_trades()
        buckets = classify_records(records)
        gates = evaluate_proof_gates(buckets, buckets["clean_settled"])
    except Exception as exc:
        fail("real_money_and_scale_locked", str(exc))
        return

    if gates.get("real_money_allowed") is not False:
        fail("real_money_and_scale_locked", f"real_money_allowed={gates.get('real_money_allowed')}")
        return
    if gates.get("scale_allowed") is not False:
        fail("real_money_and_scale_locked", f"scale_allowed={gates.get('scale_allowed')}")
        return
    ok("real_money_and_scale_locked")


def test_safety_config_unchanged():
    try:
        from config.trading_config import (
            GLOBAL_FORCED_LEARNING_MODE,
            MIN_CONFIDENCE,
            MIN_EDGE,
            TRADING_MODE,
        )
    except Exception as exc:
        fail("safety_config_unchanged", str(exc))
        return

    if TRADING_MODE != "PAPER":
        fail("safety_config_unchanged", f"TRADING_MODE={TRADING_MODE}")
        return
    if GLOBAL_FORCED_LEARNING_MODE is not True:
        fail("safety_config_unchanged", "GLOBAL_FORCED_LEARNING_MODE is not True")
        return
    if MIN_EDGE < 0.03:
        fail("safety_config_unchanged", f"MIN_EDGE={MIN_EDGE}")
        return
    if MIN_CONFIDENCE < 0.65:
        fail("safety_config_unchanged", f"MIN_CONFIDENCE={MIN_CONFIDENCE}")
        return
    ok("safety_config_unchanged")


def main():
    print()
    print("=" * 70)
    print("  PHASE 9M — PNL ACCOUNTING RECONCILIATION TESTS")
    print("  Sentinel: PROVEN_PNL_ACCOUNTING_RECONCILIATION_TESTS_OK")
    print("=" * 70)
    print()

    rp = test_import()
    if rp:
        test_formula_correctness(rp)
        test_report_runs_read_only(rp)
        test_no_critic_builder_paper_trader_mutation(rp)
        test_hybrid_reproduces_stored_pnl(rp)
        test_corrected_model_does_not_overwrite_stored_pnl(rp)
        test_model_stats_match_expected_direction(rp)
        test_kxeth_quarantine(rp)

    test_no_live_behavior_imports()
    test_real_money_and_scale_locked()
    test_safety_config_unchanged()

    print()
    total = len(PASS) + len(FAIL)
    print(f"  Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"  FAILED: {', '.join(FAIL)}")
        print()
        print("  Sentinel NOT reached.")
        sys.exit(1)

    print()
    print("  Sentinel: PROVEN_PNL_ACCOUNTING_RECONCILIATION_TESTS_OK")
    print()


if __name__ == "__main__":
    main()
