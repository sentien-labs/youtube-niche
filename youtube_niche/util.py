"""Small numeric helpers shared across signals. All scores live in [0, 1]."""
from __future__ import annotations


def clamp01(x: float | None) -> float:
    """Clamp to [0, 1]; None/NaN -> 0.0."""
    if x is None or x != x:  # x != x catches NaN
        return 0.0
    return max(0.0, min(1.0, float(x)))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    try:
        if not b:
            return default
        return a / b
    except (TypeError, ZeroDivisionError):
        return default


def saturating(x: float | None, knee: float) -> float:
    """Map [0, inf) -> [0, 1) with a soft knee.

    x == knee -> 0.5, then asymptotes toward 1. Good for "more is better but with
    diminishing returns" quantities (outlier ratios, request counts, ages).
    """
    if x is None or x < 0:
        return 0.0
    if knee <= 0:
        return 1.0 if x > 0 else 0.0
    return x / (x + knee)
