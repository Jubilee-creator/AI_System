#!/usr/bin/env bash
# Phase 11C — Run the full observability stack in order.
# READ-ONLY: no strategy changes, no log mutation, no live execution.
set -euo pipefail

PYTHON=".venv_observability/bin/python"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "================================================================"
echo "  Phase 11C Observability Stack"
echo "  $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "================================================================"

run_step() {
    local script="$1"
    echo ""
    echo "----------------------------------------------------------------"
    echo "  Running: $script"
    echo "----------------------------------------------------------------"
    "$PYTHON" "$ROOT/$script" || {
        echo ""
        echo "FAILED: $script"
        echo "Check output above for error details."
        exit 1
    }
}

run_step tools/check_observability_stack.py
run_step tools/build_duckdb_truth_warehouse.py
run_step tools/report_duckdb_post11a_root_cause.py
run_step tools/report_gx_log_quality.py
run_step tools/report_evidently_post11a_drift.py
run_step tools/report_mlflow_phase_metrics.py

echo ""
echo "================================================================"
echo "  OBSERVABILITY STACK COMPLETE"
echo "================================================================"
echo ""
echo "  Generated artifacts (all gitignored):"
echo "    data/observability/ai_system_truth.duckdb"
echo "    data/observability/mlruns/"
echo "    reports/observability/gx_results.json"
echo "    reports/observability/evidently_drift_report.html"
echo "    reports/observability/evidently_drift_results.json"
echo ""
echo "  Next step:"
echo "    Review reports in reports/observability/"
echo "    Run: .venv_observability/bin/python tools/test_observability_stack.py"
echo ""
