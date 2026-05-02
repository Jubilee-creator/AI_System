# /fix-edge

Purpose:
Improve the AI_System edge/profitability logic without fake progress, unsafe shortcuts, or random redesign.

Use when:
Samuel wants to investigate negative ROI, bad CLV, weak payoff ratio, bad blocker mix, proof deadlock, or why the system is not profitable yet.

---

## CORE MISSION

Find why the engine is not producing proven positive expectancy.

Do NOT make the system “look profitable.”
Make the evidence cleaner and the logic stronger.

---

## FIRST RULE

Before editing code:
- scan relevant files
- run truth reports
- inspect current metrics
- identify root cause
- explain plan first

Never patch blind.

---

## REQUIRED COMMANDS

Run before diagnosis:

```bash
python3 tools/report_health.py
python3 tools/report_scale_readiness.py
python3 tools/report_real_money_lockdown.py
python3 tools/audit_execution_blockers.py
python3 tools/audit_open_trade_state.py
