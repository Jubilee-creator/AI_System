# Parked Research Reports — Phase 9P-3

**Archived:** 2026-05-10 (Phase 9P-3 repo hygiene cleanup)
**Archived by:** Phase 9P-3 cleanup commit
**Reason:** These files were untracked research artifacts created during phases
8W through 9H. None are part of the primary active proof/safety report suite.
Parking removes them from the working tree to prevent accidental `git add .`
staging, stale-report confusion, and commit hygiene risk.

---

## Status at Archive Date

- System status: RESEARCH_ONLY / WATCHLIST
- proof_verdict: WATCHLIST
- normal_modern: 114 / 30 (trust gate passed)
- real_money_allowed: NO (hardcoded)
- scale_allowed: NO (hardcoded)
- KXETH quarantine: ACTIVE

## Test Results at Archive Date

Both test suites ran on the live edge_profile.json before archiving:

- `test_edge_profile_kxeth_exclusion.py` — **15/15 PASS** (PROVEN_KXETH_EXCLUSION_OK)
- `test_edge_profile_normal_modern_filter.py` — **16/16 PASS** (PROVEN_1D_POPULATION_INTEGRITY_OK)

The underlying properties they verify (KXETH quarantine in all profile buckets,
Phase 9H transparency fields) remain valid as of archive date.

---

## Files and Their Phase Origins

| File | Phase | Description |
|------|-------|-------------|
| `report_daily_cap_risk_autopsy.py` | 9B | Daily cap / risk gate autopsy. Hardcoded `PHASE_9A_DATE = "2026-05-07"`. Otherwise reads live paper_trades.jsonl. |
| `report_edge_profile_contamination_audit.py` | 9E | KXETH contamination audit of edge_profile.json. Reads live profile + trades. |
| `report_expectancy_autopsy.py` | (core dep) | Core expectancy math. Imported by 4 other files in this group. Reads live paper_trades.jsonl + edge_profile.json. |
| `report_expectancy_math_engine.py` | (core dep) | Math engine. Has hardcoded label `"n=75, normal_modern"` in print output (stale — was 75 when written, now 114). Computed metrics are live. |
| `report_post_quarantine_monitor.py` | (post-9A) | Post-quarantine performance split. Reads live execution_funnel.jsonl for quarantine activation timestamp. |
| `report_price_quality_research.py` | (post-9A) | Price quality research. Has hardcoded `"n=26 (need 4 more for PROVEN_EDGE)"` (stale). Computed 0.80-0.90 bucket metrics are live. |
| `report_quarantine_simulation.py` | (post-9A) | Quarantine simulation. Has explicit OVERFITTING WARNING. Read-only. |
| `report_shadow_price_gate.py` | (post-9A) | Shadow price gate simulator. Has hardcoded `"n<30"` conclusion text (stale). Shadow gate simulations are live. |
| `report_zero_open_throughput_autopsy.py` | 8W | Phase 8W zero-throughput diagnosis. Has hardcoded `"73 normal_modern trades"` (was 73 when written, now 114). Describes a historical deadlock that was resolved. |
| `test_edge_profile_kxeth_exclusion.py` | 9F | KXETH exclusion test suite. 15/15 pass at archive date. |
| `test_edge_profile_normal_modern_filter.py` | 9H | Profile population integrity tests. 16/16 pass at archive date. |

---

## Stale Hardcoded Text — Known Issues

These files contain hardcoded print statements that captured a snapshot of
system state at the time they were written. The computed metrics below those
lines are live, but the labels are stale:

| File | Stale text | Actual current value |
|------|-----------|---------------------|
| `report_expectancy_math_engine.py` | `"n=75, normal_modern"` label | normal_modern = 114 |
| `report_price_quality_research.py` | `"n=26 (need 4 more for PROVEN_EDGE)"` | need 0 more — n>30 now |
| `report_shadow_price_gate.py` | `"Sample just below PROVEN_EDGE threshold (n<30)"` | n=114 now exceeds 30 |
| `report_zero_open_throughput_autopsy.py` | `"73 normal_modern trades"` | 114 as of archive |

**Do not use these files as authoritative proof reports without refreshing the stale text.**

---

## Internal Dependencies

`report_expectancy_autopsy.py` is imported by four other files in this group:

- `report_expectancy_math_engine.py`
- `report_post_quarantine_monitor.py`
- `report_price_quality_research.py`
- `report_quarantine_simulation.py`
- `report_shadow_price_gate.py`

If you revive any of these, ensure `report_expectancy_autopsy.py` is
also available (move it back to `tools/` first).

`config/trading_config.py` line ~382 has a comment reference to
`report_quarantine_simulation.py` — that is a documentation comment only,
not an import. No live runtime code imports anything from this archive.

---

## Active Truth Reports (DO NOT confuse with these parked files)

Use these instead for current system state:

```
python3 tools/report_health.py
python3 tools/report_scale_readiness.py
python3 tools/report_real_money_lockdown.py
python3 tools/report_accounting_version_proof_cohorts.py
python3 tools/report_profile_freshness_watchdog.py
python3 tools/report_pnl_accounting_reconciliation.py
python3 tools/report_payoff_accounting_truth_audit.py
python3 tools/report_2d_clv_payoff_cells.py
```

---

## Revival Instructions

To revive a file from this archive:

1. Move it back to `tools/`: `mv archive/parked_research_reports_phase_9p3/<file> tools/`
2. If it has stale hardcoded text, update the labels before committing
3. Run `python3 -m py_compile tools/<file>` to verify
4. Run `python3 tools/<file>` in read-only mode to verify output is coherent
5. Check that it does NOT modify any log/data/config file
6. Commit it as an active tool only if it passes all checks

---

## Safety Confirmation

All files in this archive are READ-ONLY research tools.
None of them:
- write to paper_trades.jsonl
- write to execution_funnel.jsonl
- write to edge_profile.json
- write to risk_state.json
- modify thresholds, config, or trading logic
- enable real money or scale

They were parked for hygiene, not because they are dangerous.
