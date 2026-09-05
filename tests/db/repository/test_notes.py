"""Tests for `athena.db.repository.notes` against a real migrated SQLite file."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations
from athena.db.repository import notes

_MALICIOUS_PATH = "CLAUDE/foo'; DROP TABLE notes; --.md"


@pytest.fixture
async def conn(tmp_path: Path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(tmp_path / "test.db")
    await apply_pending_migrations(connection, DEFAULT_MIGRATIONS_DIR)
    yield connection
    await connection.close()


async def test_insert_and_get_by_path(conn: aiosqlite.Connection) -> None:
    note_id = await notes.insert(
        conn,
        path="CLAUDE/a.md",
        title="A",
        origin="ai_generated",
        provider="anthropic",
        folder="CLAUDE",
        content_hash="h1",
        created_at="2026-08-28T00:00:00+00:00",
    )

    row = await notes.get_by_path(conn, "CLAUDE/a.md")

    assert row is not None
    assert row.id == note_id
    assert row.path == "CLAUDE/a.md"
    assert row.title == "A"
    assert row.origin == "ai_generated"
    assert row.provider == "anthropic"
    assert row.folder == "CLAUDE"
    assert row.content_hash == "h1"
    assert row.status == "draft"  # DB DEFAULT applied, not passed explicitly
    assert row.deleted_at is None
    assert row.secret_scan_status == "clean"  # noqa: S105 -- scan status, not a password
    assert row.created_at == row.updated_at


async def test_insert_with_explicit_status(conn: aiosqlite.Connection) -> None:
    await notes.insert(
        conn,
        path="CLAUDE/b.md",
        title="B",
        origin="human",
        provider=None,
        folder=None,
        content_hash="h2",
        created_at="2026-08-28T00:00:00+00:00",
        status="active",
    )

    row = await notes.get_by_path(conn, "CLAUDE/b.md")

    assert row is not None
    assert row.status == "active"


async def test_get_by_path_returns_none_when_not_found(conn: aiosqlite.Connection) -> None:
    row = await notes.get_by_path(conn, "does/not/exist.md")

    assert row is None


async def test_update_content(conn: aiosqlite.Connection) -> None:
    note_id = await notes.insert(
        conn,
        path="CLAUDE/c.md",
        title="C",
        origin="human",
        provider=None,
        folder=None,
        content_hash="old",
        created_at="2026-08-28T00:00:00+00:00",
    )

    await notes.update_content(
        conn, note_id, content_hash="new", updated_at="2026-08-28T01:00:00+00:00"
    )

    row = await notes.get_by_path(conn, "CLAUDE/c.md")
    assert row is not None
    assert row.content_hash == "new"
    assert row.updated_at == "2026-08-28T01:00:00+00:00"


async def test_move(conn: aiosqlite.Connection) -> None:
    note_id = await notes.insert(
        conn,
        path="CLAUDE/old.md",
        title="Old",
        origin="human",
        provider=None,
        folder=None,
        content_hash="h",
        created_at="2026-08-28T00:00:00+00:00",
    )

    await notes.move(
        conn, note_id, new_path="CLAUDE/new.md", updated_at="2026-08-28T02:00:00+00:00"
    )

    assert await notes.get_by_path(conn, "CLAUDE/old.md") is None
    moved = await notes.get_by_path(conn, "CLAUDE/new.md")
    assert moved is not None
    assert moved.id == note_id
    assert moved.updated_at == "2026-08-28T02:00:00+00:00"


async def test_soft_delete(conn: aiosqlite.Connection) -> None:
    note_id = await notes.insert(
        conn,
        path="CLAUDE/d.md",
        title="D",
        origin="human",
        provider=None,
        folder=None,
        content_hash="h",
        created_at="2026-08-28T00:00:00+00:00",
    )

    await notes.soft_delete(conn, note_id, deleted_at="2026-08-28T03:00:00+00:00")

    row = await notes.get_by_path(conn, "CLAUDE/d.md")
    assert row is not None
    assert row.deleted_at == "2026-08-28T03:00:00+00:00"


async def test_find_by_content_hash_returns_multiple_active_matches(
    conn: aiosqlite.Connection,
) -> None:
    await notes.insert(
        conn,
        path="GROK_GPT/Grok-_04.md",
        title="Grok 04",
        origin="ai_generated",
        provider="xai",
        folder="GROK_GPT",
        content_hash="dup-hash",
        created_at="2026-08-28T00:00:00+00:00",
    )
    await notes.insert(
        conn,
        path="GROK_GPT/Grok-_04(1).md",
        title="Grok 04 (1)",
        origin="ai_generated",
        provider="xai",
        folder="GROK_GPT",
        content_hash="dup-hash",
        created_at="2026-08-28T00:00:01+00:00",
    )

    matches = await notes.find_by_content_hash(conn, "dup-hash")

    assert {m.path for m in matches} == {"GROK_GPT/Grok-_04.md", "GROK_GPT/Grok-_04(1).md"}


async def test_find_by_content_hash_excludes_soft_deleted_notes(
    conn: aiosqlite.Connection,
) -> None:
    note_id = await notes.insert(
        conn,
        path="CLAUDE/gone.md",
        title="Gone",
        origin="human",
        provider=None,
        folder=None,
        content_hash="ghost-hash",
        created_at="2026-08-28T00:00:00+00:00",
    )
    await notes.soft_delete(conn, note_id, deleted_at="2026-08-28T04:00:00+00:00")

    matches = await notes.find_by_content_hash(conn, "ghost-hash")

    assert matches == []


async def test_find_by_content_hash_returns_empty_list_when_no_match(
    conn: aiosqlite.Connection,
) -> None:
    matches = await notes.find_by_content_hash(conn, "no-such-hash")

    assert matches == []


async def test_sql_metacharacter_laden_path_is_stored_and_retrieved_literally(
    conn: aiosqlite.Connection,
) -> None:
    await notes.insert(
        conn,
        path=_MALICIOUS_PATH,
        title="Robert'); DROP TABLE notes;--",
        origin="imported",
        provider=None,
        folder=None,
        content_hash="h",
        created_at="2026-08-28T00:00:00+00:00",
    )

    row = await notes.get_by_path(conn, _MALICIOUS_PATH)

    assert row is not None
    assert row.path == _MALICIOUS_PATH
    assert row.title == "Robert'); DROP TABLE notes;--"

    # The `notes` table must still exist and be otherwise unharmed -- proves
    # the malicious-looking string was bound as data, never executed as SQL.
    cursor = await conn.execute("SELECT COUNT(*) FROM notes")
    count_row = await cursor.fetchone()
    assert count_row is not None
    assert count_row[0] == 1


async def test_list_active_excludes_soft_deleted_notes(conn: aiosqlite.Connection) -> None:
    kept_id = await notes.insert(
        conn, path="a.md", title="A", origin="human", provider=None,
        folder=None, content_hash="h1", created_at="2026-09-04T00:00:00+00:00",
    )
    deleted_id = await notes.insert(
        conn, path="b.md", title="B", origin="human", provider=None,
        folder=None, content_hash="h2", created_at="2026-09-04T00:00:00+00:00",
    )
    await notes.soft_delete(conn, deleted_id, deleted_at="2026-09-04T01:00:00+00:00")

    active = await notes.list_active(conn)

    assert [row.id for row in active] == [kept_id]


async def test_list_stale_candidates_filters_by_status_and_cutoff(
    conn: aiosqlite.Connection,
) -> None:
    active_old = await notes.insert(
        conn, path="old.md", title="Old", origin="human", provider=None,
        folder=None, content_hash="h1", created_at="2026-01-01T00:00:00+00:00", status="active",
    )
    active_recent = await notes.insert(
        conn, path="recent.md", title="Recent", origin="human", provider=None,
        folder=None, content_hash="h2", created_at="2026-09-01T00:00:00+00:00", status="active",
    )
    draft_old = await notes.insert(
        conn, path="draft.md", title="Draft", origin="human", provider=None,
        folder=None, content_hash="h3", created_at="2026-01-01T00:00:00+00:00",
    )
    archived_old = await notes.insert(
        conn, path="archived.md", title="Archived", origin="human", provider=None,
        folder=None, content_hash="h4", created_at="2026-01-01T00:00:00+00:00", status="archived",
    )

    stale_candidates = await notes.list_stale_candidates(conn, cutoff="2026-06-01T00:00:00+00:00")

    assert [row.id for row in stale_candidates] == [active_old]
    assert active_recent not in [row.id for row in stale_candidates]
    assert draft_old not in [row.id for row in stale_candidates]
    assert archived_old not in [row.id for row in stale_candidates]


async def test_count_active_excludes_soft_deleted_notes(conn: aiosqlite.Connection) -> None:
    await notes.insert(
        conn, path="a.md", title="A", origin="human", provider=None,
        folder=None, content_hash="h1", created_at="2026-09-04T00:00:00+00:00",
    )
    deleted_id = await notes.insert(
        conn, path="b.md", title="B", origin="human", provider=None,
        folder=None, content_hash="h2", created_at="2026-09-04T00:00:00+00:00",
    )
    await notes.soft_delete(conn, deleted_id, deleted_at="2026-09-04T01:00:00+00:00")

    count = await notes.count_active(conn)

    assert count == 1
