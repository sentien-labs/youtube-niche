"""Validation-calibrated scoring weight suggestions.

This module does not mutate defaults automatically. It reads resolved backtest candidate rows,
searches a small explainable grid of top-level opportunity weights, and writes the mix that best
separates later breakout hits from misses.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import sys
from pathlib import Path

from .backtest import registry_path
from .config import Config
from .util import clamp01


WEIGHT_FIELDS = ("demand", "supply_gap", "monetization", "quality_gap")
REPORT_FIELDS = [
    "rank",
    "label",
    "auc",
    "rows",
    "hits",
    "top_quartile_hit_rate",
    "bottom_quartile_hit_rate",
    "hit_lift",
    "mean_hit_score",
    "mean_miss_score",
    "demand_weight",
    "supply_gap_weight",
    "monetization_weight",
    "quality_gap_weight",
]


def _to_float(x) -> float | None:
    try:
        if x in (None, "", "none"):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _is_hit(row: dict) -> bool:
    val = str(row.get("backtest_hit") or row.get("hit") or "").strip().lower()
    if val in {"1", "true", "yes", "hit"}:
        return True
    if val in {"0", "false", "no", "miss"}:
        return False
    hit_count = _to_float(row.get("hit_count"))
    return bool(hit_count and hit_count > 0)


def _geomean(parts: list[tuple[float | None, float]]) -> float | None:
    present = [(max(v, 1e-4), w) for v, w in parts if v is not None and w > 0]
    total = sum(w for _, w in present)
    if total <= 0:
        return None
    return clamp01(math.exp(sum(w * math.log(v) for v, w in present) / total))


def calibrated_score(row: dict, weights: dict[str, float]) -> float | None:
    base = _geomean([
        (_to_float(row.get("demand")), weights.get("demand", 0.0)),
        (_to_float(row.get("supply_gap")), weights.get("supply_gap", 0.0)),
        (_to_float(row.get("cpm_score")), weights.get("monetization", 0.0)),
        (_to_float(row.get("quality_gap")), weights.get("quality_gap", 0.0)),
    ])
    if base is None:
        return None
    confidence = _to_float(row.get("confidence"))
    demand_gate = _to_float(row.get("demand_gate"))
    return clamp01(base * (confidence if confidence is not None else 1.0) * (demand_gate if demand_gate is not None else 1.0))


def auc(samples: list[tuple[float, bool]]) -> float | None:
    pos = [s for s, hit in samples if hit]
    neg = [s for s, hit in samples if not hit]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def _rate(samples: list[tuple[float, bool]]) -> float | None:
    return None if not samples else sum(1 for _, hit in samples if hit) / len(samples)


def evaluate_scores(rows: list[dict], scores: list[float | None], label: str, weights: dict[str, float]) -> dict:
    samples = [
        (score, _is_hit(row))
        for row, score in zip(rows, scores)
        if score is not None
    ]
    ordered = sorted(samples, key=lambda s: s[0])
    n = len(ordered)
    q = max(1, n // 4) if n else 0
    bottom = ordered[:q]
    top = ordered[-q:] if q else []
    top_rate = _rate(top)
    bottom_rate = _rate(bottom)
    hit_scores = [s for s, hit in samples if hit]
    miss_scores = [s for s, hit in samples if not hit]
    return {
        "label": label,
        "auc": auc(samples),
        "rows": n,
        "hits": sum(1 for _, hit in samples if hit),
        "top_quartile_hit_rate": top_rate,
        "bottom_quartile_hit_rate": bottom_rate,
        "hit_lift": None if top_rate is None or bottom_rate in (None, 0) else top_rate / bottom_rate,
        "mean_hit_score": None if not hit_scores else sum(hit_scores) / len(hit_scores),
        "mean_miss_score": None if not miss_scores else sum(miss_scores) / len(miss_scores),
        "weights": weights,
    }


def has_quality(rows: list[dict]) -> bool:
    return any(_to_float(r.get("quality_gap")) is not None for r in rows)


def candidate_weights(rows: list[dict]) -> list[dict[str, float]]:
    q_weight = 0.15 if has_quality(rows) else 0.0
    remaining = 1.0 - q_weight
    raw_triples = set()
    for demand in (2, 3, 4, 5):
        for supply in (2, 3, 4, 5):
            for monetization in (1, 2, 3, 4):
                raw_triples.add((demand, supply, monetization))
    raw_triples.add((35, 30, 20))

    out: list[dict[str, float]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for demand, supply, monetization in sorted(raw_triples):
        total = demand + supply + monetization
        weights = {
            "demand": round(remaining * demand / total, 4),
            "supply_gap": round(remaining * supply / total, 4),
            "monetization": round(remaining * monetization / total, 4),
            "quality_gap": q_weight,
        }
        key = tuple(weights[k] for k in WEIGHT_FIELDS)
        if key not in seen:
            seen.add(key)
            out.append(weights)
    return out


def evaluate_weight_grid(rows: list[dict]) -> tuple[dict, list[dict]]:
    baseline_weights = {"demand": 0.35, "supply_gap": 0.30, "monetization": 0.20, "quality_gap": 0.15}
    baseline = evaluate_scores(
        rows,
        [_to_float(r.get("opportunity")) for r in rows],
        "current opportunity",
        baseline_weights,
    )
    evaluations = []
    for weights in candidate_weights(rows):
        scores = [calibrated_score(row, weights) for row in rows]
        evaluations.append(evaluate_scores(rows, scores, "grid", weights))
    evaluations.sort(
        key=lambda r: (
            -1 if r.get("auc") is None else r["auc"],
            -1 if r.get("top_quartile_hit_rate") is None else r["top_quartile_hit_rate"],
            r.get("mean_hit_score") or 0.0,
        ),
        reverse=True,
    )
    return baseline, evaluations


def load_backtest_rows(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open() as f:
            for row in csv.DictReader(f):
                if "backtest_hit" not in row:
                    continue
                row["_source_file"] = str(path)
                rows.append(row)
    return rows


def paths_from_registry(path: Path) -> list[Path]:
    if not path.exists():
        return []
    out: list[Path] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            p = Path(row.get("csv_path") or "")
            if p.exists():
                out.append(p)
    return out


def _fmt(x):
    if x is None:
        return ""
    if isinstance(x, float):
        return round(x, 4)
    return x


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _auc(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def write_weight_report(rows: list[dict], out_dir: str, source_paths: list[Path]) -> tuple[Path, Path, dict]:
    if not rows:
        raise ValueError("No backtest rows with backtest_hit found.")
    baseline, evaluations = evaluate_weight_grid(rows)
    best = evaluations[0]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    csv_path = out / f"weight-calibration-{stamp}.csv"
    md_path = out / f"weight-calibration-{stamp}.md"

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        w.writeheader()
        for i, row in enumerate(evaluations[:25], 1):
            weights = row["weights"]
            w.writerow({
                "rank": i,
                "label": row["label"],
                "auc": _fmt(row.get("auc")),
                "rows": row["rows"],
                "hits": row["hits"],
                "top_quartile_hit_rate": _fmt(row.get("top_quartile_hit_rate")),
                "bottom_quartile_hit_rate": _fmt(row.get("bottom_quartile_hit_rate")),
                "hit_lift": _fmt(row.get("hit_lift")),
                "mean_hit_score": _fmt(row.get("mean_hit_score")),
                "mean_miss_score": _fmt(row.get("mean_miss_score")),
                "demand_weight": weights["demand"],
                "supply_gap_weight": weights["supply_gap"],
                "monetization_weight": weights["monetization"],
                "quality_gap_weight": weights["quality_gap"],
            })

    weights = best["weights"]
    auc_delta = None
    if baseline.get("auc") is not None and best.get("auc") is not None:
        auc_delta = best["auc"] - baseline["auc"]
    lines = [
        "# Weight Calibration",
        "",
        f"_Generated {stamp} from {len(source_paths)} backtest CSV(s), {best['rows']} scored rows._",
        "",
        f"Current opportunity AUC: **{_auc(baseline.get('auc'))}**. "
        f"Best grid AUC: **{_auc(best.get('auc'))}**"
        + (f" (delta {auc_delta:+.3f})." if auc_delta is not None else "."),
        "",
        f"Best top-quartile hit rate: **{_pct(best.get('top_quartile_hit_rate'))}** "
        f"vs bottom-quartile **{_pct(best.get('bottom_quartile_hit_rate'))}**.",
        "",
        "Suggested top-level weights:",
        "",
        "```text",
        f"demand={weights['demand']}",
        f"supply_gap={weights['supply_gap']}",
        f"monetization={weights['monetization']}",
        f"quality_gap={weights['quality_gap']}",
        "```",
        "",
        "Treat this as a validation report, not an automatic config migration. Re-run after adding "
        "more windows or resolved forward snapshots; tiny samples can overfit.",
        "",
        "| Rank | AUC | Top Q hit | Bottom Q hit | Demand | Supply | Monetization | Quality |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, row in enumerate(evaluations[:10], 1):
        w = row["weights"]
        lines.append(
            f"| {i} | {_auc(row.get('auc'))} | {_pct(row.get('top_quartile_hit_rate'))} | "
            f"{_pct(row.get('bottom_quartile_hit_rate'))} | {w['demand']} | "
            f"{w['supply_gap']} | {w['monetization']} | {w['quality_gap']} |"
        )
    md_path.write_text("\n".join(lines))
    return csv_path, md_path, best


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.weighting",
        description="Suggest opportunity-score weights from backtest hit/miss rows.",
    )
    p.add_argument("csv", nargs="*", help="backtest CSV files")
    p.add_argument("--registry", default=None, help="backtest registry CSV (default: out/backtest-runs.csv)")
    p.add_argument("--out-dir", default=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env(out_dir=args.out_dir)
    source_paths = [Path(p) for p in args.csv]
    if not source_paths:
        reg = registry_path(cfg.out_dir, args.registry)
        source_paths = paths_from_registry(reg)
        if not source_paths:
            print(f"No backtest CSVs found. Pass paths or run backtests first: {reg}", file=sys.stderr)
            return 1
    rows = load_backtest_rows(source_paths)
    if not rows:
        print("No usable backtest rows found in supplied CSVs.", file=sys.stderr)
        return 1
    csv_path, md_path, best = write_weight_report(rows, cfg.out_dir, source_paths)
    print(f"Loaded {len(rows)} rows from {len(source_paths)} CSV(s).")
    print(f"Best AUC: {_auc(best.get('auc'))}")
    print(f"Wrote:\n  {csv_path}\n  {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
