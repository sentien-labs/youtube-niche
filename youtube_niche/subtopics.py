"""Discovered-subtopics registry — winners-first niches written back as the stage-2 seed list.

The hand-curated `domain.subtopics` were shown by the backtest to miss where breakouts actually
happen (curated lists scored ~0% precision against real small-channel breakouts). This closes the
loop: `python -m youtube_niche.winners --domain X --emit-subtopics` mines real breakouts, reads the
niches off them, and records them here; `--from-domain X` then seeds stage-2 from these data-derived
niches instead of the hand-guessed list, falling back to the curated list when none are recorded yet.
"""
from __future__ import annotations

import json
from pathlib import Path

# Ships inside the package so regenerated lists are versioned with the tool.
DEFAULT_REGISTRY = Path(__file__).with_name("discovered_subtopics.json")


def _path(path: str | Path | None) -> Path:
    return Path(path) if path else DEFAULT_REGISTRY


def load_registry(path: str | Path | None = None) -> dict:
    p = _path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def discovered_subtopics(domain_name: str, path: str | Path | None = None) -> list[str]:
    entry = load_registry(path).get(domain_name)
    if not entry:
        return []
    subs = entry.get("subtopics", [])
    return [str(s) for s in subs] if isinstance(subs, list) else []


def effective_subtopics(domain, path: str | Path | None = None) -> tuple[list[str], str]:
    """Return (subtopics, source): prefer data-derived 'discovered', else hand-curated."""
    disc = discovered_subtopics(domain.name, path)
    if disc:
        return disc, "discovered"
    return list(domain.subtopics), "curated"


def save_discovered(domain_name: str, subtopics: list[str], meta: dict | None = None,
                    path: str | Path | None = None) -> Path:
    """Record discovered subtopics for a domain (merges into the registry, overwriting that domain)."""
    p = _path(path)
    reg = load_registry(p)
    entry = {"subtopics": [str(s) for s in subtopics]}
    if meta:
        entry.update(meta)
    reg[domain_name] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    return p
