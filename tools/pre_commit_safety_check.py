#!/usr/bin/env python3
"""
tools/pre_commit_safety_check.py
---------------------------------
Read-only pre-commit safety check.

Scans the current git diff for dangerous changes to protected files and
protected strings. Outputs a SAFE / WARNING / BLOCKER report.

Does NOT block commits automatically. Does NOT install git hooks.
This is a diagnostic script only — run it manually before committing.

Usage:
  python3 tools/pre_commit_safety_check.py          # check staged + unstaged vs HEAD
  python3 tools/pre_commit_safety_check.py --staged  # check only staged changes
  python3 tools/pre_commit_safety_check.py --all     # HEAD..working tree (all changes)
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ── Protected paths ──────────────────────────────────────────────────────────
# Changes to these files require explicit human authorization.
DANGER_FILES: list[tuple[str, str]] = [
    # (pattern, reason)
    (r"^config/",           "Trading config — contains all safety thresholds and mode flags"),
    (r"^brain/",            "Brain modules — execution, council, risk, scanner"),
    (r"^engine/",           "Engine modules — edge formula, decision logic"),
    (r"^CLAUDE\.md$",       "Project memory — defines forbidden actions and proof rules"),
    (r"^AGENTS\.md$",       "Agent definitions — defines system-level agent behavior"),
    (r"^brokers/",          "Broker client — any POST capability here enables real orders"),
    (r"^auto_settle_trades\.py$", "Auto-settlement process — touches trade records directly"),
]

WARN_FILES: list[tuple[str, str]] = [
    (r"^Dashboard\.py$",    "Dashboard — large file; verify no trading logic was changed"),
    (r"^tools/clean_truth_report\.py$",  "Truth report — contains proof gate logic"),
    (r"^tools/performance_report\.py$",  "Performance report — PnL and classification logic"),
    (r"^tools/build_edge_profile\.py$",  "Edge profile builder — affects trust gate evaluation"),
]


# ── Protected strings ─────────────────────────────────────────────────────────
# A diff line starting with '+' and containing one of these strings is flagged.
# Leading '+' means it's a newly added line in the diff.

BLOCKER_STRINGS: list[tuple[str, str]] = [
    # (regex on the diff line content, reason)
    (r"real_money_allowed\s*=\s*True",        "Would enable real money — absolutely forbidden"),
    (r"scale_allowed\s*=\s*True",             "Would enable scaling — hardcoded False required"),
    (r"GLOBAL_FORCED_LEARNING_MODE\s*=\s*False", "Would enable Kelly execution — forbidden pre-proof"),
    (r"DATA_COLLECTION_OVERRIDE_ENABLED\s*=\s*True", "DC override was disabled for good reason"),
    (r"MIN_EDGE\s*=\s*0\.0[012]",             "Would lower MIN_EDGE below 0.03 — weakens filter"),
    (r"MIN_CONFIDENCE\s*=\s*0\.[0-5]",        "Would lower MIN_CONFIDENCE below 0.60"),
    (r"DAILY_LOSS_LIMIT\s*=\s*-[0-9]{3,}",   "Would increase daily loss limit beyond -$99"),
    (r"EDGE_DANGER_BLOCK_HIGH_EDGE\s*=\s*False", "Would disable edge danger guard — empirically needed"),
    (r"EDGE_DANGER_HIGH_EDGE_MIN\s*=\s*0\.[1-9]", "Would raise guard threshold — guard must stay at 0.08"),
    (r"def\s+\w*order\w*\(",                  "Possible order/POST method added — check for broker calls"),
    (r"requests\.post\(",                     "HTTP POST call — could be a real broker order"),
    (r"kalshi.*order\b",                      "Kalshi order call — only GET operations allowed"),
    (r"bootstrap_provisional.*=.*True.*proof", "bootstrap_provisional flagged as proof — forbidden"),
    (r"data_collection_override.*=.*True.*proof", "dc_override flagged as proof — forbidden"),
    (r"normal_modern\s*>=?\s*[0-9]\b",        "Potential proof gate threshold change — verify it's not lowered"),
]

WARNING_STRINGS: list[tuple[str, str]] = [
    (r"MIN_EDGE",                             "MIN_EDGE modified — verify it was not lowered"),
    (r"MIN_CONFIDENCE",                       "MIN_CONFIDENCE modified — verify it was not lowered"),
    (r"PROOF_BASE\s*=",                       "Proof base threshold modified — verify not lowered"),
    (r"TRUST_GATE\s*=",                       "Trust gate threshold modified"),
    (r"SCALE_GATE\s*=",                       "Scale gate threshold modified"),
    (r"real_money_allowed",                   "real_money_allowed appears in diff — check direction"),
    (r"scale_allowed",                        "scale_allowed appears in diff — check direction"),
    (r"GLOBAL_FORCED_LEARNING",               "GLOBAL_FORCED_LEARNING_MODE appears in diff"),
    (r"EDGE_DANGER",                          "Edge danger guard appears in diff"),
    (r"bootstrap_provisional",               "bootstrap_provisional logic modified"),
    (r"data_collection_override",            "data_collection_override logic modified"),
    (r"evaluate_proof_gates",                "Proof gate evaluation logic modified"),
    (r"classify_records",                    "Record classification logic modified"),
    (r"paper_trades\.jsonl",                 "Reference to paper_trades log — verify no truncation/rewrite"),
    (r"truncate|DROP TABLE|DELETE FROM",     "Destructive data operation"),
]

# ── False-positive suppression ────────────────────────────────────────────────
# Lines that look dangerous but are safe (e.g., in comments, assertion checks)
SAFE_CONTEXTS = [
    r"#.*real_money_allowed.*False",
    r"assert.*real_money_allowed.*False",
    r"\"real_money_allowed\":\s*False",
    r"'real_money_allowed':\s*False",
    r"#.*scale_allowed.*False",
    r"assert.*scale_allowed.*False",
    r"\"scale_allowed\":\s*False",
]


def _git_diff(mode: str) -> str:
    """Return the git diff output for the requested mode."""
    if mode == "staged":
        cmd = ["git", "diff", "--cached"]
    else:
        cmd = ["git", "diff", "HEAD"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=ROOT, timeout=30,
        )
        return result.stdout
    except Exception as exc:
        return f"[ERROR reading diff: {exc}]"


def _current_files_changed(diff: str) -> list[str]:
    """Extract list of changed file paths from diff."""
    files = []
    for line in diff.splitlines():
        if line.startswith("diff --git a/"):
            parts = line.split(" b/")
            if len(parts) >= 2:
                files.append(parts[-1])
    return files


def _is_safe_context(line: str) -> bool:
    for pattern in SAFE_CONTEXTS:
        if re.search(pattern, line):
            return True
    return False


def _check_diff(diff: str) -> tuple[list[str], list[str], list[str]]:
    """
    Return (blockers, warnings, info_messages) lists.
    Blockers = must fix before committing (or get explicit authorization).
    Warnings = should review carefully.
    Info     = informational notices.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    info:     list[str] = []

    changed_files = _current_files_changed(diff)

    # Check file-level protections
    for path in changed_files:
        for pattern, reason in DANGER_FILES:
            if re.search(pattern, path):
                blockers.append(f"[DANGER FILE]  {path}\n             → {reason}")
        for pattern, reason in WARN_FILES:
            if re.search(pattern, path):
                warnings.append(f"[WARN FILE]    {path}\n             → {reason}")

    # Check line-level patterns
    current_file = "(unknown)"
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            parts = line.split(" b/")
            current_file = parts[-1] if len(parts) >= 2 else "(unknown)"
            continue

        # Only inspect added lines ('+' prefix), skip diff headers ('+++', '---')
        if not line.startswith("+") or line.startswith("+++"):
            continue

        content = line[1:]  # strip leading '+'

        if _is_safe_context(content):
            continue

        for pattern, reason in BLOCKER_STRINGS:
            if re.search(pattern, content, re.IGNORECASE):
                snippet = content.strip()[:100]
                blockers.append(
                    f"[DANGER LINE]  {current_file}\n"
                    f"             line: {snippet}\n"
                    f"             → {reason}"
                )

        for pattern, reason in WARNING_STRINGS:
            if re.search(pattern, content, re.IGNORECASE):
                snippet = content.strip()[:100]
                warnings.append(
                    f"[WARN LINE]    {current_file}\n"
                    f"             line: {snippet}\n"
                    f"             → {reason}"
                )

    if not changed_files:
        info.append("No changed files detected in diff. Working tree may be clean.")
    else:
        info.append(f"Changed files ({len(changed_files)}): {', '.join(changed_files[:8])}"
                    + (f" ... +{len(changed_files)-8} more" if len(changed_files) > 8 else ""))

    return blockers, warnings, info


def main() -> None:
    args = sys.argv[1:]
    mode = "staged" if "--staged" in args else "all"

    print("=" * 72)
    print("AI_SYSTEM PRE-COMMIT SAFETY CHECK")
    print(f"  Mode: {'staged changes only' if mode == 'staged' else 'all changes (staged + unstaged)'}")
    print("=" * 72)

    diff = _git_diff(mode)
    if diff.startswith("[ERROR"):
        print(f"\n  {diff}")
        sys.exit(1)

    if not diff.strip():
        print("\n  Working tree is clean — no changes to check.")
        print("\n  VERDICT: SAFE (nothing changed)")
        return

    blockers, warnings, info = _check_diff(diff)

    for msg in info:
        print(f"\n  INFO: {msg}")

    if blockers:
        print(f"\n{'━' * 72}")
        print(f"  ⛔ BLOCKERS ({len(blockers)}) — requires explicit authorization before committing")
        print(f"{'━' * 72}")
        for b in blockers:
            print(f"\n  {b}")

    if warnings:
        print(f"\n{'─' * 72}")
        print(f"  ⚠  WARNINGS ({len(warnings)}) — review carefully")
        print(f"{'─' * 72}")
        for w in warnings:
            print(f"\n  {w}")

    print(f"\n{'=' * 72}")
    if blockers:
        verdict = f"BLOCKER — {len(blockers)} dangerous change(s) detected"
        print(f"  VERDICT: {verdict}")
        print()
        print("  Before committing:")
        print("  1. Confirm each blocker is intentional and authorized by Samuel")
        print("  2. Check that real_money_allowed and scale_allowed remain False")
        print("  3. Confirm no proof gate thresholds were lowered")
        print("  4. Confirm no POST/order methods were added to broker code")
    elif warnings:
        verdict = f"WARNING — {len(warnings)} item(s) need review"
        print(f"  VERDICT: {verdict}")
        print()
        print("  Review each warning above. If all are intentional and safe, commit may proceed.")
    else:
        print("  VERDICT: SAFE — no dangerous patterns detected in this diff")
    print("=" * 72)
    print()
    print("  Reminder: real_money_allowed=False and scale_allowed=False are hardcoded.")
    print("  Any commit that weakens proof gates or enables real money MUST be")
    print("  explicitly authorized by Samuel before merging.")
    print()


if __name__ == "__main__":
    main()
