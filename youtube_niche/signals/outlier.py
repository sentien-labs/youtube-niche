"""Signal A — outlier ratio (views / subscribers).

A video with views far above its channel's subscriber count suggests the topic carried
it, not the channel's audience. This is beatability/portability context, not demand.
"""
from __future__ import annotations

from ..util import saturating


def outlier_score(videos: list[dict], knee: float = 1.0, min_views: int = 1000):
    """videos: dicts with 'views' and 'subs'. Returns (score in [0,1], detail)."""
    ratios = []
    unknown_subs = 0
    for v in videos:
        if v["views"] < min_views:
            continue
        subs = v.get("subs")
        if subs is None:
            unknown_subs += 1
            continue
        try:
            subs = int(subs)
        except (TypeError, ValueError):
            unknown_subs += 1
            continue
        if subs <= 0:
            unknown_subs += 1
            continue
        ratios.append(v["views"] / subs)
    if not ratios:
        return 0.0, {"max_ratio": 0.0, "mean_top3_ratio": 0.0, "n": 0, "unknown_subs": unknown_subs}
    ratios.sort(reverse=True)
    top = ratios[:3]  # the strongest topic-carried hits, not the long tail
    mean_top = sum(top) / len(top)
    return saturating(mean_top, knee), {
        "max_ratio": round(ratios[0], 2),
        "mean_top3_ratio": round(mean_top, 2),
        "n": len(ratios),
        "unknown_subs": unknown_subs,
    }
