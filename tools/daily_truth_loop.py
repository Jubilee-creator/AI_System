#!/usr/bin/env python3
"""
tools/daily_truth_loop.py
-------------------------
Read-only daily truth loop runner.
Runs all core truth reports in one command and saves a timestamped snapshot.

Usage:
  python3 tools/daily_truth_loop.py
  python3 tools/daily_truth_loop.py --quiet   # suppress per-tool stdout to terminal

Rules:
  - Read-only: no log mutation, no config mutation
  - Always creates reports/daily_truth/ if missing
  - Handles tool failure gracefully (continues on error)
  - Includes timestamp, git commit hash, and git status
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports" / "daily_truth"

TOOLS: list[tuple[str, list[str]]] = [
    ("HEALTH",       ["python3", "tools/report_health.py"]),
    ("BLOCKERS",     ["python3", "tools/audit_execution_blockers.py"]),
    ("OPEN_TRADE",   ["python3", "tools/audit_open_trade_state.py"]),
    ("TRUST_GATE",   ["python3", "tools/report_edge_trust_gate.py"]),
    ("MODERN_PROOF", ["python3", "tools/report_modern_only_proof.py"]),
    ("TRUTH_REPORT", ["python3", "tools/clean_truth_report.py"]),
]

_VERDICT_KEYWORDS = (
    "RESULT:", "VERDICT:", "WARN —", "OK —", "⚠",
    "DATA_COLLECTION_ONLY", "NOT_PROVEN", "PROVEN_PROFITABLE",
    "SCALE_ELIGIBLE", "WATCHLIST", "COLLECTING", "STALLED",
    "EDGE_TRUST_GATE_REPORT_OK", "MODERN_ONLY_PROOF_REPORT_OK",
    "PROFITABILITY_TRUTH_REPORT_OK", "ASYMMETRY_EDGE_INVERSION_REPORT_OK",
)


def _git_info() -> tuple[str, str]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=ROOT, timeout=10,
        ).stdout.strip()
    except Exception:
        commit = "UNKNOWN"
    try:
        status_out = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, cwd=ROOT, timeout=10,
        ).stdout.strip()
        status = status_out if status_out else "(clean)"
    except Exception:
        status = "UNKNOWN"
    return commit, status


def _run_tool(label: str, cmd: list[str]) -> tuple[bool, str]:
    """Run one tool. Returns (success, full_output)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=ROOT, timeout=120,
        )
        output = result.stdout
        if result.stderr.strip():
            output += "\n[STDERR]\n" + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"[TIMEOUT] {label} exceeded 120s deadline"
    except Exception as exc:
        return False, f"[ERROR] {label}: {exc}"


def _extract_verdict(output: str) -> str:
    """Extract a single-line verdict from tool output for the compact summary."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if any(kw in line for kw in _VERDICT_KEYWORDS):
            return line[:120]
    return "(no verdict line found)"


def main() -> None:
    quiet = "--quiet" in sys.argv
    now = datetime.now(timezone.utc)
    ts_filename = now.strftime("%Y-%m-%d_%H%M")
    ts_display = now.strftime("%Y-%m-%d %H:%M UTC")
    commit, git_status = _git_info()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    outfile = REPORTS_DIR / f"{ts_filename}_truth_report.txt"

    header = (
        "=" * 72 + "\n"
        "AI_SYSTEM DAILY TRUTH LOOP REPORT\n"
        f"  Generated  : {ts_display}\n"
        f"  Git commit : {commit}\n"
        f"  Git status : {git_status}\n"
        "=" * 72 + "\n\n"
    )

    tool_results: list[tuple[str, bool, str]] = []

    for label, cmd in TOOLS:
        if not quiet:
            print(f"  Running {label}...", end=" ", flush=True)
        ok, output = _run_tool(label, cmd)
        tool_results.append((label, ok, output))
        if not quiet:
            print("OK" if ok else "FAILED")

    # Build compact summary
    summary_lines = []
    for label, ok, output in tool_results:
        status_str = "OK  " if ok else "FAIL"
        verdict = _extract_verdict(output)
        summary_lines.append(f"  [{status_str}] {label:<20s}  {verdict}")

    summary_block = (
        "SUMMARY\n"
        + "─" * 72 + "\n"
        + "\n".join(summary_lines)
        + "\n"
    )

    # Build per-tool sections
    sections: list[str] = []
    for label, ok, output in tool_results:
        status_label = "OK" if ok else "FAILED"
        sections.append(
            f"\n{'─' * 72}\n"
            f"TOOL: {label}  [{status_label}]\n"
            f"{'─' * 72}\n"
            f"{output.rstrip()}\n"
        )

    full_report = header + summary_block + "".join(sections) + "\n"
    outfile.write_text(full_report, encoding="utf-8")

    # Print compact summary to terminal
    print()
    print("=" * 72)
    print("DAILY TRUTH LOOP — COMPACT SUMMARY")
    print("=" * 72)
    for line in summary_lines:
        print(line)
    print(f"\n  Full report: {outfile}")
    print("=" * 72)


if __name__ == "__main__":
    main()
