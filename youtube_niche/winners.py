"""Winners-first niche discovery — find niches PROVEN to have capturable demand.

Instead of grading a hand-curated niche list (which assumes you already know the niches),
this mines a high-CPM domain for BREAKOUT videos — small channels pulling big recent views,
i.e. living proof a topic is both in demand AND winnable from zero — reads the niche topics
off those winners, and scores them with the niche engine. The breakouts are the ground truth.

Run: python -m youtube_niche.winners --domain "personal finance" [--min-vpd 100 --recent-days 180]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections import Counter

from .cache import Cache
from .candidates import domain_probe_terms
from .channel_size import is_small_channel_at_publish, publish_time_sub_denominator, subs_at_publish_est
from .cli import _select_auth, analyze_topic
from .config import Config
from .domains import DOMAINS
from .enrich import enrich
from .forward import capture_score_snapshot, parse_horizons
from .formats import classify_format, positioning
from .llm import LLM_PROVIDERS, make_llm
from .relevance import relevance_score
from .report import write_reports
from .signals.trends import durability_label
from .signals.volume import views_per_day
from .topics import dedupe_ranked_rows, dedupe_topics
from .youtube_client import QuotaExceeded, YouTubeClient

_STOP = {
    "the", "a", "an", "to", "of", "for", "in", "on", "how", "why", "what", "best", "top",
    "your", "you", "is", "are", "and", "with", "my", "this", "that", "explained", "tutorial",
    "guide", "review", "vs", "2024", "2025", "2026", "ultimate", "complete", "beginners",
}

# Entertainment / non-niche title markers — a finance keyword in a movie trailer is not a niche.
_JUNK_TITLE = (
    "trailer", "teaser", "music video", "official video", "lyric", "full movie", "full episode",
    "(audio)", "live stream", "livestream", "podcast #", "ft.", "feat.",
)

# YouTube category ids that are OFF-DOMAIN for every domain this tool mines (all are
# money/info/educational: finance, business, AI, health, etc. — never gaming/music/sports).
# A "make money" video tagged Gaming is about in-game currency, not a real niche
# (e.g. "This Bank Trick makes you TON of Money in Crimson Desert"). Filter by YouTube's
# own category rather than chasing an unbounded list of game/song/team names.
#   1 Film & Animation · 2 Autos & Vehicles · 10 Music · 17 Sports · 20 Gaming
_OFFDOMAIN_CATEGORIES = {"1", "2", "10", "17", "20"}


def _is_offdomain(v: dict) -> bool:
    cat = v.get("category_id")
    return cat is not None and str(cat) in _OFFDOMAIN_CATEGORIES


def _is_short(v: dict) -> bool:
    dur = v.get("duration_s")
    return (dur is not None and dur < 60) or "#short" in v.get("title", "").lower()


# Non-Latin script blocks: Cyrillic, Hebrew, Arabic, Devanagari, Bengali, Thai, Kana, CJK, Hangul.
# A real English title contains none of these; even one block (even mixed with English) flags it.
_NONLATIN_RE = re.compile(
    r"[Ѐ-ӿ֐-׿؀-ۿऀ-ॿঀ-৿"
    r"฀-๿぀-ヿ㐀-鿿가-힯]"
)


def _is_english(v: dict) -> bool:
    lang = (v.get("lang") or "").lower()
    if lang and not lang.startswith("en"):
        return False
    return _NONLATIN_RE.search(v.get("title", "")) is None


def _is_junk(v: dict) -> bool:
    t = v.get("title", "").lower()
    return any(j in t for j in _JUNK_TITLE)


def find_breakouts(client: YouTubeClient, cfg: Config, terms: list[str],
                   recent_days: int, min_vpd: float, max_per_term: int) -> list[dict]:
    """Breakout = SMALL channel + recent + high view velocity. Proof of capturable demand."""
    now = dt.datetime.now(dt.timezone.utc)
    published_after = (now - dt.timedelta(days=recent_days)).isoformat().replace("+00:00", "Z")
    breakouts: list[dict] = []
    seen: set[str] = set()
    for t in terms:
        if client.search_calls_remaining() < 1:
            break
        try:
            res = client.search(
                t, max_results=cfg.top_n, order="viewCount", published_after=published_after,
                region=cfg.region_code, relevance_language=cfg.relevance_language,
            )
        except QuotaExceeded:
            break
        except Exception:
            continue
        scored = []
        try:
            records = enrich(client, res.get("items", []), cfg)
        except Exception:
            continue
        for v in records:
            subs = v.get("subs")
            if v["views"] < cfg.min_view_floor or subs is None or subs <= 0:
                continue
            # Small-at-publish, not small-now: keep channels that broke out and then grew past the cap.
            if not is_small_channel_at_publish(v, cfg.small_channel_subs, now):
                continue
            if _is_short(v) or _is_junk(v) or _is_offdomain(v) or not _is_english(v):
                continue  # Shorts, trailers/podcasts, off-domain (gaming/music/...), non-English
            vpd = views_per_day(v, now)
            if vpd is None or vpd < min_vpd:
                continue
            v["_vpd"] = vpd
            v["_subs_at_publish_est"] = subs_at_publish_est(v, now)
            v["_ratio"] = v["views"] / (publish_time_sub_denominator(v, now) or subs)
            scored.append(v)
        scored.sort(key=lambda v: v["_vpd"], reverse=True)
        for v in scored[:max_per_term]:
            if v["video_id"] not in seen:
                seen.add(v["video_id"])
                breakouts.append(v)

    # Keep only the strongest breakout per channel — one spammy channel shouldn't dominate.
    best_by_channel: dict[str, dict] = {}
    for v in breakouts:
        cid = v["channel_id"]
        if cid not in best_by_channel or v["_vpd"] > best_by_channel[cid]["_vpd"]:
            best_by_channel[cid] = v
    return list(best_by_channel.values())


def _keyword_niches(titles: list[str], max_niches: int) -> list[str]:
    """Fallback when no LLM: frequent 2-3 word phrases across breakout titles."""
    grams: Counter = Counter()
    for t in titles:
        toks = [w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in _STOP and len(w) > 2]
        for n in (2, 3):
            for i in range(len(toks) - n + 1):
                grams[" ".join(toks[i:i + n])] += 1
    return [g for g, c in grams.most_common(max_niches) if c >= 2]


def discover_niches(breakouts: list[dict], llm, max_niches: int) -> tuple[list[str], str]:
    """Returns (niches, method). method is one of:
      "llm"              — LLM extraction succeeded.
      "keyword"          — LLM was disabled on purpose (--no-llm / no cfg.use_llm); expected, quiet.
      "keyword_degraded" — LLM was enabled but every provider returned empty/failed; the keyword
                            fallback ran anyway, but the labels are thin and the caller must not
                            let this run pollute the forward-test snapshot ledger.
    """
    titles = [v["title"] for v in breakouts]
    llm_was_enabled = llm is not None and getattr(llm, "enabled", False)
    if llm_was_enabled:
        topics = llm.extract_niches(titles, max_niches=max_niches)
        if topics:
            return topics, "llm"
        print("⚠️  LLM niche extraction FAILED (all providers empty) — falling back to keyword n-grams.")
        print("⚠️  Labels will be thin/generic; the forward snapshot for this run will be SKIPPED.")
        return _keyword_niches(titles, max_niches), "keyword_degraded"
    return _keyword_niches(titles, max_niches), "keyword"


def matching_breakouts(topic: str, breakouts: list[dict]) -> list[dict]:
    """Breakout videos whose title targets `topic` — same topic-to-title matcher analyze_topic
    itself uses (signals.supply.filter_relevant_videos -> relevance.relevance_score), so "this
    breakout belongs to this niche" means the same thing here as it does everywhere else the
    pipeline decides a video is on-topic. Not backtest.text_matches_topic: that module imports
    discover_niches FROM this one, so importing it back here would create a circular import.
    """
    return [v for v in breakouts if relevance_score(topic, v.get("title", "")).relevant]


def replication_and_format(topic: str, breakouts: list[dict]) -> tuple[int, str]:
    """Per-niche replicability signal: how many DISTINCT small channels independently broke out
    with this topic, and what title format dominates among them.

    Industry logic: the same theme breaking out across several distinct small channels is the
    strongest replicability signal available here — it means the demand isn't tied to one
    creator's audience/personality, it's a searchable pattern anyone can walk into. One channel
    posting 5 videos on the same topic is a channel story (or a spammy upload pattern), not
    evidence the niche itself is repeatable — so replication counts DISTINCT channel_ids, not
    matching videos.

    Returns (replication_channels, dominant_format). dominant_format is "" when there are no
    matching breakouts (nothing to classify).
    """
    matches = matching_breakouts(topic, breakouts)
    replication_channels = len({v.get("channel_id") for v in matches if v.get("channel_id")})
    if not matches:
        return replication_channels, ""
    format_counts = Counter(classify_format(v.get("title", "")) for v in matches)
    dominant_format = format_counts.most_common(1)[0][0]
    return replication_channels, dominant_format


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.winners",
        description="Discover niches from breakout small channels in a domain.",
    )
    p.add_argument("--domain", required=True, help='high-CPM domain to mine, e.g. "personal finance"')
    p.add_argument("--min-vpd", type=float, default=None, help="min views/day for a breakout (default 100)")
    p.add_argument("--recent-days", type=int, default=None, help="how recent a breakout must be (default 180)")
    p.add_argument("--max-niches", type=int, default=15, help="how many discovered niches to score")
    p.add_argument("--max-probe-terms", type=int, default=15,
                   help="domain search probes for breakout mining after autocomplete expansion")
    p.add_argument("--no-probe-autocomplete", action="store_true",
                   help="mine winners only from the domain's hand-written probe terms")
    p.add_argument("--emit-subtopics", action="store_true",
                   help="write discovered niches to the subtopics registry (seeds --from-domain) and skip scoring")
    p.add_argument("--emit-out", default=None,
                   help="registry path (default: user config overlay; packaged seeds are read-only fallback)")
    p.add_argument("--llm-provider", choices=LLM_PROVIDERS, default=None)
    p.add_argument("--no-llm", action="store_true", help="skip LLM (keyword niche extraction + no depth)")
    p.add_argument("--no-trends", action="store_true", help="skip the 12-month Trends momentum signal")
    p.add_argument("--no-durability", action="store_true",
                   help="skip the 5-year Trends durability check (runs even under --no-trends)")
    p.add_argument("--top-n", type=int, default=None)
    p.add_argument("--query-samples", type=int, default=None,
                   help="search-query variants per discovered niche (default 1; try 3)")
    p.add_argument("--cache-only", action="store_true", help="only use cached YouTube responses; never call the API")
    p.add_argument("--snapshot", action="store_true", help="append scored niches to the forward-test snapshot registry")
    p.add_argument("--snapshot-horizons", default="30,60,90", help="comma-separated forward-test horizons in days")
    p.add_argument("--metrics-csv", default=None, help="optional external keyword metrics CSV")
    p.add_argument("--region-code", default=None)
    p.add_argument("--relevance-language", default=None)
    p.add_argument("--out-dir", default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env(top_n=args.top_n, out_dir=args.out_dir, llm_provider=args.llm_provider,
                          region_code=args.region_code, relevance_language=args.relevance_language,
                          winner_min_vpd=args.min_vpd, winner_recent_days=args.recent_days,
                          query_samples=args.query_samples, cache_only=args.cache_only or None,
                          keyword_metrics_csv=args.metrics_csv)
    if args.no_trends:
        cfg.use_trends = False
    if args.no_durability:
        cfg.use_durability = False
    if args.no_llm:
        cfg.use_llm = False

    match = [d for d in DOMAINS if args.domain.lower() in d.name.lower()]
    if not match:
        print(f"No domain matched {args.domain!r}. Known: {[d.name for d in DOMAINS]}", file=sys.stderr)
        return 1
    domain = match[0]

    auth = _select_auth(cfg, allow_missing=cfg.cache_only)
    if auth is None and not cfg.cache_only:
        return 2
    client = YouTubeClient(auth, Cache(cfg.cache_path), daily_quota=cfg.daily_quota_units,
                           reserve=cfg.quota_reserve, daily_search_limit=cfg.daily_search_limit,
                           cache_only=cfg.cache_only)
    llm = make_llm(cfg) if cfg.use_llm else None

    probe_terms = domain_probe_terms(
        domain,
        max_terms=args.max_probe_terms,
        include_autocomplete=not args.no_probe_autocomplete,
        region=cfg.region_code,
        lang=cfg.relevance_language,
    )
    print(f"Mining breakouts in: {domain.name} "
          f"(small channels <= {cfg.small_channel_subs:,} subs, >= {cfg.winner_min_vpd:.0f} views/day, "
          f"last {cfg.winner_recent_days}d, probes {len(probe_terms)})")
    try:
        breakouts = find_breakouts(client, cfg, probe_terms, cfg.winner_recent_days,
                                   cfg.winner_min_vpd, cfg.winner_max_per_term)
    except QuotaExceeded as e:
        print(f"quota stop: {e}")
        breakouts = []

    if not breakouts:
        print("No breakouts found (no small channels cleared the velocity bar, or quota exhausted).")
        return 1

    breakouts.sort(key=lambda v: v["_vpd"], reverse=True)
    print(f"\n{len(breakouts)} breakout videos (proof of capturable demand):")
    for v in breakouts[:15]:
        print(f"  {v['_vpd']:>6.0f}/day · {v['subs']:>6,} subs · {v['_ratio']:>5.0f}x · {v['title'][:70]}")

    niches, method = discover_niches(breakouts, llm, args.max_niches * 2)
    niches = dedupe_topics(niches)[: args.max_niches]
    if not niches:
        print("\nCould not extract niche topics from breakout titles.")
        return 1
    print(f"\nDiscovered {len(niches)} candidate niches ({method}): {niches}")

    if args.emit_subtopics:
        from .subtopics import save_discovered
        path = save_discovered(domain.name, niches, meta={
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "breakout_count": len(breakouts),
            "method": method,
            "source": "winners-first",
        }, path=args.emit_out)
        print(f"\nWrote {len(niches)} discovered subtopics for {domain.name!r} -> {path}")
        print(f'`--from-domain "{domain.name}"` will now seed stage-2 from these (skipped scoring).')
        return 0

    print("\nScoring discovered niches...")
    per_topic = cfg.per_topic_unit_estimate()
    per_topic_search = cfg.per_topic_search_estimate()
    results: list[dict] = []
    for i, topic in enumerate(niches, 1):
        if client.search_calls_remaining() < per_topic_search or client.units_remaining() < per_topic:
            print(f"  Stopping early — quota nearly exhausted "
                  f"({client.search_calls_remaining()} searches, {client.units_remaining()} units left).")
            break
        print(f"[{i}/{len(niches)}] {topic}")
        try:
            row = analyze_topic(topic, client, llm, cfg, domain=domain)
        except QuotaExceeded as e:
            print(f"  quota stop: {e}")
            break
        except Exception as e:
            print(f"  skipped ({type(e).__name__}: {e})")
            continue
        if row:
            row["candidate_source"] = f"winners_{method}"
            results.append(row)

    if not results:
        print("No niches scored.")
        return 1
    results.sort(key=lambda r: (r.get("opportunity") or 0.0), reverse=True)
    results = dedupe_ranked_rows(results)

    # Per-niche viewer-facing enrichments (report-only; never touch opportunity/ranking).
    # replication_channels/dominant_format from breakout replay; positioning from the niche's own
    # scored supply metrics. Every result row gets these — the top-5 print loop below additionally
    # narrates them and (top 5 only) asks the LLM for a one-line positioning hypothesis.
    for r in results:
        replication_channels, dominant_format = replication_and_format(r["topic"], breakouts)
        r["replication_channels"] = replication_channels
        r["dominant_format"] = dominant_format
        label, reason = positioning(
            r.get("newcomer_volume"), r.get("small_channel_frac"), r.get("authority_gap")
        )
        r["positioning"] = label
        r["positioning_reason"] = reason
        r["hypothesis"] = ""  # top-5-only LLM enrichment below; "" (never None) elsewhere

    if llm is not None and getattr(llm, "enabled", False):
        for r in results[:5]:
            matches = matching_breakouts(r["topic"], breakouts)
            titles = [v["title"] for v in matches]
            questions = [str(e) for e in (r.get("comment_examples") or [])]
            hypothesis = llm.hypothesis_statement(r["topic"], titles, comment_questions=questions)
            if hypothesis:
                r["hypothesis"] = hypothesis

    csv_path, md_path = write_reports(results, cfg.out_dir, f"winners-{domain.name}")
    print(f"\nWrote:\n  {csv_path}\n  {md_path}")
    if args.snapshot:
        snap_path, snap_rows = capture_score_snapshot(
            results,
            cfg.out_dir,
            f"winners-{domain.name}",
            source="youtube_niche.winners",
            horizons=parse_horizons(args.snapshot_horizons),
            extraction_method=method,
        )
        print(f"Forward snapshot: {snap_rows} rows -> {snap_path}")
    print(f"Quota today: {client.units_spent()} units, {client.search_calls_used()} searches")
    print("\nTop discovered niches:")
    for i, r in enumerate(results[:5], 1):
        dur = r.get("trends_durability")
        dur_lbl = durability_label(dur)
        dur_txt = f", durability {round(dur * 100)}% {dur_lbl}".rstrip() if dur is not None else ""
        print(f"  {i}. {r['topic']} — {round((r.get('opportunity') or 0) * 100)}% "
              f"(raw {round((r.get('opportunity_raw') or 0) * 100)}%, "
              f"newcomer {round((r.get('newcomer_volume') or 0) * 100)}%{dur_txt})")
        # NEW lines only — the numbered entry above is unchanged for the external stdout parser.
        replication_channels = r.get("replication_channels") or 0
        dominant_format = r.get("dominant_format") or ""
        if replication_channels or dominant_format:
            repl_bit = f"🔁 replicated across {replication_channels} channels · " if replication_channels >= 3 else ""
            fmt_bit = f"dominant format: {dominant_format}" if dominant_format else "dominant format: unknown"
            print(f"     {repl_bit}{fmt_bit}")
        pos_label = r.get("positioning")
        if pos_label:
            reason = r.get("positioning_reason", "")
            newcomer_vpd = r.get("newcomer_vpd")
            if pos_label == "Learner-viable" and newcomer_vpd:
                reason = f"small channels pull {round(newcomer_vpd):,}/day here"
            print(f"     positioning: {pos_label} — {reason}")
        hypothesis = r.get("hypothesis")
        if hypothesis:
            print(f'     hypothesis: "{hypothesis}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
