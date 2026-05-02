# /report-truth

Purpose:
Create a brutally honest truth report for AI_System without hype, fake progress, or misleading green metrics.

Use when:
Samuel asks whether the engine is profitable, safe, proven, ready, blocked, or improving.

---

## PROCESS

1. Run truth reports:
- python3 tools/report_health.py
- python3 tools/report_scale_readiness.py
- python3 tools/report_real_money_lockdown.py
- python3 tools/audit_execution_blockers.py
- python3 tools/audit_open_trade_state.py

2. Separate reality from appearance:
- What looks good?
- What is actually proven?
- What is still unproven?
- What is stale/misleading?
- What is blocked for valid reasons?

3. Check core gates:
- normal_modern
- clean_settled
- modern_full_metadata
- ROI
- CLV
- profit_factor
- edge_profile_trusted
- scale_allowed
- real_money_allowed

4. Identify danger:
- fake green dashboard states
- excluded rows counted as proof
- stale dashboard state
- missing metadata
- weak edge math
- negative payout asymmetry

---

## VERDICT RULES

Use strict language:

- PROFITABLE = only if ROI > 0, CLV > 0, PF > 1.10, enough normal_modern proof
- PROVEN = only if proof gates pass
- SAFE_TO_SCALE = only if all scale gates pass and hardcoded locks are intentionally changed with approval
- REAL_MONEY_READY = almost always NO unless explicitly proven and approved

Do not soften bad metrics.

---

## OUTPUT FORMAT

1. SYSTEM VERDICT
- profitable / not profitable
- proven / not proven
- safe / unsafe
- real-money-ready / absolutely not

2. CURRENT EVIDENCE
- clean_settled
- modern_full
- normal_modern
- ROI
- CLV
- profit factor
- blocker mix

3. WHAT IS WORKING
- real confirmed positives

4. WHAT IS NOT WORKING
- real failures

5. WHAT IS MISLEADING
- anything that looks better than it is

6. CURRENT BOTTLENECK
- the single main blocker

7. NEXT BEST ACTION
- one exact command or next investigation

8. HONEST EXPERT ADVICE
- what to fix
- what not to touch
- what would be dangerous
