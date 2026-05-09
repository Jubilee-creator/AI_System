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
    MIN_EDGE,
    BOOTSTRAP_PROVISIONAL_MODE,
    BOOTSTRAP_MIN_EDGE,
    BOOTSTRAP_MIN_CONFIDENCE,
    BOOTSTRAP_ALLOW_ENABLED,
    BOOTSTRAP_CONFIDENCE_ADJUSTMENT,
    PRICE_CONDITIONED_COUNCIL_ENABLED,
    PRICE_CONDITIONED_MIN_N,
    PRICE_CONDITIONED_MIN_WR,
)

DEFAULT_PROFILE_PATH = ROOT / "data" / "edge_profile.json"
MIN_SAMPLE_SIZE = 5
# MIN_EDGE (from config) is used as the normal proof threshold — see usage below.
MAX_SMALL_SAMPLE_ADJUSTMENT = -0.15


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


def _price_bucket_critic(price: Optional[float]) -> Optional[str]:
    """Price bucket matching build_edge_profile._price_bucket exactly."""
    if price is None:
        return None
    if price < 0.10: return "<0.10"
    if price < 0.20: return "0.10-0.20"
    if price < 0.30: return "0.20-0.30"
    if price < 0.40: return "0.30-0.40"
    if price < 0.50: return "0.40-0.50"
    if price < 0.60: return "0.50-0.60"
    if price < 0.70: return "0.60-0.70"
    if price < 0.80: return "0.70-0.80"
    if price < 0.90: return "0.80-0.90"
    return "0.90-1.00"


def _price_conditioned_override(
    signal: dict[str, Any],
    profile: dict[str, Any],
    edge_key: str,
    health: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """
    Check whether the 2D (original_edge × price) cell justifies an ALLOW
    even though the 1D edge bucket is contaminated.

    Returns an ALLOW result dict if the 2D cell is strongly positive, or
    None to defer to the normal 1D bucket-checks path.

    Only fires when:
      - PRICE_CONDITIONED_COUNCIL_ENABLED is True
      - signal contains yes_ask in a known price bucket
      - the 2D table exists in the profile
      - the 2D cell meets n >= MIN_N, win_rate >= MIN_WR, total_pnl > 0
    """
    if not PRICE_CONDITIONED_COUNCIL_ENABLED:
        return None

    yes_ask = _as_float(signal.get("yes_ask"))
    if yes_ask is None:
        return None

    price_key = _price_bucket_critic(yes_ask)
    if price_key is None:
        return None

    cell_key = f"{edge_key}|{price_key}"
    cell = (
        profile.get("profiles", {})
        .get("by_edge_price_bucket", {})
        .get(cell_key)
    )
    if cell is None:
        return None

    n   = int(cell.get("trades", 0))
    wr  = float(cell.get("win_rate", 0.0))
    pnl = float(cell.get("total_pnl", 0.0))

    if n < PRICE_CONDITIONED_MIN_N or wr < PRICE_CONDITIONED_MIN_WR or pnl <= 0:
        return None

    return {
        "decision": "ALLOW",
        "reason": (
            f"price_conditioned_council_allow: "
            f"1D edge bucket '{edge_key}' is contaminated "
            f"but 2D cell '{cell_key}' shows positive evidence: "
            f"n={n}, win_rate={wr:.3f}, total_pnl={pnl:+.2f} "
            f"(normal_modern non-KXETH trades only)"
        ),
        "confidence_adjustment": 0.0,
        "price_conditioned_override": True,
        "price_conditioned_cell": cell_key,
        "edge_profile_health": health,
    }


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
        normal_modern = health.get("normal_council_approved_modern_trades", 0)
        signal_edge = _as_float(signal.get("edge"))
        signal_conf = _as_float(signal.get("confidence"))
        meets_bootstrap = (
            signal_edge is not None and signal_edge >= BOOTSTRAP_MIN_EDGE
            and signal_conf is not None and signal_conf >= BOOTSTRAP_MIN_CONFIDENCE
        )

        # Phase 6B-2: bootstrap_era_allow path.  When BOOTSTRAP_ALLOW_ENABLED,
        # signals that meet the quality bar return ALLOW (not PROVISIONAL).
        # These trades count as normal_council_approved_modern and accumulate
        # toward the 10-trade trust gate, breaking the circular deadlock.
        if meets_bootstrap and BOOTSTRAP_ALLOW_ENABLED:
            return {
                "decision": "ALLOW",
                "reason": (
                    f"bootstrap_era_council_allow: edge_profile untrusted "
                    f"(normal_modern={normal_modern}); "
                    f"signal edge={signal_edge:.4f} >= {BOOTSTRAP_MIN_EDGE} "
                    f"and confidence={signal_conf:.3f} >= {BOOTSTRAP_MIN_CONFIDENCE}; "
                    "counted as normal council-approved for proof accumulation"
                ),
                "confidence_adjustment": BOOTSTRAP_CONFIDENCE_ADJUSTMENT,
                "bootstrap_era_allow": True,
                "edge_profile_health": health,
            }

        # Fallback provisional path (BOOTSTRAP_ALLOW_ENABLED=False): only fires
        # when normal_modern == 0 to avoid permanently blocking the system.
        if (
            BOOTSTRAP_PROVISIONAL_MODE
            and normal_modern == 0
            and meets_bootstrap
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

    # Phase 9A: 2D price-conditioned early-return override.
    # Only runs when the 1D edge bucket would fire _bad_enough_sample (i.e.,
    # the bucket is large enough to be considered but has negative PnL).
    # If the 2D (original_edge × price) cell is strongly positive, return
    # ALLOW immediately, bypassing the contaminated 1D bucket.
    # confidence_adjustment=0 preserves the pre-council scanner edge.
    _1d_edge_bkt = _bucket(profile, "by_edge_bucket", edge_key)
    if _bad_enough_sample(_1d_edge_bkt):
        _pc = _price_conditioned_override(signal, profile, edge_key, health)
        if _pc is not None:
            return _pc

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
        and adjusted_edge < MIN_EDGE
        and caution_reasons
    ):
        block_reasons.append(
            "blocked: small-sample confidence reduction would put adjusted edge "
            f"{adjusted_edge:.4f} below normal threshold {MIN_EDGE:.4f}"
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
