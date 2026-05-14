#!/usr/bin/env python3
"""
Phase 10K - Forward Shadow Outcome Validation
Sentinel: FORWARD_SHADOW_OUTCOME_VALIDATION_OK

Read-only forward validation of current, payoff-aware, and strict payoff-aware
shadow selections against real settled paper outcomes.  Historical proof before
the shadow logger started is not used as shadow proof.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
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
from tools import report_calibration_payoff_truth_map as calib_map  # noqa: E402
from tools.report_accounting_version_proof_cohorts import (  # noqa: E402
    ECONOMIC_VERSION,
    classify_accounting_version,
    economic_pnl_value,
    is_clean_proof_row,
    is_kxeth_or_quarantined,
    load_trades,
)
from tools.report_payoff_geometry_candidate_quality_autopsy import (  # noqa: E402
    TRADES_LOG,
    action_of,
    reward_risk,
    side_entry_price,
)
from tools.report_payoff_aware_shadow_forward_validation import read_shadow_rows  # noqa: E402

SENTINEL = "FORWARD_SHADOW_OUTCOME_VALIDATION_OK"
MIN_TRUSTED_SETTLED = 30


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_int(value: int | None) -> str:
    return "MISSING" if value is None else f"{value:,}"


def _fmt_num(value: float | None, digits: int = 4) -> str:
    return "MISSING" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "MISSING" if value is None else f"{value * 100:.1f}%"


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _row_ts(row: dict[str, Any]) -> datetime | None:
    return _parse_ts(row.get("timestamp") or row.get("timestamp_utc"))


def _settled_ts(row: dict[str, Any]) -> datetime | None:
    return _parse_ts(row.get("settled_at") or row.get("timestamp") or row.get("timestamp_utc"))


def _is_clean_settled(row: dict[str, Any]) -> bool:
    return (
        str(row.get("status") or "").upper() == "SETTLED"
        and str(row.get("result") or "").upper() in {"WIN", "LOSS"}
        and classify_accounting_version(row) == ECONOMIC_VERSION
        and is_clean_proof_row(row)
        and not is_kxeth_or_quarantined(row)
        and economic_pnl_value(row) is not None
    )


def _action(row: dict[str, Any]) -> str:
    return str(row.get("scanner_action") or row.get("intended_action") or row.get("action") or "UNKNOWN").upper()


def _pick_key(row: dict[str, Any]) -> tuple[str, str, float | None]:
    price = _safe_float(row.get("entry_price"))
    return (str(row.get("ticker") or ""), str(row.get("action") or "UNKNOWN").upper(), round(price, 6) if price is not None else None)


def _trade_key(row: dict[str, Any]) -> tuple[str, str, float | None]:
    price = side_entry_price(row)
    return (str(row.get("ticker") or ""), _action(row), round(price, 6) if price is not None else None)


def _rate(count: int, total: int) -> float | None:
    return count / total if total else None


def _avg(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return sum(nums) / len(nums) if nums else None


def flatten_shadow_picks(shadow_rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    picks: list[dict[str, Any]] = []
    for row in shadow_rows:
        ts = row.get("timestamp_utc")
        raw = row.get(key) or []
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                picks.append({**item, "_shadow_timestamp_utc": ts, "_scan_id": row.get("scan_id"), "_run_id": row.get("run_id")})
    return picks


def _unique_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, float | None]] = set()
    unique: list[dict[str, Any]] = []
    for pick in picks:
        key = _pick_key(pick)
        if key in seen:
            continue
        seen.add(key)
        unique.append(pick)
    return unique


def _match_picks_to_settled(picks: list[dict[str, Any]], settled_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = {_pick_key(row) for row in picks}
    matched: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float | None, str]] = set()
    for trade in settled_rows:
        key = _trade_key(trade)
        if key not in keys:
            continue
        unique_key = (*key, str(trade.get("timestamp") or trade.get("settled_at") or ""))
        if unique_key in seen:
            continue
        seen.add(unique_key)
        matched.append(trade)
    return matched


def _profit_factor(rows: list[dict[str, Any]]) -> float | None:
    wins = 0.0
    losses = 0.0
    for row in rows:
        pnl = economic_pnl_value(row)
        if pnl is None:
            continue
        if pnl > 0:
            wins += pnl
        elif pnl < 0:
            losses += pnl
    if wins > 0 and losses == 0:
        return float("inf")
    if wins <= 0 or losses >= 0:
        return None
    return wins / abs(losses)


def _summarize_mode(selected: list[dict[str, Any]], settled: list[dict[str, Any]], target_slots: int | None = None) -> dict[str, Any]:
    n = len(selected)
    settled_n = len(settled)
    wins = [row for row in settled if str(row.get("result") or "").upper() == "WIN"]
    losses = [row for row in settled if str(row.get("result") or "").upper() == "LOSS"]
    win_rate = len(wins) / settled_n if settled_n else None
    entries = [side_entry_price(row) for row in settled]
    if not entries:
        entries = [_safe_float(row.get("entry_price")) for row in selected]
    avg_entry = _avg(entries)
    rrs = [reward_risk(value) for value in entries]
    breakeven_wr = avg_entry
    wr_margin = (win_rate - breakeven_wr) if win_rate is not None and breakeven_wr is not None else None
    pnl = sum(value for value in (economic_pnl_value(row) for row in settled) if value is not None)
    capital = sum(value for value in (calib_map.calib.capital_at_risk_value(row) for row in settled) if value is not None)
    roi = pnl / capital if capital > 0 else None
    avg_model = _avg([calib_map.model_probability(row) for row in settled])
    calibration_error = (win_rate - avg_model) if win_rate is not None and avg_model is not None else None
    action_mix = Counter(_action(row) for row in settled) if settled else Counter(str(row.get("action") or "UNKNOWN").upper() for row in selected)
    selected_entries = [_safe_float(row.get("entry_price")) for row in selected]
    selected_rrs = [reward_risk(value) for value in selected_entries]
    geometry_base = settled if settled else selected

    def entry_for(row: dict[str, Any]) -> float | None:
        return side_entry_price(row) if "status" in row else _safe_float(row.get("entry_price"))

    expensive_count = sum(1 for row in geometry_base if (entry_for(row) is not None and (entry_for(row) or 0.0) >= 0.80))
    toxic_count = sum(1 for row in geometry_base if (entry_for(row) is not None and 0.80 <= (entry_for(row) or 0.0) < 0.90))
    weak_rr_count = sum(1 for row in geometry_base if (reward_risk(entry_for(row)) is not None and (reward_risk(entry_for(row)) or 0.0) < 0.25))
    base_n = len(geometry_base)
    trusted = bool(settled_n >= MIN_TRUSTED_SETTLED and roi is not None and roi > 0 and (_profit_factor(settled) or 0.0) > 1.10 and wr_margin is not None and wr_margin > 0)
    maturity = "TRUSTED" if trusted else "PARTIAL" if settled_n > 0 else "IMMATURE"
    if settled_n < MIN_TRUSTED_SETTLED:
        evidence_score = min(0.49, settled_n / MIN_TRUSTED_SETTLED)
    else:
        positives = sum([
            bool(roi is not None and roi > 0),
            bool((_profit_factor(settled) or 0.0) > 1.10),
            bool(wr_margin is not None and wr_margin > 0),
        ])
        evidence_score = min(1.0, 0.50 + positives / 6.0)

    return {
        "selected_n": n,
        "unique_selected_n": len(_unique_picks(selected)) if selected and "status" not in selected[0] else n,
        "target_slots": target_slots,
        "starvation_count": max(0, (target_slots or n) - n) if target_slots is not None else 0,
        "starvation_rate": _rate(max(0, (target_slots or n) - n), target_slots or n) if target_slots is not None else None,
        "settled_rows": settled_n,
        "win_rate": win_rate,
        "breakeven_wr": breakeven_wr,
        "wr_margin": wr_margin,
        "roi": roi,
        "profit_factor": _profit_factor(settled),
        "avg_entry_price": avg_entry if settled else _avg(selected_entries),
        "avg_reward_risk": _avg(rrs) if settled else _avg(selected_rrs),
        "avg_win": _avg([economic_pnl_value(row) for row in wins]),
        "avg_loss": _avg([economic_pnl_value(row) for row in losses]),
        "calibration_error": calibration_error,
        "expensive_entry_rate": _rate(expensive_count, base_n),
        "toxic_80_90_rate": _rate(toxic_count, base_n),
        "weak_reward_risk_rate": _rate(weak_rr_count, base_n),
        "action_mix": dict(action_mix.most_common()),
        "maturity": maturity,
        "trusted": trusted,
        "evidence_confidence_score": round(evidence_score, 4),
    }


def _first_shadow_ts(rows: list[dict[str, Any]]) -> datetime | None:
    values = [_parse_ts(row.get("timestamp_utc")) for row in rows]
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _future_clean_settled(trades: list[dict[str, Any]], start: datetime | None) -> list[dict[str, Any]]:
    rows = [row for row in trades if _is_clean_settled(row)]
    if start is None:
        return rows
    return [row for row in rows if (_settled_ts(row) is not None and (_settled_ts(row) or start) >= start)]


def _future_opened_trades(trades: list[dict[str, Any]], start: datetime | None) -> list[dict[str, Any]]:
    rows = [
        row for row in trades
        if classify_accounting_version(row) == ECONOMIC_VERSION
        and is_clean_proof_row(row)
        and not is_kxeth_or_quarantined(row)
        and str(row.get("action") or "").upper() in {"BET_YES", "BET_NO", "ARB"}
    ]
    if start is None:
        return rows
    return [row for row in rows if (_row_ts(row) is not None and (_row_ts(row) or start) >= start)]


def build_report(
    shadow_path: Path = SHADOW_LOG,
    trades_path: Path = TRADES_LOG,
) -> dict[str, Any]:
    shadow_rows = read_shadow_rows(shadow_path)
    trades = load_trades(trades_path)
    shadow_start = _first_shadow_ts(shadow_rows)
    future_settled = _future_clean_settled(trades, shadow_start)
    future_opened = _future_opened_trades(trades, shadow_start)
    actual_settled = [row for row in future_opened if _is_clean_settled(row)]

    current_picks = flatten_shadow_picks(shadow_rows, "current_top_3")
    payoff_picks = flatten_shadow_picks(shadow_rows, "payoff_aware_top_3")
    strict_picks = flatten_shadow_picks(shadow_rows, "strict_payoff_top_3")
    target_slots = len(shadow_rows) * 3

    current_settled = _match_picks_to_settled(current_picks, future_settled)
    payoff_settled = _match_picks_to_settled(payoff_picks, future_settled)
    strict_settled = _match_picks_to_settled(strict_picks, future_settled)

    modes = {
        "actual_opened": _summarize_mode(future_opened, actual_settled),
        "current_shadow": _summarize_mode(current_picks, current_settled, target_slots),
        "payoff_aware_shadow": _summarize_mode(payoff_picks, payoff_settled, target_slots),
        "strict_payoff_shadow": _summarize_mode(strict_picks, strict_settled, target_slots),
    }

    def settled_roi(name: str) -> float | None:
        return modes[name]["roi"]

    payoff_outperforming = (
        modes["payoff_aware_shadow"]["settled_rows"] >= MIN_TRUSTED_SETTLED
        and modes["current_shadow"]["settled_rows"] >= MIN_TRUSTED_SETTLED
        and (settled_roi("payoff_aware_shadow") or -999.0) > (settled_roi("current_shadow") or -999.0)
    )
    strict_outperforming = (
        modes["strict_payoff_shadow"]["settled_rows"] >= MIN_TRUSTED_SETTLED
        and modes["current_shadow"]["settled_rows"] >= MIN_TRUSTED_SETTLED
        and (settled_roi("strict_payoff_shadow") or -999.0) > (settled_roi("current_shadow") or -999.0)
    )

    current_geom = modes["current_shadow"]
    payoff_geom = modes["payoff_aware_shadow"]
    strict_geom = modes["strict_payoff_shadow"]
    enough_outcome_proof = any(mode["trusted"] for mode in modes.values())

    return {
        "read_only": True,
        "paths": {"shadow": str(shadow_path), "paper_trades": str(trades_path)},
        "shadow_rows": len(shadow_rows),
        "shadow_start": shadow_start.isoformat() if shadow_start else None,
        "future_opened_rows": len(future_opened),
        "future_clean_settled_rows": len(future_settled),
        "modes": modes,
        "answers": {
            "payoff_aware_outperforming_current": payoff_outperforming,
            "strict_outperforming_current": strict_outperforming,
            "improvements_only_geometry_deep": not enough_outcome_proof,
            "expensive_entries_decreasing_payoff": (
                payoff_geom["expensive_entry_rate"] is not None
                and current_geom["expensive_entry_rate"] is not None
                and payoff_geom["expensive_entry_rate"] < current_geom["expensive_entry_rate"]
            ),
            "reward_risk_improving_payoff": (
                payoff_geom["avg_reward_risk"] is not None
                and current_geom["avg_reward_risk"] is not None
                and payoff_geom["avg_reward_risk"] > current_geom["avg_reward_risk"]
            ),
            "strict_starves_too_much": bool((strict_geom["starvation_rate"] or 0.0) > 0.25),
            "bet_no_improving": False if not any("BET_NO" in mode["action_mix"] for mode in modes.values()) else "UNPROVEN",
            "confidence_still_overconfident": any(
                mode["calibration_error"] is not None and mode["calibration_error"] < -0.10
                for mode in modes.values()
            ),
            "enough_proof_for_live_patching": False,
            "do_not_deploy_warning": not enough_outcome_proof,
            "next_safest_step": "Keep shadow logging active until each mode has at least 30 settled matched outcomes; then compare ROI/PF/WR-margin before any live ranking patch.",
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


def _print_mode_table(modes: dict[str, dict[str, Any]]) -> None:
    print()
    print("MODE COMPARISON")
    print("---------------")
    print(
        f"{'mode':<24} {'n':>5} {'settled':>7} {'WR':>8} {'BE':>8} {'mrg':>8} {'ROI':>8} {'PF':>8} "
        f"{'entry':>8} {'RR':>8} {'avgW':>8} {'avgL':>8} {'calErr':>8} {'exp':>8} {'tox80':>8} {'weakRR':>8} {'maturity':>10}"
    )
    for name, stats in modes.items():
        print(
            f"{name:<24} {stats['selected_n']:>5} {stats['settled_rows']:>7} "
            f"{_fmt_pct(stats['win_rate']):>8} {_fmt_pct(stats['breakeven_wr']):>8} {_fmt_pct(stats['wr_margin']):>8} "
            f"{_fmt_pct(stats['roi']):>8} {_fmt_num(stats['profit_factor']):>8} {_fmt_num(stats['avg_entry_price']):>8} "
            f"{_fmt_num(stats['avg_reward_risk']):>8} {_fmt_num(stats['avg_win']):>8} {_fmt_num(stats['avg_loss']):>8} "
            f"{_fmt_pct(stats['calibration_error']):>8} {_fmt_pct(stats['expensive_entry_rate']):>8} "
            f"{_fmt_pct(stats['toxic_80_90_rate']):>8} {_fmt_pct(stats['weak_reward_risk_rate']):>8} {stats['maturity']:>10}"
        )
        print(
            f"{'':<24} action_mix={stats['action_mix']} "
            f"starvation={_fmt_pct(stats['starvation_rate'])} evidence_score={_fmt_num(stats['evidence_confidence_score'])}"
        )


def print_report(report: dict[str, Any]) -> None:
    print("=" * 112)
    print("FORWARD SHADOW OUTCOME VALIDATION")
    print("=" * 112)
    print("Read-only: no strategy, thresholds, scanner order, PaperTrader, logs, risk, or live-money state are modified.")
    print(f"shadow log:                {report['paths']['shadow']}")
    print(f"paper trades:              {report['paths']['paper_trades']}")
    print(f"shadow rows:               {_fmt_int(report['shadow_rows'])}")
    print(f"shadow start:              {report['shadow_start'] or 'MISSING'}")
    print(f"future opened rows:        {_fmt_int(report['future_opened_rows'])}")
    print(f"future clean settled rows: {_fmt_int(report['future_clean_settled_rows'])}")

    _print_mode_table(report["modes"])

    print()
    print("DIRECT ANSWERS")
    print("--------------")
    for key, value in report["answers"].items():
        print(f"{key:<42} {value}")
    if report["answers"]["do_not_deploy_warning"]:
        print("DO NOT DEPLOY: proof is insufficient for live ranking or strategy changes.")

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
