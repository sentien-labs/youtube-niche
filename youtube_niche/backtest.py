"""Retrospective proxy backtest for niche scoring.

The harness asks a practical question:

  If small-channel breakout videos appeared in a holdout window, did our pre-window
  scoring rank matching candidate niches near the top?

Important limitation: the YouTube Data API does not expose historical view/subscriber snapshots.
We can restrict evidence to videos published before the holdout window, but the view counts on
those older videos are still current. Treat this as a directional validation harness, not a
perfect historical replay.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

from .cache import Cache
from .cli import _select_auth, analyze_topic
from .config import Config
from .domains import DOMAINS
from .enrich import enrich
from .llm import make_llm
from .winners import _is_english, _is_junk, _is_short, discover_niches
from .signals.volume import views_per_day
from .youtube_client import QuotaExceeded, YouTubeClient

STOP = {
    "the", "a", "an", "to", "of", "for", "in", "on", "how", "why", "what", "best", "top",
    "your", "you", "is", "are", "and", "with", "my", "this", "that", "explained", "tutorial",
    "guide", "review", "vs", "2024", "2025", "2026", "ultimate", "complete", "beginners",
    "want", "here", "bare", "minimum", "setup", "build", "video", "videos", "quietly",
}
TOKEN_RE = re.compile(r"[a-z0-9]+")
GENERIC_MATCH_TOKENS = {
    "ai", "chatgpt", "claude", "youtube", "video", "videos", "tool", "tools", "business",
    "beginner", "beginners", "tutorial", "guide", "explained",
}
REGISTRY_FIELDS = [
    "run_id", "generated_at", "domain", "holdout_start", "holdout_end", "candidate_source",
    "max_candidates", "query_samples", "top_n", "with_llm", "with_trends", "with_comments",
    "cache_only", "breakout_videos", "scored_candidates", "positive_candidates",
    "first_hit_rank", "metrics_json", "csv_path", "md_path",
]


def _parse_date(s: str) -> dt.datetime:
    d = dt.date.fromisoformat(s)
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)


def _iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:48] or "backtest"


def tokens(s: str) -> set[str]:
    return {t for t in TOKEN_RE.findall(s.lower()) if len(t) > 1 and t not in STOP}


def text_matches_topic(text: str, topic: str) -> bool:
    tt = tokens(topic)
    if not tt:
        return False
    tx = tokens(text)
    overlap = tt & tx
    meaningful_topic = tt - GENERIC_MATCH_TOKENS
    meaningful_overlap = overlap - GENERIC_MATCH_TOKENS
    if len(meaningful_topic) >= 2:
        return len(meaningful_overlap) >= 2
    return len(overlap) >= max(1, min(2, len(tt)))


def simple_label_from_title(title: str) -> str:
    toks = [t for t in TOKEN_RE.findall(title.lower()) if len(t) > 1 and t not in STOP]
    return " ".join(toks[:5]).strip()


def mine_holdout_breakouts(
    client: YouTubeClient,
    cfg: Config,
    domain,
    holdout_start: dt.datetime,
    holdout_end: dt.datetime | None,
    min_vpd: float,
    max_per_term: int,
) -> list[dict]:
    """Mine small-channel breakout videos published in the holdout window."""
    breakouts: list[dict] = []
    seen: set[str] = set()
    now = dt.datetime.now(dt.timezone.utc)
    for term in domain.terms:
        if client.search_calls_remaining() < 1:
            break
        try:
            res = client.search(
                term,
                max_results=cfg.top_n,
                order="viewCount",
                published_after=_iso_z(holdout_start),
                published_before=_iso_z(holdout_end) if holdout_end else None,
                region=cfg.region_code,
                relevance_language=cfg.relevance_language,
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
            if v["views"] < cfg.min_view_floor or subs is None or subs <= 0 or subs > cfg.small_channel_subs:
                continue
            if _is_short(v) or _is_junk(v) or not _is_english(v):
                continue
            vpd = views_per_day(v, now)
            if vpd is None or vpd < min_vpd:
                continue
            v["_vpd"] = vpd
            v["_ratio"] = v["views"] / subs
            scored.append(v)
        scored.sort(key=lambda v: v["_vpd"], reverse=True)
        for v in scored[:max_per_term]:
            if v["video_id"] not in seen:
                seen.add(v["video_id"])
                breakouts.append(v)

    best_by_channel: dict[str, dict] = {}
    for v in breakouts:
        cid = v["channel_id"]
        if cid not in best_by_channel or v["_vpd"] > best_by_channel[cid]["_vpd"]:
            best_by_channel[cid] = v
    return list(best_by_channel.values())


def labels_from_breakouts(breakouts: list[dict], llm, max_labels: int) -> list[str]:
    topics, _method = discover_niches(breakouts, llm, max_niches=max_labels)
    if topics:
        return topics
    labels: list[str] = []
    seen: set[str] = set()
    for v in breakouts:
        label = simple_label_from_title(v["title"])
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
        if len(labels) >= max_labels:
            break
    return labels


def candidate_topics(domain, labels: list[str], source: str, max_candidates: int) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if source in ("labels", "both"):
        candidates.extend((t, "holdout_label") for t in labels)
    if source in ("subtopics", "both"):
        candidates.extend((t, "subtopic") for t in domain.subtopics)

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for topic, src in candidates:
        key = topic.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append((topic, src))
        if len(out) >= max_candidates:
            break
    return out


def matched_breakouts(topic: str, labels: list[str], breakouts: list[dict]) -> list[dict]:
    """Return breakout videos that appear to belong to this candidate topic."""
    matching_labels = [
        label for label in labels
        if text_matches_topic(label, topic) or text_matches_topic(topic, label)
    ]
    hits = []
    for v in breakouts:
        if text_matches_topic(v["title"], topic) or text_matches_topic(topic, v["title"]):
            hits.append(v)
            continue
        if any(text_matches_topic(v["title"], label) or text_matches_topic(label, v["title"])
               for label in matching_labels):
            hits.append(v)
    return hits


def _fmt(x):
    if x is None:
        return ""
    if isinstance(x, float):
        return round(x, 4)
    return x


def write_backtest_report(
    rows: list[dict],
    breakouts: list[dict],
    metrics: dict,
    out_dir: str,
    domain_name: str,
    holdout_start: dt.datetime,
    holdout_end: dt.datetime | None,
) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"backtest-{_slug(domain_name)}-{stamp}"
    csv_path = out / f"{base}.csv"
    md_path = out / f"{base}.md"
    fields = [
        "rank", "topic", "candidate_source", "backtest_hit", "hit_count", "hit_titles",
        "opportunity", "opportunity_raw", "confidence", "demand_gate", "relevance_gate",
        "demand", "supply_gap", "cpm_score", "median_vpd", "newcomer_vpd",
        "credible_results", "title_match_frac", "query_samples", "search_queries",
    ]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, row in enumerate(rows, 1):
            out_row = {k: _fmt(row.get(k)) for k in fields}
            out_row["rank"] = i
            w.writerow(out_row)

    end_label = holdout_end.date().isoformat() if holdout_end else "now"
    lines = [
        f"# Backtest — {domain_name}",
        "",
        f"_Generated {stamp}. Holdout window: {holdout_start.date().isoformat()} to {end_label}._",
        "",
        "This is a retrospective proxy backtest. Searches for scoring are restricted to videos "
        "published before the holdout start when possible, but YouTube returns current public "
        "view/subscriber counts, not historical snapshots.",
        "",
        "## Metrics",
        "",
    ]
    for k, v in metrics.items():
        lines.append(f"- **{k}**: {v}")
    lines += ["", "## Holdout Breakouts", ""]
    for v in sorted(breakouts, key=lambda x: x["_vpd"], reverse=True)[:20]:
        lines.append(f"- {v['_vpd']:.0f}/day · {v['subs']:,} subs · {v['title']}")
    lines += ["", "## Ranked Candidates", ""]
    for i, row in enumerate(rows[:25], 1):
        marker = "hit" if row.get("backtest_hit") else "miss"
        lines.append(
            f"{i}. **{row['topic']}** — {round((row.get('opportunity') or 0) * 100)}% "
            f"({marker}, raw {round((row.get('opportunity_raw') or 0) * 100)}%, "
            f"confidence {round((row.get('confidence') or 0) * 100)}%)"
        )
        if row.get("hit_titles"):
            lines.append(f"   - matched: {row['hit_titles']}")
    md_path.write_text("\n".join(lines))
    return csv_path, md_path


def compute_metrics(rows: list[dict], breakouts: list[dict], top_ks: list[int]) -> dict:
    total_breakouts = max(len(breakouts), 1)
    metrics: dict[str, str] = {
        "breakout videos": str(len(breakouts)),
        "scored candidates": str(len(rows)),
        "positive candidates": str(sum(1 for r in rows if r.get("backtest_hit"))),
    }
    first_hit = next((i for i, r in enumerate(rows, 1) if r.get("backtest_hit")), None)
    metrics["first hit rank"] = str(first_hit or "none")
    for k in top_ks:
        top = rows[:k]
        hit_candidates = [r for r in top if r.get("backtest_hit")]
        hit_ids = set()
        for r in top:
            hit_ids.update(r.get("hit_video_ids", []))
        precision = len(hit_candidates) / max(len(top), 1)
        recall = len(hit_ids) / total_breakouts
        metrics[f"precision@{k}"] = f"{precision:.2f}"
        metrics[f"breakout recall@{k}"] = f"{recall:.2f}"
    return metrics


def registry_path(out_dir: str, explicit: str | None = None) -> Path:
    return Path(explicit) if explicit else Path(out_dir) / "backtest-runs.csv"


def append_registry(
    path: Path,
    metrics: dict,
    csv_path: Path,
    md_path: Path,
    domain_name: str,
    holdout_start: dt.datetime,
    holdout_end: dt.datetime | None,
    args,
    cfg: Config,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    row = {
        "run_id": dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
        "generated_at": generated_at,
        "domain": domain_name,
        "holdout_start": holdout_start.date().isoformat(),
        "holdout_end": holdout_end.date().isoformat() if holdout_end else "now",
        "candidate_source": args.candidate_source,
        "max_candidates": args.max_candidates,
        "query_samples": cfg.query_samples,
        "top_n": cfg.top_n,
        "with_llm": bool(args.with_llm),
        "with_trends": bool(args.with_trends),
        "with_comments": bool(args.with_comments),
        "cache_only": bool(args.cache_only),
        "breakout_videos": metrics.get("breakout videos", ""),
        "scored_candidates": metrics.get("scored candidates", ""),
        "positive_candidates": metrics.get("positive candidates", ""),
        "first_hit_rank": metrics.get("first hit rank", ""),
        "metrics_json": json.dumps(metrics, sort_keys=True),
        "csv_path": str(csv_path),
        "md_path": str(md_path),
    }
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REGISTRY_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _as_float(x) -> float | None:
    try:
        if x in (None, "", "none"):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def aggregate_registry(path: Path, out_dir: str) -> tuple[Path, Path]:
    if not path.exists():
        raise FileNotFoundError(f"No backtest registry found: {path}")
    with path.open() as f:
        rows = list(csv.DictReader(f))
    groups: dict[str, list[dict]] = {"ALL": rows}
    for row in rows:
        groups.setdefault(row.get("domain") or "unknown", []).append(row)

    summary_rows = []
    for domain, group in sorted(groups.items()):
        first_hits = [_as_float(r.get("first_hit_rank")) for r in group]
        first_hits = [x for x in first_hits if x is not None]
        metrics = []
        for r in group:
            try:
                metrics.append(json.loads(r.get("metrics_json") or "{}"))
            except json.JSONDecodeError:
                metrics.append({})

        def avg_metric(key: str) -> str:
            vals = [_as_float(m.get(key)) for m in metrics]
            vals = [v for v in vals if v is not None]
            return "" if not vals else f"{sum(vals) / len(vals):.2f}"

        summary_rows.append({
            "domain": domain,
            "runs": len(group),
            "avg_first_hit_rank": "" if not first_hits else f"{sum(first_hits) / len(first_hits):.1f}",
            "hit_run_rate": f"{len(first_hits) / max(len(group), 1):.2f}",
            "avg_precision@5": avg_metric("precision@5"),
            "avg_recall@5": avg_metric("breakout recall@5"),
            "avg_precision@10": avg_metric("precision@10"),
            "avg_recall@10": avg_metric("breakout recall@10"),
        })

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out / f"backtest-aggregate-{stamp}.csv"
    md_path = out / f"backtest-aggregate-{stamp}.md"
    fields = [
        "domain", "runs", "avg_first_hit_rank", "hit_run_rate",
        "avg_precision@5", "avg_recall@5", "avg_precision@10", "avg_recall@10",
    ]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(summary_rows)
    lines = [
        "# Backtest Aggregate",
        "",
        f"_Generated {stamp} from `{path}`._",
        "",
        "| Domain | Runs | Hit Run Rate | Avg First Hit | P@5 | R@5 | P@10 | R@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary_rows:
        lines.append(
            f"| {r['domain']} | {r['runs']} | {r['hit_run_rate']} | {r['avg_first_hit_rank']} | "
            f"{r['avg_precision@5']} | {r['avg_recall@5']} | {r['avg_precision@10']} | {r['avg_recall@10']} |"
        )
    md_path.write_text("\n".join(lines))
    return csv_path, md_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.backtest",
        description="Retrospective proxy backtest against small-channel breakout videos.",
    )
    p.add_argument("--domain", default=None, help='domain to backtest, e.g. "personal finance"')
    p.add_argument("--aggregate", action="store_true", help="aggregate prior backtest runs from the registry")
    p.add_argument("--registry", default=None, help="registry CSV path (default: out/backtest-runs.csv)")
    p.add_argument("--no-registry", action="store_true", help="do not append this run to the registry")
    p.add_argument("--holdout-days", type=int, default=180, help="holdout window length (default 180)")
    p.add_argument("--cutoff", default=None, help="holdout start date YYYY-MM-DD (default: today - holdout-days)")
    p.add_argument("--candidate-source", choices=["subtopics", "labels", "both"], default="both")
    p.add_argument("--max-candidates", type=int, default=25)
    p.add_argument("--max-labels", type=int, default=12)
    p.add_argument("--top-k", default="5,10", help="comma-separated k values for precision/recall")
    p.add_argument("--min-vpd", type=float, default=None, help="breakout views/day threshold")
    p.add_argument("--max-per-term", type=int, default=None, help="breakouts kept per domain probe term")
    p.add_argument("--query-samples", type=int, default=None)
    p.add_argument("--top-n", type=int, default=None)
    p.add_argument("--with-llm", action="store_true", help="use LLM for holdout labels and quality scoring")
    p.add_argument("--with-trends", action="store_true", help="include current Trends signal")
    p.add_argument("--with-comments", action="store_true", help="include current comment-demand signal")
    p.add_argument("--llm-provider", choices=["auto", "anthropic", "codex", "claude", "agy"], default=None)
    p.add_argument("--cache-only", action="store_true", help="only use cached YouTube responses; never call the API")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--region-code", default=None)
    p.add_argument("--relevance-language", default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env(
        top_n=args.top_n,
        query_samples=args.query_samples,
        out_dir=args.out_dir,
        llm_provider=args.llm_provider,
        region_code=args.region_code,
        relevance_language=args.relevance_language,
        cache_only=args.cache_only or None,
    )
    reg_path = registry_path(cfg.out_dir, args.registry)
    if args.aggregate:
        try:
            csv_path, md_path = aggregate_registry(reg_path, cfg.out_dir)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"Wrote aggregate:\n  {csv_path}\n  {md_path}")
        return 0

    if not args.domain:
        print("ERROR: provide --domain, or use --aggregate.", file=sys.stderr)
        return 2
    cfg.use_llm = bool(args.with_llm)
    cfg.use_trends = bool(args.with_trends)
    if not args.with_comments:
        cfg.comment_videos = 0

    match = [d for d in DOMAINS if args.domain.lower() in d.name.lower()]
    if not match:
        print(f"No domain matched {args.domain!r}. Known: {[d.name for d in DOMAINS]}", file=sys.stderr)
        return 1
    domain = match[0]

    now = dt.datetime.now(dt.timezone.utc)
    holdout_start = _parse_date(args.cutoff) if args.cutoff else now - dt.timedelta(days=args.holdout_days)
    holdout_end = holdout_start + dt.timedelta(days=args.holdout_days) if args.cutoff else None
    if holdout_end and holdout_end > now:
        holdout_end = None

    auth = _select_auth(cfg, allow_missing=cfg.cache_only)
    if auth is None and not cfg.cache_only:
        return 2
    client = YouTubeClient(auth, Cache(cfg.cache_path), daily_quota=cfg.daily_quota_units,
                           reserve=cfg.quota_reserve, daily_search_limit=cfg.daily_search_limit,
                           cache_only=cfg.cache_only)
    llm = make_llm(cfg) if cfg.use_llm else None

    min_vpd = args.min_vpd if args.min_vpd is not None else cfg.winner_min_vpd
    max_per_term = args.max_per_term if args.max_per_term is not None else cfg.winner_max_per_term
    print(f"Backtesting {domain.name}: holdout {holdout_start.date()} to "
          f"{holdout_end.date() if holdout_end else 'now'}")
    breakouts = mine_holdout_breakouts(client, cfg, domain, holdout_start, holdout_end, min_vpd, max_per_term)
    if not breakouts:
        print("No holdout breakouts found.")
        return 1

    labels = labels_from_breakouts(breakouts, llm, args.max_labels)
    candidates = candidate_topics(domain, labels, args.candidate_source, args.max_candidates)
    if not candidates:
        print("No candidates to score.")
        return 1

    rows: list[dict] = []
    per_topic_units = cfg.per_topic_unit_estimate()
    per_topic_search = cfg.per_topic_search_estimate()
    published_before = _iso_z(holdout_start)
    for i, (topic, source) in enumerate(candidates, 1):
        if client.search_calls_remaining() < per_topic_search or client.units_remaining() < per_topic_units:
            print("Stopping early: quota nearly exhausted.")
            break
        print(f"[{i}/{len(candidates)}] {topic}")
        try:
            row = analyze_topic(topic, client, llm, cfg, domain=domain,
                                published_before=published_before, as_of=holdout_start)
        except QuotaExceeded as e:
            print(f"  quota stop: {e}")
            break
        except Exception as e:
            print(f"  skipped ({type(e).__name__}: {e})")
            continue
        if not row:
            continue
        hits = matched_breakouts(topic, labels, breakouts)
        row["candidate_source"] = source
        row["backtest_hit"] = bool(hits)
        row["hit_count"] = len(hits)
        row["hit_video_ids"] = [h["video_id"] for h in hits]
        row["hit_titles"] = " | ".join(h["title"][:90] for h in hits[:3])
        rows.append(row)

    if not rows:
        print("No rows scored.")
        return 1
    rows.sort(key=lambda r: (r.get("opportunity") or 0.0), reverse=True)
    top_ks = [int(x) for x in args.top_k.split(",") if x.strip().isdigit()]
    metrics = compute_metrics(rows, breakouts, top_ks or [5, 10])
    csv_path, md_path = write_backtest_report(
        rows, breakouts, metrics, cfg.out_dir, domain.name, holdout_start, holdout_end
    )
    if not args.no_registry:
        append_registry(reg_path, metrics, csv_path, md_path, domain.name, holdout_start, holdout_end, args, cfg)
    print(f"\nWrote:\n  {csv_path}\n  {md_path}")
    if not args.no_registry:
        print(f"Registry: {reg_path}")
    print(f"Quota today: {client.units_spent()} units, {client.search_calls_used()} searches")
    print("Metrics:")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
