"""Snapshot registry for ranked video/channel evidence.

Topic forward snapshots answer "did this topic break out later?" Evidence snapshots answer the
next question: "did the specific videos/channels we trusted point toward later breakouts?"
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path


EVIDENCE_SNAPSHOT_FIELDS = [
    "snapshot_id",
    "created_at",
    "label",
    "source",
    "evidence_type",
    "category_rank",
    "topic_rank",
    "topic",
    "topic_opportunity",
    "candidate_source",
    "evidence_score",
    "opportunity_evidence_score",
    "evidence_role",
    "title",
    "video_id",
    "video_url",
    "channel_title",
    "channel_id",
    "channel_url",
    "views_per_day",
    "views",
    "subs",
    "channel_trajectory_score",
    "status",
    "checked_at",
    "future_breakout_count",
    "notes",
]


def evidence_snapshot_path(out_dir: str, explicit: str | None = None) -> Path:
    return Path(explicit) if explicit else Path(out_dir) / "evidence-snapshots.csv"


def _snapshot_id(label: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")[:32] or "evidence"
    return f"{stamp}-{slug}"


def _fmt(x):
    if x is None:
        return ""
    if isinstance(x, float):
        return round(x, 4)
    return x


def rows_from_evidence(
    results: list[dict],
    label: str,
    source: str = "report",
    top_n: int = 10,
) -> list[dict]:
    created = dt.datetime.now(dt.timezone.utc).isoformat()
    sid = _snapshot_id(label)
    rows: list[dict] = []
    for topic_rank, topic_row in enumerate(results, 1):
        base = {
            "snapshot_id": sid,
            "created_at": created,
            "label": label,
            "source": source,
            "topic_rank": topic_rank,
            "topic": topic_row.get("topic"),
            "topic_opportunity": topic_row.get("opportunity"),
            "candidate_source": topic_row.get("candidate_source"),
            "status": "pending",
            "checked_at": "",
            "future_breakout_count": "",
            "notes": "",
        }
        for ev in (topic_row.get("video_evidence") or [])[:top_n]:
            rows.append({
                **base,
                "evidence_type": "video",
                "category_rank": ev.get("evidence_rank"),
                "evidence_score": ev.get("evidence_score"),
                "opportunity_evidence_score": (topic_row.get("opportunity") or 0.0) * (ev.get("evidence_score") or 0.0),
                "evidence_role": ev.get("evidence_role"),
                "title": ev.get("title"),
                "video_id": ev.get("video_id"),
                "video_url": ev.get("video_url"),
                "channel_title": ev.get("channel_title"),
                "channel_id": ev.get("channel_id"),
                "channel_url": ev.get("channel_url"),
                "views_per_day": ev.get("views_per_day"),
                "views": ev.get("views"),
                "subs": ev.get("subs"),
                "channel_trajectory_score": "",
            })
        for ev in (topic_row.get("channel_evidence") or [])[:top_n]:
            score = ev.get("channel_trajectory_score")
            rows.append({
                **base,
                "evidence_type": "channel",
                "category_rank": ev.get("channel_rank"),
                "evidence_score": ev.get("channel_evidence_score"),
                "opportunity_evidence_score": (topic_row.get("opportunity") or 0.0) * (score or ev.get("channel_evidence_score") or 0.0),
                "evidence_role": ev.get("best_evidence_role"),
                "title": ev.get("best_video_title"),
                "video_id": "",
                "video_url": ev.get("best_video_url"),
                "channel_title": ev.get("channel_title"),
                "channel_id": ev.get("channel_id"),
                "channel_url": ev.get("channel_url"),
                "views_per_day": ev.get("max_views_per_day"),
                "views": ev.get("total_views"),
                "subs": ev.get("subscribers"),
                "channel_trajectory_score": score,
            })
    rows.sort(
        key=lambda r: (
            float(r.get("opportunity_evidence_score") or 0.0),
            float(r.get("views_per_day") or 0.0),
        ),
        reverse=True,
    )
    for i, row in enumerate(rows, 1):
        row["category_rank"] = i
    return rows[: max(top_n, 1) * 2]


def append_evidence_snapshot_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EVIDENCE_SNAPSHOT_FIELDS)
        if not exists:
            w.writeheader()
        for row in rows:
            w.writerow({k: _fmt(row.get(k)) for k in EVIDENCE_SNAPSHOT_FIELDS})


def capture_evidence_snapshot(
    results: list[dict],
    out_dir: str,
    label: str,
    source: str = "report",
    top_n: int = 10,
    path: str | None = None,
) -> tuple[Path, int]:
    rows = rows_from_evidence(results, label, source=source, top_n=top_n)
    p = evidence_snapshot_path(out_dir, path)
    append_evidence_snapshot_rows(p, rows)
    return p, len(rows)
