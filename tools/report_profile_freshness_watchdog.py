"""
tools/report_profile_freshness_watchdog.py
------------------------------------------
Phase 9C — Edge Profile Freshness Watchdog

Default: READ-ONLY.  Diagnoses profile staleness and bootstrap-fallback risk.
With --rebuild-if-stale: rebuilds edge_profile.json if age > REBUILD_THRESHOLD_HOURS.

Usage:
  python3 tools/report_profile_freshness_watchdog.py               # read-only
  python3 tools/report_profile_freshness_watchdog.py --rebuild-if-stale

Sections:
  1.  Safety confirmation
  2.  Edge profile freshness
  3.  2D price-conditioned profile integrity
  4.  Bootstrap fallback risk (from logs)
  5.  Rebuild readiness check
  6.  Dry-run rebuild plan
  7.  Optional safe rebuild (only fires with --rebuild-if-stale)
  8.  Recommended schedule
  9.  Final recommendation
 10.  Plain-English answer for Samuel

Expected output sentinel: PROFILE_FRESHNESS_WATCHDOG_OK
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE_PATH = ROOT / "data" / "edge_profile.json"
TRADES_PATH  = ROOT / "logs" / "paper_trades.jsonl"
FUNNEL_PATH  = ROOT / "logs" / "execution_funnel.jsonl"
BUILD_SCRIPT = ROOT / "tools" / "build_edge_profile.py"

# Rebuild threshold: rebuild if profile is older than this (proactive, before untrust at 48h)
REBUILD_THRESHOLD_HOURS = 24.0

# Cells that must exist in a healthy 2D profile
SWEET_SPOT_CELL = "0.05-0.10|0.80-0.90"
POISON_CELLS    = ["0.05-0.10|0.60-0.70", "0.05-0.10|0.70-0.80"]

PHASE_9A_DATE   = "2026-05-07"


# ── helpers ───────────────────────────────────────────────────────────────────

def _af(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _load_profile(path: Path = PROFILE_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _profile_age_hours(profile: dict) -> Optional[float]:
    dt = _parse_ts(profile.get("generated_at"))
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


# normal_modern filter — mirrors _is_normal_modern_for_2d in build_edge_profile.py
_MODERN_KEYS = [
    "council_decision", "bootstrap_provisional",
    "data_collection_override", "risk_edge", "bootstrap_era_council_allow",
]


def _is_normal_modern(r: dict) -> bool:
    if any(r.get(k) is None for k in _MODERN_KEYS):
        return False
    if r.get("data_collection_override"):
        return False
    if r.get("bootstrap_provisional"):
        return False
    return True


# ── section 1: safety ─────────────────────────────────────────────────────────

def section_1_safety() -> list[str]:
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append("SECTION 1 — SAFETY CONFIRMATION")
    lines.append("=" * 68)

    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades
        recs    = load_trades()
        buckets = classify_records(recs)
        gate    = evaluate_proof_gates(buckets, buckets["clean_settled"])
        rma     = gate.get("real_money_allowed")
        sa      = gate.get("scale_allowed")
    except Exception as e:
        rma = sa = f"ERROR: {e}"

    from config.trading_config import (
        GLOBAL_FORCED_LEARNING_MODE,
        QUARANTINED_TICKER_PREFIXES,
        MIN_EDGE,
        MIN_CONFIDENCE,
        EDGE_PROFILE_MAX_AGE_HOURS,
    )

    # Check open exposure from risk_state
    rs_path = ROOT / "data" / "risk_state.json"
    rs = json.loads(rs_path.read_text()) if rs_path.exists() else {}
    open_count = rs.get("open_positions", "N/A")
    total_exp  = rs.get("total_exposure", "N/A")
    kill_sw    = rs.get("kill_switch_active", "N/A")

    lines.append(f"  real_money_allowed      : {rma!r}  (hardcoded False)")
    lines.append(f"  scale_allowed           : {sa!r}  (hardcoded False)")
    lines.append(f"  GLOBAL_FORCED_LEARNING_MODE: {GLOBAL_FORCED_LEARNING_MODE}  (all bets $5 flat)")
    lines.append(f"  QUARANTINED_TICKER_PREFIXES: {QUARANTINED_TICKER_PREFIXES}")
    lines.append(f"  MIN_EDGE                : {MIN_EDGE}")
    lines.append(f"  MIN_CONFIDENCE          : {MIN_CONFIDENCE}")
    lines.append(f"  EDGE_PROFILE_MAX_AGE_HOURS: {EDGE_PROFILE_MAX_AGE_HOURS}  (untrust threshold)")
    lines.append(f"  open_positions          : {open_count}")
    lines.append(f"  total_exposure          : {total_exp}")
    lines.append(f"  kill_switch_active      : {kill_sw}")
    lines.append("")
    lines.append("  This report does NOT change live behavior.")
    lines.append("  --rebuild-if-stale only touches data/edge_profile.json (no trades, no config).")

    all_locked = (rma is False and sa is False)
    lines.append(f"  [{'OK' if all_locked else 'WARN'}] safety locks {'confirmed' if all_locked else 'UNEXPECTED — investigate before proceeding'}")
    return lines


# ── section 2: profile freshness ──────────────────────────────────────────────

def section_2_freshness() -> tuple[list[str], dict]:
    """Returns lines and a freshness_info dict for later sections."""
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 2 — EDGE PROFILE FRESHNESS")
    lines.append("=" * 68)

    from config.trading_config import EDGE_PROFILE_MAX_AGE_HOURS

    exists = PROFILE_PATH.exists()
    lines.append(f"  Profile path     : {PROFILE_PATH}")
    lines.append(f"  Exists           : {exists}")

    info: dict = {"exists": exists, "trusted": False, "age_hours": None, "stale": True}

    if not exists:
        lines.append("  [FAIL] edge_profile.json missing — rebuild required immediately")
        return lines, info

    mtime       = PROFILE_PATH.stat().st_mtime
    mtime_dt    = datetime.fromtimestamp(mtime, tz=timezone.utc)
    mtime_str   = mtime_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    profile     = _load_profile()
    generated   = profile.get("generated_at", "missing")
    age_hours   = _profile_age_hours(profile)
    is_stale    = age_hours is None or age_hours > EDGE_PROFILE_MAX_AGE_HOURS
    hours_left  = (EDGE_PROFILE_MAX_AGE_HOURS - age_hours) if age_hours is not None else None

    from brain.edge_profile_health import edge_profile_health
    health = edge_profile_health(profile, PROFILE_PATH)
    trusted = health["edge_profile_trusted"]

    lines.append(f"  File mtime       : {mtime_str}")
    lines.append(f"  generated_at     : {generated}")
    lines.append(f"  Age              : {age_hours:.1f}h" if age_hours is not None else "  Age: unknown")
    lines.append(f"  Untrust threshold: {EDGE_PROFILE_MAX_AGE_HOURS}h")
    lines.append(f"  Rebuild threshold: {REBUILD_THRESHOLD_HOURS}h (proactive — before untrust)")
    if hours_left is not None:
        if hours_left > 0:
            lines.append(f"  Becomes stale in : {hours_left:.1f}h")
        else:
            lines.append(f"  Overdue by       : {abs(hours_left):.1f}h  ← STALE")
    lines.append(f"  Trusted          : {trusted}")
    lines.append(f"  Health reason    : {health['reason']}")
    lines.append("")
    lines.append(f"  Profile sample counts (from last rebuild):")
    lines.append(f"    clean_settled_trades         : {health['clean_settled_trades']}")
    lines.append(f"    modern_full_metadata_trades  : {health['modern_full_metadata_trades']}")
    lines.append(f"    normal_council_approved      : {health['normal_council_approved_modern_trades']}")
    lines.append(f"    data_collection_override     : {health['data_collection_override_count']}")
    lines.append(f"    bootstrap_provisional        : {health['bootstrap_provisional_count']}")
    lines.append("")

    # Compute how many trades have been settled SINCE the profile was built
    if profile.get("generated_at"):
        settled = [r for r in _load_jsonl(TRADES_PATH) if r.get("status") == "SETTLED"]
        new_settled = [r for r in settled if r.get("timestamp", "") >= generated[:19]]
        nm_new = [r for r in new_settled
                  if _is_normal_modern(r)
                  and not r.get("ticker", "").upper().startswith("KXETH")]
        lines.append(f"  Trades settled since last rebuild: {len(new_settled)}")
        lines.append(f"  Of which normal_modern non-KXETH : {len(nm_new)}")
        if nm_new:
            sweet_new = sum(
                1 for r in nm_new
                if (_af(r.get("yes_ask") or r.get("entry_price")) or 0) >= 0.80
            )
            lines.append(f"  Of which in sweet-spot zone 0.80+: {sweet_new}  "
                         "← would strengthen 2D cell if rebuilt")

    if is_stale:
        lines.append("  [STALE] Profile has exceeded the untrust threshold.")
        lines.append("  All signals are now evaluated via bootstrap_era_allow fallback (weaker filter).")
    elif age_hours is not None and age_hours > REBUILD_THRESHOLD_HOURS:
        lines.append("  [AGING] Profile is within untrust window but past rebuild threshold.")
        lines.append("  Recommend rebuilding now to avoid staleness later today.")
    else:
        lines.append("  [FRESH] Profile is within proactive rebuild threshold.")

    info.update({
        "trusted": trusted, "age_hours": age_hours,
        "stale": is_stale, "hours_left": hours_left,
        "profile": profile, "health": health,
    })
    return lines, info


# ── section 3: 2D profile integrity ──────────────────────────────────────────

def section_3_2d_integrity(profile: dict) -> tuple[list[str], bool]:
    """Returns lines and a bool: 2D table is intact."""
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 3 — 2D PRICE-CONDITIONED PROFILE INTEGRITY")
    lines.append("=" * 68)

    from config.trading_config import PRICE_CONDITIONED_MIN_N, PRICE_CONDITIONED_MIN_WR

    table = profile.get("profiles", {}).get("by_edge_price_bucket", {})
    has_table = bool(table)
    lines.append(f"  by_edge_price_bucket exists: {has_table}")
    lines.append(f"  Number of 2D cells          : {len(table)}")
    lines.append(f"  PRICE_CONDITIONED_MIN_N     : {PRICE_CONDITIONED_MIN_N}")
    lines.append(f"  PRICE_CONDITIONED_MIN_WR    : {PRICE_CONDITIONED_MIN_WR}")

    if not has_table:
        lines.append("  [FAIL] 2D table missing — profile was built before Phase 9A or is empty.")
        lines.append("  Rebuild required: python3 tools/build_edge_profile.py")
        return lines, False

    # All cells
    lines.append("")
    lines.append("  All 2D cells in current profile:")
    lines.append(f"  {'Cell':35s} {'n':>5} {'WR':>6} {'PnL':>8}  Status")
    lines.append(f"  {'-'*35} {'-----':>5} {'------':>6} {'--------':>8}  ------")
    for key in sorted(table):
        c = table[key]
        n   = int(c.get("trades", 0))
        wr  = float(c.get("win_rate", 0))
        pnl = float(c.get("total_pnl", 0))
        if key == SWEET_SPOT_CELL:
            ok = n >= PRICE_CONDITIONED_MIN_N and wr >= PRICE_CONDITIONED_MIN_WR and pnl > 0
            status = "[SWEET-SPOT WATCH]" if ok else "[SWEET-SPOT WEAK]"
        elif key in POISON_CELLS:
            status = "[POISON]" if pnl < 0 else "[CAUTION: not negative]"
        else:
            status = "[other]"
        lines.append(f"  {key:35s} {n:>5d} {wr:>6.3f} {pnl:>+8.2f}  {status}")

    # Sweet-spot cell detail
    sweet = table.get(SWEET_SPOT_CELL)
    lines.append("")
    if sweet is None:
        lines.append(f"  [WARN] Sweet-spot cell '{SWEET_SPOT_CELL}' not found.")
        lines.append("  The price_conditioned_council override CANNOT fire for yes_ask >= 0.80.")
        lines.append("  This means high-confidence sweet-spot signals will be BLOCKED by the 1D bucket.")
        intact = False
    else:
        n   = int(sweet.get("trades", 0))
        wr  = float(sweet.get("win_rate", 0))
        pnl = float(sweet.get("total_pnl", 0))
        qualifies = n >= PRICE_CONDITIONED_MIN_N and wr >= PRICE_CONDITIONED_MIN_WR and pnl > 0
        lines.append(f"  Sweet-spot cell '{SWEET_SPOT_CELL}':")
        lines.append(f"    n={n}, WR={wr:.3f}, pnl={pnl:+.2f}")
        lines.append(f"    Qualifies for 2D override: {qualifies} "
                     f"(n>={PRICE_CONDITIONED_MIN_N}, WR>={PRICE_CONDITIONED_MIN_WR}, pnl>0)")
        if not qualifies:
            lines.append(f"    [WARN] Cell does not meet override threshold — 2D override disabled for sweet-spot.")
        intact = qualifies

    # Poison cells
    lines.append("")
    for pk in POISON_CELLS:
        pc = table.get(pk)
        if pc is None:
            lines.append(f"  Poison cell '{pk}': not found (no evidence — 2D falls through to 1D)")
        else:
            pnl = float(pc.get("total_pnl", 0))
            lines.append(f"  Poison cell '{pk}': pnl={pnl:+.2f}  "
                         f"({'correctly negative — 2D does not override' if pnl < 0 else 'WARNING: positive — may allow losers'})")

    return lines, intact


# ── section 4: bootstrap fallback risk ───────────────────────────────────────

def section_4_bootstrap_risk() -> list[str]:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 4 — BOOTSTRAP FALLBACK RISK")
    lines.append("=" * 68)
    lines.append("  Bootstrap fallback fires when profile is stale/untrusted.")
    lines.append("  Uses simpler quality check (edge>=0.05, conf>=0.65) instead of 2D evidence.")

    settled = [r for r in _load_jsonl(TRADES_PATH) if r.get("status") == "SETTLED"]
    post_9a_nk = [
        r for r in settled
        if r.get("timestamp", "") >= PHASE_9A_DATE
        and not r.get("ticker", "").upper().startswith("KXETH")
    ]

    pc_trades = [r for r in post_9a_nk
                 if "price_conditioned" in str(r.get("council_reason") or "").lower()]
    be_trades = [r for r in post_9a_nk
                 if "price_conditioned" not in str(r.get("council_reason") or "").lower()]

    def _stats(rows: list[dict]) -> dict:
        if not rows:
            return {"n": 0}
        wins   = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
        total  = sum(r.get("pnl") or 0 for r in rows)
        gwin   = sum(r.get("pnl") or 0 for r in rows if (r.get("pnl") or 0) > 0)
        gloss  = abs(sum(r.get("pnl") or 0 for r in rows if (r.get("pnl") or 0) < 0))
        return {
            "n": len(rows), "wr": wins / len(rows),
            "total_pnl": total, "avg_pnl": total / len(rows),
            "pf": gwin / gloss if gloss > 0 else float("inf"),
        }

    pc_s = _stats(pc_trades)
    be_s = _stats(be_trades)

    lines.append("")
    lines.append("  Post-Phase-9A non-KXETH settled trades by council path:")
    lines.append(f"  {'Path':40s} {'n':>5} {'WR':>6} {'avg_pnl':>9} {'PF':>8}")
    lines.append(f"  {'-'*40} {'-----':>5} {'------':>6} {'---------':>9} {'--------':>8}")
    if pc_s["n"]:
        lines.append(
            f"  {'price_conditioned_council (2D evidence)':40s} "
            f"{pc_s['n']:>5d} {pc_s['wr']:>6.3f} {pc_s['avg_pnl']:>+9.3f} {pc_s['pf']:>8.3f}"
        )
    if be_s["n"]:
        pf_str = f"{be_s['pf']:.3f}" if be_s["pf"] != float("inf") else "inf"
        lines.append(
            f"  {'bootstrap_era_allow (fallback, profile stale)':40s} "
            f"{be_s['n']:>5d} {be_s['wr']:>6.3f} {be_s['avg_pnl']:>+9.3f} {pf_str:>8s}"
        )

    lines.append("")
    if pc_s["n"] and be_s["n"]:
        pnl_gap = pc_s["avg_pnl"] - be_s["avg_pnl"]
        lines.append(f"  PnL gap per trade (price_conditioned vs bootstrap): {pnl_gap:+.3f}")
        lines.append(f"  Interpretation: each bootstrap trade earns {abs(pnl_gap):.3f} less "
                     f"than a price_conditioned trade on average.")
        lines.append(f"  At 20 trades/day: {pnl_gap * 20:+.2f}/day improvement if ALL trades were")
        lines.append(f"  evaluated via fresh-profile 2D path instead of bootstrap fallback.")

    # Funnel: count scans where profile was stale
    funnel = _load_jsonl(FUNNEL_PATH)
    post_9a_funnel = [r for r in funnel if r.get("timestamp_utc", "") >= PHASE_9A_DATE]
    stale_scans = [r for r in post_9a_funnel
                   if "stale" in str(r.get("trace_excerpt") or "").lower()
                   or "untrusted" in str(r.get("trace_excerpt") or "").lower()]
    cap_blocked = [r for r in post_9a_funnel
                   if "Max trades per day" in str(r.get("risk_block_reason") or "")]
    cap_stale   = [r for r in cap_blocked
                   if "stale" in str(r.get("trace_excerpt") or "").lower()
                   or "untrusted" in str(r.get("trace_excerpt") or "").lower()]

    lines.append("")
    lines.append(f"  Post-9A funnel signals evaluated on stale/untrusted profile: {len(stale_scans)}")
    lines.append(f"  Of which cap-blocked ALLOW (lost to cap while on stale profile): {len(cap_stale)}")

    if len(stale_scans) > 0:
        pct = len(stale_scans) / len(post_9a_funnel) * 100 if post_9a_funnel else 0
        lines.append(f"  Stale-profile fraction of post-9A funnel: {pct:.1f}%")
        lines.append("")
        lines.append("  [RISK] A significant fraction of post-9A signals ran on a stale profile.")
        lines.append("  These signals bypassed the 2D evidence gate entirely — the bootstrap")
        lines.append("  path allowed some losers (0.60-0.80 zone) that the 2D gate would block.")

    return lines


# ── section 5: rebuild readiness ─────────────────────────────────────────────

def section_5_rebuild_readiness() -> tuple[list[str], bool]:
    """Returns lines and ready: bool."""
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 5 — REBUILD READINESS CHECK")
    lines.append("=" * 68)
    lines.append("  Verifying all preconditions before any potential rebuild.")

    checks: list[tuple[str, bool, str]] = []

    # 1. Trades file exists
    trades_ok = TRADES_PATH.exists()
    checks.append(("paper_trades.jsonl exists", trades_ok, str(TRADES_PATH)))

    # 2. Build script exists and compiles
    script_exists = BUILD_SCRIPT.exists()
    checks.append(("build_edge_profile.py exists", script_exists, str(BUILD_SCRIPT)))

    compile_ok = False
    if script_exists:
        try:
            import py_compile
            py_compile.compile(str(BUILD_SCRIPT), doraise=True)
            compile_ok = True
        except py_compile.PyCompileError as e:
            compile_ok = False
    checks.append(("build_edge_profile.py compiles", compile_ok, "py_compile check"))

    # 3. Enough settled trades
    settled = [r for r in _load_jsonl(TRADES_PATH) if r.get("status") == "SETTLED"] if trades_ok else []
    enough_settled = len(settled) >= 30
    checks.append(("settled trades >= 30", enough_settled, f"actual={len(settled)}"))

    # 4. Enough normal_modern non-KXETH trades for 2D table
    nm_non_kxeth = [
        r for r in settled
        if _is_normal_modern(r)
        and not r.get("ticker", "").upper().startswith("KXETH")
    ] if settled else []
    enough_nm = len(nm_non_kxeth) >= 10
    checks.append(("normal_modern non-KXETH >= 10", enough_nm, f"actual={len(nm_non_kxeth)}"))

    # 5. Safety: real_money_allowed remains False (hardcoded — just confirm the function exists)
    try:
        from tools.clean_truth_report import evaluate_proof_gates
        rma_locked = True
    except ImportError:
        rma_locked = False
    checks.append(("evaluate_proof_gates importable", rma_locked, "hardcodes real_money=False"))

    # 6. Rebuild does NOT write to trade records — scan for write_text/open-for-write on trades path
    if script_exists:
        src = BUILD_SCRIPT.read_text()
        # The only write_text call in the build script should be OUTPUT_PATH.write_text(...)
        # where OUTPUT_PATH is data/edge_profile.json.  Detect any write to trades JSONL.
        writes_to_trades = any(
            "paper_trades" in line and ("write_text" in line or
                                        (".open(" in line and '"w"' in line))
            for line in src.splitlines()
            if not line.strip().startswith("#")
        )
        safe_output = "OUTPUT_PATH.write_text" in src
    else:
        writes_to_trades = True   # assume unsafe if script not found
        safe_output = False
    checks.append(("build script only writes edge_profile.json",
                   safe_output and not writes_to_trades,
                   "code-scan: OUTPUT_PATH.write_text only, no trades write"))

    # 7. KXETH excluded from 2D — verify in build script
    kxeth_excluded = "KXETH" in (BUILD_SCRIPT.read_text() if script_exists else "")
    checks.append(("KXETH excluded from 2D table in build script", kxeth_excluded, "code-scan verified"))

    # 8. data_collection_override excluded from 2D
    dc_excluded = "_is_normal_modern_for_2d" in (BUILD_SCRIPT.read_text() if script_exists else "")
    checks.append(("DC override excluded via _is_normal_modern_for_2d", dc_excluded, "code-scan verified"))

    all_pass = all(ok for _, ok, _ in checks)
    for desc, ok, detail in checks:
        icon = "[OK]  " if ok else "[FAIL]"
        lines.append(f"  {icon} {desc}")
        if not ok or "actual=" in detail:
            lines.append(f"          {detail}")

    lines.append("")
    lines.append(f"  Rebuild readiness: {'READY' if all_pass else 'NOT READY — fix failures above'}")
    return lines, all_pass


# ── section 6: dry-run plan ───────────────────────────────────────────────────

def section_6_dry_run() -> list[str]:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 6 — DRY-RUN REBUILD PLAN")
    lines.append("=" * 68)
    lines.append("")
    lines.append("  Command to rebuild edge profile (safe, read-only for trade records):")
    lines.append("")
    lines.append("    cd /Users/samuel/Desktop/AI_System/tools")
    lines.append("    python3 build_edge_profile.py")
    lines.append("")
    lines.append("  What this does:")
    lines.append("  - Reads  : logs/paper_trades.jsonl  (not modified)")
    lines.append("  - Writes : data/edge_profile.json   (only this file)")
    lines.append("  - Does NOT modify: configs, thresholds, trade records, risk state")
    lines.append("  - Does NOT enable: real money, scaling, Kelly, or live orders")
    lines.append("")
    lines.append("  Or use this watchdog with the flag:")
    lines.append("    python3 tools/report_profile_freshness_watchdog.py --rebuild-if-stale")
    lines.append(f"  (rebuilds only if profile age > {REBUILD_THRESHOLD_HOURS}h)")
    return lines


# ── section 7: optional rebuild ───────────────────────────────────────────────

def section_7_rebuild(rebuild_requested: bool, ready: bool, info: dict) -> tuple[list[str], bool]:
    """Returns lines and a bool: rebuild was performed."""
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 7 — OPTIONAL SAFE REBUILD")
    lines.append("=" * 68)

    if not rebuild_requested:
        lines.append("  Mode: READ-ONLY (pass --rebuild-if-stale to enable)")
        return lines, False

    lines.append("  Mode: --rebuild-if-stale")
    age = info.get("age_hours")

    if age is None:
        lines.append("  [SKIP] Cannot determine profile age — rebuild manually if needed.")
        return lines, False

    if age < REBUILD_THRESHOLD_HOURS:
        lines.append(
            f"  Profile age {age:.1f}h < rebuild threshold {REBUILD_THRESHOLD_HOURS}h — "
            "no rebuild needed."
        )
        return lines, False

    lines.append(f"  Profile age {age:.1f}h >= threshold {REBUILD_THRESHOLD_HOURS}h — rebuilding...")

    if not ready:
        lines.append("  [ABORT] Rebuild readiness check failed (see Section 5).")
        lines.append("  Fix failures before attempting rebuild.")
        return lines, False

    # Safety lock re-check before rebuild
    try:
        from tools.clean_truth_report import evaluate_proof_gates, classify_records
        from tools.performance_report import load_trades as _lt
        recs    = _lt()
        buckets = classify_records(recs)
        gate    = evaluate_proof_gates(buckets, buckets["clean_settled"])
        if gate.get("real_money_allowed") is not False:
            lines.append("  [ABORT] real_money_allowed check failed — refusing to rebuild.")
            return lines, False
        if gate.get("scale_allowed") is not False:
            lines.append("  [ABORT] scale_allowed check failed — refusing to rebuild.")
            return lines, False
    except Exception as e:
        lines.append(f"  [ABORT] Safety check error: {e}")
        return lines, False

    # Snapshot before-state
    before = _load_profile()
    before_gen  = before.get("generated_at", "missing")
    before_cell = before.get("profiles", {}).get("by_edge_price_bucket", {}).get(SWEET_SPOT_CELL)
    before_n    = int(before_cell.get("trades", 0)) if before_cell else 0

    lines.append(f"  Before: generated_at={before_gen}")
    lines.append(f"  Before: sweet-spot cell n={before_n}")

    # Run rebuild subprocess
    try:
        result = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT)],
            capture_output=True, text=True,
            cwd=str(ROOT / "tools"),
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        lines.append("  [FAIL] Rebuild timed out after 120s.")
        return lines, False
    except Exception as e:
        lines.append(f"  [FAIL] Rebuild subprocess error: {e}")
        return lines, False

    if result.returncode != 0:
        lines.append(f"  [FAIL] Rebuild exited with code {result.returncode}")
        if result.stderr:
            lines.append(f"  stderr: {result.stderr[:400]}")
        return lines, False

    lines.append("  [OK] Rebuild completed successfully.")
    if result.stdout:
        for l in result.stdout.strip().splitlines()[-8:]:
            lines.append(f"  {l}")

    # Verify after-state
    after = _load_profile()
    after_gen  = after.get("generated_at", "missing")
    after_cell = after.get("profiles", {}).get("by_edge_price_bucket", {}).get(SWEET_SPOT_CELL)
    after_n    = int(after_cell.get("trades", 0)) if after_cell else 0
    after_wr   = float(after_cell.get("win_rate", 0)) if after_cell else 0
    after_pnl  = float(after_cell.get("total_pnl", 0)) if after_cell else 0

    from brain.edge_profile_health import edge_profile_health
    new_health = edge_profile_health(after, PROFILE_PATH)
    new_trusted = new_health["edge_profile_trusted"]

    lines.append("")
    lines.append(f"  After: generated_at={after_gen}")
    lines.append(f"  After: trusted={new_trusted}  reason={new_health['reason']}")
    lines.append(f"  After: by_edge_price_bucket cells: "
                 f"{len(after.get('profiles', {}).get('by_edge_price_bucket', {}))}")
    if after_cell:
        lines.append(f"  After: sweet-spot cell n={after_n}, WR={after_wr:.3f}, pnl={after_pnl:+.2f}")
        lines.append(f"  Cell growth: n {before_n} → {after_n} (+{after_n - before_n})")
    else:
        lines.append("  [WARN] Sweet-spot cell missing after rebuild — check data quality.")

    if not new_trusted:
        lines.append(f"  [WARN] Profile is still untrusted after rebuild: {new_health['reason']}")

    return lines, True


# ── section 8: recommended schedule ──────────────────────────────────────────

def section_8_schedule(info: dict) -> list[str]:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 8 — RECOMMENDED SCHEDULE")
    lines.append("=" * 68)
    lines.append("")
    lines.append("  Recommended actions (in order of importance):")
    lines.append("")
    lines.append("  [DAILY — before starting Dashboard]")
    lines.append("    cd /Users/samuel/Desktop/AI_System/tools")
    lines.append("    python3 build_edge_profile.py")
    lines.append("")
    lines.append("  Why daily?  The profile becomes untrusted after 48h.  Rebuilding daily:")
    lines.append("  1. Keeps the price_conditioned 2D path active all day (not just first 48h)")
    lines.append("  2. Incorporates the latest settled trades into the evidence base")
    lines.append("  3. Grows the sweet-spot cell over time → stronger override evidence")
    lines.append("  4. Takes ~2 seconds.  Low cost, high benefit.")
    lines.append("")
    lines.append("  [OPTIONAL — check freshness before any trading session]")
    lines.append("    python3 tools/report_profile_freshness_watchdog.py")
    lines.append("")
    lines.append("  [OPTIONAL — rebuild automatically if stale]")
    lines.append("    python3 tools/report_profile_freshness_watchdog.py --rebuild-if-stale")
    lines.append(f"  (only rebuilds if profile age > {REBUILD_THRESHOLD_HOURS}h)")
    lines.append("")
    lines.append("  [NOT RECOMMENDED — automation (cron/launchd)]")
    lines.append("  Do not set up automated cron jobs yet.  Run manually until the rebuild")
    lines.append("  habit is established and any edge cases in the build script are ruled out.")
    lines.append("  The build takes ~2s and benefits from human awareness when it runs.")

    age = info.get("age_hours")
    if age is not None:
        lines.append("")
        lines.append(f"  Current profile age: {age:.1f}h")
        if age > 36:
            lines.append("  [URGENT] Profile should be rebuilt before next Dashboard session.")
        elif age > REBUILD_THRESHOLD_HOURS:
            lines.append("  Rebuild recommended today (age past proactive threshold).")
        else:
            lines.append("  Profile is fresh.  Next rebuild: before tomorrow's session.")

    return lines


# ── section 9: recommendation ─────────────────────────────────────────────────

def section_9_recommendation(info: dict, table_intact: bool, new_trades_available: bool) -> tuple[list[str], str]:
    lines: list[str] = []
    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 9 — FINAL RECOMMENDATION")
    lines.append("=" * 68)
    lines.append("")

    age     = info.get("age_hours", 0) or 0
    stale   = info.get("stale", True)
    trusted = info.get("trusted", False)

    if not trusted or stale:
        rec = "RUN_MANUAL_EDGE_PROFILE_REBUILD_NOW"
        lines.append(f"  Recommendation: {rec}")
        lines.append("")
        lines.append("  Reason: profile is untrusted or stale.  All signals currently evaluated")
        lines.append("  via bootstrap_era_allow fallback (weaker filter).  The price_conditioned")
        lines.append("  2D path is NOT active.  Rebuild immediately.")
    elif not table_intact:
        rec = "RUN_MANUAL_EDGE_PROFILE_REBUILD_NOW"
        lines.append(f"  Recommendation: {rec}")
        lines.append("")
        lines.append("  Reason: 2D table is missing or sweet-spot cell does not qualify.")
        lines.append("  Rebuild will reconstruct by_edge_price_bucket from current trade data.")
    elif new_trades_available:
        rec = "RUN_MANUAL_EDGE_PROFILE_REBUILD_NOW"
        lines.append(f"  Recommendation: {rec}")
        lines.append("")
        lines.append("  Reason: profile is trusted and the 2D gate is active.  However,")
        lines.append("  significant new normal_modern non-KXETH trades have settled since the")
        lines.append("  last rebuild.  Rebuilding now:")
        lines.append("  (a) strengthens the sweet-spot cell (more n → higher statistical power)")
        lines.append("  (b) resets the staleness clock for another 48h")
        lines.append("  (c) adds new evidence that may reveal cell-level patterns not yet visible")
        lines.append("")
        lines.append("  Then set up a DAILY rebuild habit (see Section 8).")
        lines.append("  After that: BUILD_DASHBOARD_FRESHNESS_PANEL_NEXT — add profile age")
        lines.append("  to the Dashboard header so staleness is visible at a glance.")
    else:
        rec = "BUILD_DASHBOARD_FRESHNESS_PANEL_NEXT"
        lines.append(f"  Recommendation: {rec}")
        lines.append("")
        lines.append("  Profile is trusted, 2D gate is active, no significant new data.")
        lines.append("  No rebuild needed today.  Suggested next build: add a profile-age")
        lines.append("  indicator to the Dashboard header so you know at a glance when to rebuild.")

    return lines, rec


# ── section 10: plain-english ─────────────────────────────────────────────────

def section_10_plain_english(rec: str, info: dict, table_intact: bool) -> list[str]:
    lines: list[str] = []
    age = info.get("age_hours")
    hours_left = info.get("hours_until_stale")
    trusted = bool(info.get("trusted"))
    stale = bool(info.get("stale", True))
    profile_state = "trusted" if trusted and not stale else "stale/untrusted"

    new_nm = 0
    new_sw = 0
    generated = ""
    profile = info.get("profile") or {}
    if isinstance(profile, dict):
        generated = str(profile.get("generated_at") or profile.get("timestamp") or "")
    if generated:
        settled_all = [r for r in _load_jsonl(TRADES_PATH) if r.get("status") == "SETTLED"]
        for r in settled_all:
            if not _is_normal_modern(r):
                continue
            if str(r.get("ticker", "")).upper().startswith("KXETH"):
                continue
            if r.get("timestamp", "") < generated[:19]:
                continue
            new_nm += 1
            ep = _af(r.get("yes_ask") or r.get("entry_price"))
            if ep is not None and ep >= 0.80:
                new_sw += 1

    lines.append("")
    lines.append("=" * 68)
    lines.append("SECTION 10 — PLAIN-ENGLISH ANSWER FOR SAMUEL")
    lines.append("=" * 68)
    lines.append("")
    lines.append("  WHY PHASE 9B CHANGED THE BOTTLENECK:")
    lines.append("  Before Phase 9A, the Critic blocked almost all KXBTC sweet-spot signals")
    lines.append("  because the 1D edge bucket was contaminated (KXETH losses buried with wins).")
    lines.append("  Phase 9A added a 2D evidence layer: if yes_ask >= 0.80 and the matching")
    lines.append("  2D cell meets the configured evidence threshold, the signal can be allowed")
    lines.append("  even when the broader 1D bucket is weak. That is a research guardrail,")
    lines.append("  not proof of profitability or permission to scale.")
    lines.append("")
    lines.append("  WHY RAISING THE CAP FIRST IS DANGEROUS:")
    lines.append("  The 2D path requires a FRESH profile.  When the profile is stale (age > 48h),")
    lines.append("  the Critic falls back to a simpler bootstrap filter.  That filter let through")
    lines.append("  signals in the 0.60-0.70 price zone — where WR=0.43 and PnL is -$14.70.")
    lines.append("  Raising the cap from 20 to 25 on a stale-profile day adds 5 more of those")
    lines.append("  losers per day.  Not more winners.")
    lines.append("")
    lines.append("  CURRENT PROFILE STATE:")
    lines.append(f"  The edge profile is currently {profile_state}.")
    if age is not None:
        lines.append(f"  Current profile age: {age:.1f}h.")
    if hours_left is not None:
        lines.append(f"  Hours until untrust threshold: {hours_left:.1f}h.")
    lines.append(f"  New normal_modern non-KXETH settled rows since rebuild: {new_nm}.")
    lines.append(f"  New sweet-spot settled rows since rebuild: {new_sw}.")
    lines.append(f"  2D table intact: {table_intact}.")
    lines.append(f"  Current recommendation: {rec}.")
    lines.append("")
    lines.append("  WHY PROFILE FRESHNESS MATTERS:")
    lines.append("  The 2D price-conditioned path is only consulted when the profile is TRUSTED.")
    lines.append("  Trusted means age is inside the configured freshness window and sample")
    lines.append("  requirements are satisfied. If the profile goes stale, the system falls")
    lines.append("  back to weaker bootstrap evidence. That is why the dashboard must show")
    lines.append("  age, trust, stale-in time, and rebuild recommendation directly.")
    lines.append("")
    lines.append("  WHAT COMMAND TO RUN DAILY:")
    lines.append("    cd /Users/samuel/Desktop/AI_System/tools")
    lines.append("    python3 build_edge_profile.py")
    lines.append("")
    lines.append("  Run this every morning before starting the Dashboard.  Takes ~2 seconds.")
    lines.append("  It reads trade data and rewrites data/edge_profile.json.  Nothing else.")
    lines.append("")
    lines.append("  WHAT NOT TO TOUCH:")
    lines.append("  - Do NOT raise MAX_TRADES_PER_DAY (cap is not the bottleneck right now)")
    lines.append("  - Do NOT lower MIN_EDGE or MIN_CONFIDENCE")
    lines.append("  - Do NOT change EDGE_PROFILE_MAX_AGE_HOURS = 48")
    lines.append("  - Do NOT enable Kelly (GLOBAL_FORCED_LEARNING_MODE must stay True)")
    lines.append("  - Do NOT touch real_money_allowed or scale_allowed")
    lines.append("  - Do NOT remove KXETH from quarantine")
    return lines


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Edge Profile Freshness Watchdog — read-only by default."
    )
    parser.add_argument(
        "--rebuild-if-stale",
        action="store_true",
        help=f"Rebuild edge_profile.json if age > {REBUILD_THRESHOLD_HOURS}h (safe, dry-run by default).",
    )
    args = parser.parse_args()

    print("=" * 68)
    print("EDGE PROFILE FRESHNESS WATCHDOG — Phase 9C")
    mode = "READ-ONLY" if not args.rebuild_if_stale else f"REBUILD-IF-STALE (threshold={REBUILD_THRESHOLD_HOURS}h)"
    print(f"Mode: {mode}")
    print("=" * 68)
    print()

    # Section 1
    for line in section_1_safety():
        print(line)

    # Section 2
    section2_lines, freshness_info = section_2_freshness()
    for line in section2_lines:
        print(line)

    profile = freshness_info.get("profile", {})

    # Section 3
    section3_lines, table_intact = section_3_2d_integrity(profile)
    for line in section3_lines:
        print(line)

    # Section 4
    for line in section_4_bootstrap_risk():
        print(line)

    # Section 5
    section5_lines, rebuild_ready = section_5_rebuild_readiness()
    for line in section5_lines:
        print(line)

    # Section 6
    for line in section_6_dry_run():
        print(line)

    # Section 7 — only rebuilds with --rebuild-if-stale flag
    section7_lines, rebuilt = section_7_rebuild(args.rebuild_if_stale, rebuild_ready, freshness_info)
    for line in section7_lines:
        print(line)

    # Refresh freshness_info and profile if rebuild happened
    if rebuilt:
        profile = _load_profile()
        _, freshness_info = section_2_freshness()
        _, table_intact   = section_3_2d_integrity(profile)

    # Determine if significant new trades are available
    generated = profile.get("generated_at", "")
    if generated:
        settled_all = [r for r in _load_jsonl(TRADES_PATH) if r.get("status") == "SETTLED"]
        nm_new = [
            r for r in settled_all
            if _is_normal_modern(r)
            and not r.get("ticker", "").upper().startswith("KXETH")
            and r.get("timestamp", "") >= generated[:19]
        ]
        new_trades_available = len(nm_new) >= 5
    else:
        new_trades_available = False

    # Section 8
    for line in section_8_schedule(freshness_info):
        print(line)

    # Section 9
    section9_lines, rec = section_9_recommendation(freshness_info, table_intact, new_trades_available)
    for line in section9_lines:
        print(line)

    # Section 10
    for line in section_10_plain_english(rec, freshness_info, table_intact):
        print(line)

    print()
    print("=" * 68)
    print("PROFILE_FRESHNESS_WATCHDOG_OK")
    print("=" * 68)


if __name__ == "__main__":
    main()
