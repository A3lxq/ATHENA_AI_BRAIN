from __future__ import annotations

import hashlib
from pathlib import Path

import aiosqlite
import pytest
from huey import SqliteHuey

from athena.db.repository import events as events_repo
from athena.db.repository import notes as notes_repo
from athena.db.repository import provenance as provenance_repo
from athena.safety.paths import VaultRoot
from athena.vault import lifecycle
from athena.vault.ingest import ingest_note
from athena.vault.reconcile import reconcile_vault


def _write(vault_dir: Path, relative: str, text: str = "content\n") -> Path:
    path = vault_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def test_reconcile_ingests_a_file_the_watcher_missed(
    conn: aiosqlite.Connection, huey: SqliteHuey, vault_root: VaultRoot, vault_dir: Path
) -> None:
    _write(vault_dir, "missed.md")

    summary = await reconcile_vault(conn, huey, vault_root)

    assert summary.discrepancies_found == 1
    assert summary.paths_scanned == 1
    assert await notes_repo.get_by_path(conn, "missed.md") is not None


async def test_reconcile_is_a_true_noop_when_index_matches_disk(
    conn: aiosqlite.Connection, huey: SqliteHuey, vault_root: VaultRoot, vault_dir: Path
) -> None:
    path = _write(vault_dir, "already-current.md")
    await ingest_note(conn, huey, vault_root, str(path), correlation_id="c1")

    summary = await reconcile_vault(conn, huey, vault_root)

    assert summary.paths_scanned == 1
    assert summary.discrepancies_found == 0


async def test_reconcile_detects_note_missing_from_disk(
    conn: aiosqlite.Connection, huey: SqliteHuey, vault_root: VaultRoot, vault_dir: Path
) -> None:
    path = _write(vault_dir, "will-vanish.md")
    result = await ingest_note(conn, huey, vault_root, str(path), correlation_id="c1")
    path.unlink()

    summary = await reconcile_vault(conn, huey, vault_root)

    assert summary.discrepancies_found == 1
    row = await notes_repo.get_by_path(conn, "will-vanish.md")
    assert row is not None
    assert row.id == result.note_id
    assert row.deleted_at is not None


async def test_reconcile_detects_hash_mismatch(
    conn: aiosqlite.Connection, huey: SqliteHuey, vault_root: VaultRoot, vault_dir: Path
) -> None:
    path = _write(vault_dir, "changes.md", "version one\n")
    await ingest_note(conn, huey, vault_root, str(path), correlation_id="c1")
    path.write_text("version two\n", encoding="utf-8")

    summary = await reconcile_vault(conn, huey, vault_root)

    assert summary.discrepancies_found == 1
    row = await notes_repo.get_by_path(conn, "changes.md")
    assert row is not None


async def test_reconcile_completed_event_has_correct_summary_counts(
    conn: aiosqlite.Connection, huey: SqliteHuey, vault_root: VaultRoot, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md")
    _write(vault_dir, "b.md")

    summary = await reconcile_vault(conn, huey, vault_root)

    cursor = await conn.execute(
        "SELECT payload_json FROM events WHERE event_type = 'reconciliation.completed' "
        "AND correlation_id = ?",
        (summary.correlation_id,),
    )
    row = await cursor.fetchone()
    assert row is not None

    cursor = await conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'reconciliation.discrepancy_found' "
        "AND correlation_id = ?",
        (summary.correlation_id,),
    )
    assert (await cursor.fetchone())[0] == summary.discrepancies_found == 2


# --- Worker-crash-mid-job recovery (design doc §2.5, TESTING_STRATEGY.md's
# "Job Queue" Recovery row: "kill a worker mid-job and assert the job is
# recoverable on restart, with the reconciliation job as an independent
# second safety net if job-level recovery also fails") ---------------------
#
# `test_reconcile_ingests_a_file_the_watcher_missed` above already covers
# the simplest crash flavor (a file `ingest_note`/bootstrap never touched at
# all). The test below simulates a more precise, more realistic crash: a
# job that actually started -- real `job.started` event recorded, exactly
# as `_ingest_note_locked`'s first line does -- and was killed before it
# reached the note-row write or emitted any `job.completed`/`job.failed`.
# This is a state a `kill -9` mid-`_ingest_note_locked` genuinely produces
# (its `job.started` append is `conn.commit()`-ed immediately, independent
# of every later step), and it exercises something the simpler scenario
# doesn't: reconciliation must recover the note correctly *despite* an
# orphaned, never-completed `job.started` event from the crashed attempt
# sitting in the `events` table, not merely when no job ever ran at all.


async def test_reconcile_recovers_after_a_job_started_but_crashed_before_any_write(
    conn: aiosqlite.Connection, huey: SqliteHuey, vault_root: VaultRoot, vault_dir: Path
) -> None:
    _write(vault_dir, "crashed-early.md")

    # The crashed job's only trace: a `job.started` event, committed
    # immediately by `_append`/`events_repo.append_event`, with no matching
    # `job.completed`/`job.failed` -- because the process died before
    # `_ingest_note_locked` reached either. No `notes` row was ever created.
    crashed_correlation_id = "crashed-job-correlation"
    await events_repo.append_event(
        conn,
        event_type="job.started",
        source="huey_job",
        correlation_id=crashed_correlation_id,
        causation_id=None,
        payload={"job_type": "ingestion", "path": "crashed-early.md"},
    )

    summary = await reconcile_vault(conn, huey, vault_root)

    # Reconciliation re-derives truth from disk independently of any job
    # event trail -- it recovers the note correctly regardless.
    assert summary.discrepancies_found == 1
    row = await notes_repo.get_by_path(conn, "crashed-early.md")
    assert row is not None
    assert row.deleted_at is None

    # The crashed job's own event trail is left exactly as the crash left
    # it (reconciliation does not retroactively complete/clean up someone
    # else's job.started event) -- confirming this test actually simulated
    # an orphaned in-flight job, not a no-op.
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM events WHERE correlation_id = ? "
        "AND event_type IN ('job.completed', 'job.failed')",
        (crashed_correlation_id,),
    )
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known, tracked gap (found while implementing docs/design/"
        "production-hardening.md §2.5, not fixed here per that phase's "
        "test-only scope): reconcile_vault's discrepancy detection is "
        "purely notes.content_hash-vs-disk based (see "
        "_ingest_note_locked's existing.content_hash == content_hash "
        "short-circuit to 'noop', athena/vault/ingest.py). A worker crash "
        "*after* the note row is written (content_hash already correct) "
        "but *before* _persist_secret_scan_result/provenance/tags persist "
        "is invisible to reconciliation -- it reports zero discrepancies "
        "and never backfills the missing provenance/secret-scan/tag rows. "
        "This test documents that gap as a visible, tracked xfail rather "
        "than a silent one, matching this project's own established "
        "convention (see TESTING_STRATEGY.md's RAG Pipeline Security "
        "paragraph, 'this test exists as a tracked, visible xfail'). "
        "Remove/flip once reconcile_vault is extended to also check for "
        "notes with no provenance activity."
    ),
)
async def test_reconcile_backfills_provenance_after_crash_between_note_write_and_provenance_write(
    conn: aiosqlite.Connection, huey: SqliteHuey, vault_root: VaultRoot, vault_dir: Path
) -> None:
    content = "partially ingested content\n"
    path = _write(vault_dir, "partially-ingested.md", content)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Simulate a crash inside `_ingest_note_locked` immediately after
    # `lifecycle.create_note` (the note row, with the CORRECT content_hash,
    # is committed) but before `_persist_secret_scan_result` /
    # `provenance_repo.insert_activity` / tag attachment ever ran -- a real
    # reachable state since each of those is its own committed statement,
    # not one wrapping transaction (docs/design/migration-runner-and-
    # vault-ingestion.md never claims otherwise).
    note_id = await lifecycle.create_note(
        conn,
        path=str(path.relative_to(vault_dir)),
        title="partially-ingested",
        origin="human",
        provider=None,
        folder=None,
        content_hash=content_hash,
        created_at="2026-01-01T00:00:00+00:00",
        changed_by="huey_job",
    )

    summary = await reconcile_vault(conn, huey, vault_root)

    # This is the discovered gap: content_hash already matches disk, so
    # ingest_note's own idempotency check reports "noop" and reconciliation
    # never notices the note is missing its provenance record.
    assert summary.discrepancies_found == 1
    activities = await provenance_repo.get_activities_for_note(conn, note_id)
    assert activities, "provenance should have been backfilled by reconciliation"
