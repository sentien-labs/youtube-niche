"""Topic-to-title relevance scoring.

The first scorer used strict lexical overlap. That is explainable, but it misses common semantic
variants ("individual retirement account" vs "IRA", "offline" vs "local") and makes validation
look worse than the underlying research process. This module stays deterministic and offline:
semantic expansion first, lexical fallback always available, no hidden model calls.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .topics import topic_tokens
from .util import clamp01


@dataclass(frozen=True)
class RelevanceResult:
    score: float
    method: str
    overlap: int
    needed: int

    @property
    def relevant(self) -> bool:
        return self.method != "none" and self.score >= 0.62


SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("local", "locally", "offline", "selfhosted", "self", "hosted", "private"),
    ("cancel", "canceling", "cancelling", "abandon", "abandoned", "pause", "paused", "stop"),
    ("company", "companies", "ceo", "ceos", "business", "corporate", "enterprise"),
    ("retire", "retirement"),
    ("debt", "payoff", "repayment"),
    ("budget", "budgeting"),
    ("selling", "reselling"),
    ("reits", "reit"),
)

TOKEN_ALIASES: dict[str, set[str]] = {}
for group in SYNONYM_GROUPS:
    expanded = set(group)
    for token in group:
        TOKEN_ALIASES.setdefault(token, set()).update(expanded)

PHRASE_ALIASES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("individual", "retirement", "account"), ("ira",)),
    (("self", "hosted"), ("local", "offline")),
    (("on", "device"), ("local", "offline")),
    (("make", "money"), ("income", "earn")),
    (("social", "security"), ("ssa", "retirement")),
    (("real", "estate"), ("property", "rental", "reit")),
)

GENERIC_CONTEXT_TOKENS = {
    "ai", "tool", "tools", "business", "company", "companies", "finance", "money",
    "online", "real", "estate", "agent", "agents", "video", "make", "income",
}


def _expanded_tokens(text: str | None) -> set[str]:
    toks = set(topic_tokens(text))
    text_l = " ".join(str(text or "").lower().split())
    for phrase, aliases in PHRASE_ALIASES:
        if " ".join(phrase) in text_l:
            toks.update(aliases)
    for token in list(toks):
        toks.update(TOKEN_ALIASES.get(token, ()))
    return toks


def _needed(n_tokens: int) -> int:
    if n_tokens >= 4:
        return 3
    return max(1, min(2, n_tokens))


def relevance_score(topic: str | None, text: str | None) -> RelevanceResult:
    """Score whether ``text`` targets ``topic``.

    ``score`` is intentionally threshold-friendly rather than probabilistic. ``method`` is:
    - lexical: exact meaning-bearing token overlap was sufficient;
    - semantic: expanded aliases/phrases supplied the match;
    - fuzzy: canonical token strings were close enough;
    - none: no useful match.
    """
    topic_toks = topic_tokens(topic)
    text_toks = topic_tokens(text)
    if not topic_toks:
        return RelevanceResult(1.0, "empty-topic", 0, 0)
    needed = _needed(len(topic_toks))
    lexical_overlap = len(topic_toks & text_toks)
    distinctive = topic_toks - GENERIC_CONTEXT_TOKENS
    distinctive_overlap = len(distinctive & text_toks)
    lexical_score = clamp01(lexical_overlap / needed)
    has_distinctive_anchor = (
        len(topic_toks) < 3
        or not distinctive
        or distinctive_overlap > 0
    )
    if has_distinctive_anchor and lexical_score >= 1.0:
        return RelevanceResult(1.0, "lexical", lexical_overlap, needed)

    topic_sem = _expanded_tokens(topic)
    text_sem = _expanded_tokens(text)
    # Count original topic concepts covered by expanded text, not every alias token. Otherwise
    # one match like "local" can inflate into many overlapping aliases.
    semantic_overlap = len(topic_toks & text_sem)
    # Require enough exact/expanded overlap, but discount pure synonym matches so broad aliases
    # like "business" do not mark everything as relevant.
    semantic_needed = _needed(len(topic_toks))
    semantic_score = clamp01((0.75 * semantic_overlap + 0.25 * lexical_overlap) / semantic_needed)
    if (
        has_distinctive_anchor
        and lexical_overlap > 0
        and semantic_overlap >= semantic_needed
        and semantic_score >= 0.62
    ):
        return RelevanceResult(semantic_score, "semantic", semantic_overlap, semantic_needed)

    topic_key = " ".join(sorted(topic_toks))
    text_key = " ".join(sorted(text_toks))
    fuzzy = SequenceMatcher(None, topic_key, text_key).ratio() if topic_key and text_key else 0.0
    fuzzy_score = 0.70 * lexical_score + 0.30 * fuzzy
    if has_distinctive_anchor and fuzzy_score >= 0.62:
        return RelevanceResult(clamp01(fuzzy_score), "fuzzy", lexical_overlap, needed)

    return RelevanceResult(lexical_score, "none", lexical_overlap, needed)


def is_relevant(topic: str | None, text: str | None) -> bool:
    return relevance_score(topic, text).relevant
