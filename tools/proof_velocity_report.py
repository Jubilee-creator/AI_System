#!/usr/bin/env python3
"""
tools/proof_velocity_report.py
-------------------------------
Read-only proof velocity tracker.

Shows whether proof accumulation is moving or stalled:
  - Evidence counts by category
  - Open/ghost trade state
  - Trades opened / settled in last 24h and 7d
  - Estimated days to trust gate, scale gate, and proof base
  - Lock status

Never writes, modifies, or settles anything.
Do not count excluded rows as proof.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TRADES_LOG   = ROOT / "logs" / "paper_trades.jsonl"
EDGE_PROFILE = ROOT / "data" / "edge_profile.json"

TRUST_GATE   = 10
SCALE_GATE   = 30
PROOF_BASE   = 30   # clean_settled minimum
MODERN_MIN   = 30   # modern_full minimum


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _load_rows() -> list[dict]:
    if not TRADES_LOG.exists():
        return []
    rows = []
    for line in TRADES_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _is_modern(r: dict) -> bool:
    has_risk_edge   = r.get("risk_edge") is not None
    has_model_prob  = r.get("model_probability") is not None
    has_quote       = any(r.get(k) is not None for k in
                         ("yes_bid", "yes_ask", "no_bid", "no_ask", "price_yes", "price_no"))
    return has_risk_edge and has_model_prob and has_quote


def _classify(rows: list[dict]) -> dict:
    """
    Classify rows using the same logic as clean_truth_report / Dashboard.
    Falls back to manual classification if performance_report import fails.
    """
    try:
        from tools.performance_report import (
            build_terminal_key_sets,
            classify_open_records,
            classify_settled_records,
        )
        active_opens, stale_opens = classify_open_records(rows)
        settled_keys, forced_keys, void_keys = build_terminal_key_sets(rows)
        clean_settled, conflicted = classify_settled_records(
            rows, settled_keys, forced_keys, void_keys,
        )
    except Exception:
        # Minimal fallback
        active_opens = [r for r in rows if r.get("status") == "OPEN"]
        stale_opens  = []
        clean_settled = [r for r in rows if r.get("status") == "SETTLED"]
        conflicted = []

    modern_full   = [r for r in clean_settled if _is_modern(r)]
    dc_override   = [r for r in modern_full if r.get("data_collection_override")]
    provisional   = [
        r for r in modern_full
        if r.get("bootstrap_provisional") and not r.get("data_collection_override")
    ]
    era_allow     = [r for r in modern_full if r.get("bootstrap_era_council_allow")]
    normal_modern = [
        r for r in modern_full
        if not r.get("data_collection_override") and not r.get("bootstrap_provisional")
    ]
    legacy        = [r for r in clean_settled if not _is_modern(r)]

    open_era_allow = [r for r in active_opens if r.get("bootstrap_era_council_allow")]
    open_provisional = [r for r in active_opens if r.get("bootstrap_provisional")]

    return {
        "all_rows":        rows,
        "clean_settled":   clean_settled,
        "conflicted":      conflicted,
        "modern_full":     modern_full,
        "dc_override":     dc_override,
        "provisional":     provisional,
        "era_allow":       era_allow,
        "normal_modern":   normal_modern,
        "legacy":          legacy,
        "active_opens":    active_opens,
        "stale_opens":     stale_opens,
        "open_era_allow":  open_era_allow,
        "open_provisional": open_provisional,
    }


def _recency(rows: list[dict], hours: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [
        r for r in rows
        if (ts := _parse_ts(r.get("timestamp") or r.get("timestamp_utc"))) and ts >= cutoff
    ]


def _rate_per_day(rows: list[dict], window_days: int) -> float | None:
    """
    Compute average settlements per day for the given window.
    Uses actual span from oldest row in window to now.
    Returns None if no rows exist in window.
    """
    recent = _recency(rows, window_days * 24)
    if not recent:
        return None
    ts_list = [
        _parse_ts(r.get("timestamp") or r.get("timestamp_utc"))
        for r in recent
    ]
    ts_list = [t for t in ts_list if t]
    if not ts_list:
        return None
    oldest = min(ts_list)
    span = (datetime.now(timezone.utc) - oldest).total_seconds() / 86400
    span = max(span, 1.0)  # floor at 1 day to avoid divide-by-zero
    return round(len(recent) / span, 3)


def _eta_str(current: int, target: int, rate: float | None) -> str:
    remaining = target - current
    if remaining <= 0:
        return "DONE"
    if rate is None or rate <= 0:
        return "STALLED — no data in window"
    days = remaining / rate
    return f"~{days:.0f} days  (rate: {rate:.3f}/day)"


def _edge_profile_status() -> str:
    if not EDGE_PROFILE.exists():
        return "MISSING — run tools/build_edge_profile.py"
    try:
        data = json.loads(EDGE_PROFILE.read_text(encoding="utf-8"))
        raw_ts = data.get("generated_at") or data.get("timestamp")
        ts = _parse_ts(raw_ts)
        if ts:
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            label = "FRESH" if age_h < 48 else "STALE"
            return f"{age_h:.1f}h old  [{label}]"
    except Exception:
        pass
    return "UNREADABLE"


# ── Report ───────────────────────────────────────────────────────────────────

def report() -> None:
    if not TRADES_LOG.exists():
        print(f"[WARN] No trades log found: {TRADES_LOG}")
        return

    rows = _load_rows()
    c = _classify(rows)
    now = datetime.now(timezone.utc)

    nm = c["normal_modern"]
    cs = c["clean_settled"]
    mf = c["modern_full"]

    # Recency windows on all rows (opened)
    all_24h  = _recency(c["all_rows"], 24)
    all_7d   = _recency(c["all_rows"], 168)
    # Settled recency
    settled_24h = _recency(cs, 24)
    settled_7d  = _recency(cs, 168)
    # Normal modern recency (by open timestamp)
    nm_7d  = _recency(nm, 168)
    nm_30d = _recency(nm, 720)

    # Rate calculation for ETAs
    nm_rate_7d  = _rate_per_day(nm, 7)
    nm_rate_30d = _rate_per_day(nm, 30)
    # Use better estimate: prefer 7d if available and non-zero, else 30d
    nm_rate_best = (nm_rate_7d  if (nm_rate_7d  is not None and nm_rate_7d  > 0)
                    else nm_rate_30d)

    cs_rate = _rate_per_day(cs, 30)
    mf_rate = _rate_per_day(mf, 30)

    print("=" * 72)
    print("PROOF VELOCITY REPORT")
    print(f"  Generated : {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 72)

    # ── Evidence counts ───────────────────────────────────────────────────────
    print("\nEVIDENCE INVENTORY")
    print(f"  {'all_rows in log':<40s} {len(rows):4d}")
    print(f"  {'clean_settled':<40s} {len(cs):4d}  (target >= {PROOF_BASE})")
    print(f"  {'conflicted / excluded':<40s} {len(c['conflicted']):4d}")
    print(f"  {'modern_full_metadata':<40s} {len(mf):4d}  (target >= {MODERN_MIN})")
    print(f"    {'├─ data_collection_override [EXCLUDED]':<38s} {len(c['dc_override']):4d}")
    print(f"    {'├─ bootstrap_provisional    [EXCLUDED]':<38s} {len(c['provisional']):4d}")
    print(f"    {'├─ bootstrap_era_allow      [COUNTS]':<38s} {len(c['era_allow']):4d}")
    print(f"    {'└─ normal_council_approved  [PROOF]':<38s} {len(nm):4d}  ← main proof base")
    print(f"  {'legacy_edge_only [QUARANTINED]':<40s} {len(c['legacy']):4d}")

    # ── Open trade state ──────────────────────────────────────────────────────
    print("\nOPEN TRADE STATE")
    print(f"  {'active_opens (counting against cap=3)':<40s} {len(c['active_opens']):4d}")
    print(f"  {'stale_opens (ghost, settled on disk)':<40s} {len(c['stale_opens']):4d}")
    print(f"  {'open era_allow (pending settlement)':<40s} {len(c['open_era_allow']):4d}")
    print(f"  {'open provisional (pending, excluded)':<40s} {len(c['open_provisional']):4d}")
    cap_full = len(c["active_opens"]) >= 3
    print(f"  {'cap_status':<40s} {'FULL — blocking new trades' if cap_full else 'NOT FULL — ok'}")

    # ── Recent activity ───────────────────────────────────────────────────────
    print("\nRECENT ACTIVITY")
    print(f"  {'rows opened in last 24h':<40s} {len(all_24h):4d}")
    print(f"  {'rows opened in last 7d':<40s} {len(all_7d):4d}")
    print(f"  {'clean_settled in last 24h':<40s} {len(settled_24h):4d}")
    print(f"  {'clean_settled in last 7d':<40s} {len(settled_7d):4d}")
    print(f"  {'normal_modern settled in last 7d':<40s} {len(nm_7d):4d}")
    print(f"  {'normal_modern settled in last 30d':<40s} {len(nm_30d):4d}")

    # ── Accumulation rates ────────────────────────────────────────────────────
    print("\nACCUMULATION RATE (normal_modern settlements / day)")
    if nm_rate_7d is not None:
        print(f"  7-day window  : {nm_rate_7d:.3f} / day")
    else:
        print(f"  7-day window  : NO DATA")
    if nm_rate_30d is not None:
        print(f"  30-day window : {nm_rate_30d:.3f} / day")
    else:
        print(f"  30-day window : NO DATA")
    if nm_rate_best is None:
        print("  [WARNING] Rate is effectively zero — proof is stalled.")

    # ── ETA projections ───────────────────────────────────────────────────────
    print("\nETA TO PROOF GATES  (at best available rate)")
    print(f"  clean_settled >= {PROOF_BASE}                : {_eta_str(len(cs), PROOF_BASE, cs_rate)}")
    print(f"  modern_full >= {MODERN_MIN}                  : {_eta_str(len(mf), MODERN_MIN, mf_rate)}")
    print(f"  normal_modern >= {TRUST_GATE}  (trust gate)  : {_eta_str(len(nm), TRUST_GATE, nm_rate_best)}")
    print(f"  normal_modern >= {SCALE_GATE}  (scale gate)  : {_eta_str(len(nm), SCALE_GATE, nm_rate_best)}")

    # ── System locks ──────────────────────────────────────────────────────────
    print("\nSYSTEM LOCKS  (hardcoded — cannot change via config)")
    print(f"  real_money_allowed : False")
    print(f"  scale_allowed      : False")
    print(f"  Kelly execution    : DISABLED (GLOBAL_FORCED_LEARNING_MODE=True)")
    print(f"  edge_profile       : {_edge_profile_status()}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print("\nVERDICT")
    n_nm = len(nm)
    if n_nm == 0:
        verdict = ("CRITICAL — 0 normal_modern trades. Bootstrap path must produce "
                   "settled era_allow trades to accumulate proof.")
    elif nm_rate_best is None or nm_rate_best < 0.05:
        verdict = (f"STALLED — {n_nm}/{TRUST_GATE} normal_modern. "
                   f"Rate < 0.05/day. System is technically healthy but proof accumulation is negligible.")
    elif n_nm < TRUST_GATE:
        eta = _eta_str(n_nm, TRUST_GATE, nm_rate_best)
        verdict = f"SLOW — {n_nm}/{TRUST_GATE} normal_modern to trust gate. {eta}"
    elif n_nm < SCALE_GATE:
        eta = _eta_str(n_nm, SCALE_GATE, nm_rate_best)
        verdict = f"BUILDING — {n_nm}/{SCALE_GATE} normal_modern to scale gate. {eta}"
    else:
        verdict = (f"GATE COUNT MET — {n_nm} normal_modern. "
                   "Performance gates (ROI>0, CLV>0, PF>1.10) must ALSO pass before scale review.")
    print(f"  {verdict}")

    print("\n  Do not count dc_override or bootstrap_provisional as proof.")
    print("  real_money_allowed and scale_allowed are hardcoded False regardless of evidence.")
    print()


if __name__ == "__main__":
    report()
