#!/usr/bin/env python3
"""
tools/test_real_money_lockdown.py
-----------------------------------
Verifies that the real-money lockdown is intact:

  1. TRADING_MODE = "PAPER"
  2. kalshi_client.py has no write HTTP methods (POST/PUT/PATCH/DELETE)
  3. paper_trader.py has no broker/kalshi imports
  4. GLOBAL_FORCED_LEARNING_MODE = True (Kelly disabled)
  5. scale_allowed is hardcoded False in clean_truth_report.py
  6. real_money_allowed is hardcoded False in clean_truth_report.py
  7. DATA_COLLECTION_OVERRIDE_ENABLED = False
  8. EDGE_DANGER_BLOCK_HIGH_EDGE = True
  9. evaluate_proof_gates() returns scale_allowed=False and real_money_allowed=False at runtime

Expected result: PROVEN_REAL_MONEY_LOCKDOWN_OK
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── helpers ────────────────────────────────────────────────────────────────────

def _ast_has_write_http(source: str) -> list[str]:
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.lower() in ("post", "put", "patch", "delete"):
                if isinstance(node.value, ast.Name) and node.value.id == "requests":
                    found.append(f"requests.{node.attr} at line {node.lineno}")
    return found


def _ast_has_broker_import(source: str) -> list[str]:
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "kalshi" in mod.lower() or "broker" in mod.lower():
                found.append(f"from {mod} import ... at line {node.lineno}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "kalshi" in alias.name.lower() or "broker" in alias.name.lower():
                    found.append(f"import {alias.name} at line {node.lineno}")
    return found


def _text_contains_false_hardcode(source: str, name: str) -> bool:
    """Regex-tolerant check — handles whitespace before ':' or '=' and after."""
    n = re.escape(name)
    return bool(
        re.search(rf'"{n}"\s*:\s*False', source)
        or re.search(rf"'{n}'\s*:\s*False", source)
        or re.search(rf'\b{n}\b\s*=\s*False', source)
    )


def _text_contains_true_hardcode(source: str, name: str) -> bool:
    """Regex-tolerant check — handles whitespace before ':' or '=' and after."""
    n = re.escape(name)
    return bool(
        re.search(rf'"{n}"\s*:\s*True', source)
        or re.search(rf"'{n}'\s*:\s*True", source)
        or re.search(rf'\b{n}\b\s*=\s*True', source)
    )


# ── 1. Config: TRADING_MODE ────────────────────────────────────────────────────

def test_trading_mode_is_paper() -> str | None:
    from config.trading_config import TRADING_MODE
    if TRADING_MODE != "PAPER":
        return f"TRADING_MODE={TRADING_MODE!r} — must be 'PAPER'"
    return None


# ── 2. kalshi_client.py: no write HTTP methods ────────────────────────────────

def test_kalshi_client_no_write_http() -> str | None:
    source = (ROOT / "brokers" / "kalshi_client.py").read_text()
    violations = _ast_has_write_http(source)
    if violations:
        return (
            "kalshi_client.py contains write HTTP methods — "
            "order execution is possible: " + str(violations)
        )
    return None


def test_kalshi_client_has_get() -> str | None:
    """Sanity: the client must have at least one GET call (it is the only allowed method)."""
    source = (ROOT / "brokers" / "kalshi_client.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.lower() == "get" and isinstance(node.value, ast.Name) and node.value.id == "requests":
                return None
    return "kalshi_client.py appears to have no requests.get() calls — verify file integrity"


# ── 3. paper_trader.py: no broker imports ─────────────────────────────────────

def test_paper_trader_no_broker_import() -> str | None:
    source = (ROOT / "brain" / "paper_trader.py").read_text()
    violations = _ast_has_broker_import(source)
    if violations:
        return (
            "paper_trader.py imports broker/kalshi modules — "
            "live execution path may exist: " + str(violations)
        )
    return None


def test_paper_trader_no_write_http() -> str | None:
    source = (ROOT / "brain" / "paper_trader.py").read_text()
    violations = _ast_has_write_http(source)
    if violations:
        return (
            "paper_trader.py contains write HTTP calls: " + str(violations)
        )
    return None


# ── 4. GLOBAL_FORCED_LEARNING_MODE ────────────────────────────────────────────

def test_forced_learning_mode_is_on() -> str | None:
    from config.trading_config import GLOBAL_FORCED_LEARNING_MODE
    if not GLOBAL_FORCED_LEARNING_MODE:
        return (
            "GLOBAL_FORCED_LEARNING_MODE=False — Kelly is live. "
            "Position sizes are NOT capped at $5 learning bets."
        )
    return None


# ── 5 & 6. scale_allowed / real_money_allowed hardcoded False ─────────────────

def test_scale_allowed_hardcoded_false() -> str | None:
    source = (ROOT / "tools" / "clean_truth_report.py").read_text()
    if not _text_contains_false_hardcode(source, "scale_allowed"):
        return "clean_truth_report.py: 'scale_allowed = False' not found — lockdown may be broken"
    if _text_contains_true_hardcode(source, "scale_allowed"):
        return "clean_truth_report.py: 'scale_allowed = True' found — lockdown BROKEN"
    return None


def test_real_money_allowed_hardcoded_false() -> str | None:
    source = (ROOT / "tools" / "clean_truth_report.py").read_text()
    if not _text_contains_false_hardcode(source, "real_money_allowed"):
        return "clean_truth_report.py: 'real_money_allowed = False' not found — lockdown may be broken"
    if _text_contains_true_hardcode(source, "real_money_allowed"):
        return "clean_truth_report.py: 'real_money_allowed = True' found — lockdown BROKEN"
    return None


# ── 7. DATA_COLLECTION_OVERRIDE_ENABLED = False ───────────────────────────────

def test_dc_override_is_off() -> str | None:
    from config.trading_config import DATA_COLLECTION_OVERRIDE_ENABLED
    if DATA_COLLECTION_OVERRIDE_ENABLED:
        return "DATA_COLLECTION_OVERRIDE_ENABLED=True — trust deadlock is active"
    return None


# ── 8. EDGE_DANGER_BLOCK_HIGH_EDGE = True ─────────────────────────────────────

def test_edge_danger_guard_is_on() -> str | None:
    from config.trading_config import EDGE_DANGER_BLOCK_HIGH_EDGE
    if not EDGE_DANGER_BLOCK_HIGH_EDGE:
        return (
            "EDGE_DANGER_BLOCK_HIGH_EDGE=False — M-45 high-edge guard is disabled. "
            "High-edge signals (negative ROI bucket) can pass through."
        )
    return None


# ── 9. evaluate_proof_gates() runtime check ───────────────────────────────────

def test_evaluate_proof_gates_returns_locked() -> str | None:
    """evaluate_proof_gates() must return scale_allowed=False and real_money_allowed=False."""
    from tools.clean_truth_report import evaluate_proof_gates, classify_records
    from tools.performance_report import load_trades

    all_records = load_trades()
    buckets = classify_records(all_records)
    clean_settled = buckets["clean_settled"]

    result = evaluate_proof_gates(buckets, clean_settled)

    if result.get("scale_allowed") is not False:
        return (
            f"evaluate_proof_gates() returned scale_allowed={result.get('scale_allowed')!r} — "
            "expected False; lockdown is broken"
        )
    if result.get("real_money_allowed") is not False:
        return (
            f"evaluate_proof_gates() returned real_money_allowed={result.get('real_money_allowed')!r} — "
            "expected False; lockdown is broken"
        )
    return None


# ── Runner ─────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("TRADING_MODE = PAPER",                       test_trading_mode_is_paper),
        ("kalshi_client no write HTTP",                test_kalshi_client_no_write_http),
        ("kalshi_client has GET (sanity)",             test_kalshi_client_has_get),
        ("paper_trader no broker import",              test_paper_trader_no_broker_import),
        ("paper_trader no write HTTP",                 test_paper_trader_no_write_http),
        ("GLOBAL_FORCED_LEARNING_MODE = True",         test_forced_learning_mode_is_on),
        ("scale_allowed hardcoded False",              test_scale_allowed_hardcoded_false),
        ("real_money_allowed hardcoded False",         test_real_money_allowed_hardcoded_false),
        ("DATA_COLLECTION_OVERRIDE_ENABLED = False",   test_dc_override_is_off),
        ("EDGE_DANGER_BLOCK_HIGH_EDGE = True",         test_edge_danger_guard_is_on),
        ("evaluate_proof_gates returns locked",        test_evaluate_proof_gates_returns_locked),
    ]

    fails: list[str] = []

    print("=" * 70)
    print("TEST: REAL MONEY LOCKDOWN INTEGRITY")
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
    from config.trading_config import (
        DATA_COLLECTION_OVERRIDE_ENABLED,
        EDGE_DANGER_BLOCK_HIGH_EDGE,
        GLOBAL_FORCED_LEARNING_MODE,
        TRADING_MODE,
    )
    print("LOCKDOWN STATE:")
    print(f"  TRADING_MODE                 = {TRADING_MODE}")
    print(f"  GLOBAL_FORCED_LEARNING_MODE  = {GLOBAL_FORCED_LEARNING_MODE}")
    print(f"  DATA_COLLECTION_OVERRIDE     = {DATA_COLLECTION_OVERRIDE_ENABLED}")
    print(f"  EDGE_DANGER_BLOCK_HIGH_EDGE  = {EDGE_DANGER_BLOCK_HIGH_EDGE}")
    print()

    if fails:
        for f in fails:
            print(f"  [FAIL] {f}")
        print("RESULT: FAILED")
        sys.exit(1)
    else:
        print("RESULT: PROVEN_REAL_MONEY_LOCKDOWN_OK")


if __name__ == "__main__":
    main()
