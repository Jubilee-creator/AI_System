"""
Phase 9O — Dashboard Bet / Reward Truth Tests
Sentinel: PROVEN_DASHBOARD_BET_REWARD_TRUTH_TESTS_OK

Read-only tests for Dashboard.py trade economics display helpers.
"""

import hashlib
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"

PASS = []
FAIL = []


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


def test_economic_trade_math(dashboard):
    row = {
        "entry_price": 0.93,
        "size": 45.0,
        "accounting_version": "economic_contract_notional_v1",
        "economic_pnl": 0.0,
        "recorded_pnl": 0.0,
    }
    truth = dashboard.build_bet_reward_truth(row, current_price=0.94)
    if truth["payout_notional"] != 45.00:
        fail("economic_payout_notional", truth)
        return
    if truth["capital_at_risk"] != 41.85:
        fail("economic_capital_at_risk", truth)
        return
    if truth["max_profit_if_win"] != 3.15:
        fail("economic_max_profit", truth)
        return
    if truth["max_loss_if_loss"] != 41.85:
        fail("economic_max_loss", truth)
        return
    if truth["breakeven_wr"] != 0.93:
        fail("economic_breakeven", truth)
        return
    if truth["reward_risk"] != 0.0753:
        fail("economic_reward_risk", truth)
        return
    if "High-price contract" not in (truth.get("warning") or ""):
        fail("economic_high_price_warning", truth)
        return
    ok("economic_trade_math")


def test_legacy_trade_math(dashboard):
    row = {
        "entry_price": 0.67,
        "size": 10.0,
        "pnl": -10.0,
    }
    truth = dashboard.build_bet_reward_truth(row)
    expected_be = round(1.0 / (2.0 - 0.67), 4)
    if truth["accounting_version"] != "legacy_hybrid_or_unversioned":
        fail("legacy_accounting_version", truth)
        return
    if truth["breakeven_wr"] != expected_be:
        fail("legacy_breakeven", truth)
        return
    if "Legacy accounting row" not in (truth.get("warning") or ""):
        fail("legacy_warning", truth)
        return
    ok("legacy_trade_math")


def test_labels_present(dashboard):
    truth = dashboard.build_bet_reward_truth({
        "entry_price": 0.80,
        "size": 5.0,
        "accounting_version": "economic_contract_notional_v1",
    })
    labels = set(truth.get("labels") or [])
    required = {
        "Payout Notional",
        "Capital at Risk",
        "Max Profit",
        "Max Loss",
        "Accounting Version",
        "Economic PnL",
        "Recorded PnL",
    }
    missing = sorted(required - labels)
    if missing:
        fail("labels_present", f"missing {missing}")
        return

    src = (ROOT / "Dashboard.py").read_text()
    for text in ("Payout Notional", "Capital at Risk", "Bet / Reward Truth", "Notional $"):
        if text not in src:
            fail("labels_present", f"{text!r} not in Dashboard.py")
            return
    ok("labels_present")


def test_no_historical_log_modified(dashboard):
    before = file_hash(TRADES_LOG)
    _ = dashboard.build_bet_reward_truth({
        "entry_price": 0.93,
        "size": 45.0,
        "accounting_version": "economic_contract_notional_v1",
    })
    after = file_hash(TRADES_LOG)
    if before != after:
        fail("no_historical_log_modified", "paper_trades hash changed")
        return
    ok("no_historical_log_modified")


def test_safety_locks():
    try:
        from tools.clean_truth_report import classify_records, evaluate_proof_gates
        from tools.performance_report import load_trades
    except Exception as exc:
        fail("safety_locks", str(exc))
        return

    records = load_trades()
    buckets = classify_records(records)
    gates = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gates.get("real_money_allowed") is not False:
        fail("safety_locks", f"real_money_allowed={gates.get('real_money_allowed')}")
        return
    if gates.get("scale_allowed") is not False:
        fail("safety_locks", f"scale_allowed={gates.get('scale_allowed')}")
        return
    ok("safety_locks")


def main():
    print()
    print("=" * 72)
    print("  PHASE 9O — DASHBOARD BET / REWARD TRUTH TESTS")
    print("  Sentinel: PROVEN_DASHBOARD_BET_REWARD_TRUTH_TESTS_OK")
    print("=" * 72)
    print()

    before = file_hash(TRADES_LOG)
    try:
        import Dashboard as dashboard
    except Exception as exc:
        fail("import_dashboard", str(exc))
        dashboard = None
    after = file_hash(TRADES_LOG)
    if before != after:
        fail("import_dashboard_read_only", "paper_trades hash changed during import")
    else:
        ok("import_dashboard_read_only")

    if dashboard:
        test_economic_trade_math(dashboard)
        test_legacy_trade_math(dashboard)
        test_labels_present(dashboard)
        test_no_historical_log_modified(dashboard)
    test_safety_locks()

    print()
    total = len(PASS) + len(FAIL)
    print(f"  Results: {len(PASS)}/{total} passed")
    if FAIL:
        print(f"  FAILED: {', '.join(FAIL)}")
        print()
        print("  Sentinel NOT reached.")
        sys.exit(1)

    print()
    print("  Sentinel: PROVEN_DASHBOARD_BET_REWARD_TRUTH_TESTS_OK")
    print()


if __name__ == "__main__":
    main()
