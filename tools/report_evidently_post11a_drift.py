#!/usr/bin/env python3
"""
Phase 11C — Evidently pre-vs-post-11A distribution drift report.

Uses Evidently 0.7.21 (legacy API).
READ-ONLY.  Generated artifacts written to:
  reports/observability/evidently_drift_report.html
  reports/observability/evidently_drift_results.json

Compares pre-11A (reference) vs post-11A (current) distributions on:
  entry_price, confidence, edge, original_edge

If not enough data in either window, prints NOT_ENOUGH_DATA and exits 0.

Sentinel: EVIDENTLY_POST11A_DRIFT_OK
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PHASE_11A_CUTOFF = "2026-05-15T13:46:29+00:00"
MIN_ROWS = 5               # below this, skip entirely
MIN_STRONG_INFERENCE = 30  # below this, label as exploratory only
REPORTS_DIR = ROOT / "reports" / "observability"
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
SENTINEL = "EVIDENTLY_POST11A_DRIFT_OK"
NUMERIC_COLS = ["entry_price", "confidence", "edge", "original_edge", "pnl"]


def _load_trades() -> list[dict]:
    if not TRADES_LOG.exists():
        return []
    rows = []
    with open(TRADES_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> int:
    try:
        import pandas as pd
        from evidently.legacy.report import Report
        from evidently.legacy.metric_preset import DataDriftPreset
    except ImportError as e:
        print(f"ERROR: {e} — run with .venv_observability/bin/python")
        return 1

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 60)
    print("  Evidently — Pre-vs-Post-11A Drift Report")
    print(f"  Cutoff: {PHASE_11A_CUTOFF}")
    print("=" * 60)

    all_rows = _load_trades()
    settled = [r for r in all_rows if r.get("status") == "SETTLED"]

    pre = [r for r in settled if str(r.get("timestamp") or "") < PHASE_11A_CUTOFF]
    post = [r for r in settled if str(r.get("timestamp") or "") >= PHASE_11A_CUTOFF]

    print(f"  settled pre-11A:   {len(pre)}")
    print(f"  settled post-11A:  {len(post)}")

    low_sample = len(post) < MIN_STRONG_INFERENCE
    if low_sample:
        print()
        print(f"  WARNING: post-11A n={len(post)} < {MIN_STRONG_INFERENCE}")
        print(f"  EXPLORATORY_ONLY_NOT_DECISION_GRADE")
        print(f"  Drift results below are indicative only — accumulate >= {MIN_STRONG_INFERENCE} post-11A")
        print(f"  settled trades before drawing strong conclusions.")

    if len(pre) < MIN_ROWS or len(post) < MIN_ROWS:
        print()
        print(f"  NOT_ENOUGH_DATA — need >={MIN_ROWS} settled in each window")
        print(f"  (pre={len(pre)}, post={len(post)})")
        print()
        print(f"  {SENTINEL}")
        print()
        return 0

    def _to_df(rows: list[dict]) -> pd.DataFrame:
        records = []
        for r in rows:
            rec = {col: _to_float(r.get(col)) for col in NUMERIC_COLS}
            records.append(rec)
        return pd.DataFrame(records).dropna(how="all")

    ref_df = _to_df(pre)
    curr_df = _to_df(post)

    # Keep only columns present in both with sufficient data
    shared_cols = [
        c for c in NUMERIC_COLS
        if c in ref_df.columns and c in curr_df.columns
        and ref_df[c].notna().sum() >= 3
        and curr_df[c].notna().sum() >= 3
    ]

    if not shared_cols:
        print("  NOT_ENOUGH_DATA — no shared numeric columns with sufficient coverage")
        print(f"  {SENTINEL}")
        return 0

    ref_df = ref_df[shared_cols].dropna()
    curr_df = curr_df[shared_cols].dropna()

    print(f"  columns compared:  {shared_cols}")
    print(f"  ref rows (clean):  {len(ref_df)}")
    print(f"  curr rows (clean): {len(curr_df)}")

    report = Report(metrics=[DataDriftPreset()])
    report.run(reference_data=ref_df, current_data=curr_df)
    result_dict = report.as_dict()

    # Parse drift summary
    drift_info: dict = {}
    for m in result_dict.get("metrics", []):
        metric_name = m.get("metric", "")
        result = m.get("result", {})
        if metric_name == "DatasetDriftMetric":
            drift_info = {
                "dataset_drift": result.get("dataset_drift"),
                "drift_share": result.get("drift_share"),
                "drifted_columns": result.get("number_of_drifted_columns"),
                "total_columns": result.get("number_of_columns"),
            }
        elif metric_name == "DataDriftTable":
            per_col = result.get("drift_by_columns", {})
            drift_info["per_column"] = {
                col: {
                    "drift_detected": v.get("drift_detected"),
                    "p_value": round(v.get("p_value") or 0, 4),
                    "stattest": v.get("stattest_name"),
                }
                for col, v in per_col.items()
            }

    # Print summary
    print()
    print(f"  dataset_drift:     {drift_info.get('dataset_drift')}")
    print(f"  drift_share:       {drift_info.get('drift_share')}")
    print(f"  drifted_columns:   {drift_info.get('drifted_columns')} / {drift_info.get('total_columns')}")
    print()
    for col, info in drift_info.get("per_column", {}).items():
        marker = "DRIFT" if info["drift_detected"] else "stable"
        print(f"  [{marker:<6}] {col:<20} p={info['p_value']:.4f}  ({info['stattest']})")

    # Save HTML
    html_path = REPORTS_DIR / "evidently_drift_report.html"
    report.save_html(str(html_path))
    print(f"\n  HTML saved: {html_path.relative_to(ROOT)}")

    # Add low-sample caveat to saved JSON
    drift_info["post_11a_n"] = len(post)
    drift_info["exploratory_only"] = low_sample
    if low_sample:
        drift_info["verdict_caveat"] = (
            f"NOT_ENOUGH_POST11A_ROWS_FOR_STRONG_DRIFT_INFERENCE "
            f"(n={len(post)}, need>={MIN_STRONG_INFERENCE})"
        )

    # Save JSON summary
    json_path = REPORTS_DIR / "evidently_drift_results.json"
    json_path.write_text(json.dumps(drift_info, indent=2, default=str))
    print(f"  JSON saved: {json_path.relative_to(ROOT)}")

    if low_sample:
        print()
        print(f"  EXPLORATORY_ONLY_NOT_DECISION_GRADE")
        print(f"  NOT_ENOUGH_POST11A_ROWS_FOR_STRONG_DRIFT_INFERENCE (n={len(post)})")

    print()
    print(f"  {SENTINEL}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
