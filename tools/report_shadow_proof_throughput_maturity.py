#!/usr/bin/env python3
"""
Phase 10L - Shadow Proof Throughput + Settlement Maturity Audit
Sentinel: SHADOW_PROOF_THROUGHPUT_MATURITY_OK

Read-only audit explaining why forward shadow outcome proof is or is not
maturing.  It does not modify logs, strategy, thresholds, scanner order,
PaperTrader, risk state, or live-money settings.
"""
from __future__ import annotations

import json
import os
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
from tools.clean_truth_report import row_quality_group  # noqa: E402
from tools.performance_report import is_side_coverage_record  # noqa: E402
from tools.report_accounting_version_proof_cohorts import (  # noqa: E402
    ECONOMIC_VERSION,
    classify_accounting_version,
    economic_pnl_value,
    entry_price,
    is_clean_proof_row,
    is_kxeth_or_quarantined,
    is_normal_modern,
    load_trades,
)
from tools.report_forward_shadow_outcome_validation import flatten_shadow_picks  # noqa: E402
from tools.report_payoff_aware_shadow_forward_validation import read_shadow_rows  # noqa: E402

TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
AUTO_SETTLE_HEARTBEAT = ROOT / "data" / "auto_settle_last_run.json"
AUTO_SETTLE_LOCK = ROOT / "data" / "auto_settle_loop.lock"
AUTO_SETTLE_LOG = ROOT / "logs" / "auto_settle_loop.log"
SENTINEL = "SHADOW_PROOF_THROUGHPUT_MATURITY_OK"
STALE_HEARTBEAT_SECONDS = 900


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


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or "").upper()


def _action(row: dict[str, Any]) -> str:
    return str(row.get("scanner_action") or row.get("intended_action") or row.get("action") or "").upper()


def _price(row: dict[str, Any]) -> float | None:
    return entry_price(row)


def _pick_key(row: dict[str, Any]) -> tuple[str, str, float | None]:
    price = _safe_float(row.get("entry_price"))
    return (str(row.get("ticker") or ""), str(row.get("action") or "").upper(), round(price, 6) if price is not None else None)


def _trade_key(row: dict[str, Any]) -> tuple[str, str, float | None]:
    price = _price(row)
    return (str(row.get("ticker") or ""), _action(row), round(price, 6) if price is not None else None)


def _shadow_bounds(rows: list[dict[str, Any]]) -> tuple[datetime | None, datetime | None]:
    timestamps = [_parse_ts(row.get("timestamp_utc")) for row in rows]
    timestamps = [ts for ts in timestamps if ts is not None]
    return (min(timestamps), max(timestamps)) if timestamps else (None, None)


def _rows_after_shadow(rows: list[dict[str, Any]], start: datetime | None) -> list[dict[str, Any]]:
    if start is None:
        return list(rows)
    selected = []
    for row in rows:
        ts = _row_ts(row) or _settled_ts(row)
        if ts is not None and ts >= start:
            selected.append(row)
    return selected


def exclusion_reasons(row: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if _status(row) != "SETTLED":
        reasons.append("not_settled")
    if str(row.get("result") or "").upper() not in {"WIN", "LOSS"}:
        reasons.append("missing_outcome")
    if classify_accounting_version(row) != ECONOMIC_VERSION:
        reasons.append("wrong_accounting_version")
    if is_kxeth_or_quarantined(row):
        reasons.append("kxeth_or_quarantined")
    if bool(row.get("data_collection_override")):
        reasons.append("data_collection_override")
    if bool(row.get("bootstrap_provisional")):
        reasons.append("bootstrap_provisional")
    if is_side_coverage_record(row) or bool(row.get("side_coverage")):
        reasons.append("side_coverage_contamination")
    if economic_pnl_value(row) is None:
        reasons.append("missing_economic_pnl")
    if not is_normal_modern(row):
        reasons.append("not_normal_modern")
    if entry_price(row) is None:
        reasons.append("missing_entry_price")
    if not _action(row):
        reasons.append("missing_action")
    return reasons


def _clean_settled_after(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if _status(row) == "SETTLED"
        and str(row.get("result") or "").upper() in {"WIN", "LOSS"}
        and classify_accounting_version(row) == ECONOMIC_VERSION
        and is_clean_proof_row(row)
        and not is_kxeth_or_quarantined(row)
        and economic_pnl_value(row) is not None
        and entry_price(row) is not None
        and bool(_action(row))
        and not (is_side_coverage_record(row) or bool(row.get("side_coverage")))
    ]


def _settlement_lag(rows: list[dict[str, Any]]) -> dict[str, Any]:
    lags = []
    for row in rows:
        opened = _row_ts(row)
        settled = _settled_ts(row)
        if opened is None or settled is None or settled < opened:
            continue
        lags.append((settled - opened).total_seconds() / 60.0)
    if not lags:
        return {"n": 0, "avg_minutes": None, "min_minutes": None, "max_minutes": None}
    return {
        "n": len(lags),
        "avg_minutes": sum(lags) / len(lags),
        "min_minutes": min(lags),
        "max_minutes": max(lags),
    }


def _auto_settle_evidence() -> dict[str, Any]:
    heartbeat: dict[str, Any] = {}
    if AUTO_SETTLE_HEARTBEAT.exists():
        try:
            heartbeat = json.loads(AUTO_SETTLE_HEARTBEAT.read_text())
        except json.JSONDecodeError:
            heartbeat = {"error": "JSONDecodeError"}
    hb_ts = _parse_ts(heartbeat.get("timestamp_utc"))
    age = (datetime.now(timezone.utc) - hb_ts).total_seconds() if hb_ts else None
    lock_pid = None
    lock_running = False
    if AUTO_SETTLE_LOCK.exists():
        lock_pid = AUTO_SETTLE_LOCK.read_text().strip()
        if lock_pid.isdigit():
            try:
                os.kill(int(lock_pid), 0)
                lock_running = True
            except OSError:
                lock_running = False
    last_log_line = None
    if AUTO_SETTLE_LOG.exists():
        try:
            lines = [line.strip() for line in AUTO_SETTLE_LOG.read_text(errors="replace").splitlines() if line.strip()]
            last_log_line = lines[-1] if lines else None
        except OSError:
            last_log_line = None
    return {
        "heartbeat_exists": AUTO_SETTLE_HEARTBEAT.exists(),
        "heartbeat_timestamp": heartbeat.get("timestamp_utc"),
        "heartbeat_age_seconds": age,
        "heartbeat_recent": bool(age is not None and age <= STALE_HEARTBEAT_SECONDS),
        "heartbeat": heartbeat,
        "lock_exists": AUTO_SETTLE_LOCK.exists(),
        "lock_pid": lock_pid,
        "lock_pid_running": lock_running,
        "last_log_line": last_log_line,
    }


def _all_picks(shadow_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "current": flatten_shadow_picks(shadow_rows, "current_top_3"),
        "payoff_aware": flatten_shadow_picks(shadow_rows, "payoff_aware_top_3"),
        "strict": flatten_shadow_picks(shadow_rows, "strict_payoff_top_3"),
    }


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


def _match_one(pick: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no_opened_trade"
    ticker = str(pick.get("ticker") or "")
    action = str(pick.get("action") or "").upper()
    price = _safe_float(pick.get("entry_price"))
    ticker_rows = [row for row in rows if str(row.get("ticker") or "") == ticker]
    if not ticker_rows:
        return "ticker_mismatch"
    action_rows = [row for row in ticker_rows if _action(row) == action]
    if not action_rows:
        return "action_mismatch"
    price_rows = [row for row in action_rows if price is not None and _price(row) is not None and round(price, 6) == round(_price(row) or 0.0, 6)]
    if not price_rows:
        return "price_mismatch"
    if not any(_status(row) == "SETTLED" for row in price_rows):
        return "no_settled_trade"
    return "matched"


def _match_report(shadow_rows: list[dict[str, Any]], paper_after: list[dict[str, Any]]) -> dict[str, Any]:
    modes = _all_picks(shadow_rows)
    out: dict[str, Any] = {}
    for mode, picks in modes.items():
        unique = _unique_picks(picks)
        reasons = Counter(_match_one(pick, paper_after) for pick in unique)
        matched = reasons.get("matched", 0)
        out[mode] = {
            "pick_count": len(picks),
            "unique_pick_count": len(unique),
            "matched": matched,
            "match_rate": matched / len(unique) if unique else None,
            "unmatched_reasons": dict(reasons),
        }
    return out


def _classify_bottleneck(
    paper_after: list[dict[str, Any]],
    opened_after: list[dict[str, Any]],
    settled_after: list[dict[str, Any]],
    clean_settled_after: list[dict[str, Any]],
    match_report: dict[str, Any],
    auto_settle: dict[str, Any],
) -> str:
    if not opened_after:
        return "NO_FORWARD_OPENED_TRADES"
    active_open = [row for row in opened_after if _status(row) == "OPEN"]
    if active_open and not settled_after:
        if not auto_settle.get("heartbeat_recent"):
            return "AUTO_SETTLE_NOT_RUNNING"
        return "OPEN_TRADES_NOT_SETTLING"
    if settled_after and not clean_settled_after:
        return "SETTLED_BUT_NOT_CLEAN"
    current = match_report.get("current", {})
    reasons = current.get("unmatched_reasons", {})
    if clean_settled_after and current.get("matched", 0) == 0:
        if reasons.get("price_mismatch", 0) > 0:
            return "VALIDATOR_TOO_STRICT"
        return "SHADOW_MATCHING_MISMATCH"
    if len(clean_settled_after) < 30:
        return "INSUFFICIENT_TIME_ONLY"
    return "HEALTHY_WAIT_FOR_MORE_DATA"


def build_report(
    shadow_path: Path = SHADOW_LOG,
    trades_path: Path = TRADES_LOG,
) -> dict[str, Any]:
    shadow_rows = read_shadow_rows(shadow_path)
    trades = load_trades(trades_path)
    first_shadow, latest_shadow = _shadow_bounds(shadow_rows)
    paper_before = [row for row in trades if first_shadow and (_row_ts(row) or datetime.min.replace(tzinfo=timezone.utc)) < first_shadow]
    paper_after = _rows_after_shadow(trades, first_shadow)
    opened_after = [row for row in paper_after if _action(row)]
    open_after = [row for row in paper_after if _status(row) == "OPEN"]
    settled_after = [row for row in paper_after if _status(row) == "SETTLED"]
    clean_after = _clean_settled_after(paper_after)
    exclusions = Counter(reason for row in paper_after for reason in exclusion_reasons(row))
    auto_settle = _auto_settle_evidence()
    matches = _match_report(shadow_rows, paper_after)
    bottleneck = _classify_bottleneck(paper_after, opened_after, settled_after, clean_after, matches, auto_settle)
    validator_too_strict = bottleneck == "VALIDATOR_TOO_STRICT"
    correctly_strict = not validator_too_strict
    pick_counts = {mode: data["pick_count"] for mode, data in matches.items()}
    return {
        "read_only": True,
        "paths": {"shadow": str(shadow_path), "paper_trades": str(trades_path)},
        "first_shadow_timestamp": first_shadow.isoformat() if first_shadow else None,
        "latest_shadow_timestamp": latest_shadow.isoformat() if latest_shadow else None,
        "shadow_rows": len(shadow_rows),
        "pick_counts": pick_counts,
        "paper_trades_before_shadow_start": len(paper_before) if first_shadow else 0,
        "paper_trades_after_shadow_start": len(paper_after),
        "opened_trades_after_shadow_start": len(opened_after),
        "settled_trades_after_shadow_start": len(settled_after),
        "clean_settled_trades_after_shadow_start": len(clean_after),
        "open_rows_after_shadow_start": len(open_after),
        "excluded_rows_by_reason": dict(exclusions.most_common()),
        "settlement_lag": _settlement_lag(settled_after),
        "auto_settle": auto_settle,
        "shadow_match": matches,
        "main_bottleneck": bottleneck,
        "validator_assessment": "VALIDATOR_TOO_STRICT" if validator_too_strict else "CORRECTLY_STRICT",
        "proof_starvation_cause": (
            "no paper trades opened after shadow logging started"
            if bottleneck == "NO_FORWARD_OPENED_TRADES"
            else "open trades have not settled"
            if bottleneck in {"OPEN_TRADES_NOT_SETTLING", "AUTO_SETTLE_NOT_RUNNING"}
            else "settled rows are not clean proof eligible"
            if bottleneck == "SETTLED_BUT_NOT_CLEAN"
            else "shadow picks do not match paper rows"
            if bottleneck in {"SHADOW_MATCHING_MISMATCH", "VALIDATOR_TOO_STRICT"}
            else "insufficient elapsed outcome data"
        ),
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


def print_report(report: dict[str, Any]) -> None:
    print("=" * 108)
    print("SHADOW PROOF THROUGHPUT + SETTLEMENT MATURITY AUDIT")
    print("=" * 108)
    print("Read-only: no strategy, thresholds, scanner order, PaperTrader, logs, risk, or live-money state are modified.")
    print(f"first_shadow_timestamp:              {report['first_shadow_timestamp'] or 'MISSING'}")
    print(f"latest_shadow_timestamp:             {report['latest_shadow_timestamp'] or 'MISSING'}")
    print(f"shadow_rows:                         {_fmt_int(report['shadow_rows'])}")
    print(f"pick_counts:                         {report['pick_counts']}")
    print(f"paper_trades_before_shadow_start:    {_fmt_int(report['paper_trades_before_shadow_start'])}")
    print(f"paper_trades_after_shadow_start:     {_fmt_int(report['paper_trades_after_shadow_start'])}")
    print(f"opened_trades_after_shadow_start:    {_fmt_int(report['opened_trades_after_shadow_start'])}")
    print(f"settled_trades_after_shadow_start:   {_fmt_int(report['settled_trades_after_shadow_start'])}")
    print(f"clean_settled_after_shadow_start:    {_fmt_int(report['clean_settled_trades_after_shadow_start'])}")
    print(f"open_rows_after_shadow_start:        {_fmt_int(report['open_rows_after_shadow_start'])}")

    print()
    print("EXCLUDED ROWS BY REASON")
    print("-----------------------")
    if report["excluded_rows_by_reason"]:
        for key, value in report["excluded_rows_by_reason"].items():
            print(f"{key:<34} {_fmt_int(value)}")
    else:
        print("(none)")

    lag = report["settlement_lag"]
    print()
    print("SETTLEMENT LAG")
    print("--------------")
    print(f"n={lag['n']} avg_minutes={_fmt_num(lag['avg_minutes'])} min={_fmt_num(lag['min_minutes'])} max={_fmt_num(lag['max_minutes'])}")

    auto = report["auto_settle"]
    print()
    print("AUTO-SETTLE EVIDENCE")
    print("--------------------")
    print(f"heartbeat_exists:      {auto['heartbeat_exists']}")
    print(f"heartbeat_timestamp:   {auto.get('heartbeat_timestamp') or 'MISSING'}")
    print(f"heartbeat_age_seconds: {_fmt_num(auto.get('heartbeat_age_seconds'))}")
    print(f"heartbeat_recent:      {auto['heartbeat_recent']}")
    print(f"lock_exists:           {auto['lock_exists']}")
    print(f"lock_pid:              {auto.get('lock_pid') or 'MISSING'}")
    print(f"lock_pid_running:      {auto['lock_pid_running']}")
    print(f"last_log_line:         {auto.get('last_log_line') or 'MISSING'}")

    print()
    print("SHADOW PICK MATCHING")
    print("--------------------")
    for mode, data in report["shadow_match"].items():
        print(
            f"{mode:<14} picks={_fmt_int(data['pick_count'])} unique={_fmt_int(data['unique_pick_count'])} "
            f"matched={_fmt_int(data['matched'])} match_rate={_fmt_pct(data['match_rate'])} "
            f"reasons={data['unmatched_reasons']}"
        )

    print()
    print("BOTTLENECK")
    print("----------")
    print(f"main_bottleneck:       {report['main_bottleneck']}")
    print(f"validator_assessment:  {report['validator_assessment']}")
    print(f"proof_starvation_cause:{report['proof_starvation_cause']}")

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
