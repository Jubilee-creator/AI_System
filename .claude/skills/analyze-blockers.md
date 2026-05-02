# /analyze-blockers

Purpose:
Analyze why AI_System is not opening trades and identify which blockers are correct, misleading, stale, or worth fixing.

Use when:
Samuel sees 0 trades opened, high blocker counts, deadlock warnings, or confusing dashboard/report behavior.

---

## PROCESS

1. Run reports:
- python3 tools/audit_execution_blockers.py
- python3 tools/report_health.py
- python3 tools/audit_open_trade_state.py

2. Classify blockers:
- BLOCKED_MARKET_QUALITY = usually correct filter
- BLOCKED_MIN_EDGE = usually correct filter
- BLOCKED_EDGE_DANGER_GUARD = high-edge safety guard
- BLOCKED_COUNCIL = trust/proof gate
- BLOCKED_RISK = risk manager or post-council edge issue
- BLOCKED_MAX_OPEN_TRADES = cap issue only if open trades are stale

3. Diagnose:
- Is the system correctly rejecting bad markets?
- Is stale state blocking trades?
- Is council blocking because edge profile is untrusted?
- Is edge disappearing after council adjustment?
- Are blocker reasons missing or null?
- Is bootstrap_era_allow actually broken, or has no qualifying signal passed yet?

4. Decide:
- Correct blocker = do not bypass
- Missing reason = fix logging/reporting
- Stale cap = fix sync
- Bootstrap warning after restart = inspect logic before patching
- Weak edge = investigate pricing, probability, payout, and CLV

---

## FORBIDDEN

Do NOT:
- loosen thresholds just to create trades
- bypass council or risk
- count blocked signals as proof
- fake green dashboard metrics
- call valid blockers bugs

---

## OUTPUT FORMAT

1. BLOCKER VERDICT
2. BLOCKER COUNTS
3. CORRECT BLOCKERS
4. SUSPICIOUS BLOCKERS
5. ROOT CAUSE
6. FIX PRIORITY
7. NEXT COMMAND
8. HONEST ADVICE
