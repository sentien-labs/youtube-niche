"""Signals B, C, D — the supply gap.

B supply_age      — older top results = staler supply = bigger opening
C competition     — fewer credible results = thinner supply
D small_channel   — small channels ranking = beatable supply
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from statistics import median

from ..relevance import relevance_score
from ..topics import topic_tokens
from ..util import clamp01, saturating

MIN_DENSITY_SAMPLE = 15


def _age_days(published_iso: str, now: dt.datetime | None = None) -> float | None:
    try:
        d = dt.datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        now = now or dt.datetime.now(dt.timezone.utc)
        return max(0.0, (now - d).days)
    except Exception:
        return None


def _views_per_day(v: dict, now: dt.datetime | None = None) -> float | None:
    try:
        age = max(_age_days(v["published_at"], now=now) or 0.0, 1.0)
        return v["views"] / age
    except Exception:
        return None


def _topic_tokens(topic: str | None) -> set[str]:
    return topic_tokens(topic)


def _title_relevant(v: dict, required_tokens: set[str]) -> bool:
    if not required_tokens:
        return True
    title_tokens = topic_tokens(str(v.get("title", "")))
    if not title_tokens:
        return False
    overlap = len(required_tokens & title_tokens)
    return overlap >= max(1, min(2, len(required_tokens)))


def filter_relevant_videos(videos: list[dict], topic: str | None) -> list[dict]:
    """Keep videos whose title appears to target the topic.

    Uses semantic relevance with lexical fallback; see ``youtube_niche.relevance``.
    """
    return [v for v in videos if relevance_score(topic, str(v.get("title", ""))).relevant]


def supply_scores(
    videos: list[dict],
    total_results: int,
    small_channel_subs: int = 50000,
    competition_knee: float = 30.0,
    age_knee_days: float = 365.0,
    min_views: int = 1000,
    topic: str | None = None,
    recent_days: int = 180,
    recent_supply_knee: float = 8.0,
    min_small_channel_vpd: float = 50.0,
    now: dt.datetime | None = None,
):
    """Returns ({competition_gap, age_gap, small_channel_gap}, detail). Each gap in [0,1]."""
    raw_credible = [v for v in videos if v["views"] >= min_views]
    relevance_results = [relevance_score(topic, str(v.get("title", ""))) for v in raw_credible]
    credible = [v for v, rel in zip(raw_credible, relevance_results) if rel.relevant]
    n_credible = len(credible)

    # C — fewer credible results => bigger gap. A dense top-result sample is also
    # competitive, but only when there are enough sampled results and likely unseen results.
    sample_size = len(videos)
    credible_density = n_credible / sample_size if sample_size else 0.0
    count_strength = clamp01(n_credible / competition_knee) if competition_knee > 0 else 1.0
    density_strength = (
        credible_density
        if sample_size >= MIN_DENSITY_SAMPLE and total_results > sample_size
        else 0.0
    )
    competition_gap = 1.0 - clamp01(max(count_strength, density_strength))

    recent_credible = [
        v for v in credible
        if (_age_days(v["published_at"], now=now) is not None and _age_days(v["published_at"], now=now) <= recent_days)
    ]
    recent_supply_gap = 1.0 - clamp01(len(recent_credible) / recent_supply_knee)

    # Authority concentration — if one/few channels dominate a sufficiently populated result
    # page, the supply is less beatable even when raw result count looks modest.
    channel_keys = [
        str(v.get("channel_id") or v.get("channel_title") or "").strip()
        for v in credible
        if str(v.get("channel_id") or v.get("channel_title") or "").strip()
    ]
    channel_counts = Counter(channel_keys)
    authority_gap = None
    top_channel_share = None
    top3_channel_share = None
    dominant_channel = None
    if n_credible >= 5 and channel_counts:
        shares = [c / n_credible for _, c in channel_counts.most_common()]
        top_channel_share = shares[0]
        top3_channel_share = sum(shares[:3])
        dominant_channel = channel_counts.most_common(1)[0][0]
        authority_strength = clamp01(((top3_channel_share or 0.0) - 0.35) / 0.55)
        authority_gap = 1.0 - authority_strength

    # B — older median top result => bigger gap
    age_source = credible if credible else videos
    ages = [a for a in (_age_days(v["published_at"], now=now) for v in age_source) if a is not None]
    med_age = median(ages) if ages else 0.0
    age_gap = saturating(med_age, age_knee_days)

    # D — prevalence of small channels among credible results with meaningful view velocity.
    known_subs = [v for v in credible if v.get("subs") is not None]
    if known_subs:
        successful_known = [
            v for v in known_subs
            if (_views_per_day(v, now=now) is not None and _views_per_day(v, now=now) >= min_small_channel_vpd)
        ]
        if successful_known:
            small = sum(1 for v in successful_known if v["subs"] <= small_channel_subs)
            small_frac = small / len(successful_known)
        else:
            small_frac = 0.0
    else:
        small_frac = None

    detail = {
        "credible_results": n_credible,
        "raw_credible_results": len(raw_credible),
        "sampled_results": sample_size,
        "credible_density": round(credible_density, 2),
        "title_match_frac": round((n_credible / len(raw_credible)), 2) if raw_credible else None,
        "semantic_title_match_frac": round(
            (sum(1 for r in relevance_results if r.method in {"semantic", "fuzzy"} and r.relevant) / len(raw_credible)),
            2,
        ) if raw_credible else None,
        "avg_relevance_score": round(
            sum(r.score for r in relevance_results) / len(relevance_results),
            2,
        ) if relevance_results else None,
        "recent_credible_results": len(recent_credible),
        "reported_total": total_results,  # YouTube's totalResults is unreliable; kept for context
        "median_age_days": round(med_age),
        "known_subscriber_results": len(known_subs),
        "unknown_subscriber_results": n_credible - len(known_subs),
        "small_channel_frac": round(small_frac, 2) if small_frac is not None else None,
        "unique_credible_channels": len(channel_counts) if channel_counts else None,
        "top_channel_share": round(top_channel_share, 2) if top_channel_share is not None else None,
        "top3_channel_share": round(top3_channel_share, 2) if top3_channel_share is not None else None,
        "dominant_channel": dominant_channel,
    }
    return {
        "competition_gap": competition_gap,
        "authority_gap": authority_gap,
        "recent_supply_gap": recent_supply_gap,
        "age_gap": age_gap,
        "small_channel_gap": clamp01(small_frac) if small_frac is not None else None,
    }, detail
