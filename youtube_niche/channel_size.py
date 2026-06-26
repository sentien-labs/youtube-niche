"""Helpers for reasoning about channel size at video publish time."""
from __future__ import annotations

import datetime as dt


def subs_at_publish_est(v: dict, now: dt.datetime) -> int | None:
    """Estimate the channel's subscriber count when the video was published.

    YouTube exposes current subscribers, not historical snapshots. If channel creation time is
    known, prorate current subscribers by channel age at publish vs. channel age now. This is
    crude, but it avoids excluding channels that were tiny when a breakout video was posted and
    grew because of that video. Falls back to current subscribers when dates are missing.
    """
    subs = v.get("subs")
    if subs is None:
        return None
    try:
        subs = int(subs)
    except (TypeError, ValueError):
        return None
    created_iso, pub_iso = v.get("channel_published_at"), v.get("published_at")
    if not created_iso or not pub_iso:
        return subs
    try:
        created = dt.datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        posted = dt.datetime.fromisoformat(pub_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return subs
    try:
        channel_age_now = (now - created).total_seconds()
        age_at_publish = (posted - created).total_seconds()
    except TypeError:
        return subs
    if channel_age_now <= 0:
        return subs
    frac = max(0.0, min(1.0, age_at_publish / channel_age_now))
    return int(subs * frac)


def is_small_channel_at_publish(v: dict, cap: int, now: dt.datetime) -> bool:
    """True when the channel was plausibly at or below ``cap`` subscribers at publish."""
    est = subs_at_publish_est(v, now)
    return est is not None and est <= cap


def publish_time_sub_denominator(v: dict, now: dt.datetime) -> int | None:
    """Subscriber denominator for views/sub ratios, using publish-time estimate when possible."""
    current = v.get("subs")
    if current is None:
        return None
    try:
        if int(current) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    est = subs_at_publish_est(v, now)
    if est is None:
        return None
    return max(int(est), 1)
