"""Offline tests for the product_fit axis — report-only, must never affect opportunity scoring.

Run: python -m pytest tests/test_product_fit.py -q
"""
from __future__ import annotations

from youtube_niche.domains import DOMAINS, Domain
from youtube_niche.monetization import product_fit_score


def _domain(name: str) -> Domain:
    matches = [d for d in DOMAINS if d.name == name]
    assert matches, f"no domain named {name!r} in DOMAINS"
    return matches[0]


def test_domain_product_fit_defaults_to_neutral():
    """Every other consumer of Domain (discover.py etc.) is unaffected by this additive field."""
    d = Domain("Unscored", ["x"], 10, 20)
    assert d.product_fit == 0.5


def test_base_mapping_sanity_finance_beats_insurance():
    """Personal finance is high-ticket-coaching-and-affiliate-rich; insurance is mostly lead-gen."""
    finance = _domain("Personal finance / investing")
    insurance = _domain("Insurance")
    assert finance.product_fit > insurance.product_fit


def test_all_domains_have_sensible_relative_product_fit():
    """Every curated domain got an explicit, in-range value (none silently left at the 0.5 default)."""
    for d in DOMAINS:
        assert 0.0 <= d.product_fit <= 1.0
        assert d.product_fit != 0.5, f"{d.name} looks unset (still at the dataclass default)"


def test_neutral_topic_returns_domain_base():
    d = Domain("Neutral", ["x"], 10, 20, product_fit=0.7)
    assert product_fit_score(d, "widgets for beginners") == 0.7


def test_commercial_intent_nudges_up():
    d = Domain("Neutral", ["x"], 10, 20, product_fit=0.5)
    base = product_fit_score(d, "budgeting basics")
    nudged = product_fit_score(d, "best budgeting apps review")
    assert nudged > base


def test_service_intent_nudges_up():
    d = Domain("Neutral", ["x"], 10, 20, product_fit=0.5)
    base = product_fit_score(d, "budgeting basics")
    nudged = product_fit_score(d, "career coaching roadmap")
    assert nudged > base


def test_free_diy_intent_nudges_down():
    d = Domain("Neutral", ["x"], 10, 20, product_fit=0.5)
    base = product_fit_score(d, "budgeting apps")
    nudged = product_fit_score(d, "free diy budgeting apps")
    assert nudged < base


def test_multiple_commercial_hits_nudge_more_than_a_single_hit():
    d = Domain("Neutral", ["x"], 10, 20, product_fit=0.5)
    neutral = product_fit_score(d, "budgeting basics")
    one_hit = product_fit_score(d, "budgeting gear basics")  # only "gear" matches
    two_hits = product_fit_score(d, "best budgeting tools review")  # best/tools/review -> 3 hits
    assert neutral < one_hit < two_hits


def test_clamps_to_zero_one_at_high_base():
    """A near-max base plus positive nudges must not exceed 1.0."""
    d = Domain("Nearly maxed", ["x"], 10, 20, product_fit=0.98)
    score = product_fit_score(d, "best coaching roadmap review tools")
    assert 0.0 <= score <= 1.0
    assert score == 1.0


def test_clamps_to_zero_one_at_low_base():
    """A near-zero base plus a negative nudge must not go below 0.0."""
    d = Domain("Nearly zero", ["x"], 10, 20, product_fit=0.02)
    score = product_fit_score(d, "free diy cheap")
    assert 0.0 <= score <= 1.0
    assert score == 0.0


def test_word_boundary_vs_does_not_fire_on_vsauce():
    """'vs' must match as a whole word only — not as a substring of 'vsauce'."""
    d = Domain("Neutral", ["x"], 10, 20, product_fit=0.5)
    vsauce = product_fit_score(d, "vsauce explains physics")
    neutral = product_fit_score(d, "physics explains vsauce")
    assert vsauce == neutral == 0.5  # no commercial-intent nudge should have fired


def test_word_boundary_vs_does_fire_as_whole_word():
    d = Domain("Neutral", ["x"], 10, 20, product_fit=0.5)
    assert product_fit_score(d, "iphone vs android") > 0.5


def test_word_boundary_diy_does_not_fire_on_diyala():
    """'diy' must match as a whole word only — not as a substring of an unrelated word."""
    d = Domain("Neutral", ["x"], 10, 20, product_fit=0.5)
    assert product_fit_score(d, "history of diyala province") == 0.5


def test_domain_none_defaults_to_neutral_base():
    assert product_fit_score(None, "some random topic") == 0.5


def test_determinism():
    d = _domain("AI / AI tools")
    topic = "best ai automation tools review"
    scores = {product_fit_score(d, topic) for _ in range(5)}
    assert len(scores) == 1


def test_determinism_across_domains_and_topics():
    for d in DOMAINS:
        for topic in ("best gear review", "free diy setup", "career coaching course", "plain topic"):
            a = product_fit_score(d, topic)
            b = product_fit_score(d, topic)
            assert a == b
