"""Per-video and per-channel evidence rows for scored topics."""
from __future__ import annotations

import datetime as dt
from statistics import mean, median

from .relevance import relevance_score
from .util import clamp01, saturating

VIDEO_EVIDENCE_FIELDS = [
    "topic",
    "evidence_rank",
    "evidence_role",
    "evidence_score",
    "title",
    "video_id",
    "video_url",
    "channel_title",
    "channel_id",
    "channel_url",
    "views",
    "subs",
    "views_per_day",
    "age_days",
    "published_at",
    "duration_s",
    "relevant",
    "relevance_score",
    "relevance_method",
    "small_channel",
    "views_per_sub",
    "demand_score",
    "beatability_score",
    "newcomer_proof_score",
]

CHANNEL_EVIDENCE_FIELDS = [
    "topic",
    "channel_rank",
    "channel_title",
    "channel_id",
    "channel_url",
    "subscribers",
    "sampled_videos",
    "relevant_videos",
    "newcomer_breakout_videos",
    "repeat_breakout_rate",
    "niche_specificity",
    "recent_uploads",
    "upload_span_days",
    "total_views",
    "median_views_per_day",
    "max_views_per_day",
    "max_views_per_sub",
    "channel_evidence_score",
    "channel_trajectory_score",
    "best_evidence_role",
    "best_video_title",
    "best_video_url",
]


def _age_days(published_iso: str | None, now: dt.datetime | None = None) -> float | None:
    if not published_iso:
        return None
    try:
        pub = dt.datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
        now = now or dt.datetime.now(dt.timezone.utc)
        return max(0.0, (now - pub).days)
    except Exception:
        return None


def _views_per_day(views: int | float | None, age_days: float | None) -> float | None:
    if views is None or age_days is None:
        return None
    return float(views) / max(age_days, 1.0)


def _video_url(video_id: str | None) -> str | None:
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else None


def _channel_url(channel_id: str | None) -> str | None:
    return f"https://www.youtube.com/channel/{channel_id}" if channel_id else None


def _round(x):
    if x is None:
        return None
    if isinstance(x, float):
        return round(x, 4)
    return x


def _score_recent_activity(recent_uploads: int, span_days: float | None) -> float:
    if recent_uploads <= 0:
        return 0.0
    if span_days is None or span_days <= 0:
        return min(recent_uploads / 3.0, 1.0)
    cadence = recent_uploads / max(span_days / 30.0, 1.0)
    return saturating(cadence, 1.0)


def video_evidence_rows(
    topic: str,
    videos: list[dict],
    cfg,
    *,
    volume_knee: float | None = None,
    now: dt.datetime | None = None,
    velocity_now: dt.datetime | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Rank sampled videos by how strongly they prove demand is capturable.

    ``now`` is the decision-point clock (shown as ``age_days``). ``velocity_now`` is the clock used
    for views/day; in as-of/backtest mode it should be the real wall-clock so current cumulative
    views are not divided by a shorter past window. Defaults to ``now``.
    """
    knee = volume_knee or getattr(cfg, "volume_knee_vpd", 500.0)
    vnow = velocity_now or now
    rows: list[dict] = []

    for v in videos:
        views = v.get("views") or 0
        subs = v.get("subs")
        age = _age_days(v.get("published_at"), now=now)
        vpd = _views_per_day(views, _age_days(v.get("published_at"), now=vnow))
        rel = relevance_score(topic, v.get("title"))
        relevant = rel.relevant
        small = subs is not None and 0 < subs <= cfg.small_channel_subs
        views_per_sub = (views / subs) if subs else None
        demand_score = saturating(vpd, knee)
        beatability_score = saturating(views_per_sub, cfg.outlier_knee) if views_per_sub else 0.0

        newcomer_proof_score = 0.0
        if relevant and small and vpd is not None and vpd >= cfg.min_small_channel_vpd:
            newcomer_proof_score = clamp01(
                0.55 * demand_score
                + 0.30 * beatability_score
                + 0.15
            )
        demand_leader_score = demand_score if relevant else 0.0

        if newcomer_proof_score >= 0.50:
            role = "newcomer_breakout"
        elif relevant and demand_leader_score >= 0.50:
            role = "demand_leader"
        elif relevant:
            role = "relevant_supply"
        else:
            role = "off_topic_sample"
        evidence_score = max(newcomer_proof_score, demand_leader_score * 0.70)

        rows.append({
            "topic": topic,
            "evidence_rank": 0,
            "evidence_role": role,
            "evidence_score": _round(evidence_score),
            "title": v.get("title"),
            "video_id": v.get("video_id"),
            "video_url": _video_url(v.get("video_id")),
            "channel_title": v.get("channel_title"),
            "channel_id": v.get("channel_id"),
            "channel_url": _channel_url(v.get("channel_id")),
            "views": views,
            "subs": subs,
            "views_per_day": _round(vpd),
            "age_days": _round(age),
            "published_at": v.get("published_at"),
            "duration_s": v.get("duration_s"),
            "relevant": relevant,
            "relevance_score": _round(rel.score),
            "relevance_method": rel.method,
            "small_channel": small if subs is not None else None,
            "views_per_sub": _round(views_per_sub),
            "demand_score": _round(demand_score),
            "beatability_score": _round(beatability_score),
            "newcomer_proof_score": _round(newcomer_proof_score),
        })

    rows.sort(
        key=lambda r: (
            float(r.get("evidence_score") or 0.0),
            float(r.get("views_per_day") or 0.0),
            int(r.get("views") or 0),
        ),
        reverse=True,
    )
    for i, row in enumerate(rows, 1):
        row["evidence_rank"] = i
    return rows[:limit] if limit else rows


def channel_evidence_rows(
    topic: str,
    video_rows: list[dict],
    *,
    limit: int | None = None,
) -> list[dict]:
    """Aggregate video evidence to channel-level proof rows."""
    grouped: dict[tuple[str | None, str | None], list[dict]] = {}
    for row in video_rows:
        key = (row.get("channel_id"), row.get("channel_title"))
        grouped.setdefault(key, []).append(row)

    out: list[dict] = []
    for (channel_id, channel_title), rows in grouped.items():
        relevant = [r for r in rows if r.get("relevant")]
        breakout = [r for r in relevant if r.get("evidence_role") == "newcomer_breakout"]
        best = max(
            rows,
            key=lambda r: (
                float(r.get("evidence_score") or 0.0),
                float(r.get("views_per_day") or 0.0),
            ),
        )
        scores = [float(r.get("evidence_score") or 0.0) for r in relevant]
        vpds = [float(r.get("views_per_day") or 0.0) for r in rows if r.get("views_per_day") is not None]
        ages = [float(r.get("age_days") or 0.0) for r in rows if r.get("age_days") is not None]
        subscribers = [r.get("subs") for r in rows if r.get("subs") is not None]
        repeat_breakout_rate = len(breakout) / max(len(relevant), 1) if relevant else 0.0
        niche_specificity = len(relevant) / max(len(rows), 1)
        recent_uploads = sum(1 for r in rows if (r.get("age_days") is not None and float(r.get("age_days") or 0) <= 180))
        upload_span = (max(ages) - min(ages)) if len(ages) >= 2 else (ages[0] if ages else None)
        channel_evidence_score = mean(scores) if scores else 0.0
        recent_activity = _score_recent_activity(recent_uploads, upload_span)
        consistency = saturating(median(vpds) if vpds else None, 100.0)
        trajectory_score = clamp01(
            0.35 * channel_evidence_score
            + 0.25 * repeat_breakout_rate
            + 0.20 * niche_specificity
            + 0.12 * recent_activity
            + 0.08 * consistency
        )
        out.append({
            "topic": topic,
            "channel_rank": 0,
            "channel_title": channel_title,
            "channel_id": channel_id,
            "channel_url": _channel_url(channel_id),
            "subscribers": max(subscribers) if subscribers else None,
            "sampled_videos": len(rows),
            "relevant_videos": len(relevant),
            "newcomer_breakout_videos": len(breakout),
            "repeat_breakout_rate": _round(repeat_breakout_rate),
            "niche_specificity": _round(niche_specificity),
            "recent_uploads": recent_uploads,
            "upload_span_days": _round(upload_span),
            "total_views": sum(int(r.get("views") or 0) for r in rows),
            "median_views_per_day": _round(median(vpds) if vpds else None),
            "max_views_per_day": _round(max((r.get("views_per_day") or 0.0) for r in rows)),
            "max_views_per_sub": _round(max((r.get("views_per_sub") or 0.0) for r in rows)),
            "channel_evidence_score": _round(channel_evidence_score),
            "channel_trajectory_score": _round(trajectory_score),
            "best_evidence_role": best.get("evidence_role"),
            "best_video_title": best.get("title"),
            "best_video_url": best.get("video_url"),
        })

    out.sort(
        key=lambda r: (
            float(r.get("channel_trajectory_score") or 0.0),
            int(r.get("newcomer_breakout_videos") or 0),
            float(r.get("max_views_per_day") or 0.0),
        ),
        reverse=True,
    )
    for i, row in enumerate(out, 1):
        row["channel_rank"] = i
    return out[:limit] if limit else out
