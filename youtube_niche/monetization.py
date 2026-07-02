"""Monetization proxies for niche scoring.

YouTube does not expose public CPM for arbitrary topics, so this module combines curated domain
CPM ranges with keyword-level advertiser intent. The output is a proxy, not a revenue guarantee.

Two separate monetization axes live here, and they are NOT interchangeable:
  - `monetization_score` / `ad_intent_score` — programmatic ad CPM (AdSense demand). This feeds
    `cpm_score` in the opportunity math (see score.py).
  - `product_fit_score` — how well the niche supports selling the creator's OWN products
    (high-ticket coaching/courses > mid/low-ticket digital products > affiliate > AdSense-only).
    A niche can have high ad CPM but poor product fit (e.g. tech reviews: valuable clicks, but
    the audience buys someone else's gear, not the creator's course) or the reverse. `product_fit`
    is REPORT-ONLY: it is threaded into the per-topic result dict for display but is never blended
    into `opportunity` or any other existing score.
"""
from __future__ import annotations

import re

from .util import clamp01

AD_INTENT_KEYWORDS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("insurance", "medicare", "life insurance", "health insurance"), 0.95),
    (("mortgage", "refinance", "loan", "credit card", "debt", "bankruptcy"), 0.90),
    (("lawyer", "attorney", "legal", "lawsuit"), 0.88),
    (("tax", "ira", "401k", "roth", "retirement", "investing", "dividend"), 0.82),
    (("saas", "crm", "email marketing", "seo", "b2b", "sales funnel"), 0.78),
    (("real estate", "rental property", "home buyer", "house hacking"), 0.76),
    (("ai automation", "ai agent", "chatgpt", "business automation"), 0.72),
    (("crypto", "bitcoin", "defi", "altcoin"), 0.65),
    (("course", "certification", "freelancing", "ecommerce"), 0.60),
)

# Topic-level nudges applied on top of a domain's base `product_fit`. Word-boundary matched (same
# convention as AD_INTENT_KEYWORDS) so e.g. "vs" doesn't fire inside "vsauce".
# Affiliate/commercial-intent terms (buyer's-guide content — sells someone else's product, but
# signals a monetizable, purchase-ready audience): +0.05 for a single hit, +0.10 if two or more
# distinct terms hit (stronger evidence of commercial intent).
PRODUCT_FIT_COMMERCIAL_TERMS: tuple[str, ...] = (
    "best", "review", "vs", "top", "gear", "setup", "tools",
)
PRODUCT_FIT_COMMERCIAL_DELTA_ONE = 0.05
PRODUCT_FIT_COMMERCIAL_DELTA_MANY = 0.10
# Service/coaching-intent terms (audience is shopping for expertise, not gear): flat +0.05.
PRODUCT_FIT_SERVICE_TERMS: tuple[str, ...] = (
    "coaching", "consulting", "course", "roadmap", "career",
)
PRODUCT_FIT_SERVICE_DELTA = 0.05
# Free/DIY-intent terms (audience is actively avoiding paying): flat -0.05.
PRODUCT_FIT_FREE_TERMS: tuple[str, ...] = ("free", "diy", "cheap")
PRODUCT_FIT_FREE_DELTA = -0.05


def _term_hits(hay: str, terms: tuple[str, ...]) -> int:
    """Count distinct terms that word-boundary-match `hay` (already lowercased)."""
    return sum(1 for term in terms if re.search(rf"\b{re.escape(term)}\b", hay))


def cpm_score_from_mid(cpm_mid: float | None, full_scale: float = 40.0) -> float | None:
    if cpm_mid is None:
        return None
    return clamp01(cpm_mid / full_scale)


def ad_intent_score(topic: str) -> tuple[float, str]:
    hay = topic.lower()
    best = 0.25
    best_term = "generic"
    for terms, score in AD_INTENT_KEYWORDS:
        for term in terms:
            # word-boundary match so "ira" doesn't fire on "iran", "loan" not on "download"
            if score > best and re.search(rf"\b{re.escape(term)}\b", hay):
                best = score
                best_term = term
    return best, best_term


def monetization_score(topic: str, domain=None, full_scale: float = 40.0):
    intent, intent_term = ad_intent_score(topic)
    if domain is None:
        return intent, {
            "cpm_source": f"keyword:{intent_term}",
            "cpm_mid": None,
            "ad_intent": intent,
        }

    domain_score = cpm_score_from_mid(domain.cpm_mid, full_scale) or 0.0
    # Domain CPM is the stronger prior; topic-level advertiser intent adjusts within the domain.
    score = clamp01(0.75 * domain_score + 0.25 * intent)
    return score, {
        "cpm_source": f"domain:{domain.name};keyword:{intent_term}",
        "cpm_mid": domain.cpm_mid,
        "ad_intent": intent,
    }


def product_fit_score(domain, topic: str) -> float:
    """How well `topic` supports selling the creator's OWN products (0-1).

    Distinct from ad CPM (see module docstring): a domain's curated `product_fit` is the base
    (high-ticket coaching/courses > mid/low-ticket digital products > affiliate > AdSense-only),
    and topic-level commercial/service/free-intent keywords nudge it up or down. Pure and
    deterministic — same inputs always give the same output. `domain=None` falls back to the
    dataclass default (0.5), matching `Domain.product_fit`'s neutral prior.
    """
    base = getattr(domain, "product_fit", None)
    if base is None:
        base = 0.5
    hay = topic.lower()

    commercial_hits = _term_hits(hay, PRODUCT_FIT_COMMERCIAL_TERMS)
    delta = 0.0
    if commercial_hits >= 2:
        delta += PRODUCT_FIT_COMMERCIAL_DELTA_MANY
    elif commercial_hits == 1:
        delta += PRODUCT_FIT_COMMERCIAL_DELTA_ONE
    if _term_hits(hay, PRODUCT_FIT_SERVICE_TERMS) > 0:
        delta += PRODUCT_FIT_SERVICE_DELTA
    if _term_hits(hay, PRODUCT_FIT_FREE_TERMS) > 0:
        delta += PRODUCT_FIT_FREE_DELTA

    return clamp01(base + delta)
