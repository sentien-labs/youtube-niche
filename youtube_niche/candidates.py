"""Candidate-topic generation with source labels.

The scorer can only rank topics it is given. This module keeps candidate generation explicit
and auditable so validation can separate "bad scoring" from "we never scored the winning topic."
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .seeds import autocomplete
from .subtopics import discovered_subtopics
from .topics import topic_similarity


@dataclass(frozen=True)
class CandidateTopic:
    topic: str
    source: str


AutocompleteFn = Callable[[str, str, str], list[str]]


def dedupe_candidates(
    candidates: list[CandidateTopic],
    threshold: float = 0.78,
) -> list[CandidateTopic]:
    """Deduplicate near-identical candidates while preserving the first source label."""
    kept: list[CandidateTopic] = []
    for cand in candidates:
        clean = " ".join(str(cand.topic).split())
        if not clean:
            continue
        normalized = CandidateTopic(clean, cand.source)
        if any(topic_similarity(normalized.topic, prior.topic) >= threshold for prior in kept):
            continue
        kept.append(normalized)
    return kept


def _add_autocomplete(
    out: list[CandidateTopic],
    bases: list[str],
    *,
    source: str,
    region: str,
    lang: str,
    autocomplete_fn: AutocompleteFn,
    per_base: int,
) -> None:
    for base in bases:
        if per_base <= 0:
            return
        for suggestion in autocomplete_fn(base, region, lang)[:per_base]:
            out.append(CandidateTopic(suggestion, source))


def domain_seed_candidates(
    domain,
    *,
    max_seeds: int,
    mode: str = "hybrid",
    region: str = "US",
    lang: str = "en",
    subtopics_registry: str | Path | None = None,
    autocomplete_fn: AutocompleteFn = autocomplete,
    autocomplete_per_base: int = 3,
) -> list[CandidateTopic]:
    """Return labeled candidates for a domain drilldown.

    Modes:
    - ``effective``: existing behavior; discovered subtopics if present, else curated.
    - ``hybrid``: discovered subtopics, domain-term autocomplete, curated subtopics, then
      subtopic autocomplete. This is the operational default because current validation shows
      candidate coverage is the main miss source.
    - ``expanded``: domain-term autocomplete, curated subtopics, then subtopic autocomplete;
      excludes discovered topics for cleaner candidate-generation audits.
    - ``curated``: hand-maintained domain subtopics only.
    - ``discovered``: winners-first discovered registry only.
    """
    if max_seeds <= 0:
        return []
    mode = (mode or "hybrid").lower()
    discovered = discovered_subtopics(domain.name, subtopics_registry)
    curated = list(getattr(domain, "subtopics", []) or [])
    domain_terms = list(getattr(domain, "terms", []) or [])

    out: list[CandidateTopic] = []
    if mode == "curated":
        out.extend(CandidateTopic(t, "curated") for t in curated)
    elif mode == "discovered":
        out.extend(CandidateTopic(t, "discovered") for t in discovered)
    elif mode == "effective":
        if discovered:
            out.extend(CandidateTopic(t, "discovered") for t in discovered)
        else:
            out.extend(CandidateTopic(t, "curated") for t in curated)
    elif mode in {"hybrid", "expanded"}:
        if mode == "hybrid":
            out.extend(CandidateTopic(t, "discovered") for t in discovered)
        _add_autocomplete(
            out,
            domain_terms,
            source="domain_autocomplete",
            region=region,
            lang=lang,
            autocomplete_fn=autocomplete_fn,
            per_base=autocomplete_per_base,
        )
        out.extend(CandidateTopic(t, "curated") for t in curated)
        # If the list is still thin, broaden from the subtopics themselves. This path is useful
        # for domains without discovered winners yet.
        if len(dedupe_candidates(out)) < max_seeds:
            _add_autocomplete(
                out,
                ((discovered if mode == "hybrid" else []) or curated)[: max_seeds],
                source="subtopic_autocomplete",
                region=region,
                lang=lang,
                autocomplete_fn=autocomplete_fn,
                per_base=max(1, autocomplete_per_base - 1),
            )
    else:
        raise ValueError(f"unknown candidate mode: {mode}")

    return dedupe_candidates(out)[:max_seeds]


def domain_probe_terms(
    domain,
    *,
    max_terms: int = 20,
    include_autocomplete: bool = True,
    region: str = "US",
    lang: str = "en",
    autocomplete_fn: AutocompleteFn = autocomplete,
    autocomplete_per_base: int = 2,
) -> list[str]:
    """Search probes for mining breakouts inside a domain.

    Domain terms are broad priors. Autocomplete-expanded probes add real query language before
    any YouTube quota is spent, which improves the odds that winners-first sees emerging demand.
    """
    base_terms = list(getattr(domain, "terms", []) or [])
    out = [CandidateTopic(t, "domain_term") for t in base_terms]
    if include_autocomplete:
        _add_autocomplete(
            out,
            base_terms,
            source="domain_probe_autocomplete",
            region=region,
            lang=lang,
            autocomplete_fn=autocomplete_fn,
            per_base=autocomplete_per_base,
        )
    return [c.topic for c in dedupe_candidates(out)[:max_terms]]


def source_summary(candidates: list[CandidateTopic]) -> str:
    counts: dict[str, int] = {}
    for cand in candidates:
        counts[cand.source] = counts.get(cand.source, 0) + 1
    return ", ".join(f"{source}:{counts[source]}" for source in sorted(counts)) or "none"
