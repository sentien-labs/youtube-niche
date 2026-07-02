"""Offline tests for the four viewer-facing winners-report enrichments:

  1. classify_format         (youtube_niche.formats)
  2. replication_and_format  (youtube_niche.winners)
  3. positioning              (youtube_niche.formats)
  4. LLM.hypothesis_statement (youtube_niche.llm) + report row emission (youtube_niche.report)

No network, no real LLM CLI — hypothesis_statement is exercised against a fake backend
constructed the same way as tests/test_llm_failover.py.

Run: ./venv/bin/python -m pytest tests/test_enrichments.py -q
"""
from __future__ import annotations

import csv

from youtube_niche.formats import classify_format, positioning
from youtube_niche.llm import LLM
from youtube_niche.report import CSV_FIELDS, write_reports
from youtube_niche.winners import replication_and_format


# --------------------------------------------------------------------- Feature 1: classify_format
def test_classify_format_real_titles_from_actual_runs():
    """The four concrete examples the winners report has actually produced."""
    assert classify_format("6 Wealth Tiers in Retirement") == "listicle"
    assert classify_format("How I spend my 66 LPA salary") == "story"
    assert classify_format("Tech CEOs cancelling AI") == "news"
    assert classify_format("How to Run AI Agents Locally") == "explainer"


def test_classify_format_listicle_leading_number():
    assert classify_format("7 ways to save on taxes") == "listicle"


def test_classify_format_listicle_top_n():
    assert classify_format("Top 5 budgeting apps for 2026") == "listicle"


def test_classify_format_listicle_embedded_count_noun():
    assert classify_format("5 mistakes new investors make") == "listicle"


def test_classify_format_story_first_person_tried():
    assert classify_format("I tried the 75 hard challenge for 30 days") == "story"


def test_classify_format_story_my_journey():
    assert classify_format("My debt payoff journey") == "story"


def test_classify_format_news_announcement():
    assert classify_format("OpenAI announced a new pricing tier") == "news"


def test_classify_format_news_is_over():
    assert classify_format("The AI hype is over") == "news"


def test_classify_format_explainer_what_is():
    assert classify_format("What is a Roth IRA") == "explainer"


def test_classify_format_explainer_vs_comparison():
    assert classify_format("iPhone vs Android for creators") == "explainer"


def test_classify_format_other_when_nothing_matches():
    assert classify_format("My cat is cute") == "other"


def test_classify_format_case_insensitive():
    assert classify_format("HOW TO RUN AI AGENTS LOCALLY") == "explainer"
    assert classify_format("6 WEALTH TIERS IN RETIREMENT") == "listicle"


def test_classify_format_empty_title_is_other():
    assert classify_format("") == "other"


# --- precedence rules -------------------------------------------------------------------------
def test_precedence_listicle_beats_explainer_when_both_could_fire():
    """A numbered how-to list reads as a listicle, not an explainer."""
    assert classify_format("5 ways to explain how to budget") == "listicle"
    assert classify_format("7 steps: how to retire early") == "listicle"


def test_precedence_story_beats_explainer_for_how_i():
    """'How I...' must never fall through to the generic 'how to' explainer bucket."""
    assert classify_format("How I automated my entire business") == "story"


def test_precedence_news_beats_explainer_for_cancelling():
    """'cancelling' is an event marker even though 'why'-like analytical language could apply."""
    assert classify_format("Tech CEOs cancelling AI") == "news"


# --------------------------------------------------------------------- Feature 2: replication
def _breakout(title: str, channel_id: str, video_id: str | None = None) -> dict:
    return {
        "video_id": video_id or f"vid-{title[:10]}-{channel_id}",
        "title": title,
        "channel_id": channel_id,
    }


def test_replication_counts_distinct_channels_across_matching_breakouts():
    breakouts = [
        _breakout("Backdoor Roth IRA explained simply", "chanA"),
        _breakout("How to do a Backdoor Roth IRA in 2026", "chanB"),
        _breakout("Backdoor Roth IRA mistakes to avoid", "chanC"),
        _breakout("Unrelated cooking video", "chanD"),
    ]
    channels, fmt = replication_and_format("backdoor roth ira", breakouts)
    assert channels == 3
    assert fmt  # some format was classified


def test_replication_dedupes_repeated_channel_not_repeated_videos():
    """One channel posting the same theme 5x is a channel story, not niche replication —
    replication_channels must count DISTINCT channel_ids, not matching video count."""
    breakouts = [
        _breakout("Backdoor Roth IRA part 1", "chanA", "v1"),
        _breakout("Backdoor Roth IRA part 2", "chanA", "v2"),
        _breakout("Backdoor Roth IRA part 3", "chanA", "v3"),
        _breakout("Backdoor Roth IRA part 4", "chanA", "v4"),
        _breakout("Backdoor Roth IRA part 5", "chanA", "v5"),
    ]
    channels, _fmt = replication_and_format("backdoor roth ira", breakouts)
    assert channels == 1  # NOT 5 — same channel, repeated uploads


def test_replication_dominant_format_is_majority_vote():
    breakouts = [
        _breakout("7 Backdoor Roth IRA mistakes", "chanA"),
        _breakout("5 Backdoor Roth IRA rules", "chanB"),
        _breakout("How to do a Backdoor Roth IRA", "chanC"),
    ]
    channels, fmt = replication_and_format("backdoor roth ira", breakouts)
    assert channels == 3
    assert fmt == "listicle"  # 2 listicles vs 1 explainer


def test_replication_no_matches_returns_zero_channels_and_empty_format():
    breakouts = [_breakout("Completely unrelated gardening tips", "chanA")]
    channels, fmt = replication_and_format("backdoor roth ira", breakouts)
    assert channels == 0
    assert fmt == ""


def test_replication_empty_breakouts_list():
    channels, fmt = replication_and_format("backdoor roth ira", [])
    assert channels == 0
    assert fmt == ""


# --------------------------------------------------------------------- Feature 3: positioning
def test_positioning_clear_learner_viable():
    label, reason = positioning(newcomer_volume=0.8, small_share=0.75, authority_gap=0.7)
    assert label == "Learner-viable"
    assert reason  # grounded, non-empty


def test_positioning_learner_viable_on_newcomer_volume_alone():
    """High newcomer_volume alone (even with a middling/low small_share) should still read as
    learner-viable — newcomers demonstrably pulling views is the strongest of the two 'open' signals."""
    label, _reason = positioning(newcomer_volume=0.9, small_share=0.2, authority_gap=0.5)
    assert label == "Learner-viable"


def test_positioning_clear_expert_required():
    label, reason = positioning(newcomer_volume=0.1, small_share=0.1, authority_gap=0.1)
    assert label == "Expert-required"
    assert reason


def test_positioning_expert_required_needs_more_than_low_newcomer_alone():
    """Low newcomer_volume alone, with healthy share/authority_gap, should NOT be expert-required —
    expert-required requires the concentration signals to agree, not just one low number."""
    label, _reason = positioning(newcomer_volume=0.1, small_share=0.8, authority_gap=0.8)
    assert label != "Expert-required"


def test_positioning_insufficient_data_when_all_missing():
    label, reason = positioning(None, None, None)
    assert label == "Enthusiast"
    assert "insufficient data" in reason.lower()


def test_positioning_insufficient_data_when_only_one_present():
    label, reason = positioning(newcomer_volume=0.9, small_share=None, authority_gap=None)
    assert label == "Enthusiast"
    assert "insufficient data" in reason.lower()


def test_positioning_mixed_signal_is_enthusiast():
    label, reason = positioning(newcomer_volume=0.45, small_share=0.45, authority_gap=0.45)
    assert label == "Enthusiast"
    assert reason


def test_positioning_never_crashes_on_boundary_values():
    for nv in (0.0, 1.0, None):
        for ss in (0.0, 1.0, None):
            for ag in (0.0, 1.0, None):
                label, reason = positioning(nv, ss, ag)
                assert label in ("Learner-viable", "Enthusiast", "Expert-required")
                assert isinstance(reason, str)


# --------------------------------------------------------------------- Feature 4: hypothesis_statement
class _FakeBackend:
    """Minimal stand-in for a real LLM backend — mirrors tests/test_llm_failover.py."""

    def __init__(self, name: str, result=None, available: bool = True):
        self.name = name
        self._result = result
        self.available = available
        self.calls = 0
        self.last_args = None

    def complete_json(self, system: str, user: str, tier: str = "cheap", max_tokens=None):
        self.calls += 1
        self.last_args = (system, user, tier, max_tokens)
        return self._result


def test_hypothesis_statement_valid_json_returns_string():
    backend = _FakeBackend("fake", result={"hypothesis": "I help new investors avoid backdoor Roth IRA mistakes"})
    llm = LLM(backend)
    out = llm.hypothesis_statement("backdoor roth ira", ["Backdoor Roth IRA explained", "5 Roth IRA mistakes"])
    assert out == "I help new investors avoid backdoor Roth IRA mistakes"


def test_hypothesis_statement_routes_through_call_chain():
    """Must go through the same failover chain as extract_niches — primary fails, fallback succeeds."""
    primary = _FakeBackend("agy", result=None)
    fallback = _FakeBackend("codex", result={"hypothesis": "I help creators pick a winnable niche"})
    llm = LLM(primary, fallback_builders=[("codex", lambda: fallback)])

    out = llm.hypothesis_statement("niche picking", ["How I picked my niche", "Niche picking mistakes"])

    assert out == "I help creators pick a winnable niche"
    assert primary.calls == 1
    assert fallback.calls == 1
    assert llm.last_provider == "codex"


def test_hypothesis_statement_malformed_json_missing_field_returns_none():
    backend = _FakeBackend("fake", result={"not_hypothesis": "something else"})
    llm = LLM(backend)
    out = llm.hypothesis_statement("topic", ["some title"])
    assert out is None


def test_hypothesis_statement_missing_i_help_prefix_returns_none():
    backend = _FakeBackend("fake", result={"hypothesis": "New investors avoid Roth IRA mistakes"})
    llm = LLM(backend)
    out = llm.hypothesis_statement("topic", ["some title"])
    assert out is None


def test_hypothesis_statement_i_help_prefix_is_case_insensitive():
    backend = _FakeBackend("fake", result={"hypothesis": "i help new investors avoid mistakes"})
    llm = LLM(backend)
    out = llm.hypothesis_statement("topic", ["some title"])
    assert out == "i help new investors avoid mistakes"


def test_hypothesis_statement_too_long_returns_none():
    backend = _FakeBackend("fake", result={"hypothesis": "I help " + "x" * 160})
    llm = LLM(backend)
    out = llm.hypothesis_statement("topic", ["some title"])
    assert out is None


def test_hypothesis_statement_empty_result_returns_none():
    backend = _FakeBackend("fake", result=None)
    llm = LLM(backend, fallback_builders=[])
    out = llm.hypothesis_statement("topic", ["some title"])
    assert out is None


def test_hypothesis_statement_llm_disabled_returns_none_without_calling_backend():
    backend = _FakeBackend("fake", available=False)
    llm = LLM(backend, fallback_builders=[])
    assert llm.enabled is False
    out = llm.hypothesis_statement("topic", ["some title"])
    assert out is None
    assert backend.calls == 0


def test_hypothesis_statement_no_matching_titles_returns_none_without_calling_backend():
    backend = _FakeBackend("fake", result={"hypothesis": "I help someone"})
    llm = LLM(backend)
    out = llm.hypothesis_statement("topic", [])
    assert out is None
    assert backend.calls == 0


def test_hypothesis_statement_passes_tier_cheap_and_caps_tokens():
    backend = _FakeBackend("fake", result={"hypothesis": "I help creators find a niche"})
    llm = LLM(backend)
    llm.hypothesis_statement("niche", ["title one", "title two"])
    _system, _user, tier, max_tokens = backend.last_args
    assert tier == "cheap"
    assert max_tokens is not None and max_tokens <= 200


def test_hypothesis_statement_includes_comment_questions_in_prompt():
    backend = _FakeBackend("fake", result={"hypothesis": "I help creators find a niche"})
    llm = LLM(backend)
    llm.hypothesis_statement(
        "niche picking", ["How I picked my niche"], comment_questions=["How do I know if a niche is winnable?"]
    )
    _system, user, _tier, _max_tokens = backend.last_args
    assert "How do I know if a niche is winnable?" in user


# --------------------------------------------------------------------- report row emission
def test_report_new_columns_are_appended_at_the_end():
    tail = CSV_FIELDS[-5:]
    assert tail == ["product_fit", "positioning", "dominant_format", "replication_channels", "hypothesis"]


def test_report_emits_new_columns_when_present(tmp_path):
    rows = [{
        "topic": "backdoor roth ira",
        "opportunity": 0.7,
        "product_fit": 0.65,
        "positioning": "Learner-viable",
        "dominant_format": "listicle",
        "replication_channels": 4,
        "hypothesis": "I help new investors avoid backdoor Roth IRA mistakes",
    }]
    csv_path, md_path = write_reports(rows, str(tmp_path), "test-niche")

    with csv_path.open() as f:
        out_rows = list(csv.DictReader(f))
    assert out_rows[0]["product_fit"] == "0.65"
    assert out_rows[0]["positioning"] == "Learner-viable"
    assert out_rows[0]["dominant_format"] == "listicle"
    assert out_rows[0]["replication_channels"] == "4"
    assert out_rows[0]["hypothesis"] == "I help new investors avoid backdoor Roth IRA mistakes"

    md_text = md_path.read_text()
    assert '"I help new investors avoid backdoor Roth IRA mistakes"' in md_text
    assert "Learner-viable" in md_text
    assert "listicle" in md_text
    assert "replicated across 4 channels" in md_text


def test_report_tolerates_rows_missing_new_keys(tmp_path):
    """Rows from OTHER entry points (e.g. cli.py --seeds) won't have replication/hypothesis —
    write_reports must not crash, and the new columns should just come out empty."""
    rows = [{
        "topic": "some other niche",
        "opportunity": 0.5,
        # no product_fit / positioning / dominant_format / replication_channels / hypothesis
    }]
    csv_path, md_path = write_reports(rows, str(tmp_path), "seeds-niche")

    with csv_path.open() as f:
        out_rows = list(csv.DictReader(f))
    assert out_rows[0]["product_fit"] == ""
    assert out_rows[0]["positioning"] == ""
    assert out_rows[0]["dominant_format"] == ""
    assert out_rows[0]["replication_channels"] == ""
    assert out_rows[0]["hypothesis"] == ""

    # MD generation must not crash, and must not print misleading placeholder enrichment lines.
    md_text = md_path.read_text()
    assert "some other niche" in md_text
    assert "Hypothesis" not in md_text
    assert "Positioning" not in md_text


def test_report_replication_line_hidden_below_three_channels(tmp_path):
    """MD 'replicated across N channels' phrase should only render at the >=3 threshold used in
    winners.py's stdout narration (below 3, still show format/positioning but skip the replication claim)."""
    rows = [{
        "topic": "small niche",
        "opportunity": 0.4,
        "positioning": "Enthusiast",
        "dominant_format": "explainer",
        "replication_channels": 2,
        "hypothesis": "",
    }]
    _csv_path, md_path = write_reports(rows, str(tmp_path), "small-niche")
    md_text = md_path.read_text()
    assert "replicated across" not in md_text
    assert "explainer" in md_text
