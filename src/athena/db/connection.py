"""Connection lifecycle for ATHENA AI-BRAIN's metadata SQLite database.

Per docs/DATA_MODEL.md §1, every connection must set three pragmas -- they are
per-connection and reset on every new connection, so this is not one-time setup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from athena.hardening.permissions import ensure_private_file

_MANDATORY_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)


async def set_mandatory_pragmas(conn: aiosqlite.Connection) -> None:
    """Set the three pragmas docs/DATA_MODEL.md §1 requires on every connection."""
    for pragma in _MANDATORY_PRAGMAS:
        await conn.execute(pragma)


@asynccontextmanager
async def open_connection(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open one connection to `db_path`, with mandatory pragmas set, closed on exit.

    Per docs/design/migration-runner-and-vault-ingestion.md §3.3: no connection
    pool is introduced here (ADR-0004's own open question, deliberately deferred).
    Callers needing several operations in one transaction pass this connection to
    multiple repository calls rather than opening a connection per call.
    """
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    # `aiosqlite.connect` creates `db_path` via plain POSIX file creation if
    # it doesn't yet exist, so its mode is whatever the process umask leaves
    # (commonly 0644, not the 0600 docs/SECURITY_MODEL.md P0 item 5 and
    # docs/design/storage-runtime-hardening.md require). `ensure_private_file`
    # is idempotent (a no-op re-chmod if the file is already 0600), so this
    # runs on every connection, not just the first -- cheaper than tracking
    # "did we already fix this file" and immune to a TOCTOU gap that an
    # "if not already hardened" guard could introduce.
    ensure_private_file(db_path)
    try:
        await set_mandatory_pragmas(conn)
        yield conn
    finally:
        await conn.close()
