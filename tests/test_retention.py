"""Offline tests for the retention scrubber — tmp_path only, never touches real out/ or .cache/.

Run: ./venv/bin/python -m pytest tests/test_retention.py -q
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
from pathlib import Path

from youtube_niche.evidence_snapshot import EVIDENCE_SNAPSHOT_FIELDS
from youtube_niche.retention import SCRUBBED_LEDGER_FIELDS, apply_retention, plan_retention

NOW = dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
OLD_DAYS = 45  # older than the 30-day default window
FRESH_DAYS = 5  # younger than the 30-day default window


def _touch(path: Path, age_days: float, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    mtime = (NOW - dt.timedelta(days=age_days)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


def _iso_age(days: float) -> str:
    """created_at in the exact format evidence_snapshot.py writes (ISO 8601, +00:00)."""
    return (NOW - dt.timedelta(days=days)).isoformat()


def _ledger_row(created_at: str, **overrides) -> dict:
    """A full evidence-snapshots row using the real production header fields."""
    row = {f: "" for f in EVIDENCE_SNAPSHOT_FIELDS}
    row.update({
        "snapshot_id": "20260501-000000-000000-test",
        "created_at": created_at,
        "label": "AI / AI tools",
        "source": "report",
        "evidence_type": "video",
        "category_rank": "1",
        "topic_rank": "1",
        "topic": "how to use chatgpt effectively",
        "topic_opportunity": "0.4271",
        "candidate_source": "domain_autocomplete",
        "evidence_score": "0.9565",
        "opportunity_evidence_score": "0.4085",
        "evidence_role": "newcomer_breakout",
        "title": "10 Secret Prompts, that 95% Don't Use",  # comma exercises CSV quoting round-trip
        "video_id": "5NSFwuhsOao",
        "video_url": "https://www.youtube.com/watch?v=5NSFwuhsOao",
        "channel_title": "Some Channel",
        "channel_id": "UC2TMwBU0KdwbAoy0ieSMZkA",
        "channel_url": "https://www.youtube.com/channel/UC2TMwBU0KdwbAoy0ieSMZkA",
        "views_per_day": "2679.8038",
        "views": "1256828",
        "subs": "1930",
        "channel_trajectory_score": "0.5432",  # derived score — must survive the scrub
        "status": "pending",
    })
    row.update(overrides)
    return row


def _write_ledger(path: Path, rows: list[dict], mtime_age_days: float) -> Path:
    """Write a ledger exactly the way evidence_snapshot.append_evidence_snapshot_rows does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EVIDENCE_SNAPSHOT_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    mtime = (NOW - dt.timedelta(days=mtime_age_days)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        return header, list(reader)


def _make_cache(cache_dir: Path, rows: list[tuple[str, str, float]]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cache_dir / "youtube_niche.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT, ts REAL)")
    conn.executemany("INSERT INTO kv (k, v, ts) VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


def _seed_out_dir(out_dir: Path) -> dict[str, Path]:
    """Create one of every kind of artifact this tool writes, at both old and fresh ages."""
    paths = {}

    # Raw evidence CSVs at top level — old (should be deleted) and fresh (should be kept).
    paths["old_video_evidence"] = _touch(
        out_dir / "ai-tools-20260501-old-video-evidence.csv", OLD_DAYS
    )
    paths["old_channel_evidence"] = _touch(
        out_dir / "ai-tools-20260501-old-channel-evidence.csv", OLD_DAYS
    )
    paths["fresh_video_evidence"] = _touch(
        out_dir / "ai-tools-20260628-fresh-video-evidence.csv", FRESH_DAYS
    )
    paths["fresh_channel_evidence"] = _touch(
        out_dir / "ai-tools-20260628-fresh-channel-evidence.csv", FRESH_DAYS
    )

    # Raw evidence CSVs in a daily-run subdir (e.g. out/m002-ai-2026.../*) — recursive scan.
    subdir = out_dir / "m002-ai-20260501"
    paths["old_video_evidence_subdir"] = _touch(
        subdir / "ai-20260501-video-evidence.csv", OLD_DAYS
    )
    paths["old_channel_evidence_subdir"] = _touch(
        subdir / "ai-20260501-channel-evidence.csv", OLD_DAYS
    )
    fresh_subdir = out_dir / "m002-ai-20260628"
    paths["fresh_video_evidence_subdir"] = _touch(
        fresh_subdir / "ai-20260628-video-evidence.csv", FRESH_DAYS
    )

    # Evidence snapshot ledgers (resolve-later validation ledgers) — NEVER deleted; old rows
    # get raw metrics scrubbed row-level instead.
    # "Active" ledger: FRESH mtime (still being appended) but contains an OLD row — whole-file
    # mtime deletion would have retained that raw row forever (the compliance flaw). Also
    # carries an unparseable-created_at row, which must be kept unscrubbed and warned about.
    paths["ledger_active"] = _write_ledger(
        fresh_subdir / "evidence-snapshots.csv",
        [
            _ledger_row(_iso_age(OLD_DAYS), video_id="active-old"),
            _ledger_row(_iso_age(FRESH_DAYS), video_id="active-fresh"),
            _ledger_row("not-a-timestamp", video_id="active-unparseable"),
        ],
        FRESH_DAYS,
    )
    # "Quiet" ledger: OLD mtime, pending rows — whole-file mtime deletion would have destroyed
    # its unresolved 60/90-day checkpoints (the data-loss flaw).
    paths["ledger_quiet"] = _write_ledger(
        subdir / "evidence-snapshots.csv",
        [_ledger_row(_iso_age(OLD_DAYS), video_id="quiet-old")],
        OLD_DAYS,
    )

    # Forward-test ledger — scores only, must NEVER be planned for deletion even when old.
    paths["old_forward_snapshots"] = _touch(
        out_dir / "forward-snapshots.csv", OLD_DAYS
    )
    paths["old_forward_snapshot_summary"] = _touch(
        out_dir / "forward-snapshot-summary-20260501-000000.csv", OLD_DAYS
    )

    # Derived ranked report .csv/.md — old copy should be kept (derived, not raw).
    paths["old_report_csv"] = _touch(out_dir / "ai-tools-20260501-old.csv", OLD_DAYS)
    paths["old_report_md"] = _touch(out_dir / "ai-tools-20260501-old.md", OLD_DAYS)

    # File under out/backups/ — never touched, even though it matches an evidence glob and is old.
    paths["old_backup_evidence"] = _touch(
        out_dir / "backups" / "ai-tools-20260101-video-evidence.csv", OLD_DAYS
    )

    return paths


def test_plan_selects_only_old_evidence_and_stale_cache(tmp_path):
    out_dir = tmp_path / "out"
    cache_dir = tmp_path / ".cache"
    paths = _seed_out_dir(out_dir)
    _make_cache(cache_dir, [
        ("old1", "{}", (NOW - dt.timedelta(days=OLD_DAYS)).timestamp()),
        ("old2", "{}", (NOW - dt.timedelta(days=OLD_DAYS)).timestamp()),
        ("fresh1", "{}", (NOW - dt.timedelta(days=FRESH_DAYS)).timestamp()),
    ])

    plan = plan_retention(out_dir, cache_dir, NOW, max_age_days=30)

    planned = {item.path for item in plan.to_delete}
    expected_deleted = {
        paths["old_video_evidence"],
        paths["old_channel_evidence"],
        paths["old_video_evidence_subdir"],
        paths["old_channel_evidence_subdir"],
    }
    assert planned == expected_deleted, f"unexpected plan: {planned}"

    # Ledger scrub plan: both ledgers found; each has exactly 1 old row to scrub — including
    # the active ledger whose file mtime is fresh (row created_at governs, not mtime). The
    # unparseable-created_at row is NOT counted for scrubbing; it surfaces as a plan warning.
    scrubs = {ls.path: ls for ls in plan.ledger_scrubs}
    assert set(scrubs) == {paths["ledger_active"], paths["ledger_quiet"]}
    assert scrubs[paths["ledger_active"]].rows_to_scrub == 1
    assert scrubs[paths["ledger_active"]].total_rows == 3
    assert len(scrubs[paths["ledger_active"]].warnings) == 1
    assert "unparseable created_at" in scrubs[paths["ledger_active"]].warnings[0]
    assert scrubs[paths["ledger_quiet"]].rows_to_scrub == 1
    assert scrubs[paths["ledger_quiet"]].total_rows == 1
    assert scrubs[paths["ledger_quiet"]].warnings == []

    # Cache purge plan: 2 stale rows, 1 kept row.
    assert plan.cache_purge is not None
    assert plan.cache_purge.stale_rows == 2
    assert plan.cache_purge.kept_rows == 1


def test_ledger_backups_and_report_never_planned(tmp_path):
    out_dir = tmp_path / "out"
    paths = _seed_out_dir(out_dir)

    plan = plan_retention(out_dir, None, NOW, max_age_days=30)

    planned = {item.path for item in plan.to_delete}
    never_delete = {
        paths["old_forward_snapshots"],
        paths["old_forward_snapshot_summary"],
        paths["old_report_csv"],
        paths["old_report_md"],
        paths["old_backup_evidence"],
        paths["ledger_active"],
        paths["ledger_quiet"],
    }
    assert planned.isdisjoint(never_delete), f"protected files were planned for deletion: {planned & never_delete}"

    kept_paths = {item.path for item in plan.kept}
    assert never_delete <= kept_paths


def test_fresh_evidence_is_kept_not_deleted(tmp_path):
    out_dir = tmp_path / "out"
    paths = _seed_out_dir(out_dir)

    plan = plan_retention(out_dir, None, NOW, max_age_days=30)

    planned = {item.path for item in plan.to_delete}
    fresh = {
        paths["fresh_video_evidence"],
        paths["fresh_channel_evidence"],
        paths["fresh_video_evidence_subdir"],
    }
    assert planned.isdisjoint(fresh)
    kept_paths = {item.path for item in plan.kept}
    assert fresh <= kept_paths


def test_dry_run_deletes_nothing(tmp_path):
    out_dir = tmp_path / "out"
    cache_dir = tmp_path / ".cache"
    paths = _seed_out_dir(out_dir)
    db_path = _make_cache(cache_dir, [
        ("old1", "{}", (NOW - dt.timedelta(days=OLD_DAYS)).timestamp()),
    ])
    ledger_bytes = {k: paths[k].read_bytes() for k in ("ledger_active", "ledger_quiet")}

    plan_retention(out_dir, cache_dir, NOW, max_age_days=30)  # planning must not touch disk

    # Nothing removed or rewritten by planning alone.
    for p in paths.values():
        assert p.exists(), f"{p} was deleted by planning (should be dry-run/no-op)"
    for k, before in ledger_bytes.items():
        assert paths[k].read_bytes() == before, f"{k} was modified by planning"
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0] == 1
    conn.close()


def test_cli_main_dry_run_deletes_nothing(tmp_path, capsys):
    from youtube_niche.retention import main

    out_dir = tmp_path / "out"
    cache_dir = tmp_path / ".cache"
    paths = _seed_out_dir(out_dir)
    _make_cache(cache_dir, [
        ("old1", "{}", (NOW - dt.timedelta(days=OLD_DAYS)).timestamp()),
    ])
    ledger_bytes = {k: paths[k].read_bytes() for k in ("ledger_active", "ledger_quiet")}

    rc = main(["--out-dir", str(out_dir), "--cache-dir", str(cache_dir)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Dry run only" in out
    assert "row(s) to scrub in" in out  # ledger scrub is reported, not silently skipped
    assert "warning:" in out and "unparseable created_at" in out  # seed's bad row is surfaced
    for p in paths.values():
        assert p.exists()
    for k, before in ledger_bytes.items():
        assert paths[k].read_bytes() == before, f"{k} was modified by a dry run"


def test_apply_deletes_exactly_the_planned_set_and_leaves_rest(tmp_path):
    out_dir = tmp_path / "out"
    cache_dir = tmp_path / ".cache"
    paths = _seed_out_dir(out_dir)
    db_path = _make_cache(cache_dir, [
        ("old1", "{}", (NOW - dt.timedelta(days=OLD_DAYS)).timestamp()),
        ("old2", "{}", (NOW - dt.timedelta(days=OLD_DAYS)).timestamp()),
        ("fresh1", "{}", (NOW - dt.timedelta(days=FRESH_DAYS)).timestamp()),
    ])

    plan = plan_retention(out_dir, cache_dir, NOW, max_age_days=30)
    before_header, before_rows = _read_csv(paths["ledger_active"])
    result = apply_retention(plan)

    assert result.files_deleted == 4
    assert result.ledger_rows_scrubbed == 2  # one old row in each ledger
    assert result.cache_rows_deleted == 2

    deleted_keys = {
        "old_video_evidence", "old_channel_evidence",
        "old_video_evidence_subdir", "old_channel_evidence_subdir",
    }
    for key, p in paths.items():
        if key in deleted_keys:
            assert not p.exists(), f"{key} ({p}) should have been deleted"
        else:
            assert p.exists(), f"{key} ({p}) should NOT have been deleted"

    # Active ledger after apply: same header and row count; old row's metrics blanked;
    # fresh and unparseable rows byte-identical (unparseable kept per never-destroy-on-parse-failure).
    after_header, after_rows = _read_csv(paths["ledger_active"])
    assert after_header == before_header
    assert len(after_rows) == len(before_rows) == 3
    idx = {name: i for i, name in enumerate(after_header)}
    for col in SCRUBBED_LEDGER_FIELDS:
        assert after_rows[0][idx[col]] == ""  # old row scrubbed
    assert after_rows[1] == before_rows[1]  # fresh row untouched
    assert after_rows[2] == before_rows[2]  # unparseable row untouched, metrics still present
    assert after_rows[2][idx["views"]] != ""

    conn = sqlite3.connect(str(db_path))
    remaining = {row[0] for row in conn.execute("SELECT k FROM kv")}
    conn.close()
    assert remaining == {"fresh1"}


def test_ledger_scrub_blanks_only_metrics_and_preserves_all_other_fields(tmp_path):
    out_dir = tmp_path / "out"
    old_row = _ledger_row(_iso_age(OLD_DAYS), video_id="old-vid")
    fresh_row = _ledger_row(
        _iso_age(FRESH_DAYS), video_id="fresh-vid", views="42", subs="7", views_per_day="6.0"
    )
    path = _write_ledger(out_dir / "evidence-snapshots.csv", [old_row, fresh_row], FRESH_DAYS)
    header_before, rows_before = _read_csv(path)
    first_line_before = path.read_bytes().split(b"\n", 1)[0]

    plan = plan_retention(out_dir, None, NOW, max_age_days=30)
    assert len(plan.ledger_scrubs) == 1
    ls = plan.ledger_scrubs[0]
    assert ls.path == path and ls.rows_to_scrub == 1 and ls.total_rows == 2
    assert ls.warnings == []
    assert path not in {i.path for i in plan.to_delete}

    result = apply_retention(plan)
    assert result.ledger_rows_scrubbed == 1
    assert path.exists(), "ledger file must never be deleted"

    header_after, rows_after = _read_csv(path)
    assert header_after == header_before == EVIDENCE_SNAPSHOT_FIELDS
    assert path.read_bytes().split(b"\n", 1)[0] == first_line_before  # header byte-identical
    assert len(rows_after) == len(rows_before) == 2  # row count unchanged

    idx = {name: i for i, name in enumerate(header_after)}
    for col in EVIDENCE_SNAPSHOT_FIELDS:
        i = idx[col]
        if col in SCRUBBED_LEDGER_FIELDS:
            assert rows_before[0][i] != "", f"seed row should have had {col} populated"
            assert rows_after[0][i] == "", f"{col} should be blanked in the old row"
        else:
            # Every non-metric field preserved byte-for-byte (incl. created_at, ids, urls,
            # title-with-comma, derived channel_trajectory_score, status/checkpoint fields).
            assert rows_after[0][i] == rows_before[0][i], f"{col} was altered by the scrub"
    assert rows_after[1] == rows_before[1], "fresh row must be untouched"


def test_ledger_unparseable_created_at_kept_and_warned(tmp_path):
    out_dir = tmp_path / "out"
    bad_row = _ledger_row("not-a-timestamp", video_id="bad-vid")
    blank_row = _ledger_row("", video_id="blank-vid")
    old_row = _ledger_row(_iso_age(OLD_DAYS), video_id="old-vid")
    path = _write_ledger(
        out_dir / "evidence-snapshots.csv", [bad_row, blank_row, old_row], FRESH_DAYS
    )
    _, rows_before = _read_csv(path)

    plan = plan_retention(out_dir, None, NOW, max_age_days=30)
    ls = plan.ledger_scrubs[0]
    assert ls.rows_to_scrub == 1  # only the parseable old row
    assert len(ls.warnings) == 2  # one per unparseable/blank created_at
    assert all("created_at" in w and "kept unscrubbed" in w for w in ls.warnings)

    result = apply_retention(plan)
    assert result.ledger_rows_scrubbed == 1

    header_after, rows_after = _read_csv(path)
    assert len(rows_after) == 3
    assert rows_after[0] == rows_before[0], "unparseable created_at row must be kept untouched"
    assert rows_after[1] == rows_before[1], "blank created_at row must be kept untouched"
    idx = {name: i for i, name in enumerate(header_after)}
    for col in SCRUBBED_LEDGER_FIELDS:
        assert rows_after[2][idx[col]] == ""  # the parseable old row was scrubbed


def test_ledger_apply_is_idempotent(tmp_path):
    out_dir = tmp_path / "out"
    path = _write_ledger(
        out_dir / "evidence-snapshots.csv",
        [_ledger_row(_iso_age(OLD_DAYS)), _ledger_row(_iso_age(FRESH_DAYS), video_id="fresh")],
        FRESH_DAYS,
    )

    first_plan = plan_retention(out_dir, None, NOW, max_age_days=30)
    assert first_plan.ledger_scrubs[0].rows_to_scrub == 1
    first = apply_retention(first_plan)
    assert first.ledger_rows_scrubbed == 1
    bytes_after_first = path.read_bytes()

    # Re-plan: already-scrubbed rows are not counted again.
    second_plan = plan_retention(out_dir, None, NOW, max_age_days=30)
    assert second_plan.ledger_scrubs[0].rows_to_scrub == 0
    second = apply_retention(second_plan)
    assert second.ledger_rows_scrubbed == 0
    assert path.read_bytes() == bytes_after_first  # no rewrite when nothing to scrub

    # Even re-applying the stale FIRST plan is safe: apply re-derives from the same cutoff.
    third = apply_retention(first_plan)
    assert third.ledger_rows_scrubbed == 0
    assert path.read_bytes() == bytes_after_first


def test_apply_is_idempotent_on_already_deleted_files(tmp_path):
    out_dir = tmp_path / "out"
    _seed_out_dir(out_dir)
    plan = plan_retention(out_dir, None, NOW, max_age_days=30)
    first = apply_retention(plan)
    assert first.files_deleted == 4
    assert first.ledger_rows_scrubbed == 2
    # Re-applying the same (now-stale) plan should not raise even though files are gone,
    # and the already-scrubbed ledgers must not be re-scrubbed.
    second = apply_retention(plan)
    assert second.files_deleted == 0
    assert second.ledger_rows_scrubbed == 0


def test_missing_out_and_cache_dirs_plan_cleanly(tmp_path):
    out_dir = tmp_path / "does-not-exist"
    cache_dir = tmp_path / "also-missing"
    plan = plan_retention(out_dir, cache_dir, NOW, max_age_days=30)
    assert plan.to_delete == []
    assert plan.ledger_scrubs == []
    assert plan.cache_purge is None
    result = apply_retention(plan)
    assert result.files_deleted == 0
    assert result.ledger_rows_scrubbed == 0
    assert result.cache_rows_deleted == 0


def test_custom_max_age_days_boundary(tmp_path):
    out_dir = tmp_path / "out"
    # Exactly at 10 days old, with a 10-day window: should NOT be deleted (age must exceed window).
    at_boundary = _touch(out_dir / "topic-1-video-evidence.csv", 10.0)
    just_over = _touch(out_dir / "topic-2-video-evidence.csv", 10.001)

    plan = plan_retention(out_dir, None, NOW, max_age_days=10)
    planned = {item.path for item in plan.to_delete}

    assert at_boundary not in planned
    assert just_over in planned
