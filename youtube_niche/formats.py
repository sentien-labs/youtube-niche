"""Viewer-facing enrichments: title format classification + positioning readout.

Both functions here are pure and deterministic — no network, no LLM — so they're cheap to run
over every breakout title and safe to unit test exhaustively.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------- format classifier
# Order matters: listicle is checked before explainer (a numbered how-to reads as a listicle),
# and story ("How I...") is checked before explainer ("how to...") — see classify_format.

# Leading or embedded count patterns: "7 ways...", "Top 5 ...", "6 Wealth Tiers...",
# "N things/rules/mistakes/habits/tiers/ways/steps/signs/lessons/reasons".
_LISTICLE_NOUNS = (
    "ways", "way", "things", "thing", "rules", "rule", "mistakes", "mistake", "habits", "habit",
    "tiers", "tier", "steps", "step", "signs", "sign", "lessons", "lesson", "reasons", "reason",
    "tips", "tip", "secrets", "secret", "types", "type", "levels", "level", "stages", "stage",
)
_LISTICLE_LEADING_RE = re.compile(r"^\s*(top\s+)?\d+\s+\S", re.IGNORECASE)
_LISTICLE_EMBEDDED_RE = re.compile(
    r"\b\d+\s+(?:" + "|".join(_LISTICLE_NOUNS) + r")\b", re.IGNORECASE
)
_LISTICLE_TOP_N_RE = re.compile(r"\btop\s+\d+\b", re.IGNORECASE)

# First-person journey markers: "How I ...", "I tried ...", "I spent ...", "My ... journey",
# "Why I quit ...". Story beats explainer so "How I built X" doesn't read as an explainer.
_STORY_RE = re.compile(
    r"^\s*how\s+i\b"          # "How I spend my 66 LPA salary"
    r"|^\s*why\s+i\b"          # "Why I quit my job"
    r"|\bi\s+tried\b"
    r"|\bi\s+spent\b"
    r"|\bi\s+quit\b"
    r"|\bi\s+built\b"
    r"|\bi\s+left\b"
    r"|\bi\s+became\b"
    r"|\bmy\s+\w+(?:\s+\w+){0,3}\s+journey\b",
    re.IGNORECASE,
)

# Event/announcement markers.
_NEWS_RE = re.compile(
    r"\bannounc(?:ed|es|ing)\b"
    r"|\bis\s+over\b"
    r"|\bjust\s+happened\b"
    r"|\bbreaking\b"
    r"|\bcancel(?:ed|led|ing|ling)\b"
    r"|\bnew\s+\S+\s+launch(?:ed)?\b"
    r"|\blaunch(?:es|ed|ing)?\b",
    re.IGNORECASE,
)

# Instructional/analytical markers.
_EXPLAINER_RE = re.compile(
    r"\bhow\s+to\b"
    r"|\bwhy\b"
    r"|\bwhat\s+is\b"
    r"|\bexplained\b"
    r"|\bguide\b"
    r"|\btutorial\b"
    r"|\bvs\.?\b",
    re.IGNORECASE,
)

FORMATS = ("listicle", "explainer", "story", "news", "other")


def _is_listicle(title: str) -> bool:
    if _LISTICLE_LEADING_RE.search(title):
        return True
    if _LISTICLE_EMBEDDED_RE.search(title):
        return True
    if _LISTICLE_TOP_N_RE.search(title):
        return True
    return False


def classify_format(title: str) -> str:
    """Classify a YouTube title into one of FORMATS via conservative, deterministic heuristics.

    Precedence (checked in this order, first match wins):
      1. listicle  — leading/embedded count patterns ("7 ways...", "Top 5...", "6 Wealth Tiers").
                      Checked first so a numbered how-to list ("5 ways to retire early") reads as
                      a listicle, not an explainer.
      2. story      — first-person journey markers ("How I...", "I tried...", "My ... journey").
                      Checked before explainer so "How I spend..." doesn't match "how to"-style
                      explainer heuristics (it doesn't, but keeping story ahead of explainer keeps
                      the precedence honest as patterns evolve).
      3. news       — event/announcement markers ("announced", "is over", "breaking", "cancelling").
      4. explainer  — instructional/analytical markers ("how to", "why", "guide", "tutorial", "vs").
      5. other      — nothing matched.

    Pure and case-insensitive; no network, no LLM.
    """
    title = title or ""
    if _is_listicle(title):
        return "listicle"
    if _STORY_RE.search(title):
        return "story"
    if _NEWS_RE.search(title):
        return "news"
    if _EXPLAINER_RE.search(title):
        return "explainer"
    return "other"


# --------------------------------------------------------------------- positioning readout
_LEARNER_THRESHOLD = 0.55
_EXPERT_NEWCOMER_CEILING = 0.30
_EXPERT_SHARE_CEILING = 0.30
_EXPERT_AUTHORITY_GAP_CEILING = 0.35  # authority_gap: high = open/beatable, low = concentrated


def positioning(
    newcomer_volume: float | None,
    small_share: float | None,
    authority_gap: float | None,
) -> tuple[str, str]:
    """Report-only readout: can a newcomer realistically win this niche right now?

    Inputs (all 0..1 scores already present on a scored niche row — see cli.analyze_topic):
      - newcomer_volume: saturating score of the median views/day pulled by SMALL channels among
        the niche's own credible supply (youtube_niche.signals.volume). High = newcomers are
        demonstrably pulling real views here right now.
      - small_share: fraction of the niche's credible supply that comes from small channels
        (`small_channel_frac` on the scored row). High = the ranking page itself is not owned by
        incumbents.
      - authority_gap: 1 - authority_strength, where authority_strength grows with top3_channel_share
        (youtube_niche.signals.supply). VERIFIED DIRECTION: high authority_gap = the top 3 channels
        do NOT dominate = open/beatable; low authority_gap = a few incumbents own the results =
        concentrated/expert-required. Same "gap" convention as every other *_gap signal in this
        codebase (higher = more opportunity for a newcomer).

    Why these three and not Feature 2's per-niche breakout list: every breakout video mined by
    winners.py is small-at-publish BY CONSTRUCTION (see winners.find_breakouts), so a "small share
    of matching breakouts" computed from that list is trivially ~1.0 for every niche and carries no
    signal. The niche's OWN scored supply metrics (newcomer_volume / small_channel_frac /
    authority_gap) are the honest, discriminating inputs — they describe who is ranking for the
    topic today, not who mined the winning proof video.

    Returns (label, reason). label is one of:
      - "Learner-viable"  — newcomers are demonstrably thriving in this niche right now.
      - "Enthusiast"      — mixed/middle signal, or the default when data is incomplete.
      - "Expert-required" — authority-concentrated: newcomers aren't winning here yet.

    None-tolerant: any missing input degrades toward "Enthusiast" with an "insufficient data"
    reason rather than a false-confident verdict either way.
    """
    have = [x for x in (newcomer_volume, small_share, authority_gap) if x is not None]
    if len(have) < 2:
        return "Enthusiast", "insufficient data to call this confidently"

    if (newcomer_volume is not None and newcomer_volume >= _LEARNER_THRESHOLD) or (
        small_share is not None and small_share >= _LEARNER_THRESHOLD
    ):
        bits = []
        if newcomer_volume is not None:
            bits.append(f"newcomer volume {round(newcomer_volume * 100)}%")
        if small_share is not None:
            bits.append(f"{round(small_share * 100)}% of credible results are small channels")
        return "Learner-viable", "small channels pull real views here — " + ", ".join(bits)

    newcomer_low = newcomer_volume is not None and newcomer_volume < _EXPERT_NEWCOMER_CEILING
    share_low = small_share is not None and small_share < _EXPERT_SHARE_CEILING
    authority_concentrated = (
        authority_gap is not None and authority_gap < _EXPERT_AUTHORITY_GAP_CEILING
    )
    if newcomer_low and (share_low or authority_concentrated):
        bits = [f"newcomer volume only {round(newcomer_volume * 100)}%"]
        if share_low:
            bits.append(f"small channels are only {round(small_share * 100)}% of credible results")
        if authority_concentrated:
            bits.append(f"top 3 channels concentrate the results (authority gap {round(authority_gap * 100)}%)")
        return "Expert-required", "; ".join(bits)

    return "Enthusiast", "mixed signal — neither clearly open nor clearly concentrated"
