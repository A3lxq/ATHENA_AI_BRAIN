"""Tests for `athena.db.repository.research_jobs` against a real migrated SQLite file."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations
from athena.db.repository import notes, research_jobs


@pytest.fixture
async def conn(tmp_path: Path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(tmp_path / "test.db")
    await apply_pending_migrations(connection, DEFAULT_MIGRATIONS_DIR)
    yield connection
    await connection.close()


async def test_insert_defaults_status_to_queued(conn: aiosqlite.Connection) -> None:
    job_id = await research_jobs.insert(
        conn,
        huey_task_id="huey-task-1",
        job_type="ingestion",
        created_at="2026-08-28T00:00:00+00:00",
    )

    cursor = await conn.execute(
        "SELECT huey_task_id, job_type, status, query, requested_by, started_at, finished_at "
        "FROM research_jobs WHERE id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    assert row == ("huey-task-1", "ingestion", "queued", None, None, None, None)


async def test_insert_with_query_and_requested_by(conn: aiosqlite.Connection) -> None:
    job_id = await research_jobs.insert(
        conn,
        huey_task_id="huey-task-2",
        job_type="research_start",
        query="what is RAG?",
        requested_by="mcp:research_start",
        created_at="2026-08-28T00:00:00+00:00",
    )

    cursor = await conn.execute(
        "SELECT query, requested_by FROM research_jobs WHERE id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    assert row == ("what is RAG?", "mcp:research_start")


async def test_mark_started_sets_status_running_and_started_at(
    conn: aiosqlite.Connection,
) -> None:
    job_id = await research_jobs.insert(
        conn,
        huey_task_id="huey-task-3",
        job_type="ingestion",
        created_at="2026-08-28T00:00:00+00:00",
    )

    await research_jobs.mark_started(conn, job_id, "2026-08-28T00:00:05+00:00")

    cursor = await conn.execute(
        "SELECT status, started_at FROM research_jobs WHERE id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    assert row == ("running", "2026-08-28T00:00:05+00:00")


async def test_mark_finished_succeeded_with_result_note_id(conn: aiosqlite.Connection) -> None:
    job_id = await research_jobs.insert(
        conn,
        huey_task_id="huey-task-4",
        job_type="ingestion",
        created_at="2026-08-28T00:00:00+00:00",
    )
    note_id = await notes.insert(
        conn,
        path="CLAUDE/a.md",
        title="A",
        origin="human",
        provider=None,
        folder=None,
        content_hash="h",
        created_at="2026-08-28T00:00:00+00:00",
    )

    await research_jobs.mark_finished(
        conn,
        job_id,
        status="succeeded",
        finished_at="2026-08-28T00:01:00+00:00",
        result_note_id=note_id,
    )

    cursor = await conn.execute(
        "SELECT status, finished_at, error_message, result_note_id FROM research_jobs "
        "WHERE id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    assert row == ("succeeded", "2026-08-28T00:01:00+00:00", None, note_id)


async def test_mark_finished_failed_with_error_message(conn: aiosqlite.Connection) -> None:
    job_id = await research_jobs.insert(
        conn,
        huey_task_id="huey-task-5",
        job_type="ingestion",
        created_at="2026-08-28T00:00:00+00:00",
    )

    await research_jobs.mark_finished(
        conn,
        job_id,
        status="failed",
        finished_at="2026-08-28T00:01:00+00:00",
        error_message="disk read error",
    )

    cursor = await conn.execute(
        "SELECT status, error_message, result_note_id FROM research_jobs WHERE id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    assert row == ("failed", "disk read error", None)


async def test_mark_finished_rejects_non_terminal_status(conn: aiosqlite.Connection) -> None:
    job_id = await research_jobs.insert(
        conn,
        huey_task_id="huey-task-6",
        job_type="ingestion",
        created_at="2026-08-28T00:00:00+00:00",
    )

    with pytest.raises(ValueError, match="queued"):
        await research_jobs.mark_finished(
            conn, job_id, status="queued", finished_at="2026-08-28T00:01:00+00:00"
        )


async def test_get_by_id_returns_the_row(conn: aiosqlite.Connection) -> None:
    job_id = await research_jobs.insert(
        conn, huey_task_id="huey-task-7", job_type="reindex_start",
        created_at="2026-08-28T00:00:00+00:00",
    )

    row = await research_jobs.get_by_id(conn, job_id)

    assert row is not None
    assert row.id == job_id
    assert row.huey_task_id == "huey-task-7"
    assert row.job_type == "reindex_start"
    assert row.status == "queued"


async def test_get_by_id_returns_none_for_unknown_id(conn: aiosqlite.Connection) -> None:
    row = await research_jobs.get_by_id(conn, 999_999)

    assert row is None


async def test_mark_cancelled_sets_status_and_finished_at(conn: aiosqlite.Connection) -> None:
    job_id = await research_jobs.insert(
        conn, huey_task_id="huey-task-8", job_type="duplicates_scan",
        created_at="2026-08-28T00:00:00+00:00",
    )

    await research_jobs.mark_cancelled(conn, job_id, cancelled_at="2026-08-28T00:01:00+00:00")

    row = await research_jobs.get_by_id(conn, job_id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.finished_at == "2026-08-28T00:01:00+00:00"


async def test_record_draft_stores_title_body_and_source_urls(
    conn: aiosqlite.Connection,
) -> None:
    job_id = await research_jobs.insert(
        conn, huey_task_id="huey-task-draft-1", job_type="research_start",
        created_at="2026-09-10T00:00:00+00:00",
    )

    await research_jobs.record_draft(
        conn,
        job_id,
        draft_title="What is RAG?",
        draft_body="## Source: https://a.example/\n\nsome content",
        draft_source_urls=["https://a.example/", "https://b.example/"],
    )

    row = await research_jobs.get_by_id(conn, job_id)
    assert row is not None
    assert row.draft_title == "What is RAG?"
    assert row.draft_body == "## Source: https://a.example/\n\nsome content"
    assert row.draft_source_urls == ["https://a.example/", "https://b.example/"]


async def test_get_by_id_returns_none_draft_fields_before_record_draft(
    conn: aiosqlite.Connection,
) -> None:
    job_id = await research_jobs.insert(
        conn, huey_task_id="huey-task-draft-2", job_type="research_start",
        created_at="2026-09-10T00:00:00+00:00",
    )

    row = await research_jobs.get_by_id(conn, job_id)

    assert row is not None
    assert row.draft_title is None
    assert row.draft_body is None
    assert row.draft_source_urls is None


async def test_record_result_note_sets_result_note_id_without_touching_status(
    conn: aiosqlite.Connection,
) -> None:
    job_id = await research_jobs.insert(
        conn, huey_task_id="huey-task-draft-3", job_type="research_start",
        created_at="2026-09-10T00:00:00+00:00",
    )
    await research_jobs.mark_finished(
        conn, job_id, status="succeeded", finished_at="2026-09-10T00:01:00+00:00"
    )
    note_id = await notes.insert(
        conn, path="research/a.md", title="A", origin="web_research", provider=None,
        folder="research", content_hash="h", created_at="2026-09-10T00:02:00+00:00",
    )

    await research_jobs.record_result_note(conn, job_id, note_id)

    cursor = await conn.execute(
        "SELECT status, finished_at, result_note_id FROM research_jobs WHERE id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    assert row == ("succeeded", "2026-09-10T00:01:00+00:00", note_id)


async def test_get_by_huey_task_id_finds_the_row(conn: aiosqlite.Connection) -> None:
    job_id = await research_jobs.insert(
        conn, huey_task_id="huey-task-draft-4", job_type="research_start",
        created_at="2026-09-10T00:00:00+00:00",
    )

    row = await research_jobs.get_by_huey_task_id(conn, "huey-task-draft-4")

    assert row is not None
    assert row.id == job_id


async def test_get_by_huey_task_id_returns_none_for_unknown_task_id(
    conn: aiosqlite.Connection,
) -> None:
    row = await research_jobs.get_by_huey_task_id(conn, "no-such-huey-task-id")

    assert row is None


async def test_count_by_status_groups_correctly(conn: aiosqlite.Connection) -> None:
    job_a = await research_jobs.insert(
        conn, huey_task_id="huey-task-9", job_type="ingestion",
        created_at="2026-08-28T00:00:00+00:00",
    )
    await research_jobs.insert(
        conn, huey_task_id="huey-task-10", job_type="ingestion",
        created_at="2026-08-28T00:00:00+00:00",
    )
    await research_jobs.mark_finished(
        conn, job_a, status="succeeded", finished_at="2026-08-28T00:01:00+00:00"
    )

    counts = await research_jobs.count_by_status(conn)

    assert counts == {"queued": 1, "succeeded": 1}
