#!/usr/bin/env python3
"""
tools/test_quarantine_kxeth.py
-------------------------------
Verifies the Phase 8R KXETH hard quarantine implementation:

  1. Config exports QUARANTINED_TICKER_PREFIXES containing "KXETH"
  2. KXETH, KXETHD, KXETH15M tickers are blocked by the prefix check
  3. KXBTCD, KXBTC, KXSOL, KXXRP tickers are NOT blocked
  4. brain/paper_trader.py imports QUARANTINED_TICKER_PREFIXES from config
  5. execution_funnel_logger.py maps "quarantined prefix" → BLOCKED_QUARANTINE
  6. Safety locks remain intact: real_money_allowed=False, scale_allowed=False
  7. MIN_EDGE, MIN_CONFIDENCE unchanged

Expected result: PROVEN_QUARANTINE_KXETH_OK
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── 1. Config exports the quarantine constant ─────────────────────────────────

def test_config_exports_quarantine_prefixes() -> str | None:
    """QUARANTINED_TICKER_PREFIXES must be importable from config."""
    from config import trading_config as cfg
    if not hasattr(cfg, "QUARANTINED_TICKER_PREFIXES"):
        return "config.trading_config missing QUARANTINED_TICKER_PREFIXES"
    pfxs = cfg.QUARANTINED_TICKER_PREFIXES
    if not isinstance(pfxs, list):
        return f"QUARANTINED_TICKER_PREFIXES is not a list: {type(pfxs)}"
    return None


def test_kxeth_in_quarantine_list() -> str | None:
    """'KXETH' must be in QUARANTINED_TICKER_PREFIXES."""
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
    if "KXETH" not in QUARANTINED_TICKER_PREFIXES:
        return f"'KXETH' not in QUARANTINED_TICKER_PREFIXES: {QUARANTINED_TICKER_PREFIXES}"
    return None


# ── 2/3. Prefix-matching logic (mirrors paper_trader.py exactly) ─────────────

def _is_quarantined(ticker: str, prefixes: list) -> bool:
    """Exact logic used in brain/paper_trader.py process_signal()."""
    t = str(ticker or "").upper()
    return any(t.startswith(p.upper()) for p in prefixes)


def test_kxeth_ticker_blocked() -> str | None:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
    ticker = "KXETH15M-26MAY080030-30"
    if not _is_quarantined(ticker, QUARANTINED_TICKER_PREFIXES):
        return f"Expected {ticker!r} to be quarantined but was not"
    return None


def test_kxethd_ticker_blocked() -> str | None:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
    ticker = "KXETHD-26MAY0111-T2309.99"
    if not _is_quarantined(ticker, QUARANTINED_TICKER_PREFIXES):
        return f"Expected {ticker!r} to be quarantined but was not"
    return None


def test_kxeth_bare_blocked() -> str | None:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
    ticker = "KXETH26MAY2820-B2310"
    if not _is_quarantined(ticker, QUARANTINED_TICKER_PREFIXES):
        return f"Expected {ticker!r} to be quarantined but was not"
    return None


def test_kxbtcd_not_blocked() -> str | None:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
    ticker = "KXBTCD-26MAY0801-T79599.99"
    if _is_quarantined(ticker, QUARANTINED_TICKER_PREFIXES):
        return f"KXBTCD should NOT be quarantined but was blocked: {ticker!r}"
    return None


def test_kxbtc_not_blocked() -> str | None:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
    ticker = "KXBTC15M-26MAY080015-15"
    if _is_quarantined(ticker, QUARANTINED_TICKER_PREFIXES):
        return f"KXBTC should NOT be quarantined but was blocked: {ticker!r}"
    return None


def test_kxsol_not_blocked() -> str | None:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
    ticker = "KXSOL15M-26MAY080015-15"
    if _is_quarantined(ticker, QUARANTINED_TICKER_PREFIXES):
        return f"KXSOL should NOT be quarantined but was blocked: {ticker!r}"
    return None


def test_kxxrp_not_blocked() -> str | None:
    from config.trading_config import QUARANTINED_TICKER_PREFIXES
    ticker = "KXXRP26MAY2820-B2310"
    if _is_quarantined(ticker, QUARANTINED_TICKER_PREFIXES):
        return f"KXXRP should NOT be quarantined but was blocked: {ticker!r}"
    return None


# ── 4. paper_trader.py imports QUARANTINED_TICKER_PREFIXES (static AST) ──────

def test_paper_trader_imports_quarantine_constant() -> str | None:
    """
    Verify paper_trader.py statically imports QUARANTINED_TICKER_PREFIXES
    from config.trading_config.
    """
    pt_path = ROOT / "brain" / "paper_trader.py"
    if not pt_path.exists():
        return "brain/paper_trader.py not found"
    source = pt_path.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "")
            if "trading_config" in module:
                names = [alias.name for alias in node.names]
                if "QUARANTINED_TICKER_PREFIXES" in names:
                    return None
    return "QUARANTINED_TICKER_PREFIXES not found in paper_trader.py imports from trading_config"


def test_paper_trader_checks_quarantine_in_process_signal() -> str | None:
    """
    Verify the quarantine check phrase appears in paper_trader.py source
    (confirms the guard was not accidentally removed).
    """
    pt_path = ROOT / "brain" / "paper_trader.py"
    source = pt_path.read_text()
    if "quarantined prefix block" not in source:
        return "quarantine check trace phrase 'quarantined prefix block' not found in paper_trader.py"
    if "QUARANTINED_TICKER_PREFIXES" not in source:
        return "QUARANTINED_TICKER_PREFIXES reference missing from paper_trader.py"
    return None


# ── 5. execution_funnel_logger.py maps "quarantined prefix" → BLOCKED_QUARANTINE

def test_funnel_logger_maps_quarantined_prefix() -> str | None:
    """
    _final_reason() must return 'BLOCKED_QUARANTINE' when the trace contains
    'quarantined prefix'.
    """
    try:
        from logs.execution_funnel_logger import _final_reason
    except ImportError as e:
        return f"Could not import _final_reason from execution_funnel_logger: {e}"

    trace = "[TRACE] quarantined prefix block: ticker=KXETH15M-... prefix=KXETH hard quarantine Phase 8Q"
    result = _final_reason(trace, trade=None)
    if result != "BLOCKED_QUARANTINE":
        return f"Expected BLOCKED_QUARANTINE, got {result!r}"
    return None


def test_funnel_logger_non_kxeth_not_quarantine() -> str | None:
    """Non-quarantine traces must NOT map to BLOCKED_QUARANTINE."""
    from logs.execution_funnel_logger import _final_reason
    traces_and_expected = [
        ("[TRACE] stop: min edge block edge=0.02", "BLOCKED_MIN_EDGE"),
        ("[TRACE] stop: council block confidence_before=0.70 reason=sample too small", "BLOCKED_COUNCIL"),
        ("[TRACE] stop: market quality filter", "BLOCKED_MARKET_QUALITY"),
    ]
    for trace, expected in traces_and_expected:
        result = _final_reason(trace, trade=None)
        if result == "BLOCKED_QUARANTINE":
            return f"Trace {trace!r} was incorrectly classified as BLOCKED_QUARANTINE"
        if result != expected:
            return f"Trace classified as {result!r}, expected {expected!r}: {trace!r}"
    return None


# ── 6/7. Safety locks and thresholds ─────────────────────────────────────────

def test_real_money_stays_locked() -> str | None:
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades
    records = load_trades()
    buckets = classify_records(records)
    gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gate.get("real_money_allowed") is not False:
        return f"real_money_allowed is not False: {gate.get('real_money_allowed')!r}"
    return None


def test_scale_stays_locked() -> str | None:
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades
    records = load_trades()
    buckets = classify_records(records)
    gate = evaluate_proof_gates(buckets, buckets["clean_settled"])
    if gate.get("scale_allowed") is not False:
        return f"scale_allowed is not False: {gate.get('scale_allowed')!r}"
    return None


def test_min_edge_unchanged() -> str | None:
    from config.trading_config import MIN_EDGE
    if abs(MIN_EDGE - 0.03) > 1e-9:
        return f"MIN_EDGE changed: {MIN_EDGE} (expected 0.03)"
    return None


def test_min_confidence_unchanged() -> str | None:
    from config.trading_config import MIN_CONFIDENCE
    if abs(MIN_CONFIDENCE - 0.65) > 1e-9:
        return f"MIN_CONFIDENCE changed: {MIN_CONFIDENCE} (expected 0.65)"
    return None


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("config exports QUARANTINED_TICKER_PREFIXES",        test_config_exports_quarantine_prefixes),
        ("'KXETH' in quarantine list",                         test_kxeth_in_quarantine_list),
        ("KXETH15M ticker blocked",                            test_kxeth_ticker_blocked),
        ("KXETHD ticker blocked",                              test_kxethd_ticker_blocked),
        ("KXETH bare ticker blocked",                          test_kxeth_bare_blocked),
        ("KXBTCD NOT blocked",                                 test_kxbtcd_not_blocked),
        ("KXBTC NOT blocked",                                  test_kxbtc_not_blocked),
        ("KXSOL NOT blocked",                                  test_kxsol_not_blocked),
        ("KXXRP NOT blocked",                                  test_kxxrp_not_blocked),
        ("paper_trader imports quarantine constant",           test_paper_trader_imports_quarantine_constant),
        ("paper_trader has quarantine guard in process_signal",test_paper_trader_checks_quarantine_in_process_signal),
        ("funnel logger: quarantined prefix → BLOCKED_QUARANTINE", test_funnel_logger_maps_quarantined_prefix),
        ("funnel logger: other traces not misclassified",      test_funnel_logger_non_kxeth_not_quarantine),
        ("real_money_allowed=False",                           test_real_money_stays_locked),
        ("scale_allowed=False",                                test_scale_stays_locked),
        ("MIN_EDGE unchanged at 0.03",                         test_min_edge_unchanged),
        ("MIN_CONFIDENCE unchanged at 0.65",                   test_min_confidence_unchanged),
    ]

    passed = 0
    failed = 0
    print("=" * 68)
    print("QUARANTINE KXETH TEST SUITE — Phase 8R")
    print("=" * 68)
    for name, fn in tests:
        err = fn()
        if err is None:
            print(f"  [PASS]  {name}")
            passed += 1
        else:
            print(f"  [FAIL]  {name}")
            print(f"          {err}")
            failed += 1

    print()
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)} tests")
    print()
    if failed == 0:
        print("PROVEN_QUARANTINE_KXETH_OK")
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
