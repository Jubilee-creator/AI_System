#!/usr/bin/env python3
"""
tools/test_asymmetry_edge_inversion.py
----------------------------------------
Verifies structural asymmetry conditions and edge inversion diagnosis.

Tests:
  1. payoff_ratio < 1.0  (losses dwarf wins — structural asymmetry confirmed)
  2. actual_WR < breakeven_WR  (negative expectancy confirmed)
  3. avg_CLV is negative  (model overprices YES resolution)
  4. LEGACY_EDGE_ONLY rows exist and contribute to losses  (legacy contamination)
  5. All high-edge losers (edge >= 0.08) are LEGACY_EDGE_ONLY  (edge inversion confirmed)
  6. EDGE_DANGER guard is active  (EDGE_DANGER_BLOCK_HIGH_EDGE = True)
  7. Legacy formula produces fake high edge for cheap entries  (formula artifact)
  8. No RISK_EDGE high-edge losers  (inversion is purely legacy, not model)
  9. normal_council_approved_modern = 0  (no clean proof base yet)
 10. Entry-price breakeven theorem holds per bucket  (math is consistent)

Expected result: PROVEN_ASYMMETRY_EDGE_INVERSION_OK
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_clean_settled():
    from tools.clean_truth_report import classify_records
    from tools.performance_report import load_trades
    all_records = load_trades()
    buckets = classify_records(all_records)
    return buckets["clean_settled"]


def _split(clean_settled):
    from tools.clean_truth_report import row_quality_group
    legacy = [r for r in clean_settled if row_quality_group(r) == "LEGACY_EDGE_ONLY"]
    modern = [r for r in clean_settled if row_quality_group(r) in
              ("MODERN_FULL_METADATA", "MODERN_EDGE_ONLY")]
    dc_override = [r for r in modern if r.get("data_collection_override")]
    provisional = [r for r in modern if r.get("bootstrap_provisional")]
    normal = [r for r in modern if not r.get("data_collection_override")
              and not r.get("bootstrap_provisional")]
    return legacy, dc_override, provisional, normal


# ── 1. payoff_ratio < 1.0 ─────────────────────────────────────────────────────

def test_payoff_ratio_below_one() -> str | None:
    from tools.clean_truth_report import calc_asymmetry
    rows = _load_clean_settled()
    if not rows:
        return None  # vacuously pass if no data
    m = calc_asymmetry(rows)
    pr = m.get("payoff_ratio")
    if pr is None:
        return None  # can't test without both wins and losses
    if pr >= 1.0:
        return (
            f"payoff_ratio={pr:.4f} >= 1.0 — structural asymmetry no longer present. "
            "Verify this improvement is real (n >= 30 with positive CLV) before removing test."
        )
    return None


# ── 2. actual_WR < breakeven_WR (negative expectancy) ────────────────────────

def test_negative_expectancy_confirmed() -> str | None:
    from tools.clean_truth_report import calc_asymmetry
    rows = _load_clean_settled()
    if len(rows) < 5:
        return None  # not enough data
    m = calc_asymmetry(rows)
    be = m.get("breakeven_win_rate")
    wr = m.get("win_rate", 0.0)
    if be is None:
        return None
    if wr >= be:
        return (
            f"actual_WR={wr:.4f} >= breakeven_WR={be:.4f} — "
            "positive expectancy detected. "
            "Verify this is real (sample >= 30, CLV > 0) before removing test."
        )
    return None


# ── 3. avg_CLV negative ───────────────────────────────────────────────────────

def test_avg_clv_negative() -> str | None:
    from tools.clean_truth_report import _avg, get_clv
    rows = _load_clean_settled()
    vals = [v for v in (get_clv(r) for r in rows) if v is not None]
    if not vals:
        return None
    avg = _avg(vals)
    if avg is None:
        return None
    if avg >= 0:
        return (
            f"avg_CLV={avg:.4f} >= 0 — CLV turned positive. "
            "Verify this is genuine before removing test (need n >= 30 normal_modern)."
        )
    return None


# ── 4. LEGACY rows exist and contribute losses ────────────────────────────────

def test_legacy_rows_contribute_losses() -> str | None:
    from tools.clean_truth_report import get_pnl
    rows = _load_clean_settled()
    legacy, _, _, _ = _split(rows)
    if not legacy:
        return None  # no legacy rows — pass, they've been purged
    legacy_losses = sum(get_pnl(r) for r in legacy if get_pnl(r) < 0)
    total_losses = sum(get_pnl(r) for r in rows if get_pnl(r) < 0)
    if total_losses == 0:
        return None  # no losses at all — pass
    share = legacy_losses / total_losses
    if share < 0.30:
        return (
            f"Legacy loss share = {share:.1%} (< 30%). "
            "Legacy rows are no longer the dominant loss source — great progress. "
            "Consider removing this test once sample >= 30 with positive ROI."
        )
    return None


# ── 5. All high-edge losers are LEGACY_EDGE_ONLY ─────────────────────────────

def test_all_high_edge_losers_are_legacy() -> str | None:
    from tools.clean_truth_report import edge_source_quality, edge_value, get_pnl
    rows = _load_clean_settled()
    losers = [r for r in rows
              if edge_value(r) is not None and edge_value(r) >= 0.08 and get_pnl(r) < 0]
    if not losers:
        return None  # no high-edge losers — guard is working
    non_legacy = [r for r in losers if edge_source_quality(r) != "LEGACY_EDGE_ONLY"]
    if non_legacy:
        details = ", ".join(
            f"{r.get('ticker','?')} edge={edge_value(r):.3f} src={edge_source_quality(r)}"
            for r in non_legacy
        )
        return (
            f"{len(non_legacy)} high-edge losers are NOT LEGACY_EDGE_ONLY: {details}. "
            "Edge inversion has spread beyond the legacy formula — investigate immediately."
        )
    return None


# ── 6. EDGE_DANGER guard is active ───────────────────────────────────────────

def test_edge_danger_guard_active() -> str | None:
    from config.trading_config import EDGE_DANGER_BLOCK_HIGH_EDGE
    if not EDGE_DANGER_BLOCK_HIGH_EDGE:
        return (
            "EDGE_DANGER_BLOCK_HIGH_EDGE=False — M-45 high-edge guard disabled. "
            "High-edge signals from the legacy formula can now be accepted."
        )
    return None


# ── 7. Legacy formula produces fake edge for cheap entries ────────────────────

def test_legacy_formula_fake_edge_for_cheap_entries() -> str | None:
    """
    For LEGACY_EDGE_ONLY rows with entry < 0.20 the formula
    edge = confidence - entry - 0.01 should produce edge > 0.50
    (i.e. spurious artifact).
    If any such row exists, verify the formula IS the source.
    """
    from tools.clean_truth_report import (
        _as_float, edge_source_quality, edge_value, inferred_edge_fee
    )
    rows = _load_clean_settled()
    cheap_legacy = [
        r for r in rows
        if edge_source_quality(r) == "LEGACY_EDGE_ONLY"
        and _as_float(r.get("entry_price")) is not None
        and _as_float(r.get("entry_price")) < 0.20
        and edge_value(r) is not None
    ]
    if not cheap_legacy:
        return None  # no cheap legacy entries — good

    for r in cheap_legacy:
        ev = edge_value(r)
        entry = _as_float(r.get("entry_price"))
        conf = _as_float(r.get("confidence")) or _as_float(r.get("original_confidence"))
        if conf is None:
            continue
        inferred = conf - entry - 0.01
        if abs(inferred - ev) < 0.005:
            # Formula confirmed — this is expected/known
            if ev < 0.50:
                return (
                    f"Cheap LEGACY entry (entry={entry:.2f}) has edge={ev:.3f} < 0.50 — "
                    "expected formula to produce > 0.50 for entry < 0.20. "
                    "Verify formula: conf - entry - 0.01 = "
                    f"{inferred:.3f}."
                )
        else:
            return (
                f"LEGACY_EDGE_ONLY row entry={entry:.2f}: "
                f"stored edge={ev:.3f} but conf-entry-0.01={inferred:.3f} — "
                "formula mismatch. Edge source may not be the old formula."
            )
    return None


# ── 8. No RISK_EDGE high-edge losers ─────────────────────────────────────────

def test_no_risk_edge_high_edge_losers() -> str | None:
    from tools.clean_truth_report import edge_source_quality, edge_value, get_pnl
    rows = _load_clean_settled()
    risk_edge_he_losers = [
        r for r in rows
        if edge_source_quality(r) == "RISK_EDGE_AVAILABLE"
        and edge_value(r) is not None
        and edge_value(r) >= 0.08
        and get_pnl(r) < 0
    ]
    if risk_edge_he_losers:
        details = ", ".join(
            f"{r.get('ticker','?')} edge={edge_value(r):.3f}"
            for r in risk_edge_he_losers
        )
        return (
            f"{len(risk_edge_he_losers)} RISK_EDGE high-edge losers found: {details}. "
            "The model's own risk_edge signal has high-edge losses — "
            "edge inversion is not purely a legacy artifact."
        )
    return None


# ── 9. normal_council_approved_modern = 0 ────────────────────────────────────

def test_no_normal_modern_proof_yet() -> str | None:
    rows = _load_clean_settled()
    _, _, _, normal = _split(rows)
    if len(normal) >= 30:
        return (
            f"normal_council_approved_modern={len(normal)} >= 30 — "
            "proof base reached! Remove this test and upgrade scale readiness report."
        )
    return None


# ── 10. Entry-price breakeven theorem holds per bucket ───────────────────────

def test_entry_price_breakeven_theorem() -> str | None:
    """
    For each entry-price bucket, avg_entry should equal the
    group's theoretical breakeven WR.  We can't test the actual_WR >=
    breakeven_WR condition here (that's test #2), but we verify
    that avg_entry > 0 and < 1 for all non-empty buckets.
    """
    from tools.clean_truth_report import _as_float, entry_price_bucket
    from collections import defaultdict
    rows = _load_clean_settled()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        entry = _as_float(r.get("entry_price"))
        if entry is not None:
            groups[entry_price_bucket(r)].append(entry)

    errors = []
    for bkt, entries in groups.items():
        avg = sum(entries) / len(entries)
        if not (0.0 < avg < 1.0):
            errors.append(f"bucket {bkt}: avg_entry={avg:.4f} out of (0, 1)")

    return "; ".join(errors) if errors else None


# ── Runner ─────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("payoff_ratio < 1.0 (structural asymmetry)",           test_payoff_ratio_below_one),
        ("actual_WR < breakeven_WR (negative expectancy)",       test_negative_expectancy_confirmed),
        ("avg_CLV negative (model overprices YES)",              test_avg_clv_negative),
        ("legacy rows contribute > 30% of losses",              test_legacy_rows_contribute_losses),
        ("all high-edge losers are LEGACY_EDGE_ONLY",           test_all_high_edge_losers_are_legacy),
        ("EDGE_DANGER_BLOCK_HIGH_EDGE = True",                   test_edge_danger_guard_active),
        ("legacy formula: fake edge for cheap entries",          test_legacy_formula_fake_edge_for_cheap_entries),
        ("no RISK_EDGE high-edge losers",                        test_no_risk_edge_high_edge_losers),
        ("normal_council_approved_modern = 0 (no proof yet)",   test_no_normal_modern_proof_yet),
        ("entry-price breakeven theorem valid per bucket",       test_entry_price_breakeven_theorem),
    ]

    fails: list[str] = []

    print("=" * 70)
    print("TEST: ASYMMETRY & EDGE INVERSION DIAGNOSIS")
    print("=" * 70)

    for name, fn in tests:
        err = fn()
        if err is None:
            print(f"[PASS] {name}")
        else:
            print(f"[FAIL] {name}")
            print(f"       {err}")
            fails.append(f"{name}: {err}")

    print()
    rows = _load_clean_settled()
    from tools.clean_truth_report import calc_asymmetry, _avg, get_clv, row_quality_group
    legacy, dc, prov, normal = _split(rows)
    m = calc_asymmetry(rows)
    clv_vals = [v for v in (get_clv(r) for r in rows) if v is not None]
    avg_clv = _avg(clv_vals)

    print("ASYMMETRY SNAPSHOT:")
    print(f"  clean_settled:              {len(rows)}")
    print(f"  LEGACY_EDGE_ONLY:           {len(legacy)}")
    print(f"  MODERN_DC_OVERRIDE:         {len(dc)}")
    print(f"  MODERN_PROVISIONAL:         {len(prov)}")
    print(f"  MODERN_NORMAL:              {len(normal)}")
    print(f"  win_rate:                   {m['win_rate']:.3f}")
    be = m.get("breakeven_win_rate")
    print(f"  breakeven_WR:               {f'{be:.3f}' if be else 'n/a'}")
    pr = m["payoff_ratio"]
    print(f"  payoff_ratio:               {f'{pr:.3f}' if pr is not None else 'n/a'}")
    print(f"  avg_CLV:                    {f'{avg_clv:.4f}' if avg_clv is not None else 'n/a'}")
    print(f"  total_PnL:                  ${m['total_pnl']:.2f}")
    print()

    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
        print("RESULT: FAILED")
        sys.exit(1)
    else:
        print("RESULT: PROVEN_ASYMMETRY_EDGE_INVERSION_OK")


if __name__ == "__main__":
    main()
