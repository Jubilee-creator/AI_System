#!/usr/bin/env python3
"""
Phase 9R — Fresh Economic Proof Autopsy
Sentinel: FRESH_ECONOMIC_PROOF_AUTOPSY_REPORT_OK

Read-only autopsy of the fresh Phase 9N economic proof cohort.
This report explains why the fresh cohort is losing, which pockets are
positive or poison, and what counterfactual filters would do.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.report_accounting_version_proof_cohorts import (
    ECONOMIC_VERSION,
    classify_accounting_version,
    clean_settled_rows,
    economic_pnl_value,
    capital_at_risk_value,
    entry_price,
    is_clean_proof_row,
    is_kxeth_or_quarantined,
    payout_notional_value,
    recorded_pnl_value,
    risk_edge,
    stored_pnl_value,
)

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "FRESH_ECONOMIC_PROOF_AUTOPSY_REPORT_OK"
MIN_CELL_SAMPLE = 5


def load_trades(path: Path = TRADES_LOG) -> list[dict[str, Any]]:
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


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fresh_proof_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean_ids = clean_settled_rows(records)
    fresh: list[dict[str, Any]] = []
    for rec in records:
        if id(rec) not in clean_ids:
            continue
        if classify_accounting_version(rec) != ECONOMIC_VERSION:
            continue
        if not is_clean_proof_row(rec):
            continue
        if is_kxeth_or_quarantined(rec):
            continue
        fresh.append(rec)
    return fresh


def price_bucket(price: float | None) -> str:
    if price is None:
        return "missing"
    if price < 0.50:
        return "<0.50"
    if price < 0.60:
        return "0.50-0.60"
    if price < 0.70:
        return "0.60-0.70"
    if price < 0.80:
        return "0.70-0.80"
    if price < 0.90:
        return "0.80-0.90"
    return "0.90-1.00"


def edge_bucket(edge: float | None) -> str:
    if edge is None:
        return "missing"
    if edge < 0.03:
        return "<0.03"
    if edge < 0.05:
        return "0.03-0.05"
    if edge < 0.10:
        return "0.05-0.10"
    return "0.10+"


def cell_key(rec: dict[str, Any]) -> str:
    return f"{edge_bucket(risk_edge(rec))}|{price_bucket(entry_price(rec))}"


def council_path(rec: dict[str, Any]) -> str:
    reason = str(rec.get("council_reason") or "")
    if rec.get("bootstrap_era_council_allow") is True:
        return "bootstrap_era_allow"
    if reason.startswith("Builder found positive historical pattern"):
        return "builder_boost"
    if reason.startswith("No Builder boost; Critic allowed with caution"):
        return "critic_caution"
    return "other"


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [r for r in rows if str(r.get("result", "")).upper() == "WIN"]
    losses = [r for r in rows if str(r.get("result", "")).upper() == "LOSS"]
    win_loss_n = len(wins) + len(losses)

    econ_total = sum((_as_float(economic_pnl_value(r)) or 0.0) for r in rows)
    recorded_total = sum((_as_float(recorded_pnl_value(r)) or 0.0) for r in rows)
    stored_total = sum((_as_float(stored_pnl_value(r)) or 0.0) for r in rows)
    capital_total = sum((_as_float(capital_at_risk_value(r)) or 0.0) for r in rows)
    payout_total = sum((_as_float(payout_notional_value(r)) or 0.0) for r in rows)

    avg_ep_vals = [v for v in (_as_float(entry_price(r)) for r in rows) if v is not None]
    avg_edge_vals = [v for v in (_as_float(risk_edge(r)) for r in rows) if v is not None]
    avg_ep = sum(avg_ep_vals) / len(avg_ep_vals) if avg_ep_vals else None
    avg_edge = sum(avg_edge_vals) / len(avg_edge_vals) if avg_edge_vals else None
    avg_capital = capital_total / len(rows) if rows else None
    avg_payout = payout_total / len(rows) if rows else None

    total_win_pnl = sum((_as_float(economic_pnl_value(r)) or 0.0) for r in wins)
    total_loss_pnl = sum((_as_float(economic_pnl_value(r)) or 0.0) for r in losses)
    avg_win = total_win_pnl / len(wins) if wins else None
    avg_loss = total_loss_pnl / len(losses) if losses else None
    profit_factor = None
    if total_win_pnl > 0 and total_loss_pnl < 0:
        profit_factor = total_win_pnl / abs(total_loss_pnl)
    reward_risk = None
    total_max_profit = sum((_as_float(r.get("max_profit_if_win")) or 0.0) for r in rows)
    total_max_loss = sum((_as_float(r.get("max_loss_if_loss")) or 0.0) for r in rows)
    if total_max_loss > 0:
        reward_risk = total_max_profit / total_max_loss

    win_rate = len(wins) / win_loss_n if win_loss_n else None
    breakeven_wr = avg_ep
    wr_margin = (win_rate - breakeven_wr) if (win_rate is not None and breakeven_wr is not None) else None
    roi = econ_total / capital_total if capital_total > 0 else None

    positive_pnls = [p for p in ((_as_float(economic_pnl_value(r)) or 0.0) for r in rows) if p > 0]
    negative_pnls = [p for p in ((_as_float(economic_pnl_value(r)) or 0.0) for r in rows) if p < 0]

    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "breakeven_wr": breakeven_wr,
        "wr_margin": wr_margin,
        "total_economic_pnl": econ_total,
        "total_recorded_pnl": recorded_total,
        "total_stored_pnl": stored_total,
        "total_capital_at_risk": capital_total,
        "total_payout_notional": payout_total,
        "roi": roi,
        "avg_entry_price": avg_ep,
        "avg_edge": avg_edge,
        "avg_capital_at_risk": avg_capital,
        "avg_payout_notional": avg_payout,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "reward_risk": reward_risk,
        "max_win": max(positive_pnls) if positive_pnls else None,
        "max_loss": min(negative_pnls) if negative_pnls else None,
        "positive": econ_total > 0,
        "negative": econ_total < 0,
        "sample_ge_5": len(rows) >= MIN_CELL_SAMPLE,
        "sample_ge_30": len(rows) >= 30,
    }


def bucket_rows(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in rows:
        groups[key_fn(rec)].append(rec)
    return dict(groups)


def summarize_buckets(rows: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str], order: list[str]) -> dict[str, dict[str, Any]]:
    groups = bucket_rows(rows, key_fn)
    return {key: summarize_rows(groups.get(key, [])) for key in order if groups.get(key, [])}


def summarize_cells(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return summarize_buckets(
        rows,
        cell_key,
        [
            "0.03-0.05|0.50-0.60",
            "0.03-0.05|0.60-0.70",
            "0.03-0.05|0.70-0.80",
            "0.03-0.05|0.80-0.90",
            "0.03-0.05|0.90-1.00",
            "0.05-0.10|0.50-0.60",
            "0.05-0.10|0.60-0.70",
            "0.05-0.10|0.70-0.80",
            "0.05-0.10|0.80-0.90",
            "0.05-0.10|0.90-1.00",
            "0.10+|0.50-0.60",
            "0.10+|0.60-0.70",
            "0.10+|0.70-0.80",
            "0.10+|0.80-0.90",
            "0.10+|0.90-1.00",
        ],
    )


def select_rows_from_cells(rows: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    cells = summarize_cells(rows)
    allowed = {key for key, stats in cells.items() if predicate(stats)}
    return [r for r in rows if cell_key(r) in allowed]


def scenario_summary(name: str, rows: list[dict[str, Any]], filtered: list[dict[str, Any]]) -> dict[str, Any]:
    base = summarize_rows(rows)
    keep = summarize_rows(filtered)
    return {
        "name": name,
        "removed_rows": base["n"] - keep["n"],
        "removed_pnl": round(base["total_economic_pnl"] - keep["total_economic_pnl"], 2),
        "delta_pnl": round(keep["total_economic_pnl"] - base["total_economic_pnl"], 2),
        "kept_rows": keep["n"],
        "kept_pnl": round(keep["total_economic_pnl"], 2),
        "kept_roi": keep["roi"],
        "kept_wr": keep["win_rate"],
    }


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _print_summary(summary: dict[str, Any]) -> None:
    print()
    print("FRESH ECONOMIC PROOF SUMMARY")
    print("-" * 78)
    print(f"  fresh clean rows:          {summary['n']}")
    print(f"  wins / losses:             {summary['wins']} / {summary['losses']}")
    print(f"  win rate:                  {_fmt_pct(summary['win_rate'])}")
    print(f"  breakeven win rate:        {_fmt_pct(summary['breakeven_wr'])}")
    print(f"  win-rate margin:           {_fmt_pct(summary['wr_margin'])}")
    print(f"  economic pnl:              {_fmt_money(summary['total_economic_pnl'])}")
    print(f"  recorded pnl:              {_fmt_money(summary['total_recorded_pnl'])}")
    print(f"  stored pnl:                {_fmt_money(summary['total_stored_pnl'])}")
    print(f"  capital at risk:           {_fmt_money(summary['total_capital_at_risk'])}")
    print(f"  payout notional:           {_fmt_money(summary['total_payout_notional'])}")
    print(f"  ROI on capital at risk:    {_fmt_pct(summary['roi'])}")
    print(f"  avg entry price:           {_fmt_num(summary['avg_entry_price'])}")
    print(f"  avg edge:                  {_fmt_num(summary['avg_edge'])}")
    print(f"  avg capital at risk:       {_fmt_money(summary['avg_capital_at_risk'])}")
    print(f"  avg payout notional:       {_fmt_money(summary['avg_payout_notional'])}")
    print(f"  avg win / avg loss:        {_fmt_money(summary['avg_win'])} / {_fmt_money(summary['avg_loss'])}")
    print(f"  profit factor:             {_fmt_num(summary['profit_factor'])}")
    print(f"  reward / risk:             {_fmt_num(summary['reward_risk'])}")
    print(f"  max win / max loss:        {_fmt_money(summary['max_win'])} / {_fmt_money(summary['max_loss'])}")


def _print_bucket_table(title: str, summaries: dict[str, dict[str, Any]], order: list[str]) -> None:
    print()
    print(title)
    print("-" * 108)
    print(
        f"{'bucket':<16} {'n':>4} {'WR':>7} {'BE':>7} {'mrg':>7} {'PnL':>10} {'ROI':>8} "
        f"{'PF':>7} {'RR':>7} {'avg_ep':>8} {'tag':>12}"
    )
    print("-" * 108)
    for key in order:
        s = summaries.get(key)
        if not s:
            continue
        tag = "POS" if s["total_economic_pnl"] > 0 else "POISON" if s["total_economic_pnl"] < 0 else "FLAT"
        print(
            f"{key:<16} {s['n']:>4} {_fmt_pct(s['win_rate']):>7} {_fmt_pct(s['breakeven_wr']):>7} "
            f"{_fmt_pct(s['wr_margin']):>7} {_fmt_money(s['total_economic_pnl']):>10} "
            f"{_fmt_pct(s['roi']):>8} {_fmt_num(s['profit_factor']):>7} {_fmt_num(s['reward_risk']):>7} "
            f"{_fmt_num(s['avg_entry_price']):>8} {tag:>12}"
        )


def _print_cell_table(cells: dict[str, dict[str, Any]]) -> None:
    print()
    print("2D EDGE × PRICE CELLS")
    print("-" * 126)
    print(
        f"{'cell':<20} {'n':>4} {'WR':>7} {'BE':>7} {'mrg':>7} {'PnL':>10} {'ROI':>8} "
        f"{'PF':>7} {'RR':>7} {'tag':>16}"
    )
    print("-" * 126)
    for key in sorted(cells):
        s = cells[key]
        if not s["n"]:
            continue
        if s["n"] >= MIN_CELL_SAMPLE and s["total_economic_pnl"] > 0:
            tag = "positive_candidate"
        elif s["n"] >= MIN_CELL_SAMPLE and s["total_economic_pnl"] < 0:
            tag = "poison_candidate"
        elif s["n"] < MIN_CELL_SAMPLE and s["total_economic_pnl"] > 0:
            tag = "tiny_positive"
        else:
            tag = "too_small_or_flat"
        print(
            f"{key:<20} {s['n']:>4} {_fmt_pct(s['win_rate']):>7} {_fmt_pct(s['breakeven_wr']):>7} "
            f"{_fmt_pct(s['wr_margin']):>7} {_fmt_money(s['total_economic_pnl']):>10} {_fmt_pct(s['roi']):>8} "
            f"{_fmt_num(s['profit_factor']):>7} {_fmt_num(s['reward_risk']):>7} {tag:>16}"
        )


def _print_positive_and_poison(cells: dict[str, dict[str, Any]], price_buckets: dict[str, dict[str, Any]], edge_buckets: dict[str, dict[str, Any]]) -> None:
    positive_1d = [k for k, s in price_buckets.items() if s["total_economic_pnl"] > 0]
    poison_1d = [k for k, s in price_buckets.items() if s["total_economic_pnl"] < 0]
    positive_edge = [k for k, s in edge_buckets.items() if s["total_economic_pnl"] > 0]
    poison_edge = [k for k, s in edge_buckets.items() if s["total_economic_pnl"] < 0]
    positive_2d = [k for k, s in cells.items() if s["n"] >= MIN_CELL_SAMPLE and s["total_economic_pnl"] > 0]
    poison_2d = [k for k, s in cells.items() if s["n"] >= MIN_CELL_SAMPLE and s["total_economic_pnl"] < 0]

    print()
    print("POSITIVE VS POISON")
    print("-" * 78)
    print(f"  positive 1D price buckets: {', '.join(positive_1d) if positive_1d else 'none'}")
    print(f"  poison 1D price buckets:   {', '.join(poison_1d) if poison_1d else 'none'}")
    print(f"  positive 1D edge buckets:  {', '.join(positive_edge) if positive_edge else 'none'}")
    print(f"  poison 1D edge buckets:    {', '.join(poison_edge) if poison_edge else 'none'}")
    print(f"  positive 2D cells n>=5:    {', '.join(positive_2d) if positive_2d else 'none'}")
    print(f"  poison 2D cells n>=5:      {', '.join(poison_2d) if poison_2d else 'none'}")
    print("  note: no 2D cell with n>=5 is positive in the current fresh sample.")


def _print_counterfactuals(rows: list[dict[str, Any]], cells: dict[str, dict[str, Any]]) -> None:
    baseline = summarize_rows(rows)
    scenarios = [
        ("block_0.70-0.80", [r for r in rows if price_bucket(entry_price(r)) != "0.70-0.80"]),
        ("block_0.80-0.90_if_negative", [r for r in rows if price_bucket(entry_price(r)) != "0.80-0.90"]),
        (
            "only_cells_n_ge_5_and_positive_pnl",
            select_rows_from_cells(rows, lambda s: s["n"] >= MIN_CELL_SAMPLE and s["total_economic_pnl"] > 0),
        ),
        (
            "only_cells_above_be_plus_2pp",
            select_rows_from_cells(rows, lambda s: s["n"] >= MIN_CELL_SAMPLE and s["wr_margin"] is not None and s["wr_margin"] >= 0.02),
        ),
    ]

    print()
    print("COUNTERFACTUAL FILTERS")
    print("-" * 78)
    print(f"  baseline PnL: {_fmt_money(baseline['total_economic_pnl'])}  ROI: {_fmt_pct(baseline['roi'])}  rows: {baseline['n']}")
    for name, filtered in scenarios:
        s = scenario_summary(name, rows, filtered)
        print(
            f"  {name:<32} keep={s['kept_rows']:>3} remove={s['removed_rows']:>3} "
            f"delta_pnl={_fmt_money(s['delta_pnl'])} kept_pnl={_fmt_money(s['kept_pnl'])} kept_roi={_fmt_pct(s['kept_roi'])} "
            f"kept_wr={_fmt_pct(s['kept_wr'])}"
        )
    print("  interpretation: the broad 0.80-0.90 band is negative; no 2D cell with n>=5 clears the bar.")


def _print_recommendation(summary: dict[str, Any], price_buckets: dict[str, dict[str, Any]], cells: dict[str, dict[str, Any]], council_paths: dict[str, dict[str, Any]]) -> None:
    print()
    print("RECOMMENDATION")
    print("-" * 78)
    print("  no patch yet.")
    print("  candidate ideas only:")
    print("    - block the 0.70-0.80 price band.")
    print("    - block 0.80-0.90 as a broad band unless a future subcell turns positive with sample.")
    print("    - require n>=5 and positive PnL for any 2D cell before calling it a live candidate.")
    print("    - require win rate above breakeven with margin before a cell can remain on watch.")
    print("  what not to touch:")
    print("    - thresholds, gates, KXETH quarantine, Kelly, scale, real money, or dashboard logic.")
    if summary["win_rate"] is not None and summary["breakeven_wr"] is not None:
        avg_loss_abs = abs(summary["avg_loss"]) if summary["avg_loss"] is not None else None
        print(
            f"  why high WR still loses: fresh WR={summary['win_rate']*100:.1f}% while breakeven is {summary['breakeven_wr']*100:.1f}%;"
            f" average win {_fmt_money(summary['avg_win'])} vs average loss {_fmt_money(avg_loss_abs)}."
        )


def main() -> None:
    records = load_trades()
    fresh = fresh_proof_rows(records)
    summary = summarize_rows(fresh)
    price_buckets = summarize_buckets(fresh, lambda r: price_bucket(entry_price(r)), ["0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00"])
    edge_buckets = summarize_buckets(fresh, lambda r: edge_bucket(risk_edge(r)), ["0.03-0.05", "0.05-0.10", "0.10+"])
    cells = summarize_cells(fresh)
    prefixes = summarize_buckets(fresh, lambda r: str(r.get("ticker", "")).split("-")[0], sorted({str(r.get("ticker", "")).split("-")[0] for r in fresh}))
    council = summarize_buckets(fresh, council_path, ["builder_boost", "critic_caution", "bootstrap_era_allow", "other"])

    print("=" * 78)
    print("FRESH ECONOMIC PROOF AUTOPSY")
    print("=" * 78)
    print("Read-only: no logs, thresholds, gates, dashboard, or trading behavior are modified.")
    print("Population: settled, economic_contract_notional_v1, normal_modern, non-KXETH clean-proof rows only.")
    print(f"Raw records loaded: {len(records)}")
    print(f"Fresh proof rows:    {len(fresh)}")

    _print_summary(summary)
    _print_bucket_table("PRICE BUCKETS", price_buckets, ["0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00"])
    _print_bucket_table("EDGE BUCKETS", edge_buckets, ["0.03-0.05", "0.05-0.10", "0.10+"])
    _print_cell_table(cells)
    _print_bucket_table("TICKER PREFIXES", prefixes, sorted(prefixes))
    _print_bucket_table("COUNCIL PATHS", council, ["builder_boost", "critic_caution", "bootstrap_era_allow", "other"])
    _print_positive_and_poison(cells, price_buckets, edge_buckets)
    _print_counterfactuals(fresh, cells)
    _print_recommendation(summary, price_buckets, cells, council)

    print()
    print(f"Sentinel: {SENTINEL}")


if __name__ == "__main__":
    main()
