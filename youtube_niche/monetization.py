"""Monetization proxies for niche scoring.

YouTube does not expose public CPM for arbitrary topics, so this module combines curated domain
CPM ranges with keyword-level advertiser intent. The output is a proxy, not a revenue guarantee.
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
