"""
Shared strategy label helpers.
"""

from typing import Any


_BASE_STRATEGIES = frozenset({"SIGNAL", "TREND", "ARB"})


def normalize_strategy(strategy: Any) -> str:
    """
    Normalize strategy labels for risk limits and analytics.

    Examples:
        SIGNAL_CRYPTO -> SIGNAL
        TREND_CRYPTO  -> TREND
        ARB_CRYPTO    -> ARB
    """
    if strategy is None:
        return "UNKNOWN"

    value = str(strategy).strip().upper()
    if not value:
        return "UNKNOWN"

    if value in _BASE_STRATEGIES:
        return value

    prefix = value.split("_", 1)[0]
    if prefix in _BASE_STRATEGIES:
        return prefix

    return value
