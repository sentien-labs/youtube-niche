"""Tests for the LLM failover chain (youtube_niche.llm).

Incident context: on 2026-06-30 the default `agy` (Gemini) CLI backend returned empty stdout
(exit 0) for every prompt. `LLM.extract_niches` returned None and the caller silently degraded
to keyword n-gram extraction with no warning anywhere. These tests cover the fix: `LLM` tries an
ordered chain of backends and only gives up (loudly) once every one of them has failed.

All backends here are fakes — no subprocess, no network, no real LLM CLI is ever invoked.
"""
from __future__ import annotations

import os

import pytest

from youtube_niche.llm import LLM


class _FakeBackend:
    """A minimal stand-in for a real backend: `available` gate + `complete_json` return value."""

    def __init__(self, name: str, result=None, available: bool = True):
        self.name = name
        self._result = result
        self.available = available
        self.calls = 0

    def complete_json(self, system: str, user: str, tier: str = "cheap", max_tokens=None):
        self.calls += 1
        return self._result


@pytest.fixture(autouse=True)
def _clean_llm_fallback_env():
    """Isolate LLM_FALLBACK across tests regardless of pass/fail."""
    old = os.environ.get("LLM_FALLBACK")
    yield
    if old is None:
        os.environ.pop("LLM_FALLBACK", None)
    else:
        os.environ["LLM_FALLBACK"] = old


def test_chain_falls_through_to_a_working_fallback(capsys):
    """Primary backend returns empty; the chain should warn about the empty hop, announce the
    fallback it actually engages, and succeed."""
    os.environ.pop("LLM_FALLBACK", None)  # default: fallback enabled

    primary = _FakeBackend("agy", result=None)  # empty stdout, like the 2026-06-30 incident
    good = _FakeBackend("codex", result={"topics": ["dave ramsey"]})

    llm = LLM(primary, fallback_builders=[("codex", lambda: good), ("claude", lambda: _FakeBackend("claude"))])

    out = llm.extract_niches(["Dave Ramsey baby step 2 explained"], max_niches=5)

    assert out == ["dave ramsey"]
    assert llm.last_provider == "codex"
    assert primary.calls == 1
    assert good.calls == 1

    captured = capsys.readouterr()
    assert "WARNING: agy returned empty" in captured.out
    assert "falling back to codex" in captured.out
    # claude was never engaged, so it must never be announced.
    assert "falling back to claude" not in captured.out


def test_chain_also_falls_through_for_a_string_result():
    """The fixture spec's ""/None -> valid-JSON case, exercised directly via _call_chain so the
    "first returns empty, second returns valid" behavior is tested at the primitive level too."""
    os.environ.pop("LLM_FALLBACK", None)

    empty_first = _FakeBackend("agy", result="")
    valid_second = _FakeBackend("codex", result={"ok": True})

    llm = LLM(empty_first, fallback_builders=[("codex", lambda: valid_second)])
    result = llm._call_chain("complete_json", "sys", "user", tier="cheap")

    assert result == {"ok": True}
    assert llm.last_provider == "codex"


def test_llm_fallback_env_opt_out_disables_the_chain(capsys):
    """LLM_FALLBACK=0 must restrict the LLM to the primary backend only — no fallback attempted,
    even if one is registered and would have succeeded."""
    os.environ["LLM_FALLBACK"] = "0"

    primary = _FakeBackend("agy", result=None)
    would_have_worked = _FakeBackend("codex", result={"topics": ["should not be reached"]})

    llm = LLM(primary, fallback_builders=[("codex", lambda: would_have_worked)])
    out = llm.extract_niches(["Some breakout title here"], max_niches=5)

    assert out is None
    assert primary.calls == 1
    assert would_have_worked.calls == 0  # never even constructed/called

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "all providers returned empty" in captured.out
    assert "falling back" not in captured.out  # opt-out: no fallback ever announced


def test_exhausted_chain_returns_none_and_warns(capsys):
    """Every backend in the chain returns empty -> overall result is None, and a final
    "all providers" warning is printed (not just per-hop warnings)."""
    os.environ.pop("LLM_FALLBACK", None)

    primary = _FakeBackend("agy", result=None)
    fb1 = _FakeBackend("codex", result="")
    fb2 = _FakeBackend("claude", result={})  # empty dict is falsy -> still counts as failure

    llm = LLM(primary, fallback_builders=[("codex", lambda: fb1), ("claude", lambda: fb2)])
    out = llm.extract_niches(["Breakout title"], max_niches=5)

    assert out is None
    assert primary.calls == 1 and fb1.calls == 1 and fb2.calls == 1

    captured = capsys.readouterr()
    assert captured.out.count("WARNING") == 4  # one per empty hop (3) + the final summary
    assert "all providers returned empty/failed" in captured.out


def test_unavailable_fallback_backends_are_skipped_without_being_called():
    """A fallback whose `available` is False must never have complete_json invoked — mirrors a
    CLI binary that isn't on PATH."""
    os.environ.pop("LLM_FALLBACK", None)

    primary = _FakeBackend("agy", result=None)
    missing_binary = _FakeBackend("codex", result={"topics": ["x"]}, available=False)
    working = _FakeBackend("claude", result={"topics": ["dividend growth investing"]})

    llm = LLM(
        primary,
        fallback_builders=[("codex", lambda: missing_binary), ("claude", lambda: working)],
    )
    out = llm.extract_niches(["Some breakout title"], max_niches=5)

    assert out == ["dividend growth investing"]
    assert missing_binary.calls == 0
    assert llm.last_provider == "claude"


def test_fallback_builders_are_constructed_lazily_only_when_needed():
    """When the primary backend succeeds outright, fallback builders must never be invoked —
    the chain should stay lazy, not eagerly construct every backend up front."""
    primary = _FakeBackend("agy", result={"topics": ["dave ramsey"]})
    built = {"count": 0}

    def _never_should_build():
        built["count"] += 1
        return _FakeBackend("codex", result={"topics": ["should not be used"]})

    llm = LLM(primary, fallback_builders=[("codex", _never_should_build)])
    out = llm.extract_niches(["Dave Ramsey debt snowball"], max_niches=5)

    assert out == ["dave ramsey"]
    assert built["count"] == 0


def test_exhausted_chain_announces_only_engaged_fallbacks_in_order(capsys):
    """Diagnostics contract for the daily health check: per-hop empty warnings, a
    "falling back to <name>" line only for fallbacks actually engaged (never an already-tried
    or unavailable provider — the old "trying <next>" phrasing could name codex again after
    claude failed), ending with the exhaustion line."""
    os.environ.pop("LLM_FALLBACK", None)

    primary = _FakeBackend("agy", result=None)
    codex = _FakeBackend("codex", result="")
    grok = _FakeBackend("grok", result={"topics": ["never"]}, available=False)  # skipped
    claude = _FakeBackend("claude", result=None)

    llm = LLM(primary, fallback_builders=[
        ("codex", lambda: codex), ("grok", lambda: grok), ("claude", lambda: claude),
    ])
    out = llm.extract_niches(["Breakout title"], max_niches=5)

    assert out is None
    assert grok.calls == 0

    text = capsys.readouterr().out
    # every tried backend gets its own empty-hop warning
    assert "WARNING: agy returned empty" in text
    assert "WARNING: codex returned empty" in text
    assert "WARNING: claude returned empty" in text
    # each engaged fallback announced exactly once, by its actual name
    assert text.count("falling back to codex") == 1
    assert text.count("falling back to claude") == 1
    # never announces the unavailable provider, the primary, or an already-consumed fallback
    assert "falling back to grok" not in text
    assert "falling back to agy" not in text
    assert "WARNING: grok" not in text
    # strict ordering, ending with the exhaustion line
    order = [
        text.index("WARNING: agy returned empty"),
        text.index("falling back to codex"),
        text.index("WARNING: codex returned empty"),
        text.index("falling back to claude"),
        text.index("WARNING: claude returned empty"),
        text.index("all providers returned empty/failed"),
    ]
    assert order == sorted(order)
    assert text.rstrip().endswith("all providers returned empty/failed")


def test_missing_primary_binary_does_not_disable_the_chain():
    """Incident-hardening: LLM_PROVIDER=agy but the agy binary is gone, while codex works.
    `enabled` must be chain-aware — the user's config says LLM on, and a provider exists — so
    discover_niches doesn't silently take the ungated quiet-keyword path."""
    os.environ.pop("LLM_FALLBACK", None)

    primary = _FakeBackend("agy", result={"topics": ["never called"]}, available=False)
    fallback = _FakeBackend("codex", result={"topics": ["dave ramsey"]})

    llm = LLM(primary, fallback_builders=[("codex", lambda: fallback)])
    assert llm.enabled is True

    out = llm.extract_niches(["Dave Ramsey baby step 2 explained"], max_niches=5)
    assert out == ["dave ramsey"]
    assert primary.calls == 0  # unavailable primary is never invoked
    assert llm.last_provider == "codex"


def test_opt_out_restores_primary_only_enabled_semantics():
    """With LLM_FALLBACK=0, `enabled` must ignore fallbacks entirely: an unavailable primary
    means disabled, exactly like before the chain existed."""
    os.environ["LLM_FALLBACK"] = "0"

    primary = _FakeBackend("agy", available=False)
    fallback = _FakeBackend("codex", result={"topics": ["x"]})

    llm = LLM(primary, fallback_builders=[("codex", lambda: fallback)])
    assert llm.enabled is False
    assert llm.extract_niches(["title"], max_niches=5) is None
    assert fallback.calls == 0


def test_no_backend_available_at_all_returns_none_quietly_enabled_false(capsys):
    """No primary and no fallbacks -> enabled False, and every public method returns None
    QUIETLY (no warnings — nothing was configured, so there's nothing to warn about)."""
    llm = LLM(backend=None, fallback_builders=[])
    assert llm.enabled is False
    assert llm.extract_niches(["title"], max_niches=5) is None
    assert llm.classify_comment_demand(["can you cover X?"]) is None
    assert llm.score_depth("topic", "some transcript text") is None
    assert capsys.readouterr().out == ""

    # unavailable primary with no fallbacks: same quiet semantics as before the chain existed.
    llm2 = LLM(_FakeBackend("agy", available=False), fallback_builders=[])
    assert llm2.enabled is False
    assert llm2.extract_niches(["title"], max_niches=5) is None
    assert capsys.readouterr().out == ""
