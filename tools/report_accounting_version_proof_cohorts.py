#!/usr/bin/env python3
"""
Phase 9Q — Accounting-Version Proof Cohort Monitor
Sentinel: ACCOUNTING_VERSION_PROOF_COHORTS_REPORT_OK

Read-only report that separates legacy/hybrid paper rows from Phase 9N+
economic-accounting rows. This prevents mixed-accounting proof metrics from
looking cleaner than the underlying evidence.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import QUARANTINED_TICKER_PREFIXES
from tools.clean_truth_report import row_quality_group
from tools.performance_report import (
    build_terminal_key_sets,
    classify_open_records,
    classify_settled_records,
    is_side_coverage_record,
)

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"

KNOWN_COHORTS = (
    "legacy_hybrid_or_unversioned",
    "economic_contract_notional_v1",
    "time_exit_mark_to_market_v1",
    "unknown_other",
)
ECONOMIC_VERSION = "economic_contract_notional_v1"
TIME_EXIT_VERSION = "time_exit_mark_to_market_v1"
LEGACY_VERSION = "legacy_hybrid_or_unversioned"
MIN_PROOF_SAMPLE = 30
WATCH_SAMPLE = 50
SENTINEL = "ACCOUNTING_VERSION_PROOF_COHORTS_REPORT_OK"

_EXCLUDED_PREFIXES = tuple(str(p).upper() for p in QUARANTINED_TICKER_PREFIXES)


def load_trades(path: Path = TRADES_LOG) -> list[dict[str, Any]]:
    """Load JSONL paper records. Read-only; malformed lines are skipped."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line_no, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"  [WARN] skipped malformed line {line_no}: {exc}")
    return records


def classify_accounting_version(rec: dict[str, Any]) -> str:
    version = rec.get("accounting_version")
    if not version:
        return LEGACY_VERSION
    version = str(version)
    if version in (ECONOMIC_VERSION, TIME_EXIT_VERSION):
        return version
    return "unknown_other"


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _status(rec: dict[str, Any]) -> str:
    return str(rec.get("status") or "").upper()


def _result(rec: dict[str, Any]) -> str:
    return str(rec.get("result") or "").upper()


def is_kxeth_or_quarantined(rec: dict[str, Any]) -> bool:
    ticker = str(rec.get("ticker") or "").upper()
    return any(ticker.startswith(prefix) for prefix in _EXCLUDED_PREFIXES)


def entry_price(rec: dict[str, Any]) -> float | None:
    value = _as_float(rec.get("entry_price"))
    if value is not None:
        return value
    return _as_float(rec.get("yes_ask"))


def risk_edge(rec: dict[str, Any]) -> float | None:
    for field in ("risk_edge", "post_council_edge", "original_edge", "edge"):
        value = _as_float(rec.get(field))
        if value is not None:
            return value
    return None


def is_normal_modern(rec: dict[str, Any]) -> bool:
    return (
        row_quality_group(rec) == "MODERN_FULL_METADATA"
        and not rec.get("data_collection_override")
        and not rec.get("bootstrap_provisional")
        and not is_side_coverage_record(rec)
    )


def is_clean_proof_row(rec: dict[str, Any]) -> bool:
    return _status(rec) == "SETTLED" and is_normal_modern(rec) and not is_kxeth_or_quarantined(rec)


def economic_pnl_value(rec: dict[str, Any]) -> float | None:
    return _as_float(rec.get("economic_pnl"))


def recorded_pnl_value(rec: dict[str, Any]) -> float | None:
    return _as_float(rec.get("recorded_pnl"))


def stored_pnl_value(rec: dict[str, Any]) -> float | None:
    value = _as_float(rec.get("pnl"))
    if value is not None:
        return value
    return _as_float(rec.get("realized_pnl"))


def capital_at_risk_value(rec: dict[str, Any]) -> float | None:
    value = _as_float(rec.get("capital_at_risk"))
    if value is not None:
        return value
    if classify_accounting_version(rec) == ECONOMIC_VERSION:
        ep = entry_price(rec)
        size = _as_float(rec.get("size"))
        if ep is not None and size is not None:
            return ep * size
    return None


def payout_notional_value(rec: dict[str, Any]) -> float | None:
    value = _as_float(rec.get("payout_notional"))
    if value is not None:
        return value
    if classify_accounting_version(rec) == ECONOMIC_VERSION:
        return _as_float(rec.get("size"))
    return None


def is_sweet_spot_candidate(rec: dict[str, Any]) -> bool:
    ep = entry_price(rec)
    edge = risk_edge(rec)
    return (
        classify_accounting_version(rec) == ECONOMIC_VERSION
        and is_clean_proof_row(rec)
        and ep is not None
        and 0.80 <= ep <= 0.90
        and edge is not None
        and 0.05 <= edge <= 0.10
    )


def clean_settled_rows(records: list[dict[str, Any]]) -> set[int]:
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(records)
    clean, _conflicted = classify_settled_records(
        records, settled_keys, forced_close_keys, void_keys
    )
    return {id(rec) for rec in clean}


def active_open_rows(records: list[dict[str, Any]]) -> set[int]:
    active, _stale = classify_open_records(records)
    return {id(rec) for rec in active}


def _avg(values: Iterable[float]) -> float | None:
    vals = list(values)
    return sum(vals) / len(vals) if vals else None


def _sum(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def _profit_factor(rows: list[dict[str, Any]], pnl_getter) -> float | None:
    gross_wins = 0.0
    gross_losses = 0.0
    for rec in rows:
        pnl = pnl_getter(rec)
        if pnl is None:
            continue
        if pnl > 0:
            gross_wins += pnl
        elif pnl < 0:
            gross_losses += pnl
    if gross_wins <= 0 or gross_losses >= 0:
        return None
    return gross_wins / abs(gross_losses)


def cohort_metrics(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    clean_ids = clean_settled_rows(records)
    active_open_ids = active_open_rows(records)
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in KNOWN_COHORTS}
    for rec in records:
        grouped[classify_accounting_version(rec)].append(rec)

    metrics: dict[str, dict[str, Any]] = {}
    for cohort in KNOWN_COHORTS:
        rows = grouped[cohort]
        clean_rows = [r for r in rows if id(r) in clean_ids]
        settled_rows = [r for r in rows if _status(r) == "SETTLED"]
        active_open = [r for r in rows if id(r) in active_open_ids]
        normal_modern = [r for r in rows if is_normal_modern(r)]
        clean_proof = [r for r in clean_rows if is_clean_proof_row(r)]
        metric_rows = clean_proof
        wins = [r for r in metric_rows if _result(r) == "WIN"]
        losses = [r for r in metric_rows if _result(r) == "LOSS"]
        win_loss_n = len(wins) + len(losses)
        econ_total = _sum(economic_pnl_value(r) for r in metric_rows)
        recorded_total = _sum(recorded_pnl_value(r) for r in metric_rows)
        stored_total = _sum(stored_pnl_value(r) for r in metric_rows)
        capital_total = _sum(capital_at_risk_value(r) for r in metric_rows)
        econ_roi = (
            econ_total / capital_total
            if econ_total is not None and capital_total and capital_total > 0
            else None
        )
        clean_n = len(clean_proof)
        metrics[cohort] = {
            "cohort": cohort,
            "total_rows": len(rows),
            "settled_rows": len(settled_rows),
            "clean_settled_rows": len(clean_rows),
            "open_rows": len(active_open),
            "raw_open_rows": sum(1 for r in rows if _status(r) == "OPEN"),
            "forced_close_rows": sum(1 for r in rows if _status(r) == "FORCED_CLOSE"),
            "time_exit_rows": sum(1 for r in rows if _result(r) == "TIME_EXIT"),
            "void_rows": sum(1 for r in rows if _status(r) == "VOID_LEGACY_DUPLICATE" or _result(r) == "VOID"),
            "normal_modern_rows": len(normal_modern),
            "normal_modern_non_kxeth_rows": sum(1 for r in normal_modern if not is_kxeth_or_quarantined(r)),
            "clean_proof_rows": clean_n,
            "kxeth_rows": sum(1 for r in rows if is_kxeth_or_quarantined(r)),
            "rows_with_economic_pnl": sum(1 for r in rows if economic_pnl_value(r) is not None),
            "rows_with_recorded_pnl": sum(1 for r in rows if recorded_pnl_value(r) is not None),
            "rows_with_capital_at_risk": sum(1 for r in rows if capital_at_risk_value(r) is not None),
            "rows_with_payout_notional": sum(1 for r in rows if payout_notional_value(r) is not None),
            "rows_with_entry_price": sum(1 for r in rows if entry_price(r) is not None),
            "total_economic_pnl": econ_total,
            "total_recorded_pnl": recorded_total,
            "total_stored_pnl": stored_total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / win_loss_n if win_loss_n else None,
            "avg_entry_price": _avg(v for v in (entry_price(r) for r in metric_rows) if v is not None),
            "avg_capital_at_risk": _avg(v for v in (capital_at_risk_value(r) for r in metric_rows) if v is not None),
            "avg_payout_notional": _avg(v for v in (payout_notional_value(r) for r in metric_rows) if v is not None),
            "roi_on_capital_at_risk": econ_roi,
            "profit_factor_economic": _profit_factor(metric_rows, economic_pnl_value),
            "profit_factor_recorded": _profit_factor(metric_rows, recorded_pnl_value),
            "avg_max_profit_if_win": _avg(v for v in (_as_float(r.get("max_profit_if_win")) for r in metric_rows) if v is not None),
            "avg_max_loss_if_loss": _avg(v for v in (_as_float(r.get("max_loss_if_loss")) for r in metric_rows) if v is not None),
            "reward_risk": None,
            "sample_ge_30": clean_n >= MIN_PROOF_SAMPLE,
            "sample_ge_50": clean_n >= WATCH_SAMPLE,
            "minimum_sample_warning": clean_n < MIN_PROOF_SAMPLE,
            "too_small_to_trust": clean_n < MIN_PROOF_SAMPLE,
            "legacy_contaminated": cohort == LEGACY_VERSION,
            "fresh_proof_tracking": cohort == ECONOMIC_VERSION,
        }
        avg_profit = metrics[cohort]["avg_max_profit_if_win"]
        avg_loss = metrics[cohort]["avg_max_loss_if_loss"]
        if avg_profit is not None and avg_loss:
            metrics[cohort]["reward_risk"] = avg_profit / avg_loss
    return metrics


def sweet_spot_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in records if is_sweet_spot_candidate(r)]
    wins = [r for r in rows if _result(r) == "WIN"]
    losses = [r for r in rows if _result(r) == "LOSS"]
    econ_total = _sum(economic_pnl_value(r) for r in rows)
    capital_total = _sum(capital_at_risk_value(r) for r in rows)
    win_loss_n = len(wins) + len(losses)
    return {
        "count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / win_loss_n if win_loss_n else None,
        "economic_pnl": econ_total,
        "capital_at_risk": capital_total,
        "roi_on_capital_at_risk": (
            econ_total / capital_total
            if econ_total is not None and capital_total and capital_total > 0
            else None
        ),
        "sample_warning": len(rows) < MIN_PROOF_SAMPLE,
    }


def _fmt_num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "MISSING"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def _print_table(metrics: dict[str, dict[str, Any]]) -> None:
    print()
    print("COHORT SUMMARY")
    print("-" * 126)
    print(
        f"{'cohort':<34} {'rows':>5} {'settled':>7} {'open':>5} {'clean_nm':>8} "
        f"{'KXETH':>5} {'econ_pnl':>10} {'rec_pnl':>10} {'stored':>10} "
        f"{'ROI_cap':>8} {'WR':>7} {'PF_econ':>8} {'proof':>10}"
    )
    print("-" * 126)
    for cohort in KNOWN_COHORTS:
        m = metrics[cohort]
        if cohort == LEGACY_VERSION:
            proof = "LEGACY"
        elif m["sample_ge_30"]:
            proof = ">=30"
        else:
            proof = "TOO_SMALL"
        print(
            f"{cohort:<34} {m['total_rows']:>5} {m['settled_rows']:>7} {m['open_rows']:>5} "
            f"{m['clean_proof_rows']:>8} {m['kxeth_rows']:>5} "
            f"{_fmt_money(m['total_economic_pnl']):>10} {_fmt_money(m['total_recorded_pnl']):>10} "
            f"{_fmt_money(m['total_stored_pnl']):>10} {_fmt_pct(m['roi_on_capital_at_risk']):>8} "
            f"{_fmt_pct(m['win_rate']):>7} {_fmt_num(m['profit_factor_economic']):>8} {proof:>10}"
        )


def _print_details(metrics: dict[str, dict[str, Any]]) -> None:
    print()
    print("FIELD COVERAGE / PROOF QUALITY")
    print("-" * 78)
    for cohort in KNOWN_COHORTS:
        m = metrics[cohort]
        print(f"\n{cohort}")
        print(f"  clean settled rows:              {m['clean_settled_rows']}")
        print(f"  normal_modern rows:              {m['normal_modern_rows']}")
        print(f"  normal_modern non-KXETH rows:    {m['normal_modern_non_kxeth_rows']}")
        print(f"  rows with economic_pnl:          {m['rows_with_economic_pnl']}")
        print(f"  rows with recorded_pnl:          {m['rows_with_recorded_pnl']}")
        print(f"  rows with capital_at_risk:       {m['rows_with_capital_at_risk']}")
        print(f"  rows with payout_notional:       {m['rows_with_payout_notional']}")
        print(f"  rows with entry_price/yes_ask:   {m['rows_with_entry_price']}")
        print(f"  avg entry_price:                 {_fmt_num(m['avg_entry_price'], 4)}")
        print(f"  avg capital_at_risk:             {_fmt_money(m['avg_capital_at_risk'])}")
        print(f"  avg payout_notional:             {_fmt_money(m['avg_payout_notional'])}")
        print(f"  avg max_profit_if_win:           {_fmt_money(m['avg_max_profit_if_win'])}")
        print(f"  avg max_loss_if_loss:            {_fmt_money(m['avg_max_loss_if_loss'])}")
        print(f"  reward/risk:                     {_fmt_num(m['reward_risk'], 4)}")
        print(f"  sample >= 30:                    {'YES' if m['sample_ge_30'] else 'NO'}")
        print(f"  sample >= 50:                    {'YES' if m['sample_ge_50'] else 'NO'}")
        print(f"  too small to trust:              {'YES' if m['too_small_to_trust'] else 'NO'}")
        print(f"  legacy contaminated:             {'YES' if m['legacy_contaminated'] else 'NO'}")
        print(f"  suitable for fresh proof track:  {'YES' if m['fresh_proof_tracking'] else 'NO'}")


def _print_sweet_spot(sweet: dict[str, Any]) -> None:
    print()
    print("PHASE 9N ECONOMIC SWEET-SPOT TRACKING")
    print("-" * 78)
    print("Criteria: accounting_version=economic_contract_notional_v1, settled")
    print("normal_modern, non-KXETH, entry_price 0.80-0.90, edge 0.05-0.10.")
    print()
    print(f"  count:                 {sweet['count']}")
    print(f"  wins/losses:           {sweet['wins']} / {sweet['losses']}")
    print(f"  win rate:              {_fmt_pct(sweet['win_rate'])}")
    print(f"  economic_pnl:          {_fmt_money(sweet['economic_pnl'])}")
    print(f"  capital_at_risk:       {_fmt_money(sweet['capital_at_risk'])}")
    print(f"  ROI on capital_at_risk:{_fmt_pct(sweet['roi_on_capital_at_risk']):>9}")
    print(f"  sample warning:        {'YES - sample < 30' if sweet['sample_warning'] else 'NO'}")


def main() -> None:
    records = load_trades()
    metrics = cohort_metrics(records)
    sweet = sweet_spot_metrics(records)

    print("=" * 78)
    print("ACCOUNTING-VERSION PROOF COHORT MONITOR")
    print("=" * 78)
    print("Read-only: no logs, profiles, gates, thresholds, or trading behavior are modified.")
    print("Purpose: separate legacy hybrid rows from Phase 9N economic-accounting proof.")
    print()
    print(f"Source: {TRADES_LOG}")
    print(f"Raw records loaded: {len(records)}")

    _print_table(metrics)
    _print_details(metrics)
    _print_sweet_spot(sweet)

    econ = metrics[ECONOMIC_VERSION]
    print()
    print("VERDICT")
    print("-" * 78)
    if econ["clean_proof_rows"] < MIN_PROOF_SAMPLE:
        print(
            "Fresh economic-accounting proof is NOT ENOUGH DATA: "
            f"{econ['clean_proof_rows']}/{MIN_PROOF_SAMPLE} clean settled normal_modern non-KXETH rows."
        )
    else:
        print("Fresh economic-accounting cohort has minimum sample floor; still requires ROI/CLV/PF proof checks.")
    print("Legacy rows remain diagnostic only for Phase 9N proof. Do not mix them into fresh proof.")
    print("Scale allowed: NO. Real money allowed: NO. Proof verdict should remain WATCHLIST.")
    print()
    print(f"Sentinel: {SENTINEL}")


if __name__ == "__main__":
    main()
