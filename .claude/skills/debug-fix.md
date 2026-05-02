# /debug-fix

Purpose:
Fix bugs properly by identifying root cause and preventing future issues.

Use when:
Something is broken, not working, or behaving unexpectedly.

---

## PROCESS

1. Reproduce the issue
- What exactly is broken?
- When does it happen?

2. Identify root cause
- Logic issue?
- UI issue?
- Data issue?

3. Fix minimally
- Change ONLY what is needed
- Do not refactor unrelated code

4. Verify fix
- Test behavior again
- Confirm issue is resolved

5. Prevent recurrence
- Add guard or note if needed

---

## OUTPUT

1. ISSUE
- What was broken

2. ROOT CAUSE
- Why it happened

3. FIX
- What changed

4. VERIFICATION
- How it was tested

5. PREVENTION
- How to avoid again

---

## RULES

- No guessing fixes
- No random changes
- Root cause > quick patch
