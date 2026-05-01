#!/usr/bin/env python3
"""
tools/audit_normal_approval_path.py
-----------------------------------
Read-only bootstrap audit.

This simulates how many historical modern evaluated rows would meet the
M-42 bootstrap provisional thresholds.  It is NOT proof of edge and does not
change trading, risk, sizing, logs, or edge profiles.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.trading_config import BOOTSTRAP_MIN_CONFIDENCE, BOOTSTRAP_MIN_EDGE
from tools.performance_report import (
    build_terminal_key_sets,
    classify_settled_records,
    get_pnl,
    get_size,
    load_trades,
)


def _as_float(value: Any):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_quote_metadata(rec: dict) -> bool:
    if rec.get("price_yes") is not None or rec.get("price_no") is not None:
        return True
    return any(rec.get(k) is not None for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def _is_modern_full_metadata(rec: dict) -> bool:
    return (
        rec.get("risk_edge") is not None
        and rec.get("model_probability") is not None
        and _has_quote_metadata(rec)
    )


def _is_time_exit(rec: dict) -> bool:
    return (
        rec.get("status") == "FORCED_CLOSE"
        and (
            rec.get("result") == "TIME_EXIT"
            or rec.get("reason") == "TIME_EXIT"
            or rec.get("cleanup_reason") == "TIME_EXIT"
        )
    )


def _bootstrap_edge(rec: dict):
    for key in ("original_edge", "risk_edge", "edge"):
        value = _as_float(rec.get(key))
        if value is not None:
            return value
    return None


def _bootstrap_confidence(rec: dict):
    for key in ("original_confidence", "model_probability", "confidence"):
        value = _as_float(rec.get(key))
        if value is not None:
            return value
    return None


def _roi(rows: list[dict]) -> float:
    wagered = sum(get_size(r) for r in rows)
    return sum(get_pnl(r) for r in rows) / wagered if wagered else 0.0


def main() -> None:
    all_records = load_trades()
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted_settled = classify_settled_records(
        all_records,
        settled_keys,
        forced_close_keys,
        void_keys,
    )
    time_exits = [r for r in all_records if _is_time_exit(r)]
    evaluated = clean_settled + time_exits
    modern = [r for r in evaluated if _is_modern_full_metadata(r)]

    candidates = [
        r for r in modern
        if (_bootstrap_edge(r) is not None and _bootstrap_edge(r) >= BOOTSTRAP_MIN_EDGE)
        and (_bootstrap_confidence(r) is not None and _bootstrap_confidence(r) >= BOOTSTRAP_MIN_CONFIDENCE)
    ]
    normal = [
        r for r in modern
        if not r.get("data_collection_override") and not r.get("bootstrap_provisional")
    ]

    print("BOOTSTRAP PROVISIONAL SIMULATION")
    print("--------------------------------")
    print("This is a read-only simulation. It is NOT proof of edge.")
    print(
        f"Thresholds: original_edge >= {BOOTSTRAP_MIN_EDGE:.4f}, "
        f"confidence >= {BOOTSTRAP_MIN_CONFIDENCE:.3f}"
    )
    print(f"Evaluated rows:                 {len(evaluated)}")
    print(f"Modern full-metadata rows:      {len(modern)}")
    print(f"Normal modern proof rows:       {len(normal)}")
    print(f"Bootstrap-qualified historical: {len(candidates)}")
    print(f"Bootstrap candidate ROI:        {_roi(candidates) * 100:+.2f}%")
    print()

    if not candidates:
        print("No historical modern rows meet bootstrap provisional thresholds.")
        return

    print("Candidate rows:")
    for rec in candidates:
        print(
            "- "
            f"{rec.get('ticker', 'UNKNOWN')} "
            f"status={rec.get('status')} result={rec.get('result')} "
            f"edge={_bootstrap_edge(rec):+.4f} "
            f"confidence={_bootstrap_confidence(rec):.3f} "
            f"pnl={get_pnl(rec):+.2f} "
            f"data_collection={bool(rec.get('data_collection_override'))} "
            f"bootstrap_provisional={bool(rec.get('bootstrap_provisional'))}"
        )


if __name__ == "__main__":
    main()
