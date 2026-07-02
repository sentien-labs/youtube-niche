"""Tests for the forward-test ledger hardening (youtube_niche.forward).

Incident context (2026-06-30): a silent LLM failure caused `discover_niches` to degrade to
keyword n-grams unannounced, and the day's forward snapshot wrote a single thin niche
("dave ramsey" — actually fine on its own, but produced by a degraded run with no other
evidence) straight into the live ~177-row ledger. An earlier --no-llm run separately wrote a
junk title-fragment label ("will make") into the same file. These tests cover the three guards
added in response: a pre-write backup, an `extraction_method` provenance column with backward-
compatible migration, and a junk-label gate that also blocks degraded runs from writing at all.

Everything here runs against tmp_path -- the real out/forward-snapshots.csv (~177 rows of live
forward-test data) is never touched, read, or imported by path.
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest

from youtube_niche.forward import (
    DEGRADED_METHOD,
    MAX_BACKUPS,
    SNAPSHOT_FIELDS,
    _acceptable_label,
    _backup_ledger,
    append_snapshot_rows,
    capture_score_snapshot,
    rows_from_scores,
)

# The ledger's column layout BEFORE the extraction_method provenance column was added --
# hand-copied from forward.py's schema at the time of the incident, used to build an old-format
# fixture file that migration must handle cleanly.
_LEGACY_FIELDS = [
    "snapshot_id", "created_at", "due_at", "horizon_days", "label", "source",
    "rank", "topic", "opportunity", "opportunity_raw", "confidence", "demand",
    "supply_gap", "cpm_score", "relevance_gate", "query_samples", "status",
    "checked_at", "breakout_count", "notes",
]


def _write_legacy_ledger(path: Path) -> None:
    """3-row CSV in the OLD column layout (no extraction_method), simulating the live ledger as
    it existed before this fix shipped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_LEGACY_FIELDS)
        w.writeheader()
        for i, topic in enumerate(["dave ramsey", "backdoor roth ira", "coast fire"], 1):
            w.writerow({
                "snapshot_id": "20260624-000000-legacy",
                "created_at": "2026-06-24T00:00:00+00:00",
                "due_at": "2026-07-24",
                "horizon_days": 30,
                "label": "legacy label",
                "source": "legacy-source",
                "rank": i,
                "topic": topic,
                "opportunity": 0.5,
                "opportunity_raw": 0.6,
                "confidence": 0.7,
                "demand": 0.5,
                "supply_gap": 0.5,
                "cpm_score": 0.5,
                "relevance_gate": 1.0,
                "query_samples": 1,
                "status": "pending",
                "checked_at": "",
                "breakout_count": "",
                "notes": "",
            })


# --------------------------------------------------------------------- _acceptable_label


@pytest.mark.parametrize(
    "topic,expected",
    [
        ("will make", False),          # title fragment / all-filler -> reject
        ("dave ramsey", True),         # real niche -> accept
        ("dividend growth investing", True),  # real niche, 3 tokens -> accept
        ("x", False),                  # single token -> reject
    ],
)
def test_acceptable_label_cases(topic, expected):
    assert _acceptable_label(topic) is expected


def test_acceptable_label_rejects_empty_and_non_string():
    assert _acceptable_label("") is False
    assert _acceptable_label(None) is False
    assert _acceptable_label("   ") is False


def test_acceptable_label_rejects_all_filler_even_with_more_tokens():
    # "how to make" -- 3 tokens, every single one is filler/auxiliary-verb-ish.
    assert _acceptable_label("how to make") is False


def test_acceptable_label_accepts_mixed_filler_plus_content():
    # A real niche can contain a stopword as long as not EVERY token is filler.
    assert _acceptable_label("best budget apps") is True


# --------------------------------------------------------------------- degraded method gate


def test_degraded_method_skips_snapshot_entirely_with_warning(tmp_path, capsys):
    results = [{"topic": "dave ramsey", "opportunity": 0.5, "confidence": 0.5}]
    path, n = capture_score_snapshot(
        results, str(tmp_path), "label", horizons=[30], extraction_method=DEGRADED_METHOD,
    )
    assert n == 0
    assert not path.exists()  # nothing was ever written

    captured = capsys.readouterr()
    assert "SKIPPED" in captured.out
    assert "keyword_degraded" in captured.out


def test_non_degraded_methods_write_normally(tmp_path):
    results = [{"topic": "dave ramsey", "opportunity": 0.5, "confidence": 0.5}]
    path, n = capture_score_snapshot(
        results, str(tmp_path), "label", horizons=[30], extraction_method="llm",
    )
    assert n == 1
    assert path.exists()


# --------------------------------------------------------------------- junk gate at write time


def test_junk_labels_are_skipped_at_snapshot_time_with_notice(tmp_path, capsys):
    results = [
        {"topic": "dave ramsey", "opportunity": 0.5, "confidence": 0.5},
        {"topic": "will make", "opportunity": 0.5, "confidence": 0.5},  # junk: must be dropped
    ]
    path, n = capture_score_snapshot(
        results, str(tmp_path), "label", horizons=[30], extraction_method="keyword",
    )
    assert n == 1  # only the acceptable row was written
    text = path.read_text()
    assert "dave ramsey" in text
    assert "will make" not in text

    captured = capsys.readouterr()
    assert "will make" in captured.out
    assert "skipping" in captured.out.lower()


def test_all_rows_junk_writes_nothing(tmp_path):
    results = [{"topic": "will make", "opportunity": 0.5, "confidence": 0.5}]
    path, n = capture_score_snapshot(
        results, str(tmp_path), "label", horizons=[30], extraction_method="keyword",
    )
    assert n == 0
    # append_snapshot_rows is never called when there's nothing accepted -> file untouched.
    assert not path.exists()


# --------------------------------------------------------------------- provenance column


def test_new_rows_carry_extraction_method(tmp_path):
    results = [{"topic": "dividend growth investing", "opportunity": 0.5, "confidence": 0.5}]
    path, n = capture_score_snapshot(
        results, str(tmp_path), "label", horizons=[30], extraction_method="llm",
    )
    assert n == 1
    with path.open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["extraction_method"] == "llm"


def test_rows_from_scores_defaults_extraction_method_to_empty_string():
    rows = rows_from_scores([{"topic": "coast fire"}], "label", "source", [30])
    assert rows[0]["extraction_method"] == ""


# --------------------------------------------------------------------- backup


def test_backup_created_before_append(tmp_path):
    ledger = tmp_path / "forward-snapshots.csv"
    _write_legacy_ledger(ledger)

    backup_dir = tmp_path / "backups"
    assert not backup_dir.exists()

    append_snapshot_rows(ledger, rows_from_scores(
        [{"topic": "coast fire"}], "label", "source", [30], "llm",
    ))

    assert backup_dir.exists()
    backups = list(backup_dir.glob("forward-snapshots-*.csv"))
    assert len(backups) == 1
    # the backup must reflect the file's state BEFORE this append/migration touched it.
    backup_text = backups[0].read_text()
    assert "extraction_method" not in backup_text.splitlines()[0]
    assert "dave ramsey" in backup_text


def test_no_backup_when_ledger_does_not_exist_yet(tmp_path):
    ledger = tmp_path / "forward-snapshots.csv"
    assert _backup_ledger(ledger) is None
    assert not (tmp_path / "backups").exists()

    # capture_score_snapshot on a brand-new ledger must not create a backups/ dir either.
    results = [{"topic": "coast fire", "opportunity": 0.5, "confidence": 0.5}]
    capture_score_snapshot(results, str(tmp_path), "label", horizons=[30], extraction_method="llm")
    assert not (tmp_path / "backups").exists()


def test_backups_pruned_to_newest_30(tmp_path):
    ledger = tmp_path / "forward-snapshots.csv"
    _write_legacy_ledger(ledger)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # Pre-seed 35 fake backups with distinct, monotonically increasing timestamps in the name
    # (matching _backup_ledger's stamp format) so pruning has an unambiguous oldest/newest order.
    base = dt.datetime(2026, 1, 1)
    for i in range(35):
        stamp = (base + dt.timedelta(minutes=i)).strftime("%Y%m%d-%H%M%S")
        (backup_dir / f"forward-snapshots-{stamp}.csv").write_text("old,backup\n1,2\n")

    assert len(list(backup_dir.glob("forward-snapshots-*.csv"))) == 35

    dest = _backup_ledger(ledger)
    assert dest is not None

    remaining = sorted(backup_dir.glob("forward-snapshots-*.csv"), key=lambda p: p.name)
    assert len(remaining) == MAX_BACKUPS
    # the newest-created backup (this call's) must have survived pruning.
    assert dest.name == remaining[-1].name


# --------------------------------------------------------------------- legacy migration


def test_legacy_ledger_migrates_cleanly_on_append(tmp_path):
    ledger = tmp_path / "forward-snapshots.csv"
    _write_legacy_ledger(ledger)

    with ledger.open() as f:
        before_header = next(csv.reader(f))
    assert "extraction_method" not in before_header
    assert len(before_header) == len(_LEGACY_FIELDS)

    append_snapshot_rows(ledger, rows_from_scores(
        [{"topic": "social security timing"}], "label", "source", [30], "llm",
    ))

    with ledger.open() as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 4  # 3 legacy + 1 new
    # migrated legacy rows: extraction_method backfilled empty, everything else preserved.
    legacy_topics = {r["topic"] for r in rows[:3]}
    assert legacy_topics == {"dave ramsey", "backdoor roth ira", "coast fire"}
    for r in rows[:3]:
        assert r["extraction_method"] == ""
        assert r["status"] == "pending"
    # the new row carries real provenance.
    assert rows[3]["topic"] == "social security timing"
    assert rows[3]["extraction_method"] == "llm"

    with ledger.open() as f:
        header = next(csv.reader(f))
    assert header == SNAPSHOT_FIELDS


def test_legacy_ledger_migration_happens_after_backup(tmp_path):
    """The backup snapshot must be the OLD (pre-migration) format -- if migration ran first, the
    backup would be pointless (it'd just be a copy of the already-migrated file)."""
    ledger = tmp_path / "forward-snapshots.csv"
    _write_legacy_ledger(ledger)

    append_snapshot_rows(ledger, rows_from_scores(
        [{"topic": "social security timing"}], "label", "source", [30], "llm",
    ))

    backups = list((tmp_path / "backups").glob("forward-snapshots-*.csv"))
    assert len(backups) == 1
    with backups[0].open() as f:
        backup_header = next(csv.reader(f))
    assert backup_header == _LEGACY_FIELDS
    assert "extraction_method" not in backup_header


def test_resolve_still_parses_old_format_ledger(tmp_path):
    """resolve_due_snapshots must keep working on rows read from an old-format (pre-migration)
    file -- it never references extraction_method, so a plain dict missing that key must not
    raise."""
    from youtube_niche.forward import resolve_due_snapshots

    ledger = tmp_path / "forward-snapshots.csv"
    _write_legacy_ledger(ledger)
    with ledger.open() as f:
        rows = list(csv.DictReader(f))
    assert "extraction_method" not in rows[0]

    class _FakeCfg:
        winner_min_vpd = 100.0
        cache_only = True

    class _FakeClient:
        def search_calls_remaining(self):
            return 10

    far_future = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)  # before any due_at -> nothing due
    resolved_rows, summary = resolve_due_snapshots(rows, _FakeClient(), _FakeCfg(), now=far_future)
    assert summary["due"] == 0
    assert resolved_rows == rows


def test_resolve_parses_mixed_migrated_file(tmp_path):
    """A file that has already been migrated (legacy rows + new rows, all under the new header)
    must resolve without error -- mixed extraction_method values across rows."""
    from youtube_niche.forward import resolve_due_snapshots

    ledger = tmp_path / "forward-snapshots.csv"
    _write_legacy_ledger(ledger)
    append_snapshot_rows(ledger, rows_from_scores(
        [{"topic": "social security timing"}], "label", "source", [30], "llm",
    ))

    with ledger.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4
    assert {r["extraction_method"] for r in rows} == {"", "llm"}

    class _FakeCfg:
        winner_min_vpd = 100.0
        cache_only = True

    class _FakeClient:
        def search_calls_remaining(self):
            return 10

    far_future = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    resolved_rows, summary = resolve_due_snapshots(rows, _FakeClient(), _FakeCfg(), now=far_future)
    assert summary["due"] == 0
    assert len(resolved_rows) == 4
