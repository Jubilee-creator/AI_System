# SIDE_BALANCED_RESEARCH Mode Spec

Phase 5L design only. This document does not implement execution behavior.

## Current Evidence

- System mode remains RESEARCH_ONLY.
- Proof verdict remains NOT_PROVEN.
- edge_profile_trusted remains false.
- real_money_allowed remains false.
- scale_allowed remains false.
- PaperTrader now honors valid intended_action values BET_YES and BET_NO.
- Controlled BET_NO handoff test returns PROVEN_OK.
- Production paper trade log remains 100% BET_YES.
- Historical scanner rows include BET_NO.
- Historical BET_NO rows reached the execution funnel.
- Historical BET_NO funnel rows have not opened and were blocked by max-open cap.
- Fresh restart-safe production runs observed BET_YES opportunities only and filled available slots before any BET_NO appeared.

## Purpose

SIDE_BALANCED_RESEARCH is a paper-only research mechanism for collecting side coverage evidence across both BET_YES and BET_NO.

It is not a strategy. It is not proof of edge. It must not be used as profitability evidence. It exists only to answer whether natural scanner BET_NO opportunities can pass the normal execution path and produce auditable paper outcomes without being starved by ordering and open-slot dynamics.

## Non-Goals

- Do not synthesize BET_NO from PASS rows.
- Do not invert BET_YES into BET_NO.
- Do not change signal formulas.
- Do not change scanner scoring.
- Do not change scanner global ranking.
- Do not change risk limits.
- Do not change proof gates.
- Do not enable Kelly execution.
- Do not enable real money.
- Do not claim profitability.

## Activation Rules

The mode must be disabled by default.

It may activate only when all conditions are true:

- Config flag `SIDE_BALANCED_RESEARCH_ENABLED` is true.
- System mode is RESEARCH_ONLY.
- Paper trading is enabled.
- Real-money mode is false.
- real_money_allowed is false.
- scale_allowed is false.
- Kelly execution is disabled.
- Risk manager kill switch is off.
- Risk manager cooldown is off.
- Daily loss limit has not been hit.
- Open exposure is within configured paper research limits.

It must hard-fail closed if any real-money or scaling flag is true.

## Candidate Selection

Use only natural scanner opportunities where `action` is BET_YES or BET_NO.

Candidate selection should happen after `market_scanner.py` returns the normal opportunity list. Do not modify scanner formulas or scanner global ranking. The scanner should continue to produce the canonical opportunity stream and logs.

The safest implementation location is a small helper module called from `Dashboard.py`, for example `brain/side_coverage_queue.py`, because:

- `market_scanner.py` should remain responsible for market discovery and signal generation only.
- `PaperTrader` should remain responsible for execution, risk, sizing, and trade logging.
- `Dashboard.py` already owns the scan loop, open-slot context, run_id, scan_id, rank fields, and execution funnel logging.

The helper should receive:

- current scan opportunities
- open trade count
- max open trades
- run_id
- scan_id
- current date/session counters

It should return zero or one side-coverage candidate per configured interval/day.

## Design Options Compared

### A. Reserve One Paper Slot For Highest-Ranked Natural BET_NO

Reserve one paper-only slot for the highest-ranked natural BET_NO when side coverage is enabled.

Pros:

- Directly addresses observed cap starvation.
- Produces real PaperTrader execution evidence when natural BET_NO appears.
- Keeps normal scanner decisions intact.

Cons:

- Competes with normal high-ranked BET_YES candidates.
- Changes paper execution ordering.
- Needs strict proof isolation to avoid fake performance conclusions.

Verdict: acceptable only as disabled-by-default research instrumentation after report exclusions and tests exist.

### B. Separate Side-Coverage Queue After Normal Candidates

Run normal execution first, then allow a side-coverage queue to open a natural BET_NO only if capacity remains.

Pros:

- Does not compete with normal candidates.
- Lower behavior risk.
- Easier to reason about.

Cons:

- Does not solve the observed starvation problem when normal BET_YES fills all slots.
- Likely continues producing zero BET_NO opens.

Verdict: safest operationally but weak evidence value.

### C. Log-Only Shadow Selection

Select the highest-ranked natural BET_NO for audit only, without opening a trade.

Pros:

- No trading behavior change.
- No risk exposure.
- Useful for proving how often BET_NO would have been selected.

Cons:

- Cannot prove production PaperTrader BET_NO execution.
- Cannot produce realized paper outcomes.

Verdict: best first implementation step before any execution mode.

### D. Time-Windowed Natural BET_NO Collection

Allow a natural BET_NO to open only during explicit research windows, subject to all risk checks and daily coverage caps.

Pros:

- Limits exposure and log pollution.
- Can be scheduled when open slots are available.
- Easier to audit.

Cons:

- Still may miss rare BET_NO windows.
- Requires more state and reporting.

Verdict: best execution design when combined with shadow selection and strict daily caps.

## Recommendation

Use a staged C then D design.

Phase 5M should implement log-only shadow side selection first. It should identify the highest-ranked natural BET_NO per run/scan, record why it would or would not be eligible, and prove report isolation before any execution changes.

Only after shadow reports are working should a disabled-by-default execution path be added for D: time-windowed natural BET_NO collection. It should open at most one natural BET_NO per run/day when capacity, risk, market quality, and stop conditions all allow it.

Do not implement A first. Reserving a slot before report exclusions and tests exist is too easy to confuse with a strategy change.

## Execution Path If Approved Later

1. `market_scanner.py` returns normal opportunities unchanged.
2. `Dashboard.py` logs scanner opportunities unchanged.
3. `Dashboard.py` computes rank context unchanged.
4. A new helper evaluates natural BET_NO coverage candidates from the current opportunity list.
5. The helper never edits scanner opportunity contents except adding side-coverage metadata to the execution request/log context.
6. If execution is disabled, the helper emits shadow-only diagnostics.
7. If execution is enabled and all guards pass, Dashboard passes the selected natural BET_NO to `PaperTrader.process_signal()` through the normal path.
8. `intended_action=opp.get("action")` must remain BET_NO.
9. PaperTrader must record scanner_action, intended_action, executed_action, and handoff_action_mismatch.
10. Execution funnel logging must record coverage metadata and final reason.

PaperTrader should not receive synthetic opportunities. It should receive the same MarketData fields and scanner confidence semantics used for normal candidates.

## Slot Logic

Initial implementation must be shadow-only.

If execution is later approved:

- It may use only paper capacity.
- It must not increase global max-open cap.
- It must not bypass duplicate ticker limits.
- It must not run if open slots are zero.
- It should stop after one BET_NO side-coverage execution per day by default.
- It should not compete with normal candidates until shadow data proves that unused-capacity collection is insufficient.
- If a reserved slot is later considered, it must be a separate reviewed phase and remain paper-only.

Recommended initial execution behavior:

- normal candidates execute first;
- if a natural BET_NO was shadow-selected and capacity remains, the helper may submit it;
- if capacity is full, log `final_reason=SIDE_COVERAGE_CAP_FULL`;
- do not reorder normal scanner opportunities in the first execution version.

This is conservative. If it produces no BET_NO opens, the evidence will justify discussing a reserved paper slot later.

## Size Logic

Side-coverage trades must use tiny learning size only.

- Never use Kelly execution.
- Never scale with confidence.
- Never increase size because the trade is for coverage.
- Use `MIN_LEARNING_BET`.
- Respect existing validation caps.
- Respect max exposure and daily loss limits.
- Record `final_bet_size_reason="SIDE_COVERAGE_RESEARCH_MIN_LEARNING"` if implemented.

## Risk Checks

Side-coverage execution must pass the normal PaperTrader and RiskManager path.

It must pass:

- kill switch
- cooldown
- daily loss limit
- max exposure
- max concurrent open trades
- duplicate ticker guard
- market spread / liquidity filters
- stale/missing market data checks
- valid YES/NO price checks
- Council decision path if Council is part of normal execution

It must not bypass Council. It may be marked as data-collection research after Council, but the Council decision and reason must remain logged.

Any risk override behavior already present for learning trades must be explicitly visible in logs through `risk_override_used` and `risk_override_reason`. A future implementation should consider disabling learning-risk overrides for side coverage unless explicitly approved.

## Proof Isolation

Every side-coverage trade must be excluded from normal proof and normal ROI.

Required trade fields:

- `side_coverage_test=True`
- `coverage_mode="SIDE_BALANCED_RESEARCH"`
- `proof_eligible=False`
- `data_collection_override=True`
- `normal_strategy_trade=False`
- `scanner_action`
- `intended_action`
- `executed_action`
- `handoff_action_mismatch`
- `original_rank`
- `side_queue_rank`
- `selected_for_side_coverage_reason`
- `risk_allowed`
- `risk_block_reason`
- `council_decision`
- `council_reason`
- `final_reason`

Reports must:

- exclude side-coverage rows from clean proof;
- exclude side-coverage rows from edge_profile_trusted proof;
- exclude side-coverage rows from normal ROI;
- show side-coverage rows in a separate side coverage research section;
- show BET_YES/BET_NO coverage counts separately from normal paper trades;
- warn if side-coverage rows appear in normal proof populations.

## Failure Cases And Final Reasons

The implementation should log a final reason for every side-coverage candidate:

- `SIDE_COVERAGE_DISABLED`
- `SIDE_COVERAGE_NO_BET_NO_AVAILABLE`
- `SIDE_COVERAGE_SHADOW_ONLY`
- `SIDE_COVERAGE_CAP_FULL`
- `SIDE_COVERAGE_DUPLICATE_TICKER`
- `SIDE_COVERAGE_KILL_SWITCH_ACTIVE`
- `SIDE_COVERAGE_COOLDOWN_ACTIVE`
- `SIDE_COVERAGE_DAILY_LOSS_LIMIT`
- `SIDE_COVERAGE_MAX_EXPOSURE`
- `SIDE_COVERAGE_STALE_MARKET_DATA`
- `SIDE_COVERAGE_MISSING_MARKET_DATA`
- `SIDE_COVERAGE_INVALID_NO_PRICE`
- `SIDE_COVERAGE_WEAK_NO_LIQUIDITY`
- `SIDE_COVERAGE_MARKET_CLOSE_TOO_NEAR`
- `SIDE_COVERAGE_RISK_BLOCKED`
- `SIDE_COVERAGE_COUNCIL_BLOCKED`
- `SIDE_COVERAGE_OPENED`

## Stop Conditions

Stop side coverage immediately if:

- kill switch activates;
- daily loss limit is hit;
- cooldown activates;
- prices are stale;
- market data is missing;
- NO price is invalid;
- BET_NO liquidity is too weak;
- market close is too near;
- max side-coverage trades per day is reached;
- side-coverage rows appear in normal proof metrics;
- real-money mode or scaling becomes enabled.

## Config Flags For Future Phase

Recommended disabled-by-default config:

- `SIDE_BALANCED_RESEARCH_ENABLED = False`
- `SIDE_BALANCED_RESEARCH_EXECUTE = False`
- `SIDE_BALANCED_RESEARCH_SHADOW_ONLY = True`
- `SIDE_BALANCED_RESEARCH_REQUIRE_PAPER_ONLY = True`
- `SIDE_BALANCED_RESEARCH_MAX_TRADES_PER_DAY = 1`
- `SIDE_BALANCED_RESEARCH_MIN_NO_LIQUIDITY = MIN_VOLUME`
- `SIDE_BALANCED_RESEARCH_MIN_TIME_TO_EXPIRY_MINUTES = 5`
- `SIDE_BALANCED_RESEARCH_SIZE = MIN_LEARNING_BET`
- `SIDE_BALANCED_RESEARCH_PROOF_ELIGIBLE = False`

The code must validate that execute mode cannot be true unless enabled is true, paper-only is true, real money is false, and proof eligibility is false.

## Required Tests For Implementation

Add tests before execution behavior:

- side coverage cannot run in real-money mode;
- side coverage cannot run when scaling is allowed;
- side coverage cannot run with kill switch active;
- side coverage cannot run during cooldown;
- side coverage rows always have `proof_eligible=False`;
- side coverage rows always have `normal_strategy_trade=False`;
- side coverage rows are excluded from clean proof and edge-profile trust;
- risk manager is called for any executable side-coverage candidate;
- intended_action=BET_NO remains executed_action=BET_NO;
- no PASS row can become BET_NO;
- no BET_YES row can be inverted into BET_NO;
- no synthetic opportunity is written to normal logs;
- report_side_coverage, report_signal_bias, and performance/proof reports separate coverage rows from normal rows.

## Files To Touch In Phase 5M

Recommended order:

1. `config/trading_config.py`
   - Add disabled-by-default flags.
   - Add validation that execution cannot be enabled outside paper-only research.

2. New `tools/report_side_coverage.py`
   - Read scanner/funnel/paper logs.
   - Show shadow-selected natural BET_NO candidates.
   - Show exclusion checks.

3. Existing proof/performance reports
   - Exclude `side_coverage_test=True` from normal proof populations.
   - Add warnings if coverage rows leak into normal metrics.

4. New `brain/side_coverage_queue.py`
   - Shadow-only candidate selection first.
   - No execution behavior in the first patch.

5. `Dashboard.py`
   - Call helper after normal rank context is built.
   - Log shadow diagnostics.
   - Do not execute side coverage until a later explicit phase.

6. Tests / controlled tools
   - Add a small synthetic natural scanner-opportunity test harness.
   - Keep all synthetic output outside normal logs.

## Implementation Sequence Recommendation

Phase 5M should not start with execution.

Safe order:

1. Add config flags first.
2. Add report exclusions first.
3. Add tests first.
4. Add shadow-only candidate selection.
5. Verify no normal metrics change except new side-coverage diagnostics.
6. Only then add disabled-by-default execution path.
7. Run one controlled paper-only coverage session after explicit approval.

## Brutally Honest Bottom Line

Side-balanced research mode is justified as instrumentation, not as a trading improvement.

The evidence is not good: paper trades are still 100% BET_YES, proof is not established, and current signal construction still has structural concerns. A side-coverage mode would answer a narrow execution/evidence question. It must not be allowed to become a backdoor for forcing trades, improving dashboards, or claiming edge.

