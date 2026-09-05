"""Repository functions for the `notes` table (docs/DATA_MODEL.md §2.2).

Per docs/design/migration-runner-and-vault-ingestion.md §2.2/§3.2: thin,
parameterized-SQL wrappers used only by the vault ingestion pipeline. No
business logic (provenance inference, secret scanning, etc.) lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

_COLUMNS = (
    "id, path, title, origin, provider, model, folder, status, confidence, "
    "content_hash, chunk_count, tags_text, created_at, updated_at, "
    "last_indexed_at, deleted_at, secret_scan_status, index_state, last_index_error"
)


@dataclass(frozen=True)
class NoteRow:
    id: int
    path: str
    title: str
    origin: str
    provider: str | None
    model: str | None
    folder: str | None
    status: str
    confidence: float | None
    content_hash: str
    chunk_count: int
    tags_text: str
    created_at: str
    updated_at: str
    last_indexed_at: str | None
    deleted_at: str | None
    secret_scan_status: str
    index_state: str
    last_index_error: str | None


def _row_to_note(row: Any) -> NoteRow:
    return NoteRow(
        id=row[0],
        path=row[1],
        title=row[2],
        origin=row[3],
        provider=row[4],
        model=row[5],
        folder=row[6],
        status=row[7],
        confidence=row[8],
        content_hash=row[9],
        chunk_count=row[10],
        tags_text=row[11],
        created_at=row[12],
        updated_at=row[13],
        last_indexed_at=row[14],
        deleted_at=row[15],
        secret_scan_status=row[16],
        index_state=row[17],
        last_index_error=row[18],
    )


async def get_by_path(conn: aiosqlite.Connection, path: str) -> NoteRow | None:
    # _COLUMNS is a fixed, module-private literal (never user input); the value
    # parameter is bound, not interpolated. noqa: S608 -- false positive.
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM notes WHERE path = ?", (path,)  # noqa: S608
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_note(row)


async def get_by_id(conn: aiosqlite.Connection, note_id: int) -> NoteRow | None:
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM notes WHERE id = ?", (note_id,)  # noqa: S608
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_note(row)


async def insert(
    conn: aiosqlite.Connection,
    *,
    path: str,
    title: str,
    origin: str,
    provider: str | None,
    folder: str | None,
    content_hash: str,
    created_at: str,
    status: str | None = None,
) -> int:
    """Insert a new note row and return its new id.

    `status` defaults to `None`, meaning "let the notes table's own
    DEFAULT 'draft' apply" -- an explicit column list that omits `status`
    when unset, rather than passing the literal string 'draft', keeps this
    function from silently duplicating a policy decision that belongs to
    the DDL (docs/DATA_MODEL.md §2.2).
    """
    if status is None:
        cursor = await conn.execute(
            "INSERT INTO notes (path, title, origin, provider, folder, "
            "content_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (path, title, origin, provider, folder, content_hash, created_at, created_at),
        )
    else:
        cursor = await conn.execute(
            "INSERT INTO notes (path, title, origin, provider, folder, "
            "content_hash, created_at, updated_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path, title, origin, provider, folder, content_hash, created_at, created_at, status),
        )
    await conn.commit()
    note_id = cursor.lastrowid
    if note_id is None:
        raise RuntimeError("INSERT INTO notes did not yield a rowid")
    return note_id


async def update_content(
    conn: aiosqlite.Connection, note_id: int, *, content_hash: str, updated_at: str
) -> None:
    await conn.execute(
        "UPDATE notes SET content_hash = ?, updated_at = ? WHERE id = ?",
        (content_hash, updated_at, note_id),
    )
    await conn.commit()


async def move(
    conn: aiosqlite.Connection, note_id: int, *, new_path: str, updated_at: str
) -> None:
    await conn.execute(
        "UPDATE notes SET path = ?, updated_at = ? WHERE id = ?",
        (new_path, updated_at, note_id),
    )
    await conn.commit()


async def update_status(
    conn: aiosqlite.Connection, note_id: int, *, status: str, updated_at: str
) -> None:
    await conn.execute(
        "UPDATE notes SET status = ?, updated_at = ? WHERE id = ?",
        (status, updated_at, note_id),
    )
    await conn.commit()


async def soft_delete(conn: aiosqlite.Connection, note_id: int, *, deleted_at: str) -> None:
    await conn.execute(
        "UPDATE notes SET deleted_at = ? WHERE id = ?",
        (deleted_at, note_id),
    )
    await conn.commit()


async def update_secret_scan_status(
    conn: aiosqlite.Connection, note_id: int, *, secret_scan_status: str
) -> None:
    await conn.execute(
        "UPDATE notes SET secret_scan_status = ? WHERE id = ?",
        (secret_scan_status, note_id),
    )
    await conn.commit()


async def mark_indexed(
    conn: aiosqlite.Connection, note_id: int, *, chunk_count: int, indexed_at: str
) -> None:
    """The success-path commit marker for docs/design/indexing-pipeline.md
    §2.5 step 8 -- sets index_state='current', clears any prior
    last_index_error, and records the new chunk_count/last_indexed_at
    together in one write."""
    await conn.execute(
        "UPDATE notes SET index_state = 'current', last_index_error = NULL, "
        "chunk_count = ?, last_indexed_at = ? WHERE id = ?",
        (chunk_count, indexed_at, note_id),
    )
    await conn.commit()


async def mark_index_failed(conn: aiosqlite.Connection, note_id: int, *, error: str) -> None:
    await conn.execute(
        "UPDATE notes SET index_state = 'failed', last_index_error = ? WHERE id = ?",
        (error, note_id),
    )
    await conn.commit()


async def list_ids_needing_index(conn: aiosqlite.Connection) -> list[int]:
    """Every active note not currently indexed -- covers both never-indexed
    (the `'stale'` migration default) and previously-`'failed'` notes.
    Feeds `athena index bootstrap` (design doc §6)."""
    cursor = await conn.execute(
        "SELECT id FROM notes WHERE deleted_at IS NULL AND index_state != 'current'"
    )
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def list_active_paths(conn: aiosqlite.Connection) -> set[str]:
    """Every `path` for a currently-active (`deleted_at IS NULL`) note.

    Used by reconciliation (design doc §2.7) to detect notes recorded as
    active whose file has vanished from disk entirely.
    """
    cursor = await conn.execute("SELECT path FROM notes WHERE deleted_at IS NULL")
    rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def find_by_content_hash(conn: aiosqlite.Connection, content_hash: str) -> list[NoteRow]:
    """Return every active (`deleted_at IS NULL`) note with this content hash.

    Filters out soft-deleted notes because this is used for move detection
    (design doc §2.5) against currently-live notes only -- a tombstoned
    note's stale hash match would misclassify a genuine new note as a move.
    """
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM notes WHERE content_hash = ? AND deleted_at IS NULL",  # noqa: S608
        (content_hash,),
    )
    rows = await cursor.fetchall()
    return [_row_to_note(row) for row in rows]


async def list_active(conn: aiosqlite.Connection) -> list[NoteRow]:
    """Every currently-active (`deleted_at IS NULL`) note, full rows.

    Feeds duplicate-detection scans (docs/design/knowledge-intelligence.md
    §2.1), which need each note's `content_hash`/`title`/`path` to compute
    the exact/lexical/metadata signals -- unlike `list_active_paths`, which
    only ever needed the path.
    """
    cursor = await conn.execute(f"SELECT {_COLUMNS} FROM notes WHERE deleted_at IS NULL")  # noqa: S608
    rows = await cursor.fetchall()
    return [_row_to_note(row) for row in rows]


async def list_stale_candidates(conn: aiosqlite.Connection, *, cutoff: str) -> list[NoteRow]:
    """Active notes in `'active'`/`'verified'` status last updated before
    `cutoff` (an ISO-8601 string, compared lexicographically like every
    other timestamp column in this schema) -- feeds the stale-sweep job
    (docs/design/knowledge-intelligence.md §2.5). Excludes notes already
    `'stale'`/`'superseded'`/`'archived'`/`'draft'` -- promoting a note to
    `'stale'` only makes sense starting from `'active'` or `'verified'`.
    """
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM notes WHERE deleted_at IS NULL "  # noqa: S608
        "AND status IN ('active', 'verified') AND updated_at < ?",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return [_row_to_note(row) for row in rows]


async def count_active(conn: aiosqlite.Connection) -> int:
    """Total currently-active (`deleted_at IS NULL`) note count -- feeds
    `vault_status` (docs/design/mcp-server.md §2.1), a cheap `COUNT(*)`
    rather than `len(list_active(...))` since the caller has no other use
    for the full rows.
    """
    cursor = await conn.execute("SELECT COUNT(*) FROM notes WHERE deleted_at IS NULL")
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("SELECT COUNT(*) did not yield a row")
    return int(row[0])
