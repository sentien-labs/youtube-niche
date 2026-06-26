"""Offline tests for the scoring logic and signals — no API keys or network needed.

Run: python -m pytest -q   (or: python tests/test_logic.py)
"""
from __future__ import annotations

import datetime as dt
import tempfile

from youtube_niche.cache import Cache
from youtube_niche.cli import analyze_topic
from youtube_niche.config import Config, Weights
from youtube_niche.domains import DOMAINS, Domain
from youtube_niche.report import write_reports
from youtube_niche.score import _weighted, opportunity_score
from youtube_niche.signals.comments import comment_demand_score
from youtube_niche.signals.outlier import outlier_score
from youtube_niche.signals.supply import supply_scores
from youtube_niche.util import clamp01, safe_div, saturating
from youtube_niche.youtube_client import APIError, YouTubeClient


def _iso_days_ago(days: int) -> str:
    d = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return d.isoformat().replace("+00:00", "Z")


def test_util():
    assert clamp01(2.0) == 1.0 and clamp01(-1) == 0.0 and clamp01(None) == 0.0
    assert safe_div(10, 0, default=0.0) == 0.0
    assert saturating(1.0, 1.0) == 0.5  # x == knee -> 0.5
    assert saturating(0, 1.0) == 0.0
    assert 0.66 < saturating(2.0, 1.0) < 0.67


def test_outlier_rewards_topic_carried_hits():
    big = [{"views": 1_000_000, "subs": 10_000}]  # ratio 100
    small = [{"views": 5_000, "subs": 500_000}]  # ratio 0.01
    s_big, d_big = outlier_score(big, knee=1.0)
    s_small, _ = outlier_score(small, knee=1.0)
    assert s_big > 0.95 and s_small < 0.05
    assert d_big["max_ratio"] == 100.0


def test_unknown_subscribers_do_not_create_fake_outliers_or_small_channels():
    unknown_subs = [{"views": 1_000_000, "subs": None, "published_at": _iso_days_ago(30)}]
    outlier, detail = outlier_score(unknown_subs, knee=1.0)
    assert outlier == 0.0
    assert detail["n"] == 0 and detail["unknown_subs"] == 1

    scores, supply_detail = supply_scores(unknown_subs, total_results=1000)
    assert scores["small_channel_gap"] is None
    assert supply_detail["small_channel_frac"] is None
    assert supply_detail["unknown_subscriber_results"] == 1


def test_supply_gap_old_thin_small_scores_high():
    thin_old_small = [
        {"views": 20_000, "subs": 2_000, "published_at": _iso_days_ago(1500)},
        {"views": 15_000, "subs": 3_000, "published_at": _iso_days_ago(1200)},
    ]
    scores, detail = supply_scores(thin_old_small, total_results=2)
    assert scores["competition_gap"] > 0.8   # only 2 credible results
    assert scores["age_gap"] > 0.7           # ~3-4y old
    assert scores["small_channel_gap"] == 0.0  # old low-velocity small channels do not prove beatability
    assert detail["credible_results"] == 2

    fast_small = [
        {"views": 200_000, "subs": 5_000, "published_at": _iso_days_ago(30)},
        {"views": 150_000, "subs": 7_000, "published_at": _iso_days_ago(40)},
    ]
    scores_fast, _ = supply_scores(fast_small, total_results=2)
    assert scores_fast["small_channel_gap"] == 1.0

    fresh_crowded_big = [
        {"views": 500_000, "subs": 5_000_000, "published_at": _iso_days_ago(10)}
        for _ in range(40)
    ]
    scores2, _ = supply_scores(fresh_crowded_big, total_results=40)
    assert scores2["competition_gap"] < 0.5
    assert scores2["age_gap"] < 0.1
    assert scores2["small_channel_gap"] == 0.0


def test_competition_gap_uses_dense_sample_not_just_raw_count():
    crowded_sample = [
        {"views": 50_000, "subs": 1_000_000, "published_at": _iso_days_ago(60)}
        for _ in range(15)
    ]
    scores, detail = supply_scores(crowded_sample, total_results=100_000, competition_knee=30)
    assert detail["credible_density"] == 1.0
    assert scores["competition_gap"] == 0.0


def test_title_relevance_filters_competition():
    videos = [
        {"title": "Backdoor Roth IRA tutorial", "views": 50_000, "subs": 10_000, "published_at": _iso_days_ago(30)},
        {"title": "Roth conversion ladder guide", "views": 80_000, "subs": 20_000, "published_at": _iso_days_ago(30)},
        {"title": "Unrelated market news", "views": 120_000, "subs": 20_000, "published_at": _iso_days_ago(30)},
    ]
    scores, detail = supply_scores(videos, total_results=1000, topic="backdoor roth ira")
    assert detail["raw_credible_results"] == 3
    assert detail["credible_results"] == 1
    assert detail["title_match_frac"] == 0.33
    assert scores["competition_gap"] > 0.9


def test_semantic_relevance_is_useful_but_conservative():
    from youtube_niche.relevance import relevance_score

    assert relevance_score(
        "tech companies canceling ai",
        "Why Tech CEOs Are Quietly Cancelling Their AI Plans",
    ).relevant
    assert relevance_score(
        "local ai agent setup",
        "Run AI Agents Locally on a Mini PC",
    ).relevant
    assert not relevance_score(
        "ai automation for real estate agents",
        "Local AI agents are changing small business workflows",
    ).relevant
    assert not relevance_score(
        "real estate syndication",
        "Real Estate vs Stocks: Which Builds Wealth Faster?",
    ).relevant


def test_comment_demand_keyword_path():
    comments = [
        "Great video!",
        "Can you make a video on tax for this?",
        "Please explain the wiring step",
        "first",
    ]
    score, detail = comment_demand_score(comments, llm=None)
    assert detail["n_requests"] == 2 and detail["method"] == "keyword"
    assert score > 0


def test_weighted_renormalizes_when_missing():
    import math

    w = Weights()
    sub = {
        "volume": 0.8,
        "outlier": 0.9,  # present, but must NOT leak into demand
        "trends": None,
        "comment_demand": None,
        "competition_gap": 0.6,
        "age_gap": 0.6,
        "small_channel_gap": 0.6,
        "quality_gap": None,
    }
    out = opportunity_score(sub, w)
    assert abs(out["demand"] - 0.8) < 1e-9  # demand = volume only; outlier excluded
    assert abs(out["supply_gap"] - 0.6) < 1e-9
    assert out["quality_gap"] is None
    # total = weighted geometric mean of demand 0.8 and supply 0.6 over their weights
    expected = math.exp(
        (w.demand * math.log(0.8) + w.supply_gap * math.log(0.6)) / (w.demand + w.supply_gap)
    )
    assert abs(out["opportunity"] - expected) < 1e-9


def test_weighted_all_none_is_none():
    assert _weighted([(None, 1.0), (None, 2.0)]) is None


def test_opportunity_model_prefers_high_demand_low_supply_high_cpm():
    w = Weights()
    underserved = opportunity_score({
        "volume": 0.9, "p75_volume": 0.9, "recent_demand": 0.8,
        "competition_gap": 0.85, "recent_supply_gap": 0.9, "age_gap": 0.7,
        "small_channel_gap": 0.6, "cpm_score": 0.8, "quality_gap": 0.6,
        "demand_gate": 0.9, "confidence": 0.95,
    }, w)
    saturated = opportunity_score({
        "volume": 0.9, "p75_volume": 0.9, "recent_demand": 0.8,
        "competition_gap": 0.1, "recent_supply_gap": 0.1, "age_gap": 0.1,
        "small_channel_gap": 0.0, "cpm_score": 0.8, "quality_gap": 0.6,
        "demand_gate": 0.9, "confidence": 0.95,
    }, w)
    backwater = opportunity_score({
        "volume": 0.08, "p75_volume": 0.1, "recent_demand": 0.0,
        "competition_gap": 0.95, "recent_supply_gap": 1.0, "age_gap": 0.9,
        "small_channel_gap": 0.7, "cpm_score": 0.95, "quality_gap": 0.9,
        "demand_gate": 0.08, "confidence": 0.95,
    }, w)
    assert underserved["opportunity"] > saturated["opportunity"]
    assert underserved["opportunity"] > backwater["opportunity"]


def test_cpm_and_confidence_affect_stage_two_ranking():
    w = Weights()
    base = {
        "volume": 0.7, "p75_volume": 0.7, "recent_demand": 0.5,
        "competition_gap": 0.7, "recent_supply_gap": 0.7, "age_gap": 0.6,
        "small_channel_gap": 0.5, "quality_gap": 0.5, "demand_gate": 0.8,
    }
    high_cpm = opportunity_score({**base, "cpm_score": 0.9, "confidence": 0.9}, w)
    low_cpm = opportunity_score({**base, "cpm_score": 0.2, "confidence": 0.9}, w)
    low_conf = opportunity_score({**base, "cpm_score": 0.9, "confidence": 0.4}, w)
    assert high_cpm["opportunity"] > low_cpm["opportunity"]
    assert high_cpm["opportunity_raw"] == low_conf["opportunity_raw"]
    assert high_cpm["opportunity"] > low_conf["opportunity"]


def test_low_absolute_demand_is_not_an_opportunity():
    """The insurance trap: high views/subs RATIO but tiny absolute views = NOT demand."""
    from youtube_niche.config import Config
    from youtube_niche.discover import assess_domain
    from youtube_niche.domains import Domain

    class Trap:  # small channels, ~13x views/subs ratio, but old + tiny absolute views
        def search(self, q, max_results=20, **k):
            return {"pageInfo": {"totalResults": 5},
                    "items": [{"id": {"videoId": f"{q[:2]}{i}"}} for i in range(5)]}

        def videos(self, ids):
            return {v: {"id": v, "snippet": {"title": v, "channelId": "c" + v,
                    "channelTitle": "x", "publishedAt": _iso_days_ago(1200)},
                    "statistics": {"viewCount": "40000"}} for v in ids}  # 40k views, ~33/day

        def channels(self, ids):
            return {c: {"id": c, "statistics": {"subscriberCount": "3000"}} for c in ids}

    r = assess_domain(Domain("Trap", ["a", "b"], 20, 50, ""), Trap(),
                      Config(), use_trends=False, terms_per_domain=2)
    assert r["outlier"] > 0.8          # the ratio IS high (this is the trap)
    assert r["demand_volume"] < 0.15   # but absolute volume is tiny
    assert r["demand"] < 0.1           # richer demand signals stay low


class _FakeAnalyzeClient:
    def search(self, q, max_results=30, **kwargs):
        return {
            "pageInfo": {"totalResults": 1000},
            "items": [{"id": {"videoId": f"v{i}"}} for i in range(3)],
        }

    def videos(self, ids):
        titles = [
            "Term life insurance for parents explained",
            "Best term life insurance for new parents",
            "How much life insurance parents need",
        ]
        return {
            v: {
                "id": v,
                "snippet": {
                    "title": titles[i],
                    "channelId": f"c{i}",
                    "channelTitle": f"Channel {i}",
                    "publishedAt": _iso_days_ago(30 + i),
                },
                "statistics": {"viewCount": str(80_000 + i * 10_000)},
            }
            for i, v in enumerate(ids)
        }

    def channels(self, ids):
        return {
            c: {"id": c, "statistics": {"subscriberCount": str(10_000 + i * 5_000)}}
            for i, c in enumerate(ids)
        }

    def comment_threads(self, video_id, pages=2):
        return [
            {"snippet": {"topLevelComment": {"snippet": {"textDisplay": "Can you make a video on beneficiaries?"}}}},
            {"snippet": {"topLevelComment": {"snippet": {"textDisplay": "Great explanation"}}}},
        ]


def test_analyze_topic_emits_stage_two_cpm_gate_and_confidence():
    cfg = Config()
    cfg.use_trends = False
    cfg.use_llm = False
    cfg.top_n = 3
    cfg.enrich_n = 3
    row = analyze_topic(
        "term life insurance for parents",
        _FakeAnalyzeClient(),
        llm=None,
        cfg=cfg,
        domain=Domain("Insurance", [], 20, 50),
    )
    assert row["cpm_score"] > 0.8
    assert row["cpm_mid"] == 35
    assert row["demand_gate"] > 0.9
    assert row["confidence"] < 1.0
    assert row["opportunity"] < row["opportunity_raw"]


def test_analyze_topic_gates_unrelated_search_demand():
    """High-view fuzzy search results must not count as demand for the exact niche."""
    cfg = Config()
    cfg.use_trends = False
    cfg.use_llm = False
    cfg.top_n = 3
    cfg.enrich_n = 3

    class C:
        def search(self, q, max_results=30, **kwargs):
            return {
                "pageInfo": {"totalResults": 1000},
                "items": [{"id": {"videoId": f"v{i}"}} for i in range(3)],
            }

        def videos(self, ids):
            titles = ["General market news", "Savings account update", "Budgeting routine"]
            return {
                v: {
                    "id": v,
                    "snippet": {"title": titles[i], "channelId": f"c{i}",
                                "channelTitle": "x", "publishedAt": _iso_days_ago(20)},
                    "statistics": {"viewCount": "500000"},
                }
                for i, v in enumerate(ids)
            }

        def channels(self, ids):
            return {c: {"id": c, "statistics": {"subscriberCount": "10000"}} for c in ids}

        def comment_threads(self, video_id, pages=2):
            raise AssertionError("comments should only be mined from relevant videos")

    row = analyze_topic("backdoor roth ira", C(), llm=None, cfg=cfg,
                        domain=Domain("Personal finance / investing", [], 12, 30))
    assert row["credible_results"] == 0
    assert row["relevance_gate"] == 0.0
    assert row["demand_gate"] == 0.0
    assert row["opportunity_raw"] == 0.0


def test_multi_query_sampling_finds_relevant_later_sample():
    cfg = Config()
    cfg.use_trends = False
    cfg.use_llm = False
    cfg.top_n = 3
    cfg.enrich_n = 3
    cfg.query_samples = 2

    class C:
        def __init__(self):
            self.queries = []

        def search(self, q, max_results=30, **kwargs):
            self.queries.append(q)
            ids = ["x0", "x1"] if len(self.queries) == 1 else ["r0", "r1", "r2"]
            return {"pageInfo": {"totalResults": 1000},
                    "items": [{"id": {"videoId": v}} for v in ids]}

        def videos(self, ids):
            meta = {
                "x0": ("Market recap", 500_000, 20),
                "x1": ("Savings habits", 400_000, 30),
                "r0": ("HSA investing for beginners", 120_000, 30),
                "r1": ("Best HSA investing strategy", 100_000, 40),
                "r2": ("HSA investing explained", 90_000, 45),
            }
            return {
                v: {
                    "id": v,
                    "snippet": {"title": meta[v][0], "channelId": "c" + v,
                                "channelTitle": "x", "publishedAt": _iso_days_ago(meta[v][2])},
                    "statistics": {"viewCount": str(meta[v][1])},
                }
                for v in ids
            }

        def channels(self, ids):
            return {c: {"id": c, "statistics": {"subscriberCount": "12000"}} for c in ids}

        def comment_threads(self, video_id, pages=2):
            return []

    client = C()
    row = analyze_topic("hsa investing", client, llm=None, cfg=cfg,
                        domain=Domain("Personal finance / investing", [], 12, 30, volume_knee_vpd=100))
    assert client.queries == ["hsa investing", '"hsa investing"']
    assert row["query_samples"] == 2
    assert row["query_coverage"] == 0.5
    assert row["credible_results"] == 3
    assert row["relevance_gate"] == 1.0
    assert row["demand_gate"] > 0.9


def test_topic_dedupe_and_ranked_clusters():
    from youtube_niche.topics import dedupe_ranked_rows, dedupe_topics, topic_similarity

    assert topic_similarity("AI agents for real estate", "real estate AI agents") == 1.0
    topics = dedupe_topics([
        "AI agents for real estate",
        "real estate AI agents",
        "run AI agents locally",
    ])
    assert topics == ["AI agents for real estate", "run AI agents locally"]

    rows = dedupe_ranked_rows([
        {"topic": "AI agents for real estate", "opportunity": 0.8},
        {"topic": "real estate AI agents", "opportunity": 0.7},
        {"topic": "run AI agents locally", "opportunity": 0.6},
    ])
    assert len(rows) == 2
    assert rows[0]["cluster_size"] == 2
    assert "real estate AI agents" in rows[0]["cluster_topics"]


def test_external_keyword_metrics_csv_matches_topic():
    from pathlib import Path

    from youtube_niche.external import match_external_metric

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "metrics.csv"
        path.write_text("keyword,search_volume,rpm\nrun ai agents locally,12000,32\n")
        metric = match_external_metric("Run AI agents locally", str(path))
        assert metric is not None
        assert metric.monthly_searches == 12000
        assert metric.demand_score and metric.demand_score > 0.5
        assert metric.cpm_score == 0.8


def test_authority_concentration_penalizes_dominated_results():
    dominated = [
        {"title": "AI automation for dentists", "views": 50_000, "subs": 200_000,
         "published_at": _iso_days_ago(60), "channel_id": "big"}
        for _ in range(10)
    ]
    diverse = [
        {"title": "AI automation for dentists", "views": 50_000, "subs": 20_000,
         "published_at": _iso_days_ago(60), "channel_id": f"c{i}"}
        for i in range(10)
    ]
    s_dom, d_dom = supply_scores(dominated, total_results=10, topic="ai automation dentists")
    s_div, d_div = supply_scores(diverse, total_results=10, topic="ai automation dentists")
    assert d_dom["top3_channel_share"] == 1.0
    assert s_dom["authority_gap"] == 0.0
    assert d_div["top3_channel_share"] == 0.3
    assert s_div["authority_gap"] == 1.0


def test_all_domains_have_stage_two_subtopics_and_report_renders():
    assert all(d.subtopics for d in DOMAINS)
    sample = {
        "topic": "term life insurance for parents",
        "opportunity": 0.5,
        "opportunity_raw": 0.7,
        "opportunity_base": 0.8,
        "confidence": 0.71,
        "demand_gate": 0.9,
        "demand": 0.8,
        "supply_gap": 0.7,
        "cpm_score": 0.88,
        "cpm_mid": 35,
        "cpm_source": "domain:Insurance;keyword:insurance",
        "ad_intent": 0.95,
        "quality_gap": None,
        "volume": 0.8,
        "p75_volume": 0.9,
        "recent_demand": 0.5,
        "median_vpd": 900,
        "p75_vpd": 1200,
        "median_views": 80_000,
        "recent_success_count": 2,
        "outlier": 0.7,
        "trends": None,
        "comment_demand": 0.2,
        "competition_gap": 0.7,
        "recent_supply_gap": 0.6,
        "age_gap": 0.5,
        "small_channel_gap": 0.5,
        "max_outlier_ratio": 8.0,
        "outlier_unknown_subs": 0,
        "credible_results": 8,
        "raw_credible_results": 10,
        "sampled_results": 30,
        "credible_density": 0.3,
        "title_match_frac": 0.8,
        "recent_credible_results": 2,
        "median_age_days": 300,
        "known_subscriber_results": 8,
        "unknown_subscriber_results": 0,
        "small_channel_frac": 0.5,
        "n_comments": 25,
        "n_comment_requests": 2,
        "trends_status": "disabled",
        "quality_status": "disabled",
        "quality_attempted": 0,
        "quality_scored": 0,
        "avg_depth": None,
    }
    with tempfile.TemporaryDirectory() as d:
        csv_path, md_path = write_reports([sample], d, "Insurance")
        assert csv_path.exists() and md_path.exists()
        assert "Confidence" in md_path.read_text()


class _FakeAuth:
    def apply(self, params, headers):
        params["key"] = "fake"


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        raise RuntimeError(self.text)


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, *args, **kwargs):
        return self.responses.pop(0)


def test_quota_tracks_search_calls_separately_and_failed_units():
    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/cache.sqlite")
        client = YouTubeClient(_FakeAuth(), cache, daily_quota=10, reserve=0, daily_search_limit=2)
        client.session = _FakeSession([
            _FakeResponse(200, {"items": [], "pageInfo": {"totalResults": 0}}),
            _FakeResponse(200, {"items": []}),
            _FakeResponse(404, {"error": {"errors": [{"reason": "notFound"}]}}),
        ])
        client.search("x")
        assert client.search_calls_used() == 1
        assert client.units_spent() == 0
        client.videos(["v1"])
        assert client.units_spent() == 1
        try:
            client.channels(["missing"])
        except APIError:
            pass
        else:
            raise AssertionError("expected APIError")
        assert client.units_spent() == 2
        cache.close()


def test_newcomer_ceiling_reflects_small_channel_views():
    """Demand a NEW creator can capture = what small channels achieve, not the giants' median."""
    from youtube_niche.signals.volume import volume_score

    # Only giant channels get the views -> no newcomer ceiling (not capturable from zero).
    big_only = [
        {"views": 500_000, "subs": 2_000_000, "published_at": _iso_days_ago(60)},
        {"views": 400_000, "subs": 3_000_000, "published_at": _iso_days_ago(80)},
    ]
    _, d_big = volume_score(big_only, small_channel_subs=50_000)
    assert d_big["newcomer_volume"] is None and d_big["newcomer_sample"] == 0

    # Small channels pull real views/day -> high newcomer ceiling.
    small_win = [
        {"views": 300_000, "subs": 8_000, "published_at": _iso_days_ago(30)},   # ~10k/day
        {"views": 120_000, "subs": 12_000, "published_at": _iso_days_ago(40)},  # ~3k/day
    ]
    _, d_small = volume_score(small_win, small_channel_subs=50_000)
    assert d_small["newcomer_sample"] == 2 and d_small["newcomer_volume"] > 0.8


def test_trends_cache_returns_stored_result_without_network():
    """A cached Trends result is returned (case-insensitively) without hitting pytrends."""
    from youtube_niche.signals.trends import trends_score

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        cache.set(
            cache.key("trends", "dividend growth investing", "US"),
            {"score": 0.73, "detail": {"status": "ok", "slope_score": 0.6}},
        )
        score, detail = trends_score("Dividend Growth Investing", geo="US", cache=cache)
        assert score == 0.73 and detail["cached"] is True
        cache.close()


def test_trends_baseline_cache_key_returns_stored_result_without_network():
    from youtube_niche.signals.trends import trends_score

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        cache.set(
            cache.key("trends", "hsa investing", "US", ["personal finance"]),
            {"score": 0.62, "detail": {"status": "ok", "level_score": 0.7}},
        )
        score, detail = trends_score(
            "HSA Investing",
            geo="US",
            cache=cache,
            baseline_terms=["personal finance"],
        )
        assert score == 0.62 and detail["cached"] is True
        assert detail["level_score"] == 0.7
        cache.close()


def test_per_domain_knee_changes_demand_gate():
    """A domain's calibrated knee scales demand within that domain (stage-2 discrimination)."""
    from youtube_niche.domains import Domain

    cfg = Config()
    cfg.use_trends = False
    cfg.use_llm = False
    cfg.top_n = 3
    cfg.enrich_n = 3

    class C:  # videos at ~250 views/day (25k views, 100 days)
        def search(self, q, max_results=30, **k):
            return {"pageInfo": {"totalResults": 1000},
                    "items": [{"id": {"videoId": f"v{i}"}} for i in range(3)]}

        def videos(self, ids):
            return {v: {"id": v, "snippet": {"title": "backdoor roth ira tips", "channelId": "c" + v,
                    "channelTitle": "x", "publishedAt": _iso_days_ago(100)},
                    "statistics": {"viewCount": "25000"}} for v in ids}

        def channels(self, ids):
            return {c: {"id": c, "statistics": {"subscriberCount": "10000"}} for c in ids}

        def comment_threads(self, video_id, pages=2):
            return []

    low_knee = analyze_topic("backdoor roth ira", C(), None, cfg,
                             domain=Domain("D", [], 20, 50, volume_knee_vpd=50))
    high_knee = analyze_topic("backdoor roth ira", C(), None, cfg,
                              domain=Domain("D", [], 20, 50, volume_knee_vpd=5000))
    assert low_knee["demand_gate"] > high_knee["demand_gate"]


def test_find_breakouts_keeps_small_fast_only():
    """Breakout = small channel + above the velocity floor. Giants and slow videos are dropped."""
    from youtube_niche.config import Config
    from youtube_niche.winners import find_breakouts

    class C:
        def search(self, q, max_results=30, **k):
            return {"items": [{"id": {"videoId": f"v{i}"}} for i in range(4)]}

        def videos(self, ids):
            meta = {
                "v0": ("Backdoor Roth IRA in 5 minutes", 300_000, 30),  # small, ~10k/day -> KEEP
                "v1": ("Index funds basics", 5_000_000, 30),           # giant channel -> drop
                "v2": ("HSA tricks", 200, 30),                         # below view floor -> drop
                "v3": ("Slow dividend video", 50_000, 2000),           # ~25/day < min_vpd -> drop
            }
            return {
                vid: {"id": vid, "snippet": {"title": tt, "channelId": "c" + vid,
                      "channelTitle": "x", "publishedAt": _iso_days_ago(age)},
                      "statistics": {"viewCount": str(views)}}
                for vid, (tt, views, age) in meta.items() if vid in ids
            }

        def channels(self, ids):
            subs = {"cv0": 8_000, "cv1": 4_000_000, "cv2": 3_000, "cv3": 9_000}
            return {c: {"id": c, "statistics": {"subscriberCount": str(subs[c])}} for c in ids}

        def search_calls_remaining(self):
            return 10

    out = find_breakouts(C(), Config(), ["roth"], recent_days=180, min_vpd=100, max_per_term=8)
    assert [v["title"] for v in out] == ["Backdoor Roth IRA in 5 minutes"]


def test_find_breakouts_drops_noise_and_dedupes_channel():
    """Shorts, trailers, non-English are dropped; channel-spam collapses to its best video."""
    from youtube_niche.config import Config
    from youtube_niche.winners import find_breakouts

    rows = {  # vid: (title, views, age, duration_iso, lang, channel)
        "v0": ("Rent vs buy a house in 2026", 300_000, 30, "PT12M", "en", "c0"),       # KEEP
        "v1": ("Quick money tip #Shorts", 600_000, 30, "PT0M30S", "en", "c1"),          # short -> drop
        "v2": ("Retirement Plan - Official Trailer", 400_000, 30, "PT2M", "en", "c2"),   # junk -> drop
        "v3": ("AI দিয়ে ভিডিও বানানো Text To Video", 300_000, 30, "PT10M", None, "c3"),  # non-Latin script -> drop
        "v4": ("Budget tips part one", 250_000, 30, "PT8M", "en", "shared"),             # dup channel (lower vpd)
        "v5": ("Budget tips part two", 290_000, 30, "PT8M", "en", "shared"),             # dup channel (kept)
    }

    class C:
        def search(self, q, max_results=30, **k):
            return {"items": [{"id": {"videoId": v}} for v in rows]}

        def videos(self, ids):
            return {
                v: {"id": v, "snippet": {"title": tt, "channelId": ch, "channelTitle": "x",
                    "publishedAt": _iso_days_ago(age), "defaultAudioLanguage": lang},
                    "contentDetails": {"duration": dur}, "statistics": {"viewCount": str(views)}}
                for v, (tt, views, age, dur, lang, ch) in rows.items() if v in ids
            }

        def channels(self, ids):
            return {c: {"id": c, "statistics": {"subscriberCount": "9000"}} for c in ids}

        def search_calls_remaining(self):
            return 10

    out = find_breakouts(C(), Config(), ["budget"], recent_days=180, min_vpd=100, max_per_term=20)
    titles = {v["title"] for v in out}
    assert titles == {"Rent vs buy a house in 2026", "Budget tips part two"}


def test_backtest_breakout_mining_allows_cached_searches_when_budget_is_zero():
    from youtube_niche.backtest import mine_holdout_breakouts
    from youtube_niche.config import Config
    from youtube_niche.domains import Domain

    class C:
        def __init__(self):
            self.searches = 0

        def search_calls_remaining(self):
            return 0

        def search(self, q, max_results=30, **k):
            self.searches += 1
            return {"items": [{"id": {"videoId": "v0"}}]}

        def videos(self, ids):
            return {
                "v0": {
                    "id": "v0",
                    "snippet": {
                        "title": "Rent vs Buy a House in 2026",
                        "channelId": "c0",
                        "channelTitle": "x",
                        "publishedAt": _iso_days_ago(20),
                        "defaultAudioLanguage": "en",
                    },
                    "contentDetails": {"duration": "PT12M"},
                    "statistics": {"viewCount": "300000"},
                }
            }

        def channels(self, ids):
            return {"c0": {"id": "c0", "statistics": {"subscriberCount": "8000"}}}

    domain = Domain("Demo finance", ["rent vs buy"], 1, 2)
    cfg = Config()
    now = dt.datetime.now(dt.timezone.utc)
    rows = mine_holdout_breakouts(
        C(), cfg, domain, now - dt.timedelta(days=180), None, min_vpd=100, max_per_term=8
    )
    assert [r["title"] for r in rows] == ["Rent vs Buy a House in 2026"]


def test_keyword_niches_extracts_repeated_phrases():
    from youtube_niche.winners import _keyword_niches

    titles = [
        "Dividend growth investing for beginners",
        "Dividend growth investing strategy",
        "My dividend growth portfolio",
        "Roth conversion ladder explained",
    ]
    assert "dividend growth" in _keyword_niches(titles, max_niches=10)


def test_calibrate_percentile():
    from youtube_niche.calibrate import percentile

    assert percentile([], 0.5) is None
    assert percentile([10], 0.5) == 10
    assert percentile([0, 100], 0.5) == 50
    assert percentile([0, 50, 100], 0.5) == 50


def test_backtest_topic_matching_and_labels():
    from pathlib import Path

    from youtube_niche.backtest import (
        candidate_topics,
        discovered_candidates_from_breakouts,
        matched_breakouts,
        simple_label_from_title,
        text_matches_topic,
    )
    from youtube_niche.subtopics import save_discovered

    title = "Want to Run AI Agents Locally? Here is The Bare Minimum Setup/Build"
    assert simple_label_from_title(title) == "run ai agents locally"
    assert text_matches_topic(title, "run ai agents locally")
    assert text_matches_topic(title, "local ai agent setup")
    assert not text_matches_topic(title, "backdoor roth ira")
    assert not text_matches_topic(title, "ai automation for real estate agents")
    assert text_matches_topic(
        "Why Tech CEOs Are Quietly Cancelling Their AI Plans",
        "tech companies canceling ai",
    )
    assert text_matches_topic("I Retired Broke... And This Is What It Feels Like", "retiring broke")
    assert text_matches_topic(
        "These 7 REASONS Will Make You File for Social Security at Age 62",
        "social security at 62",
    )
    assert text_matches_topic(
        "How To Become Debt Free Fast,Best Budgeting Tips 2026,Dave Ramsey Inspired Budget Plan,make money,",
        "how to become debt free",
    )
    assert text_matches_topic(
        "Real Estate Vs Stocks — The Real Math (Which One Will Make You More Money?)",
        "real estate vs stocks",
    )
    assert not text_matches_topic(
        "Real Estate Vs Stocks — The Real Math (Which One Will Make You More Money?)",
        "real estate syndication",
    )
    assert text_matches_topic(
        "$25/week into Fidelity Index Funds will SURPASS Your Full-Time Job ($4800 /month)",
        "fidelity index funds investing",
    )
    assert text_matches_topic(
        "Steps to start a SUCCESSFUL small business in 2026 Everything you need to know",
        "start small business 2026",
    )
    assert text_matches_topic(
        "The Top 5 AI Businesses To Start In 2026",
        "ai business ideas 2026",
    )
    assert text_matches_topic(
        "Start Selling Online in 2026 | A Beginner's Guide to eBay!",
        "how to sell on ebay",
    )

    breakouts = [
        {"video_id": "a", "title": title},
        {"video_id": "b", "title": "Why Tech CEOs Are Quietly Cancelling Their AI Plans"},
    ]
    hits = matched_breakouts("run ai agents locally", ["run ai agents locally"], breakouts)
    assert [h["video_id"] for h in hits] == ["a"]

    domain = Domain("AI", [], 1, 2, subtopics=["generic ai tools"])
    assert candidate_topics(domain, ["run ai agents locally"], "both", 1) == [
        ("run ai agents locally", "holdout_label")
    ]
    assert candidate_topics(domain, [], "effective", 10) == [
        ("generic ai tools", "subtopic")
    ]

    with tempfile.TemporaryDirectory() as d:
        reg = Path(d) / "reg.json"
        save_discovered("AI", ["run ai agents locally"], path=reg)
        assert candidate_topics(domain, [], "effective", 10, subtopics_registry=reg) == [
            ("run ai agents locally", "discovered_subtopic")
        ]
    temporal_candidates = discovered_candidates_from_breakouts(
        [{"title": title}], llm=None, max_candidates=5
    )
    assert temporal_candidates == [("run ai agents locally", "temporal_discovered_subtopic")]


def test_backtest_registry_aggregate_report():
    import csv
    import json
    from pathlib import Path

    from youtube_niche.backtest import REGISTRY_FIELDS, aggregate_registry

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "runs.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=REGISTRY_FIELDS)
            w.writeheader()
            w.writerow({
                "run_id": "r1",
                "domain": "AI / AI tools",
                "first_hit_rank": "2",
                "metrics_json": json.dumps({
                    "precision@5": "0.40",
                    "breakout recall@5": "0.50",
                    "precision@10": "0.30",
                    "breakout recall@10": "0.75",
                }),
            })
        csv_path, md_path = aggregate_registry(path, d)
        assert csv_path.exists() and md_path.exists()
        assert "AI / AI tools" in md_path.read_text()


def test_forward_snapshot_capture_rows():
    from pathlib import Path

    from youtube_niche.forward import capture_score_snapshot, summarize_snapshots

    with tempfile.TemporaryDirectory() as d:
        path, n = capture_score_snapshot(
            [{"topic": "run ai agents locally", "opportunity": 0.42, "confidence": 0.8}],
            d,
            "AI",
            horizons=[30, 60],
        )
        assert path.exists() and n == 2
        assert "run ai agents locally" in path.read_text()
        csv_path, md_path = summarize_snapshots(path, d)
        assert csv_path.exists() and md_path.exists()
        assert "AI" in md_path.read_text()


def test_demo_cli_writes_report_without_auth():
    from pathlib import Path

    from youtube_niche.cli import main

    with tempfile.TemporaryDirectory() as d:
        assert main(["--demo", "--out-dir", d]) == 0
        outputs = list(Path(d).glob("demo-*.md"))
        assert outputs
        text = outputs[0].read_text()
        assert "run ai agents locally" in text
        assert "Query consensus" in text


def test_cache_only_client_uses_cache_and_raises_on_miss():
    from youtube_niche.youtube_client import CacheMiss

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/cache.sqlite")
        client = YouTubeClient(None, cache, cache_only=True)
        key = cache.key(
            "yt",
            "videos",
            {"id": "v1", "part": "statistics,snippet,contentDetails"},
        )
        cache.set(key, {"items": [{"id": "v1", "snippet": {}}]})
        assert "v1" in client.videos(["v1"])
        try:
            client.channels(["c1"])
        except CacheMiss:
            pass
        else:
            raise AssertionError("expected cache miss")
        cache.close()


def test_retryable_errors_do_not_burn_quota():
    """429/5xx are unbilled + retried: they must NOT each charge a search/unit."""
    import youtube_niche.youtube_client as yc

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/cache.sqlite")
        client = YouTubeClient(_FakeAuth(), cache, daily_quota=1000, reserve=0, daily_search_limit=5)
        client.session = _FakeSession([
            _FakeResponse(429, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}),
            _FakeResponse(503, {"error": {"errors": [{"reason": "backendError"}]}}),
            _FakeResponse(200, {"items": [], "pageInfo": {"totalResults": 0}}),
        ])
        orig_sleep = yc.time.sleep
        yc.time.sleep = lambda *a, **k: None
        try:
            client.search("x")
        finally:
            yc.time.sleep = orig_sleep
        assert client.search_calls_used() == 1  # 2 retries charged nothing
        cache.close()


def test_backtest_default_holdout_start_uses_stable_date_boundary():
    from youtube_niche.backtest import _default_holdout_start

    now = dt.datetime(2026, 6, 25, 14, 46, 12, tzinfo=dt.timezone.utc)
    assert _default_holdout_start(now, 180).isoformat() == "2025-12-27T00:00:00+00:00"


def test_failure_audit_compares_curated_and_discovered_seed_coverage():
    from pathlib import Path

    from youtube_niche.audit import audit_backtest_report, write_failure_audit
    from youtube_niche.subtopics import save_discovered

    report = """# Backtest — AI / AI tools

_Generated 20260625-123742. Holdout window: 2025-12-27 to now._

## Metrics

- **breakout videos**: 2
- **scored candidates**: 1
- **positive candidates**: 0
- **first hit rank**: none

## Holdout Breakouts

- 31724/day · 9,610 subs · Why Tech CEOs Are Quietly Cancelling Their AI Plans
- 4368/day · 32,200 subs · Want to Run AI Agents Locally? Here is The Bare Minimum Setup/Build

## Ranked Candidates

1. **local business ai automation** — 24% (miss, raw 50%, confidence 47%)
"""
    with tempfile.TemporaryDirectory() as d:
        report_path = Path(d) / "bt.md"
        report_path.write_text(report)
        registry_path = Path(d) / "reg.json"
        save_discovered(
            "AI / AI tools",
            ["run ai agents locally", "tech companies canceling ai"],
            meta={"generated_at": "2026-06-25T18:00:00+00:00"},
            path=registry_path,
        )

        summary, details = audit_backtest_report(report_path, registry_path)
        assert summary["curated_covered"] == 0
        assert summary["discovered_covered"] == 2
        assert summary["discovered_timing"] == "after-or-unknown-holdout"
        assert "seed-source gap" in summary["assessment"]
        assert len(details) == 2

        summary_expanded, _ = audit_backtest_report(
            report_path,
            registry_path,
            candidate_mode="expanded",
            autocomplete_fn=lambda q, region="US", lang="en": ["run ai agents locally"] if q == "ai tools for business" else [],
        )
        assert summary_expanded["candidate_mode"] == "expanded"
        assert summary_expanded["candidate_covered"] == 1

        summary2, details2 = audit_backtest_report(
            report_path,
            registry_path,
            candidate_mode="hybrid",
            autocomplete_fn=lambda q, region="US", lang="en": ["run ai agents locally"] if q == "ai tools for business" else [],
        )
        assert summary2["candidate_mode"] == "hybrid"
        assert summary2["candidate_covered"] == 2
        assert any("run ai agents locally" in d["candidate_matches"] for d in details2)

        csv_path, md_path = write_failure_audit([report_path], d, registry_path)
        assert csv_path.exists() and md_path.exists()
        text = md_path.read_text()
        assert "Why Tech CEOs" in text
        assert "tech companies canceling ai" in text


def test_backtest_metrics_split_by_source():
    """Per-source precision must separate circular holdout labels from clean subtopics."""
    from youtube_niche.backtest import compute_metrics

    breakouts = [{"video_id": "b1"}, {"video_id": "b2"}, {"video_id": "b3"}, {"video_id": "b4"}]
    rows = [  # already ranked by opportunity
        {"candidate_source": "holdout_label", "backtest_hit": True, "hit_video_ids": ["b1"]},
        {"candidate_source": "subtopic", "backtest_hit": True, "hit_video_ids": ["b2"]},
        {"candidate_source": "holdout_label", "backtest_hit": True, "hit_video_ids": ["b3"]},
        {"candidate_source": "subtopic", "backtest_hit": False, "hit_video_ids": []},
        {"candidate_source": "discovered_subtopic", "backtest_hit": True, "hit_video_ids": ["b1"]},
        {"candidate_source": "temporal_discovered_subtopic", "backtest_hit": True, "hit_video_ids": ["b4"]},
    ]
    m = compute_metrics(rows, breakouts, [5])
    assert m["clean source"] == "subtopic"
    assert "holdout_label note" in m  # flagged as circular
    assert "discovered_subtopic note" in m  # flagged unless generated before holdout
    assert "temporal_discovered_subtopic note" in m
    # holdout labels hit 2/2; subtopics hit 1/2 — the honest, lower number
    assert m["holdout_label precision@5"] == "1.00"
    assert m["subtopic precision@5"] == "0.50"
    assert m["discovered_subtopic precision@5"] == "1.00"
    assert m["temporal_discovered_subtopic precision@5"] == "1.00"
    assert m["precision@5"] == "0.80"  # mixed overall

    temporal_only = compute_metrics(
        [{"candidate_source": "temporal_discovered_subtopic", "backtest_hit": True, "hit_video_ids": ["b1"]}],
        breakouts,
        [5],
    )
    assert temporal_only["clean source"] == "temporal_discovered_subtopic"


def test_resolve_due_snapshots_marks_hit_and_miss():
    """resolve mines breakouts per due topic; small-at-publish matches count as hits."""
    from youtube_niche.config import Config
    from youtube_niche.forward import resolve_due_snapshots

    now = dt.datetime(2026, 6, 24, tzinfo=dt.timezone.utc)

    def created(days):
        return (now - dt.timedelta(days=days)).isoformat()

    def due(days_from_now):
        return (now + dt.timedelta(days=days_from_now)).date().isoformat()

    def pub_days_ago(age):
        return (now - dt.timedelta(days=age)).isoformat().replace("+00:00", "Z")

    class C:
        def search(self, q, **k):
            vid = "r1" if "rent" in q.lower() else "g1"
            return {"items": [{"id": {"videoId": vid}}]}

        def videos(self, ids):
            meta = {  # vid: (title, views, age_days, duration, lang, channel)
                "r1": ("Rent vs Buy a House in 2026", 300_000, 20, "PT10M", "en", "cr1"),
                "g1": ("Backdoor Roth IRA explained", 500_000, 20, "PT10M", "en", "cg1"),
            }
            return {
                v: {"id": v, "snippet": {"title": tt, "channelId": ch, "channelTitle": "x",
                    "publishedAt": pub_days_ago(age), "defaultAudioLanguage": lang},
                    "contentDetails": {"duration": dur}, "statistics": {"viewCount": str(views)}}
                for v, (tt, views, age, dur, lang, ch) in meta.items() if v in ids
            }

        def channels(self, ids):
            meta = {
                "cr1": (200_000, 25),    # grew past cap, but was ~40k at publish -> hit
                "cg1": (4_000_000, 1000),  # always giant -> filtered out -> miss
            }
            return {
                c: {
                    "id": c,
                    "statistics": {"subscriberCount": str(meta[c][0])},
                    "snippet": {"publishedAt": pub_days_ago(meta[c][1])},
                }
                for c in ids
            }

        def search_calls_remaining(self):
            return 10

    rows = [
        {"topic": "rent vs buy a house", "created_at": created(40), "due_at": due(-10),
         "horizon_days": "30", "status": "pending", "breakout_count": "", "checked_at": "", "notes": ""},
        {"topic": "rent vs buy a house", "created_at": created(40), "due_at": due(50),
         "horizon_days": "90", "status": "pending", "breakout_count": "", "checked_at": "", "notes": ""},
        {"topic": "backdoor roth ira", "created_at": created(40), "due_at": due(-5),
         "horizon_days": "30", "status": "pending", "breakout_count": "", "checked_at": "", "notes": ""},
    ]
    rows, summary = resolve_due_snapshots(rows, C(), Config(), now=now)
    assert summary["due"] == 2 and summary["resolved"] == 2 and summary["searches"] == 2
    assert rows[0]["status"] == "checked" and rows[0]["breakout_count"] == 1  # small at publish -> hit
    assert rows[0]["notes"].startswith("hit:")
    assert rows[1]["status"] == "pending"  # horizon not yet due
    assert rows[2]["status"] == "checked" and rows[2]["breakout_count"] == 0  # giant -> miss


def test_community_validation_flags_bad_rows():
    from youtube_niche.community import validate_rows

    assert validate_rows([{"topic": "x", "opportunity": "0.6", "status": "checked", "breakout_count": "2"}]) == []
    assert any("required column" in m for m in validate_rows([{"topic": "x", "opportunity": "0.6"}]))
    bad = validate_rows([{"topic": "x", "opportunity": "9", "status": "checked", "breakout_count": "-1"}])
    assert any("opportunity in [0,1]" in m for m in bad)
    assert any("breakout_count >= 0" in m for m in bad)
    pending = [{"topic": "x", "opportunity": "0.6", "status": "pending", "breakout_count": ""}]
    assert any("no resolved" in m for m in validate_rows(pending))


def test_community_calibration_curve_and_auc():
    from youtube_niche.community import calibration_curve

    def row(opp, bc):
        return {"topic": "t", "opportunity": str(opp), "status": "checked", "breakout_count": str(bc)}

    # higher scores break out, lower scores don't -> perfect ranking
    rows = [row(0.1, 0), row(0.15, 0), row(0.25, 0), row(0.45, 0),
            row(0.55, 1), row(0.65, 2), row(0.85, 1), row(0.95, 3)]
    bands, overall = calibration_curve(rows)
    assert overall["resolved_rows"] == 8 and overall["hits"] == 4
    assert overall["auc"] == 1.0  # every breakout outranks every non-breakout
    assert overall["top_half_hit_rate"] == 1.0 and overall["bottom_half_hit_rate"] == 0.0
    assert overall["monotonic"] is True
    # pending rows are ignored
    _, overall2 = calibration_curve(rows + [row(0.9, 0) | {"status": "pending"}])
    assert overall2["resolved_rows"] == 8


def test_scoring_golden_is_stable():
    """Lock full-pipeline scoring (fixed dates + as_of) so logic changes are intentional, not silent.

    Regenerate the expected numbers only when you deliberately change scoring math.
    """
    as_of = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    def pub(days):
        return (as_of - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")

    class C:
        def search(self, q, **k):
            return {"items": [{"id": {"videoId": v}} for v in ("g1", "g2", "g3")],
                    "pageInfo": {"totalResults": 3}}

        def videos(self, ids):
            meta = {  # vid: (title, views, subs, age_days, duration)
                "g1": ("Dividend growth investing for beginners", 200_000, 12_000, 60, "PT12M"),
                "g2": ("My dividend growth portfolio update", 120_000, 8_000, 90, "PT10M"),
                "g3": ("Dividend growth investing strategy 2026", 90_000, 30_000, 120, "PT9M"),
            }
            return {
                v: {"id": v, "snippet": {"title": t, "channelId": "c" + v, "channelTitle": "x",
                    "publishedAt": pub(age), "defaultAudioLanguage": "en"},
                    "contentDetails": {"duration": dur}, "statistics": {"viewCount": str(views)}}
                for v, (t, views, subs, age, dur) in meta.items() if v in ids
            }

        def channels(self, ids):
            subs = {"cg1": 12_000, "cg2": 8_000, "cg3": 30_000}
            return {c: {"id": c, "statistics": {"subscriberCount": str(subs[c])}} for c in ids}

        def comment_threads(self, vid, pages=2):
            return []

    cfg = Config()
    cfg.use_trends = cfg.use_llm = False
    cfg.comment_videos = 0
    # Pin the velocity clock to as_of so the golden numbers stay deterministic. (In a real backtest
    # velocity_now defaults to the wall-clock; pinning it here isolates the scoring math under test.)
    row = analyze_topic("dividend growth investing", C(), None, cfg, as_of=as_of, velocity_now=as_of)
    assert round(row["opportunity"], 3) == 0.372
    assert round(row["opportunity_raw"], 3) == 0.737
    assert round(row["demand"], 3) == 0.646
    assert round(row["supply_gap"], 3) == 0.801
    assert round(row["cpm_score"], 3) == 0.82
    assert round(row["confidence"], 3) == 0.504


def test_discovered_subtopics_registry_and_fallback():
    from youtube_niche.subtopics import (
        discovered_subtopics,
        effective_subtopics,
        save_discovered,
    )

    dom = Domain("Personal finance / investing", [], 12, 30,
                 subtopics=["backdoor roth ira", "coast fire"])
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/reg.json"
        # before any emit: fall back to the hand-curated list
        subs, src = effective_subtopics(dom, path)
        assert src == "curated" and subs == ["backdoor roth ira", "coast fire"]
        # winners-first writes data-derived niches; they now take precedence
        save_discovered(dom.name, ["social security timing", "retirement income", "debt free"],
                        meta={"breakout_count": 11, "method": "llm"}, path=path)
        assert discovered_subtopics(dom.name, path) == ["social security timing", "retirement income", "debt free"]
        subs2, src2 = effective_subtopics(dom, path)
        assert src2 == "discovered" and subs2[0] == "social security timing"
        # a different domain with nothing recorded still falls back to curated
        other = Domain("Other", [], 1, 2, subtopics=["x"])
        assert effective_subtopics(other, path) == (["x"], "curated")


def test_default_discovered_registry_uses_writable_user_overlay():
    import os
    from pathlib import Path

    from youtube_niche.subtopics import (
        ENV_REGISTRY,
        default_user_registry,
        discovered_subtopics,
        save_discovered,
    )

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "reg.json"
        old = os.environ.get(ENV_REGISTRY)
        os.environ[ENV_REGISTRY] = str(path)
        try:
            assert default_user_registry() == path
            written = save_discovered("User Domain", ["fresh breakout niche"])
            assert written == path
            assert discovered_subtopics("User Domain") == ["fresh breakout niche"]
        finally:
            if old is None:
                os.environ.pop(ENV_REGISTRY, None)
            else:
                os.environ[ENV_REGISTRY] = old


def test_winners_emit_subtopics_flag_parses():
    from youtube_niche.winners import build_parser

    args = build_parser().parse_args(["--domain", "personal finance", "--emit-subtopics"])
    assert args.emit_subtopics is True


def test_grok_llm_provider_is_wired_without_requiring_login():
    from youtube_niche.backtest import build_parser as build_backtest_parser
    from youtube_niche.cli import build_parser as build_cli_parser
    from youtube_niche.llm import GrokCliBackend, LLM_PROVIDERS
    from youtube_niche.winners import build_parser as build_winners_parser

    assert "grok" in LLM_PROVIDERS
    assert build_cli_parser().parse_args(["x", "--llm-provider", "grok"]).llm_provider == "grok"
    assert build_backtest_parser().parse_args(["--fixtures", "--llm-provider", "grok"]).llm_provider == "grok"
    temporal_args = build_backtest_parser().parse_args([
        "--fixtures", "--candidate-source", "temporal", "--seed-window-days", "90",
    ])
    assert temporal_args.candidate_source == "temporal"
    assert temporal_args.seed_window_days == 90
    hybrid_args = build_backtest_parser().parse_args(["--fixtures", "--candidate-source", "hybrid"])
    assert hybrid_args.candidate_source == "hybrid"
    expanded_args = build_backtest_parser().parse_args(["--fixtures", "--candidate-source", "expanded"])
    assert expanded_args.candidate_source == "expanded"
    backtest_probe_args = build_backtest_parser().parse_args([
        "--fixtures", "--max-probe-terms", "8", "--no-probe-autocomplete",
    ])
    assert backtest_probe_args.max_probe_terms == 8
    assert backtest_probe_args.no_probe_autocomplete is True
    assert build_winners_parser().parse_args(["--domain", "AI", "--llm-provider", "grok"]).llm_provider == "grok"
    winner_probe_args = build_winners_parser().parse_args([
        "--domain", "AI", "--max-probe-terms", "7", "--no-probe-autocomplete",
    ])
    assert winner_probe_args.max_probe_terms == 7
    assert winner_probe_args.no_probe_autocomplete is True

    backend = GrokCliBackend(bin="definitely-not-a-real-grok-binary")
    assert backend.name == "grok"
    assert backend.available is False


def test_grok_model_env_and_tier_overrides_are_wired_without_requiring_login():
    import os

    from youtube_niche.config import Config
    from youtube_niche.llm import make_llm

    keys = ["GROK_MODEL", "GROK_COMMENT_MODEL", "GROK_QUALITY_MODEL", "LLM_PROVIDER"]
    old = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["LLM_PROVIDER"] = "grok"
        os.environ["GROK_MODEL"] = "grok-composer-2.5-fast"
        os.environ["GROK_QUALITY_MODEL"] = "grok-build"
        os.environ.pop("GROK_COMMENT_MODEL", None)

        cfg = Config.from_env()
        assert cfg.llm_provider == "grok"
        assert cfg.grok_model == "grok-composer-2.5-fast"
        assert cfg.grok_quality_model == "grok-build"

        llm = make_llm(cfg)
        assert llm.backend.name == "grok"
        assert llm.backend.models == {
            "cheap": "grok-composer-2.5-fast",
            "quality": "grok-build",
        }
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_fixtures_backtest_runs_keyless():
    from youtube_niche.backtest import main as backtest_main

    with tempfile.TemporaryDirectory() as d:
        rc = backtest_main(["--fixtures", "--candidate-source", "subtopics", "--no-registry", "--out-dir", d])
        assert rc == 0
        from pathlib import Path
        reports = list(Path(d).glob("backtest-*.md"))
        assert reports and "Backtest" in reports[0].read_text()


def test_fixtures_benchmark_runs_multi_window_and_calibrates_weights():
    from pathlib import Path

    from youtube_niche.benchmark import main as benchmark_main, window_cutoffs

    assert window_cutoffs(
        3,
        90,
        latest_cutoff="2026-06-01",
        step_days=30,
    ) == ["2026-06-01", "2026-05-02", "2026-04-02"]

    with tempfile.TemporaryDirectory() as d:
        rc = benchmark_main([
            "--fixtures",
            "--windows", "2",
            "--candidate-source", "subtopics",
            "--max-candidates", "3",
            "--top-n", "5",
            "--out-dir", d,
            "--calibrate-weights",
        ])
        assert rc == 0
        out = Path(d)
        assert (out / "backtest-runs.csv").exists()
        assert list(out.glob("benchmark-*.md"))
        assert list(out.glob("backtest-aggregate-*.md"))
        assert list(out.glob("weight-calibration-*.md"))
        assert len(list(out.glob("backtest-*.csv"))) >= 2


def test_weighting_grid_prefers_supply_when_supply_predicts_hits():
    from youtube_niche.weighting import evaluate_weight_grid

    rows = [
        {"backtest_hit": "true", "opportunity": "0.6", "confidence": "1", "demand_gate": "1",
         "demand": "0.55", "supply_gap": "0.95", "cpm_score": "0.6"},
        {"backtest_hit": "true", "opportunity": "0.58", "confidence": "1", "demand_gate": "1",
         "demand": "0.50", "supply_gap": "0.90", "cpm_score": "0.6"},
        {"backtest_hit": "false", "opportunity": "0.7", "confidence": "1", "demand_gate": "1",
         "demand": "0.95", "supply_gap": "0.25", "cpm_score": "0.6"},
        {"backtest_hit": "false", "opportunity": "0.68", "confidence": "1", "demand_gate": "1",
         "demand": "0.90", "supply_gap": "0.30", "cpm_score": "0.6"},
    ]
    baseline, evaluations = evaluate_weight_grid(rows)
    best = evaluations[0]
    assert baseline["auc"] == 0.0
    assert best["auc"] == 1.0
    assert best["weights"]["supply_gap"] >= best["weights"]["demand"]


def test_hybrid_domain_candidates_keep_source_labels_and_broaden_coverage():
    import json
    from pathlib import Path

    from youtube_niche.candidates import domain_seed_candidates, source_summary

    def fake_autocomplete(q, region="US", lang="en"):
        assert region == "US" and lang == "en"
        return {
            "ai tools": ["run ai agents locally", "ai tools for accountants"],
            "ai automation": ["local business ai automation"],
        }.get(q, [])

    domain = Domain(
        "AI",
        ["ai tools", "ai automation"],
        6,
        18,
        subtopics=["ai automation for real estate agents", "chatgpt for accountants"],
    )
    with tempfile.TemporaryDirectory() as d:
        registry = Path(d) / "discovered.json"
        registry.write_text(json.dumps({"AI": {"subtopics": ["tech companies canceling ai"]}}))
        candidates = domain_seed_candidates(
            domain,
            max_seeds=6,
            mode="hybrid",
            subtopics_registry=registry,
            autocomplete_fn=fake_autocomplete,
            autocomplete_per_base=2,
        )
        expanded = domain_seed_candidates(
            domain,
            max_seeds=6,
            mode="expanded",
            subtopics_registry=registry,
            autocomplete_fn=fake_autocomplete,
            autocomplete_per_base=2,
        )
    topics = [c.topic for c in candidates]
    expanded_topics = [c.topic for c in expanded]
    sources = {c.topic: c.source for c in candidates}
    assert topics[:3] == [
        "tech companies canceling ai",
        "run ai agents locally",
        "ai tools for accountants",
    ]
    assert "tech companies canceling ai" not in expanded_topics
    assert sources["tech companies canceling ai"] == "discovered"
    assert sources["run ai agents locally"] == "domain_autocomplete"
    assert "curated:2" in source_summary(candidates)


def test_domain_probe_terms_expand_from_autocomplete_with_cap():
    from youtube_niche.candidates import domain_probe_terms

    def fake_autocomplete(q, region="US", lang="en"):
        return {
            "ai tools": ["run ai agents locally", "ai tools for accountants"],
            "ai automation": ["local business ai automation"],
        }.get(q, [])

    domain = Domain("AI", ["ai tools", "ai automation"], 6, 18)
    probes = domain_probe_terms(
        domain,
        max_terms=4,
        autocomplete_fn=fake_autocomplete,
        autocomplete_per_base=2,
    )
    assert probes == [
        "ai tools",
        "ai automation",
        "run ai agents locally",
        "ai tools for accountants",
    ]


def test_video_and_channel_evidence_rank_newcomer_breakouts():
    from youtube_niche.evidence import channel_evidence_rows, video_evidence_rows

    cfg = Config()
    cfg.volume_knee_vpd = 100
    videos = [
        {
            "video_id": "v1",
            "title": "Run AI agents locally on a mini PC",
            "channel_id": "c1",
            "channel_title": "Small Lab",
            "published_at": _iso_days_ago(10),
            "views": 50_000,
            "subs": 2_000,
            "duration_s": 600,
        },
        {
            "video_id": "v2",
            "title": "Unrelated AI news",
            "channel_id": "c2",
            "channel_title": "News",
            "published_at": _iso_days_ago(5),
            "views": 500_000,
            "subs": 1_000_000,
            "duration_s": 600,
        },
    ]
    video_rows = video_evidence_rows("run ai agents locally", videos, cfg, volume_knee=100)
    assert video_rows[0]["video_id"] == "v1"
    assert video_rows[0]["evidence_role"] == "newcomer_breakout"
    assert video_rows[0]["video_url"] == "https://www.youtube.com/watch?v=v1"

    channel_rows = channel_evidence_rows("run ai agents locally", video_rows)
    assert channel_rows[0]["channel_id"] == "c1"
    assert channel_rows[0]["newcomer_breakout_videos"] == 1
    assert channel_rows[0]["repeat_breakout_rate"] == 1.0
    assert channel_rows[0]["niche_specificity"] == 1.0
    assert channel_rows[0]["channel_trajectory_score"] > 0.85


def test_report_writes_video_and_channel_evidence_sidecars():
    from pathlib import Path

    row = {
        "topic": "run ai agents locally",
        "candidate_source": "domain_autocomplete",
        "opportunity": 0.7,
        "video_evidence": [{
            "topic": "run ai agents locally",
            "evidence_rank": 1,
            "evidence_role": "newcomer_breakout",
            "evidence_score": 0.9,
            "title": "Run AI agents locally on a mini PC",
            "video_id": "v1",
            "video_url": "https://www.youtube.com/watch?v=v1",
            "channel_title": "Small Lab",
            "channel_id": "c1",
            "channel_url": "https://www.youtube.com/channel/c1",
            "views": 50_000,
            "subs": 2_000,
            "views_per_day": 5000,
            "age_days": 10,
            "relevant": True,
            "small_channel": True,
            "views_per_sub": 25,
            "demand_score": 0.98,
            "beatability_score": 0.96,
            "newcomer_proof_score": 0.9,
        }],
        "channel_evidence": [{
            "topic": "run ai agents locally",
            "channel_rank": 1,
            "channel_title": "Small Lab",
            "channel_id": "c1",
            "channel_url": "https://www.youtube.com/channel/c1",
            "subscribers": 2_000,
            "sampled_videos": 1,
            "relevant_videos": 1,
            "newcomer_breakout_videos": 1,
            "total_views": 50_000,
            "max_views_per_day": 5000,
            "max_views_per_sub": 25,
            "channel_evidence_score": 0.9,
            "best_evidence_role": "newcomer_breakout",
            "best_video_title": "Run AI agents locally on a mini PC",
            "best_video_url": "https://www.youtube.com/watch?v=v1",
        }],
    }
    with tempfile.TemporaryDirectory() as d:
        csv_path, md_path = write_reports([row], d, "AI")
        video_path = Path(d) / f"{csv_path.stem}-video-evidence.csv"
        channel_path = Path(d) / f"{csv_path.stem}-channel-evidence.csv"
        assert video_path.exists()
        assert channel_path.exists()
        snapshot_path = Path(d) / "evidence-snapshots.csv"
        assert snapshot_path.exists()
        video_header = video_path.read_text().splitlines()[0]
        channel_header = channel_path.read_text().splitlines()[0]
        assert "category_evidence_rank" in video_header
        assert "opportunity_evidence_score" in video_header
        assert "category_channel_rank" in channel_header
        assert "opportunity_channel_score" in channel_header
        assert "domain_autocomplete" in csv_path.read_text()
        snapshot_text = snapshot_path.read_text()
        assert "evidence_type" in snapshot_text
        assert "pending" in snapshot_text
        md = md_path.read_text()
        assert "Top video proof" in md
        assert "Top channel proof" in md
        assert "Evidence snapshot registry" in md


def test_velocity_clock_split_de_inflates_backtest():
    """A video published just before the as-of date must not be inflated by dividing current
    cumulative views over a short past window. The de-leaked path measures velocity against the
    real wall-clock (a consistent lifetime-average), giving a strictly lower vpd than the old
    now=as_of behavior, while pinning velocity_now=as_of reproduces the old number exactly."""
    from youtube_niche.signals.volume import volume_score

    as_of = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    measured_now = as_of + dt.timedelta(days=170)  # we run the backtest 170 days later
    # Published 10 days before as_of with 100k current views.
    pub = (as_of - dt.timedelta(days=10)).isoformat().replace("+00:00", "Z")
    videos = [{"video_id": "v1", "title": "x", "views": 100_000, "subs": 5_000, "published_at": pub}]

    # Old (leaky) behavior: current views over the 10-day pre-as_of window => ~10k/day.
    _, leaky = volume_score(videos, now=as_of)
    # De-leaked: same current views over the true ~180-day lifetime => ~556/day.
    _, fixed = volume_score(videos, now=as_of, velocity_now=measured_now)

    assert leaky["median_vpd"] > 9_000          # the inflated number
    assert fixed["median_vpd"] < 600            # de-inflated lifetime-average
    assert fixed["median_vpd"] < leaky["median_vpd"]
    # Pinning velocity_now back to as_of reproduces the old behavior exactly (determinism hook).
    _, pinned = volume_score(videos, now=as_of, velocity_now=as_of)
    assert pinned["median_vpd"] == leaky["median_vpd"]
    # Live mode (no velocity_now) is unchanged: equals passing now alone.
    _, live = volume_score(videos, now=measured_now)
    _, live_default = volume_score(videos, now=measured_now, velocity_now=None)
    assert live["median_vpd"] == live_default["median_vpd"]


def test_subs_at_publish_est_prorates_by_channel_age():
    """subs-at-publish ≈ current_subs × (channel_age_at_post / channel_age_now); fallbacks safe."""
    from youtube_niche.winners import subs_at_publish_est

    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    def iso(days):
        return (now - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")

    # Channel created 100d ago, now 100k subs. Video posted 10d ago => channel was 90d old => ~90k.
    v_estab = {"subs": 100_000, "channel_published_at": iso(100), "published_at": iso(10)}
    assert abs(subs_at_publish_est(v_estab, now) - 90_000) < 2_000
    # Same channel, video posted 95d ago => channel was only 5d old at post => ~5k (newcomer breakout).
    v_newcomer = {"subs": 100_000, "channel_published_at": iso(100), "published_at": iso(95)}
    assert subs_at_publish_est(v_newcomer, now) < 6_000
    # Missing channel creation date => fall back to current subs (no spurious relaxation).
    assert subs_at_publish_est({"subs": 42_000, "published_at": iso(10)}, now) == 42_000
    # Unknown current subs => None (cannot estimate).
    assert subs_at_publish_est({"subs": None}, now) is None


def test_scoring_counts_grew_past_cap_channel_as_newcomer_at_publish():
    """Current-large channels still prove newcomer demand when tiny at publish."""
    from youtube_niche.evidence import video_evidence_rows
    from youtube_niche.signals.volume import volume_score

    cfg = Config()
    cfg.volume_knee_vpd = 100
    now = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    def iso(days):
        return (now - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")

    video = {
        "video_id": "v1",
        "title": "Rent vs buy a house in 2026",
        "channel_id": "c1",
        "channel_title": "Fast Grower",
        "published_at": iso(30),
        "channel_published_at": iso(35),
        "views": 300_000,
        "subs": 200_000,  # current subs now exceed the 50k cap
        "duration_s": 600,
    }

    _, volume_detail = volume_score([video], knee=cfg.volume_knee_vpd, now=now)
    assert volume_detail["newcomer_sample"] == 1
    assert volume_detail["newcomer_vpd"] == 10_000

    supply, supply_detail = supply_scores(
        [video],
        total_results=1,
        topic="rent vs buy a house",
        min_small_channel_vpd=cfg.min_small_channel_vpd,
        now=now,
    )
    assert supply["small_channel_gap"] == 1.0
    assert supply_detail["small_channel_frac"] == 1.0

    outlier, outlier_detail = outlier_score([video], knee=1.0, now=now)
    assert outlier > 0.90
    assert outlier_detail["max_ratio"] == 10.5

    rows = video_evidence_rows("rent vs buy a house", [video], cfg, volume_knee=100, now=now)
    assert rows[0]["evidence_role"] == "newcomer_breakout"
    assert rows[0]["small_channel"] is True
    assert rows[0]["small_channel_current"] is False
    assert rows[0]["subs_at_publish_est"] == 28_571


def test_find_breakouts_includes_channel_that_grew_past_cap():
    """A channel now over the cap but SMALL when it posted the breakout is kept; an always-big
    channel is still dropped. This is the audit's 'current-subs filter inverts the signal' fix."""
    from youtube_niche.config import Config
    from youtube_niche.winners import find_breakouts

    # vid: (title, views, video_age_days, channel_id, current_subs, channel_age_days)
    rows = {
        "g0": ("Rent vs buy a house in 2026", 300_000, 30, "c0", 200_000, 35),    # blew up from ~0 -> KEEP
        "g1": ("Index fund basics explained", 300_000, 30, "c1", 200_000, 1000),  # always big -> DROP
        "g2": ("My first budget video changed everything", 300_000, 30, "c2", 200_000, 30),  # brand-new channel (est~0) -> KEEP
    }

    class C:
        def search(self, q, max_results=30, **k):
            return {"items": [{"id": {"videoId": v}} for v in rows]}

        def videos(self, ids):
            return {
                v: {"id": v, "snippet": {"title": tt, "channelId": ch, "channelTitle": "x",
                    "publishedAt": _iso_days_ago(vage), "defaultAudioLanguage": "en"},
                    "contentDetails": {"duration": "PT10M"}, "statistics": {"viewCount": str(views)}}
                for v, (tt, views, vage, ch, subs, chage) in rows.items() if v in ids
            }

        def channels(self, ids):
            info = {r[3]: (r[4], r[5]) for r in rows.values()}
            return {
                c: {"id": c, "statistics": {"subscriberCount": str(info[c][0])},
                    "snippet": {"publishedAt": _iso_days_ago(info[c][1])}}
                for c in ids
            }

        def search_calls_remaining(self):
            return 10

    out = find_breakouts(C(), Config(), ["house"], recent_days=180, min_vpd=100, max_per_term=8)
    titles = {v["title"] for v in out}
    assert titles == {"Rent vs buy a house in 2026", "My first budget video changed everything"}


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
