"""
brain/decision_council.py
-------------------------
Standalone debate layer for signal review.

Builder proposes a small confidence boost.  Critic evaluates the adjusted
signal.  Council returns one combined decision.  This module is not integrated
into paper trading execution.
"""

import json
import sys
from typing import Any, Optional
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.builder_brain import suggest_signal_improvement
from brain.critic_brain import critique_signal


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_confidence(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _error_view(module: str, exc: Exception) -> dict[str, Any]:
    return {
        "error": True,
        "module": module,
        "reason": f"{module} error: {exc}",
    }


def decide_signal(signal: dict) -> dict:
    """
    Combine Builder Brain and Critic Brain into one decision.

    Builder never overrides Critic.  If Critic blocks, final decision blocks.
    If either module errors, this function returns a structured result instead
    of raising.  If both modules fail, it allows for paper mode only.
    """
    base_confidence = _as_float(signal.get("confidence"), 0.0)
    adjusted_signal = dict(signal)

    builder_view: dict[str, Any]
    critic_view: dict[str, Any]
    builder_error: Optional[Exception] = None
    critic_error: Optional[Exception] = None

    try:
        builder_view = suggest_signal_improvement(signal)
    except Exception as exc:
        builder_error = exc
        builder_view = {
            "confidence_boost": 0.0,
            "reason": f"Builder error: {exc}",
            "error": True,
        }

    builder_boost = _as_float(builder_view.get("confidence_boost"), 0.0)
    adjusted_confidence = _clamp_confidence(base_confidence + builder_boost)
    adjusted_signal["confidence"] = adjusted_confidence

    try:
        critic_view = critique_signal(adjusted_signal)
    except Exception as exc:
        critic_error = exc
        critic_view = _error_view("Critic", exc)

    critic_decision = str(critic_view.get("decision", "")).upper()
    critic_adjustment = _as_float(critic_view.get("confidence_adjustment"), 0.0)
    net_adjustment = builder_boost + critic_adjustment
    final_confidence = _clamp_confidence(base_confidence + net_adjustment)

    if critic_decision == "BLOCK":
        final_decision = "BLOCK"
        reason = (
            "Critic blocked adjusted signal. "
            f"Builder: {builder_view.get('reason', '')}. "
            f"Critic: {critic_view.get('reason', '')}"
        )
    elif critic_decision == "ALLOW":
        final_decision = "ALLOW"
        if builder_boost > 0:
            reason = (
                "Builder found positive historical pattern and Critic allowed. "
                f"Builder: {builder_view.get('reason', '')}. "
                f"Critic: {critic_view.get('reason', '')}"
            )
        else:
            reason = (
                "No Builder boost; Critic allowed with caution. "
                f"Builder: {builder_view.get('reason', '')}. "
                f"Critic: {critic_view.get('reason', '')}"
            )
    elif critic_error and not builder_error:
        final_decision = "ALLOW"
        reason = (
            "Critic error; fail-safe allowing paper-mode signal after Builder review. "
            f"Builder: {builder_view.get('reason', '')}. "
            f"Critic: {critic_view.get('reason', '')}"
        )
    elif builder_error and critic_error:
        final_decision = "ALLOW"
        reason = (
            "Builder and Critic both errored; fail-safe ALLOW for paper mode only. "
            f"Builder: {builder_view.get('reason', '')}. "
            f"Critic: {critic_view.get('reason', '')}"
        )
    else:
        final_decision = "ALLOW"
        reason = (
            "Council defaulted to ALLOW for paper mode. "
            f"Builder: {builder_view.get('reason', '')}. "
            f"Critic: {critic_view.get('reason', '')}"
        )

    builder_positive = builder_boost > 0 and not builder_error
    critic_allows = critic_decision == "ALLOW"
    agreement = (
        (final_decision == "ALLOW" and critic_allows)
        or (final_decision == "BLOCK" and not builder_positive)
    )

    return {
        "builder_view": builder_view,
        "critic_view": critic_view,
        "agreement": bool(agreement),
        "final_decision": final_decision,
        "final_confidence": round(final_confidence, 6),
        "net_confidence_adjustment": round(net_adjustment, 6),
        "reason": reason,
    }


if __name__ == "__main__":
    sample_signal = {
        "confidence": 0.75,
        "edge": 0.30,
        "ticker": "ETH",
        "strategy": "default",
        "market_type": "CRYPTO",
        "action": "BET_YES",
    }
    print(json.dumps(decide_signal(sample_signal), indent=2, sort_keys=True))
