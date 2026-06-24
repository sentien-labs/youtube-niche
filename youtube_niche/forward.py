"""Forward-test snapshots for scored niche opportunities.

Retrospective backtests are useful, but the cleanest validation is prospective:
save today's ranked opportunities, then check 30/60/90 days later whether small-channel
breakouts appeared in those exact niches.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path


SNAPSHOT_FIELDS = [
    "snapshot_id", "created_at", "due_at", "horizon_days", "label", "source",
    "rank", "topic", "opportunity", "opportunity_raw", "confidence", "demand",
    "supply_gap", "cpm_score", "relevance_gate", "query_samples", "status",
    "checked_at", "breakout_count", "notes",
]


def _snapshot_id(label: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")[:32] or "snapshot"
    return f"{stamp}-{slug}"


def snapshot_path(out_dir: str, explicit: str | None = None) -> Path:
    return Path(explicit) if explicit else Path(out_dir) / "forward-snapshots.csv"


def parse_horizons(s: str) -> list[int]:
    vals = []
    for part in s.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            vals.append(int(part))
    return vals or [30, 60, 90]


def _fmt(x):
    if x is None:
        return ""
    if isinstance(x, float):
        return round(x, 4)
    return x


def rows_from_scores(results: list[dict], label: str, source: str, horizons: list[int]) -> list[dict]:
    created = dt.datetime.now(dt.timezone.utc)
    sid = _snapshot_id(label)
    out = []
    for rank, row in enumerate(results, 1):
        for horizon in horizons:
            due = created + dt.timedelta(days=horizon)
            out.append({
                "snapshot_id": sid,
                "created_at": created.isoformat(),
                "due_at": due.date().isoformat(),
                "horizon_days": horizon,
                "label": label,
                "source": source,
                "rank": row.get("rank") or rank,
                "topic": row.get("topic"),
                "opportunity": _fmt(row.get("opportunity")),
                "opportunity_raw": _fmt(row.get("opportunity_raw")),
                "confidence": _fmt(row.get("confidence")),
                "demand": _fmt(row.get("demand")),
                "supply_gap": _fmt(row.get("supply_gap")),
                "cpm_score": _fmt(row.get("cpm_score")),
                "relevance_gate": _fmt(row.get("relevance_gate")),
                "query_samples": row.get("query_samples"),
                "status": "pending",
                "checked_at": "",
                "breakout_count": "",
                "notes": "",
            })
    return out


def append_snapshot_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        if not exists:
            w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in SNAPSHOT_FIELDS})


def capture_score_snapshot(
    results: list[dict],
    out_dir: str,
    label: str,
    source: str = "score",
    horizons: list[int] | None = None,
    path: str | None = None,
) -> tuple[Path, int]:
    rows = rows_from_scores(results, label, source, horizons or [30, 60, 90])
    p = snapshot_path(out_dir, path)
    append_snapshot_rows(p, rows)
    return p, len(rows)


def capture_from_csv(score_csv: Path, out_dir: str, label: str | None, source: str, horizons: list[int]) -> tuple[Path, int]:
    with score_csv.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"No rows in {score_csv}")
    label = label or score_csv.stem
    p, n = capture_score_snapshot(rows, out_dir, label, source=source, horizons=horizons)
    return p, n


def summarize_snapshots(path: Path, out_dir: str) -> tuple[Path, Path]:
    if not path.exists():
        raise FileNotFoundError(f"No snapshot registry found: {path}")
    with path.open() as f:
        rows = list(csv.DictReader(f))
    today = dt.date.today()
    summary: dict[str, dict] = {}
    for row in rows:
        label = row.get("label") or "unknown"
        item = summary.setdefault(label, {"label": label, "rows": 0, "pending": 0, "due": 0, "checked": 0})
        item["rows"] += 1
        status = row.get("status") or "pending"
        if status == "pending":
            item["pending"] += 1
        if status == "checked":
            item["checked"] += 1
        try:
            due = dt.date.fromisoformat(row.get("due_at") or "")
            if status == "pending" and due <= today:
                item["due"] += 1
        except ValueError:
            pass

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out / f"forward-snapshot-summary-{stamp}.csv"
    md_path = out / f"forward-snapshot-summary-{stamp}.md"
    fields = ["label", "rows", "pending", "due", "checked"]
    summary_rows = list(summary.values())
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(summary_rows)
    lines = [
        "# Forward Snapshot Summary",
        "",
        f"_Generated {stamp} from `{path}`._",
        "",
        "| Label | Rows | Pending | Due | Checked |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        lines.append(
            f"| {row['label']} | {row['rows']} | {row['pending']} | {row['due']} | {row['checked']} |"
        )
    md_path.write_text("\n".join(lines))
    return csv_path, md_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.forward",
        description="Capture and summarize forward-test score snapshots.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture", help="capture a scored CSV as pending forward-test snapshots")
    cap.add_argument("score_csv", help="CSV produced by youtube_niche scoring")
    cap.add_argument("--label", default=None)
    cap.add_argument("--source", default="manual")
    cap.add_argument("--horizons", default="30,60,90")
    cap.add_argument("--out-dir", default="out")
    cap.add_argument("--snapshot-path", default=None)

    summ = sub.add_parser("summary", help="summarize pending/due snapshot rows")
    summ.add_argument("--out-dir", default="out")
    summ.add_argument("--snapshot-path", default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "capture":
        try:
            path, n = capture_from_csv(
                Path(args.score_csv),
                args.out_dir,
                args.label,
                args.source,
                parse_horizons(args.horizons),
            )
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"Captured {n} snapshot rows -> {path}")
        return 0
    if args.cmd == "summary":
        path = snapshot_path(args.out_dir, args.snapshot_path)
        try:
            csv_path, md_path = summarize_snapshots(path, args.out_dir)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"Wrote:\n  {csv_path}\n  {md_path}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
