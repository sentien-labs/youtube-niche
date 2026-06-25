"""Community validation: pool resolved forward-test snapshots and measure score-vs-reality.

The tool's scarcest resource is ground truth — did a high opportunity score actually predict a
small-channel breakout? Every contributor who runs `python -m youtube_niche.forward resolve`
produces exactly that evidence. This module pools resolved snapshot rows (your local
`out/forward-snapshots.csv` plus any CSVs contributors drop under `community/`), validates the
schema, and builds a **calibration curve**: for each opportunity-score band, what fraction of
niches actually saw a breakout. The headline number is AUC — the probability that a niche that
broke out was scored higher than one that didn't (0.5 = the score is noise; 1.0 = perfect).

  python -m youtube_niche.community calibrate            # build the curve from pooled snapshots
  python -m youtube_niche.community validate <csv...>    # check a contribution before submitting
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

# A contributed row must carry at least these; they are a subset of forward.SNAPSHOT_FIELDS.
REQUIRED_FIELDS = ("topic", "opportunity", "status", "breakout_count")
DEFAULT_BAND_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0001)


def _to_float(x):
    try:
        if x in (None, "", "none"):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_int(x):
    f = _to_float(x)
    return None if f is None else int(f)


def validate_rows(rows: list[dict]) -> list[str]:
    """Return a list of human-readable problems. Empty list == a clean, submittable file."""
    issues: list[str] = []
    if not rows:
        return ["file has no data rows"]
    header = set(rows[0].keys())
    missing = [f for f in REQUIRED_FIELDS if f not in header]
    if missing:
        issues.append(f"missing required column(s): {', '.join(missing)}")
        return issues  # nothing else is checkable without the columns
    checked = 0
    for i, r in enumerate(rows, 1):
        status = (r.get("status") or "").strip()
        if status not in ("pending", "checked"):
            issues.append(f"row {i}: status must be 'pending' or 'checked' (got {status!r})")
        if status != "checked":
            continue
        checked += 1
        opp = _to_float(r.get("opportunity"))
        if opp is None or not (0.0 <= opp <= 1.0):
            issues.append(f"row {i}: checked row needs opportunity in [0,1] (got {r.get('opportunity')!r})")
        bc = _to_int(r.get("breakout_count"))
        if bc is None or bc < 0:
            issues.append(f"row {i}: checked row needs breakout_count >= 0 (got {r.get('breakout_count')!r})")
    if checked == 0:
        issues.append("no resolved (status=checked) rows — run `forward resolve` before submitting")
    return issues


def load_rows(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            continue
        with p.open() as f:
            for r in csv.DictReader(f):
                r["_source_file"] = p.name
                rows.append(r)
    return rows


def gather_paths(snapshot_path: Path | None, community_dir: Path | None) -> list[Path]:
    paths: list[Path] = []
    if snapshot_path and snapshot_path.exists():
        paths.append(snapshot_path)
    if community_dir and community_dir.exists():
        paths.extend(sorted(community_dir.glob("*.csv")))
    return paths


def _resolved(rows: list[dict]) -> list[tuple[float, bool, int]]:
    """(opportunity, hit, breakout_count) for every checked row with usable numbers."""
    out = []
    for r in rows:
        if (r.get("status") or "").strip() != "checked":
            continue
        opp = _to_float(r.get("opportunity"))
        bc = _to_int(r.get("breakout_count"))
        if opp is None or bc is None:
            continue
        out.append((opp, bc > 0, bc))
    return out


def auc(samples: list[tuple[float, bool, int]]) -> float | None:
    """Probability a hit is scored above a miss (Mann–Whitney U / n_pos·n_neg). None if degenerate."""
    pos = [s for s, hit, _ in samples if hit]
    neg = [s for s, hit, _ in samples if not hit]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def calibration_curve(rows: list[dict], edges: tuple[float, ...] = DEFAULT_BAND_EDGES) -> tuple[list[dict], dict]:
    samples = _resolved(rows)
    bands: list[dict] = []
    for lo, hi in zip(edges, edges[1:]):
        in_band = [s for s in samples if lo <= s[0] < hi]
        n = len(in_band)
        hits = sum(1 for _, hit, _ in in_band if hit)
        bands.append({
            "band": f"{lo:.1f}-{min(hi, 1.0):.1f}",
            "n": n,
            "hits": hits,
            "hit_rate": (hits / n) if n else None,
            "avg_opportunity": (sum(s[0] for s in in_band) / n) if n else None,
        })
    n = len(samples)
    hits = sum(1 for _, hit, _ in samples if hit)
    # lift = how much more often the top half breaks out vs the bottom half
    ordered = sorted(samples, key=lambda s: s[0])
    half = n // 2
    bottom, top = ordered[:half], ordered[n - half:]
    def _rate(xs):
        return (sum(1 for _, hit, _ in xs if hit) / len(xs)) if xs else None
    overall = {
        "resolved_rows": n,
        "hits": hits,
        "hit_rate": (hits / n) if n else None,
        "auc": auc(samples),
        "top_half_hit_rate": _rate(top),
        "bottom_half_hit_rate": _rate(bottom),
        "monotonic": _is_monotonic([b["hit_rate"] for b in bands]),
    }
    return bands, overall


def _is_monotonic(rates: list[float | None]) -> bool:
    present = [r for r in rates if r is not None]
    return all(a <= b + 1e-9 for a, b in zip(present, present[1:]))


def write_calibration_report(bands: list[dict], overall: dict, out_dir: str, sources: list[Path]) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out / f"calibration-{stamp}.csv"
    md_path = out / f"calibration-{stamp}.md"
    fields = ["band", "n", "hits", "hit_rate", "avg_opportunity"]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for b in bands:
            w.writerow({k: (round(b[k], 4) if isinstance(b.get(k), float) else b.get(k)) for k in fields})

    def pct(x):
        return "—" if x is None else f"{x * 100:.0f}%"

    auc_v = overall["auc"]
    auc_line = "—" if auc_v is None else f"{auc_v:.2f}"
    verdict = (
        "not enough resolved data yet" if auc_v is None
        else "the score meaningfully predicts breakouts" if auc_v >= 0.65
        else "the score weakly predicts breakouts" if auc_v >= 0.55
        else "the score is close to noise — needs work"
    )
    lines = [
        "# Score-vs-Reality Calibration",
        "",
        f"_Generated {stamp} from {len(sources)} source file(s): "
        f"{', '.join(s.name for s in sources) or '(none)'}._",
        "",
        f"**AUC = {auc_line}** ({verdict}). "
        f"Resolved niches: {overall['resolved_rows']}, of which {overall['hits']} saw a breakout "
        f"(overall hit rate {pct(overall['hit_rate'])}).",
        "",
        f"Top-half vs bottom-half hit rate: **{pct(overall['top_half_hit_rate'])}** vs "
        f"**{pct(overall['bottom_half_hit_rate'])}**. "
        f"Hit rate rises monotonically across bands: {'yes' if overall['monotonic'] else 'no'}.",
        "",
        "| Opportunity band | n | breakouts | hit rate | avg score |",
        "|---|---:|---:|---:|---:|",
    ]
    for b in bands:
        avg = b["avg_opportunity"]
        avg_str = "—" if avg is None else f"{avg:.2f}"
        lines.append(
            f"| {b['band']} | {b['n']} | {b['hits']} | {pct(b['hit_rate'])} | {avg_str} |"
        )
    lines += [
        "",
        "AUC is the probability a niche that broke out was scored above one that didn't "
        "(0.50 = noise, 1.00 = perfect). This is the honest, leakage-free measure of whether the "
        "tool works — it grows more trustworthy as more contributors submit resolved snapshots.",
    ]
    md_path.write_text("\n".join(lines))
    return csv_path, md_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.community",
        description="Pool resolved forward-test snapshots into a score-vs-reality calibration curve.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    cal = sub.add_parser("calibrate", help="build the calibration curve from pooled resolved snapshots")
    cal.add_argument("--snapshot-path", default=None, help="local snapshot CSV (default out/forward-snapshots.csv)")
    cal.add_argument("--community-dir", default="community", help="dir of contributed resolved CSVs (default community/)")
    cal.add_argument("--out-dir", default="out")

    val = sub.add_parser("validate", help="check contributed CSV(s) before submitting")
    val.add_argument("csv", nargs="+", help="one or more resolved-snapshot CSV files")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "validate":
        bad = 0
        for path in args.csv:
            p = Path(path)
            if not p.exists():
                print(f"{path}: ERROR file not found")
                bad += 1
                continue
            with p.open() as f:
                rows = list(csv.DictReader(f))
            issues = validate_rows(rows)
            if issues:
                bad += 1
                print(f"{path}: {len(issues)} issue(s)")
                for it in issues:
                    print(f"  - {it}")
            else:
                checked = sum(1 for r in rows if (r.get("status") or "") == "checked")
                print(f"{path}: OK ({len(rows)} rows, {checked} resolved)")
        return 1 if bad else 0

    if args.cmd == "calibrate":
        from .forward import snapshot_path
        snap = snapshot_path(args.out_dir, args.snapshot_path)
        community_dir = Path(args.community_dir) if args.community_dir else None
        sources = gather_paths(snap, community_dir)
        if not sources:
            print("No snapshot sources found. Run scoring with --snapshot, then `forward resolve`, "
                  "or add contributed CSVs under community/.", file=sys.stderr)
            return 1
        rows = load_rows(sources)
        bands, overall = calibration_curve(rows)
        csv_path, md_path = write_calibration_report(bands, overall, args.out_dir, sources)
        print(f"Pooled {overall['resolved_rows']} resolved niches from {len(sources)} file(s).")
        print(f"AUC: {overall['auc'] if overall['auc'] is not None else '— (need both hits and misses)'}")
        print(f"Wrote:\n  {csv_path}\n  {md_path}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
