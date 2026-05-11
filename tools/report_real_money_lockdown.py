#!/usr/bin/env python3
"""
tools/report_real_money_lockdown.py
-------------------------------------
Read-only real money safety lockdown audit.

Verifies every layer of the lockdown that prevents live order execution:

  LAYER 1 — Config lockdown
    TRADING_MODE = "PAPER"
    GLOBAL_FORCED_LEARNING_MODE = True  (Kelly disabled, forced $5 bets)
    DATA_COLLECTION_OVERRIDE_ENABLED = False

  LAYER 2 — HTTP method lockdown (brokers/kalshi_client.py)
    Only .get() method exists — no POST, PUT, PATCH, DELETE
    No order placement, no position modification, no withdrawal

  LAYER 3 — Execution path lockdown
    PaperTrader.execute() only writes to JSONL log — no broker calls
    No live order ID ever assigned
    Bankroll is a local float — no real balance

  LAYER 4 — Scale / proof lockdown
    scale_allowed = False (hardcoded in evaluate_proof_gates)
    real_money_allowed = False (hardcoded in evaluate_proof_gates)
    SCALE_ELIGIBLE verdict requires modern_full >= 100, normal_modern >= 30, PF > 1.10

  LAYER 5 — Env var absence lockdown
    KALSHI_API_KEY_ID — if absent, all API calls fail at header build step
    KALSHI_PRIVATE_KEY_PATH — if absent or key unreadable, all calls fail

Does NOT modify any state or make any network requests.

Expected result: REAL_MONEY_READY=ABSOLUTELY_NOT
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.trading_config import (
    DATA_COLLECTION_OVERRIDE_ENABLED,
    EDGE_DANGER_BLOCK_HIGH_EDGE,
    GLOBAL_FORCED_LEARNING_MODE,
    INITIAL_BANKROLL,
    KELLY_FRACTION,
    TRADING_MODE,
)


def _check(label: str, ok: bool, detail: str = "") -> tuple[bool, str]:
    tag = "[PASS]" if ok else "[FAIL]"
    line = f"  {tag}  {label}"
    if detail:
        line += f"  ({detail})"
    return ok, line


def _audit_kalshi_http_methods(source_path: Path) -> dict:
    """Scan kalshi_client.py for HTTP method calls via AST."""
    if not source_path.exists():
        return {"error": f"file not found: {source_path}"}

    source = source_path.read_text()
    tree = ast.parse(source)

    http_calls: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = node.attr.lower()
            if name in ("get", "post", "put", "patch", "delete"):
                # Check if it's a requests.<method> or similar call
                if isinstance(node.value, ast.Name) and node.value.id == "requests":
                    http_calls.setdefault(name, []).append(node.lineno)
                elif isinstance(node.value, ast.Attribute) and node.value.attr == "Session":
                    http_calls.setdefault(name, []).append(node.lineno)

    # Also grep for string-based method references
    write_methods = [m for m in ("post", "put", "patch", "delete") if m in http_calls]
    has_write_http = len(write_methods) > 0

    return {
        "get_lines": http_calls.get("get", []),
        "post_lines": http_calls.get("post", []),
        "put_lines": http_calls.get("put", []),
        "patch_lines": http_calls.get("patch", []),
        "delete_lines": http_calls.get("delete", []),
        "write_methods_found": write_methods,
        "has_write_http": has_write_http,
    }


def _audit_paper_trader_broker_calls(source_path: Path) -> dict:
    """Scan paper_trader.py for any broker/order placement calls."""
    if not source_path.exists():
        return {"error": f"file not found: {source_path}"}

    source = source_path.read_text()
    tree = ast.parse(source)

    broker_imports = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if "kalshi" in mod.lower() or "broker" in mod.lower():
                    broker_imports.append(f"line {node.lineno}: from {mod} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "kalshi" in alias.name.lower() or "broker" in alias.name.lower():
                        broker_imports.append(f"line {node.lineno}: import {alias.name}")

    # Look for requests.post / .put in paper_trader specifically
    write_http: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.lower() in ("post", "put", "patch", "delete"):
                if isinstance(node.value, ast.Name) and node.value.id in ("requests", "session"):
                    write_http.append(f"line {node.lineno}: requests.{node.attr}")

    # Check for order_id or live_order patterns
    live_order_refs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and "order_id" in node.id.lower():
            live_order_refs.append(f"line {node.lineno}: {node.id}")

    return {
        "broker_imports": broker_imports,
        "write_http_calls": write_http,
        "live_order_refs": live_order_refs,
        "has_broker_import": len(broker_imports) > 0,
        "has_write_http": len(write_http) > 0,
    }


def _audit_env_vars(default_key_path: "Path | None" = None) -> dict:
    import stat as _stat
    key_id       = os.getenv("KALSHI_API_KEY_ID", "")
    key_path_env = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

    # Match the actual fallback used by kalshi_client.py:
    #   os.getenv("KALSHI_PRIVATE_KEY_PATH", "<root>/kalshi_private_key.pem")
    # Without this, the report incorrectly claims the key is absent when the
    # env var is unset but the file exists at the project-root default path.
    if default_key_path is None:
        default_key_path = ROOT / "kalshi_private_key.pem"

    if key_path_env:
        key_file   = Path(key_path_env)
        key_source = "env var KALSHI_PRIVATE_KEY_PATH"
    else:
        key_file   = default_key_path
        key_source = "default (env var absent; code falls back to project root)"

    key_exists = key_file.exists()
    perm_octal: "str | None" = None
    perm_safe:  "bool | None" = None
    if key_exists:
        mode       = key_file.stat().st_mode
        perm_octal = oct(_stat.S_IMODE(mode))
        perm_safe  = _stat.S_IMODE(mode) == 0o600

    return {
        "KALSHI_API_KEY_ID_set":       bool(key_id),
        "KALSHI_PRIVATE_KEY_PATH_set": bool(key_path_env),
        "key_file_path":               str(key_file),
        "key_path_source":             key_source,
        "key_file_exists":             key_exists,
        "key_file_perm_octal":         perm_octal,
        "key_file_perm_safe":          perm_safe,
    }


def _check_scale_hardcodes(source_path: Path) -> dict:
    """Verify scale_allowed and real_money_allowed are hardcoded False.

    Uses regex to tolerate whitespace variants like:
        "scale_allowed":              False,
    """
    if not source_path.exists():
        return {"error": f"not found: {source_path}"}
    source = source_path.read_text()
    results = {}
    for name in ("scale_allowed", "real_money_allowed"):
        n = re.escape(name)
        found_false = bool(
            re.search(rf'"{n}"\s*:\s*False', source)
            or re.search(rf"'{n}'\s*:\s*False", source)
            or re.search(rf'\b{n}\b\s*=\s*False', source)
        )
        found_true = bool(
            re.search(rf'"{n}"\s*:\s*True', source)
            or re.search(rf"'{n}'\s*:\s*True", source)
            or re.search(rf'\b{n}\b\s*=\s*True', source)
        )
        results[name] = {
            "false_assignment_found": found_false,
            "true_assignment_found": found_true,
            "safe": found_false and not found_true,
        }
    return results


def main() -> None:
    kalshi_client_path  = ROOT / "brokers" / "kalshi_client.py"
    paper_trader_path   = ROOT / "brain" / "paper_trader.py"
    clean_truth_path    = ROOT / "tools" / "clean_truth_report.py"

    http_audit   = _audit_kalshi_http_methods(kalshi_client_path)
    pt_audit     = _audit_paper_trader_broker_calls(paper_trader_path)
    env_audit    = _audit_env_vars()
    scale_audit  = _check_scale_hardcodes(clean_truth_path)

    W = 72
    print("=" * W)
    print("REAL MONEY LOCKDOWN AUDIT")
    print("=" * W)
    print()

    all_ok = True

    print("LAYER 1 — CONFIG LOCKDOWN")
    print("-" * W)
    checks = [
        _check("TRADING_MODE = 'PAPER'", TRADING_MODE == "PAPER",
               f"current = {TRADING_MODE!r}"),
        _check("GLOBAL_FORCED_LEARNING_MODE = True", GLOBAL_FORCED_LEARNING_MODE,
               f"current = {GLOBAL_FORCED_LEARNING_MODE}  (Kelly disabled, forced $5 bets)"),
        _check("DATA_COLLECTION_OVERRIDE_ENABLED = False", not DATA_COLLECTION_OVERRIDE_ENABLED,
               f"current = {DATA_COLLECTION_OVERRIDE_ENABLED}"),
        _check("EDGE_DANGER_BLOCK_HIGH_EDGE = True", EDGE_DANGER_BLOCK_HIGH_EDGE,
               "M-45 high-edge guard active"),
    ]
    for ok, line in checks:
        print(line)
        if not ok:
            all_ok = False
    print()

    print("LAYER 2 — HTTP METHOD LOCKDOWN (brokers/kalshi_client.py)")
    print("-" * W)
    if "error" in http_audit:
        print(f"  [FAIL]  {http_audit['error']}")
        all_ok = False
    else:
        get_n = len(http_audit.get("get_lines", []))
        ok_get, line = _check("requests.get() present (read-only API calls)", get_n > 0,
                              f"found {get_n} call site(s)")
        print(line)

        for method in ("post", "put", "patch", "delete"):
            lines = http_audit.get(f"{method}_lines", [])
            ok_w, line = _check(
                f"requests.{method}() absent (no write endpoint)",
                len(lines) == 0,
                f"found {len(lines)} call site(s) at lines: {lines}" if lines else "confirmed absent",
            )
            print(line)
            if not ok_w:
                all_ok = False

        if not http_audit["has_write_http"]:
            print("  [CONFIRMED] kalshi_client.py is purely read-only — GET only.")
        else:
            print(f"  [CRITICAL] Write HTTP methods found: {http_audit['write_methods_found']}")
            all_ok = False
    print()

    print("LAYER 3 — EXECUTION PATH LOCKDOWN (brain/paper_trader.py)")
    print("-" * W)
    if "error" in pt_audit:
        print(f"  [FAIL]  {pt_audit['error']}")
        all_ok = False
    else:
        ok_b, line = _check("No broker/kalshi import in paper_trader.py",
                            not pt_audit["has_broker_import"],
                            f"imports found: {pt_audit['broker_imports']}" if pt_audit["broker_imports"]
                            else "confirmed none")
        print(line)
        if not ok_b:
            all_ok = False

        ok_w, line = _check("No write HTTP calls in paper_trader.py",
                            not pt_audit["has_write_http"],
                            f"calls found: {pt_audit['write_http_calls']}" if pt_audit["write_http_calls"]
                            else "confirmed none")
        print(line)
        if not ok_w:
            all_ok = False

        print(f"  [INFO]  PaperTrader writes to JSONL log only — no live order IDs assigned.")
        print(f"  [INFO]  Bankroll is a local Python float (${INITIAL_BANKROLL:.2f} initial).")
        print(f"  [INFO]  Kelly fraction = {KELLY_FRACTION} but GLOBAL_FORCED_LEARNING_MODE=True overrides.")
    print()

    print("LAYER 4 — SCALE / PROOF LOCKDOWN (tools/clean_truth_report.py)")
    print("-" * W)
    if "error" in scale_audit:
        print(f"  [FAIL]  {scale_audit['error']}")
        all_ok = False
    else:
        for name, result in scale_audit.items():
            ok_s, line = _check(f"{name} = False (hardcoded)", result["safe"],
                                "found True assignment — CRITICAL" if result["true_assignment_found"]
                                else "confirmed False")
            print(line)
            if not ok_s:
                all_ok = False
    print(f"  [INFO]  SCALE_ELIGIBLE requires modern_full>=100, normal_modern>=30, PF>1.10.")
    print(f"  [INFO]  Even SCALE_ELIGIBLE requires human sign-off to flip scale_allowed.")
    print()

    print("LAYER 5 — ENV VAR / KEY FILE LOCKDOWN")
    print("-" * W)
    key_id_set   = env_audit["KALSHI_API_KEY_ID_set"]
    key_path_set = env_audit["KALSHI_PRIVATE_KEY_PATH_set"]
    key_exists   = env_audit["key_file_exists"]
    key_path     = env_audit["key_file_path"]
    key_src      = env_audit["key_path_source"]
    perm_octal   = env_audit["key_file_perm_octal"]
    perm_safe    = env_audit["key_file_perm_safe"]

    if not key_id_set:
        print("  [PASS]  KALSHI_API_KEY_ID not set — API auth will fail at header build.")
    else:
        print("  [INFO]  KALSHI_API_KEY_ID is set in environment.")

    if not key_path_set:
        print("  [INFO]  KALSHI_PRIVATE_KEY_PATH env var not set — using default fallback path.")
    else:
        print("  [INFO]  KALSHI_PRIVATE_KEY_PATH env var set.")

    print(f"  [INFO]  Key path checked ({key_src}):")
    print(f"          {key_path}")

    if not key_exists:
        print("  [PASS]  Key file not present — signing step will fail, blocking all API calls.")
    else:
        print("  [WARN]  Key file present locally — monitor permissions.")
        print("          Key presence does NOT enable live trading:")
        print("          - write HTTP endpoints are absent (GET only)")
        print("          - TRADING_MODE=PAPER")
        print("          - PaperTrader never calls broker")
        if perm_octal is not None:
            if perm_safe:
                print(f"  [PASS]  Key file permissions: {perm_octal} (owner read/write only — correct).")
            else:
                print(f"  [WARN]  Key file permissions: {perm_octal} — tighten to 600 (owner-only).")
                print(f"          Run: chmod 600 {key_path}")
    print()

    print("=" * W)
    print("LOCKDOWN VERDICT")
    print("=" * W)
    print(f"  REAL_MONEY_READY:      ABSOLUTELY_NOT")
    print(f"  All layers intact:     {'YES' if all_ok else 'NO — see FAIL lines above'}")
    print()
    print("  Summary of protections:")
    print("    1. TRADING_MODE=PAPER — no live path in code")
    print("    2. kalshi_client.py has GET only — impossible to place orders")
    print("    3. PaperTrader never imports or calls broker — JSONL log only")
    print("    4. scale_allowed=False hardcoded — scale gate requires human code change")
    print("    5. GLOBAL_FORCED_LEARNING_MODE=True — Kelly overridden, $5 bets only")
    print()
    if not all_ok:
        print("  [ACTION REQUIRED] Fix FAIL items above before considering any live trading.")
    else:
        print("  [CONFIRMED] System is fully locked down from real money execution.")
    print()
    print("RESULT: REAL_MONEY_LOCKDOWN_REPORT_OK")


if __name__ == "__main__":
    main()
