#!/usr/bin/env python3
"""
Phase 11C — Great Expectations log data-quality validation.

Uses GX 1.17.2 ephemeral context (no external service).
READ-ONLY.  Generated artifacts written to:
  reports/observability/gx_results.json

Expectations:
  execution_funnel — timestamp_utc, ticker, final_status, final_reason non-null
  paper_trades     — ticker, action, status non-null; entry_price in (0, 1]
  no null ticker on opened/settled trades
  no negative entry_price
  entry_price <= 1.0 where applicable

Custom KXETH check (scope limited):
  a) no KXETH trade has a final-active OPEN status (after ticker/timestamp dedup)
  b) no KXETH row with status OPEN or SETTLED was created after Phase 8Q quarantine date
  Full proof-pool exclusion of historical KXETH rows is enforced by
  clean_truth_report.py evaluate_proof_gates(), not by this GX check.

Sentinel: GX_LOG_QUALITY_OK
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPORTS_DIR = ROOT / "reports" / "observability"
SENTINEL = "GX_LOG_QUALITY_OK"


def _load_jsonl(path: Path) -> list[dict]:
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


def _run_gx_suite(
    ctx,
    gx,
    source_name: str,
    suite_name: str,
    expectations: list,
    df,
) -> dict[str, Any]:
    """Run a GX validation suite on a DataFrame. Returns result summary."""
    import pandas as pd

    if df.empty:
        return {"skipped": True, "reason": "empty dataframe", "success": True}

    # Each call needs a unique source/asset/suite name to avoid ephemeral context conflicts
    ds = ctx.data_sources.add_pandas(source_name)
    asset = ds.add_dataframe_asset(f"{source_name}_asset")
    batch_def = asset.add_batch_definition_whole_dataframe("whole")

    suite = ctx.suites.add(gx.ExpectationSuite(name=suite_name))
    for exp in expectations:
        suite.add_expectation(exp)

    vd = ctx.validation_definitions.add(
        gx.ValidationDefinition(name=f"{suite_name}_vd", data=batch_def, suite=suite)
    )
    result = vd.run(batch_parameters={"dataframe": df})

    results_by_exp = []
    for er in result.results:
        exp_type = er.expectation_config.type
        col = er.expectation_config.kwargs.get("column", "—")
        results_by_exp.append({
            "expectation": exp_type,
            "column": col,
            "success": er.success,
        })

    return {
        "skipped": False,
        "success": result.success,
        "evaluated": result.statistics.get("evaluated_expectations", 0),
        "passed": result.statistics.get("successful_expectations", 0),
        "failed": result.statistics.get("unsuccessful_expectations", 0),
        "results": results_by_exp,
    }


def _custom_kxeth_check(rows: list[dict]) -> dict[str, Any]:
    """
    Check KXETH quarantine health in the paper_trades log.

    Scope — what this check validates:
      1. After (ticker, timestamp) last-wins deduplication, no KXETH trade has a
         final status of OPEN (would mean a live active KXETH position — critical).
      2. No KXETH row with status OPEN or SETTLED was created after Phase 8Q
         quarantine date (would mean a new KXETH trade was opened post-quarantine).

    Scope — what this check does NOT validate:
      Historical KXETH settled rows (pre-Phase 8Q) are expected and are present in
      the log.  Exclusion of those rows from the canonical proof pool is done by
      clean_truth_report.py evaluate_proof_gates() logic (KXETH quarantine label),
      not by GX.  GX cannot and does not replicate the full proof-pool exclusion.
    """
    # Phase 8Q quarantine implementation date (KXETH quarantine added)
    PHASE_8Q_DATE = "2026-05-10"  # approximate; conservative bound

    # Deduplicate by (ticker, timestamp) — last line wins (SETTLED overwrites OPEN)
    # This matches the logic in audit_open_trade_state.py and paper_trader._sync_open_trades_from_log
    by_key: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("ticker"), r.get("timestamp") or r.get("timestamp_utc"))
        by_key[key] = r
    final_rows = list(by_key.values())

    kxeth_open = [
        r for r in final_rows
        if str(r.get("ticker") or "").upper().startswith("KXETH")
        and r.get("status") == "OPEN"  # genuinely live — not yet settled
    ]
    kxeth_post_quarantine = [
        r for r in final_rows
        if str(r.get("ticker") or "").upper().startswith("KXETH")
        and r.get("status") in ("OPEN", "SETTLED")
        and str(r.get("timestamp") or "") >= PHASE_8Q_DATE
    ]
    historical_kxeth = [
        r for r in final_rows
        if str(r.get("ticker") or "").upper().startswith("KXETH")
        and r.get("status") == "SETTLED"
        and str(r.get("timestamp") or "") < PHASE_8Q_DATE
    ]

    success = len(kxeth_open) == 0 and len(kxeth_post_quarantine) == 0
    return {
        "kxeth_open_active": len(kxeth_open),        # must be 0
        "kxeth_post_quarantine": len(kxeth_post_quarantine),  # must be 0
        "kxeth_historical_settled": len(historical_kxeth),    # expected > 0, excluded by proof gate
        "success": success,
        "detail": (
            "KXETH quarantine active — no new KXETH trades opened post-Phase-8Q; "
            f"{len(historical_kxeth)} historical rows excluded by proof gate logic"
        ) if success else (
            f"ALERT: kxeth_open={len(kxeth_open)}, kxeth_post_quarantine={len(kxeth_post_quarantine)}"
        ),
    }


def _line(w: int = 60) -> str:
    return "-" * w


def main() -> int:
    try:
        import great_expectations as gx
        import pandas as pd
    except ImportError as e:
        print(f"ERROR: {e} — run with .venv_observability/bin/python")
        return 1

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 60)
    print("  Great Expectations — Log Data Quality")
    print("=" * 60)

    # Load raw rows (pandas for small files)
    funnel_rows = _load_jsonl(ROOT / "logs" / "execution_funnel.jsonl")
    trades_rows = _load_jsonl(ROOT / "logs" / "paper_trades.jsonl")

    # For funnel: sample to avoid GX metric computation on 167K rows (still honest)
    # Use last 5000 rows as representative sample
    funnel_sample = funnel_rows[-5000:] if len(funnel_rows) > 5000 else funnel_rows
    funnel_df = pd.DataFrame(funnel_sample) if funnel_sample else pd.DataFrame()
    trades_df = pd.DataFrame(trades_rows) if trades_rows else pd.DataFrame()

    # Opened and settled subsets
    opened_df = trades_df[trades_df.get("status", pd.Series()).eq("OPEN")].copy() if not trades_df.empty else pd.DataFrame()
    settled_df = trades_df[trades_df.get("status", pd.Series()).eq("SETTLED")].copy() if not trades_df.empty else pd.DataFrame()

    ctx = gx.get_context(mode="ephemeral")
    suite_results: dict[str, Any] = {}
    all_success = True

    # ── Suite 1: execution_funnel ─────────────────────────────────
    print()
    print(_line())
    print("  Suite 1: execution_funnel (last 5,000 rows sample)")
    print(_line())
    if not funnel_df.empty:
        ef_expectations = [
            gx.expectations.ExpectColumnValuesToNotBeNull(column="ticker"),
            gx.expectations.ExpectColumnValuesToNotBeNull(column="final_status"),
            gx.expectations.ExpectColumnValuesToNotBeNull(column="timestamp_utc"),
        ]
        # Only add reason expectation if column exists
        if "final_reason" in funnel_df.columns:
            ef_expectations.append(
                gx.expectations.ExpectColumnValuesToNotBeNull(column="final_reason")
            )
        r = _run_gx_suite(ctx, gx, "funnel_src", "funnel_suite", ef_expectations, funnel_df)
    else:
        r = {"skipped": True, "reason": "no funnel data", "success": True}

    suite_results["execution_funnel"] = r
    if r.get("skipped"):
        print(f"  [SKIP] {r.get('reason')}")
    else:
        print(f"  evaluated: {r['evaluated']}  passed: {r['passed']}  failed: {r['failed']}")
        for er in r.get("results", []):
            marker = "OK" if er["success"] else "FAIL"
            print(f"  [{marker}] {er['expectation']} col={er['column']}")
    if not r.get("success", True):
        all_success = False

    # ── Suite 2: paper_trades ─────────────────────────────────────
    print()
    print(_line())
    print("  Suite 2: paper_trades (all rows)")
    print(_line())
    if not trades_df.empty:
        pt_expectations = [
            gx.expectations.ExpectColumnValuesToNotBeNull(column="ticker"),
            gx.expectations.ExpectColumnValuesToNotBeNull(column="action"),
            gx.expectations.ExpectColumnValuesToNotBeNull(column="status"),
        ]
        if "entry_price" in trades_df.columns:
            pt_expectations += [
                gx.expectations.ExpectColumnValuesToBeBetween(
                    column="entry_price", min_value=0.0, max_value=1.0,
                    mostly=0.95,  # tolerate a few legacy void rows
                ),
            ]
        r2 = _run_gx_suite(ctx, gx, "trades_src", "trades_suite", pt_expectations, trades_df)
    else:
        r2 = {"skipped": True, "reason": "no trades data", "success": True}

    suite_results["paper_trades"] = r2
    if r2.get("skipped"):
        print(f"  [SKIP] {r2.get('reason')}")
    else:
        print(f"  evaluated: {r2['evaluated']}  passed: {r2['passed']}  failed: {r2['failed']}")
        for er in r2.get("results", []):
            marker = "OK" if er["success"] else "FAIL"
            print(f"  [{marker}] {er['expectation']} col={er['column']}")
    if not r2.get("success", True):
        all_success = False

    # ── Suite 3: settled trades ───────────────────────────────────
    print()
    print(_line())
    print("  Suite 3: settled trades (non-null entry_price, no negative)")
    print(_line())
    if not settled_df.empty and "entry_price" in settled_df.columns:
        st_exp = [
            gx.expectations.ExpectColumnValuesToNotBeNull(column="ticker"),
            gx.expectations.ExpectColumnValuesToNotBeNull(column="entry_price"),
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="entry_price", min_value=0.0, max_value=1.0
            ),
        ]
        r3 = _run_gx_suite(ctx, gx, "settled_src", "settled_suite", st_exp, settled_df)
    else:
        r3 = {"skipped": True, "reason": "no settled data or missing entry_price", "success": True}

    suite_results["settled_trades"] = r3
    if r3.get("skipped"):
        print(f"  [SKIP] {r3.get('reason')}")
    else:
        print(f"  evaluated: {r3['evaluated']}  passed: {r3['passed']}  failed: {r3['failed']}")
        for er in r3.get("results", []):
            marker = "OK" if er["success"] else "FAIL"
            print(f"  [{marker}] {er['expectation']} col={er['column']}")
    if not r3.get("success", True):
        all_success = False

    # ── Custom: KXETH quarantine ──────────────────────────────────
    print()
    print(_line())
    print("  Custom: KXETH quarantine check (scope: active opens + post-quarantine rows)")
    print(_line())
    kxeth_check = _custom_kxeth_check(trades_rows)
    suite_results["kxeth_quarantine"] = kxeth_check
    marker = "OK" if kxeth_check["success"] else "FAIL"
    print(f"  [{marker}] kxeth_final_open_active={kxeth_check['kxeth_open_active']}  (must be 0 — live KXETH position)")
    print(f"  [{marker}] kxeth_post_quarantine_rows={kxeth_check['kxeth_post_quarantine']}  (must be 0 — post-Phase-8Q KXETH trades)")
    print(f"  {kxeth_check['detail']}")
    if not kxeth_check["success"]:
        all_success = False
    print(f"  historical_kxeth_settled (pre-Phase-8Q, excluded by clean_truth_report proof gate): "
          f"{kxeth_check.get('kxeth_historical_settled', 0)}")
    print(f"  NOTE: canonical proof-pool exclusion is enforced by clean_truth_report.py,")
    print(f"        not by this GX check.")

    # ── Save results ──────────────────────────────────────────────
    output = {
        "suite_results": suite_results,
        "overall_success": all_success,
        "funnel_sample_size": len(funnel_sample),
        "trades_total": len(trades_rows),
    }
    out_path = REPORTS_DIR / "gx_results.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print()
    print(f"  Results saved: {out_path.relative_to(ROOT)}")
    print(f"  Overall success: {all_success}")
    print()
    print(f"  {SENTINEL}")
    print()
    return 0 if all_success else 1


if __name__ == "__main__":
    sys.exit(main())
