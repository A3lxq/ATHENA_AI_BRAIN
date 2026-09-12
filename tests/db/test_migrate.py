from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from athena.db.migrate import (
    DEFAULT_MIGRATIONS_DIR,
    MigrationChecksumMismatchError,
    MigrationError,
    apply_pending_migrations,
    discover_migrations,
)

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY, filename TEXT NOT NULL,
    checksum TEXT NOT NULL, applied_at TEXT NOT NULL
);
"""


@pytest.fixture
async def conn(tmp_path: Path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(tmp_path / "test.db")
    yield connection
    await connection.close()


def test_discover_migrations_sorts_by_numeric_prefix(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 1;")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;")
    (tmp_path / "not_a_migration.txt").write_text("ignored")

    migrations = discover_migrations(tmp_path)

    assert [v for v, _ in migrations] == [1, 2]
    assert migrations[0][1].name == "0001_first.sql"


async def test_apply_real_migrations_creates_full_schema(conn: aiosqlite.Connection) -> None:
    records = await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)

    assert [r.version for r in records] == [1, 2, 3, 4, 5]
    cursor = await conn.execute("PRAGMA user_version")
    assert (await cursor.fetchone())[0] == 5

    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    table_names = {row[0] for row in await cursor.fetchall()}
    assert {
        "notes", "tags", "note_tags", "provenance", "provenance_sources",
        "provenance_derivations", "note_lifecycle_history", "duplicate_candidates",
        "note_minhash_signatures", "research_jobs", "chunks", "events",
        "note_secret_findings", "secret_scan_allowlist", "schema_migrations",
    }.issubset(table_names)


async def test_reapplying_is_a_true_noop(conn: aiosqlite.Connection) -> None:
    first = await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)
    second = await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)

    assert first == second


async def test_mandatory_pragmas_are_set(conn: aiosqlite.Connection) -> None:
    await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)

    cursor = await conn.execute("PRAGMA foreign_keys")
    assert (await cursor.fetchone())[0] == 1
    cursor = await conn.execute("PRAGMA journal_mode")
    assert (await cursor.fetchone())[0].lower() == "wal"
    cursor = await conn.execute("PRAGMA busy_timeout")
    assert (await cursor.fetchone())[0] == 5000


async def test_notes_fts_trigger_sync_via_migrated_schema(conn: aiosqlite.Connection) -> None:
    await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)

    await conn.execute("BEGIN")
    await conn.execute(
        "INSERT INTO notes (path, title, origin, content_hash, created_at, updated_at) "
        "VALUES ('a.md', 'A', 'human', 'h1', 't', 't')"
    )
    await conn.execute("INSERT INTO tags (name, display_name) VALUES ('rag', 'RAG')")
    await conn.execute("INSERT INTO note_tags (note_id, tag_id) VALUES (1, 1)")
    await conn.commit()

    cursor = await conn.execute("SELECT tags_text FROM notes WHERE id = 1")
    assert (await cursor.fetchone())[0] == "rag"
    cursor = await conn.execute("SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'rag'")
    assert await cursor.fetchall() == [(1,)]


async def test_secret_scan_columns_and_tables_present(conn: aiosqlite.Connection) -> None:
    await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)

    await conn.execute("BEGIN")
    await conn.execute(
        "INSERT INTO notes (path, title, origin, content_hash, created_at, updated_at) "
        "VALUES ('a.md', 'A', 'human', 'h1', 't', 't')"
    )
    await conn.commit()
    cursor = await conn.execute("SELECT secret_scan_status FROM notes WHERE id = 1")
    assert (await cursor.fetchone())[0] == "clean"


async def test_tampered_applied_migration_is_detected(
    conn: aiosqlite.Connection, tmp_path: Path
) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_ok.sql").write_text(_SCHEMA_MIGRATIONS_DDL + "CREATE TABLE t1 (id INTEGER);")
    await apply_pending_migrations(conn, mig_dir)

    (mig_dir / "0001_ok.sql").write_text(
        _SCHEMA_MIGRATIONS_DDL + "CREATE TABLE t1 (id INTEGER); -- tampered"
    )

    with pytest.raises(MigrationChecksumMismatchError):
        await apply_pending_migrations(conn, mig_dir)


async def test_a_deleted_applied_migration_file_is_detected(
    conn: aiosqlite.Connection, tmp_path: Path
) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_ok.sql").write_text(_SCHEMA_MIGRATIONS_DDL)
    await apply_pending_migrations(conn, mig_dir)
    (mig_dir / "0001_ok.sql").unlink()

    with pytest.raises(MigrationChecksumMismatchError):
        await apply_pending_migrations(conn, mig_dir)


async def test_failed_migration_rolls_back_and_leaves_user_version_unchanged(
    conn: aiosqlite.Connection, tmp_path: Path
) -> None:
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_init.sql").write_text(_SCHEMA_MIGRATIONS_DDL)
    (mig_dir / "0002_bad.sql").write_text(
        "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\nCREATE TABLE t2 (id INTEGER PRIMARY KEY);"
    )

    with pytest.raises(MigrationError):
        await apply_pending_migrations(conn, mig_dir)

    cursor = await conn.execute("PRAGMA user_version")
    assert (await cursor.fetchone())[0] == 1
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert [row[0] for row in await cursor.fetchall()] == ["schema_migrations"]

    # The failure is not fatal to the runner itself -- fixing the file and re-running succeeds.
    (mig_dir / "0002_bad.sql").write_text("CREATE TABLE t2 (id INTEGER PRIMARY KEY);")
    records = await apply_pending_migrations(conn, mig_dir)
    assert [r.version for r in records] == [1, 2]
