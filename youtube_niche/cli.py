"""Entrypoint: `python -m youtube_niche "<niche>"`.

Pipeline per topic:
  search.list -> enrich (videos.list + channels.list) -> signals A–G -> opportunity score
Then rank everything and write CSV + Markdown.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from statistics import mean, median

import os

from .auth import ApiKeyAuth, OAuthAuth
from .cache import Cache
from .config import Config
from .demo import demo_rows
from .enrich import enrich
from .external import match_external_metric
from .forward import capture_score_snapshot, parse_horizons
from .llm import make_llm
from .monetization import monetization_score
from .report import write_reports
from .score import confidence_score, opportunity_score
from .seeds import expand_seeds
from .signals.comments import comment_demand_score
from .signals.outlier import outlier_score
from .signals.quality import quality_gap_score
from .signals.supply import filter_relevant_videos, supply_scores
from .signals.trends import trends_score
from .signals.volume import volume_score
from .topics import dedupe_ranked_rows, dedupe_topics
from .util import clamp01
from .youtube_client import QuotaExceeded, YouTubeClient


def query_variants(topic: str, max_samples: int = 1) -> list[str]:
    """Search variants for the same topic. More samples reduce one fuzzy-result-page risk."""
    base = topic.strip()
    if not base:
        return []
    candidates = [
        base,
        f'"{base}"',
        f"{base} explained",
        f"{base} tutorial",
        f"{base} guide",
    ]
    if not base.lower().startswith("how to "):
        candidates.append(f"how to {base}")

    out: list[str] = []
    seen: set[str] = set()
    for q in candidates:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
        if len(out) >= max(1, max_samples):
            break
    return out


def _median_present(values: list[float | int | None]) -> float | None:
    present = [float(v) for v in values if v is not None]
    return median(present) if present else None


def _mean_present(values: list[float | int | None]) -> float | None:
    present = [float(v) for v in values if v is not None]
    return mean(present) if present else None


def _agreement(values: list[float | int | None]) -> float | None:
    present = [clamp01(float(v)) for v in values if v is not None]
    if not present:
        return None
    if len(present) == 1:
        return 1.0
    return clamp01(1.0 - (max(present) - min(present)))


def collect_topic_samples(
    topic: str,
    client: YouTubeClient,
    cfg: Config,
    published_before: str | None = None,
) -> tuple[list[dict], list[dict], int, list[str]]:
    """Run one or more searches, returning per-query samples plus a deduped video pool."""
    videos_by_id: dict[str, dict] = {}
    totals: list[int] = []
    samples: list[dict] = []
    queries = query_variants(topic, cfg.query_samples)
    for q in queries:
        search = client.search(
            q,
            max_results=cfg.top_n,
            published_before=published_before,
            region=cfg.region_code,
            relevance_language=cfg.relevance_language,
        )
        items = search.get("items", [])
        total = search.get("pageInfo", {}).get("totalResults", len(items))
        totals.append(total)
        records = enrich(client, items, cfg)
        samples.append({"query": q, "videos": records, "total": total})
        for v in records:
            videos_by_id.setdefault(v["video_id"], v)
    total = max(totals) if totals else 0
    return samples, list(videos_by_id.values()), total, queries


def collect_topic_sample(
    topic: str,
    client: YouTubeClient,
    cfg: Config,
    published_before: str | None = None,
) -> tuple[list[dict], int, list[str]]:
    """Backward-compatible merged sample wrapper."""
    _, videos, total, queries = collect_topic_samples(
        topic, client, cfg, published_before=published_before
    )
    return videos, total, queries


def _sample_signal_summary(
    topic: str,
    videos: list[dict],
    total: int,
    cfg: Config,
    domain=None,
    as_of: dt.datetime | None = None,
) -> dict:
    relevant_videos = filter_relevant_videos(videos, topic)

    knee = cfg.volume_knee_vpd
    if domain is not None and getattr(domain, "volume_knee_vpd", None):
        knee = domain.volume_knee_vpd
    vol_score, vol_detail = volume_score(
        relevant_videos,
        knee=knee,
        min_views=cfg.min_view_floor,
        recent_days=cfg.recent_days,
        recent_success_knee=cfg.recent_success_knee,
        small_channel_subs=cfg.small_channel_subs,
        now=as_of,
    )
    o_score, o_detail = outlier_score(
        relevant_videos, knee=cfg.outlier_knee, min_views=cfg.min_view_floor
    )
    s_scores, s_detail = supply_scores(
        videos,
        total,
        small_channel_subs=cfg.small_channel_subs,
        competition_knee=cfg.competition_knee,
        age_knee_days=cfg.age_knee_days,
        min_views=cfg.min_view_floor,
        topic=topic,
        recent_days=cfg.recent_days,
        recent_supply_knee=cfg.recent_supply_knee,
        min_small_channel_vpd=cfg.min_small_channel_vpd,
        now=as_of,
    )
    relevance_gate = clamp01((s_detail.get("credible_results") or 0) / max(cfg.min_relevant_results, 1))
    return {
        "relevant_videos": relevant_videos,
        "sub": {
            "volume": vol_score,
            "newcomer_volume": vol_detail.get("newcomer_volume"),
            "p75_volume": vol_detail.get("p75_volume"),
            "recent_demand": vol_detail.get("recent_demand"),
            "demand_gate": clamp01((vol_detail.get("demand_gate") or 0.0) * relevance_gate),
            "outlier": o_score,
            "competition_gap": s_scores["competition_gap"],
            "authority_gap": s_scores.get("authority_gap"),
            "recent_supply_gap": s_scores["recent_supply_gap"],
            "age_gap": s_scores["age_gap"],
            "small_channel_gap": s_scores["small_channel_gap"],
        },
        "vol_detail": vol_detail,
        "outlier_detail": o_detail,
        "supply_detail": s_detail,
        "relevance_gate": relevance_gate,
    }


def analyze_topic(
    topic: str,
    client: YouTubeClient,
    llm,
    cfg: Config,
    domain=None,
    published_before: str | None = None,
    as_of: dt.datetime | None = None,
) -> dict | None:
    samples, videos, total, queries = collect_topic_samples(topic, client, cfg, published_before=published_before)
    if not videos:
        return None

    sample_summaries = [
        _sample_signal_summary(topic, s["videos"], s["total"], cfg, domain=domain, as_of=as_of)
        for s in samples
        if s.get("videos")
    ]
    if not sample_summaries:
        return None
    usable_summaries = [
        s for s in sample_summaries
        if (s["supply_detail"].get("credible_results") or 0) > 0
    ] or sample_summaries
    query_coverage = len(usable_summaries) / max(len(sample_summaries), 1)
    query_consensus = _mean_present([
        _agreement([s["sub"].get("demand_gate") for s in usable_summaries]),
        _agreement([s["sub"].get("competition_gap") for s in usable_summaries]),
        _agreement([s.get("relevance_gate") for s in usable_summaries]),
    ])
    if query_consensus is None:
        query_consensus = 1.0

    def med_sub(key: str) -> float | None:
        return _median_present([s["sub"].get(key) for s in usable_summaries])

    def med_detail(section: str, key: str) -> float | None:
        return _median_present([s[section].get(key) for s in usable_summaries])

    relevant_videos = filter_relevant_videos(videos, topic)
    # E — comment demand
    comment_texts: list[str] = []
    for v in relevant_videos[: cfg.comment_videos]:
        for t in client.comment_threads(v["video_id"], pages=cfg.comment_pages):
            try:
                comment_texts.append(
                    t["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
                )
            except Exception:
                pass
    c_score, c_detail = comment_demand_score(
        comment_texts, llm=llm if cfg.use_llm else None
    )
    # F — trends (cached on disk; throttled live calls)
    if cfg.use_trends:
        baseline_terms = getattr(domain, "terms", [])[:4] if domain is not None else None
        t_score, t_detail = trends_score(
            topic, geo=cfg.trends_geo, cache=client.cache, baseline_terms=baseline_terms
        )
    else:
        t_score, t_detail = None, {"status": "disabled"}
    # G — quality gap
    if cfg.use_llm:
        q_gap, q_detail = quality_gap_score(
            relevant_videos, topic, llm, max_videos=cfg.quality_videos
        )
    else:
        q_gap, q_detail = None, {"status": "disabled", "videos": []}

    cpm_score, cpm_detail = monetization_score(topic, domain, full_scale=cfg.cpm_full_scale)
    external_metric = match_external_metric(topic, cfg.keyword_metrics_csv)
    external_demand = external_metric.demand_score if external_metric else None
    external_cpm_score = external_metric.cpm_score if external_metric else None
    if external_cpm_score is not None:
        cpm_score = _mean_present([cpm_score, external_cpm_score])
        cpm_detail["cpm_source"] = (
            f"{cpm_detail.get('cpm_source', 'unknown')};external:{external_metric.source}"
        )
    quality_videos = q_detail.get("videos") or []
    quality_scored = sum(1 for v in quality_videos if v.get("depth") is not None)
    quality_attempted = len(quality_videos)
    comments_expected = max(cfg.comment_videos * cfg.comment_pages * 25, 1)
    confidence = confidence_score([
        (query_coverage, 0.10),
        (query_consensus, 0.10),
        (med_detail("supply_detail", "title_match_frac") or 0.0, 0.12),
        ((med_detail("supply_detail", "known_subscriber_results") or 0) / max(med_detail("supply_detail", "credible_results") or 1, 1), 0.13),
        (1.0 if t_score is not None else 0.0, 0.15),
        (min((c_detail.get("n_comments") or 0) / comments_expected, 1.0), 0.15),
        (quality_scored / max(cfg.quality_videos, 1), 0.20),
        (1.0 if cpm_detail.get("cpm_mid") is not None else 0.65, 0.20),
    ])

    sub = {
        "volume": med_sub("volume"),
        "newcomer_volume": med_sub("newcomer_volume"),
        "p75_volume": med_sub("p75_volume"),
        "recent_demand": med_sub("recent_demand"),
        "demand_gate": med_sub("demand_gate"),
        "outlier": med_sub("outlier"),
        "trends": t_score,
        "comment_demand": c_score,
        "external_demand": external_demand,
        "cpm_score": cpm_score,
        "competition_gap": med_sub("competition_gap"),
        "authority_gap": med_sub("authority_gap"),
        "recent_supply_gap": med_sub("recent_supply_gap"),
        "age_gap": med_sub("age_gap"),
        "small_channel_gap": med_sub("small_channel_gap"),
        "quality_gap": q_gap,
        "confidence": confidence,
    }
    scored = opportunity_score(sub, cfg.weights)

    return {
        "topic": topic,
        **scored,
        **sub,
        "query_samples": len(queries),
        "search_queries": "; ".join(queries),
        "query_coverage": query_coverage,
        "query_consensus": query_consensus,
        "relevance_gate": _median_present([s.get("relevance_gate") for s in usable_summaries]),
        "median_vpd": med_detail("vol_detail", "median_vpd"),
        "p75_vpd": med_detail("vol_detail", "p75_vpd"),
        "median_views": med_detail("vol_detail", "median_views"),
        "newcomer_vpd": med_detail("vol_detail", "newcomer_vpd"),
        "newcomer_volume": med_detail("vol_detail", "newcomer_volume"),
        "newcomer_sample": med_detail("vol_detail", "newcomer_sample"),
        "recent_success_count": med_detail("vol_detail", "recent_success_count"),
        "max_outlier_ratio": med_detail("outlier_detail", "max_ratio"),
        "outlier_unknown_subs": med_detail("outlier_detail", "unknown_subs"),
        "credible_results": med_detail("supply_detail", "credible_results"),
        "raw_credible_results": med_detail("supply_detail", "raw_credible_results"),
        "sampled_results": med_detail("supply_detail", "sampled_results"),
        "credible_density": med_detail("supply_detail", "credible_density"),
        "title_match_frac": med_detail("supply_detail", "title_match_frac"),
        "recent_credible_results": med_detail("supply_detail", "recent_credible_results"),
        "median_age_days": med_detail("supply_detail", "median_age_days"),
        "known_subscriber_results": med_detail("supply_detail", "known_subscriber_results"),
        "unknown_subscriber_results": med_detail("supply_detail", "unknown_subscriber_results"),
        "small_channel_frac": med_detail("supply_detail", "small_channel_frac"),
        "unique_credible_channels": med_detail("supply_detail", "unique_credible_channels"),
        "top_channel_share": med_detail("supply_detail", "top_channel_share"),
        "top3_channel_share": med_detail("supply_detail", "top3_channel_share"),
        "n_comment_requests": c_detail.get("n_requests"),
        "n_comments": c_detail.get("n_comments"),
        "comment_examples": c_detail.get("examples"),
        "trends_status": (t_detail or {}).get("status"),
        "trend_slope_score": (t_detail or {}).get("slope_score"),
        "trend_level_score": (t_detail or {}).get("level_score"),
        "trend_breakout_score": (t_detail or {}).get("breakout_score"),
        "trend_rising_score": (t_detail or {}).get("rising_queries"),
        "trend_rising_terms": "; ".join((t_detail or {}).get("rising_terms") or []),
        "external_metric_topic": external_metric.topic if external_metric else None,
        "external_demand": external_demand,
        "external_cpm_score": external_cpm_score,
        "external_cpm": external_metric.cpm if external_metric else None,
        "external_monthly_searches": external_metric.monthly_searches if external_metric else None,
        "cpm_source": cpm_detail.get("cpm_source"),
        "cpm_mid": cpm_detail.get("cpm_mid"),
        "ad_intent": cpm_detail.get("ad_intent"),
        "quality_status": q_detail.get("status"),
        "quality_attempted": quality_attempted,
        "quality_scored": quality_scored,
        "avg_depth": q_detail.get("avg_depth"),
    }


def _select_auth(cfg: Config, allow_missing: bool = False):
    """Pick YouTube auth: API key if set, else OAuth files if present. None -> error printed."""
    if cfg.youtube_api_key:
        print("Auth: YouTube API key")
        return ApiKeyAuth(cfg.youtube_api_key)

    cs, tok = cfg.youtube_oauth_client_secret, cfg.youtube_oauth_token
    if cs and tok and os.path.exists(cs) and os.path.exists(tok):
        try:
            auth = OAuthAuth(cs, tok)
        except Exception as e:
            print(f"ERROR: failed to load OAuth credentials: {e}", file=sys.stderr)
            return None
        print(f"Auth: OAuth (token scope: {auth.scope or 'unknown'})")
        return auth

    missing = cs if cs and not os.path.exists(cs) else tok
    if allow_missing:
        print("Auth: none (cache-only mode)")
        return None
    print(
        "ERROR: no YouTube auth available.\n"
        "  - Set YOUTUBE_API_KEY (env or .env), OR\n"
        f"  - Provide OAuth files. Expected but not found: {missing or '(paths unset)'}\n"
        "    (Check the paths, or override with YOUTUBE_OAUTH_CLIENT_SECRET / "
        "YOUTUBE_OAUTH_TOKEN.)",
        file=sys.stderr,
    )
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche",
        description="Find high-demand, high-monetization, low-supply YouTube topics.",
    )
    p.add_argument("niche", nargs="?", default=None, help='Seed niche, e.g. "off-grid solar for vans"')
    p.add_argument(
        "--from-domain",
        default=None,
        help='Stage 2: seed niches from a domain\'s curated sub-topics, e.g. "personal finance"',
    )
    p.add_argument("--seeds", default=None, help="comma-separated EXACT topics to score (skips autocomplete)")
    p.add_argument("--max-seeds", type=int, default=None, help="cap candidate topics (default 20)")
    p.add_argument("--top-n", type=int, default=None, help="search results scanned per seed (default 30)")
    p.add_argument(
        "--query-samples",
        type=int,
        default=None,
        help="search-query variants per topic (default 1; try 3 to reduce fuzzy-search noise)",
    )
    p.add_argument("--alphabet-soup", action="store_true", help="aggressive autocomplete expansion")
    p.add_argument("--no-trends", action="store_true", help="skip Google Trends signal")
    p.add_argument("--no-llm", action="store_true", help="skip comment + quality LLM signals")
    p.add_argument(
        "--llm-provider",
        choices=["auto", "anthropic", "codex", "claude", "agy"],
        default=None,
        help="LLM backend for signals E & G (default: auto = anthropic key if set, else codex)",
    )
    p.add_argument("--out-dir", default=None, help="output directory (default ./out)")
    p.add_argument("--quota-budget", type=int, default=None, help="override daily unit budget")
    p.add_argument("--search-limit", type=int, default=None, help="override daily search.list call budget")
    p.add_argument("--region-code", default=None, help="YouTube/autocomplete region code (default US)")
    p.add_argument("--relevance-language", default=None, help="YouTube relevance language (default en)")
    p.add_argument("--trends-geo", default=None, help="Google Trends geo (default US)")
    p.add_argument("--metrics-csv", default=None, help="optional external keyword metrics CSV")
    p.add_argument("--cache-only", action="store_true", help="only use cached YouTube responses; never call the API")
    p.add_argument("--snapshot", action="store_true", help="append scored topics to the forward-test snapshot registry")
    p.add_argument("--snapshot-horizons", default="30,60,90", help="comma-separated forward-test horizons in days")
    p.add_argument("--demo", action="store_true", help="write a synthetic offline demo report and exit")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env(
        max_seeds=args.max_seeds,
        top_n=args.top_n,
        query_samples=args.query_samples,
        out_dir=args.out_dir,
        daily_quota_units=args.quota_budget,
        daily_search_limit=args.search_limit,
        alphabet_soup=args.alphabet_soup or None,
        llm_provider=args.llm_provider,
        region_code=args.region_code,
        relevance_language=args.relevance_language,
        trends_geo=args.trends_geo,
        keyword_metrics_csv=args.metrics_csv,
        cache_only=args.cache_only or None,
    )
    if args.no_trends:
        cfg.use_trends = False
    if args.no_llm:
        cfg.use_llm = False

    if args.demo:
        rows = demo_rows()
        csv_path, md_path = write_reports(rows, cfg.out_dir, "demo")
        print(f"Wrote offline demo:\n  {csv_path}\n  {md_path}")
        return 0

    auth = _select_auth(cfg, allow_missing=cfg.cache_only)
    if auth is None and not cfg.cache_only:
        return 2

    cache = Cache(cfg.cache_path)
    client = YouTubeClient(
        auth,
        cache,
        daily_quota=cfg.daily_quota_units,
        reserve=cfg.quota_reserve,
        daily_search_limit=cfg.daily_search_limit,
        cache_only=cfg.cache_only,
    )
    llm = make_llm(cfg) if cfg.use_llm else None
    if cfg.use_llm and (llm is None or not llm.enabled):
        print(
            f"NOTE: no working LLM backend (provider={cfg.llm_provider!r}) — "
            "comment + quality signals will be skipped and confidence will be lower.",
            file=sys.stderr,
        )
    elif cfg.use_llm:
        print(f"LLM: {llm.backend.name}")

    if args.seeds:
        label = "shortlist"
        domain = None
        seeds = dedupe_topics(s.strip() for s in args.seeds.split(",") if s.strip())[: cfg.max_seeds]
        print(f"Scoring {len(seeds)} explicit topics: {seeds}")
    elif args.from_domain:
        from .domains import DOMAINS

        match = [d for d in DOMAINS if args.from_domain.lower() in d.name.lower()]
        if not match or not match[0].subtopics:
            print(
                f"No domain with sub-topics matched {args.from_domain!r}. "
                f"Known: {[d.name for d in DOMAINS if d.subtopics]}",
                file=sys.stderr,
            )
            return 1
        domain = match[0]
        label = domain.name
        seeds = dedupe_topics(domain.subtopics)[: cfg.max_seeds]
        print(f"Stage-2 drill-down into: {label} — {len(seeds)} sub-niches")
    else:
        if not args.niche:
            print("ERROR: provide a niche, or --from-domain <name>.", file=sys.stderr)
            return 2
        label = args.niche
        domain = None
        print(f"Expanding seeds from: {args.niche!r}")
        seeds = dedupe_topics(expand_seeds(
            args.niche,
            max_seeds=cfg.max_seeds,
            alphabet_soup=cfg.alphabet_soup,
            region=cfg.region_code,
            lang=cfg.relevance_language,
        ))[: cfg.max_seeds]
        print(f"  {len(seeds)} candidate topics: {seeds}")

    per_topic = cfg.per_topic_unit_estimate()
    per_topic_search = cfg.per_topic_search_estimate()
    results: list[dict] = []
    for i, topic in enumerate(seeds, 1):
        if client.units_remaining() < per_topic or client.search_calls_remaining() < per_topic_search:
            print(
                f"  Stopping early — quota budget nearly exhausted "
                f"({client.units_remaining()} units and {client.search_calls_remaining()} searches left, "
                f"~{per_topic} units + {per_topic_search} search/topic)."
            )
            break
        print(
            f"[{i}/{len(seeds)}] {topic}  "
            f"(quota left ~{client.units_remaining()} units, {client.search_calls_remaining()} searches)"
        )
        try:
            row = analyze_topic(topic, client, llm, cfg, domain=domain)
        except QuotaExceeded as e:
            print(f"  quota stop: {e}")
            break
        except Exception as e:
            print(f"  skipped ({type(e).__name__}: {e})")
            continue
        if row:
            results.append(row)

    if not results:
        print("No results produced.")
        return 1

    results.sort(key=lambda r: (r.get("opportunity") or 0.0), reverse=True)
    results = dedupe_ranked_rows(results)
    csv_path, md_path = write_reports(results, cfg.out_dir, label)

    print(f"\nWrote:\n  {csv_path}\n  {md_path}")
    if args.snapshot:
        snap_path, snap_rows = capture_score_snapshot(
            results,
            cfg.out_dir,
            label,
            source="youtube_niche",
            horizons=parse_horizons(args.snapshot_horizons),
        )
        print(f"Forward snapshot: {snap_rows} rows -> {snap_path}")
    print(
        f"Quota used today: {client.units_spent()} / {cfg.daily_quota_units} units; "
        f"{client.search_calls_used()} / {cfg.daily_search_limit} searches"
    )
    print("\nTop opportunities:")
    for i, r in enumerate(results[:5], 1):
        print(f"  {i}. {r['topic']} — {round((r.get('opportunity') or 0) * 100)}%")
    cache.close()
    return 0
