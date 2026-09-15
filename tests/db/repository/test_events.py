"""Tests for `athena.db.repository.events` against a real migrated SQLite file."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import aiosqlite
import pytest

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations
from athena.db.repository import events


@pytest.fixture
async def conn(tmp_path: Path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(tmp_path / "test.db")
    await apply_pending_migrations(connection, DEFAULT_MIGRATIONS_DIR)
    yield connection
    await connection.close()


async def test_append_event_mints_event_id_and_occurred_at_when_not_supplied(
    conn: aiosqlite.Connection,
) -> None:
    correlation_id = str(uuid.uuid4())

    returned_event_id = await events.append_event(
        conn,
        event_type="fs.path_changed",
        source="filesystem_watcher",
        correlation_id=correlation_id,
        payload={"path": "CLAUDE/a.md", "raw_event_kinds": ["modified"]},
    )

    uuid.UUID(returned_event_id)  # raises ValueError if not a valid UUID string
    cursor = await conn.execute(
        "SELECT event_id, event_type, source, correlation_id, causation_id, "
        "idempotency_key, actor, payload_json, occurred_at, schema_version "
        "FROM events WHERE event_id = ?",
        (returned_event_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    (
        event_id,
        event_type,
        source,
        row_correlation_id,
        causation_id,
        idempotency_key,
        actor,
        payload_json,
        occurred_at,
        schema_version,
    ) = row
    assert event_id == returned_event_id
    assert event_type == "fs.path_changed"
    assert source == "filesystem_watcher"
    assert row_correlation_id == correlation_id
    assert causation_id is None
    assert idempotency_key is None
    assert actor is None
    assert json.loads(payload_json) == {"path": "CLAUDE/a.md", "raw_event_kinds": ["modified"]}
    assert occurred_at  # non-empty, minted
    assert schema_version == 1


async def test_append_event_uses_caller_supplied_event_id_and_occurred_at(
    conn: aiosqlite.Connection,
) -> None:
    fixed_event_id = "11111111-1111-1111-1111-111111111111"
    fixed_occurred_at = "2026-08-28T00:00:00+00:00"

    returned_event_id = await events.append_event(
        conn,
        event_type="job.completed",
        source="huey_job",
        correlation_id=str(uuid.uuid4()),
        causation_id="22222222-2222-2222-2222-222222222222",
        idempotency_key="index:CLAUDE/a.md",
        actor="mcp:note_update",
        payload={"job_id": "huey-1", "job_type": "ingestion", "duration_ms": 10, "noop": False},
        event_id=fixed_event_id,
        occurred_at=fixed_occurred_at,
    )

    assert returned_event_id == fixed_event_id
    cursor = await conn.execute(
        "SELECT occurred_at, causation_id, idempotency_key, actor FROM events WHERE event_id = ?",
        (fixed_event_id,),
    )
    row = await cursor.fetchone()
    assert row == (
        fixed_occurred_at,
        "22222222-2222-2222-2222-222222222222",
        "index:CLAUDE/a.md",
        "mcp:note_update",
    )


async def test_append_event_serializes_payload_as_json(conn: aiosqlite.Connection) -> None:
    payload = {"notes_ingested": 3, "notes_skipped": 1, "duration_ms": 120}

    event_id = await events.append_event(
        conn,
        event_type="ingestion.job_completed",
        source="huey_job",
        correlation_id=str(uuid.uuid4()),
        payload=payload,
    )

    cursor = await conn.execute("SELECT payload_json FROM events WHERE event_id = ?", (event_id,))
    row = await cursor.fetchone()
    assert row is not None
    assert json.loads(row[0]) == payload


async def test_append_event_with_sql_metacharacter_laden_actor_stored_literally(
    conn: aiosqlite.Connection,
) -> None:
    malicious_actor = "mcp:note_update'; DROP TABLE events; --"

    event_id = await events.append_event(
        conn,
        event_type="vault.note_modified",
        source="mcp_tool_call",
        correlation_id=str(uuid.uuid4()),
        actor=malicious_actor,
        payload={},
    )

    cursor = await conn.execute("SELECT actor FROM events WHERE event_id = ?", (event_id,))
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == malicious_actor

    cursor = await conn.execute("SELECT COUNT(*) FROM events")
    count_row = await cursor.fetchone()
    assert count_row is not None
    assert count_row[0] == 1


async def test_record_vault_event_persists_with_defaults(conn: aiosqlite.Connection) -> None:
    await events.record_vault_event(
        conn,
        event_type="vault.note_created",
        payload={"path": "a.md", "note_id": 1},
    )

    cursor = await conn.execute(
        "SELECT event_type, source, causation_id, payload_json FROM events "
        "WHERE event_type = 'vault.note_created'"
    )
    row = await cursor.fetchone()
    assert row is not None
    event_type, source, causation_id, payload_json = row
    assert event_type == "vault.note_created"
    assert source == "mcp_tool_call"
    assert causation_id is None
    assert json.loads(payload_json) == {"path": "a.md", "note_id": 1}


async def test_record_vault_event_accepts_an_explicit_source(conn: aiosqlite.Connection) -> None:
    await events.record_vault_event(
        conn,
        event_type="vault.note_created",
        payload={"path": "a.md"},
        source="huey_job",
    )

    cursor = await conn.execute("SELECT source FROM events WHERE event_type = 'vault.note_created'")
    row = await cursor.fetchone()
    assert row == ("huey_job",)


async def test_record_vault_event_never_raises_on_a_db_failure(
    conn: aiosqlite.Connection,
) -> None:
    await conn.close()  # guarantee the next write fails

    # Must not raise, matching auto_commit_mutation's identical guarantee.
    await events.record_vault_event(conn, event_type="vault.note_created", payload={"path": "a.md"})


async def test_two_calls_mint_distinct_event_ids(conn: aiosqlite.Connection) -> None:
    correlation_id = str(uuid.uuid4())

    first = await events.append_event(
        conn,
        event_type="job.started",
        source="huey_job",
        correlation_id=correlation_id,
        payload={},
    )
    second = await events.append_event(
        conn,
        event_type="job.completed",
        source="huey_job",
        correlation_id=correlation_id,
        payload={},
    )

    assert first != second
