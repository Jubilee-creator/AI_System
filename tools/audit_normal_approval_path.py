"""
tools/audit_normal_approval_path.py
------------------------------------
Read-only diagnostic: why are normal council-approved trades zero?

Traces the approval gate sequence for every modern full-metadata trade and
simulates what the Critic would decide under different conditions:
  - Current rules (M-40 trust gate active)
  - Without M-40 trust gate (pre-M-40 bucket-check path only)
  - What original_edge threshold would be required for a normal ALLOW

Usage:
  cd tools && python3 audit_normal_approval_path.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from performance_report import (
    build_terminal_key_sets,
    classify_settled_records,
    load_trades,
)
from brain.edge_profile_health import edge_profile_health

EDGE_PROFILE_PATH = ROOT / "data" / "edge_profile.json"
MIN_SAMPLE_SIZE = 5          # critic_brain.MIN_SAMPLE_SIZE
MIN_NORMAL_EDGE = 0.03       # critic_brain.MIN_NORMAL_EDGE
MAX_ADJ_CURRENT = -0.15      # critic_brain.MAX_SMALL_SAMPLE_ADJUSTMENT


def _load_profile() -> dict[str, Any]:
    if not EDGE_PROFILE_PATH.exists():
        return {}
    return json.loads(EDGE_PROFILE_PATH.read_text())


def _is_modern(rec: dict) -> bool:
    has_quote = (
        rec.get("price_yes") is not None
        or rec.get("price_no") is not None
        or any(rec.get(k) is not None for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"))
    )
    return (
        rec.get("risk_edge") is not None
        and rec.get("model_probability") is not None
        and has_quote
    )


def _confidence_bucket(c: Optional[float]) -> str:
    if c is None:
        return "unknown"
    if c < 0.65: return "<0.65"
    if c < 0.70: return "0.65-0.70"
    if c < 0.75: return "0.70-0.75"
    if c < 0.80: return "0.75-0.80"
    if c < 0.90: return "0.80-0.90"
    return ">=0.90"


def _edge_bucket(e: Optional[float]) -> str:
    if e is None:
        return "unknown"
    if e < 0.03: return "<0.03"
    if e < 0.05: return "0.03-0.05"
    if e < 0.10: return "0.05-0.10"
    if e < 0.25: return "0.10-0.25"
    if e < 0.50: return "0.25-0.50"
    return ">=0.50"


def _bucket_n(profile: dict, group: str, key: str) -> int:
    b = profile.get("profiles", {}).get(group, {}).get(key, {})
    return int(b.get("trades", 0))


def _bucket_pnl(profile: dict, group: str, key: str) -> float:
    b = profile.get("profiles", {}).get(group, {}).get(key, {})
    return float(b.get("total_pnl", 0.0))


def _simulate_critic_bucket_path(
    original_edge: float,
    confidence: float,
    profile: dict,
    max_adj: float,
) -> tuple[str, float, list[str]]:
    """
    Run the Critic's bucket-check path (without trust gate).

    Returns (decision, confidence_adjustment, reasons).
    """
    conf_key = _confidence_bucket(confidence)
    edge_key = _edge_bucket(original_edge)

    # Primary bucket checks (confidence, edge)
    primary_keys = [("by_confidence_bucket", conf_key), ("by_edge_bucket", edge_key)]
    reasons: list[str] = []
    confidence_adjustment = 0.0
    block_reasons: list[str] = []

    for group, key in primary_keys:
        n = _bucket_n(profile, group, key)
        pnl = _bucket_pnl(profile, group, key)
        sparse = n < MIN_SAMPLE_SIZE

        if not sparse and (pnl < 0):
            # _bad_enough_sample: n >= 5 and losing
            block_reasons.append(f"confirmed-bad: {group}/{key} n={n} pnl={pnl:.2f}")
            continue

        if sparse:
            if pnl < 0 and n > 0:
                confidence_adjustment -= 0.10
            else:
                confidence_adjustment -= 0.05
            reasons.append(f"sparse: {group}/{key} n={n}")

    confidence_adjustment = max(confidence_adjustment, max_adj)
    adjusted_edge = original_edge + confidence_adjustment

    if block_reasons:
        return "BLOCK", confidence_adjustment, block_reasons

    if adjusted_edge < MIN_NORMAL_EDGE and reasons:
        block_reasons.append(
            f"adjusted_edge={adjusted_edge:.4f} < {MIN_NORMAL_EDGE} after "
            f"sparse penalty={confidence_adjustment:.3f}"
        )
        return "BLOCK", confidence_adjustment, block_reasons

    return "ALLOW", confidence_adjustment, reasons


def _min_edge_for_allow(confidence: float, profile: dict, max_adj: float) -> Optional[float]:
    """
    Compute the minimum original_edge that would survive the bucket-check path.
    Returns None when the confidence bucket is confirmed-bad (blocked regardless of edge).
    """
    conf_key = _confidence_bucket(confidence)
    conf_n = _bucket_n(profile, "by_confidence_bucket", conf_key)
    conf_pnl = _bucket_pnl(profile, "by_confidence_bucket", conf_key)

    # Confirmed-bad primary bucket: blocked regardless of edge level
    if conf_n >= MIN_SAMPLE_SIZE and conf_pnl < 0:
        return None  # no edge value survives this block

    adj = 0.0
    if conf_n < MIN_SAMPLE_SIZE:
        adj += -0.10 if (conf_pnl < 0 and conf_n > 0) else -0.05
    adj = max(adj, max_adj)
    # Need: original_edge + adj >= MIN_NORMAL_EDGE
    return MIN_NORMAL_EDGE - adj


def main() -> None:
    W = 80
    print("=" * W)
    print("  NORMAL APPROVAL PATH AUDIT")
    print("  M-41 — Why are council-approved normal trades zero?")
    print("=" * W)

    # Load data
    all_records = load_trades()
    settled_keys, forced_close_keys, void_keys = build_terminal_key_sets(all_records)
    clean_settled, _ = classify_settled_records(
        all_records, settled_keys, forced_close_keys, void_keys
    )

    profile = _load_profile()
    health = edge_profile_health(profile, EDGE_PROFILE_PATH)

    # Collect all unique modern records (all statuses, deduplicated by ticker+timestamp)
    seen: dict[tuple, dict] = {}
    for r in all_records:
        k = (r.get("ticker", ""), r.get("timestamp", ""))
        seen[k] = r
    unique_records = list(seen.values())
    modern_all = [r for r in unique_records if _is_modern(r)]
    modern_settled = [r for r in clean_settled if _is_modern(r)]

    dc = [r for r in modern_settled if bool(r.get("data_collection_override"))]
    normal = [r for r in modern_settled if not bool(r.get("data_collection_override"))]

    print()
    print("EDGE PROFILE STATE")
    print("-" * W)
    print(f"  trusted:                  {health['edge_profile_trusted']}")
    print(f"  clean_settled:            {health['clean_settled_trades']}  (need >= {health['thresholds']['min_clean_settled']})")
    print(f"  modern_full_metadata:     {health['modern_full_metadata_trades']}  (need >= {health['thresholds']['min_modern_full']})")
    print(f"  normal_council_approved:  {health['normal_council_approved_modern_trades']}  (need >= {health['thresholds']['min_normal_modern']})")
    print(f"  data_collection_only:     {health['data_collection_override_count']}/{health['modern_full_metadata_trades']}")
    print(f"  reason:                   {health['reason']}")

    print()
    print("TRADE COUNTS")
    print("-" * W)
    print(f"  Total unique records in log:    {len(unique_records)}")
    print(f"  Modern full-metadata (all):     {len(modern_all)}")
    print(f"  Modern full-metadata (settled): {len(modern_settled)}")
    print(f"  data_collection_override=True:  {len(dc)}")
    print(f"  Normal council-approved:        {len(normal)}")
    print(f"  M-38 audit fields populated:    {sum(1 for r in unique_records if r.get('council_decision') not in (None, ''))}")

    print()
    print("GATE ANALYSIS (per modern settled trade)")
    print("-" * W)
    trust_blocked_count = 0
    bucket_block_count = 0
    bucket_allow_count = 0

    for r in modern_settled:
        ticker = r.get("ticker", "unknown")
        original_edge = r.get("risk_edge") or r.get("original_edge") or 0.0
        confidence = r.get("confidence") or r.get("original_confidence") or 0.0

        # Gate 1: M-40 trust gate (current behavior)
        trust_blocked = not health["edge_profile_trusted"]
        # Gate 2 simulation: what would bucket-check decide without M-40?
        bucket_decision, adj, _ = _simulate_critic_bucket_path(
            original_edge, confidence, profile, MAX_ADJ_CURRENT
        )

        if trust_blocked:
            trust_blocked_count += 1
        # Count bucket results independently (not elif — both can apply)
        if bucket_decision == "BLOCK":
            bucket_block_count += 1
        else:
            bucket_allow_count += 1

        bucket_tag = f"bucket_without_m40={bucket_decision} adj={adj:+.3f}"
        trust_tag = "M40_BLOCKED" if trust_blocked else "m40_passes"
        print(
            f"  {ticker[:43]:<43}  "
            f"orig_edge={original_edge:.4f}  conf={confidence:.3f}  "
            f"{trust_tag}  {bucket_tag}"
        )

    print()
    print("GATE SUMMARY")
    print("-" * W)
    total = len(modern_settled)
    print(f"  Blocked by M-40 trust gate (current):          {trust_blocked_count}/{total}")
    print(f"  Would ALSO be blocked by bucket-check:         {bucket_block_count}/{total}  (pre-M-40 Critic path)")
    print(f"  Would be ALLOWED by bucket-check (no M-40):    {bucket_allow_count}/{total}")

    print()
    print("MINIMUM EDGE FOR NORMAL ALLOW (bucket-check path, no M-40 trust gate)")
    print("-" * W)
    sample_confidences = [0.55, 0.65, 0.70, 0.75]
    for conf in sample_confidences:
        min_edge = _min_edge_for_allow(conf, profile, MAX_ADJ_CURRENT)
        conf_key = _confidence_bucket(conf)
        conf_n = _bucket_n(profile, "by_confidence_bucket", conf_key)
        conf_pnl = _bucket_pnl(profile, "by_confidence_bucket", conf_key)
        if min_edge is None:
            note = f"BLOCKED regardless of edge — confirmed-bad bucket (n={conf_n} pnl={conf_pnl:.2f})"
            print(f"  confidence={conf:.2f} bucket={conf_key}: {note}")
        else:
            feasible = "achievable" if min_edge <= 0.15 else "very unlikely in practice"
            print(
                f"  confidence={conf:.2f} bucket={conf_key} (n={conf_n}): "
                f"need original_edge >= {min_edge:.4f}  ({feasible})"
            )

    print()
    print("BOOTSTRAP GAP")
    print("-" * W)
    need_normal = health["thresholds"]["min_normal_modern"]
    need_settled = health["thresholds"]["min_clean_settled"]
    need_modern = health["thresholds"]["min_modern_full"]
    have_normal = health["normal_council_approved_modern_trades"]
    have_settled = health["clean_settled_trades"]
    have_modern = health["modern_full_metadata_trades"]
    print(f"  Normal approvals needed for trust:    {need_normal - have_normal} more  ({have_normal}/{need_normal})")
    print(f"  Clean settled needed for trust:       {need_settled - have_settled} more  ({have_settled}/{need_settled})")
    print(f"  Modern full-metadata needed for trust: {need_modern - have_modern} more  ({have_modern}/{need_modern})")
    print()
    print("  Bootstrap problem:")
    print("    Trust requires normal_modern >= 10.")
    print("    Normal approvals require trust.")
    print("    => System CANNOT bootstrap from cold start under current rules.")
    print()
    print("    Pre-M-40 deadlock (SAME outcome):")
    print(f"    MAX_SMALL_SAMPLE_ADJUSTMENT = {MAX_ADJ_CURRENT}")
    print(f"    All buckets sparse. Confidence penalty applied every time.")
    print(f"    For signal to survive: original_edge + ({MAX_ADJ_CURRENT}) >= {MIN_NORMAL_EDGE}")
    print(f"    => original_edge >= {MIN_NORMAL_EDGE - MAX_ADJ_CURRENT:.2f}  (18% edge — effectively impossible)")
    print()
    print("    M-40 did NOT cause this deadlock. It formalized and surfaced it.")

    print()
    print("WHAT WOULD UNBLOCK NORMAL APPROVALS")
    print("-" * W)
    print("  Option 1 (REJECTED — unsafe): lower EDGE_PROFILE_MIN_NORMAL_MODERN to 0")
    print("    Effect: trust gate passes with all-DC profile.")
    print("    Risk: DC-only evidence treated as normal proof. Defeats the gate.")
    print()
    print("  Option 2 (REQUIRES M-42 design): bootstrap provisional mode")
    print("    When normal_modern == 0, apply smaller penalty (e.g., -0.05 instead of -0.15)")
    print("    Label trades as bootstrap_provisional=True, distinct from both DC and normal.")
    print("    These do NOT count as proof. They bootstrap the profile so real normal")
    print("    approvals can eventually occur when performance is confirmed.")
    print(f"    With -0.05 penalty, edge >= {MIN_NORMAL_EDGE + 0.05:.2f} would survive "
          f"(achievable at current signal quality).")
    print()
    print("  Option 3 (DIAGNOSTIC): no code change, wait for the system to run")
    print("    Current state: valid — no normal approvals possible yet.")
    print("    Action: start M-42 planning for bootstrap provisional class.")

    print()
    print("=" * W)
    print("  M-41 VERDICT: system is correctly blocked AND structurally deadlocked.")
    print("  No patch to live execution. M-42 required for bootstrap design.")
    print("=" * W)


if __name__ == "__main__":
    main()
