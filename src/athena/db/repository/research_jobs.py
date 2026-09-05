"""Repository functions for `research_jobs` (docs/DATA_MODEL.md §2.7).

Per the design doc §1, this task's scope covers only `job_type='ingestion'`
usage -- the functions themselves are generic over `job_type` since the DDL
is, but no research/duplicate-scan business logic is implemented here.

`get_by_id`/`mark_cancelled`/`count_by_status` (docs/design/mcp-server.md
§2.2) extend this for the MCP server's `job_status`/`job_cancel` interim
tasks-shim -- `research_jobs` (already tracking `status` and linked to
`huey_task_id`) is the source of truth for job state, not a fresh query
against Huey's own storage, which isn't guaranteed queryable independent of
whether the worker process is running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

_COLUMNS = (
    "id, huey_task_id, job_type, status, created_at, started_at, finished_at, error_message"
)


@dataclass(frozen=True)
class ResearchJobRow:
    id: int
    huey_task_id: str
    job_type: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    error_message: str | None


def _row_to_job(row: Any) -> ResearchJobRow:
    return ResearchJobRow(
        id=row[0],
        huey_task_id=row[1],
        job_type=row[2],
        status=row[3],
        created_at=row[4],
        started_at=row[5],
        finished_at=row[6],
        error_message=row[7],
    )


async def insert(
    conn: aiosqlite.Connection,
    *,
    huey_task_id: str,
    job_type: str,
    created_at: str,
    query: str | None = None,
    requested_by: str | None = None,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO research_jobs (huey_task_id, job_type, query, requested_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (huey_task_id, job_type, query, requested_by, created_at),
    )
    await conn.commit()
    job_id = cursor.lastrowid
    if job_id is None:
        raise RuntimeError("INSERT INTO research_jobs did not yield a rowid")
    return job_id


async def mark_started(conn: aiosqlite.Connection, job_id: int, started_at: str) -> None:
    await conn.execute(
        "UPDATE research_jobs SET status = 'running', started_at = ? WHERE id = ?",
        (started_at, job_id),
    )
    await conn.commit()


async def mark_finished(
    conn: aiosqlite.Connection,
    job_id: int,
    *,
    status: str,
    finished_at: str,
    error_message: str | None = None,
    result_note_id: int | None = None,
) -> None:
    if status not in _TERMINAL_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_TERMINAL_STATUSES)}, got {status!r}"
        )
    await conn.execute(
        "UPDATE research_jobs SET status = ?, finished_at = ?, error_message = ?, "
        "result_note_id = ? WHERE id = ?",
        (status, finished_at, error_message, result_note_id, job_id),
    )
    await conn.commit()


async def get_by_id(conn: aiosqlite.Connection, job_id: int) -> ResearchJobRow | None:
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM research_jobs WHERE id = ?",  # noqa: S608
        (job_id,),
    )
    row = await cursor.fetchone()
    return _row_to_job(row) if row is not None else None


async def mark_cancelled(conn: aiosqlite.Connection, job_id: int, *, cancelled_at: str) -> None:
    await conn.execute(
        "UPDATE research_jobs SET status = 'cancelled', finished_at = ? WHERE id = ?",
        (cancelled_at, job_id),
    )
    await conn.commit()


async def count_by_status(conn: aiosqlite.Connection) -> dict[str, int]:
    cursor = await conn.execute("SELECT status, COUNT(*) FROM research_jobs GROUP BY status")
    rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}
