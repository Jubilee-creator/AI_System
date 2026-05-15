#!/usr/bin/env python3
"""
Phase 11C — MLflow phase metrics tracker.

Logs current system metrics to local MLflow tracking URI:
  data/observability/mlruns

Experiment: ai_system_phases
Run: phase_11c_{timestamp}

READ-ONLY.  Does not modify logs, config, or strategy.
No secrets are logged.

Cohort classification uses the same canonical logic as clean_truth_report /
row_quality_group (MODERN_FULL_METADATA requires risk_edge + model_probability +
quote metadata).  avg_clv is the canonical exit_price − entry_price metric from
performance_report.get_clv(), NOT council_confidence − entry_price.  The latter
is logged separately as avg_model_margin for diagnostic comparison.

Sentinel: MLFLOW_PHASE_METRICS_OK
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.performance_report import build_terminal_key_sets, classify_settled_records

MLFLOW_URI = ROOT / "data" / "observability" / "mlruns"
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_LOG = ROOT / "logs" / "execution_funnel.jsonl"
PHASE_11A_CUTOFF = "2026-05-15T13:46:29+00:00"
SENTINEL = "MLFLOW_PHASE_METRICS_OK"


def _load_jsonl_small(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Canonical row quality (mirrors row_quality_group in clean_truth_report) ──
def _as_float_local(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _has_quote_metadata(rec: dict) -> bool:
    if rec.get("price_yes") is not None or rec.get("price_no") is not None:
        return True
    return any(rec.get(f) is not None for f in ("yes_bid", "yes_ask", "no_bid", "no_ask"))


def _canonical_quality_group(rec: dict) -> str:
    has_risk_edge  = _as_float_local(rec.get("risk_edge"))         is not None
    has_model_prob = _as_float_local(rec.get("model_probability")) is not None
    has_quotes     = _has_quote_metadata(rec)
    if has_risk_edge and has_model_prob and has_quotes:
        return "MODERN_FULL_METADATA"
    if has_risk_edge:
        return "MODERN_EDGE_ONLY"
    if _as_float_local(rec.get("edge")) is not None:
        return "LEGACY_EDGE_ONLY"
    return "MISSING_EDGE"


def _canonical_get_clv(rec: dict) -> Optional[float]:
    """Canonical CLV: stored rec['clv'] if present, else exit_price - entry_price."""
    if rec.get("clv") is not None:
        return float(rec["clv"])
    if rec.get("entry_price") is None or rec.get("exit_price") is None:
        return None
    return round(float(rec["exit_price"]) - float(rec["entry_price"]), 4)


def _compute_metrics() -> dict[str, Any]:
    """Compute current phase metrics using canonical classification and CLV."""
    trades = _load_jsonl_small(TRADES_LOG)

    # ── Canonical clean_settled: exclude conflicted rows (key shared with FORCED_CLOSE/VOID) ──
    # Raw status=="SETTLED" overcounts by including rows whose key also has a
    # FORCED_CLOSE or VOID_LEGACY_DUPLICATE terminal record.  Those 9 rows are
    # "conflicted_settled" and are excluded from all canonical proof metrics.
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(trades)
    clean_settled_rows, _ = classify_settled_records(
        trades, settled_keys, forced_close_keys, void_keys
    )
    clean_settled = len(clean_settled_rows)

    modern_full = [t for t in clean_settled_rows if _canonical_quality_group(t) == "MODERN_FULL_METADATA"]
    dc_override    = sum(1 for t in modern_full if t.get("data_collection_override"))
    bootstrap_prov = sum(1 for t in modern_full if t.get("bootstrap_provisional"))
    normal_modern  = max(0, len(modern_full) - dc_override - bootstrap_prov)
    bootstrap_era_allow = sum(1 for t in modern_full if t.get("bootstrap_era_council_allow"))

    # ── Performance on normal_modern proof rows (subset of clean_settled_rows) ──
    proof_rows = [
        t for t in modern_full
        if not t.get("data_collection_override")
        and not t.get("bootstrap_provisional")
    ]
    pnl_vals = [_safe_float(t.get("pnl")) for t in proof_rows]
    pnl_vals = [p for p in pnl_vals if p is not None]
    entry_prices = [_safe_float(t.get("entry_price")) for t in proof_rows]
    entry_prices = [e for e in entry_prices if e is not None]

    total_pnl = sum(pnl_vals)
    total_invested = sum(ep * 5 for ep in entry_prices)
    modern_roi   = total_pnl / total_invested if total_invested > 0 else 0.0
    gross_win    = sum(p for p in pnl_vals if p > 0)
    gross_loss   = abs(sum(p for p in pnl_vals if p < 0))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else 0.0

    # ── Canonical CLV: exit_price − entry_price (matches performance_report.get_clv) ──
    clv_vals = [v for v in (_canonical_get_clv(t) for t in proof_rows) if v is not None]
    avg_clv = round(sum(clv_vals) / len(clv_vals), 4) if clv_vals else 0.0

    # ── Model margin (council_confidence − entry_price) — diagnostic only ──
    margin_vals = []
    for t in proof_rows:
        cc = _safe_float(t.get("council_confidence") or t.get("confidence"))
        ep = _safe_float(t.get("entry_price"))
        if cc is not None and ep is not None:
            margin_vals.append(cc - ep)
    avg_model_margin = round(sum(margin_vals) / len(margin_vals), 4) if margin_vals else 0.0

    # ── Post-11A counts — deduplicated by (ticker, timestamp[:19]) ──
    # Raw JSONL has OPEN + SETTLED rows for the same trade; last-wins dedup
    # matches paper_trader._sync_open_trades_from_log key logic.
    post_11a_raw = [
        t for t in trades
        if str(t.get("timestamp") or "") >= PHASE_11A_CUTOFF
    ]
    _post11a_by_key: dict[tuple, dict] = {}
    for t in post_11a_raw:
        key = (t.get("ticker"), str(t.get("timestamp") or "")[:19])
        _post11a_by_key[key] = t
    post_11a_deduped = list(_post11a_by_key.values())

    post_11a_raw_paper_rows = len(post_11a_raw)
    post_11a_unique_opened  = len([
        t for t in post_11a_deduped
        if t.get("status") in ("OPEN", "SETTLED")
    ])
    post_11a_settled = len([t for t in post_11a_deduped if t.get("status") == "SETTLED"])

    # ── Blocker counts from funnel tail (last 10k lines) ──
    market_quality_blocks = 0
    min_edge_blocks       = 0
    council_blocks        = 0
    quarantine_blocks     = 0
    edge_danger_blocks    = 0

    if FUNNEL_LOG.exists():
        buf: list[str] = []
        with open(FUNNEL_LOG) as f:
            for line in f:
                buf.append(line)
                if len(buf) > 10000:
                    buf.pop(0)
        for line in buf:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            reason = str(r.get("final_reason") or r.get("final_status") or "")
            if "MARKET_QUALITY" in reason:
                market_quality_blocks += 1
            elif "MIN_EDGE" in reason:
                min_edge_blocks += 1
            elif "COUNCIL" in reason:
                council_blocks += 1
            elif "QUARANTINE" in reason:
                quarantine_blocks += 1
            elif "EDGE_DANGER" in reason:
                edge_danger_blocks += 1

    # ── Git commit ──
    try:
        import subprocess
        commit_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, text=True
        ).strip()
    except Exception:
        commit_hash = "unknown"

    return {
        "params": {
            "phase": "11C",
            "commit_hash": commit_hash,
            "phase_11a_cutoff": PHASE_11A_CUTOFF,
            "proof_verdict": "WATCHLIST" if normal_modern >= 30 else "NOT_PROVEN",
            "trading_mode": "PAPER",
            "real_money_allowed": "False",
            "scale_allowed": "False",
            "kelly_enabled": "False",
        },
        "metrics": {
            "clean_settled":          float(clean_settled),
            "modern_full":            float(len(modern_full)),
            "normal_modern":          float(normal_modern),
            "dc_override":            float(dc_override),
            "bootstrap_provisional":  float(bootstrap_prov),
            "bootstrap_era_allow":    float(bootstrap_era_allow),
            "modern_roi":             round(modern_roi, 4),
            "avg_clv":                avg_clv,           # canonical: exit_price − entry_price
            "avg_model_margin":       avg_model_margin,  # diagnostic: council_confidence − entry_price
            "profit_factor":          round(profit_factor, 4),
            "total_pnl":              round(total_pnl, 2),
            "post_11a_raw_paper_rows":  float(post_11a_raw_paper_rows),
            "post_11a_unique_opened":   float(post_11a_unique_opened),
            "post_11a_settled":         float(post_11a_settled),
            "market_quality_blocks":  float(market_quality_blocks),
            "min_edge_blocks":        float(min_edge_blocks),
            "council_blocks":         float(council_blocks),
            "quarantine_blocks":      float(quarantine_blocks),
            "edge_danger_blocks":     float(edge_danger_blocks),
        },
    }


def main() -> int:
    try:
        import mlflow
    except ImportError as e:
        print(f"ERROR: {e} — run with .venv_observability/bin/python")
        return 1

    MLFLOW_URI.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(str(MLFLOW_URI))
    mlflow.set_experiment("ai_system_phases")

    print()
    print("=" * 60)
    print("  MLflow Phase Metrics — Phase 11C")
    print(f"  Tracking URI: {MLFLOW_URI.relative_to(ROOT)}")
    print("=" * 60)

    data = _compute_metrics()
    run_name = f"phase_11c_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params(data["params"])
        mlflow.log_metrics(data["metrics"])

        run_id = run.info.run_id
        print(f"\n  run_name:  {run_name}")
        print(f"  run_id:    {run_id}")

    print()
    print("  PARAMS:")
    for k, v in data["params"].items():
        print(f"    {k:<35} {v}")
    print()
    print("  METRICS:")
    for k, v in data["metrics"].items():
        print(f"    {k:<35} {v}")

    print()
    print(f"  NOTE: avg_clv = canonical exit_price − entry_price")
    print(f"  NOTE: avg_model_margin = council_confidence − entry_price (diagnostic only)")
    print(f"  NOTE: post_11a_unique_opened = deduplicated by (ticker, timestamp)")
    print()
    print(f"  {SENTINEL}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
