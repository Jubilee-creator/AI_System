#!/usr/bin/env python3
"""
tools/report_runtime_hygiene_shadow_maturity.py
------------------------------------------------
Phase 10R — Runtime Hygiene Shadow Maturity Report

Read-only maturity tracker for the upstream hygiene shadow logger (Phase 10P).
Tracks whether the runtime shadow log has accumulated enough rows to compare
variants reliably, whether safety invariants hold in every row, and whether
any settled paper-trade outcomes can yet be matched to shadow scans.

Does NOT:
  - deploy filters or change scanner order/ranking
  - modify PaperTrader, council, risk thresholds, or strategy
  - write to any log or data file
  - force trades or bypass any safety gate

Sentinel: RUNTIME_HYGIENE_SHADOW_MATURITY_OK
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from logs.upstream_hygiene_shadow_logger import LOG_PATH  # noqa: E402

SENTINEL = "RUNTIME_HYGIENE_SHADOW_MATURITY_OK"
TRADES_LOG = ROOT / "logs" / "paper_trades.jsonl"

# ── Maturity thresholds (row count) ──────────────────────────────────────────
THRESHOLD_IMMATURE   = 50
THRESHOLD_EARLY      = 100
THRESHOLD_DEVELOPING = 300

# ── Freshness thresholds (seconds since latest row) ───────────────────────────
FRESH_MAX_S = 600    # < 10 min
STALE_MIN_S = 1800   # > 30 min

VARIANT_NAMES = (
    "current",
    "stack1_quarantine_only",
    "research_variant_weak_rr",
    "research_variant_expensive_entry",
    "aggressive_stack",
)

_AGGREGATE_KEYS = (
    "candidate_count",
    "removed_count",
    "quality_score",
    "avg_entry",
    "avg_reward_risk",
    "avg_model_margin",
    "quarantine_count",
    "weak_reward_risk_count",
    "expensive_80_90_count",
    "market_quality_estimate",
    "min_edge_estimate",
)

W = 68


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _avg(values: list[Any]) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else None


def _fmt(value: float | None, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}"


# ── Row classification ────────────────────────────────────────────────────────

def is_projection_row(row: dict[str, Any]) -> bool:
    """Projection rows are report-only fallbacks, not live runtime captures."""
    run_id  = str(row.get("run_id")  or "")
    scan_id = str(row.get("scan_id") or "")
    return "REPORT_ONLY" in run_id or "REPORT_ONLY" in scan_id


def split_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runtime    = [r for r in rows if not is_projection_row(r)]
    projection = [r for r in rows if is_projection_row(r)]
    return runtime, projection


# ── Maturity and freshness labels ─────────────────────────────────────────────

def maturity_label(count: int) -> str:
    if count == 0:
        return "NO_RUNTIME_ROWS"
    if count < THRESHOLD_IMMATURE:
        return "IMMATURE_UNDER_50_ROWS"
    if count < THRESHOLD_EARLY:
        return "EARLY_50_TO_100_ROWS"
    if count < THRESHOLD_DEVELOPING:
        return "DEVELOPING_100_TO_300_ROWS"
    return "MATURE_300_PLUS_ROWS"


def freshness_label(age_s: float | None) -> str:
    if age_s is None:
        return "UNKNOWN"
    if age_s < FRESH_MAX_S:
        return "FRESH"
    if age_s < STALE_MIN_S:
        return "AGING"
    return "STALE"


# ── Safety violation scan ─────────────────────────────────────────────────────

def check_safety_violations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ec  = sum(1 for r in rows if r.get("execution_changed")    is True)
    lsm = sum(1 for r in rows if r.get("live_strategy_mutated") is True)
    ld  = sum(1 for r in rows if r.get("live_deployable")       is True)
    so  = sum(1 for r in rows if r.get("shadow_only")           is False)
    note_wrong = sum(1 for r in rows if r.get("note") != "SHADOW_ONLY_NOT_EXECUTION")

    variant_ld = 0
    for row in rows:
        for vdata in (row.get("variants") or {}).values():
            if isinstance(vdata, dict) and vdata.get("live_deployable") is True:
                variant_ld += 1

    total = ec + lsm + ld + so + variant_ld
    return {
        "execution_changed_violations":     ec,
        "live_strategy_mutated_violations": lsm,
        "live_deployable_violations":       ld,
        "shadow_only_false_violations":     so,
        "note_wrong_violations":            note_wrong,
        "variant_live_deployable_violations": variant_ld,
        "total_violations": total,
        "all_clear":        total == 0,
    }


# ── Variant aggregation ───────────────────────────────────────────────────────

def aggregate_variants(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, list]] = {n: defaultdict(list) for n in VARIANT_NAMES}
    counts: dict[str, int]             = {n: 0                  for n in VARIANT_NAMES}

    for row in rows:
        variants = row.get("variants") or {}
        for name in VARIANT_NAMES:
            vdata = variants.get(name)
            if not isinstance(vdata, dict):
                continue
            counts[name] += 1
            for key in _AGGREGATE_KEYS:
                val = vdata.get(key)
                if isinstance(val, (int, float)):
                    values[name][key].append(float(val))

    result: dict[str, dict[str, Any]] = {}
    for name in VARIANT_NAMES:
        result[name] = {
            "row_count": counts[name],
            **{key: _avg(values[name][key]) for key in _AGGREGATE_KEYS},
        }
    return result


def starvation_distribution(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    dist: dict[str, Counter] = {n: Counter() for n in VARIANT_NAMES}
    for row in rows:
        sr = row.get("starvation_risk") or {}
        for name in VARIANT_NAMES:
            val = sr.get(name)
            if val:
                dist[name][val] += 1
    return {n: dict(c) for n, c in dist.items()}


def safety_class_distribution(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    dist: dict[str, Counter] = {n: Counter() for n in VARIANT_NAMES}
    for row in rows:
        sc = row.get("safety_classification") or {}
        for name in VARIANT_NAMES:
            val = sc.get(name)
            if val:
                dist[name][val] += 1
    return {n: dict(c) for n, c in dist.items()}


# ── Variant comparison against current baseline ───────────────────────────────

def compare_variants(aggregate: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    baseline = aggregate.get("current", {})
    b_quality    = baseline.get("quality_score")
    b_candidates = baseline.get("candidate_count")
    b_quarantine = baseline.get("quarantine_count")
    b_weak_rr    = baseline.get("weak_reward_risk_count")
    b_expensive  = baseline.get("expensive_80_90_count")

    comparison: dict[str, dict[str, Any]] = {}
    for name in VARIANT_NAMES:
        if name == "current":
            continue
        v = aggregate.get(name, {})
        v_quality    = v.get("quality_score")
        v_candidates = v.get("candidate_count")

        def _delta(a: float | None, b: float | None) -> float | None:
            return (a - b) if (a is not None and b is not None) else None

        q_delta   = _delta(v_quality,                  b_quality)
        c_delta   = _delta(v_candidates,               b_candidates)
        quar_delta = _delta(v.get("quarantine_count"), b_quarantine)
        wrr_delta  = _delta(v.get("weak_reward_risk_count"), b_weak_rr)
        exp_delta  = _delta(v.get("expensive_80_90_count"),  b_expensive)

        avg_remaining = v_candidates or 0.0
        starvation_risk = avg_remaining < 2.0

        if q_delta is None:
            assessment = "UNKNOWN"
        elif q_delta > 20:
            assessment = "SUBSTANTIAL_QUALITY_GAIN"
        elif q_delta > 8:
            assessment = "MODERATE_QUALITY_GAIN"
        elif q_delta > 2:
            assessment = "MARGINAL_QUALITY_GAIN"
        else:
            assessment = "MINIMAL_OR_NO_GAIN"

        if name == "stack1_quarantine_only":
            rec = "STACK1_SAFE_TO_CONTINUE_SHADOW"
        elif name == "aggressive_stack":
            rec = "AGGRESSIVE_REJECT_FOR_LIVE"
        elif name == "research_variant_weak_rr":
            rec = "WEAK_RR_RESEARCH_ONLY"
        elif name == "research_variant_expensive_entry":
            rec = "EXPENSIVE_ENTRY_RESEARCH_ONLY"
        else:
            rec = "RESEARCH_ONLY"

        comparison[name] = {
            "quality_delta":               q_delta,
            "candidate_delta":             c_delta,
            "quarantine_count_delta":      quar_delta,
            "weak_rr_count_delta":         wrr_delta,
            "expensive_entry_count_delta": exp_delta,
            "avg_candidates_remaining":    v_candidates,
            "starvation_risk":             starvation_risk,
            "improvement_assessment":      assessment,
            "likely_useful":               (q_delta is not None and q_delta > 5 and not starvation_risk),
            "safe_for_more_shadow":        (name != "aggressive_stack" and not starvation_risk),
            "recommendation":             rec,
        }
    return comparison


# ── Scan-id stats ─────────────────────────────────────────────────────────────

def scan_id_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scan_ids = [str(r.get("scan_id") or "") for r in rows if r.get("scan_id")]
    run_ids  = sorted({str(r.get("run_id") or "") for r in rows if r.get("run_id")})

    nums: list[int] = []
    for sid in scan_ids:
        parts = sid.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            nums.append(int(parts[1]))

    rows_per_run: dict[str, int] = defaultdict(int)
    for r in rows:
        rid = str(r.get("run_id") or "UNKNOWN")
        rows_per_run[rid] += 1

    return {
        "scan_id_count":   len(scan_ids),
        "run_id_count":    len(run_ids),
        "run_ids":         run_ids,
        "scan_number_min": min(nums) if nums else None,
        "scan_number_max": max(nums) if nums else None,
        "rows_per_run_id": dict(rows_per_run),
    }


# ── Outcome matching ──────────────────────────────────────────────────────────

def attempt_outcome_matching(
    runtime_rows: list[dict[str, Any]],
    trades_path: Path = TRADES_LOG,
) -> dict[str, Any]:
    if not runtime_rows:
        return {
            "attempted": False,
            "reason":    "NO_RUNTIME_ROWS",
            "verdict":   "NO_OUTCOME_MATCHES_YET",
        }

    timestamps = [_parse_ts(r.get("timestamp_utc")) for r in runtime_rows]
    valid_ts   = sorted(ts for ts in timestamps if ts is not None)
    if not valid_ts:
        return {
            "attempted": False,
            "reason":    "NO_VALID_TIMESTAMPS_IN_SHADOW_LOG",
            "verdict":   "NO_OUTCOME_MATCHES_YET",
        }

    shadow_start = valid_ts[0]
    trade_rows   = _read_jsonl(trades_path)

    settled_after = open_after = 0
    for tr in trade_rows:
        ts = _parse_ts(tr.get("timestamp") or tr.get("trade_timestamp") or tr.get("timestamp_utc"))
        if ts is None or ts < shadow_start:
            continue
        status = str(tr.get("status") or "").upper()
        if status == "SETTLED":
            settled_after += 1
        elif status == "OPEN":
            open_after += 1

    # Direct scan_id matching is impossible: paper_trades.jsonl does not
    # record scan_ids.  We report counts only and do not fake matches.
    if settled_after == 0 and open_after == 0:
        verdict = "NO_OUTCOME_MATCHES_YET"
    elif settled_after == 0:
        verdict = f"NO_SETTLED_OUTCOMES_YET_{open_after}_OPEN_TRADES_PENDING"
    else:
        verdict = f"POTENTIAL_OUTCOMES_AVAILABLE_{settled_after}_SETTLED_NO_DIRECT_ID_MATCH"

    return {
        "attempted":                   True,
        "shadow_start_utc":            shadow_start.isoformat(),
        "total_trades_in_log":         len(trade_rows),
        "settled_after_shadow_start":  settled_after,
        "open_after_shadow_start":     open_after,
        "direct_scan_id_matching":     False,
        "note": (
            "paper_trades.jsonl does not record scan_ids; "
            "timestamp-proximity matching only"
        ),
        "verdict": verdict,
    }


# ── Verdict and recommendations ───────────────────────────────────────────────

def verdict_label(n: int, freshness: str, has_violations: bool) -> str:
    if has_violations:
        return "LOGGER_SAFETY_VIOLATION"
    if n == 0:
        return "COLLECTION_NOT_STARTED"
    if freshness == "STALE":
        return "LOGGER_STALE"
    if n < THRESHOLD_IMMATURE:
        return "COLLECTION_ACTIVE_BUT_IMMATURE"
    if n < THRESHOLD_EARLY:
        return "COLLECTION_ACTIVE_EARLY_SIGNAL_ONLY"
    if n < THRESHOLD_DEVELOPING:
        return "COLLECTION_ACTIVE_DEVELOPING"
    return "COLLECTION_MATURE_NO_OUTCOMES"


def recommendation_labels(
    comparison: dict[str, dict[str, Any]],
    n: int,
) -> list[str]:
    recs: list[str] = []
    if n < THRESHOLD_EARLY:
        recs.append("KEEP_COLLECTING")
    if n >= THRESHOLD_IMMATURE:
        recs.append("READY_FOR_OUTCOME_MATCHING")
    recs.append("NOT_READY_FOR_STRATEGY_PATCH")
    for cmp in comparison.values():
        rec = cmp.get("recommendation", "")
        if rec and rec not in recs:
            recs.append(rec)
    return recs


# ── Main build ────────────────────────────────────────────────────────────────

def build_report(
    log_path:    Path = LOG_PATH,
    trades_path: Path = TRADES_LOG,
) -> dict[str, Any]:
    now      = datetime.now(timezone.utc)
    all_rows = _read_jsonl(log_path)
    runtime_rows, projection_rows = split_rows(all_rows)

    n      = len(runtime_rows)
    mat    = maturity_label(n)

    timestamps = [_parse_ts(r.get("timestamp_utc")) for r in runtime_rows]
    valid_ts   = sorted(ts for ts in timestamps if ts is not None)
    first_ts   = valid_ts[0]  if valid_ts else None
    latest_ts  = valid_ts[-1] if valid_ts else None

    age_s     = (now - latest_ts).total_seconds() if latest_ts else None
    freshness = freshness_label(age_s)

    violations     = check_safety_violations(runtime_rows)
    has_violations = not violations["all_clear"]

    agg         = aggregate_variants(runtime_rows)
    comparison  = compare_variants(agg)
    starv_dist  = starvation_distribution(runtime_rows)
    class_dist  = safety_class_distribution(runtime_rows)
    sid_stats   = scan_id_stats(runtime_rows)
    outcome     = attempt_outcome_matching(runtime_rows, trades_path)
    verdict     = verdict_label(n, freshness, has_violations)
    recs        = recommendation_labels(comparison, n)

    return {
        "log_path":              str(log_path),
        "log_exists":            log_path.exists(),
        "total_rows_in_log":     len(all_rows),
        "runtime_rows":          n,
        "projection_rows":       len(projection_rows),
        "first_runtime_ts":      first_ts.isoformat()  if first_ts  else None,
        "latest_runtime_ts":     latest_ts.isoformat() if latest_ts else None,
        "latest_row_age_seconds": round(age_s, 1) if age_s is not None else None,
        "freshness":             freshness,
        "maturity":              mat,
        "scan_stats":            sid_stats,
        "violations":            violations,
        "aggregate":             agg,
        "comparison":            comparison,
        "starvation_distribution": starv_dist,
        "safety_class_distribution": class_dist,
        "outcome_matching":      outcome,
        "verdict":               verdict,
        "recommendations":       recs,
        "live_deployable":       False,
        "execution_changed":     False,
        "live_strategy_mutated": False,
        "source": "runtime_shadow_log" if n > 0 else "none",
    }


# ── Formatted output ──────────────────────────────────────────────────────────

def _section(title: str) -> None:
    print(f"\n{title}")
    print("-" * W)


def _variant_block(name: str, vstats: dict[str, Any], cmp: dict[str, Any] | None = None) -> None:
    rr   = vstats.get("row_count", 0)
    cand = vstats.get("candidate_count")
    rem  = vstats.get("removed_count")
    qs   = vstats.get("quality_score")
    print(f"  {name}:")
    print(f"    rows={rr}  avg_candidates={_fmt(cand,2)}  avg_removed={_fmt(rem,2)}"
          f"  avg_quality={_fmt(qs,2)}")
    print(f"    avg_entry={_fmt(vstats.get('avg_entry'))}  "
          f"avg_rr={_fmt(vstats.get('avg_reward_risk'))}  "
          f"avg_margin={_fmt(vstats.get('avg_model_margin'))}")
    print(f"    avg_quarantine={_fmt(vstats.get('quarantine_count'),2)}  "
          f"avg_weak_rr={_fmt(vstats.get('weak_reward_risk_count'),2)}  "
          f"avg_expensive={_fmt(vstats.get('expensive_80_90_count'),2)}")
    if cmp:
        print(f"    quality_delta={_fmt_delta(cmp.get('quality_delta'))}  "
              f"candidate_delta={_fmt_delta(cmp.get('candidate_delta'))}  "
              f"assessment={cmp.get('improvement_assessment')}")
        print(f"    starvation_risk={cmp.get('starvation_risk')}  "
              f"likely_useful={cmp.get('likely_useful')}  "
              f"recommendation={cmp.get('recommendation')}")


def print_report(report: dict[str, Any]) -> None:
    print("=" * W)
    print("RUNTIME HYGIENE SHADOW MATURITY REPORT  (Phase 10R)")
    print("=" * W)

    _section("COLLECTION STATUS")
    print(f"  log_path:              {report['log_path']}")
    print(f"  log_exists:            {report['log_exists']}")
    print(f"  total_rows_in_log:     {report['total_rows_in_log']}")
    print(f"  runtime_rows:          {report['runtime_rows']}")
    print(f"  projection_rows:       {report['projection_rows']}  (excluded from maturity)")
    print(f"  source:                {report['source']}")

    _section("TIMESTAMPS")
    print(f"  first_runtime_ts:      {report['first_runtime_ts'] or 'NONE'}")
    print(f"  latest_runtime_ts:     {report['latest_runtime_ts'] or 'NONE'}")
    age = report["latest_row_age_seconds"]
    print(f"  latest_row_age:        {f'{age:.0f}s' if age is not None else 'n/a'}")
    print(f"  freshness:             {report['freshness']}")

    ss = report["scan_stats"]
    _section("SCAN / RUN-ID STATS")
    print(f"  scan_id_count:         {ss['scan_id_count']}")
    print(f"  scan_number_range:     {ss['scan_number_min']} → {ss['scan_number_max']}")
    print(f"  run_id_count:          {ss['run_id_count']}")
    for rid, cnt in ss["rows_per_run_id"].items():
        print(f"    {rid}  →  {cnt} rows")

    _section("MATURITY")
    print(f"  maturity:              {report['maturity']}")
    print(f"  (thresholds: <50=IMMATURE | 50-100=EARLY | 100-300=DEVELOPING | 300+=MATURE)")

    _section("SAFETY VIOLATIONS")
    v = report["violations"]
    print(f"  all_clear:                      {v['all_clear']}")
    print(f"  execution_changed_violations:   {v['execution_changed_violations']}")
    print(f"  live_strategy_mutated_violations:{v['live_strategy_mutated_violations']}")
    print(f"  live_deployable_violations:     {v['live_deployable_violations']}")
    print(f"  shadow_only_false_violations:   {v['shadow_only_false_violations']}")
    print(f"  variant_live_deployable_viol:   {v['variant_live_deployable_violations']}")
    print(f"  total_violations:               {v['total_violations']}")
    if v["all_clear"]:
        print("  [CONFIRMED] No safety violations in any runtime row.")
    else:
        print("  [CRITICAL] Safety violations detected — see details above.")

    _section("AGGREGATE — CURRENT STREAM (baseline)")
    _variant_block("current", report["aggregate"].get("current", {}))

    _section("AGGREGATE — STACK 1 (quarantine-only filter)")
    _variant_block(
        "stack1_quarantine_only",
        report["aggregate"].get("stack1_quarantine_only", {}),
        report["comparison"].get("stack1_quarantine_only"),
    )

    _section("AGGREGATE — RESEARCH: WEAK REWARD/RISK FILTER")
    _variant_block(
        "research_variant_weak_rr",
        report["aggregate"].get("research_variant_weak_rr", {}),
        report["comparison"].get("research_variant_weak_rr"),
    )

    _section("AGGREGATE — RESEARCH: EXPENSIVE ENTRY FILTER (0.80-0.90)")
    _variant_block(
        "research_variant_expensive_entry",
        report["aggregate"].get("research_variant_expensive_entry", {}),
        report["comparison"].get("research_variant_expensive_entry"),
    )

    _section("AGGREGATE — AGGRESSIVE STACK (all three filters combined)")
    _variant_block(
        "aggressive_stack",
        report["aggregate"].get("aggressive_stack", {}),
        report["comparison"].get("aggressive_stack"),
    )

    _section("STARVATION RISK DISTRIBUTION")
    for name in VARIANT_NAMES:
        dist = report["starvation_distribution"].get(name, {})
        if dist:
            print(f"  {name}: {dist}")

    _section("SAFETY CLASSIFICATION DISTRIBUTION")
    for name in VARIANT_NAMES:
        dist = report["safety_class_distribution"].get(name, {})
        if dist:
            print(f"  {name}: {dist}")

    _section("LIVE DEPLOYABILITY CHECK")
    print(f"  live_deployable (report-level):   {report['live_deployable']}")
    print(f"  execution_changed (report-level): {report['execution_changed']}")
    print(f"  live_strategy_mutated:            {report['live_strategy_mutated']}")
    print("  [CONFIRMED] No variant is live-deployable.  All are SHADOW_ONLY.")

    _section("OUTCOME MATCHING")
    om = report["outcome_matching"]
    print(f"  attempted:                  {om['attempted']}")
    if om.get("shadow_start_utc"):
        print(f"  shadow_start_utc:           {om['shadow_start_utc']}")
    if om.get("total_trades_in_log") is not None:
        print(f"  total_trades_in_log:        {om['total_trades_in_log']}")
    if om.get("settled_after_shadow_start") is not None:
        print(f"  settled_after_shadow_start: {om['settled_after_shadow_start']}")
    if om.get("open_after_shadow_start") is not None:
        print(f"  open_after_shadow_start:    {om['open_after_shadow_start']}")
    print(f"  direct_scan_id_matching:    {om.get('direct_scan_id_matching', False)}")
    print(f"  outcome_verdict:            {om['verdict']}")

    _section("VERDICT")
    print(f"  verdict:         {report['verdict']}")
    print(f"  recommendations: {report['recommendations']}")

    print()
    print("=" * W)
    print(f"  live_deployable:       False   (no filter is deployable)")
    print(f"  execution_changed:     False")
    print(f"  live_strategy_mutated: False")
    print("=" * W)
    print(SENTINEL)


def main() -> None:
    print_report(build_report())


if __name__ == "__main__":
    main()
