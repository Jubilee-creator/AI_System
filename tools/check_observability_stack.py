#!/usr/bin/env python3
"""
Phase 11C — Observability stack import and version check.

Run with:
  .venv_observability/bin/python tools/check_observability_stack.py

Sentinel: OBSERVABILITY_STACK_CHECK_OK
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUIRED = [
    ("duckdb",            "duckdb",            "__version__"),
    ("pandas",            "pandas",            "__version__"),
    ("pyarrow",           "pyarrow",           "__version__"),
    ("mlflow",            "mlflow",            "__version__"),
    ("evidently",         "evidently",         "__version__"),
    ("great_expectations","great_expectations", "__version__"),
]

SENTINEL = "OBSERVABILITY_STACK_CHECK_OK"


def main() -> int:
    print()
    print("=" * 60)
    print("  Observability Stack — Import & Version Check")
    print("=" * 60)

    missing = []
    for label, module_name, version_attr in REQUIRED:
        try:
            mod = __import__(module_name)
            ver = getattr(mod, version_attr, "unknown")
            print(f"  [OK] {label:<22} {ver}")
        except ImportError as exc:
            print(f"  [MISSING] {label:<20} {exc}")
            missing.append(label)

    print()
    if missing:
        print("MISSING PACKAGES — install with:")
        print(f"  .venv_observability/bin/pip install {' '.join(missing)}")
        print()
        return 1

    print(f"  {SENTINEL}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
