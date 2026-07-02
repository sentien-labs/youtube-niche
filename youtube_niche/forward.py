"""Forward-test snapshots for scored niche opportunities.

Retrospective backtests are useful, but the cleanest validation is prospective:
save today's ranked opportunities, then check 30/60/90 days later whether small-channel
breakouts appeared in those exact niches.

Ledger hardening (2026-06-30 incident): the LLM backend went silently empty for a whole day,
`discover_niches` degraded to keyword n-grams without anyone noticing, and the forward snapshot
for that run wrote a single thin niche ("dave ramsey") straight into the live ledger — and an
earlier --no-llm run had separately written a junk title-fragment label ("will make"). Three
guards now sit in front of every write to `out/forward-snapshots.csv`:
  1. BACKUP  — `out/backups/forward-snapshots-<stamp>.csv` before any append/rewrite (pruned to 30).
  2. PROVENANCE — an `extraction_method` column records how each row's topic was produced
     ("llm" | "keyword" | "" for legacy rows), migrated in place on first write with new code.
  3. JUNK GATE — `_acceptable_label` rejects thin/fragment labels, and a "keyword_degraded"
     extraction method (LLM enabled but every provider failed) skips snapshotting entirely.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import shutil
import sys
from pathlib import Path


# NOTE: `extraction_method` was added after the ledger already had ~177 live rows under the
# fields below it. `append_snapshot_rows` migrates an old-header file in place (see
# `_migrate_legacy_header`) — always AFTER taking a backup — so this list can just describe the
# current/target schema; old files converge to it on their next write.
SNAPSHOT_FIELDS = [
    "snapshot_id", "created_at", "due_at", "horizon_days", "label", "source",
    "rank", "topic", "opportunity", "opportunity_raw", "confidence", "demand",
    "supply_gap", "cpm_score", "relevance_gate", "query_samples", "status",
    "checked_at", "breakout_count", "notes", "extraction_method",
]

# Snapshotting is gated on this method string (set by youtube_niche.winners.discover_niches):
# an LLM-enabled run whose extraction chain came back completely empty must not pollute the
# forward-test ledger with generic keyword-n-gram labels.
DEGRADED_METHOD = "keyword_degraded"

BACKUP_DIR_NAME = "backups"
MAX_BACKUPS = 30

# Labels rejected outright: title fragments / auxiliary-verb filler that slipped through when
# --no-llm keyword extraction (or a degraded run) grabbed a raw n-gram like "will make".
_FILLER_TOKENS = {
    "will", "make", "makes", "made", "get", "gets", "got", "how", "what", "why", "your",
    "this", "that", "best", "top", "new", "the", "a", "an", "and", "or", "to", "of", "in",
    "for", "with", "you", "my", "me", "we", "is", "are", "do", "does", "did", "can", "could",
    "should", "would", "just", "now", "here", "there", "it", "its", "on", "at", "by", "be",
    "been", "being", "was", "were",
}


def _backup_ledger(path: Path) -> Path | None:
    """Copy the current ledger to out/backups/<name>-<stamp>.csv before any append/rewrite, then
    prune to the newest MAX_BACKUPS. No-op (returns None) if the ledger doesn't exist yet — there's
    nothing to lose."""
    if not path.exists():
        return None
    backup_dir = path.parent / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{path.stem}-{stamp}{path.suffix}"
    # Same-second re-entry (e.g. two writes in one test): don't clobber a just-made backup, and
    # don't fail — just skip taking a second copy this instant.
    if not dest.exists():
        shutil.copy2(path, dest)
    _prune_backups(backup_dir, path.stem)
    return dest


def _prune_backups(backup_dir: Path, stem: str) -> None:
    backups = sorted(backup_dir.glob(f"{stem}-*"), key=lambda p: p.name)
    excess = len(backups) - MAX_BACKUPS
    for p in backups[:max(0, excess)]:
        try:
            p.unlink()
        except OSError:
            pass


def _migrate_legacy_header(path: Path) -> None:
    """If the ledger exists with a header missing `extraction_method` (pre-hardening format),
    rewrite it in the current schema with old rows getting extraction_method="". Call only AFTER
    `_backup_ledger`. No-op if the file doesn't exist or is already current."""
    if not path.exists():
        return
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "extraction_method" in fieldnames:
            return  # already current
        rows = list(reader)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in SNAPSHOT_FIELDS})


def _acceptable_label(topic: str | None) -> bool:
    """Reject thin/junk niche labels before they reach the ledger: title fragments like
    "will make" (an auxiliary-verb phrase, not a niche) must fail; real niches like
    "dave ramsey" or "dividend growth investing" must pass.

    Rules: at least 2 alphanumeric tokens, AND not every token is filler/stopword-ish.
    """
    if not topic or not isinstance(topic, str):
        return False
    tokens = re.findall(r"[a-z0-9]+", topic.lower())
    if len(tokens) < 2:
        return False
    if all(t in _FILLER_TOKENS for t in tokens):
        return False
    return True


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


def rows_from_scores(
    results: list[dict], label: str, source: str, horizons: list[int],
    extraction_method: str = "",
) -> list[dict]:
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
                "extraction_method": extraction_method or "",
            })
    return out


def append_snapshot_rows(path: Path, rows: list[dict]) -> None:
    """Append rows to the ledger, hardened: backup the current file first (if any), migrate a
    legacy (pre-extraction_method) header in place, THEN append. Order matters — the backup must
    capture the file exactly as it was before either the migration or the append touches it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_ledger(path)
    _migrate_legacy_header(path)
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
    extraction_method: str = "",
) -> tuple[Path, int]:
    """Build and append forward-test snapshot rows, gated against the 2026-06-30 incident:
      - extraction_method == DEGRADED_METHOD ("keyword_degraded"): the LLM was enabled but every
        provider came back empty, so the caller already fell back to generic keyword n-grams.
        Skip the ledger write entirely — a degraded run must not pollute forward-test data.
      - Otherwise, individual rows whose topic fails `_acceptable_label` (e.g. a stray title
        fragment like "will make") are dropped with a printed notice; the rest are written with
        their extraction_method recorded ("llm" | "keyword" | whatever the caller passed).
    """
    p = snapshot_path(out_dir, path)
    if extraction_method == DEGRADED_METHOD:
        print(
            "⚠️  Forward snapshot SKIPPED: extraction method is 'keyword_degraded' "
            "(LLM enabled but all providers failed) — not writing thin labels to the ledger."
        )
        return p, 0
    rows = rows_from_scores(results, label, source, horizons or [30, 60, 90], extraction_method)
    accepted, rejected = [], []
    for row in rows:
        (accepted if _acceptable_label(row.get("topic")) else rejected).append(row)
    if rejected:
        bad_topics = sorted({str(r.get("topic")) for r in rejected})
        print(f"  [forward] skipping {len(rejected)} snapshot row(s) with junk label(s): {bad_topics}")
    if accepted:
        append_snapshot_rows(p, accepted)
    return p, len(accepted)


def capture_from_csv(
    score_csv: Path, out_dir: str, label: str | None, source: str, horizons: list[int],
    extraction_method: str = "",
) -> tuple[Path, int]:
    with score_csv.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"No rows in {score_csv}")
    label = label or score_csv.stem
    p, n = capture_score_snapshot(
        rows, out_dir, label, source=source, horizons=horizons, extraction_method=extraction_method
    )
    return p, n


def _parse_dt(s: str | None) -> dt.datetime | None:
    """Parse an ISO datetime (created_at) or a date (due_at) to an aware UTC datetime."""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        try:
            day = dt.date.fromisoformat(str(s))
        except ValueError:
            return None
        return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)


def _iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def mine_topic_breakouts(client, cfg, topic: str, since: dt.datetime, until: dt.datetime,
                         min_vpd: float, now: dt.datetime) -> list[dict]:
    """Small-channel, title-relevant breakout videos for one topic in [since, until]."""
    # Lazy imports: winners imports forward at module load, so importing winners up top would cycle.
    from .backtest import text_matches_topic
    from .enrich import enrich
    from .signals.volume import views_per_day
    from .winners import _is_english, _is_junk, _is_short, is_small_channel_at_publish, subs_at_publish_est

    try:
        res = client.search(
            topic, max_results=cfg.top_n, order="viewCount",
            published_after=_iso_z(since), published_before=_iso_z(until),
            region=cfg.region_code, relevance_language=cfg.relevance_language,
        )
    except Exception:
        return []
    try:
        records = enrich(client, res.get("items", []), cfg)
    except Exception:
        return []
    out: list[dict] = []
    for v in records:
        subs = v.get("subs")
        if v["views"] < cfg.min_view_floor or subs is None or subs <= 0:
            continue
        if not is_small_channel_at_publish(v, cfg.small_channel_subs, now):
            continue
        if _is_short(v) or _is_junk(v) or not _is_english(v):
            continue
        if not text_matches_topic(v.get("title", ""), topic):
            continue
        vpd = views_per_day(v, now)
        if vpd is None or vpd < min_vpd:
            continue
        v["_vpd"] = vpd
        v["_subs_at_publish_est"] = subs_at_publish_est(v, now)
        out.append(v)
    return out


def resolve_due_snapshots(rows: list[dict], client, cfg, now: dt.datetime | None = None,
                          min_vpd: float | None = None, max_searches: int | None = None) -> tuple[list[dict], dict]:
    """For each pending row whose due date has passed, mine breakouts in its prediction window
    [created_at, due_at] and mark it checked with a hit/miss + breakout count. One search per topic."""
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    min_vpd = cfg.winner_min_vpd if min_vpd is None else min_vpd

    due_idx = []
    for i, r in enumerate(rows):
        if (r.get("status") or "pending") != "pending":
            continue
        due = _parse_dt(r.get("due_at"))
        if due is not None and due.date() <= today:
            due_idx.append(i)

    groups: dict[str, list[int]] = {}
    for i in due_idx:
        groups.setdefault(rows[i].get("topic") or "", []).append(i)

    resolved = 0
    searches = 0
    for topic, idxs in groups.items():
        if not topic:
            continue
        if max_searches is not None and searches >= max_searches:
            break
        # cache-only reads cost no real quota, so don't let a spent daily budget block them.
        if (not getattr(cfg, "cache_only", False)
                and hasattr(client, "search_calls_remaining")
                and client.search_calls_remaining() < 1):
            break
        sinces = [d for d in (_parse_dt(rows[i].get("created_at")) for i in idxs) if d]
        since = min(sinces) if sinces else now - dt.timedelta(days=90)
        breakouts = mine_topic_breakouts(client, cfg, topic, since, now, min_vpd, now)
        searches += 1
        for i in idxs:
            r = rows[i]
            c_dt = _parse_dt(r.get("created_at")) or since
            d_dt = _parse_dt(r.get("due_at"))
            upper = (d_dt + dt.timedelta(days=1)) if d_dt else now  # due date inclusive
            window = [
                b for b in breakouts
                if (pub := _parse_dt(b.get("published_at"))) is not None and c_dt <= pub < upper
            ]
            window.sort(key=lambda b: b.get("_vpd", 0), reverse=True)
            r["status"] = "checked"
            r["checked_at"] = today.isoformat()
            r["breakout_count"] = len(window)
            r["notes"] = (
                "hit: " + " | ".join(b.get("title", "")[:60] for b in window[:2])
                if window else "miss"
            )
            resolved += 1
    return rows, {"due": len(due_idx), "resolved": resolved, "searches": searches}


def rewrite_snapshot_rows(path: Path, rows: list[dict]) -> None:
    """Full-file rewrite (used by `resolve`). Backed up first, like `append_snapshot_rows` —
    this is the same ledger file and the same incident risk (a bad rewrite is just as
    destructive as a bad append)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_ledger(path)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in SNAPSHOT_FIELDS})


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

    res = sub.add_parser("resolve", help="check due snapshots against actual breakouts; mark hit/miss")
    res.add_argument("--out-dir", default="out")
    res.add_argument("--snapshot-path", default=None)
    res.add_argument("--min-vpd", type=float, default=None, help="breakout views/day threshold")
    res.add_argument("--max-searches", type=int, default=None, help="cap searches this run (quota guard)")
    res.add_argument("--cache-only", action="store_true", help="only use cached YouTube responses")
    res.add_argument("--region-code", default=None)
    res.add_argument("--relevance-language", default=None)
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
    if args.cmd == "resolve":
        return _resolve_main(args)
    return 2


def _resolve_main(args) -> int:
    # Lazy imports: cli imports forward at module load, so importing cli up top would cycle.
    from .cache import Cache
    from .cli import _select_auth
    from .config import Config
    from .youtube_client import YouTubeClient

    cfg = Config.from_env(
        out_dir=args.out_dir,
        region_code=args.region_code,
        relevance_language=args.relevance_language,
        cache_only=args.cache_only or None,
    )
    path = snapshot_path(cfg.out_dir, args.snapshot_path)
    if not path.exists():
        print(f"No snapshot registry found: {path}", file=sys.stderr)
        return 1
    with path.open() as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"No snapshot rows in {path}")
        return 0

    auth = _select_auth(cfg, allow_missing=cfg.cache_only)
    if auth is None and not cfg.cache_only:
        return 2
    client = YouTubeClient(
        auth, Cache(cfg.cache_path), daily_quota=cfg.daily_quota_units,
        reserve=cfg.quota_reserve, daily_search_limit=cfg.daily_search_limit,
        cache_only=cfg.cache_only,
    )
    rows, summary = resolve_due_snapshots(
        rows, client, cfg, min_vpd=args.min_vpd, max_searches=args.max_searches
    )
    rewrite_snapshot_rows(path, rows)
    hits = sum(
        1 for r in rows
        if r.get("status") == "checked" and str(r.get("breakout_count") or "0") not in ("", "0")
    )
    print(f"Resolved {summary['resolved']}/{summary['due']} due rows "
          f"in {summary['searches']} searches -> {path}")
    print(f"Checked rows with a breakout hit: {hits}")
    print(f"Quota today: {client.units_spent()} units, {client.search_calls_used()} searches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
