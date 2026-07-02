"""Wayback Machine CDX backfill — reconstruct historical subscriber / view counts.

The YouTube Data API exposes only CURRENT cumulative counts (no history), which is why a
clean RETROSPECTIVE backtest of the niche scores was judged "structurally unobtainable from
the API alone": you can't know a channel's subs-at-publish or a video's view-velocity-at-publish.

The Internet Archive's Wayback Machine has crawled YouTube channel and watch pages for years,
and the archived HTML embeds the EXACT (un-rounded) ``subscriberCount`` / ``viewCount`` inside
the ``ytInitialData`` JSON (older pages carry "12,345 subscribers" / "123 views" text instead).
This module queries the free, key-less CDX index and parses those numbers, giving a leakage-free
point-in-time source — for any channel/video the Archive happened to snapshot near the date of
interest. Coverage is power-law by fame: mega and mid-size channels are well covered; long-tail
small channels are often snapshotted once or never. So this backfills the retrospective backtest
for the channels it *can* see, and we instrument the actual hit-rate rather than assume it.

No API key. Be polite: the CDX endpoint is rate-limited, so calls are throttled + cached.

Empirical status (tested 2026-06-26, 6 live-mined small finance breakout channels): 1/6 (17%)
had ANY archived subscriber snapshot at all. The one hit (a ~25.1k-sub channel) corroborated the
subs-at-publish estimator used elsewhere in this codebase — 22,400 archived subscribers vs 24,023
estimated. The other 5 channels (819-89,500 current subs) had zero archived /about-page snapshots
in the window searched. This matches the expected power-law-by-fame coverage described above: it
is a partial backfill for mid-size+ channels, NOT a validation shortcut for small-channel
populations, which is what this tool's breakout mining actually targets — the forward test
remains the cleanest leakage-free validation path for that population. The probe that produced
this hit-rate only tried the `/channel/{id}` and `/about` URL forms (see `channel_url_variants`
above for the fuller set, including `@handle`, `/c/`, and `/user/`, none of which were exercised
in this test), so 17% is a conservative floor, not a ceiling, on achievable coverage. One more
asymmetry worth noting: this path is ToS-advantaged relative to the rest of the tool — Internet
Archive data is not YouTube API data, so the YouTube API Services Developer Policies' ~30-day
retention limit (see `youtube_niche/retention.py`) does not apply to anything sourced from here.
"""
from __future__ import annotations

import datetime as dt
import re
import time

import requests

from .cache import Cache

CDX_URL = "http://web.archive.org/cdx/search/cdx"
_UA = "youtube-niche-research/0.1 (+https://github.com/sentien-labs/youtube-niche)"
_TS_FMT = "%Y%m%d%H%M%S"
_last_call = [0.0]
_MIN_INTERVAL = 0.5  # seconds between archive.org hits


def _throttle() -> None:
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def _parse_ts(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(ts, _TS_FMT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def channel_url_variants(channel_id: str | None, handle: str | None = None,
                         custom: str | None = None) -> list[str]:
    """Wayback keys snapshots by exact URL, so a channel may live under several forms."""
    urls: list[str] = []
    if channel_id:
        urls.append(f"youtube.com/channel/{channel_id}/about")
        urls.append(f"youtube.com/channel/{channel_id}")
    if handle:
        h = handle.lstrip("@")
        urls.append(f"youtube.com/@{h}")
        urls.append(f"youtube.com/c/{h}")
        urls.append(f"youtube.com/user/{h}")
    if custom:
        urls.append(f"youtube.com/c/{custom}")
        urls.append(f"youtube.com/user/{custom}")
    # dedupe preserving order
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def video_url(video_id: str) -> str:
    return f"youtube.com/watch?v={video_id}"


def cdx_snapshots(url: str, frm: dt.datetime | None = None, to: dt.datetime | None = None,
                  collapse: str = "timestamp:8", limit: int = 1000,
                  cache: Cache | None = None, session: requests.Session | None = None) -> list[dict]:
    """Return archived snapshots of ``url`` as [{ts, datetime, original, status}], 200s only.

    ``collapse=timestamp:8`` keeps at most one snapshot per day; ``:6`` = per month.
    """
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original,statuscode",
        "filter": "statuscode:200",
        "collapse": collapse,
        "limit": str(limit),
    }
    if frm:
        params["from"] = frm.strftime(_TS_FMT)
    if to:
        params["to"] = to.strftime(_TS_FMT)
    ckey = "cdx:" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    rows = None
    if cache is not None:
        rows = cache.get(ckey)
    if rows is None:
        _throttle()
        sess = session or requests
        try:
            r = sess.get(CDX_URL, params=params, headers={"User-Agent": _UA}, timeout=30)
            r.raise_for_status()
            rows = r.json()[1:]  # row 0 is the header
        except Exception:
            return []
        if cache is not None:
            cache.set(ckey, rows)
    out: list[dict] = []
    for row in rows:
        if len(row) < 3:
            continue
        ts, original, status = row[0], row[1], row[2]
        when = _parse_ts(ts)
        if when is None:
            continue
        out.append({"ts": ts, "datetime": when, "original": original, "status": status})
    return out


def nearest_snapshot(snaps: list[dict], target: dt.datetime,
                     max_gap_days: float | None = None) -> dict | None:
    """Pick the snapshot closest in time to ``target``; None if none within ``max_gap_days``."""
    if not snaps:
        return None
    best = min(snaps, key=lambda s: abs((s["datetime"] - target).total_seconds()))
    gap_days = abs((best["datetime"] - target).total_seconds()) / 86400.0
    if max_gap_days is not None and gap_days > max_gap_days:
        return None
    best = dict(best)
    best["gap_days"] = round(gap_days, 1)
    return best


def fetch_archived(ts: str, url: str, session: requests.Session | None = None) -> str | None:
    """Fetch the archived page HTML. ``id_`` raw mode avoids the Archive's rewrite banner."""
    archived = f"http://web.archive.org/web/{ts}id_/{url}"
    _throttle()
    sess = session or requests
    try:
        r = sess.get(archived, headers={"User-Agent": _UA}, timeout=45)
        r.raise_for_status()
        return r.text
    except Exception:
        return None


_SUBS_EXACT = re.compile(r'"subscriberCount"\s*:\s*"(\d+)"')
_VIEWS_EXACT = re.compile(r'"viewCount"\s*:\s*"(\d+)"')
# Display-text fallbacks (older pages, no ytInitialData), e.g. "12,345 subscribers", "1.2M subscribers"
_SUBS_TEXT = re.compile(r'([\d,.]+\s*[KMB]?)\s*subscribers', re.I)
_VIEWS_TEXT = re.compile(r'([\d,]+)\s*views', re.I)


def _denote(num: str) -> int | None:
    """Parse '1.2M' / '12,345' style numbers to int."""
    num = num.strip().replace(",", "")
    mult = 1
    if num and num[-1].upper() in "KMB":
        mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[num[-1].upper()]
        num = num[:-1]
    try:
        return int(float(num) * mult)
    except ValueError:
        return None


def parse_counts(html: str) -> dict:
    """Extract {subscriber_count, view_count, subscriber_exact, view_exact} from archived HTML.

    ``*_exact`` flag whether the value came from the un-rounded ytInitialData JSON (preferred)
    rather than rounded display text.
    """
    out: dict = {"subscriber_count": None, "view_count": None,
                 "subscriber_exact": False, "view_exact": False}
    if not html:
        return out
    m = _SUBS_EXACT.search(html)
    if m:
        out["subscriber_count"] = int(m.group(1))
        out["subscriber_exact"] = True
    else:
        m = _SUBS_TEXT.search(html)
        if m:
            out["subscriber_count"] = _denote(m.group(1))
    m = _VIEWS_EXACT.search(html)
    if m:
        out["view_count"] = int(m.group(1))
        out["view_exact"] = True
    else:
        m = _VIEWS_TEXT.search(html)
        if m:
            out["view_count"] = _denote(m.group(1).replace(",", ""))
    return out


def counts_at(url_variants: list[str], target: dt.datetime, window_days: float = 365,
              max_gap_days: float = 120, cache: Cache | None = None,
              session: requests.Session | None = None) -> dict | None:
    """Best-effort historical counts for a channel/video near ``target``.

    Tries each URL form, finds the archived snapshot nearest the target date (within
    ``max_gap_days``), fetches it, and parses the embedded counts. Returns None on total miss.
    """
    frm = target - dt.timedelta(days=window_days)
    to = target + dt.timedelta(days=window_days)
    for url in url_variants:
        snaps = cdx_snapshots(url, frm=frm, to=to, cache=cache, session=session)
        snap = nearest_snapshot(snaps, target, max_gap_days=max_gap_days)
        if snap is None:
            continue
        html = fetch_archived(snap["ts"], snap["original"], session=session)
        counts = parse_counts(html or "")
        if counts["subscriber_count"] is not None or counts["view_count"] is not None:
            counts.update({
                "url": url,
                "snapshot_ts": snap["ts"],
                "snapshot_datetime": snap["datetime"].isoformat(),
                "gap_days": snap["gap_days"],
                "n_snapshots": len(snaps),
            })
            return counts
    return None
