"""
brain/edge_profile_health.py
----------------------------
Central trust checks for data/edge_profile.json.

This module is intentionally read-only.  It prevents Builder/Critic/Council
from silently treating stale, tiny, or data-collection-only profile data as
reliable historical proof.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.trading_config import (
    EDGE_PROFILE_MAX_AGE_HOURS,
    EDGE_PROFILE_MIN_CLEAN_SETTLED,
    EDGE_PROFILE_MIN_MODERN_FULL,
    EDGE_PROFILE_MIN_NORMAL_MODERN,
)


def _parse_timestamp(value: Any):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def edge_profile_health(profile: dict[str, Any] | None, path: Path) -> dict[str, Any]:
    reasons: list[str] = []
    profile = profile or {}
    exists = path.exists() or bool(profile)

    generated_at = profile.get("generated_at")
    generated_dt = _parse_timestamp(generated_at)
    age_hours = None
    if not exists:
        reasons.append(f"edge_profile missing: {path}")
    elif not profile:
        reasons.append(f"edge_profile empty: {path}")
    elif generated_dt is None:
        reasons.append("edge_profile missing/invalid generated_at")
    else:
        age_hours = (
            datetime.now(timezone.utc) - generated_dt
        ).total_seconds() / 3600
        if age_hours > EDGE_PROFILE_MAX_AGE_HOURS:
            reasons.append(
                "edge_profile stale "
                f"age_hours={age_hours:.2f} > {EDGE_PROFILE_MAX_AGE_HOURS}"
            )

    clean_settled = int(profile.get("clean_settled_trades") or 0)
    modern_full = int(profile.get("modern_full_metadata_trades") or 0)
    normal_modern = int(profile.get("normal_council_approved_modern_trades") or 0)
    data_collection = int(profile.get("data_collection_override_count") or 0)
    bootstrap_provisional = int(profile.get("bootstrap_provisional_count") or 0)

    if clean_settled < EDGE_PROFILE_MIN_CLEAN_SETTLED:
        reasons.append(
            "sample too small "
            f"clean_settled={clean_settled} < {EDGE_PROFILE_MIN_CLEAN_SETTLED}"
        )
    if modern_full < EDGE_PROFILE_MIN_MODERN_FULL:
        reasons.append(
            "modern sample too small "
            f"modern_full_metadata={modern_full} < {EDGE_PROFILE_MIN_MODERN_FULL}"
        )
    if normal_modern < EDGE_PROFILE_MIN_NORMAL_MODERN:
        reasons.append(
            "normal council-approved modern sample too small "
            f"normal_modern={normal_modern} < {EDGE_PROFILE_MIN_NORMAL_MODERN}"
        )
    if modern_full > 0 and data_collection >= modern_full:
        reasons.append(
            "modern profile is data_collection_override only "
            f"data_collection={data_collection}/{modern_full}"
        )
    if modern_full > 0 and normal_modern <= 0 and bootstrap_provisional > 0:
        reasons.append(
            "modern profile has bootstrap provisional trades but no normal proof "
            f"bootstrap_provisional={bootstrap_provisional}/{modern_full}"
        )

    sample_ok = (
        clean_settled >= EDGE_PROFILE_MIN_CLEAN_SETTLED
        and modern_full >= EDGE_PROFILE_MIN_MODERN_FULL
        and normal_modern >= EDGE_PROFILE_MIN_NORMAL_MODERN
    )
    trusted = exists and bool(profile) and generated_dt is not None and sample_ok and not reasons

    return {
        "edge_profile_exists": exists,
        "edge_profile_age_hours": round(age_hours, 4) if age_hours is not None else None,
        "edge_profile_sample_ok": sample_ok,
        "edge_profile_trusted": trusted,
        "reason": "; ".join(reasons) if reasons else "edge_profile trusted",
        "clean_settled_trades": clean_settled,
        "modern_full_metadata_trades": modern_full,
        "normal_council_approved_modern_trades": normal_modern,
        "data_collection_override_count": data_collection,
        "bootstrap_provisional_count": bootstrap_provisional,
        "thresholds": {
            "max_age_hours": EDGE_PROFILE_MAX_AGE_HOURS,
            "min_clean_settled": EDGE_PROFILE_MIN_CLEAN_SETTLED,
            "min_modern_full": EDGE_PROFILE_MIN_MODERN_FULL,
            "min_normal_modern": EDGE_PROFILE_MIN_NORMAL_MODERN,
        },
    }
