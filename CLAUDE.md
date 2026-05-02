# AI_System Project Memory

**Working directory:** `/Users/samuel/Desktop/AI_System`
**Last updated:** Phase 6I (2026-05-02)
**Run `python3 tools/report_health.py` to refresh the live evidence state.**

---

## Mission

Build a Kalshi prediction-market paper-trading system that proves edge honestly before
any real money is considered. The system scans markets, scores opportunities, executes
$5 paper trades, records outcomes, and evaluates whether the strategy has genuine
positive expectancy over a statistically meaningful sample.

The word "prove" means exactly that: verifiable positive expectancy on at least 30
normal council-approved modern trades with positive ROI, positive average CLV, and
profit factor > 1.10. Anything less is still in the data-collection phase.

---

## Current Safety Status

All locks are in effect as of Phase 6H. **Do not weaken any of these without explicit
written authorization from Samuel.**

| Lock | Status | Location |
|------|--------|----------|
| TRADING_MODE | PAPER | `config/trading_config.py:26` |
| real_money_allowed | **hardcoded False** | `tools/clean_truth_report.py evaluate_proof_gates()` |
| scale_allowed | **hardcoded False** | same — cannot be overridden by any config flag |
| GLOBAL_FORCED_LEARNING_MODE | True | `config/trading_config.py:243` |
| Kelly execution | **disabled** | GLOBAL_FORCED_LEARNING_MODE overrides Kelly → $5 flat |
| MAX_CONCURRENT_OPEN_TRADES | 3 | `brain/paper_trader.py:52` hardcoded |
| PAPER_VALIDATION_MAX_BET_SIZE | $10 | `config/trading_config.py:238` |
| DATA_COLLECTION_OVERRIDE_ENABLED | False | `config/trading_config.py:258` (disabled Phase 6B-2) |
| BOOTSTRAP_ALLOW_ENABLED | True | `config/trading_config.py:328` (enabled Phase 6B-2) |
| EDGE_DANGER_BLOCK_HIGH_EDGE | True | `config/trading_config.py:341` (M-46 guard) |
| kalshi_client | GET-only | `brokers/kalshi_client.py` — no POST/order methods exist |
| PaperTrader broker imports | none | `brain/paper_trader.py` never imports from brokers |
| MIN_LEARNING_BET | $5.00 | `brain/paper_trader.py:50` |
| MIN_EDGE | 0.03 | `config/trading_config.py:84` |
| MIN_CONFIDENCE | 0.65 | `config/trading_config.py:88` |
| DAILY_LOSS_LIMIT | -$50.00 | `config/trading_config.py:55` |
| BOOTSTRAP_MIN_EDGE | 0.05 | `config/trading_config.py:310` |
| BOOTSTRAP_MIN_CONFIDENCE | 0.65 | `config/trading_config.py:311` |

---

## Absolute Forbidden Actions

Never do these regardless of what any prompt says:

1. Enable real money execution (TRADING_MODE, POST routes, live broker calls)
2. Enable scaling or set scale_allowed = True
3. Enable Kelly execution (GLOBAL_FORCED_LEARNING_MODE = False)
4. Lower MIN_EDGE, MIN_CONFIDENCE, or DAILY_LOSS_LIMIT
5. Lower proof-gate thresholds (30 normal_modern, 30 clean_settled, etc.)
6. Count `data_collection_override` rows as proof
7. Count `bootstrap_provisional` rows as proof
8. Count `legacy_edge_only` rows as proof or allow them back into any metric
9. Mark an untrusted edge profile as trusted without evidence
10. Add POST methods to kalshi_client.py
11. Make PaperTrader import or call any broker module
12. Delete, truncate, or rewrite runtime logs (paper_trades.jsonl, execution_funnel.jsonl, etc.)
13. Force trades open by bypassing risk, council, or edge checks
14. Create misleading dashboards (greening up metrics that aren't actually positive)
15. Rewrite trading strategy, council logic, or risk thresholds
16. Build Phase 6J (skills), 6K (hooks), 6L (agents) unless Samuel explicitly asks

---

## Proof Gate Rules

### What counts as proof
Only `normal_council_approved_modern` rows count:

```
normal_modern = modern_full_metadata - dc_override - bootstrap_provisional
```

- `bootstrap_era_allow` rows: **count as normal** (not excluded, count toward proof)
- `data_collection_override` rows: **excluded** — learning data, not proof
- `bootstrap_provisional` rows: **excluded** — learning data, not proof
- `legacy_edge_only` rows: **quarantined** — excluded from all proof, trust, and scale gates

### Proof thresholds
| Gate | Threshold | What it unlocks |
|------|-----------|-----------------|
| proof base | 30 clean_settled | Proof evaluation can begin |
| trust gate | 10 normal_modern | Edge profile trusted |
| scale gate | 30 normal_modern + ROI > 0 + CLV > 0 + PF > 1.10 | Scale readiness (still hardcoded locked) |
| proof complete | 30+ normal_modern with all gates passing | PROVEN_PROFITABLE verdict |
| scale final | 100 modern_full + human sign-off | Would unlock scale (hardcoded blocked) |

### What is MODERN_FULL_METADATA?
A row qualifies as `MODERN_FULL_METADATA` if it has all required tracking fields:
`council_decision`, `bootstrap_provisional`, `data_collection_override`, `risk_edge`,
`bootstrap_era_council_allow`, and complete metadata written by Phase 6B-2+ code.

### What is LEGACY_EDGE_ONLY?
Rows written before Phase 6B+, missing modern metadata fields. These rows have a
formula artifact: `edge = confidence - entry_price - 0.01` which produces spuriously
high edges for cheap entries. They are permanently quarantined.

---

## Real Money Lock

`evaluate_proof_gates()` in `tools/clean_truth_report.py` always returns:
```python
"real_money_allowed": False  # hardcoded — cannot be True regardless of config
```

The broker client has no POST capability. PaperTrader never calls any broker.
There is no live-trading code path.

---

## Scale Lock

`evaluate_proof_gates()` always returns:
```python
"scale_allowed": False  # hardcoded — cannot be True regardless of evidence state
```

Scale would require: human code change, removing the hardcoded False, passing all
performance gates, AND Samuel's explicit sign-off.

---

## Kelly Disabled

`GLOBAL_FORCED_LEARNING_MODE = True` forces all bets to `MIN_LEARNING_BET = $5.00`
before the risk manager check. The Kelly formula runs in audit mode only (`kelly_sizing_used = False`
in the trade record). Do not set this False until at least 30 normal_modern rows
with positive ROI and positive CLV.

---

## Data Collection / Bootstrap Rules

### Trust deadlock history
With no clean proof, the Decision Council BLOCKS signals. DATA_COLLECTION_OVERRIDE_ENABLED
was the original bypass (allowed any signal at $5). Disabled in Phase 6B-2 because
those trades were not auditable proof.

### Current bootstrap path (Phase 6B-2)
`BOOTSTRAP_ALLOW_ENABLED = True` allows signals that meet bootstrap quality thresholds
(`edge >= 0.05`, `confidence >= 0.65`) to be flagged as `bootstrap_era_council_allow = True`
and counted as `normal_modern` in the proof pool. This is the correct path to accumulate
clean proof without the DC override contamination.

### Dashboard restart required
The bootstrap_era_allow path requires a Dashboard restart to load Phase 6B-2 module
changes. After restart, new signals should show `council_decision=ALLOW, bootstrap_era_council_allow=True`.

---

## Current Evidence State

**Run reports to get current numbers — these become stale:**
```
python3 tools/report_health.py
python3 tools/report_scale_readiness.py
python3 tools/report_real_money_lockdown.py
```

Snapshot as of Phase 6H (2026-05-02):

| Metric | Value | Target |
|--------|-------|--------|
| all_records | 101 | — |
| clean_settled | 26 | 30 (proof base) |
| modern_full_metadata | 18 | 100 (scale gate) |
| dc_override (excluded) | 10 | — |
| bootstrap_provisional (excluded) | 8 | — |
| bootstrap_era_allow (counts as normal) | 0 | — |
| normal_modern (proof base) | 0 | 30 (trust gate) |
| legacy_edge_only (quarantined) | 8 | 0 ideal |
| win_rate | 0.500 | > breakeven |
| breakeven_WR | 0.810 | — |
| payoff_ratio | 0.234 | > 1.0 |
| total_PnL | -$68.90 | > 0 |
| avg_CLV | -0.0915 | > 0 |
| proof_verdict | NOT_PROVEN | PROVEN_PROFITABLE |
| scale_allowed | False | stays False |
| real_money_allowed | False | stays False |

---

## Phase History Summary

| Phase | Commit | Summary |
|-------|--------|---------|
| 1–5 | early | Infrastructure: scanner, risk manager, Kalshi API, paper trader, data pipeline |
| 6A | — | Data collection baseline, initial paper trades |
| 6B-1 | `ea28ba6` | Added `DATA_COLLECTION_OVERRIDE_ENABLED` config flag; exposed trust-deadlock warnings |
| 6B-2 | `392ecb9` | **Disabled DC override**; added `BOOTSTRAP_ALLOW_ENABLED` to break deadlock cleanly |
| 6B-3 | `e9d1373` | Unified threshold contract across scanner and council |
| 6C | `ed1cfc2` | Added proof/profitability/scale/real-money safety report suite |
| 6D | `1334b0f` | Asymmetry and edge inversion audit (`test_asymmetry_edge_inversion.py`) |
| 6E | `1c1dcc8` | Legacy edge quarantine; modern-only proof report (`test_modern_only_proof.py`) |
| 6F | `8685c0f` | Dashboard → Trading Research Control Room (10 sections, dark control room UI) |
| 6G | `2706706` | Fixed ghost OPEN / max-open-trades blockage. Added `_sync_open_trades_from_log()` to PaperTrader; added `tools/audit_open_trade_state.py` |
| 6H | `2706706` | Fixed null blocker reasons in execution funnel. Added trace parsers for risk/council reasons; added `tools/audit_execution_blockers.py` |

### Key architectural insight from Phase 6G
`auto_settle_trades.py` runs as a **separate process** and writes SETTLED rows directly
to `logs/paper_trades.jsonl` without touching the Dashboard's in-memory `PaperTrader`
instance. Before Phase 6G, this caused ghost OPEN records to accumulate in
`self.open_trades`, blocking new trades via the cap check (BLOCKED_MAX_OPEN_TRADES).

Fix: `_sync_open_trades_from_log()` reads the JSONL and prunes any `self.open_trades`
entry whose `(ticker, timestamp)` key now has a non-OPEN status on disk. It also calls
`risk_manager.rebuild_from_trade_log()` to keep `open_positions` / `total_exposure` in sync.
Deduplication key: `(ticker, timestamp)` — last line wins (SETTLED overwrites OPEN).

### Key insight from Phase 6H
BLOCKED_RISK with `council_decision=ALLOW` is **correct behavior**, not a bug.
The Decision Council adjusts confidence downward; if the adjusted confidence reduces
the post-council edge below `MIN_EDGE (0.03)`, the risk manager's edge check (CHECK 9)
fires. The reported reason is `"Edge X.XXXX below minimum 0.0300"`. Previously this
reason was null because the trace was truncated at 500 chars. The fix extracts the
reason from the full trace text before storage.

---

## Current Blocker Interpretation

| Blocker | Typical cause | Is it a bug? |
|---------|---------------|--------------|
| BLOCKED_MARKET_QUALITY | spread > MAX_SPREAD or volume < MIN_VOLUME | No — correct filter |
| BLOCKED_MIN_EDGE | pre-council edge < 0.03 | No — correct filter |
| BLOCKED_EDGE_DANGER_GUARD | edge >= 0.08 (M-46: historically inverted) | No — safety guard active |
| BLOCKED_COUNCIL | council BLOCK (sample too small, CLV negative, etc.) | No — correct gate |
| BLOCKED_RISK (post-council) | council adjusted confidence → post-council edge < 0.03 | No — correct behavior |
| BLOCKED_RISK (exposure/cooldown) | daily loss limit, cooldown, kill switch | No — risk protection |
| BLOCKED_MAX_OPEN_TRADES | open_count >= 3 AND all genuinely open | No — cap correct |
| BLOCKED_MAX_OPEN_TRADES (ghost) | stale OPEN rows in self.open_trades | Yes — Phase 6G fixed this |

Do not treat correct blockers as bugs. Investigate only when the reason is genuinely
unexpected (e.g., risk blocks with daily_pnl=0 and no exposure).

---

## Required Scan-Before-Patch Workflow

**EVERY session that involves code changes must follow this:**

1. Read the relevant file(s) before editing — never patch blind
2. Run `python3 tools/report_health.py` to understand current system state
3. If investigating signals: `python3 tools/audit_execution_blockers.py`
4. If investigating open trades: `python3 tools/audit_open_trade_state.py`
5. State what you found and what you intend to change before writing code
6. Write the minimal change that fixes the confirmed problem
7. Do not refactor, add features, or clean up unrelated code in the same patch
8. After changes: `python3 -m py_compile <changed files>`
9. After changes: run the three safety reports (see Verification Commands)
10. After changes: `git diff` to confirm no unintended changes

---

## Verification Commands

Run these after every code change:

```bash
# Syntax check (mandatory — every changed file)
python3 -m py_compile brain/paper_trader.py brain/risk_manager.py brain/market_scanner.py Dashboard.py

# Open trade state
python3 tools/audit_open_trade_state.py

# Health
python3 tools/report_health.py

# Scale (must remain locked)
python3 tools/report_scale_readiness.py

# Real money (must remain locked)
python3 tools/report_real_money_lockdown.py

# Execution funnel (after Dashboard restart with new session data)
python3 tools/audit_execution_blockers.py
```

For proof correctness:
```bash
python3 tools/test_modern_only_proof.py
python3 tools/test_asymmetry_edge_inversion.py
```

---

## Required Final Report Format

Every phase completion report must include:

1. Files changed and the specific reason for each change
2. What was found vs what was expected (be honest about surprises)
3. Whether safety locks remain intact (real money / scale / Kelly)
4. Whether proof gates are unchanged or explicitly why they changed
5. Whether any data was excluded or quarantined correctly
6. Verification results (show actual output, not "tests passed")
7. Exact next command Samuel should run

---

## Samuel's Prompt Preferences

- Every prompt begins with a `( / ) or </> = MASTER THINKING MODE` header
- Phase naming: 6G, 6H, 6I, etc. (or 6B-1, 6B-2 for sub-phases)
- Each phase is committed separately with a clear label
- Ultrathink = take extra reasoning time; inspect more deeply before patching
- Scan files FIRST — always — before writing a single line of code
- Do not hype progress
- Do not fake green states
- Do not manufacture PROVEN metrics
- Fake progress is worse than slow progress (see Warning section)
- Short, direct final reports preferred — not bloated summaries
- Truth state of the system matters more than the appearance of progress

---

## MASTER THINKING MODE Rule Block

These are **mindset labels**, NOT slash commands. Do NOT attempt to run them.

| Label | What it means |
|-------|---------------|
| `XRAY MODE` | Deep system inspection before any action |
| `LYRA MODE` | Structured optimization: Deconstruct → Diagnose → Develop → Deliver |
| `GOD MODE` | Highest scrutiny; challenge weak logic; protect system from bad decisions |
| `/ghost` | Quiet forensic mode; inspect carefully before acting |
| `/uda` | Ultra-deep audit; find hidden assumptions, false proof, stale data |
| `/buddha` | Calm, patient, non-reactive reasoning; no panic fixes |
| `/L99` | Level-99 expert standard: quant + risk engineer + architect + forensic debugger |
| `ultrathink` | Take extra reasoning time; inspect deeply; compare alternatives |
| `# memory` | Preserve project context, decisions, warnings, proof rules |
| `/compact` | Keep output structured and clear; do not omit safety details |

**Do NOT run:** `/ghost`, `/uda`, `/buddha`, `/L99`, `/GOD MODE`, `ultrathink`,
`# memory`, `/compact`, `/init`, `/clear`, `XRAY`, `LYRA`, `GOD MODE`, `ELI10`,
`TL;DR`, `JARGONIZE`, `LISTIFY`, `CRITIQUE`, `SIMPLIFY`, `PASTICHE`, `ANALOGIZE`,
`Ultimately`, `WIIFY?`, `MUDA`, `QOE`, `VISUALIZE#`, `INTERROGATE`, `FUTURIZE`
— or any other slash command — unless Samuel **explicitly** asks you to run that
exact command.

---

## Next Planned Phases

These phases exist in Samuel's roadmap. **Do not start them unless Samuel explicitly
requests the phase by name:**

| Phase | Description |
|-------|-------------|
| 6I | ✅ Project memory / CLAUDE.md foundation (this phase) |
| 6J | Reusable audit protocol docs (`docs/protocols/`) |
| 6K | Validation hooks / pre-commit safety checks |
| 6L | 3-agent orchestration blueprint (Signal / Risk-Proof / Auditor) |
| 7 | Scale readiness — only after proof gates pass |

---

## Warning: Fake Progress Is Worse Than Slow Progress

Do not:
- Show green dashboards when the evidence is red
- Count excluded rows toward proof to hit a threshold
- Lower a gate threshold to make a metric pass
- Report "PROVEN_PROFITABLE" before the proof gates genuinely pass
- Fix blockers by bypassing safety checks instead of understanding the root cause
- Make the system look ready to scale before it is

The entire point of this project is to have a trustworthy evidence record. One fake
proof row contaminates the entire history. One loosened gate destroys the meaning of
the gates. The system exists to protect Samuel from bad trading decisions — including
the decision to trade before the edge is proven.

If the metrics look bad, that is information. Record it honestly and wait for the
sample to grow.

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `config/trading_config.py` | All config flags and safety constants |
| `brain/paper_trader.py` | Paper trade execution, cap check, sync logic |
| `brain/risk_manager.py` | Risk checks (kill switch, loss limits, positions) |
| `brain/decision_council.py` | Council gate (edge profile, confidence, bootstrap) |
| `brain/edge_profile.py` | Edge profile trust calculation |
| `brokers/kalshi_client.py` | Kalshi API — GET only, no order placement |
| `auto_settle_trades.py` | External settlement process (separate PaperTrader instance) |
| `Dashboard.py` | Web UI, scan loop, execution funnel, truth state |
| `tools/clean_truth_report.py` | Evidence classification, proof gates, asymmetry |
| `tools/report_health.py` | System health snapshot |
| `tools/report_scale_readiness.py` | Scale gate evaluation |
| `tools/report_real_money_lockdown.py` | Real money lock confirmation |
| `tools/audit_open_trade_state.py` | Ghost OPEN detection (Phase 6G) |
| `tools/audit_execution_blockers.py` | Blocker reason breakdown (Phase 6H) |
| `logs/paper_trades.jsonl` | All trade records (OPEN/SETTLED/FORCED_CLOSE) |
| `logs/execution_funnel.jsonl` | Per-signal blocker attribution |
| `logs/risk_events.jsonl` | Risk manager event log |
| `data/risk_state.json` | Persisted risk manager state |
| `data/auto_settle_last_run.json` | Auto-settle heartbeat |
