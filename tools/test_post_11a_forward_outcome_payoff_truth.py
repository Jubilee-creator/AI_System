#!/usr/bin/env python3
"""
Phase 11B — Tests for report_post_11a_forward_outcome_payoff_truth.py

READ-ONLY: does not modify any logs, config, thresholds, or strategy.
Sentinel: POST_11A_FORWARD_OUTCOME_PAYOFF_TRUTH_OK
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import report_post_11a_forward_outcome_payoff_truth as report_mod
from report_post_11a_forward_outcome_payoff_truth import (
    EXPENSIVE_ENTRY_THRESHOLD,
    MIN_PROOF_SETTLED,
    OVERCONFIDENT_GAP,
    PHASE_11A_CUTOFF_UTC,
    SENTINEL,
    TOXIC_ZONE_HI,
    TOXIC_ZONE_LO,
    WEAK_RR_THRESHOLD,
    answer_questions,
    breakeven_win_rate,
    build_report,
    classify_trade,
    compute_aggregate,
    is_expensive_entry,
    is_kxeth,
    is_toxic_zone,
    is_weak_rr,
    load_post_cutoff_trades,
    reward_risk,
    roi,
    profit_factor,
)

try:
    from config.trading_config import (
        EDGE_DANGER_HIGH_EDGE_MIN,
        MIN_CONFIDENCE,
        MIN_EDGE,
    )
except ImportError:
    MIN_EDGE = 0.03
    MIN_CONFIDENCE = 0.65
    EDGE_DANGER_HIGH_EDGE_MIN = 0.08


PASS = "PASS"
FAIL = "FAIL"
_results: list[tuple[str, str, str]] = []


def _test(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    _results.append((name, status, detail))
    marker = "+" if ok else "x"
    print(f"  [{marker}] {name}" + (f": {detail}" if detail else ""))


def _settled_row(
    ticker: str = "KXSOL15M-26MAY2615-T20",
    timestamp_utc: str = "2026-05-16T00:00:00+00:00",
    entry_price: float = 0.85,
    confidence: float = 0.90,
    edge: float = 0.04,
    pnl: float = -4.20,
    council_decision: str = "ALLOW",
    action: str = "BET_YES",
) -> dict:
    return {
        "ticker": ticker,
        "timestamp_utc": timestamp_utc,
        "status": "SETTLED",
        "entry_price": entry_price,
        "confidence": confidence,
        "edge": edge,
        "pnl": pnl,
        "council_decision": council_decision,
        "action": action,
        "bootstrap_era_council_allow": True,
        "bootstrap_provisional": False,
        "data_collection_override": False,
    }


def _write_jsonl(rows: list[dict]) -> Path:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for r in rows:
        tmp.write(json.dumps(r) + "\n")
    tmp.close()
    return Path(tmp.name)


# ─────────────────────────────────────────────────────────────────────────────
# T01: Post-cutoff filtering excludes old rows
# ─────────────────────────────────────────────────────────────────────────────
def T01_post_cutoff_filtering_excludes_old_rows() -> None:
    cutoff = "2026-05-15T13:46:29+00:00"
    old_row = _settled_row(ticker="KXSOL15M-OLD", timestamp_utc="2026-05-10T00:00:00+00:00")
    new_row = _settled_row(ticker="KXSOL15M-26MAY2615-T20", timestamp_utc="2026-05-16T00:00:00+00:00")
    tmp = _write_jsonl([old_row, new_row])
    try:
        with patch.object(report_mod, "TRADES_LOG", tmp):
            result = load_post_cutoff_trades(cutoff)
        ok = len(result) == 1 and result[0]["ticker"] == "KXSOL15M-26MAY2615-T20"
        _test("T01_post_cutoff_filtering_excludes_old_rows", ok,
              f"got {len(result)} rows (expected 1 post-cutoff)")
    finally:
        tmp.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# T02: Zero post-11A opens does not crash
# ─────────────────────────────────────────────────────────────────────────────
def T02_zero_opens_does_not_crash() -> None:
    try:
        agg = compute_aggregate([])
        answers = answer_questions(agg, [], None, None)
        ok = (
            agg["count"] == 0
            and agg["win_rate"] is None
            and isinstance(answers, dict)
            and "recommendation" in answers
        )
        _test("T02_zero_opens_does_not_crash", ok,
              f"count={agg['count']}, win_rate={agg['win_rate']}")
    except Exception as exc:
        _test("T02_zero_opens_does_not_crash", False, f"raised: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# T03: Three opened losers classify correctly
# ─────────────────────────────────────────────────────────────────────────────
def T03_three_losers_classify_correctly() -> None:
    loser_params = [
        (0.84, 0.893, 0.043, -4.20),
        (0.85, 0.899, 0.040, -4.25),
        (0.84, 0.893, 0.043, -4.20),
    ]
    results = [
        classify_trade(
            entry_price=ep,
            confidence=conf,
            pre_council_edge=pce,
            pnl=pnl,
            actual_wr=0.0,
        )
        for ep, conf, pce, pnl in loser_params
    ]
    ok = all(
        c["loser"] and not c["winner"] and c["expensive_entry_80"] and c["weak_reward_risk"]
        for c in results
    )
    sample = results[0]
    _test("T03_three_losers_classify_correctly", ok,
          f"loser={sample['loser']}, exp_entry={sample['expensive_entry_80']}, "
          f"weak_rr={sample['weak_reward_risk']}, rr={sample['reward_risk']}")


# ─────────────────────────────────────────────────────────────────────────────
# T04: Expensive entry >=0.80 is flagged
# ─────────────────────────────────────────────────────────────────────────────
def T04_expensive_entry_flagged() -> None:
    cases = [
        (0.80, True),
        (0.85, True),
        (0.90, True),
        (0.79, False),
        (0.7999, False),
    ]
    for price, expected in cases:
        got = is_expensive_entry(price)
        if got != expected:
            _test("T04_expensive_entry_flagged", False,
                  f"price={price} expected={expected} got={got}")
            return
    clf = classify_trade(0.82, 0.90, 0.04, -4.0, 0.0)
    _test("T04_expensive_entry_flagged",
          clf["expensive_entry_80"] is True,
          f"boundary checks ok; classify(0.82) expensive_entry_80={clf['expensive_entry_80']}")


# ─────────────────────────────────────────────────────────────────────────────
# T05: 0.80-0.90 toxic entry zone is flagged correctly
# ─────────────────────────────────────────────────────────────────────────────
def T05_toxic_zone_flagged() -> None:
    cases = [
        (0.80, True),
        (0.85, True),
        (0.8999, True),
        (0.90, False),    # hi boundary exclusive
        (0.79, False),
        (0.50, False),
    ]
    for price, expected in cases:
        got = is_toxic_zone(price)
        if got != expected:
            _test("T05_toxic_zone_flagged", False,
                  f"price={price} expected={expected} got={got}")
            return
    clf = classify_trade(0.85, 0.90, 0.04, -4.0, 0.0)
    _test("T05_toxic_zone_flagged",
          clf["toxic_zone_80_90"] is True,
          f"boundary checks ok; classify(0.85) toxic_zone_80_90={clf['toxic_zone_80_90']}")


# ─────────────────────────────────────────────────────────────────────────────
# T06: Weak reward/risk is flagged
# ─────────────────────────────────────────────────────────────────────────────
def T06_weak_rr_flagged() -> None:
    # reward_risk(0.80) = 0.20/0.80 = 0.25 < WEAK_RR_THRESHOLD(0.30) → weak
    # reward_risk(0.85) = 0.15/0.85 ≈ 0.176 < 0.30 → weak
    # reward_risk(0.70) = 0.30/0.70 ≈ 0.429 > 0.30 → not weak
    # reward_risk(0.50) = 0.50/0.50 = 1.0 → not weak
    cases = [
        (0.80, True),
        (0.85, True),
        (0.70, False),
        (0.50, False),
    ]
    for price, expected in cases:
        got = is_weak_rr(price)
        rr_val = reward_risk(price)
        if got != expected:
            _test("T06_weak_rr_flagged", False,
                  f"price={price} rr={rr_val:.4f} expected={expected} got={got}")
            return
    clf = classify_trade(0.85, 0.90, 0.04, -4.0, 0.0)
    _test("T06_weak_rr_flagged",
          clf["weak_reward_risk"] is True,
          f"boundary checks ok; classify(0.85) weak_reward_risk={clf['weak_reward_risk']}, "
          f"rr={clf['reward_risk']:.4f}")


# ─────────────────────────────────────────────────────────────────────────────
# T07: Overconfidence / breakeven gap calculated correctly
# ─────────────────────────────────────────────────────────────────────────────
def T07_overconfidence_and_breakeven() -> None:
    # breakeven_win_rate equals entry price for BET_YES
    bk = breakeven_win_rate(0.84)
    bk_ok = abs(bk - 0.84) < 0.0001

    # Loser: actual_wr=0.0, conf=0.90 → conf_gap=0.90 > OVERCONFIDENT_GAP(0.10) → overconfident
    clf_loss = classify_trade(0.84, 0.90, 0.043, -4.20, 0.0)
    loss_oc = clf_loss["overconfident"] is True
    gap_ok = abs(clf_loss["confidence_gap"] - 0.90) < 0.0001

    # Winner: actual_wr=1.0, conf=0.90 → conf_gap=0.90-1.0=-0.10 → not overconfident
    clf_win = classify_trade(0.84, 0.90, 0.043, 0.16, 1.0)
    win_not_oc = clf_win["overconfident"] is False

    ok = bk_ok and loss_oc and gap_ok and win_not_oc
    _test("T07_overconfidence_and_breakeven", ok,
          f"bk(0.84)={bk:.4f}, loss_conf_gap={clf_loss['confidence_gap']:.4f}, "
          f"loss_oc={clf_loss['overconfident']}, win_oc={clf_win['overconfident']}")


# ─────────────────────────────────────────────────────────────────────────────
# T08: KXETH rows are quarantined/excluded appropriately
# ─────────────────────────────────────────────────────────────────────────────
def T08_kxeth_quarantine() -> None:
    cases = [
        ("KXETHD-26APR2817-T2259.99", True),
        ("KXETH-SOMETHING", True),
        ("kxethd-lower", True),       # case-insensitive
        ("KXSOL15M-26MAY2615-T20", False),
        ("KXBTCD-26MAY2615-T50000", False),
        ("BTCUSD", False),
        (None, False),
    ]
    for ticker, expected in cases:
        got = is_kxeth(ticker)
        if got != expected:
            _test("T08_kxeth_quarantine", False,
                  f"ticker={ticker!r} expected={expected} got={got}")
            return

    # load_post_cutoff_trades must drop KXETH rows
    cutoff = "2026-05-15T00:00:00+00:00"
    kxeth_row = _settled_row(ticker="KXETHD-26APR2817-T2259.99",
                              timestamp_utc="2026-05-16T00:00:00+00:00")
    kxsol_row = _settled_row(ticker="KXSOL15M-26MAY2615-T20",
                              timestamp_utc="2026-05-16T00:00:00+00:00")
    tmp = _write_jsonl([kxeth_row, kxsol_row])
    try:
        with patch.object(report_mod, "TRADES_LOG", tmp):
            result = load_post_cutoff_trades(cutoff)
        kxeth_excluded = all(not is_kxeth(r["ticker"]) for r in result)
        kxsol_present = any(r["ticker"] == "KXSOL15M-26MAY2615-T20" for r in result)
        ok = len(result) == 1 and kxeth_excluded and kxsol_present
        _test("T08_kxeth_quarantine", ok,
              f"is_kxeth checks ok; filtered_count={len(result)}, "
              f"kxeth_excluded={kxeth_excluded}, kxsol_present={kxsol_present}")
    finally:
        tmp.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# T09: Report is read-only — does not mutate logs
# ─────────────────────────────────────────────────────────────────────────────
def T09_report_is_read_only() -> None:
    log_paths = [
        ROOT / "logs" / "paper_trades.jsonl",
        ROOT / "logs" / "execution_funnel.jsonl",
    ]
    before: dict[Path, tuple[int, float]] = {}
    for p in log_paths:
        if p.exists():
            st = p.stat()
            before[p] = (st.st_size, st.st_mtime)

    try:
        build_report()
    except Exception as exc:
        _test("T09_report_is_read_only", False, f"build_report() raised: {exc}")
        return

    for p, (sz_before, mt_before) in before.items():
        if p.exists():
            st = p.stat()
            if st.st_size != sz_before or st.st_mtime != mt_before:
                _test("T09_report_is_read_only", False,
                      f"{p.name} mutated: size {sz_before}->{st.st_size}")
                return
    _test("T09_report_is_read_only", True, "no log files mutated by build_report()")


# ─────────────────────────────────────────────────────────────────────────────
# T10: No thresholds/safety constants are changed
# ─────────────────────────────────────────────────────────────────────────────
def T10_safety_constants_unchanged() -> None:
    checks = [
        ("MIN_EDGE", MIN_EDGE, 0.03),
        ("MIN_CONFIDENCE", MIN_CONFIDENCE, 0.65),
        ("EDGE_DANGER_HIGH_EDGE_MIN", EDGE_DANGER_HIGH_EDGE_MIN, 0.08),
        ("EXPENSIVE_ENTRY_THRESHOLD", EXPENSIVE_ENTRY_THRESHOLD, 0.80),
        ("TOXIC_ZONE_LO", TOXIC_ZONE_LO, 0.80),
        ("TOXIC_ZONE_HI", TOXIC_ZONE_HI, 0.90),
        ("WEAK_RR_THRESHOLD", WEAK_RR_THRESHOLD, 0.30),
        ("OVERCONFIDENT_GAP", OVERCONFIDENT_GAP, 0.10),
        ("MIN_PROOF_SETTLED", MIN_PROOF_SETTLED, 30),
    ]
    for name, actual, expected in checks:
        if abs(float(actual) - float(expected)) > 1e-9:
            _test("T10_safety_constants_unchanged", False,
                  f"{name}={actual} != expected {expected}")
            return
    _test("T10_safety_constants_unchanged", True,
          f"all {len(checks)} constants at expected values")


# ─────────────────────────────────────────────────────────────────────────────
# T11: Shadow mismatch handled safely
# ─────────────────────────────────────────────────────────────────────────────
def T11_shadow_mismatch_safe() -> None:
    agg = compute_aggregate([])
    try:
        a1 = answer_questions(agg, [], None, None)
        a2 = answer_questions(agg, [], None, {})
        a3 = answer_questions(agg, [], {"current_avg_entry": None,
                                         "expensive_entry_removed_count": None}, None)
        ok = isinstance(a1, dict) and isinstance(a2, dict) and isinstance(a3, dict)
        _test("T11_shadow_mismatch_safe", ok,
              "None/empty/partial shadows all handled without crash")
    except Exception as exc:
        _test("T11_shadow_mismatch_safe", False, f"raised: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# T12: Output includes sentinel
# ─────────────────────────────────────────────────────────────────────────────
def T12_sentinel_present() -> None:
    expected = "POST_11A_FORWARD_OUTCOME_PAYOFF_TRUTH_OK"
    _test("T12_sentinel_present",
          SENTINEL == expected,
          f"SENTINEL='{SENTINEL}'")


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    print()
    print("=" * 72)
    print("  Phase 11B — POST-11A Forward Outcome Payoff Truth — Test Suite")
    print("=" * 72)

    T01_post_cutoff_filtering_excludes_old_rows()
    T02_zero_opens_does_not_crash()
    T03_three_losers_classify_correctly()
    T04_expensive_entry_flagged()
    T05_toxic_zone_flagged()
    T06_weak_rr_flagged()
    T07_overconfidence_and_breakeven()
    T08_kxeth_quarantine()
    T09_report_is_read_only()
    T10_safety_constants_unchanged()
    T11_shadow_mismatch_safe()
    T12_sentinel_present()

    print()
    print("-" * 72)
    passed = sum(1 for _, s, _ in _results if s == PASS)
    failed = sum(1 for _, s, _ in _results if s == FAIL)
    total = len(_results)
    print(f"  Results: {passed}/{total} passed, {failed} failed")
    print("-" * 72)

    if failed == 0:
        print(f"  {SENTINEL}")
        print()
        return 0

    print()
    print("  FAILURES:")
    for name, status, detail in _results:
        if status == FAIL:
            print(f"    [x] {name}: {detail}")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
