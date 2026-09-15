"""Real concurrent-connection tests for `SQLITE_BUSY`/`busy_timeout`
behavior (docs/design/production-hardening.md §2.5; docs/TESTING_STRATEGY.md's
"SQLite Repository Layer" Recovery row: "sustained SQLITE_BUSY contention
surfaces as a bounded, catchable error, tested with a timeout shorter than a
hang would take"; and its "Job Queue" Recovery row: "simulate SQLITE_BUSY and
assert busy_timeout-bounded retry, never an indefinite hang").

Two independent `aiosqlite` connections are opened against the SAME real
temp-file database via `athena.db.connection.open_connection` (never
`:memory:` -- `:memory:` databases are private per-connection and can never
produce real file-level lock contention), so both connections carry the
exact production pragmas (`athena/db/connection.py`'s `_MANDATORY_PRAGMAS`:
`journal_mode = WAL`, `foreign_keys = ON`, `busy_timeout = 5000`), confirmed
by direct inspection before writing these tests.

The write lock is acquired with an explicit `BEGIN IMMEDIATE` (SQLite
immediately takes a RESERVED lock rather than deferring until the first
write, giving deterministic contention) followed by the exact `INSERT`
statement `athena.db.repository.notes.insert` issues -- the same
execute()-then-commit() write shape every repository function in this
codebase uses (no repository function wraps multiple statements in one
transaction today, so holding the lock open across an `asyncio.sleep` is
the realistic way to construct a genuine conflicting concurrent writer, not
a contrived locking API). The contending write on the second connection
goes through the real, unmodified `notes_repo.insert` function.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiosqlite
import pytest

from athena.db.connection import open_connection
from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations
from athena.db.repository import notes as notes_repo

_BUSY_TIMEOUT_S = 5.0  # matches connection.py's `PRAGMA busy_timeout = 5000`


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "athena.db"
    async with open_connection(path) as conn:
        await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)
    return path


async def _begin_immediate_insert(conn: aiosqlite.Connection, *, path: str) -> None:
    """Acquire a real write lock (RESERVED, immediately, per `BEGIN
    IMMEDIATE`) and issue the same INSERT statement
    `notes_repo.insert` uses, WITHOUT committing -- leaves the lock held
    until the caller explicitly commits or rolls back."""
    await conn.execute("BEGIN IMMEDIATE")
    created_at = "2026-01-01T00:00:00+00:00"
    await conn.execute(
        "INSERT INTO notes (path, title, origin, provider, folder, "
        "content_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (path, "held", "human", None, None, "hash-held", created_at, created_at),
    )


async def test_second_writer_succeeds_once_first_writer_commits(db_path: Path) -> None:
    """Two real writers, genuine file-level contention: connection A holds
    a real write lock for 0.5s (well inside the 5000ms busy_timeout window)
    before committing; connection B's concurrent write via the real
    `notes_repo.insert` must be retried under the hood and succeed once A
    releases the lock -- proving `busy_timeout` lets two concurrent writers
    both eventually succeed, per TESTING_STRATEGY.md's "Job Queue" Recovery
    row, rather than the second writer failing immediately.
    """
    async with open_connection(db_path) as conn_a, open_connection(db_path) as conn_b:

        async def holder() -> None:
            await _begin_immediate_insert(conn_a, path="held-by-a.md")
            await asyncio.sleep(0.5)
            await conn_a.commit()

        async def contender() -> float:
            # Give the holder a head start so it wins the race for the lock.
            await asyncio.sleep(0.1)
            start = time.perf_counter()
            await notes_repo.insert(
                conn_b,
                path="written-by-b.md",
                title="b",
                origin="human",
                provider=None,
                folder=None,
                content_hash="hash-b",
                created_at="2026-01-01T00:00:00+00:00",
            )
            return time.perf_counter() - start

        _, elapsed = await asyncio.wait_for(
            asyncio.gather(holder(), contender()), timeout=_BUSY_TIMEOUT_S + 3.0
        )

        # B genuinely waited on the lock (not an instant, uncontended write)
        # but was unblocked well within the 5000ms busy_timeout window --
        # bounded, not indefinite.
        assert 0.3 < elapsed < _BUSY_TIMEOUT_S
        assert await notes_repo.get_by_path(conn_a, "held-by-a.md") is not None
        assert await notes_repo.get_by_path(conn_b, "written-by-b.md") is not None


async def test_sustained_contention_fails_bounded_by_busy_timeout_not_forever(
    db_path: Path,
) -> None:
    """Connection A holds the write lock past the entire `busy_timeout`
    window (never commits during the test). Connection B's real
    `notes_repo.insert` must not hang indefinitely waiting for it -- it
    must raise a catchable `sqlite3.OperationalError` ("database is
    locked") once its internal busy-timeout retry is exhausted, and the
    wait must be bounded at roughly the configured 5000ms, not unbounded.
    This is the other of the two outcomes docs/design/production-hardening.md
    §2.5 names as acceptable ("succeeds within the busy_timeout window ...
    or fails with a bounded wait") -- exercised here with a conflict that
    genuinely outlasts the window, per TESTING_STRATEGY.md's "sustained
    SQLITE_BUSY contention surfaces as a bounded, catchable error, tested
    with a timeout shorter than a hang would take."
    """
    async with open_connection(db_path) as conn_a, open_connection(db_path) as conn_b:
        await _begin_immediate_insert(conn_a, path="held-forever.md")
        try:
            start = time.perf_counter()
            with pytest.raises(aiosqlite.OperationalError, match="locked"):
                # A timeout well above busy_timeout proves this is a real
                # bounded failure, not this test's own timeout masking a hang.
                await asyncio.wait_for(
                    notes_repo.insert(
                        conn_b,
                        path="never-written-by-b.md",
                        title="b",
                        origin="human",
                        provider=None,
                        folder=None,
                        content_hash="hash-b",
                        created_at="2026-01-01T00:00:00+00:00",
                    ),
                    timeout=_BUSY_TIMEOUT_S + 3.0,
                )
            elapsed = time.perf_counter() - start
        finally:
            await conn_a.rollback()

        # Bounded at roughly busy_timeout (5000ms), with headroom for
        # scheduling jitter -- proving the wait is a specific, predictable
        # duration and not "however long it takes," while still confirming
        # it actually waited out the retry window rather than failing fast.
        assert _BUSY_TIMEOUT_S - 1.0 < elapsed < _BUSY_TIMEOUT_S + 2.0
        assert await notes_repo.get_by_path(conn_b, "never-written-by-b.md") is None
