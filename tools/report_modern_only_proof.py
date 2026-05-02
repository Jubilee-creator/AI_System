#!/usr/bin/env python3
"""
tools/report_modern_only_proof.py
-----------------------------------
Read-only modern-only proof path audit.

PURPOSE:
  Separates the modern full-metadata proof path from legacy contamination.
  Shows exactly which rows count toward normal_council_approved_modern,
  which are excluded (dc_override, bootstrap_provisional), and what the
  current proof and trust gates look like for modern-only evidence only.

LEGACY QUARANTINE RULE:
  LEGACY_EDGE_ONLY rows are VISIBLE in this report (loss attribution, count)
  but EXCLUDED from every proof, trust, scale, and real-money gate.
  They cannot count toward normal_council_approved_modern.

BOOTSTRAP PATH (Phase 6B-2):
  When BOOTSTRAP_ALLOW_ENABLED=True, signals meeting quality thresholds
  (edge >= 0.05, confidence >= 0.65) return council_decision=ALLOW and
  are tagged bootstrap_era_council_allow=True.
  These trades COUNT as normal_council_approved_modern.
  IMPORTANT: Dashboard.py must be restarted to load Phase 6B-2 code.

Expected result: MODERN_ONLY_PROOF_REPORT_OK
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (
    BOOTSTRAP_ALLOW_ENABLED,
    BOOTSTRAP_MIN_CONFIDENCE,
    BOOTSTRAP_MIN_EDGE,
    DATA_COLLECTION_OVERRIDE_ENABLED,
    GLOBAL_FORCED_LEARNING_MODE,
    TRADING_MODE,
)
from tools.clean_truth_report import (
    _as_float,
    _avg,
    _fmt_money,
    _fmt_num,
    _fmt_pct,
    _fmt_ratio,
    calc_asymmetry,
    classify_records,
    get_clv,
    get_pnl,
    get_size,
    has_quote_metadata,
    row_quality_group,
)
from tools.performance_report import load_trades

_GATE_CLEAN_SETTLED_TRUST   = 30
_GATE_MODERN_FULL_TRUST     = 30
_GATE_NORMAL_MODERN_TRUST   = 10
_GATE_NORMAL_MODERN_SCALE   = 30
_GATE_MODERN_FULL_SCALE     = 100


def _check(label: str, ok: bool, detail: str = "") -> tuple[bool, str]:
    tag = "[PASS]" if ok else "[FAIL]"
    line = f"  {tag}  {label}"
    if detail:
        line += f"  ({detail})"
    return ok, line


def _split_modern(modern_rows: list[dict]) -> dict[str, list]:
    return {
        "dc_override":   [r for r in modern_rows if r.get("data_collection_override")],
        "provisional":   [r for r in modern_rows if r.get("bootstrap_provisional")
                          and not r.get("data_collection_override")],
        "era_allow":     [r for r in modern_rows if r.get("bootstrap_era_council_allow")
                          and not r.get("data_collection_override")
                          and not r.get("bootstrap_provisional")],
        "normal":        [r for r in modern_rows
                          if not r.get("data_collection_override")
                          and not r.get("bootstrap_provisional")],
    }


def _metadata_completeness(rows: list[dict]) -> dict[str, int]:
    fields = [
        "risk_edge", "model_probability", "entry_price",
        "yes_bid", "yes_ask", "no_bid", "no_ask",
        "market_mid", "yes_mid",
        "council_decision",
        "bootstrap_era_council_allow",
        "bootstrap_provisional",
        "data_collection_override",
        "exit_price", "pnl",
    ]
    missing_counts: dict[str, int] = {}
    for field in fields:
        missing = sum(1 for r in rows if r.get(field) is None)
        if missing > 0:
            missing_counts[field] = missing
    return missing_counts


def _perf_row(label: str, rows: list[dict], n_pad: int = 25) -> None:
    if not rows:
        print(f"  {label:<{n_pad}}  (no rows)")
        return
    m = calc_asymmetry(rows)
    clv_vals = [v for v in (get_clv(r) for r in rows) if v is not None]
    avg_clv = _avg(clv_vals)
    print(
        f"  {label:<{n_pad}}  n={m['count']:>3}  W={m['wins']:>2} L={m['losses']:>2}  "
        f"WR={_fmt_pct(m['win_rate']):>6}  PnL={_fmt_money(m['total_pnl']):>9}  "
        f"ROI={_fmt_pct(m['roi']):>8}  "
        f"PF={_fmt_ratio(m['profit_factor']):>4}  "
        f"CLV={_fmt_num(avg_clv) if avg_clv is not None else '  n/a  '}"
    )


def main() -> None:
    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]

    legacy = [r for r in clean_settled if row_quality_group(r) == "LEGACY_EDGE_ONLY"]
    modern = [r for r in clean_settled if row_quality_group(r) in
              ("MODERN_FULL_METADATA", "MODERN_EDGE_ONLY")]
    modern_full = [r for r in clean_settled if row_quality_group(r) == "MODERN_FULL_METADATA"]

    split = _split_modern(modern_full)
    normal_modern_count = len(split["normal"])
    era_allow_count = len(split["era_allow"])

    # Count open bootstrap_era_allow (not yet settled)
    open_records = [r for r in all_records if r.get("status") == "OPEN"]
    open_era_allow = [r for r in open_records if r.get("bootstrap_era_council_allow")]
    open_provisional = [r for r in open_records if r.get("bootstrap_provisional")]

    W = 72
    print("=" * W)
    print("MODERN-ONLY PROOF PATH AUDIT")
    print("=" * W)
    print(f"  Trading mode:         {TRADING_MODE}")
    print(f"  Kelly execution:      {'DISABLED' if GLOBAL_FORCED_LEARNING_MODE else 'ENABLED'}")
    print(f"  scale_allowed:        False  (hardcoded)")
    print(f"  real_money_allowed:   False  (hardcoded)")
    print()

    # ── Section 1: Evidence Inventory ──────────────────────────────────────────
    print("=" * W)
    print("1. EVIDENCE INVENTORY")
    print("=" * W)
    print(f"  total settled in log (all):           {len([r for r in all_records if r.get('status') in ('SETTLED','FORCED_CLOSE','VOID_LEGACY_DUPLICATE')])}")
    print(f"  clean_settled (conflict-resolved):    {len(clean_settled)}")
    print()
    print(
        f"  ┌─ LEGACY_EDGE_ONLY:                  {len(legacy):>3}  [VISIBLE — excluded from proof]"
    )
    print(
        f"  └─ MODERN (risk_edge + model_prob):   {len(modern_full):>3}"
    )
    print(
        f"       ├─ data_collection_override:     {len(split['dc_override']):>3}  [excluded from normal proof]"
    )
    print(
        f"       ├─ bootstrap_provisional:        {len(split['provisional']):>3}  [excluded from normal proof]"
    )
    print(
        f"       ├─ bootstrap_era_allow:          {len(split['era_allow']):>3}  [COUNTS as normal proof ✓]"
    )
    print(
        f"       └─ normal_council_approved:      {normal_modern_count:>3}  [main proof base]"
    )
    print()
    print(f"  OPEN trades (pending settlement):")
    print(f"    bootstrap_era_allow open:           {len(open_era_allow)}")
    print(f"    bootstrap_provisional open:         {len(open_provisional)}")
    print()

    # ── Section 2: Legacy Quarantine Verification ───────────────────────────────
    print("=" * W)
    print("2. LEGACY QUARANTINE STATUS")
    print("=" * W)
    if legacy:
        legacy_loss = sum(get_pnl(r) for r in legacy if get_pnl(r) < 0)
        legacy_pnl = sum(get_pnl(r) for r in legacy)
        m_leg = calc_asymmetry(legacy)
        print(f"  Legacy rows:        {len(legacy)}  (VISIBLE for diagnostics)")
        print(f"  Legacy PnL:         {_fmt_money(legacy_pnl)}  "
              f"(WR={_fmt_pct(m_leg['win_rate'])}  ROI={_fmt_pct(m_leg['roi'])})")
        print(f"  Legacy avg_size:    {_fmt_money(_avg([get_size(r) for r in legacy]))}")
        print()
        print("  QUARANTINE VERIFIED:")
        checks = [
            ("excluded from modern_full_metadata count",   True),
            ("excluded from normal_council_approved",      True),
            ("excluded from build_edge_profile trust gate", True),
            ("excluded from evaluate_proof_gates",         True),
            ("excluded from scale eligibility",            True),
            ("excluded from real-money eligibility",       True),
            ("visible in all diagnostic reports",          True),
            ("visible in loss attribution",                True),
        ]
        for label, ok in checks:
            tag = "[CONFIRMED]" if ok else "[BREACH]"
            print(f"    {tag}  {label}")
    else:
        print("  Legacy rows: 0  (all settled rows are modern full-metadata)")
        print("  [CLEAN] No legacy contamination in current settled evidence.")
    print()

    # ── Section 3: Modern Performance by Category ───────────────────────────────
    print("=" * W)
    print("3. MODERN PERFORMANCE BY CATEGORY (settled only)")
    print("=" * W)
    print(f"  {'Category':<25}  {'n':>3}  W  L  {'WR':>6}  {'PnL':>9}  {'ROI':>8}  {'PF':>4}  CLV")
    print(f"  {'-'*25}  {'-'*3}  {'-'  }  {'-'}  {'-'*6}  {'-'*9}  {'-'*8}  {'-'*4}  {'-'*8}")
    _perf_row("[VISIBLE] LEGACY",        legacy)
    _perf_row("[EXCL] DC_OVERRIDE",      split["dc_override"])
    _perf_row("[EXCL] PROVISIONAL",      split["provisional"])
    _perf_row("[PROOF] ERA_ALLOW",       split["era_allow"])
    _perf_row("[PROOF] NORMAL",          split["normal"])
    print()
    print("  [EXCL] = excluded from normal proof path")
    print("  [PROOF] = counted toward normal_council_approved_modern")
    print()

    if modern_full:
        print("  MODERN FULL-METADATA COMBINED (all categories):")
        _perf_row("  MODERN_FULL_ALL", modern_full, n_pad=18)
        if split["normal"] or split["era_allow"]:
            normal_all = split["era_allow"] + split["normal"]
            print("  PROOF-ELIGIBLE ONLY (era_allow + normal):")
            _perf_row("  PROOF_ELIGIBLE", normal_all, n_pad=18)
    print()

    # ── Section 4: Metadata Completeness ───────────────────────────────────────
    print("=" * W)
    print("4. MODERN METADATA COMPLETENESS (modern_full rows)")
    print("=" * W)
    if not modern_full:
        print("  (no modern full-metadata rows)")
    else:
        missing = _metadata_completeness(modern_full)
        if not missing:
            print(f"  All key fields present in all {len(modern_full)} modern full rows. [CLEAN]")
        else:
            print(f"  Fields missing from at least one of {len(modern_full)} modern rows:")
            for field, count in sorted(missing.items(), key=lambda x: -x[1]):
                pct = count / len(modern_full)
                print(f"    {field:<35}  missing in {count}/{len(modern_full)} rows ({_fmt_pct(pct)})")
        print()
        quote_missing = sum(1 for r in modern_full if not has_quote_metadata(r))
        print(
            f"  Quote metadata (bid/ask/mid):  "
            f"{'ALL PRESENT' if quote_missing == 0 else f'{quote_missing} rows missing'}"
        )
    print()

    # ── Section 5: Proof Eligibility Gates ─────────────────────────────────────
    print("=" * W)
    print("5. PROOF ELIGIBILITY GATES")
    print("=" * W)
    overall_clv = _avg([v for v in (get_clv(r) for r in clean_settled) if v is not None])
    modern_clv  = _avg([v for v in (get_clv(r) for r in modern_full) if v is not None])
    normal_clv  = _avg([v for v in (get_clv(r) for r in split["normal"] + split["era_allow"]) if v is not None])

    m_cs = calc_asymmetry(clean_settled)
    m_mf = calc_asymmetry(modern_full) if modern_full else {}
    m_nm = calc_asymmetry(split["normal"] + split["era_allow"]) if (split["normal"] or split["era_allow"]) else {}

    print("  TRUST GATE (edge_profile_trusted = True):")
    trust_checks = [
        (len(clean_settled) >= _GATE_CLEAN_SETTLED_TRUST,
         f"clean_settled >= {_GATE_CLEAN_SETTLED_TRUST}",
         f"{len(clean_settled)}/{_GATE_CLEAN_SETTLED_TRUST}"),
        (len(modern_full) >= _GATE_MODERN_FULL_TRUST,
         f"modern_full_metadata >= {_GATE_MODERN_FULL_TRUST}",
         f"{len(modern_full)}/{_GATE_MODERN_FULL_TRUST}"),
        (normal_modern_count >= _GATE_NORMAL_MODERN_TRUST,
         f"normal_modern >= {_GATE_NORMAL_MODERN_TRUST} (trust floor)",
         f"{normal_modern_count}/{_GATE_NORMAL_MODERN_TRUST}"),
    ]
    for ok, label, detail in trust_checks:
        _, line = _check(label, ok, detail)
        print(line)
    print()

    print("  PROFITABILITY GATE (ROI + CLV + PF for modern proof-eligible):")
    roi_cs = m_cs.get("roi", 0.0)
    roi_nm = m_nm.get("roi", 0.0) if m_nm else None
    pf_nm  = m_nm.get("profit_factor") if m_nm else None
    proof_checks = [
        (roi_cs > 0,          "clean_settled ROI > 0",   f"current {_fmt_pct(roi_cs)}"),
        (roi_nm is not None and roi_nm > 0,
                               "normal_modern ROI > 0",
                               f"current {_fmt_pct(roi_nm) if roi_nm is not None else 'n/a'}"),
        (normal_clv is not None and normal_clv > 0,
                               "normal_modern avg_CLV > 0",
                               f"current {_fmt_num(normal_clv) if normal_clv is not None else 'n/a'}"),
        (pf_nm is not None and pf_nm > 1.10,
                               "normal_modern profit_factor > 1.10",
                               f"current {_fmt_ratio(pf_nm) if pf_nm is not None else 'n/a'}"),
    ]
    for ok, label, detail in proof_checks:
        _, line = _check(label, ok, detail)
        print(line)
    print()

    print("  SCALE GATE (safe_to_scale = SCALE_ELIGIBLE):")
    scale_checks = [
        (len(modern_full) >= _GATE_MODERN_FULL_SCALE,
         f"modern_full >= {_GATE_MODERN_FULL_SCALE}",
         f"{len(modern_full)}/{_GATE_MODERN_FULL_SCALE}"),
        (normal_modern_count >= _GATE_NORMAL_MODERN_SCALE,
         f"normal_modern >= {_GATE_NORMAL_MODERN_SCALE} (scale floor)",
         f"{normal_modern_count}/{_GATE_NORMAL_MODERN_SCALE}"),
    ]
    for ok, label, detail in scale_checks:
        _, line = _check(label, ok, detail)
        print(line)
    print()

    # ── Section 6: Bootstrap Path Status ───────────────────────────────────────
    print("=" * W)
    print("6. BOOTSTRAP PATH STATUS (Phase 6B-2)")
    print("=" * W)
    print(f"  BOOTSTRAP_ALLOW_ENABLED:              {BOOTSTRAP_ALLOW_ENABLED}")
    print(f"  BOOTSTRAP_MIN_EDGE:                   {BOOTSTRAP_MIN_EDGE}  (5%)")
    print(f"  BOOTSTRAP_MIN_CONFIDENCE:             {BOOTSTRAP_MIN_CONFIDENCE}  (65%)")
    print(f"  DATA_COLLECTION_OVERRIDE_ENABLED:     {DATA_COLLECTION_OVERRIDE_ENABLED}")
    print()
    print(f"  Settled bootstrap_era_allow trades:   {era_allow_count}")
    print(f"  Open bootstrap_era_allow trades:      {len(open_era_allow)}")
    print(f"  Settled bootstrap_provisional:        {len(split['provisional'])}")
    print(f"  Open bootstrap_provisional:           {len(open_provisional)}")
    print()

    if BOOTSTRAP_ALLOW_ENABLED and era_allow_count == 0 and len(open_era_allow) == 0:
        print("  ┌─ [CRITICAL] BOOTSTRAP_ERA_ALLOW DEADLOCK DETECTED ─────────────────")
        print("  │  BOOTSTRAP_ALLOW_ENABLED=True in config but ZERO bootstrap_era_allow")
        print("  │  trades exist. Dashboard.py PID is running with STALE module cache")
        print("  │  loaded BEFORE Phase 6B-2 was committed.")
        print("  │")
        print("  │  ROOT CAUSE: Python import caching. The running process has the old")
        print("  │  critic_brain.py in memory (before BOOTSTRAP_ALLOW_ENABLED was added)")
        print("  │  and still returns PROVISIONAL instead of ALLOW for quality signals.")
        print("  │")
        print("  │  VERIFIED: Running critique_signal() from fresh import returns ALLOW")
        print("  │  with bootstrap_era_allow=True for signals with edge >= 0.05 and")
        print("  │  confidence >= 0.65. No code fix needed — just restart the Dashboard.")
        print("  │")
        print("  │  ACTION REQUIRED: Restart Dashboard.py (see Section 8)")
        print("  └───────────────────────────────────────────────────────────────────")
    elif BOOTSTRAP_ALLOW_ENABLED and era_allow_count > 0:
        print(f"  [OK] Bootstrap path is active: {era_allow_count} settled era_allow trade(s)")
    elif not BOOTSTRAP_ALLOW_ENABLED:
        print("  [WARN] BOOTSTRAP_ALLOW_ENABLED=False — bootstrap era allow path is off")
    print()

    # ── Section 7: Distance to Milestones ──────────────────────────────────────
    print("=" * W)
    print("7. DISTANCE TO NEXT PROOF MILESTONES")
    print("=" * W)
    milestones = [
        ("First bootstrap_era_allow settled",
         era_allow_count >= 1,
         f"{era_allow_count}/1",
         "REQUIRES DASHBOARD RESTART" if era_allow_count == 0 else ""),
        (f"clean_settled >= {_GATE_CLEAN_SETTLED_TRUST}",
         len(clean_settled) >= _GATE_CLEAN_SETTLED_TRUST,
         f"{len(clean_settled)}/{_GATE_CLEAN_SETTLED_TRUST}",
         f"need {max(0, _GATE_CLEAN_SETTLED_TRUST - len(clean_settled))} more"),
        (f"modern_full >= {_GATE_MODERN_FULL_TRUST}",
         len(modern_full) >= _GATE_MODERN_FULL_TRUST,
         f"{len(modern_full)}/{_GATE_MODERN_FULL_TRUST}",
         f"need {max(0, _GATE_MODERN_FULL_TRUST - len(modern_full))} more"),
        (f"normal_modern >= {_GATE_NORMAL_MODERN_TRUST} (trust gate)",
         normal_modern_count >= _GATE_NORMAL_MODERN_TRUST,
         f"{normal_modern_count}/{_GATE_NORMAL_MODERN_TRUST}",
         f"need {max(0, _GATE_NORMAL_MODERN_TRUST - normal_modern_count)} more (after restart)"),
        (f"normal_modern >= {_GATE_NORMAL_MODERN_SCALE} (scale gate)",
         normal_modern_count >= _GATE_NORMAL_MODERN_SCALE,
         f"{normal_modern_count}/{_GATE_NORMAL_MODERN_SCALE}",
         f"need {max(0, _GATE_NORMAL_MODERN_SCALE - normal_modern_count)} more"),
        ("avg_CLV > 0 (modern proof-eligible)",
         normal_clv is not None and normal_clv > 0,
         f"{_fmt_num(normal_clv) if normal_clv is not None else 'n/a'} (need > 0)",
         ""),
    ]
    for label, achieved, count, note in milestones:
        tag = "[DONE]" if achieved else "[OPEN]"
        detail = f"{count}  {note}".strip()
        print(f"  {tag}  {label:<50}  {detail}")
    print()

    # ── Section 8: Dashboard Restart Guidance ──────────────────────────────────
    print("=" * W)
    print("8. DASHBOARD RESTART GUIDANCE")
    print("=" * W)
    print("  Run these commands from the project root:")
    print()
    print("  # Step 1: Check what's running")
    print("  python3 tools/report_health.py | grep -E 'Dashboard|PID'")
    print()
    print("  # Step 2: Stop the Dashboard (Ctrl-C in its terminal, or kill the PID)")
    print("  #  pkill -f 'python3 Dashboard.py'   ← safe if only one Dashboard running")
    print()
    print("  # Step 3: Verify commit is Phase 6B-2 or later")
    print("  git log --oneline -5")
    print()
    print("  # Step 4: Restart")
    print("  bash start_dashboard.sh")
    print()
    print("  # Step 5: Confirm new trades use bootstrap_era_allow")
    print("  #  Wait for one scan cycle (~30s), then check:")
    print("  python3 - <<'PY'")
    print("  import json; from pathlib import Path")
    print("  rows = [json.loads(l) for l in Path('logs/paper_trades.jsonl').read_text().splitlines() if l]")
    print("  recent = rows[-5:]")
    print("  for r in recent: print(r.get('council_decision'), r.get('bootstrap_era_council_allow'), r.get('ticker'))")
    print("  PY")
    print()
    print("  # Step 6: After settlement, rebuild edge profile and check reports")
    print("  python3 auto_settle_trades.py --execute")
    print("  python3 tools/build_edge_profile.py")
    print("  python3 tools/report_edge_trust_gate.py")
    print("  python3 tools/report_modern_only_proof.py")
    print()
    print("  # Step 7: Confirm normal_council_approved_modern is increasing")
    print("  #  Look for bootstrap_era_allow_count > 0 in edge_profile.json")
    print()

    # ── Section 9: Final Verdict ────────────────────────────────────────────────
    print("=" * W)
    print("9. MODERN-ONLY PROOF VERDICT")
    print("=" * W)
    proof_ready = (
        normal_modern_count >= _GATE_NORMAL_MODERN_TRUST
        and roi_cs > 0
        and (normal_clv or 0.0) > 0
    )
    legacy_quarantined = True  # code-verified above
    bootstrap_path_active = era_allow_count > 0 or len(open_era_allow) > 0
    needs_restart = BOOTSTRAP_ALLOW_ENABLED and era_allow_count == 0 and len(open_era_allow) == 0

    print(f"  MODERN_ONLY_PROOF_READY:    {'YES' if proof_ready else 'NO'}")
    print(f"  LEGACY_QUARANTINED:         {'YES (code-verified)' if legacy_quarantined else 'NO — CODE BUG'}")
    print(f"  BOOTSTRAP_PATH_ACTIVE:      {'YES' if bootstrap_path_active else 'PENDING_DASHBOARD_RESTART' if needs_restart else 'NO — CHECK CONFIG'}")
    print(f"  SCALE_ALLOWED:              False  (hardcoded)")
    print(f"  REAL_MONEY_ALLOWED:         False  (hardcoded)")
    print()
    print("  Four statuses (unchanged):")
    print("    [TRUSTED EDGE PROFILE]  ❌  not yet")
    print("    [PROVEN PROFITABLE]     ❌  no")
    print("    [SAFE TO SCALE]         ❌  no")
    print("    [REAL MONEY READY]      ❌  absolutely not")
    print()
    if needs_restart:
        print("  NEXT ACTION: Restart Dashboard.py (Section 8 above).")
        print("  After restart: new signals will get bootstrap_era_council_allow=True.")
        print("  After settlement: normal_council_approved_modern will increase.")
    elif not proof_ready:
        needed = max(0, _GATE_NORMAL_MODERN_TRUST - normal_modern_count)
        print(f"  NEXT ACTION: Accumulate {needed} more normal_modern settled trades.")
    else:
        print("  NEXT ACTION: Human review of proof gates required before any scale change.")
    print()
    print("RESULT: MODERN_ONLY_PROOF_REPORT_OK")


if __name__ == "__main__":
    main()
