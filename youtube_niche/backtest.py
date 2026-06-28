"""Retrospective proxy backtest for niche scoring.

The harness asks a practical question:

  If small-channel breakout videos appeared in a holdout window, did our pre-window
  scoring rank matching candidate niches near the top?

Important limitation: the YouTube Data API does not expose historical view/subscriber snapshots.
We can restrict evidence to videos published before the holdout window, but the view counts on
those older videos are still current. To avoid the worst distortion, view velocity is measured
against the real wall-clock (current views / current age = a consistent lifetime-average), NOT
current views / (as_of - pub), which would inflate just-before-holdout videos by dividing current
views over a short past window. A milder, non-inflationary leak remains — lifetime-average velocity
still includes views earned after the holdout — so treat this as a directional validation harness,
not a perfect historical replay. The leakage-free measure is the forward test (forward.py).
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
from .candidates import domain_probe_terms, domain_seed_candidates
from .channel_size import is_small_channel_at_publish, publish_time_sub_denominator, subs_at_publish_est
from .cli import _select_auth, analyze_topic
from .config import Config
from .domains import DOMAINS
from .enrich import enrich
from .llm import LLM_PROVIDERS, make_llm
from .relevance import relevance_score
from .topics import normalize_token
from .winners import _is_english, _is_junk, _is_offdomain, _is_short, discover_niches
from .signals.volume import views_per_day
from .youtube_client import CacheMiss, QuotaExceeded, YouTubeClient

STOP = {
    "the", "a", "an", "to", "of", "for", "in", "on", "how", "why", "what", "best", "top",
    "your", "you", "is", "are", "and", "with", "my", "this", "that", "explained", "tutorial",
    "guide", "review", "vs", "2024", "2025", "2026", "ultimate", "complete", "beginners",
    "want", "here", "bare", "minimum", "setup", "build", "video", "videos", "quietly",
    "beginner",
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


def _default_holdout_start(now: dt.datetime, holdout_days: int) -> dt.datetime:
    d = (now - dt.timedelta(days=holdout_days)).date()
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)


def _iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:48] or "backtest"


def tokens(s: str) -> set[str]:
    out = set()
    for raw in TOKEN_RE.findall(s.lower()):
        t = normalize_token(raw)
        if len(t) > 1 and t not in STOP:
            out.add(t)
    return out


def text_matches_topic(text: str, topic: str) -> bool:
    if relevance_score(topic, text).relevant:
        return True
    tt = tokens(topic)
    if not tt:
        return False
    tx = tokens(text)
    overlap = tt & tx
    meaningful_topic = tt - GENERIC_MATCH_TOKENS
    meaningful_overlap = overlap - GENERIC_MATCH_TOKENS
    if len(meaningful_topic) >= 4:
        return len(meaningful_overlap) >= 3
    if len(meaningful_topic) >= 3:
        return len(meaningful_overlap) >= 3
    if len(meaningful_topic) == 2:
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
    terms: list[str] | None = None,
) -> list[dict]:
    """Mine small-channel breakout videos published in the holdout window."""
    breakouts: list[dict] = []
    seen: set[str] = set()
    now = dt.datetime.now(dt.timezone.utc)
    for term in (terms or domain.terms):
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
        except CacheMiss:
            raise
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
            # Small-at-publish, not small-now: a winner that grew past the cap is still a winner.
            if not is_small_channel_at_publish(v, cfg.small_channel_subs, now):
                continue
            if _is_short(v) or _is_junk(v) or _is_offdomain(v) or not _is_english(v):
                continue
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


def discovered_candidates_from_breakouts(
    breakouts: list[dict],
    llm,
    max_candidates: int,
    source: str = "temporal_discovered_subtopic",
) -> list[tuple[str, str]]:
    """Extract seed topics from prior-window breakouts and label them as discovered candidates."""
    labels = labels_from_breakouts(breakouts, llm, max_candidates)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for topic in labels:
        key = topic.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append((topic, source))
        if len(out) >= max_candidates:
            break
    return out


def candidate_topics(
    domain,
    labels: list[str],
    source: str,
    max_candidates: int,
    subtopics_registry: str | Path | None = None,
    region: str = "US",
    lang: str = "en",
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    if source in ("labels", "both"):
        candidates.extend((t, "holdout_label") for t in labels)
    if source in ("subtopics", "both"):
        candidates.extend((t, "subtopic") for t in domain.subtopics)
    if source == "effective":
        from .subtopics import effective_subtopics

        subtopics, subtopic_source = effective_subtopics(domain, subtopics_registry)
        label = "discovered_subtopic" if subtopic_source == "discovered" else "subtopic"
        candidates.extend((t, label) for t in subtopics)
    if source in ("hybrid", "expanded"):
        candidates.extend(
            (c.topic, c.source)
            for c in domain_seed_candidates(
                domain,
                max_seeds=max_candidates,
                mode=source,
                subtopics_registry=subtopics_registry,
                region=region,
                lang=lang,
            )
        )

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
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    base = f"backtest-{_slug(domain_name)}-{holdout_start.date().isoformat()}-{stamp}"
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
        "view/subscriber counts, not historical snapshots. View velocity is measured against the "
        "real wall-clock (lifetime-average), not the as-of date, so just-before-holdout videos are "
        "no longer inflated; a milder non-inflationary leak remains.",
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


def _precision_recall_at_k(ranked: list[dict], total_breakouts: int, k: int) -> tuple[float, float]:
    """Precision = hit candidates / k; recall = unique breakout videos covered / all breakouts."""
    top = ranked[:k]
    hit_candidates = [r for r in top if r.get("backtest_hit")]
    hit_ids: set[str] = set()
    for r in top:
        hit_ids.update(r.get("hit_video_ids", []))
    precision = len(hit_candidates) / max(len(top), 1)
    recall = len(hit_ids) / total_breakouts
    return precision, recall


def compute_metrics(rows: list[dict], breakouts: list[dict], top_ks: list[int]) -> dict:
    total_breakouts = max(len(breakouts), 1)
    metrics: dict[str, str] = {
        "breakout videos": str(len(breakouts)),
        "scored candidates": str(len(rows)),
        "positive candidates": str(sum(1 for r in rows if r.get("backtest_hit"))),
    }
    first_hit = next((i for i, r in enumerate(rows, 1) if r.get("backtest_hit")), None)
    metrics["first hit rank"] = str(first_hit or "none")

    # Split by candidate source. 'subtopic' candidates are curated topics NOT derived from the
    # holdout breakouts — the clean default test. 'holdout_label' candidates ARE read off the
    # breakout titles, so they hit almost by construction; reported separately, never as headline.
    # 'discovered_subtopic' candidates replay the operational winners-first path; they are only
    # non-circular when the registry was generated before the evaluated holdout window.
    by_source: dict[str, list[dict]] = {}
    for r in rows:  # rows arrive already sorted by opportunity, so sublists keep rank order
        by_source.setdefault(r.get("candidate_source") or "unknown", []).append(r)
    metrics["clean source"] = (
        "subtopic" if "subtopic" in by_source
        else "curated" if "curated" in by_source
        else "temporal_discovered_subtopic" if "temporal_discovered_subtopic" in by_source
        else "(none — re-run with --candidate-source subtopics for a non-circular score)"
    )
    if "holdout_label" in by_source:
        metrics["holdout_label note"] = "circular by construction (labels read off the breakouts)"
    if "discovered_subtopic" in by_source:
        metrics["discovered_subtopic note"] = (
            "operational replay; clean only if the registry predates this holdout"
        )
    if any(src in by_source for src in ("discovered", "domain_autocomplete", "subtopic_autocomplete")):
        metrics["hybrid source note"] = (
            "operational replay of --from-domain hybrid; autocomplete is current, not historical"
        )
    if "temporal_discovered_subtopic" in by_source:
        metrics["temporal_discovered_subtopic note"] = (
            "clean winners-first replay: seed topics mined from a pre-holdout window"
        )

    for k in top_ks:
        p, r = _precision_recall_at_k(rows, total_breakouts, k)
        metrics[f"precision@{k}"] = f"{p:.2f}"
        metrics[f"breakout recall@{k}"] = f"{r:.2f}"
        for src in sorted(by_source):
            ps, rs = _precision_recall_at_k(by_source[src], total_breakouts, k)
            metrics[f"{src} precision@{k}"] = f"{ps:.2f}"
            metrics[f"{src} recall@{k}"] = f"{rs:.2f}"
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
        "run_id": dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"),
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
            # Clean (non-circular) headline = subtopic candidates only.
            "avg_subtopic_p@5": avg_metric("subtopic precision@5"),
            "avg_subtopic_r@5": avg_metric("subtopic recall@5"),
            "avg_curated_p@5": avg_metric("curated precision@5"),
            "avg_curated_r@5": avg_metric("curated recall@5"),
            "avg_discovered_p@5": avg_metric("discovered precision@5"),
            "avg_discovered_r@5": avg_metric("discovered recall@5"),
            "avg_domain_autocomplete_p@5": avg_metric("domain_autocomplete precision@5"),
            "avg_domain_autocomplete_r@5": avg_metric("domain_autocomplete recall@5"),
            "avg_discovered_subtopic_p@5": avg_metric("discovered_subtopic precision@5"),
            "avg_discovered_subtopic_r@5": avg_metric("discovered_subtopic recall@5"),
            "avg_temporal_discovered_subtopic_p@5": avg_metric("temporal_discovered_subtopic precision@5"),
            "avg_temporal_discovered_subtopic_r@5": avg_metric("temporal_discovered_subtopic recall@5"),
            "avg_precision@5": avg_metric("precision@5"),
            "avg_recall@5": avg_metric("breakout recall@5"),
            "avg_precision@10": avg_metric("precision@10"),
            "avg_recall@10": avg_metric("breakout recall@10"),
        })

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    csv_path = out / f"backtest-aggregate-{stamp}.csv"
    md_path = out / f"backtest-aggregate-{stamp}.md"
    fields = [
        "domain", "runs", "avg_first_hit_rank", "hit_run_rate",
        "avg_subtopic_p@5", "avg_subtopic_r@5",
        "avg_curated_p@5", "avg_curated_r@5",
        "avg_discovered_p@5", "avg_discovered_r@5",
        "avg_domain_autocomplete_p@5", "avg_domain_autocomplete_r@5",
        "avg_discovered_subtopic_p@5", "avg_discovered_subtopic_r@5",
        "avg_temporal_discovered_subtopic_p@5", "avg_temporal_discovered_subtopic_r@5",
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
        "`subtopic P@5 / R@5` are the **clean, non-circular** numbers (curated topics, not "
        "labels read off the breakouts). `discovered P@5 / R@5` replay winners-first seeds and "
        "are clean only when those seeds predate the holdout. `temporal P@5 / R@5` mines those "
        "seeds from a pre-holdout window inside the run. `domain autocomplete P@5 / R@5` is the "
        "new hybrid source slice, but it is current autocomplete, not historical. The mixed "
        "`P@5 / R@5` columns include circular holdout-label candidates and read higher — treat "
        "them as a ceiling, not the score.",
        "",
        "| Domain | Runs | Hit Run Rate | Avg First Hit | subtopic P@5 | subtopic R@5 | curated P@5 | curated R@5 | discovered P@5 | discovered R@5 | domain autocomplete P@5 | domain autocomplete R@5 | discovered-subtopic P@5 | discovered-subtopic R@5 | temporal P@5 | temporal R@5 | P@5 | R@5 | P@10 | R@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary_rows:
        lines.append(
            f"| {r['domain']} | {r['runs']} | {r['hit_run_rate']} | {r['avg_first_hit_rank']} | "
            f"{r['avg_subtopic_p@5']} | {r['avg_subtopic_r@5']} | "
            f"{r['avg_curated_p@5']} | {r['avg_curated_r@5']} | "
            f"{r['avg_discovered_p@5']} | {r['avg_discovered_r@5']} | "
            f"{r['avg_domain_autocomplete_p@5']} | {r['avg_domain_autocomplete_r@5']} | "
            f"{r['avg_discovered_subtopic_p@5']} | {r['avg_discovered_subtopic_r@5']} | "
            f"{r['avg_temporal_discovered_subtopic_p@5']} | {r['avg_temporal_discovered_subtopic_r@5']} | "
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
    p.add_argument("--fixtures", action="store_true",
                   help="run against built-in keyless fixture data (no API key or quota)")
    p.add_argument("--aggregate", action="store_true", help="aggregate prior backtest runs from the registry")
    p.add_argument("--registry", default=None, help="registry CSV path (default: out/backtest-runs.csv)")
    p.add_argument("--no-registry", action="store_true", help="do not append this run to the registry")
    p.add_argument("--holdout-days", type=int, default=180, help="holdout window length (default 180)")
    p.add_argument("--cutoff", default=None, help="holdout start date YYYY-MM-DD (default: today - holdout-days)")
    p.add_argument(
        "--candidate-source",
        choices=["subtopics", "labels", "both", "effective", "hybrid", "expanded", "temporal"],
        default="both",
    )
    p.add_argument("--subtopics-registry", default=None,
                   help="optional discovered-subtopics registry for --candidate-source effective")
    p.add_argument("--seed-window-days", type=int, default=180,
                   help="for --candidate-source temporal: days before holdout used to mine winners-first seeds")
    p.add_argument("--seed-gap-days", type=int, default=0,
                   help="for --candidate-source temporal: gap between seed window and holdout start")
    p.add_argument("--max-candidates", type=int, default=25)
    p.add_argument("--max-labels", type=int, default=12)
    p.add_argument("--top-k", default="5,10", help="comma-separated k values for precision/recall")
    p.add_argument("--min-vpd", type=float, default=None, help="breakout views/day threshold")
    p.add_argument("--max-per-term", type=int, default=None, help="breakouts kept per domain probe term")
    p.add_argument("--max-probe-terms", type=int, default=15,
                   help="domain search probes for breakout mining after autocomplete expansion")
    p.add_argument("--no-probe-autocomplete", action="store_true",
                   help="mine holdout breakouts only from hand-written domain probes")
    p.add_argument("--query-samples", type=int, default=None)
    p.add_argument("--top-n", type=int, default=None)
    p.add_argument("--with-llm", action="store_true", help="use LLM for holdout labels and quality scoring")
    p.add_argument("--with-trends", action="store_true", help="include current Trends signal")
    p.add_argument("--with-comments", action="store_true", help="include current comment-demand signal")
    p.add_argument("--llm-provider", choices=LLM_PROVIDERS, default=None)
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

    cfg.use_llm = bool(args.with_llm)
    cfg.use_trends = bool(args.with_trends)
    if not args.with_comments:
        cfg.comment_videos = 0

    if args.fixtures:
        from .fixtures import FixtureClient, fixture_domain
        cfg.use_llm = cfg.use_trends = False
        cfg.comment_videos = 0
        domain = fixture_domain()
        client = FixtureClient()
        llm = None
        print(f"FIXTURES MODE — keyless demo domain: {domain.name} (no API key/quota used)")
    else:
        if not args.domain:
            print("ERROR: provide --domain, --fixtures, or --aggregate.", file=sys.stderr)
            return 2
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

    now = dt.datetime.now(dt.timezone.utc)
    holdout_start = _parse_date(args.cutoff) if args.cutoff else _default_holdout_start(now, args.holdout_days)
    holdout_end = holdout_start + dt.timedelta(days=args.holdout_days) if args.cutoff else None
    if holdout_end and holdout_end > now:
        holdout_end = None

    min_vpd = args.min_vpd if args.min_vpd is not None else cfg.winner_min_vpd
    max_per_term = args.max_per_term if args.max_per_term is not None else cfg.winner_max_per_term
    probe_terms = domain_probe_terms(
        domain,
        max_terms=args.max_probe_terms,
        include_autocomplete=not args.no_probe_autocomplete,
        region=cfg.region_code,
        lang=cfg.relevance_language,
    )
    print(f"Backtesting {domain.name}: holdout {holdout_start.date()} to "
          f"{holdout_end.date() if holdout_end else 'now'} "
          f"({len(probe_terms)} breakout probes)")
    try:
        breakouts = mine_holdout_breakouts(
            client, cfg, domain, holdout_start, holdout_end, min_vpd, max_per_term,
            terms=probe_terms,
        )
    except CacheMiss as e:
        print(f"Cache-only miss while mining holdout breakouts: {e}")
        return 1
    if not breakouts:
        print("No holdout breakouts found.")
        return 1

    labels = labels_from_breakouts(breakouts, llm, args.max_labels)
    temporal_meta: dict[str, str] = {}
    if args.candidate_source == "temporal":
        seed_end = holdout_start - dt.timedelta(days=max(args.seed_gap_days, 0))
        seed_start = seed_end - dt.timedelta(days=max(args.seed_window_days, 1))
        print(f"Temporal seed window: {seed_start.date()} to {seed_end.date()}")
        try:
            seed_breakouts = mine_holdout_breakouts(
                client, cfg, domain, seed_start, seed_end, min_vpd, max_per_term,
                terms=probe_terms,
            )
        except CacheMiss as e:
            print(f"Cache-only miss while mining temporal seed breakouts: {e}")
            return 1
        if not seed_breakouts:
            print("No pre-holdout seed breakouts found for temporal discovered candidates.")
            return 1
        candidates = discovered_candidates_from_breakouts(
            seed_breakouts, llm, args.max_candidates
        )
        temporal_meta = {
            "temporal seed window": f"{seed_start.date().isoformat()} to {seed_end.date().isoformat()}",
            "temporal seed breakouts": str(len(seed_breakouts)),
            "temporal seed candidates": str(len(candidates)),
        }
    else:
        candidates = candidate_topics(
            domain,
            labels,
            args.candidate_source,
            args.max_candidates,
            subtopics_registry=args.subtopics_registry,
            region=cfg.region_code,
            lang=cfg.relevance_language,
        )
    if not candidates:
        print("No candidates to score.")
        return 1

    rows: list[dict] = []
    published_before = _iso_z(holdout_start)
    for i, (topic, source) in enumerate(candidates, 1):
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
    metrics.update(temporal_meta)
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
