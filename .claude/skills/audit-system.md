# /audit-system

Purpose:
Audit the AI_System engine honestly before any code changes.

Use when:
Samuel wants to inspect the trading engine, dashboard, proof gates, blockers, edge logic, or profitability state.

---

## PROCESS

1. Scan files first
- Read relevant files before editing
- Do not patch blind
- Identify current architecture and active logic

2. Run truth reports
- python3 tools/report_health.py
- python3 tools/report_scale_readiness.py
- python3 tools/report_real_money_lockdown.py
- python3 tools/audit_execution_blockers.py
- python3 tools/audit_open_trade_state.py

3. Identify truth state
- Is system profitable?
- Is edge proven?
- Are proof gates passing?
- Are blockers correct?
- Is dashboard honest?

4. Diagnose weak points
- bad math
- misleading metrics
- stale state
- blocker deadlocks
- proof contamination
- fake green states
- missing metadata

5. Output brutal audit
- what is broken
- what is working
- what is misleading
- what must NOT be touched
- highest-leverage fix

---

## RULES

- Do not enable real money
- Do not enable scale
- Do not weaken gates
- Do not bypass risk/council logic
- Do not count excluded rows as proof
- Do not fake progress

---

## OUTPUT FORMAT

1. SYSTEM VERDICT
2. CURRENT TRUTH STATE
3. WHAT IS WORKING
4. WHAT IS BROKEN
5. WHAT IS MISLEADING
6. ROOT CAUSE
7. FIX PRIORITY
8. NEXT COMMAND SAMUEL SHOULD RUN
9. HONEST EXPERT ADVICE
