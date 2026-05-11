"""
Phase 9N — Future-Only Economic PnL Settlement Tests
Sentinel: PROVEN_FUTURE_ECONOMIC_PNL_SETTLEMENT_TESTS_OK

These tests verify future paper settlement accounting without touching
historical logs/paper_trades.jsonl.
"""

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"

PASS: List[str] = []
FAIL: List[str] = []


def ok(name: str) -> None:
    PASS.append(name)
    print(f"  PASS  {name}")


def fail(name: str, msg: str = "") -> None:
    FAIL.append(name)
    print(f"  FAIL  {name}  {msg}")


def file_hash(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CaptureLogger:
    def __init__(self, path: Path):
        self.log_file = str(path)
        self.records: List[Dict[str, Any]] = []

    def log_trade(self, trade: Dict[str, Any]) -> None:
        rec = dict(trade)
        self.records.append(rec)
        with open(self.log_file, "a") as fh:
            fh.write(json.dumps(rec) + "\n")


class CaptureRiskManager:
    def __init__(self):
        self.closed: List[float] = []
        self.results: List[Dict[str, Any]] = []

    def close_position(self, size: float) -> None:
        self.closed.append(float(size))

    def record_result(self, pnl: float, result: str, size: float, ticker: str) -> None:
        self.results.append({
            "pnl": float(pnl),
            "result": result,
            "size": float(size),
            "ticker": ticker,
        })


def make_trader(temp_log: Path):
    from brain.paper_trader import PaperTrader

    trader = PaperTrader.__new__(PaperTrader)
    trader.open_trades = []
    trader.trade_history = []
    trader.total_pnl = 0.0
    trader.settled_trades = 0
    trader.wins = 0
    trader.losses = 0
    trader.logger = CaptureLogger(temp_log)
    trader.risk_manager = CaptureRiskManager()
    return trader


def make_open_trade(ticker: str, entry_price: float, size: float, action: str = "BET_YES") -> Dict[str, Any]:
    return {
        "timestamp": "2099-01-01T00:00:00+00:00",
        "ticker": ticker,
        "action": action,
        "strategy": "TEST",
        "entry_price": entry_price,
        "size": size,
        "confidence": 0.70,
        "edge": 0.05,
        "status": "OPEN",
        "result": None,
        "pnl": 0.0,
        "exit_price": None,
        "settled_at": None,
        "accounting_version": "economic_contract_notional_v1",
        "economic_pnl": 0.0,
        "recorded_pnl": 0.0,
        "capital_at_risk": round(entry_price * size, 2),
        "payout_notional": round(size, 2),
        "max_profit_if_win": round((1.0 - entry_price) * size, 2),
        "max_loss_if_loss": round(entry_price * size, 2),
    }


def test_formula_helper() -> None:
    try:
        from brain.paper_trader import _contract_notional_fields
    except Exception as exc:
        fail("formula_helper_import", str(exc))
        return

    win = _contract_notional_fields(0.64, 5.0, True)
    loss = _contract_notional_fields(0.64, 5.0, False)

    if win["economic_pnl"] != round((1.0 - 0.64) * 5.0, 2):
        fail("win_economic_formula", f"got {win['economic_pnl']}")
        return
    if loss["economic_pnl"] != round(-0.64 * 5.0, 2):
        fail("loss_economic_formula", f"got {loss['economic_pnl']}")
        return
    if win["capital_at_risk"] != 3.20 or win["payout_notional"] != 5.00:
        fail("capital_notional_fields", str(win))
        return
    if win["max_profit_if_win"] != 1.80 or win["max_loss_if_loss"] != 3.20:
        fail("max_profit_loss_fields", str(win))
        return
    ok("formula_helper")


def test_future_settle_win_and_loss() -> None:
    before_hash = file_hash(TRADES_LOG)
    with tempfile.TemporaryDirectory() as tmp:
        temp_log = Path(tmp) / "paper_trades.synthetic.jsonl"

        win_trader = make_trader(temp_log)
        win_trade = make_open_trade("TEST-WIN", 0.64, 5.0)
        win_trader.open_trades.append(win_trade)
        settled_win = win_trader.settle_trade("TEST-WIN", "YES")

        loss_trader = make_trader(temp_log)
        loss_trade = make_open_trade("TEST-LOSS", 0.64, 5.0)
        loss_trader.open_trades.append(loss_trade)
        settled_loss = loss_trader.settle_trade("TEST-LOSS", "NO")

    after_hash = file_hash(TRADES_LOG)
    if before_hash != after_hash:
        fail("future_settle_win_and_loss", "historical log hash changed")
        return

    if settled_win is None or settled_loss is None:
        fail("future_settle_win_and_loss", "settle_trade returned None")
        return
    if settled_win["pnl"] != 1.80 or settled_win["economic_pnl"] != 1.80:
        fail("future_win_pnl", str(settled_win))
        return
    if settled_loss["pnl"] != -3.20 or settled_loss["economic_pnl"] != -3.20:
        fail("future_loss_pnl", str(settled_loss))
        return
    for rec in (settled_win, settled_loss):
        if rec.get("accounting_version") != "economic_contract_notional_v1":
            fail("future_accounting_version", str(rec))
            return
        if rec.get("recorded_pnl") != rec.get("pnl"):
            fail("recorded_pnl_matches_pnl", str(rec))
            return
        for field in (
            "capital_at_risk",
            "payout_notional",
            "max_profit_if_win",
            "max_loss_if_loss",
            "entry_price",
        ):
            if field not in rec:
                fail("future_fields_present", f"missing {field}")
                return
    ok("future_settle_win_and_loss")


def test_historical_records_remain_readable() -> None:
    if not TRADES_LOG.exists():
        fail("historical_records_readable", "paper trade log missing")
        return
    checked = 0
    for line in TRADES_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            fail("historical_records_readable", str(exc))
            return
        if rec.get("status") == "SETTLED":
            _ = rec.get("pnl")
            _ = rec.get("accounting_version", "legacy_hybrid_or_unversioned")
            checked += 1
        if checked >= 10:
            break
    if checked == 0:
        fail("historical_records_readable", "no settled records checked")
        return
    ok(f"historical_records_readable ({checked} sampled)")


def test_reports_distinguish_versions() -> None:
    try:
        import tools.report_pnl_accounting_reconciliation as rp
    except Exception as exc:
        fail("reports_distinguish_versions", str(exc))
        return

    legacy = {"ticker": "OLD", "status": "SETTLED", "result": "LOSS"}
    economic = {
        "ticker": "NEW",
        "status": "SETTLED",
        "result": "LOSS",
        "accounting_version": "economic_contract_notional_v1",
    }
    if rp._accounting_version(legacy) != "legacy_hybrid_or_unversioned":
        fail("reports_distinguish_versions", "legacy version not detected")
        return
    if rp._accounting_version(economic) != "economic_contract_notional_v1":
        fail("reports_distinguish_versions", "economic version not detected")
        return
    ok("reports_distinguish_versions")


def test_auto_settle_expected_pnl_and_time_exit_labels() -> None:
    try:
        import auto_settle_trades as ast
    except Exception as exc:
        fail("auto_settle_expected_pnl", str(exc))
        return

    trade = make_open_trade("TEST-AUTO", 0.64, 5.0)
    win_pnl = ast._compute_expected_pnl(trade, "YES")
    loss_pnl = ast._compute_expected_pnl(trade, "NO")
    if win_pnl != 1.80 or loss_pnl != -3.20:
        fail("auto_settle_expected_pnl", f"win={win_pnl} loss={loss_pnl}")
        return

    fields = ast._time_exit_accounting_fields(trade, -0.25)
    if fields.get("accounting_version") != "time_exit_mark_to_market_v1":
        fail("time_exit_accounting_label", str(fields))
        return
    if fields.get("economic_pnl") != -0.25 or fields.get("recorded_pnl") != -0.25:
        fail("time_exit_recorded_fields", str(fields))
        return
    ok("auto_settle_expected_pnl_and_time_exit_labels")


def test_safety_locks() -> None:
    try:
        from config.trading_config import (
            GLOBAL_FORCED_LEARNING_MODE,
            MIN_CONFIDENCE,
            MIN_EDGE,
            TRADING_MODE,
        )
        from tools.clean_truth_report import classify_records, evaluate_proof_gates
        from tools.performance_report import load_trades
    except Exception as exc:
        fail("safety_locks", str(exc))
        return

    records = load_trades()
    buckets = classify_records(records)
    gates = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if TRADING_MODE != "PAPER":
        fail("safety_locks", f"TRADING_MODE={TRADING_MODE}")
        return
    if GLOBAL_FORCED_LEARNING_MODE is not True:
        fail("safety_locks", "GLOBAL_FORCED_LEARNING_MODE is not True")
        return
    if MIN_EDGE < 0.03 or MIN_CONFIDENCE < 0.65:
        fail("safety_locks", f"MIN_EDGE={MIN_EDGE} MIN_CONFIDENCE={MIN_CONFIDENCE}")
        return
    if gates.get("real_money_allowed") is not False:
        fail("safety_locks", f"real_money_allowed={gates.get('real_money_allowed')}")
        return
    if gates.get("scale_allowed") is not False:
        fail("safety_locks", f"scale_allowed={gates.get('scale_allowed')}")
        return
    ok("safety_locks")


def main() -> None:
    print()
    print("=" * 72)
    print("  PHASE 9N — FUTURE ECONOMIC PNL SETTLEMENT TESTS")
    print("  Sentinel: PROVEN_FUTURE_ECONOMIC_PNL_SETTLEMENT_TESTS_OK")
    print("=" * 72)
    print()

    test_formula_helper()
    test_future_settle_win_and_loss()
    test_historical_records_remain_readable()
    test_reports_distinguish_versions()
    test_auto_settle_expected_pnl_and_time_exit_labels()
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
    print("  Sentinel: PROVEN_FUTURE_ECONOMIC_PNL_SETTLEMENT_TESTS_OK")
    print()


if __name__ == "__main__":
    main()
