# Observability Stack — Phase 11C

READ-ONLY audit and data-quality layer.  No strategy changes.  No real money.  No scale.  No Kelly.

---

## Stack Components

### DuckDB — Local Truth Warehouse

- **Script:** `tools/build_duckdb_truth_warehouse.py`
- **Output:** `data/observability/ai_system_truth.duckdb` (gitignored)
- **Purpose:** Ingests all JSONL logs into a queryable columnar store.  `execution_funnel.jsonl` (230MB) is streamed via DuckDB's native NDJSON reader to avoid memory pressure; smaller logs are loaded via pandas.
- **Tables:** `execution_funnel`, `paper_trades`, `payoff_aware_shadow_ranking`, `upstream_hygiene_shadow`
- **Views:** `post_11a_execution_funnel`, `post_11a_paper_trades`, `post_11a_opened_trades`, `post_11a_settled_trades`

Queries: `tools/report_duckdb_post11a_root_cause.py`

---

### Great Expectations — Log Data Quality

- **Script:** `tools/report_gx_log_quality.py`
- **Version:** GX 1.17.2 (ephemeral context — no cloud, no filesystem context)
- **Output:** `reports/observability/gx_results.json` (gitignored)
- **Purpose:** Validates that JSONL logs meet structural contracts: required columns non-null, entry_price in (0, 1], known status values.
- **API used:** `context.data_sources.add_pandas(...)` → `ValidationDefinition.run(batch_parameters={"dataframe": df})`
- **KXETH check scope:** Checks that (1) no KXETH trade has a final-active OPEN status after `(ticker, timestamp)` deduplication, and (2) no KXETH row was created after the Phase 8Q quarantine date. Historical pre-quarantine KXETH settled rows are present and expected; their exclusion from the canonical proof pool is enforced by `clean_truth_report.py evaluate_proof_gates()`, not by GX.

---

### Evidently — Distribution Drift Monitor

- **Script:** `tools/report_evidently_post11a_drift.py`
- **Version:** Evidently 0.7.21 (legacy API)
- **Output:** `reports/observability/evidently_drift_report.html`, `evidently_drift_results.json` (both gitignored)
- **Purpose:** Compares pre-11A vs post-11A numeric distributions (`entry_price`, `confidence`, `edge`, `pnl`) on settled trades.  If either window has fewer than 5 rows, prints `NOT_ENOUGH_DATA` and exits cleanly.
- **API used:** `from evidently.legacy.report import Report` + `DataDriftPreset`
- **Sample size caveat:** When post-11A settled count < 30, the report prints `EXPLORATORY_ONLY_NOT_DECISION_GRADE` and records `NOT_ENOUGH_POST11A_ROWS_FOR_STRONG_DRIFT_INFERENCE` in the JSON output. As of Phase 11C, n=5 post-11A settled trades — results are indicative only.

---

### MLflow — Phase Experiment Tracker

- **Script:** `tools/report_mlflow_phase_metrics.py`
- **Version:** MLflow 3.12.0 (local file-based tracking only)
- **Output:** `data/observability/mlruns/` (gitignored)
- **Purpose:** Logs phase-level proof and performance metrics (`normal_modern`, `modern_roi`, `avg_clv`, `profit_factor`, blocker counts) for longitudinal tracking across phases.
- **Experiment:** `ai_system_phases`
- **No secrets logged.**
- **Metric semantics:**
  - `avg_clv` — canonical: `exit_price − entry_price` (same as `performance_report.get_clv()`). Negative values mean trades settled below entry price on average.
  - `avg_model_margin` — diagnostic only: `council_confidence − entry_price`. Measures model confidence relative to market price. Do NOT confuse with canonical CLV.
  - `post_11a_unique_opened` — count of unique opened trades post-11A (deduplicated by `(ticker, timestamp)`). Each trade counted once regardless of whether it has both OPEN and SETTLED rows in the log.
  - `post_11a_raw_paper_rows` — raw row count including both OPEN and SETTLED rows for the same trades.
  - Cohort classification (`modern_full`, `normal_modern`, etc.) uses the canonical `row_quality_group()` logic: requires `risk_edge` + `model_probability` + quote metadata.

---

### Claude — Builder / Surgeon

Claude Code is used to write, modify, and reason about system code.  It operates under strict read-only constraints for audit phases (11A–11C) and is responsible for surgical fixes only (as in Phase 11A).  Claude never enables real money, scale, or Kelly.

---

### Codex — Reviewer / Auditor

Codex reviews proposed changes before commit via the Ultrareview workflow.  It acts as an independent check against the audit rules in `CLAUDE.md`.

---

## Running the Stack

```bash
# Full stack (requires .venv_observability)
bash tools/run_observability_stack.sh

# Individual tools
.venv_observability/bin/python tools/check_observability_stack.py
.venv_observability/bin/python tools/build_duckdb_truth_warehouse.py
.venv_observability/bin/python tools/report_duckdb_post11a_root_cause.py
.venv_observability/bin/python tools/report_gx_log_quality.py
.venv_observability/bin/python tools/report_evidently_post11a_drift.py
.venv_observability/bin/python tools/report_mlflow_phase_metrics.py

# Tests
.venv_observability/bin/python tools/test_observability_stack.py
```

---

## Generated Artifacts (all gitignored)

| Path | Tool | Contents |
|---|---|---|
| `data/observability/ai_system_truth.duckdb` | DuckDB | Truth warehouse DB |
| `data/observability/mlruns/` | MLflow | Experiment run files |
| `reports/observability/gx_results.json` | GX | Data quality results |
| `reports/observability/evidently_drift_report.html` | Evidently | Drift HTML report |
| `reports/observability/evidently_drift_results.json` | Evidently | Drift JSON summary |

---

## What This Proves

- JSONL logs have structural integrity (required fields, value ranges).
- No active (final-open) KXETH trades exist; no post-quarantine KXETH trades were opened.
- Post-11A blocker distribution matches expected pattern.
- Phase metrics are logged for longitudinal tracking with canonical classification.
- No structural data quality anomalies in entry_price or confidence fields.

## What This Does NOT Prove

- Strategy edge or profitability.
- That post-11A trades will be profitable.
- That sample size is sufficient — 5 post-11A settled trades as of Phase 11C is far below the 30-trade threshold. Evidently drift results are exploratory only.
- That the model is correctly calibrated.
- That real-money trading is safe or appropriate.
- That GX validates the full canonical proof-pool exclusion (it does not).

---

## Safety Locks — Unchanged

All safety locks from `CLAUDE.md` remain in full effect:

- `TRADING_MODE = PAPER`
- `real_money_allowed = False` (hardcoded)
- `scale_allowed = False` (hardcoded)
- `GLOBAL_FORCED_LEARNING_MODE = True` (Kelly disabled)
- `KXETH quarantine = ACTIVE`
- `MIN_EDGE = 0.03`, `MIN_CONFIDENCE = 0.65`
- `EDGE_DANGER_HIGH_EDGE_MIN = 0.08`

This observability layer does not touch any of these.
