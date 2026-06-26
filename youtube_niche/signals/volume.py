"""Signal — absolute demand from view velocity and recent successful uploads.

The crucial complement to the outlier ratio (signal A): a high views÷subs ratio on tiny
absolute view counts is a low-demand backwater, not an opportunity. This measures real view
velocity (per day, so old and new videos compare fairly).
"""
from __future__ import annotations

import datetime as dt
from statistics import median

from ..channel_size import is_small_channel_at_publish
from ..util import clamp01, saturating

VPD_KNEE = 500.0  # median views/day that maps to a volume score of ~0.5


def views_per_day(v: dict, now) -> float | None:
    try:
        pub = dt.datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))
        return v["views"] / max((now - pub).days, 1)
    except Exception:
        return None


def _percentile(vals: list[float], pct: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * pct
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def volume_score(
    videos: list[dict],
    knee: float = VPD_KNEE,
    min_views: int = 1000,
    recent_days: int = 180,
    recent_success_knee: float = 4.0,
    small_channel_subs: int = 50000,
    now: dt.datetime | None = None,
    velocity_now: dt.datetime | None = None,
):
    """Returns (median-volume score in [0,1], detail with p75/recent/newcomer demand scores).

    ``now`` is the decision-point clock used for recency classification (is a video "recent"?).
    ``velocity_now`` is the clock used to turn cumulative views into views/day and to interpret
    current subscriber counts for publish-time size estimates. It differs from ``now`` only in
    as-of/backtest mode: ``v["views"]`` and ``v["subs"]`` are current, so dividing or prorating
    them against ``as_of`` over-states velocity/size. Defaults to ``now`` so live scoring is
    unchanged.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    vnow = velocity_now or now
    credible = [v for v in videos if v["views"] >= min_views]
    vpds_by_video = [(v, views_per_day(v, vnow)) for v in credible]
    vpds = [x for _, x in vpds_by_video if x is not None]
    if not vpds:
        return 0.0, {
            "median_vpd": None,
            "p75_vpd": None,
            "median_views": None,
            "recent_success_count": 0,
            "recent_demand": 0.0,
            "p75_volume": 0.0,
            "newcomer_vpd": None,
            "newcomer_volume": None,
            "newcomer_sample": 0,
            "demand_gate": 0.0,  # no credible videos -> fully gated
        }
    mv = median(vpds)
    p75_vpd = _percentile(vpds, 0.75) or mv

    # Newcomer ceiling: views/day that SMALL channels achieve here — the realistic ceiling for
    # a creator starting from zero. High overall demand carried only by giants is not capturable.
    small_vpds = [
        vpd
        for v, vpd in vpds_by_video
        if vpd is not None
        and v.get("subs") is not None
        and v["subs"] > 0
        and is_small_channel_at_publish(v, small_channel_subs, vnow)
    ]
    newcomer_vpd = median(small_vpds) if small_vpds else None
    newcomer_volume = saturating(newcomer_vpd, knee) if newcomer_vpd is not None else None

    recent_cutoff = now - dt.timedelta(days=recent_days)
    recent_success = 0
    for v, vpd in vpds_by_video:
        if vpd is None:
            continue
        try:
            pub = dt.datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if pub >= recent_cutoff and vpd >= knee:
            recent_success += 1
    return saturating(mv, knee), {
        "median_vpd": mv,
        "p75_vpd": p75_vpd,
        "median_views": int(median(v["views"] for v in credible)),
        "p75_volume": saturating(p75_vpd, knee),
        "newcomer_vpd": newcomer_vpd,
        "newcomer_volume": newcomer_volume,
        "newcomer_sample": len(small_vpds),
        "recent_success_count": recent_success,
        "recent_demand": saturating(recent_success, recent_success_knee),
        "demand_gate": clamp01(mv / knee),
    }
