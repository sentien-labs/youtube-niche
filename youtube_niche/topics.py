"""Topic normalization, similarity, and lightweight clustering helpers."""
from __future__ import annotations

import re
from collections.abc import Iterable

TOKEN_RE = re.compile(r"[a-z0-9]+")

STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with", "without",
    "how", "what", "why", "when", "where", "best", "top", "guide", "tips", "tutorial",
    "explained", "complete", "ultimate", "beginner", "beginners", "review", "reviews",
    "vs", "versus", "your", "you", "is", "are", "this", "that", "my", "video", "videos",
    "want", "here", "bare", "minimum", "setup", "build", "quietly",
    "2024", "2025", "2026",
}

TOKEN_NORMALIZATIONS = {
    "canceled": "cancel",
    "cancelled": "cancel",
    "canceling": "cancel",
    "cancelling": "cancel",
    "locally": "local",
    "retired": "retire",
    "retiring": "retire",
}


def normalize_token(token: str) -> str:
    """Small, explainable normalization for topic/relevance matching."""
    token = TOKEN_NORMALIZATIONS.get(token, token)
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and not token.endswith(("ses", "xes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return TOKEN_NORMALIZATIONS.get(token, token)


def topic_tokens(text: str | None) -> set[str]:
    """Return meaning-bearing normalized tokens for rough niche comparisons."""
    if not text:
        return set()
    return {
        normalize_token(t)
        for t in TOKEN_RE.findall(text.lower())
        if len(t) > 1 and t not in STOPWORDS
    }


def canonical_topic(text: str | None) -> str:
    """Stable lexical key for a topic, useful for exact and near-exact duplicates."""
    return " ".join(sorted(topic_tokens(text)))


def topic_similarity(a: str | None, b: str | None) -> float:
    """Conservative token similarity in [0,1] for deduping near-identical niches."""
    ta, tb = topic_tokens(a), topic_tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    containment = len(ta & tb) / min(len(ta), len(tb))
    # Reward reordered equivalents and contained variants, but keep broad overlap conservative.
    if ta == tb:
        return 1.0
    return max(jaccard, 0.85 * containment if min(len(ta), len(tb)) >= 3 else 0.0)


def dedupe_topics(topics: Iterable[str], threshold: float = 0.78) -> list[str]:
    """Keep the first representative of each near-duplicate topic cluster."""
    kept: list[str] = []
    seen_keys: set[str] = set()
    for topic in topics:
        clean = " ".join(str(topic).split())
        if not clean:
            continue
        key = canonical_topic(clean)
        if key in seen_keys:
            continue
        if any(topic_similarity(clean, prior) >= threshold for prior in kept):
            continue
        seen_keys.add(key)
        kept.append(clean)
    return kept


def dedupe_ranked_rows(rows: list[dict], threshold: float = 0.78) -> list[dict]:
    """Collapse near-duplicate ranked rows, preserving the best row and recording cluster context."""
    kept: list[dict] = []
    for row in rows:
        topic = row.get("topic")
        match = None
        for prior in kept:
            if topic_similarity(topic, prior.get("topic")) >= threshold:
                match = prior
                break
        if match is None:
            clone = dict(row)
            clone["cluster_size"] = max(int(clone.get("cluster_size") or 1), 1)
            clone["cluster_topics"] = clone.get("cluster_topics") or topic
            kept.append(clone)
        else:
            topics = [t for t in str(match.get("cluster_topics") or "").split("; ") if t]
            if topic and topic not in topics:
                topics.append(topic)
            match["cluster_topics"] = "; ".join(topics)
            match["cluster_size"] = max(int(match.get("cluster_size") or 1), len(topics))
    return kept
