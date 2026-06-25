"""Keyless fixtures: a tiny canned domain + a fake YouTube client.

`python -m youtube_niche.backtest --fixtures` replays this through the *real* scoring and backtest
pipeline with no API key and no quota — so contributors can improve the scoring/validation logic
without YouTube credentials. The data is synthetic but realistic (public-style titles, plausible
view/subscriber counts); it is for exercising code paths, not for drawing conclusions about niches.
"""
from __future__ import annotations

import datetime as dt
import re

from .domains import Domain

_STOP = {"the", "a", "to", "of", "for", "in", "on", "how", "best", "your", "and", "with", "vs", "my"}


def _days_ago(n: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=n)).isoformat().replace("+00:00", "Z")


# (video_id, title, views, subs, age_days, duration_iso)
_VIDEOS = [
    ("d1", "Dividend Growth Investing for Beginners (2026)", 240_000, 18_000, 40, "PT12M30S"),
    ("d2", "My Dividend Growth Portfolio Update", 180_000, 9_000, 55, "PT9M10S"),
    ("d3", "Rent vs Buy a House in 2026 — The Real Math", 310_000, 12_000, 30, "PT14M02S"),
    ("d4", "Rent vs Buy: Why I Changed My Mind", 150_000, 6_500, 70, "PT8M44S"),
    ("d5", "Backdoor Roth IRA Explained Step by Step", 520_000, 1_200_000, 120, "PT11M00S"),
    ("d6", "HSA Investing: The Triple Tax Advantage", 95_000, 22_000, 95, "PT10M20S"),
    ("d7", "I-Bonds in 2026: Still Worth It?", 60_000, 40_000, 160, "PT7M30S"),
    ("d8", "Underconsumption Core: How I Save 60%", 280_000, 7_800, 25, "PT13M15S"),
    ("d9", "Index Funds vs ETFs — Which Wins?", 4_300_000, 3_100_000, 80, "PT15M40S"),
    ("d10", "Roth Conversion Ladder for Early Retirement", 88_000, 15_000, 140, "PT12M00S"),
]

_VMETA = {
    vid: {
        "id": vid,
        "snippet": {
            "title": title,
            "channelId": "ch_" + vid,
            "channelTitle": f"Channel {vid}",
            "publishedAt": _days_ago(age),
            "defaultAudioLanguage": "en",
        },
        "contentDetails": {"duration": dur},
        "statistics": {"viewCount": str(views)},
    }
    for (vid, title, views, subs, age, dur) in _VIDEOS
}
_CMETA = {
    "ch_" + vid: {"id": "ch_" + vid, "statistics": {"subscriberCount": str(subs)}}
    for (vid, title, views, subs, age, dur) in _VIDEOS
}


def fixture_domain() -> Domain:
    """A synthetic high-CPM domain whose breakouts live in `_VIDEOS`."""
    return Domain(
        name="demo finance (fixtures)",
        terms=["dividend investing", "rent vs buy", "roth ira", "index funds"],
        cpm_low=15,
        cpm_high=40,
        subtopics=[
            "dividend growth investing",
            "rent vs buy a house",
            "backdoor roth ira",
            "hsa investing",
            "i bonds",
            "underconsumption habits",
            "roth conversion ladder",
            "index funds for beginners",
        ],
        volume_knee_vpd=100.0,
    )


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2 and t not in _STOP}


class FixtureClient:
    """A fake YouTubeClient: serves canned responses, never touches the network or quota."""

    def __init__(self):
        self.cache = None
        self._searches = 0

    def search(self, q, max_results=30, order=None, published_after=None, published_before=None,
               region=None, relevance_language=None):
        self._searches += 1
        qt = _tokens(q)
        ranked = sorted(
            _VIDEOS,
            key=lambda v: (len(_tokens(v[1]) & qt), v[2]),  # title overlap, then views
            reverse=True,
        )
        matches = [v for v in ranked if _tokens(v[1]) & qt] or ranked
        items = [{"id": {"videoId": v[0]}} for v in matches[:max_results]]
        return {"items": items, "pageInfo": {"totalResults": len(items)}}

    def videos(self, ids):
        return {vid: _VMETA[vid] for vid in ids if vid in _VMETA}

    def channels(self, ids):
        return {cid: _CMETA[cid] for cid in ids if cid in _CMETA}

    def comment_threads(self, video_id, pages=2):
        return []

    # quota surface used by the pipelines
    def search_calls_remaining(self):
        return 10_000

    def units_remaining(self):
        return 10_000_000

    def search_calls_used(self):
        return self._searches

    def units_spent(self):
        return 0
