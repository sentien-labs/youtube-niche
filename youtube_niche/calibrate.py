"""Auto-calibrate the demand knee (views/day) from observed small-channel performance.

The fixed `volume_knee_vpd` is a guess. This derives it from data: gather the view velocity
that SMALL channels actually achieve across a domain's niches, and suggest a knee so that a
"typical successful small channel" maps to ~0.5 demand. No prior knowledge of good niches
needed — the data is the ground truth.

Reuses cached searches when available (match the cache with --top-n / --region-code /
--relevance-language); otherwise spends fresh search quota.

Run: python -m youtube_niche.calibrate --domain "personal finance" --top-n 20 --region-code ""
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from .cache import Cache
from .channel_size import is_small_channel_at_publish
from .cli import _select_auth
from .config import Config
from .domains import DOMAINS
from .enrich import enrich
from .signals.volume import views_per_day
from .youtube_client import QuotaExceeded, YouTubeClient


def percentile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * p
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def gather_small_channel_vpds(client: YouTubeClient, cfg: Config, terms: list[str]) -> list[float]:
    now = dt.datetime.now(dt.timezone.utc)
    vpds: list[float] = []
    for t in terms:
        try:
            res = client.search(
                t, max_results=cfg.top_n,
                region=cfg.region_code, relevance_language=cfg.relevance_language,
            )
        except QuotaExceeded:
            break
        except Exception:
            continue
        try:
            records = enrich(client, res.get("items", []), cfg)
        except Exception:
            continue
        for v in records:
            subs = v.get("subs")
            if v["views"] < cfg.min_view_floor or subs is None or subs <= 0:
                continue
            if not is_small_channel_at_publish(v, cfg.small_channel_subs, now):
                continue
            d = views_per_day(v, now)
            if d is not None:
                vpds.append(d)
    return vpds


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.calibrate",
        description="Derive the demand knee (views/day) from small-channel data.",
    )
    p.add_argument("--domain", default=None, help="domain to calibrate from (its terms + subtopics)")
    p.add_argument("--target-percentile", type=float, default=0.5,
                   help="small-channel vpd percentile that should map to 0.5 demand (default 0.5)")
    p.add_argument("--top-n", type=int, default=None)
    p.add_argument("--region-code", default=None)
    p.add_argument("--relevance-language", default=None)
    p.add_argument("--cache-only", action="store_true", help="only use cached YouTube responses; never call the API")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env(top_n=args.top_n, region_code=args.region_code,
                          relevance_language=args.relevance_language,
                          cache_only=args.cache_only or None)
    auth = _select_auth(cfg, allow_missing=cfg.cache_only)
    if auth is None and not cfg.cache_only:
        return 2
    client = YouTubeClient(auth, Cache(cfg.cache_path), daily_quota=cfg.daily_quota_units,
                           reserve=cfg.quota_reserve, daily_search_limit=cfg.daily_search_limit,
                           cache_only=cfg.cache_only)

    if args.domain:
        match = [d for d in DOMAINS if args.domain.lower() in d.name.lower()]
        if not match:
            print(f"No domain matched {args.domain!r}.", file=sys.stderr)
            return 1
        terms = match[0].terms + match[0].subtopics
        label = match[0].name
    else:
        terms = [t for d in DOMAINS for t in d.terms]
        label = "all domains"

    vpds = gather_small_channel_vpds(client, cfg, terms)
    if not vpds:
        print("No small-channel data available (nothing cached and/or quota exhausted). "
              "Run a scan first or retry with fresh quota.")
        return 1

    print(f"Calibration from: {label}")
    print(f"  {len(vpds)} small-channel videos (subs <= {cfg.small_channel_subs:,})")
    print("  small-channel views/day distribution:")
    for q in (0.25, 0.5, 0.75, 0.9):
        print(f"    p{int(q * 100):>2}: {percentile(vpds, q):,.0f}/day")
    suggested = percentile(vpds, args.target_percentile)
    print(f"\n  current volume_knee_vpd : {cfg.volume_knee_vpd:,.0f}/day")
    print(f"  suggested knee (p{int(args.target_percentile * 100)} -> 0.5 demand): {suggested:,.0f}/day")
    if suggested:
        ratio = suggested / cfg.volume_knee_vpd
        verdict = ("knee is about right" if 0.7 <= ratio <= 1.4
                   else "knee is too HIGH (deflating demand)" if ratio < 0.7
                   else "knee is too LOW (inflating demand)")
        print(f"  -> {verdict}. Set with: volume_knee_vpd={suggested:.0f} (env VOLUME_KNEE_VPD or config).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
