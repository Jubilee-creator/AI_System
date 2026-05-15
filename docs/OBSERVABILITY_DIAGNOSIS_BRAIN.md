# Observability Diagnosis Brain — Phase 11D

READ-ONLY audit layer.  No strategy changes.  No real money.  No scale.  No Kelly.

---

## Purpose

`tools/report_observability_diagnosis_brain.py` is the top-level synthesis layer of
the Phase 11C observability stack.  It combines canonical truth (the ground source)
with all observability tool outputs (MLflow, GX, Evidently, DuckDB) to produce a
ranked, evidence-grounded list of 15 system weakness diagnoses.

It does not change any system state.  It cannot trigger trades.  It reads logs only.

---

## Trust Hierarchy

```
canonical truth (performance_report + clean_truth_report)   ← ALWAYS WINS
  ↑ cross-checked by
MLflow (report_mlflow_phase_metrics._compute_metrics)
  ↑ cross-checked by
DuckDB post-11A counts (data/observability/ai_system_truth.duckdb)
  ↑ supporting signal only
GX results (reports/observability/gx_results.json)
Evidently drift (reports/observability/evidently_drift_results.json)
```

If any observability tool disagrees with canonical truth, the diagnosis brain flags
it as `OBSERVABILITY_CONTRADICTION` and canonical truth takes precedence.  The brain
does not patch, reweight, or suppress canonical truth.

---

## Diagnosis Categories

| # | Category | What it measures |
|---|---|---|
| 1 | `SAMPLE_TOO_SMALL` | Whether post-11A settled trades are ≥ 30 (decision threshold) |
| 2 | `PAYOFF_ASYMMETRY` | Gross win vs gross loss; profit factor vs gate |
| 3 | `ENTRY_PRICE_TOXICITY` | % expensive entries (≥ 0.80); average reward-to-risk |
| 4 | `CONFIDENCE_OVERCONFIDENCE` | Model avg confidence vs actual win rate |
| 5 | `LOW_OR_NEGATIVE_CLV` | Canonical CLV (exit_price − entry_price) vs 0 |
| 6 | `PROFIT_FACTOR_FAILURE` | PF vs scale gate threshold (1.10) |
| 7 | `ROI_FAILURE` | ROI on normal_modern and post-11A cohorts |
| 8 | `SCANNER_CONCENTRATION` | Ticker prefix concentration; top-3 prefix share |
| 9 | `SIDE_BIAS` | BET_YES vs BET_NO asymmetry |
| 10 | `DATA_QUALITY_RISK` | GX overall_success; conflicted_settled excluded count |
| 11 | `DRIFT_OR_REGIME_SHIFT` | Evidently dataset drift between pre/post-11A windows |
| 12 | `BLOCKER_STARVATION` | Post-11A pass rate = unique_opened / funnel_candidates; filter stage breakdown |
| 13 | `KXETH_QUARANTINE_HEALTH` | GX kxeth_final_open_active and kxeth_post_quarantine |
| 14 | `SAFETY_LOCK_HEALTH` | Verification of 5 safety constants from config |
| 15 | `OBSERVABILITY_CONTRADICTION` | Canonical vs tool disagreements |

---

## Severity Scale

| Level | Weight | Meaning |
|---|---|---|
| CRITICAL | 5 | Requires immediate investigation or blocking condition |
| HIGH | 4 | Significant performance or structural problem |
| MEDIUM | 3 | Notable weakness; watch and monitor |
| LOW | 2 | Minor signal; no action warranted now |
| INFO | 1 | Healthy — no problem detected |

Diagnoses are sorted descending by severity weight, then alphabetically by category.
Rank 1 = highest priority item.

---

## Canonical CLV Definition

`avg_clv` in this report means **exit_price − entry_price** (what actually happened
vs what was paid).  This is the canonical definition from `performance_report.get_clv()`.

`avg_model_margin` (council_confidence − entry_price) is logged separately by MLflow
for diagnostic purposes only and is NOT CLV.  These two numbers are not interchangeable.

On current data:
- avg_clv = negative (trades settling below entry price on average)
- avg_model_margin = positive (model is confident, but confidence is not CLV)

Using model margin as CLV would produce a false positive — the diagnosis brain
explicitly guards against this.

---

## Post-11A Sample Warning

As of Phase 11D, there are **5 post-11A settled trades**.  Any performance conclusion
drawn from n=5 has extremely wide confidence intervals and should not be acted on.

`SAMPLE_TOO_SMALL` is rated **CRITICAL** until post-11A settled trades ≥ 30.

Evidently drift results are marked `EXPLORATORY_ONLY_NOT_DECISION_GRADE` when
post-11A n < 30.

---

## Running the Report

```bash
# Canonical-only mode — no duckdb package required
python3 tools/report_observability_diagnosis_brain.py

# Full mode — includes DuckDB cross-check (requires .venv_observability)
.venv_observability/bin/python tools/report_observability_diagnosis_brain.py

# Full stack first, then diagnosis brain
bash tools/run_observability_stack.sh
.venv_observability/bin/python tools/report_observability_diagnosis_brain.py
```

The report runs in two modes:

**Canonical-only mode** (`python3`):
- Canonical truth is computed from JSONL logs using `performance_report` + `clean_truth_report`.
- Funnel/candidate denominator for BLOCKER_STARVATION falls back to parsing `logs/execution_funnel.jsonl` directly (read-only).
- DuckDB cross-check is skipped if the `duckdb` package is not installed.
- If the DuckDB warehouse (`data/observability/ai_system_truth.duckdb`) exists but the package is unavailable, the report prints `DUCKDB_WAREHOUSE_PRESENT_BUT_PACKAGE_UNAVAILABLE` and sets `tools_agree_with_canonical: PARTIAL_DUCKDB_SKIPPED`.
- `PARTIAL_DUCKDB_SKIPPED` is not a failure — it means the available tools agree, but the DuckDB cross-check was not performed.

**Full mode** (`.venv_observability/bin/python`):
- All of the above, plus DuckDB cross-check using the warehouse.
- `tools_agree_with_canonical: YES` when all tools agree.

---

## Tests

```bash
python3 tools/test_observability_diagnosis_brain.py
```

Tests T20–T39 verify:

| Test | What it checks |
|---|---|
| T20 | Sentinel `OBSERVABILITY_DIAGNOSIS_BRAIN_OK` is present in module |
| T21 | No source log files are mutated during `run_diagnosis()` |
| T22 | Safety constants are unchanged after the report runs |
| T23 | KXETH quarantine diagnosis recommends NOT removing the quarantine |
| T24 | Internal threshold constants match expected values |
| T25 | `SAMPLE_TOO_SMALL` is CRITICAL when post-11A n < 30 |
| T26 | Negative ROI is not dismissed as INFO |
| T27 | PF < 1.0 is not dismissed as INFO |
| T28 | Negative CLV is not dismissed as INFO |
| T29 | Fake-positive in observability data triggers `OBSERVABILITY_CONTRADICTION` |
| T30 | Agreeing obs → LOW severity; disagreeing obs → higher severity |
| T31 | All 15 diagnosis categories are present in output |
| T32 | `SAFETY_LOCK_HEALTH` is INFO when all 5 constants are correct |
| T33 | `SAFETY_LOCK_HEALTH` is CRITICAL when any constant is tampered |
| T34 | Diagnosis ranks are sequential 1..N |
| T35 | `BLOCKER_STARVATION` uses funnel/candidate denominator on live data |
| T36 | Synthetic: candidates=1000, opened=5, raw_rows=10 → 0.500% not 50% |
| T37 | `_compute_agree_status` returns `PARTIAL_DUCKDB_SKIPPED` when warehouse unavailable |
| T38 | `_compute_agree_status` returns `YES` / `NO` correctly |
| T39 | `SIDE_BIAS` evidence percentages are dynamic (not hardcoded to 0%) |

---

## What This Does NOT Do

- It does not patch live behavior.
- It does not change thresholds.
- It does not make trades profitable by relabeling data.
- It does not override safety locks.
- It does not count conflicted_settled rows as proof.
- It does not use model margin as a substitute for CLV.
- It does not dismiss negative ROI/PF/CLV as acceptable.
- It does not trigger any real-money or scale logic.

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

---

## Sentinel

`OBSERVABILITY_DIAGNOSIS_BRAIN_OK` — printed at the end of a successful run.
`TEST_OBSERVABILITY_DIAGNOSIS_BRAIN_OK` — printed when all tests pass.
