"""Retention scrubber — purge raw API-derived data older than YouTube's 30-day limit.

YouTube API Services Developer Policies: authorized data may generally be stored no longer
than 30 days; subscriber counts must not be retained beyond that without consent. This module
enforces that limit against everything this tool writes to disk, split by what the data IS:

- RAW per-video/per-channel API rows (view counts, subscriber counts tied to an identifiable
  channel) are the thing the policy is actually about. They are handled two ways:

  * WHOLE-FILE DELETE by mtime once past max_age_days:
      - `*-video-evidence.csv` / `*-channel-evidence.csv` sidecars written by `report.py`
        (see VIDEO_EVIDENCE_FIELDS / CHANNEL_EVIDENCE_FIELDS in `evidence.py`) — raw per-video
        and per-channel view/subscriber pulls, recursively under `out_dir` (daily runs land in
        subdirs like `out/m002-*/`). These are point-in-time dumps with no resolve-later state,
        so the whole file ages out together.
      - Stale rows in the `Cache` (see `cache.py`) — a single SQLite `kv` table keyed by
        request hash, storing raw JSON API responses plus a write timestamp. Rows are purged
        by that timestamp regardless of the cache's own read-time TTL (`max_age` passed to
        `Cache.get`), because a TTL controls when a cache entry stops being *served*, not how
        long raw API data is allowed to sit on disk.

  * ROW-LEVEL SCRUB for `evidence-snapshots*.csv` (written by `evidence_snapshot.py`): this is
    a RESOLVE-LATER validation ledger — rows carry `status` / `checked_at` /
    `future_breakout_count` (the same forward-test pattern as forward-snapshots.csv) that get
    resolved at later checkpoints, alongside a per-row `created_at`. Whole-file mtime deletion
    fails both ways: while the ledger is actively appended its mtime stays fresh, so raw rows
    older than 30d would be retained indefinitely (non-compliant); and once appending stops,
    the whole file would be reaped at 30d, destroying pending 60/90-day evidence checkpoints
    (data loss). Instead, rows whose own `created_at` is older than max_age_days get ONLY
    their raw API metric fields — `views`, `subs`, `views_per_day` — blanked in place.
    Everything else is preserved (snapshot_id, created_at, label, source, evidence_type,
    ranks, topic, derived scores like evidence_score / channel_trajectory_score, evidence_role,
    title, video/channel ids and urls, status, checked_at, future_breakout_count, notes), so
    the validation chain stays resolvable while the ToS-sensitive raw metrics are dropped.
    Rows with a missing or unparseable `created_at` are KEPT unscrubbed and surfaced as
    warnings — a parse failure must never destroy data. The file itself is never deleted; it
    is rewritten atomically (temp file + replace), only when at least one row actually needs
    scrubbing, and only under --apply.

- DERIVED aggregates (the ranked-niche report `*.csv` / `*.md`, and `out/forward-snapshots.csv`)
  are scores and medians computed FROM the raw pulls, not raw records themselves — they don't
  carry subscriber counts or other per-channel raw metrics forward, so the 30-day cap does not
  apply to them. They are kept indefinitely. `out/backups/` is likewise never touched — anything
  placed there is assumed to be an intentional, out-of-band archive.

CLI is dry-run by default (`python -m youtube_niche.retention`): it prints the plan (files to
delete, ledger rows to scrub, cache rows to purge) and changes nothing until you pass `--apply`.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Filename patterns for raw evidence CSVs written by report.py (see VIDEO_EVIDENCE_FIELDS /
# CHANNEL_EVIDENCE_FIELDS in evidence.py) — matched recursively under out_dir.
_EVIDENCE_GLOBS = ("*-video-evidence.csv", "*-channel-evidence.csv")

# Resolve-later evidence validation ledger written by evidence_snapshot.py
# (evidence_snapshot_path()) — never deleted; raw metric fields are scrubbed row-level instead.
_EVIDENCE_LEDGER_GLOB = "evidence-snapshots*.csv"

# Raw API metric fields blanked from ledger rows past the retention window. Everything else in
# EVIDENCE_SNAPSHOT_FIELDS (ids, urls, titles, derived scores, resolution state) is preserved.
SCRUBBED_LEDGER_FIELDS = ("views", "subs", "views_per_day")

# Ledger that stores only computed scores (see forward.py) — never purged by age.
_FORWARD_SNAPSHOT_GLOB = "forward-snapshots*.csv"

# Directory tree that is never touched, regardless of contents or age.
_BACKUPS_DIRNAME = "backups"

DEFAULT_CACHE_PATH = ".cache/youtube_niche.sqlite"
DEFAULT_MAX_AGE_DAYS = 30


@dataclass
class RetentionItem:
    path: Path
    age_days: float
    reason: str


@dataclass
class LedgerScrubPlan:
    path: Path
    rows_to_scrub: int
    total_rows: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class CachePurgeSummary:
    path: Path
    stale_rows: int
    kept_rows: int


@dataclass
class RetentionPlan:
    out_dir: Path
    cache_dir: Path | None
    now: dt.datetime
    max_age_days: int
    to_delete: list[RetentionItem] = field(default_factory=list)
    kept: list[RetentionItem] = field(default_factory=list)
    ledger_scrubs: list[LedgerScrubPlan] = field(default_factory=list)
    cache_purge: CachePurgeSummary | None = None

    @property
    def total_delete_bytes(self) -> int:
        total = 0
        for item in self.to_delete:
            try:
                total += item.path.stat().st_size
            except OSError:
                pass
        return total

    @property
    def total_ledger_rows_to_scrub(self) -> int:
        return sum(ls.rows_to_scrub for ls in self.ledger_scrubs)


def _age_days(path: Path, now: dt.datetime) -> float:
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return (now - mtime).total_seconds() / 86400.0


def _is_under_backups(path: Path, out_dir: Path) -> bool:
    try:
        rel = path.relative_to(out_dir)
    except ValueError:
        return False
    return _BACKUPS_DIRNAME in rel.parts


def _is_protected_ledger(path: Path) -> bool:
    """Never-touched files: forward snapshots store only scores (no raw metrics)."""
    return path.match(_FORWARD_SNAPSHOT_GLOB)


def _is_evidence_ledger(path: Path) -> bool:
    return path.match(_EVIDENCE_LEDGER_GLOB)


def _is_raw_evidence(path: Path) -> bool:
    return any(path.match(g) for g in _EVIDENCE_GLOBS)


def _evidence_reason(path: Path) -> str:
    if path.match("*-video-evidence.csv"):
        return "raw per-video API evidence (views/subs)"
    return "raw per-channel API evidence (views/subs)"


def _parse_created_at(raw: str) -> dt.datetime | None:
    """Parse the ISO-8601 created_at written by evidence_snapshot.py / forward.py.

    Returns None for anything missing or unparseable — the caller must KEEP such rows
    (and warn), never scrub or delete on a parse failure.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _scrub_ledger_rows(
    header: list[str], rows: list[list[str]], cutoff: dt.datetime
) -> tuple[list[list[str]], int, list[str]]:
    """Blank SCRUBBED_LEDGER_FIELDS in rows older than cutoff. Pure — no I/O.

    Returns (new_rows, scrub_count, warnings). Rows already scrubbed (metric cells blank),
    rows at/after the cutoff, and rows with unparseable created_at pass through unchanged;
    the latter also produce a warning. Column order is preserved exactly as given.
    """
    warnings: list[str] = []
    col_index = {name: i for i, name in enumerate(header)}
    created_i = col_index.get("created_at")
    metric_is = [col_index[f] for f in SCRUBBED_LEDGER_FIELDS if f in col_index]
    if created_i is None:
        warnings.append("no created_at column — no rows scrubbed")
        return rows, 0, warnings
    if not metric_is:
        return rows, 0, warnings

    new_rows: list[list[str]] = []
    scrubbed = 0
    for line_no, row in enumerate(rows, start=2):  # header is line 1
        if created_i >= len(row):
            warnings.append(f"row {line_no}: short row with no created_at — kept unscrubbed")
            new_rows.append(row)
            continue
        created = _parse_created_at(row[created_i])
        if created is None:
            warnings.append(
                f"row {line_no}: unparseable created_at {row[created_i]!r} — kept unscrubbed"
            )
            new_rows.append(row)
            continue
        has_metrics = any(i < len(row) and row[i].strip() for i in metric_is)
        if created < cutoff and has_metrics:
            scrubbed_row = list(row)
            for i in metric_is:
                if i < len(scrubbed_row):
                    scrubbed_row[i] = ""
            new_rows.append(scrubbed_row)
            scrubbed += 1
        else:
            new_rows.append(row)
    return new_rows, scrubbed, warnings


def _read_ledger(path: Path) -> tuple[list[str] | None, list[list[str]]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        return header, list(reader)


def _plan_ledger_scrub(path: Path, now: dt.datetime, max_age_days: int) -> LedgerScrubPlan:
    cutoff = now - dt.timedelta(days=max_age_days)
    try:
        header, rows = _read_ledger(path)
    except OSError as e:
        return LedgerScrubPlan(path, 0, 0, [f"unreadable ({e}) — skipped"])
    if header is None:
        return LedgerScrubPlan(path, 0, 0, [])
    _, count, warnings = _scrub_ledger_rows(header, rows, cutoff)
    return LedgerScrubPlan(path, count, len(rows), warnings)


def _apply_ledger_scrub(path: Path, now: dt.datetime, max_age_days: int) -> int:
    """Scrub old rows in one ledger, atomically (write temp, replace).

    Re-reads the file at apply time and applies the same deterministic cutoff as planning, so
    rows appended between plan and apply are simply fresh (kept) rather than clobbered.
    Rewrites only when at least one row actually needs scrubbing; header and column order are
    written back exactly as read.
    """
    cutoff = now - dt.timedelta(days=max_age_days)
    try:
        header, rows = _read_ledger(path)
    except FileNotFoundError:
        return 0
    if header is None:
        return 0
    new_rows, scrubbed, _warnings = _scrub_ledger_rows(header, rows, cutoff)
    if scrubbed == 0:
        return 0
    tmp = path.with_name(path.name + ".retention-tmp")
    try:
        with tmp.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(new_rows)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return scrubbed


def _scan_out_dir(out_dir: Path, now: dt.datetime, max_age_days: int, plan: RetentionPlan) -> None:
    if not out_dir.exists():
        return
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        if _is_under_backups(path, out_dir):
            plan.kept.append(RetentionItem(path, _age_days(path, now), "under out/backups/ — never purged"))
            continue
        if _is_protected_ledger(path):
            plan.kept.append(RetentionItem(path, _age_days(path, now), "forward-test ledger — scores only, ToS-compliant"))
            continue
        if _is_evidence_ledger(path):
            plan.ledger_scrubs.append(_plan_ledger_scrub(path, now, max_age_days))
            plan.kept.append(RetentionItem(
                path, _age_days(path, now),
                "resolve-later validation ledger — row-level metric scrub only, file never deleted",
            ))
            continue
        age = _age_days(path, now)
        if _is_raw_evidence(path):
            if age > max_age_days:
                plan.to_delete.append(RetentionItem(path, age, _evidence_reason(path)))
            else:
                plan.kept.append(RetentionItem(path, age, f"raw evidence, only {age:.1f}d old (< {max_age_days}d)"))
            continue
        # Everything else under out_dir is a derived aggregate (ranked report .csv/.md, etc.)
        # — retained regardless of age; it does not carry raw per-channel metrics forward.
        plan.kept.append(RetentionItem(path, age, "derived aggregate (scores/report) — not a raw record"))


def _plan_cache_purge(cache_dir: Path | None, now: dt.datetime, max_age_days: int) -> CachePurgeSummary | None:
    """Stale cache ROWS (not files) are purged regardless of the cache's own read-time TTL.

    Cache (see cache.py) is a single SQLite file with a `kv` table of (k, v, ts) rows, one row
    per cached raw API response. There's no per-entry file to age by mtime, so we count rows by
    their write timestamp instead — same 30-day cutoff, applied to the actual raw payload age.
    """
    if cache_dir is None:
        return None
    if cache_dir.is_dir():
        db_path = cache_dir / "youtube_niche.sqlite"
    else:
        db_path = cache_dir
    if not db_path.exists():
        return None
    cutoff_ts = now.timestamp() - max_age_days * 86400.0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='kv'"
                )
            }
            if "kv" not in tables:
                return None
            stale = conn.execute("SELECT COUNT(*) FROM kv WHERE ts < ?", (cutoff_ts,)).fetchone()[0]
            kept = conn.execute("SELECT COUNT(*) FROM kv WHERE ts >= ?", (cutoff_ts,)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return CachePurgeSummary(path=db_path, stale_rows=stale, kept_rows=kept)


def plan_retention(
    out_dir: Path,
    cache_dir: Path | None,
    now: dt.datetime,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> RetentionPlan:
    """Pure planning function — reads mtimes/ledger rows/cache timestamps, never writes."""
    out_dir = Path(out_dir)
    cache_dir = Path(cache_dir) if cache_dir is not None else None
    plan = RetentionPlan(out_dir=out_dir, cache_dir=cache_dir, now=now, max_age_days=max_age_days)
    _scan_out_dir(out_dir, now, max_age_days, plan)
    plan.cache_purge = _plan_cache_purge(cache_dir, now, max_age_days)
    return plan


@dataclass
class RetentionResult:
    files_deleted: int
    bytes_deleted: int
    ledger_rows_scrubbed: int
    cache_rows_deleted: int


def apply_retention(plan: RetentionPlan) -> RetentionResult:
    """Perform the deletions/scrubs described by `plan`.

    File deletions act only on plan.to_delete — never a re-scan, so the dry-run file list is
    exactly what gets deleted. Ledger scrubbing and the cache purge re-read their stores at
    apply time but use the same deterministic cutoff (plan.now, plan.max_age_days), so rows
    appended after planning are fresh by definition and untouched.
    """
    files_deleted = 0
    bytes_deleted = 0
    for item in plan.to_delete:
        try:
            size = item.path.stat().st_size
            item.path.unlink()
        except FileNotFoundError:
            continue
        files_deleted += 1
        bytes_deleted += size

    ledger_rows_scrubbed = 0
    for ls in plan.ledger_scrubs:
        if ls.rows_to_scrub > 0:
            ledger_rows_scrubbed += _apply_ledger_scrub(ls.path, plan.now, plan.max_age_days)

    cache_rows_deleted = 0
    if plan.cache_purge is not None and plan.cache_purge.stale_rows > 0:
        cutoff_ts = plan.now.timestamp() - plan.max_age_days * 86400.0
        conn = sqlite3.connect(str(plan.cache_purge.path))
        try:
            cur = conn.execute("DELETE FROM kv WHERE ts < ?", (cutoff_ts,))
            cache_rows_deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else plan.cache_purge.stale_rows
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()

    return RetentionResult(
        files_deleted=files_deleted,
        bytes_deleted=bytes_deleted,
        ledger_rows_scrubbed=ledger_rows_scrubbed,
        cache_rows_deleted=cache_rows_deleted,
    )


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024:
            return f"{x:.0f}{unit}" if unit == "B" else f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}TB"


def _print_plan(plan: RetentionPlan) -> None:
    print(
        f"Retention plan: out_dir={plan.out_dir} cache_dir={plan.cache_dir} "
        f"max_age_days={plan.max_age_days} as_of={plan.now.isoformat()}"
    )

    if not plan.to_delete:
        print("\nFiles: no raw evidence files older than the retention window.")
    else:
        print(f"\nFiles: {len(plan.to_delete)} raw evidence file(s) to delete "
              f"({_fmt_bytes(plan.total_delete_bytes)}):")
        for item in plan.to_delete:
            print(f"  {item.age_days:>6.1f}d  {item.path}  — {item.reason}")

    noteworthy = [ls for ls in plan.ledger_scrubs if ls.rows_to_scrub > 0 or ls.warnings]
    if not plan.ledger_scrubs:
        print("\nLedger scrub: no evidence-snapshots ledgers found.")
    elif not noteworthy:
        print(f"\nLedger scrub: {len(plan.ledger_scrubs)} ledger(s) checked, no rows past the window.")
    else:
        print(f"\nLedger scrub: {plan.total_ledger_rows_to_scrub} row(s) to scrub "
              f"(views/subs/views_per_day blanked; rows and files kept):")
        for ls in noteworthy:
            print(f"  {ls.rows_to_scrub} row(s) to scrub in {ls.path} ({ls.total_rows} total rows)")
            for w in ls.warnings:
                print(f"    warning: {w}")

    if plan.cache_purge is not None:
        print(
            f"\nCache: {plan.cache_purge.path}\n"
            f"  {plan.cache_purge.stale_rows} stale row(s) to purge, "
            f"{plan.cache_purge.kept_rows} row(s) kept (< {plan.max_age_days}d old)"
        )
    else:
        print("\nCache: no cache database found (nothing to purge).")

    kept_evidence = [i for i in plan.kept if i.reason.startswith("raw evidence")]
    if kept_evidence:
        print(f"\n{len(kept_evidence)} raw evidence file(s) kept (under the age limit).")
    protected = len(plan.kept) - len(kept_evidence)
    print(f"{protected} derived/ledger/backup file(s) under out_dir — never deleted by this tool.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_niche.retention",
        description=(
            "Purge raw per-video/per-channel API evidence older than the YouTube API Developer "
            "Policies 30-day retention limit: old evidence sidecar CSVs are deleted, old "
            "evidence-snapshots ledger rows get their raw metrics (views/subs/views_per_day) "
            "blanked in place, and stale cache rows are purged. Derived scores (the ranked "
            "report, forward-snapshots.csv) are never touched. Dry-run by default."
        ),
    )
    p.add_argument("--out-dir", default="out", help="root of report output (default: out)")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_PATH,
                   help=f"cache dir or sqlite path (default: {DEFAULT_CACHE_PATH})")
    p.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS,
                   help=f"retention window in days (default: {DEFAULT_MAX_AGE_DAYS})")
    p.add_argument("--apply", action="store_true",
                   help="actually delete/scrub — without this flag, only prints the plan")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    out_dir = Path(args.out_dir)
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    plan = plan_retention(out_dir, cache_dir, now, max_age_days=args.max_age_days)
    _print_plan(plan)

    if not args.apply:
        print("\nDry run only — nothing deleted or scrubbed. Re-run with --apply to purge the items above.")
        return 0

    nothing_to_do = (
        not plan.to_delete
        and plan.total_ledger_rows_to_scrub == 0
        and (plan.cache_purge is None or plan.cache_purge.stale_rows == 0)
    )
    if nothing_to_do:
        print("\nNothing to apply.")
        return 0

    result = apply_retention(plan)
    print(
        f"\nApplied: deleted {result.files_deleted} file(s) ({_fmt_bytes(result.bytes_deleted)}), "
        f"scrubbed {result.ledger_rows_scrubbed} ledger row(s), "
        f"purged {result.cache_rows_deleted} stale cache row(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
