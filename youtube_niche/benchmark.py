"""Multi-window benchmark runner for retrospective validation.

The single-window backtest is useful, but easy to overread. This wrapper runs the same backtest
across several holdout windows and writes an aggregate so ranking changes can be judged against
repeatable temporal evidence.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

from .backtest import aggregate_registry, main as backtest_main, registry_path
from .config import Config
from .domains import DOMAINS


MANIFEST_FIELDS = [
    "run_index",
    "domain",
    "cutoff",
    "holdout_days",
    "candidate_source",
    "return_code",
    "status",
]


def _parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _default_latest_cutoff(now: dt.datetime, holdout_days: int) -> dt.date:
    return (now - dt.timedelta(days=holdout_days)).date()


def window_cutoffs(
    windows: int,
    holdout_days: int,
    *,
    latest_cutoff: str | None = None,
    step_days: int | None = None,
    now: dt.datetime | None = None,
) -> list[str]:
    """Return YYYY-MM-DD holdout starts, newest first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    start = _parse_date(latest_cutoff) if latest_cutoff else _default_latest_cutoff(now, holdout_days)
    step = max(1, step_days or holdout_days)
    return [
        (start - dt.timedelta(days=step * i)).isoformat()
        for i in range(max(1, windows))
    ]


def _domain_names(args) -> list[str]:
    if args.fixtures:
        return ["(fixtures)"]
    if args.all_domains:
        return [d.name for d in DOMAINS]
    return list(args.domain or [])


def _add_option(argv: list[str], name: str, value) -> None:
    if value is not None:
        argv.extend([name, str(value)])


def _add_flag(argv: list[str], name: str, enabled: bool) -> None:
    if enabled:
        argv.append(name)


def _backtest_args(args, domain_name: str, cutoff: str, out_dir: str, reg: str | None) -> list[str]:
    argv: list[str] = []
    if args.fixtures:
        argv.append("--fixtures")
    else:
        argv.extend(["--domain", domain_name])
    argv.extend([
        "--candidate-source", args.candidate_source,
        "--cutoff", cutoff,
        "--holdout-days", str(args.holdout_days),
        "--out-dir", out_dir,
    ])
    _add_option(argv, "--registry", reg)
    _add_option(argv, "--max-candidates", args.max_candidates)
    _add_option(argv, "--max-labels", args.max_labels)
    _add_option(argv, "--top-k", args.top_k)
    _add_option(argv, "--min-vpd", args.min_vpd)
    _add_option(argv, "--max-per-term", args.max_per_term)
    _add_option(argv, "--max-probe-terms", args.max_probe_terms)
    _add_option(argv, "--query-samples", args.query_samples)
    _add_option(argv, "--top-n", args.top_n)
    _add_option(argv, "--seed-window-days", args.seed_window_days)
    _add_option(argv, "--seed-gap-days", args.seed_gap_days)
    _add_option(argv, "--llm-provider", args.llm_provider)
    _add_option(argv, "--region-code", args.region_code)
    _add_option(argv, "--relevance-language", args.relevance_language)
    _add_flag(argv, "--no-probe-autocomplete", args.no_probe_autocomplete)
    _add_flag(argv, "--with-llm", args.with_llm)
    _add_flag(argv, "--with-trends", args.with_trends)
    _add_flag(argv, "--with-comments", args.with_comments)
    _add_flag(argv, "--cache-only", args.cache_only)
    return argv


def write_manifest(rows: list[dict], out_dir: str, reg: Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    csv_path = out / f"benchmark-{stamp}.csv"
    md_path = out / f"benchmark-{stamp}.md"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerows(rows)

    ok = sum(1 for r in rows if int(r["return_code"]) == 0)
    lines = [
        "# Multi-Window Benchmark",
        "",
        f"_Generated {stamp}. Successful windows: {ok}/{len(rows)}. Registry: `{reg}`._",
        "",
        "| # | Domain | Cutoff | Holdout days | Source | RC | Status |",
        "|---:|---|---:|---:|---|---:|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['run_index']} | {r['domain']} | {r['cutoff']} | {r['holdout_days']} | "
            f"{r['candidate_source']} | {r['return_code']} | {r['status']} |"
        )
    md_path.write_text("\n".join(lines))
    return csv_path, md_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.benchmark",
        description="Run retrospective backtests over multiple holdout windows/domains.",
    )
    p.add_argument("--fixtures", action="store_true",
                   help="run against built-in keyless fixture data (no API key/quota)")
    p.add_argument("--domain", action="append", default=None,
                   help="domain to benchmark; can be repeated")
    p.add_argument("--all-domains", action="store_true", help="benchmark every configured domain")
    p.add_argument(
        "--candidate-source",
        choices=["subtopics", "labels", "both", "effective", "hybrid", "expanded", "temporal"],
        default="temporal",
    )
    p.add_argument("--windows", type=int, default=4, help="number of holdout windows (default 4)")
    p.add_argument("--holdout-days", type=int, default=90)
    p.add_argument("--window-step-days", type=int, default=None,
                   help="days between holdout starts (default: holdout-days)")
    p.add_argument("--latest-cutoff", default=None,
                   help="newest holdout start date YYYY-MM-DD (default: today - holdout-days)")
    p.add_argument("--registry", default=None, help="registry CSV path (default: out/backtest-runs.csv)")
    p.add_argument("--max-candidates", type=int, default=25)
    p.add_argument("--max-labels", type=int, default=12)
    p.add_argument("--top-k", default="5,10")
    p.add_argument("--min-vpd", type=float, default=None)
    p.add_argument("--max-per-term", type=int, default=None)
    p.add_argument("--max-probe-terms", type=int, default=15)
    p.add_argument("--no-probe-autocomplete", action="store_true")
    p.add_argument("--query-samples", type=int, default=None)
    p.add_argument("--top-n", type=int, default=None)
    p.add_argument("--seed-window-days", type=int, default=180)
    p.add_argument("--seed-gap-days", type=int, default=0)
    p.add_argument("--with-llm", action="store_true")
    p.add_argument("--with-trends", action="store_true")
    p.add_argument("--with-comments", action="store_true")
    p.add_argument("--calibrate-weights", action="store_true",
                   help="write validation-calibrated weight suggestions from successful windows")
    p.add_argument("--llm-provider", default=None)
    p.add_argument("--cache-only", action="store_true")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--region-code", default=None)
    p.add_argument("--relevance-language", default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    domains = _domain_names(args)
    if not domains:
        print("ERROR: provide --fixtures, --domain, or --all-domains.", file=sys.stderr)
        return 2

    cfg = Config.from_env(out_dir=args.out_dir)
    reg = registry_path(cfg.out_dir, args.registry)
    cutoffs = window_cutoffs(
        args.windows,
        args.holdout_days,
        latest_cutoff=args.latest_cutoff,
        step_days=args.window_step_days,
    )

    rows: list[dict] = []
    run_index = 0
    for domain_name in domains:
        for cutoff in cutoffs:
            run_index += 1
            label = "fixtures" if args.fixtures else domain_name
            print(f"\n[{run_index}/{len(domains) * len(cutoffs)}] {label} cutoff={cutoff}")
            rc = backtest_main(_backtest_args(args, domain_name, cutoff, cfg.out_dir, str(reg)))
            rows.append({
                "run_index": run_index,
                "domain": label,
                "cutoff": cutoff,
                "holdout_days": args.holdout_days,
                "candidate_source": args.candidate_source,
                "return_code": rc,
                "status": "ok" if rc == 0 else "failed",
            })

    manifest_csv, manifest_md = write_manifest(rows, cfg.out_dir, reg)
    print(f"\nWrote benchmark manifest:\n  {manifest_csv}\n  {manifest_md}")
    ok = sum(1 for r in rows if int(r["return_code"]) == 0)
    if ok:
        try:
            agg_csv, agg_md = aggregate_registry(reg, cfg.out_dir)
            print(f"Wrote aggregate:\n  {agg_csv}\n  {agg_md}")
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
        if args.calibrate_weights:
            from .weighting import load_backtest_rows, paths_from_registry, write_weight_report

            source_paths = paths_from_registry(reg)
            rows_for_weights = load_backtest_rows(source_paths)
            if rows_for_weights:
                weight_csv, weight_md, best = write_weight_report(rows_for_weights, cfg.out_dir, source_paths)
                auc = best.get("auc")
                auc_label = "n/a" if auc is None else f"{auc:.3f}"
                print(f"Wrote weight calibration (best AUC {auc_label}):\n  {weight_csv}\n  {weight_md}")
            else:
                print("No usable backtest rows found for weight calibration.", file=sys.stderr)
    else:
        print("No successful windows; aggregate not written.", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
