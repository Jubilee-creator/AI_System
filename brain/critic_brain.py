"""
brain/critic_brain.py
---------------------
Standalone historical edge critic.

Reads data/edge_profile.json and evaluates a proposed signal against clean
settled trade history.  This module is not integrated into trading execution.
"""

import json
import sys
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from brain.strategy_utils import normalize_strategy
from brain.edge_profile_health import edge_profile_health
from config.trading_config import (
    BOOTSTRAP_PROVISIONAL_MODE,
    BOOTSTRAP_MIN_EDGE,
    BOOTSTRAP_MIN_CONFIDENCE,
)

DEFAULT_PROFILE_PATH = ROOT / "data" / "edge_profile.json"
MIN_SAMPLE_SIZE = 5
MIN_NORMAL_EDGE = 0.03
MAX_SMALL_SAMPLE_ADJUSTMENT = -0.15
BOOTSTRAP_CONFIDENCE_ADJUSTMENT = -0.05  # conservative penalty for provisional mode


def confidence_bucket(confidence: Optional[float]) -> str:
    if confidence is None:
        return "unknown"
    if confidence < 0.65:
        return "<0.65"
    if confidence < 0.70:
        return "0.65-0.70"
    if confidence < 0.75:
        return "0.70-0.75"
    if confidence < 0.80:
        return "0.75-0.80"
    if confidence < 0.90:
        return "0.80-0.90"
    return ">=0.90"


def edge_bucket(edge: Optional[float]) -> str:
    if edge is None:
        return "unknown"
    if edge < 0.03:
        return "<0.03"
    if edge < 0.05:
        return "0.03-0.05"
    if edge < 0.10:
        return "0.05-0.10"
    if edge < 0.25:
        return "0.10-0.25"
    if edge < 0.50:
        return "0.25-0.50"
    return ">=0.50"


def load_edge_profile(path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize(value: Any, default: str = "UNKNOWN") -> str:
    if value is None or value == "":
        return default
    return str(value).upper()


def _market_type(signal: dict[str, Any]) -> str:
    explicit = signal.get("market_type") or signal.get("event_type")
    if explicit:
        return _normalize(explicit)

    strategy = _normalize(signal.get("strategy"), "")
    if "_" in strategy:
        suffix = strategy.rsplit("_", 1)[-1]
        if suffix:
            return suffix

    ticker = _normalize(signal.get("ticker"), "")
    if any(token in ticker for token in ("BTC", "ETH", "XRP", "SOL", "DOGE")):
        return "CRYPTO"
    if any(token in ticker for token in ("NBA", "NFL", "MLB", "NHL", "WNBA", "NCAA")):
        return "SPORTS"
    if any(token in ticker for token in ("PRES", "SENATE", "HOUSE", "TRUMP", "BIDEN")):
        return "POLITICS"
    return "OTHER"


def _bucket(profile: dict[str, Any], group: str, key: str) -> Optional[dict[str, Any]]:
    return profile.get("profiles", {}).get(group, {}).get(key)


def _is_losing(bucket: Optional[dict[str, Any]]) -> bool:
    return bool(bucket and float(bucket.get("total_pnl", 0.0)) < 0)


def _is_sparse(bucket: Optional[dict[str, Any]]) -> bool:
    return bucket is None or int(bucket.get("trades", 0)) < MIN_SAMPLE_SIZE


def _trades(bucket: Optional[dict[str, Any]]) -> int:
    return int(bucket.get("trades", 0)) if bucket else 0


def _win_rate(bucket: Optional[dict[str, Any]]) -> Optional[float]:
    if not bucket:
        return None
    return float(bucket.get("win_rate", 0.0))


def _total_pnl(bucket: Optional[dict[str, Any]]) -> float:
    return float(bucket.get("total_pnl", 0.0)) if bucket else 0.0


def _bad_enough_sample(bucket: Optional[dict[str, Any]]) -> bool:
    if _trades(bucket) < MIN_SAMPLE_SIZE:
        return False
    win_rate = _win_rate(bucket)
    return bool((win_rate is not None and win_rate < 0.40) or _total_pnl(bucket) < 0)


def _bad_any_sample(bucket: Optional[dict[str, Any]]) -> bool:
    if _trades(bucket) <= 0:
        return False
    win_rate = _win_rate(bucket)
    return bool((win_rate is not None and win_rate < 0.40) or _total_pnl(bucket) < 0)


def _format_reason(reasons: list[str]) -> str:
    if reasons:
        return "; ".join(reasons)
    return "Historical edge profile passed"


def critique_signal(
    signal: dict[str, Any],
    profile_path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    """
    Evaluate a signal against historical edge profile buckets.

    Args:
        signal: Dict with confidence, edge, ticker, strategy, and market_type.
        profile_path: Path to data/edge_profile.json.

    Returns:
        {
          "decision": "ALLOW" or "BLOCK",
          "reason": "...",
          "confidence_adjustment": number
        }
    """
    profile = load_edge_profile(profile_path)
    health = edge_profile_health(profile, profile_path)
    if not profile:
        return {
            "decision": "BLOCK",
            "reason": (
                "edge_profile_untrusted: "
                f"{health['reason']}; no normal historical approval"
            ),
            "confidence_adjustment": 0.0,
            "edge_profile_health": health,
        }

    if not health["edge_profile_trusted"]:
        # Bootstrap provisional path: when the system has zero normal-approved
        # modern trades and the signal meets a higher quality bar, allow a
        # PROVISIONAL decision instead of hard BLOCK.  These trades are NOT
        # counted as normal proof — they exist only to escape the bootstrap
        # deadlock and collect preliminary evidence.
        normal_modern = health.get("normal_council_approved_modern_trades", 0)
        signal_edge = _as_float(signal.get("edge"))
        signal_conf = _as_float(signal.get("confidence"))
        if (
            BOOTSTRAP_PROVISIONAL_MODE
            and normal_modern == 0
            and signal_edge is not None and signal_edge >= BOOTSTRAP_MIN_EDGE
            and signal_conf is not None and signal_conf >= BOOTSTRAP_MIN_CONFIDENCE
        ):
            return {
                "decision": "PROVISIONAL",
                "reason": (
                    f"edge_profile_untrusted_bootstrap_candidate: "
                    f"profile has 0 normal-approved modern trades; "
                    f"signal edge={signal_edge:.4f} >= {BOOTSTRAP_MIN_EDGE} "
                    f"and confidence={signal_conf:.3f} >= {BOOTSTRAP_MIN_CONFIDENCE}; "
                    "provisional for bootstrap data collection — NOT normal proof"
                ),
                "confidence_adjustment": BOOTSTRAP_CONFIDENCE_ADJUSTMENT,
                "bootstrap_provisional": True,
                "edge_profile_health": health,
            }
        return {
            "decision": "BLOCK",
            "reason": (
                "edge_profile_untrusted: "
                f"{health['reason']}; no normal historical approval"
            ),
            "confidence_adjustment": 0.0,
            "edge_profile_health": health,
        }

    confidence = _as_float(signal.get("confidence"))
    edge = _as_float(signal.get("edge"))
    ticker = _normalize(signal.get("ticker"))
    strategy = normalize_strategy(signal.get("strategy"))
    market_type = _market_type(signal)
    conf_key = confidence_bucket(confidence)
    edge_key = edge_bucket(edge)

    conf_profile = _bucket(profile, "by_confidence_bucket", conf_key)
    edge_profile = _bucket(profile, "by_edge_bucket", edge_key)
    ticker_profile = _bucket(profile, "by_ticker", ticker)
    strategy_profile = _bucket(profile, "by_strategy", strategy)
    market_profile = _bucket(profile, "by_market_type", market_type)

    block_reasons: list[str] = []
    caution_reasons: list[str] = []
    confidence_adjustment = 0.0

    bucket_checks = [
        ("confidence bucket", conf_key, conf_profile, True),
        ("edge bucket", edge_key, edge_profile, True),
        ("ticker", ticker, ticker_profile, True),
        ("strategy", strategy, strategy_profile, False),
        ("market_type", market_type, market_profile, False),
    ]

    bad_categories: list[str] = []

    for label, key, bucket, primary in bucket_checks:
        trades = _trades(bucket)
        win_rate = _win_rate(bucket)
        total_pnl = _total_pnl(bucket)

        if _bad_any_sample(bucket):
            bad_categories.append(
                f"{label} {key} trades={trades} "
                f"win_rate={(win_rate or 0.0):.4f} total_pnl={total_pnl:.2f}"
            )

        if primary and _bad_enough_sample(bucket):
            block_reasons.append(
                "blocked: enough sample confirms losing bucket "
                f"({label} {key}, trades={trades}, "
                f"win_rate={(win_rate or 0.0):.4f}, total_pnl={total_pnl:.2f})"
            )
            continue

        if _is_sparse(bucket):
            detail = (
                "small sample: confidence reduced, not blocked "
                f"({label} {key}, trades={trades} < {MIN_SAMPLE_SIZE})"
            )
            caution_reasons.append(detail)
            if _is_losing(bucket):
                confidence_adjustment -= 0.10 if primary else 0.05
            else:
                confidence_adjustment -= 0.05

    if len(bad_categories) >= 3 and not block_reasons:
        block_reasons.append(
            "blocked: multiple losing categories agree "
            f"({'; '.join(bad_categories[:3])})"
        )

    confidence_adjustment = max(confidence_adjustment, MAX_SMALL_SAMPLE_ADJUSTMENT)
    adjusted_edge = edge + confidence_adjustment if edge is not None else None
    action = _normalize(signal.get("action"), "")
    if (
        adjusted_edge is not None
        and action != "ARB"
        and adjusted_edge < MIN_NORMAL_EDGE
        and caution_reasons
    ):
        block_reasons.append(
            "blocked: small-sample confidence reduction would put adjusted edge "
            f"{adjusted_edge:.4f} below normal threshold {MIN_NORMAL_EDGE:.4f}"
        )

    if block_reasons:
        return {
            "decision": "BLOCK",
            "reason": _format_reason(block_reasons),
            "confidence_adjustment": round(confidence_adjustment, 4),
            "edge_profile_health": health,
        }

    return {
        "decision": "ALLOW",
        "reason": _format_reason(caution_reasons),
        "confidence_adjustment": round(confidence_adjustment, 4),
        "edge_profile_health": health,
    }


def evaluate_signal(
    signal: dict[str, Any],
    profile_path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    """Alias for callers that prefer evaluate_* naming."""
    return critique_signal(signal, profile_path)


if __name__ == "__main__":
    demo_signal = {
        "confidence": 0.70,
        "edge": 0.05,
        "ticker": "UNKNOWN",
        "strategy": "UNKNOWN",
        "market_type": "UNKNOWN",
    }
    print(json.dumps(critique_signal(demo_signal), indent=2, sort_keys=True))
