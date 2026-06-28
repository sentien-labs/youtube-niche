"""Write a scored CSV (machine-readable) and a Markdown report (human-readable, explainable)."""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from .evidence import CHANNEL_EVIDENCE_FIELDS, VIDEO_EVIDENCE_FIELDS
from .evidence_snapshot import capture_evidence_snapshot

CSV_FIELDS = [
    "rank",
    "topic",
    "candidate_source",
    "query_samples",
    "search_queries",
    "query_coverage",
    "query_consensus",
    "cluster_size",
    "cluster_topics",
    "opportunity",
    "opportunity_raw",
    "opportunity_base",
    "confidence",
    "demand_gate",
    "relevance_gate",
    "demand",
    "supply_gap",
    "cpm_score",
    "cpm_mid",
    "cpm_source",
    "ad_intent",
    "quality_gap",
    "volume",
    "newcomer_volume",
    "p75_volume",
    "recent_demand",
    "median_vpd",
    "newcomer_vpd",
    "newcomer_sample",
    "p75_vpd",
    "median_views",
    "recent_success_count",
    "outlier",
    "trends",
    "comment_demand",
    "external_demand",
    "competition_gap",
    "authority_gap",
    "recent_supply_gap",
    "age_gap",
    "small_channel_gap",
    "max_outlier_ratio",
    "outlier_unknown_subs",
    "credible_results",
    "raw_credible_results",
    "sampled_results",
    "credible_density",
    "title_match_frac",
    "semantic_title_match_frac",
    "avg_relevance_score",
    "recent_credible_results",
    "median_age_days",
    "known_subscriber_results",
    "unknown_subscriber_results",
    "small_channel_frac",
    "unique_credible_channels",
    "top_channel_share",
    "top3_channel_share",
    "n_comments",
    "n_comment_requests",
    "trends_status",
    "trend_slope_score",
    "trend_level_score",
    "trend_breakout_score",
    "trend_rising_score",
    "trend_rising_terms",
    "trends_durability",
    "trends_durability_ratio",
    "durability_status",
    "external_metric_topic",
    "external_cpm_score",
    "external_cpm",
    "external_monthly_searches",
    "quality_status",
    "quality_attempted",
    "quality_scored",
    "avg_depth",
]


def _fmt(x):
    if x is None:
        return ""
    if isinstance(x, float):
        return round(x, 3)
    return x


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:40] or "niche"


def write_reports(results: list[dict], out_dir: str, niche: str):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    base = f"{_slug(niche)}-{stamp}"
    csv_path = out / f"{base}.csv"
    md_path = out / f"{base}.md"
    video_evidence_path = out / f"{base}-video-evidence.csv"
    channel_evidence_path = out / f"{base}-channel-evidence.csv"

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for i, r in enumerate(results, 1):
            row = {k: _fmt(r.get(k)) for k in CSV_FIELDS}
            row["rank"] = i
            w.writerow(row)

    video_rows, channel_rows = _evidence_sidecar_rows(results)
    if video_rows:
        _write_rows(
            video_evidence_path,
            [
                "category_evidence_rank",
                "opportunity_evidence_score",
                "topic_rank",
                "topic_opportunity",
                "topic_demand",
                "topic_supply_gap",
                "topic_relevance_gate",
                "candidate_source",
                *VIDEO_EVIDENCE_FIELDS,
            ],
            video_rows,
        )
    if channel_rows:
        _write_rows(
            channel_evidence_path,
            [
                "category_channel_rank",
                "opportunity_channel_score",
                "topic_rank",
                "topic_opportunity",
                "topic_demand",
                "topic_supply_gap",
                "topic_relevance_gate",
                "candidate_source",
                *CHANNEL_EVIDENCE_FIELDS,
            ],
            channel_rows,
        )
    evidence_snapshot: tuple[Path, int] | None = None
    if video_rows or channel_rows:
        evidence_snapshot = capture_evidence_snapshot(results, out_dir, niche, source="report", top_n=10)

    lines = [
        f'# YouTube niche opportunities — "{niche}"',
        "",
        f"_Generated {stamp}. {len(results)} topics scored, highest opportunity first._",
        "",
        "**opportunity = confidence × demand-gate × demand × low-supply × monetization × "
        "thin-content.** Each topic shows why it scored what it did, so you can judge fit — "
        "not just trust a number.",
        "",
    ]
    evidence_bits = []
    if video_rows:
        evidence_bits.append(f"`{video_evidence_path.name}`")
    if channel_rows:
        evidence_bits.append(f"`{channel_evidence_path.name}`")
    if evidence_bits:
        lines += [
            f"_Video/channel evidence sidecars: {', '.join(evidence_bits)}._",
            "",
        ]
    if evidence_snapshot:
        snap_path, snap_rows = evidence_snapshot
        lines += [
            f"_Evidence snapshot registry: `{snap_path.name}` ({snap_rows} pending video/channel proof rows)._",
            "",
        ]
    for i, r in enumerate(results, 1):
        lines += _topic_block(i, r)
    md_path.write_text("\n".join(lines))

    return csv_path, md_path


def _write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: _fmt(row.get(k)) for k in fields})


def _evidence_sidecar_rows(results: list[dict]) -> tuple[list[dict], list[dict]]:
    video_rows: list[dict] = []
    channel_rows: list[dict] = []
    for topic_rank, row in enumerate(results, 1):
        context = {
            "topic_rank": topic_rank,
            "topic_opportunity": row.get("opportunity"),
            "topic_demand": row.get("demand"),
            "topic_supply_gap": row.get("supply_gap"),
            "topic_relevance_gate": row.get("relevance_gate"),
            "candidate_source": row.get("candidate_source"),
        }
        for ev in row.get("video_evidence") or []:
            score = (row.get("opportunity") or 0.0) * (ev.get("evidence_score") or 0.0)
            video_rows.append({**context, "opportunity_evidence_score": score, **ev})
        for ev in row.get("channel_evidence") or []:
            score = (row.get("opportunity") or 0.0) * (
                ev.get("channel_trajectory_score")
                if ev.get("channel_trajectory_score") is not None
                else ev.get("channel_evidence_score") or 0.0
            )
            channel_rows.append({**context, "opportunity_channel_score": score, **ev})
    video_rows.sort(
        key=lambda r: (
            float(r.get("opportunity_evidence_score") or 0.0),
            float(r.get("views_per_day") or 0.0),
        ),
        reverse=True,
    )
    for i, row in enumerate(video_rows, 1):
        row["category_evidence_rank"] = i
    channel_rows.sort(
        key=lambda r: (
            float(r.get("opportunity_channel_score") or 0.0),
            float(r.get("max_views_per_day") or 0.0),
        ),
        reverse=True,
    )
    for i, row in enumerate(channel_rows, 1):
        row["category_channel_rank"] = i
    return video_rows, channel_rows


def _pct(x) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def _num(x) -> str:
    if x is None:
        return "?"
    if isinstance(x, float):
        if x >= 1000:
            return f"{x / 1000:.1f}k"
        return str(round(x, 1))
    return str(x)


def _short(text, n: int = 58) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    return text[: max(0, n - 1)].rstrip() + "…"


def _vpd(x) -> str:
    if x is None:
        return "?/day"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "?/day"
    if x >= 1000:
        return f"{x / 1000:.1f}k/day"
    return f"{round(x):.0f}/day"


def _topic_block(i: int, r: dict) -> list[str]:
    out = [f"## {i}. {r['topic']} — opportunity {_pct(r.get('opportunity'))}", ""]
    vpd = r.get("median_vpd")
    vpd_str = f"{vpd / 1000:.0f}k" if vpd and vpd >= 1000 else (str(int(vpd)) if vpd else "?")
    out.append(
        f"- **Demand {_pct(r.get('demand'))}** · gate {_pct(r.get('demand_gate'))} · "
        f"relevance {_pct(r.get('relevance_gate'))} · "
        f"median {_pct(r.get('volume'))} ({vpd_str}/day) · "
        f"newcomer {_pct(r.get('newcomer_volume'))} ({_num(r.get('newcomer_vpd'))}/day, n={r.get('newcomer_sample', 0)}) · "
        f"p75 {_pct(r.get('p75_volume'))} ({_num(r.get('p75_vpd'))}/day) · "
        f"recent hits {_num(r.get('recent_success_count'))} · trends {_pct(r.get('trends'))} · "
        f"comments {_pct(r.get('comment_demand'))} ({r.get('n_comment_requests', 0)} requests) · "
        f"external {_pct(r.get('external_demand'))}"
    )
    if (r.get("demand_gate") or 0) < 0.5 and (r.get("relevance_gate") or 0) >= 1.0:
        out.append("  - _Demand capped: current view velocity is below the practical demand floor._")
    if (r.get("relevance_gate") or 0) < 1.0:
        out.append("  - _Relevance capped: too few credible videos clearly match the exact niche._")
    out.append(
        f"- **Monetization {_pct(r.get('cpm_score'))}** · "
        f"CPM proxy {_num(r.get('cpm_mid'))} · {r.get('cpm_source', 'unknown')}"
    )
    out.append(
        f"- **Low supply {_pct(r.get('supply_gap'))}** · "
        f"{r.get('credible_results', '?')}/{r.get('sampled_results', '?')} relevant credible videos · "
        f"{r.get('recent_credible_results', '?')} recent credible · "
        f"title match {_pct(r.get('title_match_frac'))} · "
        f"semantic {_pct(r.get('semantic_title_match_frac'))} · "
        f"median age {r.get('median_age_days', '?')}d · "
        f"{_pct(r.get('small_channel_frac'))} from small channels · "
        f"authority {_pct(r.get('authority_gap'))} "
        f"(top 3 channels {_pct(r.get('top3_channel_share'))}) · "
        f"beatability {_pct(r.get('outlier'))} (views÷subs ≈ {r.get('max_outlier_ratio', '?')}×)"
    )
    video_evidence = [
        e for e in (r.get("video_evidence") or [])
        if e.get("evidence_role") != "off_topic_sample"
    ][:3]
    if video_evidence:
        out.append(
            "- **Top video proof** · "
            + "; ".join(
                f"{_short(e.get('title'))} ({_vpd(e.get('views_per_day'))}, "
                f"{_num(e.get('subs'))} subs, {e.get('evidence_role')})"
                for e in video_evidence
            )
        )
    channel_evidence = [
        e for e in (r.get("channel_evidence") or [])
        if (e.get("relevant_videos") or 0) > 0
    ][:3]
    if channel_evidence:
        out.append(
            "- **Top channel proof** · "
            + "; ".join(
                f"{_short(e.get('channel_title'), 34)} "
                f"({_vpd(e.get('max_views_per_day'))}, "
                f"{e.get('relevant_videos')} relevant videos, "
                f"trajectory {_pct(e.get('channel_trajectory_score'))})"
                for e in channel_evidence
            )
        )
    qg = r.get("quality_gap")
    note = "" if qg is not None else f" _({r.get('quality_status', 'not scored')})_"
    out.append(
        f"- **Thin existing content {_pct(qg)}** · "
        f"{r.get('quality_scored', 0)}/{r.get('quality_attempted', 0)} transcripts scored{note}"
    )
    out.append(
        f"- **Confidence {_pct(r.get('confidence'))}** · raw opportunity {_pct(r.get('opportunity_raw'))}"
    )
    out.append(
        f"- **Query consensus {_pct(r.get('query_consensus'))}** · "
        f"coverage {_pct(r.get('query_coverage'))} across {r.get('query_samples', 1)} searches"
    )
    warnings = []
    if (r.get("confidence") or 0) < 0.65:
        warnings.append("low evidence coverage")
    if (r.get("relevance_gate") or 0) < 1.0:
        warnings.append("thin relevance match")
    if (r.get("credible_density") or 0) > 0.8 and (r.get("recent_credible_results") or 0) >= 5:
        warnings.append("dense recent supply")
    if (r.get("unknown_subscriber_results") or 0) > 0:
        warnings.append(f"{r.get('unknown_subscriber_results')} unknown subscriber counts")
    if (r.get("query_coverage") or 1.0) < 0.67:
        warnings.append("weak multi-query coverage")
    if (r.get("query_consensus") or 1.0) < 0.7:
        warnings.append("query samples disagree")
    if (r.get("top3_channel_share") or 0.0) >= 0.7:
        warnings.append("top channels dominate results")
    if warnings:
        out.append("- _Watch-outs:_ " + "; ".join(warnings))
    examples = r.get("comment_examples") or []
    if examples:
        out.append("- _Viewers ask:_ " + "; ".join(f'"{e[:90]}"' for e in examples[:2]))
    rising_terms = r.get("trend_rising_terms")
    if rising_terms:
        out.append("- _Rising YouTube searches:_ " + "; ".join(str(rising_terms).split("; ")[:5]))
    if r.get("external_metric_topic"):
        out.append(
            f"- _External metric match:_ {r.get('external_metric_topic')} · "
            f"monthly searches {_num(r.get('external_monthly_searches'))} · "
            f"CPM/RPM {_num(r.get('external_cpm'))}"
        )
    out.append("")
    return out
