"""Stage 1 — high-CPM domain scan.

Ranks curated high-CPM domains by `demand x low-supply x CPM`, using the same demand/supply
semantics as the niche scorer (absolute view velocity + supply gap from search, plus Trends) but aggregated
across a few representative terms per domain. No comments/depth here — those are for the
niche drill-down once a domain wins. Output: top domains with the data behind *why*.

Run: python -m youtube_niche.discover            # all domains
     python -m youtube_niche.discover --domains crypto,ai --terms 4
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from pathlib import Path
from statistics import mean, median

from .cache import Cache
from .channel_size import publish_time_sub_denominator
from .cli import _select_auth  # reuse auth selection (API key or OAuth)
from .config import Config
from .domains import DOMAINS
from .enrich import enrich
from .score import _weighted as blend
from .signals.outlier import outlier_score
from .signals.supply import supply_scores
from .signals.trends import trends_score
from .signals.volume import volume_score
from .util import clamp01
from .youtube_client import QuotaExceeded, YouTubeClient

OUTLIER_PREVALENCE_RATIO = 5.0  # a video with views >= 5x subs counts as an outlier
LOW_DEMAND_FLAG = 0.25          # below this demand_volume, warn it's a low-interest backwater


def assess_domain(domain, client: YouTubeClient, cfg: Config, use_trends: bool, terms_per_domain: int) -> dict | None:
    terms = domain.terms[:terms_per_domain]
    now = dt.datetime.now(dt.timezone.utc)
    outliers, comp, authority_gap, recent_supply_gap, age_gap, small_gap = [], [], [], [], [], []
    prevalence, ages_days, all_subs, trends_vals = [], [], [], []
    volumes, p75_volumes, recent_demand_vals, newcomer_vols, vpds, views_all = [], [], [], [], [], []
    probed: list[str] = []

    for t in terms:
        res = client.search(
            t,
            max_results=cfg.top_n,
            region=cfg.region_code,
            relevance_language=cfg.relevance_language,
        )  # QuotaExceeded propagates to caller
        items = res.get("items", [])
        try:
            records = enrich(client, items, cfg)
        except Exception:
            continue
        if not records:
            continue
        probed.append(t)

        o, _ = outlier_score(records, knee=cfg.outlier_knee, min_views=cfg.min_view_floor)
        outliers.append(o)
        vol, vd = volume_score(
            records,
            knee=cfg.volume_knee_vpd,
            min_views=cfg.min_view_floor,
            recent_days=cfg.recent_days,
            recent_success_knee=cfg.recent_success_knee,
            small_channel_subs=cfg.small_channel_subs,
        )
        volumes.append(vol)
        p75_volumes.append(vd.get("p75_volume"))
        recent_demand_vals.append(vd.get("recent_demand"))
        if vd.get("newcomer_volume") is not None:
            newcomer_vols.append(vd.get("newcomer_volume"))

        total = res.get("pageInfo", {}).get("totalResults", len(items))
        s, sd = supply_scores(
            records, total, cfg.small_channel_subs, cfg.competition_knee,
            cfg.age_knee_days, cfg.min_view_floor, topic=t,
            recent_days=cfg.recent_days,
            recent_supply_knee=cfg.recent_supply_knee,
            min_small_channel_vpd=cfg.min_small_channel_vpd,
        )
        comp.append(s["competition_gap"])
        if s.get("authority_gap") is not None:
            authority_gap.append(s["authority_gap"])
        recent_supply_gap.append(s["recent_supply_gap"])
        age_gap.append(s["age_gap"])
        if s["small_channel_gap"] is not None:
            small_gap.append(s["small_channel_gap"])
        ages_days.append(sd["median_age_days"])

        credible = [v for v in records if v["views"] >= cfg.min_view_floor]
        if credible:
            known_subs = [v for v in credible if v.get("subs") is not None and v["subs"] > 0]
            if known_subs:
                publish_sub_denoms = [
                    publish_time_sub_denominator(v, now) for v in known_subs
                ]
                known_publish_denoms = [d for d in publish_sub_denoms if d is not None]
                prevalence.append(
                    sum(
                        1
                        for v, denom in zip(known_subs, publish_sub_denoms)
                        if denom is not None and v["views"] / denom >= OUTLIER_PREVALENCE_RATIO
                    )
                    / len(known_publish_denoms)
                    if known_publish_denoms else 0.0
                )
                all_subs.extend(v["subs"] for v in known_subs)
            views_all.extend(v["views"] for v in credible)
            for v in credible:
                d = _views_per_day(v, now)
                if d is not None:
                    vpds.append(d)

        if use_trends:
            tv, _ = trends_score(t, geo=cfg.trends_geo, cache=client.cache, baseline_terms=domain.terms[:4])
            if tv is not None:
                trends_vals.append(tv)

    if not probed:
        return None

    median_vpd = median(vpds) if vpds else None
    median_views = int(median(views_all)) if views_all else None
    # Absolute demand: real view velocity — the thing the views/subs ratio does NOT capture.
    demand_volume = mean(volumes) if volumes else None

    demand = blend([
        (demand_volume, cfg.weights.volume),
        (mean(newcomer_vols) if newcomer_vols else None, cfg.weights.newcomer_volume),
        (mean([v for v in p75_volumes if v is not None]) if p75_volumes else None, cfg.weights.p75_volume),
        (mean([v for v in recent_demand_vals if v is not None]) if recent_demand_vals else None, cfg.weights.recent_demand),
        (mean(trends_vals) if trends_vals else None, cfg.weights.trends),
    ])
    supply_gap = blend([
        (mean(comp) if comp else None, cfg.weights.competition),
        (mean(authority_gap) if authority_gap else None, getattr(cfg.weights, "authority", 0.0)),
        (mean(recent_supply_gap) if recent_supply_gap else None, cfg.weights.recent_supply),
        (mean(age_gap) if age_gap else None, cfg.weights.supply_age),
        (mean(small_gap) if small_gap else None, cfg.weights.small_channel),
    ])
    cpm_score = clamp01(domain.cpm_mid / cfg.cpm_full_scale)
    # Geometric mean: a domain needs ALL of demand + supply-gap + CPM. A high score on one
    # cannot paper over a low score on another — so low demand can't hide behind low supply.
    base = _geomean([(demand, 0.5), (supply_gap, 0.3), (cpm_score, 0.2)])
    # Same hard demand gate as the niche scorer: low absolute view velocity caps the score.
    demand_gate = clamp01(median_vpd / cfg.volume_knee_vpd) if median_vpd is not None else 0.0
    score = None if base is None else clamp01(base * demand_gate)

    return {
        "name": domain.name,
        "score": score,
        "demand": demand,
        "demand_gate": demand_gate,
        "demand_volume": demand_volume,
        "median_vpd": median_vpd,
        "median_views": median_views,
        "supply_gap": supply_gap,
        "authority_gap": mean(authority_gap) if authority_gap else None,
        "cpm_score": cpm_score,
        "cpm_low": domain.cpm_low,
        "cpm_high": domain.cpm_high,
        "cpm_tier": domain.cpm_tier,
        "outlier": mean(outliers) if outliers else None,
        "trends": mean(trends_vals) if trends_vals else None,
        "prevalence": mean(prevalence) if prevalence else None,
        "median_subs": int(median(all_subs)) if all_subs else None,
        "small_frac": mean(small_gap) if small_gap else None,
        "median_age_days": int(median(ages_days)) if ages_days else None,
        "probed": probed,
        "note": domain.note,
    }


def _views_per_day(v: dict, now) -> float | None:
    try:
        pub = dt.datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))
        return v["views"] / max((now - pub).days, 1)
    except Exception:
        return None


def _geomean(parts) -> float | None:
    """Weighted geometric mean over present values — enforces 'needs all of them'."""
    present = [(max(v, 1e-3), w) for v, w in parts if v is not None]
    tw = sum(w for _, w in present)
    if tw <= 0:
        return None
    return clamp01(math.exp(sum(w * math.log(v) for v, w in present) / tw))


def _pct(x) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def _humann(n) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(int(n))


def write_domain_report(results: list[dict], out_dir: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out / f"domains-{stamp}.md"

    lines = [
        "# High-CPM domain scan — demand vs supply",
        "",
        f"_Generated {stamp}. {len(results)} domains scored, best opportunity first._",
        "",
        "**opportunity = demand × low-supply × CPM.** CPM ranges are industry estimates "
        "(not from the API). Demand/supply are live from YouTube.",
        "",
    ]
    for i, r in enumerate(results, 1):
        lines.append(
            f"## {i}. {r['name']} — {_pct(r.get('score'))}  "
            f"(CPM ~${r['cpm_low']:.0f}–{r['cpm_high']:.0f}, {r['cpm_tier']})"
        )
        lines.append("")
        lines.append(
            f"- **Demand {_pct(r.get('demand'))}** · gate {_pct(r.get('demand_gate'))} · "
            f"volume {_pct(r.get('demand_volume'))} "
            f"({_humann(r.get('median_vpd'))}/day median, {_humann(r.get('median_views'))} median views) · "
            f"outlier {_pct(r.get('outlier'))} · trends {_pct(r.get('trends'))}"
        )
        if (r.get("demand_volume") or 0) < LOW_DEMAND_FLAG:
            lines.append(
                "  - **Low absolute demand** — small/stale channels here likely reflect weak "
                "interest, not an opening. The views÷subs ratio overstates this domain."
            )
        lines.append(
            f"- **Low supply {_pct(r.get('supply_gap'))}** · "
            f"top videos median {_humann(r.get('median_subs'))} subs · "
            f"{_pct(r.get('small_frac'))} from small channels · "
            f"authority {_pct(r.get('authority_gap'))} · "
            f"median age {r.get('median_age_days', '?')}d"
        )
        lines.append(f"- _Probed:_ {', '.join(r.get('probed', []))}")
        if r.get("note"):
            lines.append(f"- _Note:_ {r['note']}")
        lines.append("")
    path.write_text("\n".join(lines))
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.discover",
        description="Stage 1: rank high-CPM domains by demand vs supply gap.",
    )
    p.add_argument("--terms", type=int, default=3, help="search terms probed per domain (default 3)")
    p.add_argument("--domains", default=None, help="comma-separated substrings to subset domains")
    p.add_argument("--top-n", type=int, default=None, help="search results scanned per term (default 30)")
    p.add_argument("--no-trends", action="store_true", help="skip Google Trends (faster)")
    p.add_argument("--out-dir", default=None, help="output directory (default ./out)")
    p.add_argument("--search-limit", type=int, default=None, help="override daily search.list call budget")
    p.add_argument("--region-code", default=None, help="YouTube search region code (default US)")
    p.add_argument("--relevance-language", default=None, help="YouTube relevance language (default en)")
    p.add_argument("--trends-geo", default=None, help="Google Trends geo (default US)")
    p.add_argument("--cache-only", action="store_true", help="only use cached YouTube responses; never call the API")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env(
        top_n=args.top_n,
        out_dir=args.out_dir,
        daily_search_limit=args.search_limit,
        region_code=args.region_code,
        relevance_language=args.relevance_language,
        trends_geo=args.trends_geo,
        cache_only=args.cache_only or None,
    )

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

    domains = DOMAINS
    if args.domains:
        wanted = [w.strip().lower() for w in args.domains.split(",") if w.strip()]
        domains = [d for d in DOMAINS if any(w in d.name.lower() for w in wanted)]
        if not domains:
            print(f"No domains matched {args.domains!r}. Known: {[d.name for d in DOMAINS]}")
            return 1

    use_trends = not args.no_trends
    per_domain = args.terms * 2  # videos + channels per term; searches are tracked separately
    per_domain_searches = args.terms
    results: list[dict] = []
    for i, d in enumerate(domains, 1):
        if client.units_remaining() < per_domain or client.search_calls_remaining() < per_domain_searches:
            print(
                f"  Stopping early — low quota ({client.units_remaining()} units, "
                f"{client.search_calls_remaining()} searches; ~{per_domain} units + "
                f"{per_domain_searches} searches/domain)."
            )
            break
        print(
            f"[{i}/{len(domains)}] {d.name}  "
            f"(quota left ~{client.units_remaining()} units, {client.search_calls_remaining()} searches)"
        )
        try:
            r = assess_domain(d, client, cfg, use_trends=use_trends, terms_per_domain=args.terms)
        except QuotaExceeded as e:
            print(f"  quota stop: {e}")
            break
        except Exception as e:
            print(f"  skipped ({type(e).__name__}: {e})")
            continue
        if r:
            results.append(r)

    if not results:
        print("No domain results produced.")
        return 1

    results.sort(key=lambda r: (r.get("score") or 0.0), reverse=True)
    path = write_domain_report(results, cfg.out_dir)

    print(f"\nWrote: {path}")
    print(
        f"Quota used today: {client.units_spent()} / {cfg.daily_quota_units} units; "
        f"{client.search_calls_used()} / {cfg.daily_search_limit} searches"
    )
    print("\nTop domains:")
    for i, r in enumerate(results[:3], 1):
        print(
            f"  {i}. {r['name']} — {_pct(r.get('score'))}  "
            f"(demand {_pct(r.get('demand'))} @ {_humann(r.get('median_vpd'))} views/day, "
            f"low-supply {_pct(r.get('supply_gap'))}, CPM {r['cpm_tier']})"
        )
    cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
