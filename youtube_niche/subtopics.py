"""Discovered-subtopics registry — winners-first niches written back as the stage-2 seed list.

The first clean backtests showed hand-curated `domain.subtopics` missing observed small-channel
breakouts. This closes the loop: `python -m youtube_niche.winners --domain X --emit-subtopics`
mines real breakouts, reads the niches off them, and records them; `--from-domain X` then seeds
stage-2 from these data-derived niches instead of the hand-guessed list, falling back to the
curated list when none are recorded yet.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ENV_REGISTRY = "YOUTUBE_NICHE_SUBTOPICS_PATH"
PACKAGE_REGISTRY = Path(__file__).with_name("discovered_subtopics.json")


def default_user_registry() -> Path:
    """Writable overlay for regenerated discovered subtopics."""
    if os.environ.get(ENV_REGISTRY):
        return Path(os.environ[ENV_REGISTRY]).expanduser()
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config).expanduser() if xdg_config else Path.home() / ".config"
    return base / "youtube-niche" / "discovered_subtopics.json"


def _read_registry(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _path(path: str | Path | None) -> Path:
    return Path(path).expanduser() if path else default_user_registry()


def load_registry(path: str | Path | None = None) -> dict:
    if path is not None:
        return _read_registry(_path(path))
    packaged = _read_registry(PACKAGE_REGISTRY)
    user = _read_registry(default_user_registry())
    return {**packaged, **user}


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
