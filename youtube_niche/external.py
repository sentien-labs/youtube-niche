"""Optional external keyword metrics import.

The YouTube API does not expose search volume or RPM. This module lets users bring a small CSV
from any keyword/RPM provider and blend it into scoring without making that provider required.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .topics import canonical_topic, topic_similarity
from .util import clamp01, saturating


@dataclass(frozen=True)
class ExternalMetric:
    topic: str
    demand_score: float | None = None
    cpm_score: float | None = None
    cpm: float | None = None
    monthly_searches: float | None = None
    source: str = "external_csv"


def _float(row: dict, *names: str) -> float | None:
    for name in names:
        raw = row.get(name)
        if raw in (None, ""):
            continue
        try:
            return float(str(raw).replace(",", "").strip())
        except ValueError:
            continue
    return None


@lru_cache(maxsize=8)
def load_external_metrics(path: str | None) -> tuple[ExternalMetric, ...]:
    """Load optional keyword metrics from CSV. Missing/invalid files yield no metrics."""
    if not path:
        return ()
    p = Path(path)
    if not p.exists():
        return ()
    rows: list[ExternalMetric] = []
    with p.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            normalized = {str(k).strip().lower(): v for k, v in row.items() if k}
            topic = (
                normalized.get("topic")
                or normalized.get("keyword")
                or normalized.get("query")
                or normalized.get("term")
            )
            if not topic:
                continue
            monthly = _float(normalized, "monthly_searches", "search_volume", "volume", "monthly_volume")
            demand_score = _float(normalized, "demand_score", "volume_score", "search_score")
            if demand_score is None and monthly is not None:
                demand_score = saturating(monthly, 10_000.0)
            cpm = _float(normalized, "cpm", "rpm", "estimated_cpm", "estimated_rpm")
            cpm_score = _float(normalized, "cpm_score", "rpm_score")
            if cpm_score is None and cpm is not None:
                cpm_score = clamp01(cpm / 40.0)
            rows.append(
                ExternalMetric(
                    topic=str(topic),
                    demand_score=clamp01(demand_score) if demand_score is not None else None,
                    cpm_score=clamp01(cpm_score) if cpm_score is not None else None,
                    cpm=cpm,
                    monthly_searches=monthly,
                    source=normalized.get("source") or p.name,
                )
            )
    return tuple(rows)


def match_external_metric(topic: str, path: str | None, threshold: float = 0.78) -> ExternalMetric | None:
    """Return the closest external metric for a topic, preferring exact canonical matches."""
    metrics = load_external_metrics(path)
    if not metrics:
        return None
    key = canonical_topic(topic)
    exact = [m for m in metrics if canonical_topic(m.topic) == key]
    if exact:
        return exact[0]
    best = max(metrics, key=lambda m: topic_similarity(topic, m.topic))
    return best if topic_similarity(topic, best.topic) >= threshold else None
