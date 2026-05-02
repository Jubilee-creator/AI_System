#!/usr/bin/env python3
"""
tools/report_edge_trust_gate.py
--------------------------------
Read-only diagnostic: why is edge_profile_trusted=False and what is needed to
break through to trusted status?

Shows:
  - Current gate state vs required thresholds
  - Distance to each gate (how many trades away)
  - Bootstrap allow path progress (Phase 6B-2)
  - Stale OPEN record breakdown (ghost trades that never counted)
  - Dashboard restart warning if bootstrap_era_allow_count=0 post-Phase-6B-2

Does NOT modify any state.

Expected result: EDGE_TRUST_GATE_REPORT_OK
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (
    BOOTSTRAP_ALLOW_ENABLED,
    BOOTSTRAP_MIN_CONFIDENCE,
    BOOTSTRAP_MIN_EDGE,
    DATA_COLLECTION_OVERRIDE_ENABLED,
    EDGE_PROFILE_MAX_AGE_HOURS,
    EDGE_PROFILE_MIN_CLEAN_SETTLED,
    EDGE_PROFILE_MIN_MODERN_FULL,
    EDGE_PROFILE_MIN_NORMAL_MODERN,
    GLOBAL_FORCED_LEARNING_MODE,
)

EDGE_PROFILE_PATH = ROOT / "data" / "edge_profile.json"
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"
PHASE_6B2_COMMIT_HASH = "392ecb9"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _age_h(ts) -> float | None:
    if ts is None:
        return None
    return (_now() - ts).total_seconds() / 3600


def _bar(current: int, required: int, width: int = 30) -> str:
    filled = min(int(width * current / required), width) if required > 0 else width
    remaining = width - filled
    pct = min(current / required * 100, 100) if required > 0 else 100
    return f"[{'#' * filled}{'.' * remaining}] {current}/{required}  ({pct:.0f}%)"


def _gate_line(label: str, current: int, required: int, ok: bool) -> str:
    status = "[PASS]" if ok else "[FAIL]"
    gap = max(0, required - current)
    gap_str = f"  need {gap} more" if not ok else "  met"
    return f"  {status}  {label:<38}  {_bar(current, required)}  {gap_str}"


def load_edge_profile() -> dict:
    if not EDGE_PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(EDGE_PROFILE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_all_trades() -> list[dict]:
    if not TRADES_LOG.exists():
        return []
    records = []
    for line in TRADES_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def analyse_open_records(all_records: list[dict]) -> dict:
    open_recs = [r for r in all_records if r.get("status") == "OPEN"]
    tickers_settled = {
        r.get("ticker")
        for r in all_records
        if r.get("status") in ("SETTLED", "FORCED_CLOSE", "VOID_LEGACY_DUPLICATE")
        and r.get("ticker")
    }
    stale = [r for r in open_recs if r.get("ticker") in tickers_settled]
    active = [r for r in open_recs if r.get("ticker") not in tickers_settled]

    now = _now()
    ages = []
    for rec in open_recs:
        ts = _parse_ts(rec.get("timestamp"))
        if ts:
            ages.append((now - ts).total_seconds() / 3600)

    council_breakdown: dict[str, int] = {}
    for rec in stale:
        key = rec.get("council_decision") or rec.get("final_decision") or "None"
        council_breakdown[str(key)] = council_breakdown.get(str(key), 0) + 1

    flag_breakdown: dict[str, int] = {}
    for rec in stale:
        flags = []
        if rec.get("data_collection_override"):
            flags.append("data_collection_override")
        if rec.get("bootstrap_provisional"):
            flags.append("bootstrap_provisional")
        if rec.get("bootstrap_era_council_allow"):
            flags.append("bootstrap_era_council_allow")
        key = "|".join(flags) if flags else "none"
        flag_breakdown[key] = flag_breakdown.get(key, 0) + 1

    return {
        "total_open": len(open_recs),
        "active": len(active),
        "stale": len(stale),
        "ages_h": sorted(ages) if ages else [],
        "council_breakdown": council_breakdown,
        "flag_breakdown": flag_breakdown,
    }


def analyse_bootstrap_era(all_records: list[dict]) -> dict:
    settled = [r for r in all_records if r.get("status") == "SETTLED"]
    era_allow = [r for r in settled if r.get("bootstrap_era_council_allow")]
    provisional = [r for r in settled if r.get("bootstrap_provisional")]
    dc_override = [r for r in settled if r.get("data_collection_override")]

    open_era_allow = [
        r for r in all_records
        if r.get("status") == "OPEN" and r.get("bootstrap_era_council_allow")
    ]

    return {
        "settled_era_allow": len(era_allow),
        "settled_provisional": len(provisional),
        "settled_dc_override": len(dc_override),
        "open_era_allow": len(open_era_allow),
    }


def main() -> None:
    profile = load_edge_profile()
    all_records = load_all_trades()
    health = profile.get("edge_profile_health", {})
    thresholds = health.get("thresholds", {})

    clean_settled  = int(profile.get("clean_settled_trades", 0))
    modern_full    = int(profile.get("modern_full_metadata_trades", 0))
    normal_modern  = int(profile.get("normal_council_approved_modern_trades", 0))
    dc_count       = int(profile.get("data_collection_override_count", 0))
    bootstrap_prov = int(profile.get("bootstrap_provisional_count", 0))
    era_allow_cnt  = int(profile.get("bootstrap_era_allow_count", 0))
    trusted        = bool(health.get("edge_profile_trusted", False))
    trust_reason   = health.get("reason", "unknown")

    req_clean   = int(thresholds.get("min_clean_settled", EDGE_PROFILE_MIN_CLEAN_SETTLED))
    req_modern  = int(thresholds.get("min_modern_full", EDGE_PROFILE_MIN_MODERN_FULL))
    req_normal  = int(thresholds.get("min_normal_modern", EDGE_PROFILE_MIN_NORMAL_MODERN))
    req_age_h   = float(thresholds.get("max_age_hours", EDGE_PROFILE_MAX_AGE_HOURS))

    generated_at = _parse_ts(profile.get("generated_at"))
    profile_age_h = _age_h(generated_at)

    open_info = analyse_open_records(all_records)
    era_info = analyse_bootstrap_era(all_records)

    W = 72
    print("=" * W)
    print("EDGE TRUST GATE DIAGNOSTIC")
    print("=" * W)
    print(f"Profile generated:  {profile.get('generated_at', 'missing')}")
    if profile_age_h is not None:
        age_status = "FRESH" if profile_age_h < req_age_h else "STALE"
        print(f"Profile age:        {profile_age_h:.1f}h  (max {req_age_h:.0f}h)  [{age_status}]")
    print(f"Trusted:            {'YES' if trusted else 'NO'}")
    print()

    print("GATE PROGRESS")
    print("-" * W)
    gate1_ok = clean_settled >= req_clean
    gate2_ok = modern_full >= req_modern
    gate3_ok = normal_modern >= req_normal
    print(_gate_line("clean_settled_trades",            clean_settled, req_clean,  gate1_ok))
    print(_gate_line("modern_full_metadata_trades",     modern_full,   req_modern, gate2_ok))
    print(_gate_line("normal_council_approved_modern",  normal_modern, req_normal, gate3_ok))
    print()
    print("Trust reason:")
    for part in trust_reason.split(";"):
        part = part.strip()
        if part:
            print(f"  - {part}")
    print()

    print("MODERN FULL-METADATA BREAKDOWN")
    print("-" * W)
    print(f"  modern_full_metadata_trades:          {modern_full}")
    print(f"  data_collection_override:             {dc_count}  (excluded from normal proof)")
    print(f"  bootstrap_provisional:                {bootstrap_prov}  (excluded from normal proof)")
    print(f"  bootstrap_era_council_allow:          {era_allow_cnt}  (COUNTS as normal proof — Phase 6B-2)")
    normal_calc = max(0, modern_full - dc_count - bootstrap_prov)
    print(f"  normal_council_approved_modern:       {normal_calc}  (= modern - dc - provisional)")
    print()

    print("BOOTSTRAP ALLOW PATH (Phase 6B-2)")
    print("-" * W)
    print(f"  BOOTSTRAP_ALLOW_ENABLED:              {BOOTSTRAP_ALLOW_ENABLED}")
    print(f"  BOOTSTRAP_MIN_EDGE:                   {BOOTSTRAP_MIN_EDGE:.3f}  ({BOOTSTRAP_MIN_EDGE*100:.0f}%)")
    print(f"  BOOTSTRAP_MIN_CONFIDENCE:             {BOOTSTRAP_MIN_CONFIDENCE:.2f}  ({BOOTSTRAP_MIN_CONFIDENCE*100:.0f}%)")
    print(f"  DATA_COLLECTION_OVERRIDE_ENABLED:     {DATA_COLLECTION_OVERRIDE_ENABLED}  (trust deadlock guard)")
    print(f"  GLOBAL_FORCED_LEARNING_MODE:          {GLOBAL_FORCED_LEARNING_MODE}")
    print()
    print(f"  Settled bootstrap_era_allow trades:   {era_info['settled_era_allow']}")
    print(f"  Open bootstrap_era_allow trades:      {era_info['open_era_allow']}")
    print(f"  Settled bootstrap_provisional:        {era_info['settled_provisional']}")
    print(f"  Settled data_collection_override:     {era_info['settled_dc_override']}")
    print()

    if BOOTSTRAP_ALLOW_ENABLED and era_info["settled_era_allow"] == 0 and era_info["open_era_allow"] == 0:
        print("  [WARNING] BOOTSTRAP_ALLOW_ENABLED=True but zero bootstrap_era_allow trades.")
        print("  Most likely cause: Dashboard.py process is still running with OLD cached")
        print("  imports from before Phase 6B-2 commit. Python import caching means the")
        print("  running process still has DATA_COLLECTION_OVERRIDE_ENABLED=True in memory.")
        print()
        print("  ACTION REQUIRED: Restart Dashboard.py to pick up Phase 6B-2 code.")
        print("  After restart, new bootstrap-quality signals will return ALLOW")
        f"  (edge>={BOOTSTRAP_MIN_EDGE:.2f}, conf>={BOOTSTRAP_MIN_CONFIDENCE:.2f}) and count as normal_council_approved_modern."
        print()

    print("STALE OPEN RECORDS")
    print("-" * W)
    print(f"  Total OPEN in log:                    {open_info['total_open']}")
    print(f"  Active (no settlement found):         {open_info['active']}")
    print(f"  Stale  (settlement exists):           {open_info['stale']}")
    if open_info["ages_h"]:
        ages = open_info["ages_h"]
        median = ages[len(ages) // 2]
        print(f"  Age range:                            {ages[0]:.1f}h – {ages[-1]:.1f}h  (median {median:.1f}h)")
    if open_info["council_breakdown"]:
        print(f"  Stale council_decision breakdown:")
        for k, v in sorted(open_info["council_breakdown"].items()):
            print(f"    {k:<30} {v}")
    if open_info["flag_breakdown"]:
        print(f"  Stale flag breakdown:")
        for k, v in sorted(open_info["flag_breakdown"].items()):
            print(f"    {k:<40} {v}")
    print()

    print("DISTANCE TO TRUSTED")
    print("-" * W)
    blockers = []
    if not gate1_ok:
        blockers.append(f"clean_settled: need {req_clean - clean_settled} more settled trades")
    if not gate2_ok:
        blockers.append(f"modern_full_metadata: need {req_modern - modern_full} more full-metadata trades")
    if not gate3_ok:
        blockers.append(f"normal_modern: need {req_normal - normal_modern} more normal council-approved trades "
                        f"(bootstrap_era_allow counts; provisional does NOT)")

    if not blockers:
        print("  All hard gates met — profile would be trusted if regenerated now.")
    else:
        print("  Remaining blockers:")
        for b in blockers:
            print(f"    - {b}")

    if not gate3_ok and BOOTSTRAP_ALLOW_ENABLED:
        need = req_normal - normal_modern
        print()
        print(f"  Bootstrap path: {need} more bootstrap_era_allow SETTLED trades needed.")
        print(f"  These come from signals with edge>={BOOTSTRAP_MIN_EDGE:.2f} and conf>={BOOTSTRAP_MIN_CONFIDENCE:.2f}.")
        print("  Phase 6B-2 bootstrap path is active — every qualifying signal returns ALLOW.")

    print()
    print("=" * W)
    print("RESULT: EDGE_TRUST_GATE_REPORT_OK")


if __name__ == "__main__":
    main()
