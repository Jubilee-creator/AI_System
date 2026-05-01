#!/usr/bin/env python3
"""
tools/report_signal_bias.py
---------------------------
Read-only audit for YES/NO action bias.

This report checks whether the current paper-trading path is structurally
one-sided before any signal redesign. It does not trade, mutate logs, or alter
strategy/risk/sizing behavior.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import (  # noqa: E402
    build_terminal_key_sets,
    classify_open_records,
    classify_settled_records,
    get_pnl,
    load_trades,
)


SOURCE_FILES = {
    "market_scanner": ROOT / "brain" / "market_scanner.py",
    "decision_engine": ROOT / "engine" / "decision_engine.py",
    "paper_trader": ROOT / "brain" / "paper_trader.py",
    "dashboard": ROOT / "Dashboard.py",
}


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(part: int, total: int) -> str:
    if total <= 0:
        return "n/a"
    return f"{part / total * 100:.1f}%"


def _fmt_num(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}"


def _fmt_money(value: float) -> str:
    return f"${value:+.2f}"


def action_value(rec: dict) -> str:
    return str(rec.get("action") or "UNKNOWN").upper()


def ticker_family(rec: dict) -> str:
    ticker = str(rec.get("ticker") or "UNKNOWN").upper()
    if "-" in ticker:
        return ticker.split("-", 1)[0]
    return ticker[:8] if ticker else "UNKNOWN"


def strategy_value(rec: dict) -> str:
    return str(rec.get("strategy") or rec.get("raw_strategy") or "UNKNOWN")


def council_value(rec: dict) -> str:
    return str(rec.get("council_decision") or "UNKNOWN")


def class_value(rec: dict) -> str:
    if rec.get("data_collection_override"):
        return "DATA_COLLECTION_OVERRIDE"
    if rec.get("bootstrap_provisional"):
        return "BOOTSTRAP_PROVISIONAL"
    if rec.get("learning_trade"):
        return "LEARNING_OTHER"
    return "NORMAL_OR_LEGACY"


def edge_value(rec: dict) -> Optional[float]:
    value = _as_float(rec.get("risk_edge"))
    if value is not None:
        return value
    return _as_float(rec.get("edge"))


def confidence_value(rec: dict) -> Optional[float]:
    for key in ("model_probability", "original_confidence", "confidence"):
        value = _as_float(rec.get(key))
        if value is not None:
            return value
    return None


def group_actions(rows: List[dict], key_fn) -> Dict[str, Counter]:
    groups: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        groups[str(key_fn(row))][action_value(row)] += 1
    return groups


def print_action_distribution(title: str, rows: List[dict]) -> None:
    counts = Counter(action_value(r) for r in rows)
    total = sum(counts.values())
    yes = counts.get("BET_YES", 0)
    no = counts.get("BET_NO", 0)
    arb = counts.get("ARB", 0)
    other = total - yes - no - arb
    print()
    print(title)
    print("-" * len(title))
    print(
        f"total={total}  BET_YES={yes} ({_pct(yes, total)})  "
        f"BET_NO={no} ({_pct(no, total)})  ARB={arb} ({_pct(arb, total)})  "
        f"other={other}"
    )


def print_group_table(title: str, groups: Dict[str, Counter], limit: int = 20) -> None:
    print()
    print(title)
    print("-" * len(title))
    if not groups:
        print("(none)")
        return
    rows = sorted(groups.items(), key=lambda item: sum(item[1].values()), reverse=True)
    for group, counts in rows[:limit]:
        total = sum(counts.values())
        yes = counts.get("BET_YES", 0)
        no = counts.get("BET_NO", 0)
        arb = counts.get("ARB", 0)
        print(
            f"{group:<35} n={total:>3}  "
            f"YES={yes:>3} ({_pct(yes, total):>6})  "
            f"NO={no:>3} ({_pct(no, total):>6})  "
            f"ARB={arb:>3}"
        )


def print_action_metrics(rows: List[dict], clean_settled: List[dict]) -> None:
    print()
    print("ACTION METRICS")
    print("--------------")
    all_actions = sorted(set(action_value(r) for r in rows) | set(action_value(r) for r in clean_settled))
    if not all_actions:
        print("(none)")
        return

    for action in all_actions:
        action_rows = [r for r in rows if action_value(r) == action]
        settled_rows = [r for r in clean_settled if action_value(r) == action]
        edges = [v for v in (edge_value(r) for r in action_rows) if v is not None]
        confs = [v for v in (confidence_value(r) for r in action_rows) if v is not None]
        wins = sum(1 for r in settled_rows if get_pnl(r) > 0)
        losses = sum(1 for r in settled_rows if get_pnl(r) < 0)
        pushes = sum(1 for r in settled_rows if get_pnl(r) == 0)
        pnl = sum(get_pnl(r) for r in settled_rows)
        denom = wins + losses
        wr = wins / denom if denom else None
        print(
            f"{action:<10} rows={len(action_rows):>3} settled={len(settled_rows):>3} "
            f"W={wins:>3} L={losses:>3} P={pushes:>3} "
            f"WR={wr * 100:>5.1f}% " if wr is not None else
            f"{action:<10} rows={len(action_rows):>3} settled={len(settled_rows):>3} "
            f"W={wins:>3} L={losses:>3} P={pushes:>3} WR=  n/a "
        , end="")
        print(
            f"PnL={_fmt_money(pnl):>9} "
            f"avg_edge={_fmt_num(mean(edges) if edges else None):>8} "
            f"avg_conf={_fmt_num(mean(confs) if confs else None, 3):>7}"
        )


def inspect_source_path() -> Dict[str, Any]:
    """Return static source-level observations about YES/NO path capability."""
    sources = {
        name: path.read_text()
        for name, path in SOURCE_FILES.items()
        if path.exists()
    }
    decision_engine = sources.get("decision_engine", "")
    paper_trader = sources.get("paper_trader", "")
    dashboard = sources.get("dashboard", "")
    market_scanner = sources.get("market_scanner", "")

    return {
        "decision_engine_has_bet_no_branch": 'action="BET_NO"' in decision_engine,
        "decision_engine_computes_no_edge": "no_edge = compute_edge(1.0 - model_prob, signal.price_no)" in decision_engine,
        "scanner_exports_decision_action": '"action": decision.action' in market_scanner,
        "dashboard_passes_intended_action_to_paper_trader": (
            "estimated_prob = opp.get(\"confidence\", 0.5)" in dashboard
            and "process_signal(" in dashboard
            and "intended_action=opp.get(\"action\")" in dashboard
        ),
        "dashboard_drops_scanner_action_before_paper_trader": (
            "estimated_prob = opp.get(\"confidence\", 0.5)" in dashboard
            and "process_signal(" in dashboard
            and "intended_action=opp.get(\"action\")" not in dashboard
        ),
        "paper_trader_honors_intended_action": (
            'scanner_action in ("BET_YES", "BET_NO")' in paper_trader
            and "action = scanner_action" in paper_trader
        ),
        "paper_trader_rederives_action_from_probability": (
            "if estimated_prob >= 0.5:" in paper_trader
            and 'action = "BET_YES"' in paper_trader
            and 'action = "BET_NO"' in paper_trader
        ),
        "model_probability_uses_yes_prior": "prior = signal.yes_mid if signal.yes_mid is not None else signal.price_yes" in decision_engine,
        "scanner_uses_yes_mid_price_history": "price_hist.append(yes_mid)" in market_scanner,
        "order_book_imbalance_hardcoded_zero": "order_book_imbalance=0.0" in market_scanner,
    }


def print_source_findings(findings: Dict[str, Any]) -> None:
    print()
    print("SOURCE PATH FINDINGS")
    print("--------------------")
    print(f"Decision engine has BET_NO branch:          {findings['decision_engine_has_bet_no_branch']}")
    print(f"Decision engine computes NO edge:           {findings['decision_engine_computes_no_edge']}")
    print(f"Scanner exports decision.action:            {findings['scanner_exports_decision_action']}")
    print(
        "Dashboard passes intended action to paper trader:   "
        f"{findings['dashboard_passes_intended_action_to_paper_trader']}"
    )
    print(
        "Dashboard drops scanner action before paper trader: "
        f"{findings['dashboard_drops_scanner_action_before_paper_trader']}"
    )
    print(
        "Paper trader honors intended action:                "
        f"{findings['paper_trader_honors_intended_action']}"
    )
    print(
        "Paper trader has legacy probability fallback:        "
        f"{findings['paper_trader_rederives_action_from_probability']}"
    )
    print(f"Model probability uses YES-side prior:       {findings['model_probability_uses_yes_prior']}")
    print(f"Scanner price history uses YES mid:          {findings['scanner_uses_yes_mid_price_history']}")
    print(f"Order book imbalance hardcoded to zero:      {findings['order_book_imbalance_hardcoded_zero']}")


def print_warnings(rows: List[dict], source_findings: Dict[str, Any]) -> None:
    counts = Counter(action_value(r) for r in rows)
    total = counts.get("BET_YES", 0) + counts.get("BET_NO", 0)
    yes = counts.get("BET_YES", 0)
    no = counts.get("BET_NO", 0)
    warnings = []
    if total and yes / total > 0.80:
        warnings.append("ONE_SIDED_ACTION_DISTRIBUTION")
    if no == 0:
        warnings.append("BET_NO_COUNT_ZERO")
    if source_findings["dashboard_drops_scanner_action_before_paper_trader"]:
        warnings.append("SCANNER_ACTION_DROPPED_BEFORE_PAPER_TRADER")
    if (
        source_findings["paper_trader_rederives_action_from_probability"]
        and not source_findings["paper_trader_honors_intended_action"]
    ):
        warnings.append("PAPER_TRADER_REDERIVES_SIDE")
    if source_findings["model_probability_uses_yes_prior"] and source_findings["scanner_uses_yes_mid_price_history"]:
        warnings.append("YES_SIDE_MARKET_PRICE_USED_AS_PRIOR_AND_SIGNAL_HISTORY")
    if source_findings["order_book_imbalance_hardcoded_zero"]:
        warnings.append("ORDER_BOOK_IMBALANCE_NOT_WIRED")

    print()
    print("WARNINGS")
    print("--------")
    for warning in warnings or ["OK_TO_WATCH"]:
        print(f"[!] {warning}" if warning != "OK_TO_WATCH" else warning)

    print()
    print("VERDICT")
    print("-------")
    if no == 0:
        print("BET_NO has not appeared in the trade log. Current realized evidence is 100% YES-side.")
    if source_findings["decision_engine_has_bet_no_branch"]:
        print("BET_NO is theoretically possible in decision_engine.py.")
    if source_findings["dashboard_drops_scanner_action_before_paper_trader"]:
        print("But the Dashboard -> PaperTrader handoff does not preserve scanner action as execution side.")
    if source_findings["paper_trader_honors_intended_action"]:
        print("PaperTrader honors valid intended_action values; legacy probability fallback remains for missing/invalid handoffs.")
    elif source_findings["paper_trader_rederives_action_from_probability"]:
        print("PaperTrader chooses BET_YES whenever side confidence is >= 0.5, which can turn scanner BET_NO confidence into BET_YES.")
    print("This report is diagnostic only. It does not prove edge and does not change trading.")


def main() -> None:
    records = load_trades()
    active_open, stale_open = classify_open_records(records)
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(records)
    clean_settled, conflicted_settled = classify_settled_records(
        records,
        settled_keys,
        forced_close_keys,
        void_keys,
    )
    terminal_rows = [
        r for r in records
        if str(r.get("status") or "").upper() in {"SETTLED", "FORCED_CLOSE", "VOID_LEGACY_DUPLICATE"}
    ]
    trade_rows = [r for r in records if action_value(r) in {"BET_YES", "BET_NO", "ARB"}]
    source_findings = inspect_source_path()

    print("=" * 86)
    print("AI_SYSTEM SIGNAL BIAS AUDIT")
    print("=" * 86)
    print("Read-only report. No trading, no signal changes, no log rewrites.")
    print()
    print(f"Total log rows:              {len(records)}")
    print(f"Trade-action rows:           {len(trade_rows)}")
    print(f"Active OPEN rows:            {len(active_open)}")
    print(f"Stale OPEN rows:             {len(stale_open)}")
    print(f"Terminal rows:               {len(terminal_rows)}")
    print(f"Clean outcome-known SETTLED: {len(clean_settled)}")
    print(f"Conflicted SETTLED excluded: {len(conflicted_settled)}")

    print_action_distribution("ACTION DISTRIBUTION - ALL TRADE ROWS", trade_rows)
    print_action_distribution("ACTION DISTRIBUTION - TERMINAL ROWS", terminal_rows)
    print_action_distribution("ACTION DISTRIBUTION - CLEAN SETTLED ONLY", clean_settled)

    print_group_table("ACTION BY STRATEGY", group_actions(trade_rows, strategy_value))
    print_group_table("ACTION BY TICKER FAMILY", group_actions(trade_rows, ticker_family))
    print_group_table("ACTION BY COUNCIL DECISION", group_actions(trade_rows, council_value))
    print_group_table("ACTION BY TRADE CLASS", group_actions(trade_rows, class_value))
    print_action_metrics(trade_rows, clean_settled)
    print_source_findings(source_findings)
    print_warnings(trade_rows, source_findings)


if __name__ == "__main__":
    main()
