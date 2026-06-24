"""Combine sub-scores into one explainable opportunity score.

opportunity = confidence × demand_gate × geomean(demand, supply_gap, monetization, quality_gap)

Missing optional axes are dropped from the raw geometric mean, but missing evidence lowers
confidence so a thinly measured topic does not look as trustworthy as a fully measured one.
"""
from __future__ import annotations

import math

from .util import clamp01


def _weighted(parts: list[tuple[float | None, float]]) -> float | None:
    """Weighted mean over present (non-None) values, weights renormalized. None if all absent."""
    present = [(v, w) for v, w in parts if v is not None]
    total_w = sum(w for _, w in present)
    if total_w <= 0:
        return None
    return clamp01(sum(v * w for v, w in present) / total_w)


def _geomean(parts: list[tuple[float | None, float]]) -> float | None:
    """Weighted geometric mean — enforces 'needs all of them'; one low axis drags the result."""
    present = [(max(v, 1e-3), w) for v, w in parts if v is not None]
    total_w = sum(w for _, w in present)
    if total_w <= 0:
        return None
    return clamp01(math.exp(sum(w * math.log(v) for v, w in present) / total_w))


def confidence_score(parts: list[tuple[float | None, float]]) -> float:
    """Weighted evidence coverage. Missing parts count as zero instead of renormalizing away."""
    total_w = sum(w for _, w in parts if w > 0)
    if total_w <= 0:
        return 0.0
    return clamp01(sum(clamp01(v) * w for v, w in parts if w > 0) / total_w)


def opportunity_score(sub: dict, weights) -> dict:
    """sub: normalized sub-scores (values may be None). Returns components + total."""
    # Demand = absolute interest only (volume + rising trend + explicit requests). The outlier
    # ratio is deliberately excluded — it's beatability, not demand (high ratio on tiny views ≠ demand).
    demand = _weighted(
        [
            (sub.get("volume"), weights.volume),
            (sub.get("newcomer_volume"), weights.newcomer_volume),
            (sub.get("p75_volume"), weights.p75_volume),
            (sub.get("recent_demand"), weights.recent_demand),
            (sub.get("trends"), weights.trends),
            (sub.get("comment_demand"), weights.comment_demand),
            (sub.get("external_demand"), getattr(weights, "external_demand", 0.0)),
        ]
    )
    supply_gap = _weighted(
        [
            (sub.get("competition_gap"), weights.competition),
            (sub.get("authority_gap"), getattr(weights, "authority", 0.0)),
            (sub.get("recent_supply_gap"), weights.recent_supply),
            (sub.get("age_gap"), weights.supply_age),
            (sub.get("small_channel_gap"), weights.small_channel),
        ]
    )
    quality_gap = sub.get("quality_gap")  # already 0..1 or None
    monetization = sub.get("cpm_score")

    # Geometric so a low score on one core axis cannot be hidden by another.
    base = _geomean(
        [
            (demand, weights.demand),
            (supply_gap, weights.supply_gap),
            (monetization, weights.monetization),
            (quality_gap, weights.quality_gap),
        ]
    )
    # demand_gate/confidence default to 1.0 (ungated) only when truly absent; an explicit
    # None (e.g. no credible videos) is treated as 0.0 by clamp01.
    dg = sub.get("demand_gate")
    demand_gate = clamp01(dg) if "demand_gate" in sub else 1.0
    conf = sub.get("confidence")
    confidence = clamp01(conf) if "confidence" in sub else 1.0
    raw = None if base is None else clamp01(base * demand_gate)
    total = None if raw is None else clamp01(raw * confidence)
    return {
        "opportunity": total,
        "opportunity_raw": raw,
        "opportunity_base": base,
        "demand": demand,
        "supply_gap": supply_gap,
        "cpm_score": monetization,
        "quality_gap": quality_gap,
        "demand_gate": demand_gate,
        "confidence": confidence,
    }
