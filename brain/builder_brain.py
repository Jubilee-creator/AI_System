"""
brain/builder_brain.py
----------------------
Standalone historical edge builder.

Reads data/edge_profile.json and suggests small confidence boosts for signals
that match historically strong patterns.  This module does not block trades
and is not integrated into execution.
"""

import json
import sys
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from brain.strategy_utils import normalize_strategy

DEFAULT_PROFILE_PATH = ROOT / "data" / "edge_profile.json"
MAX_CONFIDENCE_BOOST = 0.10


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


def _score_bucket(label: str, key: str, bucket: Optional[dict[str, Any]]) -> tuple[float, str]:
    if not bucket:
        return 0.0, ""

    trades = int(bucket.get("trades", 0))
    win_rate = float(bucket.get("win_rate", 0.0))
    total_pnl = float(bucket.get("total_pnl", 0.0))
    avg_pnl = float(bucket.get("avg_pnl", 0.0))

    if trades < 3:
        return (
            0.0,
            f"ignored: sample too small ({label} {key}, trades={trades} < 3)",
        )

    if total_pnl <= 0 or avg_pnl <= 0 or win_rate < 0.50:
        return 0.0, ""

    if trades < 5:
        return (
            0.02,
            "weak boost: moderate evidence "
            f"({label} {key}, trades={trades}, "
            f"win_rate={win_rate:.4f}, total_pnl={total_pnl:.2f})",
        )

    if win_rate >= 0.60 and total_pnl > 0:
        boost = 0.05
        if win_rate >= 0.70:
            boost += 0.02
        if total_pnl >= 10:
            boost += 0.02
        return (
            min(boost, MAX_CONFIDENCE_BOOST),
            "strong boost: high confidence historical pattern "
            f"({label} {key}, trades={trades}, "
            f"win_rate={win_rate:.4f}, total_pnl={total_pnl:.2f})",
        )

    return 0.0, ""


def suggest_signal_improvement(
    signal: dict[str, Any],
    profile_path: Path = DEFAULT_PROFILE_PATH,
) -> dict[str, Any]:
    """
    Suggest a small confidence boost for historically strong signal patterns.

    Args:
        signal: Dict with confidence, edge, ticker, strategy, and market_type.
        profile_path: Path to data/edge_profile.json.

    Returns:
        {
          "confidence_boost": number,
          "reason": "..."
        }
    """
    profile = load_edge_profile(profile_path)
    if not profile:
        return {
            "confidence_boost": 0.0,
            "reason": f"No edge profile data available: {profile_path}",
        }

    confidence = _as_float(signal.get("confidence"))
    edge = _as_float(signal.get("edge"))
    ticker = _normalize(signal.get("ticker"))
    strategy = normalize_strategy(signal.get("strategy"))
    market_type = _market_type(signal)
    conf_key = confidence_bucket(confidence)
    edge_key = edge_bucket(edge)

    checks = [
        ("confidence bucket", conf_key, _bucket(profile, "by_confidence_bucket", conf_key)),
        ("edge bucket", edge_key, _bucket(profile, "by_edge_bucket", edge_key)),
        ("ticker", ticker, _bucket(profile, "by_ticker", ticker)),
        ("strategy", strategy, _bucket(profile, "by_strategy", strategy)),
        ("market_type", market_type, _bucket(profile, "by_market_type", market_type)),
    ]

    boost = 0.0
    reasons: list[str] = []
    ignored_reasons: list[str] = []
    for label, key, bucket in checks:
        bucket_boost, reason = _score_bucket(label, key, bucket)
        if bucket_boost > 0:
            boost += bucket_boost
            reasons.append(reason)
        elif reason.startswith("ignored: sample too small"):
            ignored_reasons.append(reason)

    boost = min(boost, MAX_CONFIDENCE_BOOST)
    if boost <= 0:
        return {
            "confidence_boost": 0.0,
            "reason": "; ".join(ignored_reasons)
            if ignored_reasons else "No historically strong matching pattern found",
        }

    return {
        "confidence_boost": round(boost, 4),
        "reason": "; ".join(reasons + ignored_reasons),
    }


def build_signal(signal: dict[str, Any], profile_path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    """Alias for callers that prefer build_* naming."""
    return suggest_signal_improvement(signal, profile_path)


if __name__ == "__main__":
    demo_signal = {
        "confidence": 0.734,
        "edge": 0.034,
        "ticker": "KXETHD-26APR2817-T2259.99",
        "strategy": "SIGNAL_CRYPTO",
        "market_type": "CRYPTO",
    }
    print(json.dumps(suggest_signal_improvement(demo_signal), indent=2, sort_keys=True))
