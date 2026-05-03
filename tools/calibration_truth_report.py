#!/usr/bin/env python3
"""
tools/calibration_truth_report.py
---------------------------------
Read-only calibration truth report.

Measures whether side-aware model confidence lines up with realized outcomes.
This report does not change trading behavior, thresholds, proof gates, risk
logic, logs, or runtime state.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.clean_truth_report import _as_float, row_quality_group
from tools.performance_report import (
    build_terminal_key_sets,
    classify_settled_records,
    get_clv,
    get_pnl,
    get_size,
    is_side_coverage_record,
    load_trades,
)


BUCKET_ORDER = [
    "<0.50",
    "0.50-0.59",
    "0.60-0.69",
    "0.70-0.79",
    "0.80-0.89",
    ">=0.90",
    "unknown",
]

PROOF_MIN_N = 30
ECE_ACCEPTABLE_MAX = 0.10


def _avg(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _fmt_num(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}"


def _fmt_ratio(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}%"


def _action(rec: dict) -> str:
    for key in ("executed_action", "intended_action", "scanner_action", "action"):
        value = rec.get(key)
        if value:
            return str(value).upper()
    return "UNKNOWN"


def side_probability(rec: dict) -> Optional[float]:
    """
    Return the predicted probability of the executed/intended side winning.

    The paper-trading pipeline stores confidence/model_probability as side
    confidence. For BET_NO rows this is NO-side probability, not YES
    probability. Do not invert it here.
    """
    for key in ("model_probability", "confidence", "original_confidence"):
        value = _as_float(rec.get(key))
        if value is not None:
            return value
    return None


def realized_outcome(rec: dict) -> Optional[float]:
    pnl = get_pnl(rec)
    if pnl > 0:
        return 1.0
    if pnl < 0:
        return 0.0
    return None


def probability_bucket_from_value(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 0.50:
        return "<0.50"
    if value < 0.60:
        return "0.50-0.59"
    if value < 0.70:
        return "0.60-0.69"
    if value < 0.80:
        return "0.70-0.79"
    if value < 0.90:
        return "0.80-0.89"
    return ">=0.90"


def probability_bucket(rec: dict) -> str:
    return probability_bucket_from_value(side_probability(rec))


def side_entry_price(rec: dict) -> Optional[float]:
    """
    Return the entry price paid for the executed side.

    Prefer entry_price because PaperTrader records the actual side price there.
    Fall back to side-specific price fields only when entry_price is absent.
    """
    entry = _as_float(rec.get("entry_price"))
    if entry is not None:
        return entry

    action = _action(rec)
    if action == "BET_NO":
        for key in ("price_no", "no_ask", "no_mid"):
            value = _as_float(rec.get(key))
            if value is not None:
                return value
    if action == "BET_YES":
        for key in ("price_yes", "yes_ask", "yes_mid"):
            value = _as_float(rec.get(key))
            if value is not None:
                return value
    return None


def sample_flag(n: int) -> str:
    if n == 0:
        return "NO_DATA"
    if n < 5:
        return "TOO_TINY_TO_INTERPRET"
    if n < 15:
        return "SAMPLE_TOO_SMALL"
    if n < 30:
        return "WATCHLIST_ONLY"
    return "DIAGNOSTICALLY_USABLE"


def classify_rows(all_records: List[dict]) -> Dict[str, List[dict]]:
    settled_keys, forced_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, conflicted = classify_settled_records(
        all_records,
        settled_keys,
        forced_keys,
        void_keys,
    )

    modern_full = [
        r for r in clean_settled
        if row_quality_group(r) == "MODERN_FULL_METADATA"
    ]
    legacy = [
        r for r in clean_settled
        if row_quality_group(r) == "LEGACY_EDGE_ONLY"
    ]
    dc_override = [r for r in modern_full if r.get("data_collection_override")]
    provisional = [
        r for r in modern_full
        if r.get("bootstrap_provisional") and not r.get("data_collection_override")
    ]
    era_allow = [
        r for r in modern_full
        if r.get("bootstrap_era_council_allow")
        and not r.get("data_collection_override")
        and not r.get("bootstrap_provisional")
    ]
    normal_modern = [
        r for r in modern_full
        if not r.get("data_collection_override")
        and not r.get("bootstrap_provisional")
    ]
    side_coverage = [r for r in all_records if is_side_coverage_record(r)]

    return {
        "all_records": all_records,
        "clean_settled": clean_settled,
        "conflicted_settled": conflicted,
        "modern_full_metadata": modern_full,
        "legacy_edge_only": legacy,
        "data_collection_override": dc_override,
        "bootstrap_provisional": provisional,
        "bootstrap_era_allow": era_allow,
        "proof_eligible": normal_modern,
        "normal_modern": normal_modern,
        "side_coverage": side_coverage,
    }


def _profit_factor(wins: List[float], losses: List[float]) -> Optional[float]:
    gross_wins = sum(wins)
    gross_losses = sum(losses)
    if gross_wins > 0 and gross_losses < 0:
        return gross_wins / abs(gross_losses)
    return None


def metrics(rows: List[dict]) -> Dict[str, Any]:
    scored: List[Tuple[float, float]] = []
    for rec in rows:
        prob = side_probability(rec)
        outcome = realized_outcome(rec)
        if prob is None or outcome is None:
            continue
        scored.append((prob, outcome))

    n = len(scored)
    probs = [p for p, _outcome in scored]
    outcomes = [outcome for _p, outcome in scored]
    wins = [get_pnl(r) for r in rows if get_pnl(r) > 0]
    losses = [get_pnl(r) for r in rows if get_pnl(r) < 0]
    pushes = [r for r in rows if get_pnl(r) == 0]
    pnl = sum(wins) + sum(losses)
    wagered = sum(get_size(r) for r in rows)
    clv_values = [v for v in (get_clv(r) for r in rows) if v is not None]
    entry_values = [v for v in (side_entry_price(r) for r in rows) if v is not None]

    avg_pred = _avg(probs)
    actual_wr = sum(outcomes) / n if n else None
    gap = actual_wr - avg_pred if actual_wr is not None and avg_pred is not None else None
    brier = _avg([(p - y) ** 2 for p, y in scored])
    avg_entry = _avg(entry_values)
    payout_gap = actual_wr - avg_entry if actual_wr is not None and avg_entry is not None else None

    return {
        "rows": len(rows),
        "n": n,
        "pushes": len(pushes),
        "missing_probability": sum(1 for r in rows if side_probability(r) is None),
        "missing_entry": sum(1 for r in rows if side_entry_price(r) is None),
        "avg_pred": avg_pred,
        "actual_wr": actual_wr,
        "gap": gap,
        "brier": brier,
        "expected_wins": sum(probs),
        "actual_wins": sum(outcomes),
        "roi": pnl / wagered if wagered else None,
        "profit_factor": _profit_factor(wins, losses),
        "avg_clv": _avg(clv_values),
        "clv_n": len(clv_values),
        "avg_entry": avg_entry,
        "breakeven_wr": avg_entry,
        "payout_gap": payout_gap,
    }


def bucket_metrics(rows: List[dict]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for rec in rows:
        grouped[probability_bucket(rec)].append(rec)
    return {bucket: metrics(grouped.get(bucket, [])) for bucket in BUCKET_ORDER}


def expected_calibration_error(rows: List[dict]) -> Optional[float]:
    total_scored = metrics(rows)["n"]
    if not total_scored:
        return None
    ece = 0.0
    for bucket, m in bucket_metrics(rows).items():
        if bucket == "unknown" or not m["n"]:
            continue
        if m["gap"] is None:
            continue
        ece += (m["n"] / total_scored) * abs(m["gap"])
    return ece


def flags_for(m: Dict[str, Any]) -> List[str]:
    flags = [sample_flag(m["n"])]
    gap = m.get("gap")
    if gap is not None:
        if gap < -0.10:
            flags.append("OVERCONFIDENT")
        elif gap > 0.10:
            flags.append("UNDERCONFIDENT")
        elif m["n"] >= 15:
            flags.append("ACCEPTABLE")
        else:
            flags.append("INCONCLUSIVE")
    if m.get("roi") is not None and m["roi"] < 0:
        flags.append("NEGATIVE_ROI")
    if m.get("avg_clv") is not None and m["avg_clv"] < 0:
        flags.append("NEGATIVE_CLV")
    if m.get("payout_gap") is not None and m["payout_gap"] < 0:
        flags.append("NEGATIVE_EXPECTANCY")
    if m.get("missing_probability", 0) > 0:
        flags.append("MISSING_PROB")
    if m.get("missing_entry", 0) > 0:
        flags.append("MISSING_ENTRY")
    if m.get("pushes", 0) > 0:
        flags.append("PUSH_EXCLUDED")
    return list(dict.fromkeys(flags))


def _print_scope_summary(label: str, rows: List[dict]) -> None:
    m = metrics(rows)
    ece = expected_calibration_error(rows)
    print(
        f"  {label:<24s} rows={m['rows']:>3} scored={m['n']:>3} "
        f"avg_pred={_fmt_pct(m['avg_pred']):>7s} actual_WR={_fmt_pct(m['actual_wr']):>7s} "
        f"gap={_fmt_pct(m['gap']):>7s} Brier={_fmt_ratio(m['brier'], 4):>6s} "
        f"ECE={_fmt_ratio(ece, 4):>6s} ExpW={m['expected_wins']:>5.2f} "
        f"ActW={m['actual_wins']:>5.2f} ROI={_fmt_pct(m['roi']):>8s} "
        f"PF={_fmt_ratio(m['profit_factor']):>5s} CLV={_fmt_num(m['avg_clv']):>8s} "
        f"[{', '.join(flags_for(m))}]"
    )


def _print_bucket_table(title: str, rows: List[dict]) -> None:
    print()
    print(title)
    print("-" * min(len(title), 100))
    print(
        "  bucket       n  avg_pred actual_WR      gap    Brier      ROI     PF"
        "      CLV clv_n avg_entry breakeven payout_gap  flags"
    )
    groups = bucket_metrics(rows)
    for bucket in BUCKET_ORDER:
        m = groups[bucket]
        if not m["rows"] and bucket != "unknown":
            continue
        if not m["rows"] and bucket == "unknown":
            continue
        print(
            f"  {bucket:<10s} {m['n']:>3} "
            f"{_fmt_pct(m['avg_pred']):>9s} {_fmt_pct(m['actual_wr']):>9s} "
            f"{_fmt_pct(m['gap']):>8s} {_fmt_ratio(m['brier'], 4):>8s} "
            f"{_fmt_pct(m['roi']):>8s} {_fmt_ratio(m['profit_factor']):>6s} "
            f"{_fmt_num(m['avg_clv']):>8s} {m['clv_n']:>5} "
            f"{_fmt_ratio(m['avg_entry'], 3):>9s} {_fmt_pct(m['breakeven_wr']):>9s} "
            f"{_fmt_pct(m['payout_gap']):>10s}  {', '.join(flags_for(m))}"
        )


def _side_split(rows: List[dict]) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for rec in rows:
        grouped[_action(rec)].append(rec)
    return grouped


def _collect_bucket_alerts(scopes: Dict[str, List[dict]]) -> Tuple[List[str], List[str]]:
    over: List[str] = []
    under: List[str] = []
    for scope_name, rows in scopes.items():
        for bucket, m in bucket_metrics(rows).items():
            if not m["n"] or m["gap"] is None:
                continue
            label = (
                f"{scope_name} {bucket}: n={m['n']} "
                f"avg_pred={_fmt_pct(m['avg_pred'])} actual_WR={_fmt_pct(m['actual_wr'])} "
                f"gap={_fmt_pct(m['gap'])}"
            )
            if m["gap"] < -0.10:
                over.append(label)
            elif m["gap"] > 0.10:
                under.append(label)
    return over, under


def _entry_diagnosis(rows: List[dict]) -> Tuple[int, int, Optional[float], Optional[float]]:
    scored = 0
    negative = 0
    gaps: List[float] = []
    entries: List[float] = []
    for rec in rows:
        outcome = realized_outcome(rec)
        entry = side_entry_price(rec)
        if outcome is None or entry is None:
            continue
        scored += 1
        gap = outcome - entry
        gaps.append(gap)
        entries.append(entry)
        if gap < 0:
            negative += 1
    return scored, negative, _avg(entries), _avg(gaps)


def _clv_probability_rows(rows: List[dict]) -> List[str]:
    lines: List[str] = []
    for bucket, m in bucket_metrics(rows).items():
        if not m["rows"]:
            continue
        lines.append(
            f"  {bucket:<10s} n={m['n']:>3} avg_pred={_fmt_pct(m['avg_pred']):>7s} "
            f"CLV={_fmt_num(m['avg_clv']):>8s} clv_n={m['clv_n']:>3} "
            f"ROI={_fmt_pct(m['roi']):>8s}"
        )
    return lines


def proof_safe_verdict(proof_rows: List[dict]) -> str:
    m = metrics(proof_rows)
    ece = expected_calibration_error(proof_rows)
    flags = flags_for(m)
    if m["n"] < 30:
        return "INCONCLUSIVE"
    passes = (
        m["roi"] is not None and m["roi"] > 0
        and m["avg_clv"] is not None and m["avg_clv"] > 0
        and m["profit_factor"] is not None and m["profit_factor"] > 1.10
        and ece is not None and ece <= ECE_ACCEPTABLE_MAX
    )
    if passes:
        return "CALIBRATION_PROVEN_GOOD"
    if (
        "OVERCONFIDENT" in flags
        or (m["avg_clv"] is not None and m["avg_clv"] < 0)
        or (m["roi"] is not None and m["roi"] < 0)
    ):
        return "CALIBRATION_BAD"
    return "CALIBRATION_WEAK"


def _print_data_scope(buckets: Dict[str, List[dict]]) -> None:
    print()
    print("1. DATA SCOPE")
    print("-" * 72)
    print(f"  all rows in paper log:              {len(buckets['all_records'])}")
    print(f"  clean_settled:                      {len(buckets['clean_settled'])}")
    print(f"  modern_full_metadata:               {len(buckets['modern_full_metadata'])}")
    print(f"  proof_eligible:                     {len(buckets['proof_eligible'])}")
    print(f"  normal_modern:                      {len(buckets['normal_modern'])}")
    print()
    print("  excluded / quarantined:")
    print(f"    legacy_edge_only visible:         {len(buckets['legacy_edge_only'])}")
    print(f"    data_collection_override excl:    {len(buckets['data_collection_override'])}")
    print(f"    bootstrap_provisional excl:       {len(buckets['bootstrap_provisional'])}")
    print(f"    bootstrap_era_allow counts:       {len(buckets['bootstrap_era_allow'])}")
    print(f"    side_coverage_test excl:          {len(buckets['side_coverage'])}")
    print(f"    conflicted_settled excl:          {len(buckets['conflicted_settled'])}")
    print()
    print("  Note: probability is interpreted as executed-side confidence.")
    print("  BET_NO confidence is treated as probability NO wins, not YES probability.")


def main() -> None:
    all_records = load_trades()
    buckets = classify_rows(all_records)
    clean = buckets["clean_settled"]
    modern = buckets["modern_full_metadata"]
    proof = buckets["proof_eligible"]
    normal = buckets["normal_modern"]

    print("=" * 100)
    print("CALIBRATION TRUTH REPORT")
    print("=" * 100)
    print("Read-only quant diagnostic. This does not change strategy, risk, proof gates, scale, or real money.")

    _print_data_scope(buckets)

    print()
    print("2. OVERALL CALIBRATION SUMMARY")
    print("-" * 100)
    _print_scope_summary("clean_settled", clean)
    _print_scope_summary("modern_full_metadata", modern)
    _print_scope_summary("proof_eligible", proof)
    _print_scope_summary("normal_modern", normal)

    _print_bucket_table("3. CALIBRATION BY CONFIDENCE BUCKET - CLEAN_SETTLED", clean)
    _print_bucket_table("4. MODERN FULL-METADATA ONLY", modern)

    print()
    print("5. NORMAL_MODERN ONLY")
    print("-" * 72)
    normal_m = metrics(normal)
    if normal_m["n"] < 30:
        print(f"  STRICT SAMPLE WARNING: {sample_flag(normal_m['n'])} ({normal_m['n']}/30 scored rows).")
        print("  Normal_modern calibration is not proof until n>=30 plus ROI, CLV, and PF pass.")
    _print_bucket_table("NORMAL_MODERN CALIBRATION BUCKETS", normal)

    print()
    print("6. SIDE SPLIT")
    print("-" * 72)
    sides = _side_split(clean)
    for side in ("BET_YES", "BET_NO", "UNKNOWN"):
        if side in sides:
            _print_scope_summary(side, sides[side])
    for side in ("BET_YES", "BET_NO"):
        if side in sides:
            _print_bucket_table(f"SIDE SPLIT - {side}", sides[side])

    print()
    print("7. OVERCONFIDENCE / UNDERCONFIDENCE FLAGS")
    print("-" * 72)
    alert_scopes = {
        "clean": clean,
        "modern": modern,
        "proof": proof,
        "normal": normal,
    }
    over, under = _collect_bucket_alerts(alert_scopes)
    if over:
        print("  OVERCONFIDENT buckets:")
        for item in over[:20]:
            print(f"    - {item}")
    else:
        print("  OVERCONFIDENT buckets: none detected")
    if under:
        print("  UNDERCONFIDENT buckets:")
        for item in under[:20]:
            print(f"    - {item}")
    else:
        print("  UNDERCONFIDENT buckets: none detected")

    print()
    print("8. ENTRY PRICE / BREAKEVEN DIAGNOSIS")
    print("-" * 72)
    for label, rows in (("clean_settled", clean), ("modern_full", modern), ("proof_eligible", proof)):
        scored, negative, avg_entry, avg_gap = _entry_diagnosis(rows)
        print(
            f"  {label:<18s} scored={scored:>3} negative_gap={negative:>3} "
            f"avg_entry={_fmt_ratio(avg_entry, 3):>6s} avg_outcome_minus_entry={_fmt_num(avg_gap):>8s}"
        )
    print("  Breakeven_WR uses entry price approximation. Fee-adjusted breakeven is not implemented.")

    print()
    print("9. CLV + PROBABILITY INTERACTION")
    print("-" * 72)
    for line in _clv_probability_rows(modern):
        print(line)
    if not modern:
        print("  No modern_full_metadata rows available.")

    print()
    print("10. PROOF-SAFE VERDICT")
    print("-" * 72)
    verdict = proof_safe_verdict(proof)
    proof_m = metrics(proof)
    proof_ece = expected_calibration_error(proof)
    print(f"  VERDICT: {verdict}")
    print(
        f"  proof_eligible scored={proof_m['n']} ROI={_fmt_pct(proof_m['roi'])} "
        f"CLV={_fmt_num(proof_m['avg_clv'])} PF={_fmt_ratio(proof_m['profit_factor'])} "
        f"ECE={_fmt_ratio(proof_ece, 4)}"
    )
    print("  Calibration proof still requires normal_modern>=30, ROI>0, CLV>0, PF>1.10.")
    print("  ECE alone never implies profitability.")

    print()
    print("11. BRUTALLY HONEST NEXT RECOMMENDATION")
    print("-" * 72)
    if proof_m["n"] < 30:
        print("  Accumulate more normal_modern settled rows before trusting calibration.")
    elif verdict == "CALIBRATION_BAD":
        print("  Investigate probability generation before changing execution. The report shows calibration/performance weakness.")
    elif verdict == "CALIBRATION_PROVEN_GOOD":
        print("  Calibration is supportive, but it still does not unlock real money or scale without explicit approval.")
    else:
        print("  Treat calibration as watchlist evidence only. Do not change thresholds from this report alone.")
    print("  RESULT: CALIBRATION_TRUTH_REPORT_OK")


if __name__ == "__main__":
    main()
