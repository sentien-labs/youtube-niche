"""Seed expansion via YouTube search autocomplete (signal F's term-discovery half).

Autocomplete gives the actual query strings people type — real demand surface — but not
volume. Free, no API key, no quota cost.
"""
from __future__ import annotations

import string

import requests

AUTOCOMPLETE = "https://suggestqueries.google.com/complete/search"


def autocomplete(seed: str, region: str = "US", lang: str = "en") -> list[str]:
    params = {"client": "firefox", "ds": "yt", "q": seed, "hl": lang, "gl": region}
    try:
        r = requests.get(
            AUTOCOMPLETE,
            params=params,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()  # ["seed", ["suggestion", ...], ...]
        return [s for s in data[1] if isinstance(s, str)]
    except Exception:
        return []


def expand_seeds(
    niche: str,
    max_seeds: int = 20,
    alphabet_soup: bool = False,
    region: str = "US",
    lang: str = "en",
) -> list[str]:
    """Expand a niche into candidate topics. The original niche is always first."""
    seen: list[str] = []

    def add(x: str) -> None:
        x = x.strip().lower()
        if x and x not in seen:
            seen.append(x)

    add(niche)
    for s in autocomplete(niche, region=region, lang=lang):
        add(s)
    if alphabet_soup:
        for ch in string.ascii_lowercase:
            for s in autocomplete(f"{niche} {ch}", region=region, lang=lang):
                add(s)
            if len(seen) >= max_seeds * 3:
                break
    return seen[:max_seeds]
