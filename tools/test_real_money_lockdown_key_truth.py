#!/usr/bin/env python3
"""
tools/test_real_money_lockdown_key_truth.py
-------------------------------------------
Phase 9Q-2 — Key-file truth tests for report_real_money_lockdown.py.

Uses temporary fake files and paths only — never reads the real private key.

Tests:
  T01  No env var + default path exists      → key_file_exists = True
  T02  No env var + default path missing     → key_file_exists = False
  T03  Env var set to custom path            → checks env path, not default
  T04  Permission 600                        → key_file_perm_safe = True
  T05  Permission 644                        → key_file_perm_safe = False
  T06  _audit_env_vars never reads key bytes → returned dict has no key content
  T07  Write-call scanner: kalshi_client.py  → 0 write HTTP methods found
  T08  Final lockdown report exits cleanly   → stdout contains REAL_MONEY_LOCKDOWN_REPORT_OK

Sentinel: PROVEN_REAL_MONEY_LOCKDOWN_KEY_TRUTH_OK
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.report_real_money_lockdown import (
    _audit_env_vars,
    _audit_kalshi_http_methods,
)


# ── helpers ───────────────────────────────────────────────────────────────────

@contextmanager
def _no_env(*keys: str):
    """Context manager: remove listed env keys for the duration."""
    saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def _make_fake_key(tmp_dir: Path, name: str = "fake_key.pem", mode: int = 0o600) -> Path:
    """Write a clearly fake PEM-shaped file and set its permissions."""
    path = tmp_dir / name
    path.write_text("-----BEGIN FAKE KEY-----\nTESTONLY\n-----END FAKE KEY-----\n")
    path.chmod(mode)
    return path


# ── T01 ───────────────────────────────────────────────────────────────────────

def test_no_env_default_exists() -> Optional[str]:
    """No env var + injected default path that exists → exists=True."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _make_fake_key(Path(tmp))
        with _no_env("KALSHI_PRIVATE_KEY_PATH", "KALSHI_API_KEY_ID"):
            result = _audit_env_vars(default_key_path=fake)
    if not result["key_file_exists"]:
        return f"expected key_file_exists=True, got False (path={result['key_file_path']})"
    if result["key_file_path"] != str(fake):
        return f"expected path {fake}, got {result['key_file_path']}"
    return None


# ── T02 ───────────────────────────────────────────────────────────────────────

def test_no_env_default_missing() -> Optional[str]:
    """No env var + injected default path that does not exist → exists=False."""
    ghost = Path("/tmp/this_key_does_not_exist_9Q2.pem")
    if ghost.exists():
        return "test setup error: ghost path unexpectedly exists"
    with _no_env("KALSHI_PRIVATE_KEY_PATH", "KALSHI_API_KEY_ID"):
        result = _audit_env_vars(default_key_path=ghost)
    if result["key_file_exists"]:
        return "expected key_file_exists=False for missing path, got True"
    return None


# ── T03 ───────────────────────────────────────────────────────────────────────

def test_env_var_overrides_default() -> Optional[str]:
    """Env var KALSHI_PRIVATE_KEY_PATH set → report checks env path, not default."""
    with tempfile.TemporaryDirectory() as tmp:
        env_key  = _make_fake_key(Path(tmp), name="env_key.pem")
        default  = Path(tmp) / "default_key.pem"   # does not exist
        with mock.patch.dict(os.environ, {"KALSHI_PRIVATE_KEY_PATH": str(env_key)}, clear=False):
            result = _audit_env_vars(default_key_path=default)
    if not result["KALSHI_PRIVATE_KEY_PATH_set"]:
        return "expected KALSHI_PRIVATE_KEY_PATH_set=True"
    if result["key_file_path"] != str(env_key):
        return f"expected env key path {env_key}, got {result['key_file_path']}"
    if not result["key_file_exists"]:
        return "expected key_file_exists=True for env-var path that exists"
    return None


# ── T04 ───────────────────────────────────────────────────────────────────────

def test_permission_600_safe() -> Optional[str]:
    """Key file with mode 600 → perm_safe=True, perm_octal='0o600'."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _make_fake_key(Path(tmp), mode=0o600)
        with _no_env("KALSHI_PRIVATE_KEY_PATH", "KALSHI_API_KEY_ID"):
            result = _audit_env_vars(default_key_path=fake)
    if not result["key_file_perm_safe"]:
        return f"expected perm_safe=True for mode 600, got False (octal={result['key_file_perm_octal']})"
    if result["key_file_perm_octal"] != "0o600":
        return f"expected perm_octal='0o600', got {result['key_file_perm_octal']!r}"
    return None


# ── T05 ───────────────────────────────────────────────────────────────────────

def test_permission_644_unsafe() -> Optional[str]:
    """Key file with mode 644 → perm_safe=False."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _make_fake_key(Path(tmp), mode=0o644)
        with _no_env("KALSHI_PRIVATE_KEY_PATH", "KALSHI_API_KEY_ID"):
            result = _audit_env_vars(default_key_path=fake)
    if result["key_file_perm_safe"]:
        return f"expected perm_safe=False for mode 644, got True (octal={result['key_file_perm_octal']})"
    return None


# ── T06 ───────────────────────────────────────────────────────────────────────

def test_no_key_contents_returned() -> Optional[str]:
    """_audit_env_vars result dict must not contain the key's byte content."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = _make_fake_key(Path(tmp))
        with _no_env("KALSHI_PRIVATE_KEY_PATH", "KALSHI_API_KEY_ID"):
            result = _audit_env_vars(default_key_path=fake)
    sentinel = "TESTONLY"   # unique string written to fake key
    for k, v in result.items():
        if isinstance(v, str) and sentinel in v:
            return f"key content found in result field '{k}': {v!r}"
    return None


# ── T07 ───────────────────────────────────────────────────────────────────────

def test_kalshi_client_no_write_methods() -> Optional[str]:
    """_audit_kalshi_http_methods on the real kalshi_client.py → no write methods."""
    client_path = ROOT / "brokers" / "kalshi_client.py"
    if not client_path.exists():
        return f"kalshi_client.py not found at {client_path}"
    result = _audit_kalshi_http_methods(client_path)
    if "error" in result:
        return f"audit error: {result['error']}"
    if result["has_write_http"]:
        return f"write HTTP methods found in kalshi_client.py: {result['write_methods_found']}"
    return None


# ── T08 ───────────────────────────────────────────────────────────────────────

def test_report_exits_with_sentinel() -> Optional[str]:
    """Running the full report emits REAL_MONEY_LOCKDOWN_REPORT_OK."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "report_real_money_lockdown.py")],
        capture_output=True,
        text=True,
    )
    if "REAL_MONEY_LOCKDOWN_REPORT_OK" not in proc.stdout:
        snippet = proc.stdout[-400:] if len(proc.stdout) > 400 else proc.stdout
        return f"sentinel not found in report output. Tail:\n{snippet}"
    return None


# ── runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    tests = [
        ("no env var + default path exists → key_file_exists=True",
         test_no_env_default_exists),
        ("no env var + default path missing → key_file_exists=False",
         test_no_env_default_missing),
        ("env var set → report checks env path, not default",
         test_env_var_overrides_default),
        ("permission 600 → perm_safe=True",
         test_permission_600_safe),
        ("permission 644 → perm_safe=False",
         test_permission_644_unsafe),
        ("result dict never contains key file contents",
         test_no_key_contents_returned),
        ("kalshi_client.py has 0 write HTTP methods",
         test_kalshi_client_no_write_methods),
        ("full report emits REAL_MONEY_LOCKDOWN_REPORT_OK sentinel",
         test_report_exits_with_sentinel),
    ]

    passed = 0
    failed = 0
    print("=" * 68)
    print("REAL MONEY LOCKDOWN KEY TRUTH TEST SUITE — Phase 9Q-2")
    print("=" * 68)
    print()
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
        print("PROVEN_REAL_MONEY_LOCKDOWN_KEY_TRUTH_OK")
    else:
        print(f"FAIL — {failed} test(s) did not pass")
    print()


if __name__ == "__main__":
    main()
