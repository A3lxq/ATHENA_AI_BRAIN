"""Tests for `athena.db.repository.provenance` against a real migrated SQLite file."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations
from athena.db.repository import notes, provenance


@pytest.fixture
async def conn(tmp_path: Path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(tmp_path / "test.db")
    await apply_pending_migrations(connection, DEFAULT_MIGRATIONS_DIR)
    yield connection
    await connection.close()


async def _make_note(conn: aiosqlite.Connection) -> int:
    return await notes.insert(
        conn,
        path="CLAUDE/a.md",
        title="A",
        origin="ai_generated",
        provider="anthropic",
        folder="CLAUDE",
        content_hash="h",
        created_at="2026-08-28T00:00:00+00:00",
    )


async def test_insert_activity_returns_new_id_and_stores_fields(
    conn: aiosqlite.Connection,
) -> None:
    note_id = await _make_note(conn)

    provenance_id = await provenance.insert_activity(
        conn,
        note_id=note_id,
        activity_type="ingested",
        provider="anthropic",
        model=None,
        human_edited=False,
        occurred_at="2026-08-28T00:00:00+00:00",
        recorded_at="2026-08-28T00:00:01+00:00",
    )

    cursor = await conn.execute(
        "SELECT note_id, activity_type, provider, human_edited, transformation_notes, "
        "research_job_id FROM provenance WHERE id = ?",
        (provenance_id,),
    )
    row = await cursor.fetchone()
    assert row == (note_id, "ingested", "anthropic", 0, None, None)


async def test_insert_activity_stores_optional_fields_when_provided(
    conn: aiosqlite.Connection,
) -> None:
    note_id = await _make_note(conn)
    huey_task_id = "huey-task-1"
    await conn.execute(
        "INSERT INTO research_jobs (huey_task_id, job_type, created_at) VALUES (?, ?, ?)",
        (huey_task_id, "ingestion", "2026-08-28T00:00:00+00:00"),
    )
    await conn.commit()
    cursor = await conn.execute(
        "SELECT id FROM research_jobs WHERE huey_task_id = ?", (huey_task_id,)
    )
    job_row = await cursor.fetchone()
    assert job_row is not None
    job_id = job_row[0]

    provenance_id = await provenance.insert_activity(
        conn,
        note_id=note_id,
        activity_type="human_edit",
        provider="human",
        model=None,
        human_edited=True,
        occurred_at="2026-08-28T00:00:00+00:00",
        recorded_at="2026-08-28T00:00:01+00:00",
        transformation_notes="cleaned up formatting",
        research_job_id=job_id,
    )

    cursor = await conn.execute(
        "SELECT human_edited, transformation_notes, research_job_id FROM provenance WHERE id = ?",
        (provenance_id,),
    )
    row = await cursor.fetchone()
    assert row == (1, "cleaned up formatting", job_id)


async def test_insert_source_attaches_to_provenance_row(conn: aiosqlite.Connection) -> None:
    note_id = await _make_note(conn)
    provenance_id = await provenance.insert_activity(
        conn,
        note_id=note_id,
        activity_type="ingested",
        provider="anthropic",
        model=None,
        human_edited=False,
        occurred_at="2026-08-28T00:00:00+00:00",
        recorded_at="2026-08-28T00:00:01+00:00",
    )

    await provenance.insert_source(
        conn,
        provenance_id=provenance_id,
        url="https://chat.example.com/conversation/abc",
        title="Example conversation",
        accessed_at="2026-08-28T00:00:00+00:00",
    )

    cursor = await conn.execute(
        "SELECT provenance_id, url, title, accessed_at FROM provenance_sources "
        "WHERE provenance_id = ?",
        (provenance_id,),
    )
    row = await cursor.fetchone()
    assert row == (
        provenance_id,
        "https://chat.example.com/conversation/abc",
        "Example conversation",
        "2026-08-28T00:00:00+00:00",
    )


async def test_insert_source_with_only_required_fields(conn: aiosqlite.Connection) -> None:
    note_id = await _make_note(conn)
    provenance_id = await provenance.insert_activity(
        conn,
        note_id=note_id,
        activity_type="ingested",
        provider=None,
        model=None,
        human_edited=False,
        occurred_at="2026-08-28T00:00:00+00:00",
        recorded_at="2026-08-28T00:00:01+00:00",
    )

    await provenance.insert_source(conn, provenance_id=provenance_id, url="https://example.com")

    cursor = await conn.execute(
        "SELECT title, accessed_at FROM provenance_sources WHERE provenance_id = ?",
        (provenance_id,),
    )
    row = await cursor.fetchone()
    assert row == (None, None)


async def test_insert_source_url_with_sql_metacharacters_stored_literally(
    conn: aiosqlite.Connection,
) -> None:
    note_id = await _make_note(conn)
    provenance_id = await provenance.insert_activity(
        conn,
        note_id=note_id,
        activity_type="ingested",
        provider=None,
        model=None,
        human_edited=False,
        occurred_at="2026-08-28T00:00:00+00:00",
        recorded_at="2026-08-28T00:00:01+00:00",
    )
    malicious_url = "https://example.com/?q=1'; DROP TABLE provenance_sources; --"

    await provenance.insert_source(conn, provenance_id=provenance_id, url=malicious_url)

    cursor = await conn.execute(
        "SELECT url FROM provenance_sources WHERE provenance_id = ?", (provenance_id,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == malicious_url


async def _make_note_named(conn: aiosqlite.Connection, path: str) -> int:
    return await notes.insert(
        conn, path=path, title=path, origin="human", provider=None,
        folder=None, content_hash=f"hash-{path}", created_at="2026-09-04T00:00:00+00:00",
    )


async def test_insert_activity_stores_supersedes_note_id(conn: aiosqlite.Connection) -> None:
    kept = await _make_note_named(conn, "keep.md")
    absorbed = await _make_note_named(conn, "absorb.md")

    provenance_id = await provenance.insert_activity(
        conn, note_id=kept, activity_type="merge", provider=None, model=None,
        human_edited=False, occurred_at="t0", recorded_at="t0",
        supersedes_note_id=absorbed,
    )

    cursor = await conn.execute(
        "SELECT supersedes_note_id FROM provenance WHERE id = ?", (provenance_id,)
    )
    row = await cursor.fetchone()
    assert row == (absorbed,)


async def test_insert_derivation_attaches_source_note(conn: aiosqlite.Connection) -> None:
    kept = await _make_note_named(conn, "keep.md")
    source = await _make_note_named(conn, "source.md")
    provenance_id = await provenance.insert_activity(
        conn, note_id=kept, activity_type="merge", provider=None, model=None,
        human_edited=False, occurred_at="t0", recorded_at="t0",
    )

    await provenance.insert_derivation(conn, provenance_id=provenance_id, source_note_id=source)

    cursor = await conn.execute(
        "SELECT source_note_id FROM provenance_derivations WHERE provenance_id = ?",
        (provenance_id,),
    )
    row = await cursor.fetchone()
    assert row == (source,)


async def test_get_lineage_reports_ancestor_via_supersedes(conn: aiosqlite.Connection) -> None:
    kept = await _make_note_named(conn, "keep.md")
    absorbed = await _make_note_named(conn, "absorb.md")
    await provenance.insert_activity(
        conn, note_id=kept, activity_type="merge", provider=None, model=None,
        human_edited=False, occurred_at="t0", recorded_at="t0",
        supersedes_note_id=absorbed,
    )

    lineage = await provenance.get_lineage(conn, kept)

    assert [edge.note_id for edge in lineage.ancestors] == [absorbed]
    assert lineage.descendants == []


async def test_get_lineage_reports_descendant_from_the_other_side(
    conn: aiosqlite.Connection,
) -> None:
    kept = await _make_note_named(conn, "keep.md")
    absorbed = await _make_note_named(conn, "absorb.md")
    await provenance.insert_activity(
        conn, note_id=kept, activity_type="merge", provider=None, model=None,
        human_edited=False, occurred_at="t0", recorded_at="t0",
        supersedes_note_id=absorbed,
    )

    lineage = await provenance.get_lineage(conn, absorbed)

    assert lineage.ancestors == []
    assert [edge.note_id for edge in lineage.descendants] == [kept]


async def test_get_lineage_includes_multi_source_derivations_as_ancestors(
    conn: aiosqlite.Connection,
) -> None:
    kept = await _make_note_named(conn, "keep.md")
    source_1 = await _make_note_named(conn, "source1.md")
    source_2 = await _make_note_named(conn, "source2.md")
    provenance_id = await provenance.insert_activity(
        conn, note_id=kept, activity_type="merge", provider=None, model=None,
        human_edited=False, occurred_at="t0", recorded_at="t0",
    )
    await provenance.insert_derivation(conn, provenance_id=provenance_id, source_note_id=source_1)
    await provenance.insert_derivation(conn, provenance_id=provenance_id, source_note_id=source_2)

    lineage = await provenance.get_lineage(conn, kept)

    assert {edge.note_id for edge in lineage.ancestors} == {source_1, source_2}


async def test_get_lineage_on_a_note_with_no_history_is_empty(conn: aiosqlite.Connection) -> None:
    note_id = await _make_note_named(conn, "lonely.md")

    lineage = await provenance.get_lineage(conn, note_id)

    assert lineage.ancestors == []
    assert lineage.descendants == []


async def test_get_activities_for_note_returns_only_that_notes_rows(
    conn: aiosqlite.Connection,
) -> None:
    note_a = await _make_note_named(conn, "a.md")
    note_b = await _make_note_named(conn, "b.md")
    await provenance.insert_activity(
        conn, note_id=note_a, activity_type="ingested", provider="anthropic", model=None,
        human_edited=False, occurred_at="t0", recorded_at="t0",
    )
    await provenance.insert_activity(
        conn, note_id=note_a, activity_type="human_edit", provider="human", model=None,
        human_edited=True, occurred_at="t1", recorded_at="t1",
    )
    await provenance.insert_activity(
        conn, note_id=note_b, activity_type="ingested", provider="anthropic", model=None,
        human_edited=False, occurred_at="t0", recorded_at="t0",
    )

    activities = await provenance.get_activities_for_note(conn, note_a)

    assert [a.activity_type for a in activities] == ["ingested", "human_edit"]
    assert all(a.note_id == note_a for a in activities)


async def test_get_activities_for_note_returns_empty_for_a_note_with_none(
    conn: aiosqlite.Connection,
) -> None:
    note_id = await _make_note_named(conn, "lonely.md")

    activities = await provenance.get_activities_for_note(conn, note_id)

    assert activities == []
