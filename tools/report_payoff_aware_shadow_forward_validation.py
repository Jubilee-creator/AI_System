#!/usr/bin/env python3
"""
Phase 10G - Payoff-Aware Shadow Ranking Forward Validation
Sentinel: PAYOFF_AWARE_SHADOW_FORWARD_VALIDATION_OK

Read-only report for logs/payoff_aware_shadow_ranking.jsonl.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (  # noqa: E402
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    QUARANTINED_TICKER_PREFIXES,
    TRADING_MODE,
)
from logs.payoff_aware_shadow_ranking_logger import LOG_PATH as SHADOW_LOG  # noqa: E402
from tools.report_accounting_version_proof_cohorts import economic_pnl_value, load_trades  # noqa: E402
from tools.report_payoff_geometry_candidate_quality_autopsy import (  # noqa: E402
    TRADES_LOG,
    action_of,
    clean_fresh_rows,
    side_entry_price,
    summarize_settled_rows,
)

SENTINEL = "PAYOFF_AWARE_SHADOW_FORWARD_VALIDATION_OK"


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _fmt_money(value: float | None) -> str:
    return "MISSING" if value is None else f"${value:+.2f}"


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_shadow_rows(path: Path = SHADOW_LOG) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _avg(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def _pick_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    picks: list[dict[str, Any]] = []
    for row in rows:
        raw = row.get(key) or []
        if isinstance(raw, list):
            picks.extend(item for item in raw if isinstance(item, dict))
    return picks


def _summarize_pick_rows(rows: list[dict[str, Any]], total_scans: int) -> dict[str, Any]:
    count = len(rows)
    return {
        "count": count,
        "target_count": total_scans * 3,
        "starvation_count": max(0, total_scans * 3 - count),
        "starvation_rate": (max(0, total_scans * 3 - count) / (total_scans * 3)) if total_scans else None,
        "avg_entry": _avg([_safe_float(row.get("entry_price")) for row in rows]),
        "avg_reward_risk": _avg([_safe_float(row.get("reward_risk")) for row in rows]),
        "avg_model_margin": _avg([_safe_float(row.get("model_margin")) for row in rows]),
        "expensive_entry_rate": _rate(sum(1 for row in rows if row.get("expensive_entry")), count),
        "weak_reward_risk_rate": _rate(sum(1 for row in rows if row.get("weak_reward_risk")), count),
        "toxic_80_90_rate": _rate(sum(1 for row in rows if row.get("toxic_80_90")), count),
        "model_edge_bad_geometry_rate": _rate(sum(1 for row in rows if row.get("model_edge_bad_geometry")), count),
        "action_mix": dict(Counter(str(row.get("action") or "UNKNOWN") for row in rows).most_common()),
    }


def _rate(count: int, total: int) -> float | None:
    return count / total if total else None


def _trade_key_from_pick(row: dict[str, Any]) -> tuple[str, str, float | None]:
    price = _safe_float(row.get("entry_price"))
    return (str(row.get("ticker") or ""), str(row.get("action") or "UNKNOWN"), round(price, 6) if price is not None else None)


def _trade_key_from_trade(row: dict[str, Any]) -> tuple[str, str, float | None]:
    price = side_entry_price(row)
    return (str(row.get("ticker") or ""), action_of(row), round(price, 6) if price is not None else None)


def _matched_settled(picks: list[dict[str, Any]], fresh_rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = {_trade_key_from_pick(row) for row in picks}
    matched = [row for row in fresh_rows if _trade_key_from_trade(row) in keys]
    return {
        "matched_rows": len(matched),
        "summary": summarize_settled_rows(matched),
        "has_mature_outcomes": len(matched) >= 30,
    }


def build_report(
    shadow_path: Path = SHADOW_LOG,
    trades_path: Path = TRADES_LOG,
) -> dict[str, Any]:
    rows = read_shadow_rows(shadow_path)
    trades = load_trades(trades_path)
    fresh_rows = clean_fresh_rows(trades)

    current = _pick_rows(rows, "current_top_3")
    payoff = _pick_rows(rows, "payoff_aware_top_3")
    strict = _pick_rows(rows, "strict_payoff_top_3")

    current_summary = _summarize_pick_rows(current, len(rows))
    payoff_summary = _summarize_pick_rows(payoff, len(rows))
    strict_summary = _summarize_pick_rows(strict, len(rows))

    latest_ts = rows[-1].get("timestamp_utc") if rows else None
    shadow_only_ok = all(row.get("shadow_only") is True for row in rows) if rows else True
    execution_changed = any(row.get("execution_changed") is True for row in rows)

    return {
        "paths": {
            "shadow": str(shadow_path),
            "paper_trades": str(trades_path),
        },
        "log_exists": shadow_path.exists(),
        "rows_logged": len(rows),
        "latest_timestamp": latest_ts,
        "shadow_logging_active": bool(rows),
        "shadow_only_ok": shadow_only_ok,
        "execution_changed": execution_changed,
        "avg_overlap_current_payoff": _avg([_safe_float(row.get("overlap_current_payoff")) for row in rows]),
        "avg_overlap_current_strict": _avg([_safe_float(row.get("overlap_current_strict")) for row in rows]),
        "current": current_summary,
        "payoff_aware": payoff_summary,
        "strict": strict_summary,
        "settled_matches": {
            "current": _matched_settled(current, fresh_rows),
            "payoff_aware": _matched_settled(payoff, fresh_rows),
            "strict": _matched_settled(strict, fresh_rows),
        },
        "evidence_maturity": {
            "fresh_clean_rows": len(fresh_rows),
            "mature_shadow_rows": len(rows) >= 30,
            "mature_outcomes": any(
                mode["has_mature_outcomes"]
                for mode in (
                    _matched_settled(current, fresh_rows),
                    _matched_settled(payoff, fresh_rows),
                    _matched_settled(strict, fresh_rows),
                )
            ),
            "verdict": "NOT_MATURE_YET" if len(rows) < 30 else "SHADOW_ROWS_AVAILABLE_OUTCOMES_MAY_STILL_LAG",
        },
        "safety": {
            "trading_mode": TRADING_MODE,
            "paper_only": TRADING_MODE == "PAPER",
            "real_money_allowed": False,
            "scale_allowed": False,
            "kelly_execution_disabled": GLOBAL_FORCED_LEARNING_MODE,
            "dc_override_enabled": DATA_COLLECTION_OVERRIDE_ENABLED,
            "kxeth_quarantine_active": "KXETH" in {str(prefix).upper() for prefix in QUARANTINED_TICKER_PREFIXES},
        },
    }


def _print_summary(label: str, stats: dict[str, Any]) -> None:
    print(
        f"{label:<14} picks={stats['count']:>7,} avg_ep={_fmt_num(stats['avg_entry'])} "
        f"rr={_fmt_num(stats['avg_reward_risk'])} m-be={_fmt_num(stats['avg_model_margin'])} "
        f"exp={_fmt_pct(stats['expensive_entry_rate'])} weakRR={_fmt_pct(stats['weak_reward_risk_rate'])} "
        f"toxic80={_fmt_pct(stats['toxic_80_90_rate'])} badGeom={_fmt_pct(stats['model_edge_bad_geometry_rate'])} "
        f"starved={_fmt_pct(stats['starvation_rate'])}"
    )


def print_report(report: dict[str, Any]) -> None:
    print("=" * 98)
    print("PAYOFF-AWARE SHADOW FORWARD VALIDATION")
    print("=" * 98)
    print("Read-only: no execution, ranking, thresholds, risk, strategy, or live-money state is modified.")
    print(f"shadow log:              {report['paths']['shadow']}")
    print(f"log exists:              {report['log_exists']}")
    print(f"rows logged:             {_fmt_int(report['rows_logged'])}")
    print(f"latest timestamp:        {report['latest_timestamp'] or 'MISSING'}")
    print(f"shadow logging active:   {report['shadow_logging_active']}")
    print(f"shadow only ok:          {report['shadow_only_ok']}")
    print(f"execution changed:       {report['execution_changed']}")
    print(f"avg overlap current/payoff: {_fmt_pct(report['avg_overlap_current_payoff'])}")
    print(f"avg overlap current/strict: {_fmt_pct(report['avg_overlap_current_strict'])}")

    print()
    print("GEOMETRY COMPARISON")
    print("-------------------")
    _print_summary("current", report["current"])
    _print_summary("payoff", report["payoff_aware"])
    _print_summary("strict", report["strict"])
    print(f"current action mix: {report['current']['action_mix']}")
    print(f"payoff action mix:  {report['payoff_aware']['action_mix']}")
    print(f"strict action mix:  {report['strict']['action_mix']}")

    print()
    print("SETTLED MATCHED PERFORMANCE")
    print("---------------------------")
    for key in ("current", "payoff_aware", "strict"):
        matched = report["settled_matches"][key]
        summary = matched["summary"]
        print(
            f"{key:<14} matched={matched['matched_rows']:>4,} mature={matched['has_mature_outcomes']} "
            f"WR={_fmt_pct(summary.get('win_rate'))} ROI={_fmt_pct(summary.get('roi'))} "
            f"PF={_fmt_num(summary.get('profit_factor'))} PnL={_fmt_money(summary.get('total_economic_pnl'))}"
        )

    print()
    print("EVIDENCE MATURITY")
    print("-----------------")
    for key, value in report["evidence_maturity"].items():
        print(f"{key:<28} {value}")

    print()
    print("SAFETY LOCKS")
    print("------------")
    for key, value in report["safety"].items():
        print(f"{key:<30} {value}")

    print()
    print(f"Sentinel: {SENTINEL}")


def main() -> int:
    print_report(build_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
