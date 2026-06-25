"""Offline audit helpers for backtest miss analysis.

This module does not call YouTube. It reads existing backtest Markdown reports and compares the
holdout breakout titles against curated domain seeds and any discovered-subtopic registry. The
goal is to separate seed-source failures from scoring/matching failures before spending quota.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
from pathlib import Path

from .backtest import simple_label_from_title, text_matches_topic
from .candidates import domain_seed_candidates
from .domains import DOMAINS
from .subtopics import load_registry

DOMAIN_RE = re.compile(r"^# Backtest - (.+)$")
DOMAIN_RE_MD = re.compile(r"^# Backtest — (.+)$")
GENERATED_RE = re.compile(r"Holdout window: ([0-9]{4}-[0-9]{2}-[0-9]{2}) to ([^._]+)")
METRIC_RE = re.compile(r"^- \*\*(.+?)\*\*: (.*)$")
BREAKOUT_RE = re.compile(r"^- ([0-9.]+)/day · ([0-9,]+) subs · (.+)$")
CANDIDATE_RE = re.compile(r"^[0-9]+\. \*\*(.+?)\*\* .*?\((hit|miss),", re.I)


def _parse_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_datetime_date(s: str | None) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return _parse_date(s)


def _domain_for_name(name: str):
    for domain in DOMAINS:
        if domain.name.lower() == name.lower():
            return domain
    for domain in DOMAINS:
        if name.lower() in domain.name.lower() or domain.name.lower() in name.lower():
            return domain
    return None


def _matches(title: str, topics: list[str]) -> list[str]:
    return [
        topic for topic in topics
        if text_matches_topic(title, topic) or text_matches_topic(topic, title)
    ]


def _md_cell(value) -> str:
    return str(value).replace("|", "\\|") if value not in (None, "") else "-"


def parse_backtest_markdown(path: str | Path) -> dict:
    """Parse the stable parts of a backtest Markdown report."""
    p = Path(path)
    text = p.read_text()
    lines = text.splitlines()
    domain = None
    holdout_start = None
    holdout_end = None
    metrics: dict[str, str] = {}
    breakouts: list[dict] = []
    candidates: list[dict] = []

    for line in lines:
        if domain is None:
            m = DOMAIN_RE_MD.match(line) or DOMAIN_RE.match(line)
            if m and m.group(1).strip().lower() != "aggregate":
                domain = m.group(1).strip()
        if holdout_start is None:
            m = GENERATED_RE.search(line)
            if m:
                holdout_start = _parse_date(m.group(1))
                holdout_end = _parse_date(m.group(2).strip())
        if m := METRIC_RE.match(line):
            metrics[m.group(1)] = m.group(2)
        if m := BREAKOUT_RE.match(line):
            breakouts.append({
                "vpd": float(m.group(1)),
                "subs": int(m.group(2).replace(",", "")),
                "title": m.group(3).strip(),
            })
        if m := CANDIDATE_RE.match(line):
            candidates.append({"topic": m.group(1).strip(), "hit": m.group(2).lower() == "hit"})

    if not domain:
        raise ValueError(f"Not a backtest report: {p}")
    return {
        "path": str(p),
        "domain": domain,
        "holdout_start": holdout_start,
        "holdout_end": holdout_end,
        "metrics": metrics,
        "breakouts": breakouts,
        "candidates": candidates,
    }


def audit_backtest_report(
    path: str | Path,
    registry_path: str | Path | None = None,
    candidate_mode: str | None = None,
    max_candidates: int = 25,
    autocomplete_fn=None,
) -> tuple[dict, list[dict]]:
    """Return (summary, detail_rows) for one parsed backtest report."""
    report = parse_backtest_markdown(path)
    domain = _domain_for_name(report["domain"])
    curated = list(domain.subtopics) if domain else []
    registry = load_registry(registry_path)
    discovered_entry = registry.get(report["domain"], {})
    discovered = discovered_entry.get("subtopics", []) if isinstance(discovered_entry, dict) else []
    discovered = [str(s) for s in discovered if str(s).strip()]

    generated_date = _parse_datetime_date(discovered_entry.get("generated_at"))
    holdout_start = report.get("holdout_start")
    discovered_clean = bool(generated_date and holdout_start and generated_date < holdout_start)
    discovered_timing = (
        "clean-before-holdout" if discovered_clean
        else "after-or-unknown-holdout"
    )
    candidate_topics: list[str] = []
    if candidate_mode and domain:
        kwargs = {}
        if autocomplete_fn is not None:
            kwargs["autocomplete_fn"] = autocomplete_fn
        candidate_topics = [
            c.topic for c in domain_seed_candidates(
                domain,
                max_seeds=max_candidates,
                mode=candidate_mode,
                subtopics_registry=registry_path,
                **kwargs,
            )
        ]

    details = []
    curated_hits = 0
    discovered_hits = 0
    candidate_hits = 0
    for breakout in report["breakouts"]:
        title = breakout["title"]
        curated_matches = _matches(title, curated)
        discovered_matches = _matches(title, discovered)
        candidate_matches = _matches(title, candidate_topics) if candidate_topics else []
        if curated_matches:
            curated_hits += 1
        if discovered_matches:
            discovered_hits += 1
        if candidate_matches:
            candidate_hits += 1
        details.append({
            "report": report["path"],
            "domain": report["domain"],
            "breakout_title": title,
            "label": simple_label_from_title(title),
            "curated_matches": "; ".join(curated_matches),
            "discovered_matches": "; ".join(discovered_matches),
            "candidate_matches": "; ".join(candidate_matches),
            "vpd": breakout["vpd"],
            "subs": breakout["subs"],
        })

    breakout_count = len(report["breakouts"])
    positive = int(report["metrics"].get("positive candidates", "0") or 0)
    if positive == 0 and discovered_hits > curated_hits:
        assessment = "seed-source gap: discovered seeds cover more observed demand than curated seeds"
    elif positive == 0 and curated_hits > 0:
        assessment = "ranking/matching gap: curated seeds cover some breakouts but scored candidates missed"
    elif positive == 0:
        assessment = "candidate-generation gap: neither curated nor discovered seeds cover enough breakouts"
    else:
        assessment = "has candidate hits; inspect ranking and coverage"

    summary = {
        "report": report["path"],
        "domain": report["domain"],
        "holdout_start": holdout_start.isoformat() if holdout_start else "",
        "breakouts": breakout_count,
        "positive_candidates": positive,
        "curated_covered": curated_hits,
        "discovered_covered": discovered_hits,
        "curated_coverage": curated_hits / breakout_count if breakout_count else 0.0,
        "discovered_coverage": discovered_hits / breakout_count if breakout_count else 0.0,
        "candidate_mode": candidate_mode or "",
        "candidate_covered": candidate_hits if candidate_mode else "",
        "candidate_coverage": candidate_hits / breakout_count if (candidate_mode and breakout_count) else "",
        "discovered_generated_at": str(discovered_entry.get("generated_at", "")),
        "discovered_timing": discovered_timing,
        "assessment": assessment,
        "top_misses": "; ".join(c["topic"] for c in report["candidates"][:5] if not c["hit"]),
    }
    return summary, details


def audit_backtest_reports(
    paths: list[str | Path],
    registry_path: str | Path | None = None,
    candidate_mode: str | None = None,
    max_candidates: int = 25,
) -> tuple[list[dict], list[dict]]:
    summaries = []
    details = []
    for path in paths:
        try:
            summary, rows = audit_backtest_report(
                path,
                registry_path,
                candidate_mode=candidate_mode,
                max_candidates=max_candidates,
            )
        except ValueError:
            continue
        summaries.append(summary)
        details.extend(rows)
    return summaries, details


def write_failure_audit(
    paths: list[str | Path],
    out_dir: str = "out",
    registry_path: str | Path | None = None,
    candidate_mode: str | None = None,
    max_candidates: int = 25,
) -> tuple[Path, Path]:
    summaries, details = audit_backtest_reports(
        paths,
        registry_path,
        candidate_mode=candidate_mode,
        max_candidates=max_candidates,
    )
    if not summaries:
        raise ValueError("No backtest reports found to audit.")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out / f"backtest-failure-audit-{stamp}.csv"
    md_path = out / f"backtest-failure-audit-{stamp}.md"

    fields = [
        "domain", "holdout_start", "breakouts", "positive_candidates",
        "curated_covered", "discovered_covered", "curated_coverage",
        "discovered_coverage", "candidate_mode", "candidate_covered", "candidate_coverage",
        "discovered_timing", "discovered_generated_at",
        "assessment", "top_misses", "report",
    ]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in summaries:
            out_row = dict(row)
            out_row["curated_coverage"] = round(float(out_row["curated_coverage"]), 4)
            out_row["discovered_coverage"] = round(float(out_row["discovered_coverage"]), 4)
            if out_row.get("candidate_coverage") != "":
                out_row["candidate_coverage"] = round(float(out_row["candidate_coverage"]), 4)
            w.writerow({k: out_row.get(k, "") for k in fields})

    lines = [
        "# Backtest Failure Audit",
        "",
        f"_Generated {stamp}. Offline audit: no YouTube quota used._",
        "",
        "This report compares holdout breakout titles against hand-curated domain seeds and any "
        "discovered-subtopic registry. Discovered coverage is useful operationally, but only clean "
        "validation evidence when the registry predates the tested holdout window.",
        "",
        "| Domain | Breakouts | Candidate Hits | Curated Covered | Discovered Covered | Extra Mode | Extra Covered | Discovered Timing | Assessment |",
        "|---|---:|---:|---:|---:|---|---:|---|---|",
    ]
    for row in summaries:
        lines.append(
            f"| {_md_cell(row['domain'])} | {row['breakouts']} | {row['positive_candidates']} | "
            f"{row['curated_covered']} | {row['discovered_covered']} | "
            f"{_md_cell(row.get('candidate_mode'))} | {_md_cell(row.get('candidate_covered'))} | "
            f"{_md_cell(row['discovered_timing'])} | {_md_cell(row['assessment'])} |"
        )

    for row in summaries:
        lines += [
            "",
            f"## {row['domain']}",
            "",
            f"- Report: `{row['report']}`",
            f"- Holdout start: {row['holdout_start'] or '(unknown)'}",
            f"- Discovered registry generated: {row['discovered_generated_at'] or '(none)'}",
            f"- Top ranked misses: {row['top_misses'] or '(none)'}",
            "",
            "| Breakout | Label | Curated Matches | Discovered Matches | Extra Candidate Matches |",
            "|---|---|---|---|---|",
        ]
        for detail in [d for d in details if d["report"] == row["report"]]:
            lines.append(
                f"| {_md_cell(detail['breakout_title'])} | {_md_cell(detail['label'])} | "
                f"{_md_cell(detail['curated_matches'])} | {_md_cell(detail['discovered_matches'])} | "
                f"{_md_cell(detail.get('candidate_matches'))} |"
            )
    md_path.write_text("\n".join(lines) + "\n")
    return csv_path, md_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.audit",
        description="Offline audit tools for validation artifacts.",
    )
    p.add_argument(
        "--backtests",
        nargs="*",
        default=None,
        help="backtest Markdown reports to audit (default: out/backtest-*.md)",
    )
    p.add_argument("--registry", default=None, help="optional discovered-subtopics registry")
    p.add_argument(
        "--candidate-mode",
        choices=["hybrid", "expanded", "effective", "curated", "discovered"],
        default=None,
        help="also compare holdout breakout coverage against this generated candidate source",
    )
    p.add_argument("--max-candidates", type=int, default=25)
    p.add_argument("--out-dir", default="out")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    paths = [Path(p) for p in args.backtests] if args.backtests else sorted(Path("out").glob("backtest-*.md"))
    try:
        csv_path, md_path = write_failure_audit(
            paths,
            args.out_dir,
            registry_path=args.registry,
            candidate_mode=args.candidate_mode,
            max_candidates=args.max_candidates,
        )
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Wrote failure audit:\n  {csv_path}\n  {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
