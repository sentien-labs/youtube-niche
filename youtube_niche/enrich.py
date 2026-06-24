"""Turn raw search results into normalized records carrying views + subscriber counts.

Shared by the niche scorer (cli.py) and the domain scan (discover.py).
"""
from __future__ import annotations

import re

_DUR_RE = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def to_int(x, default=None):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def iso_duration_seconds(s: str | None) -> int | None:
    """Parse an ISO-8601 video duration (e.g. 'PT12M30S') to seconds; None if unparseable."""
    if not s:
        return None
    m = _DUR_RE.match(s)
    if not m:
        return None
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def enrich(client, search_items: list[dict], cfg) -> list[dict]:
    vids = [
        it["id"]["videoId"]
        for it in search_items
        if it.get("id", {}).get("videoId")
    ][: cfg.enrich_n]
    vmeta = client.videos(vids)
    chan_ids = list({m["snippet"]["channelId"] for m in vmeta.values()})
    cmeta = client.channels(chan_ids)

    records = []
    for vid in vids:  # preserve search rank order
        m = vmeta.get(vid)
        if not m:
            continue
        ch = cmeta.get(m["snippet"]["channelId"], {})
        ch_stats = ch.get("statistics", {})
        subs = None
        if not ch_stats.get("hiddenSubscriberCount") and ch_stats.get("subscriberCount") is not None:
            subs = to_int(ch_stats.get("subscriberCount"))
        records.append(
            {
                "video_id": vid,
                "title": m["snippet"]["title"],
                "channel_id": m["snippet"]["channelId"],
                "channel_title": m["snippet"].get("channelTitle", ""),
                "published_at": m["snippet"]["publishedAt"],
                "views": to_int(m.get("statistics", {}).get("viewCount"), 0),
                "subs": subs,
                "duration_s": iso_duration_seconds(m.get("contentDetails", {}).get("duration")),
                "lang": m["snippet"].get("defaultAudioLanguage") or m["snippet"].get("defaultLanguage"),
            }
        )
    return records
