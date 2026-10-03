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

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

_COLUMNS = (
    "id, huey_task_id, job_type, status, created_at, started_at, finished_at, error_message, "
    "draft_title, draft_body, draft_source_urls"
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
    draft_title: str | None
    draft_body: str | None
    draft_source_urls: list[str] | None


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
        draft_title=row[8],
        draft_body=row[9],
        draft_source_urls=json.loads(row[10]) if row[10] is not None else None,
    )


async def _insert_without_commit(
    conn: aiosqlite.Connection,
    *,
    huey_task_id: str,
    job_type: str,
    created_at: str,
    query: str | None = None,
    requested_by: str | None = None,
) -> int:
    """Shared INSERT logic for `insert()` and `reserve_dispatch_slot()` --
    issues the INSERT but deliberately does not commit, so `insert()` can
    commit standalone (its existing public contract) while
    `reserve_dispatch_slot()` can fold the same INSERT into its own
    surrounding `BEGIN IMMEDIATE`/`COMMIT` transaction instead."""
    cursor = await conn.execute(
        "INSERT INTO research_jobs (huey_task_id, job_type, query, requested_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (huey_task_id, job_type, query, requested_by, created_at),
    )
    job_id = cursor.lastrowid
    if job_id is None:
        raise RuntimeError("INSERT INTO research_jobs did not yield a rowid")
    return job_id


async def insert(
    conn: aiosqlite.Connection,
    *,
    huey_task_id: str,
    job_type: str,
    created_at: str,
    query: str | None = None,
    requested_by: str | None = None,
) -> int:
    job_id = await _insert_without_commit(
        conn,
        huey_task_id=huey_task_id,
        job_type=job_type,
        created_at=created_at,
        query=query,
        requested_by=requested_by,
    )
    await conn.commit()
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


async def record_draft(
    conn: aiosqlite.Connection,
    job_id: int,
    *,
    draft_title: str,
    draft_body: str,
    draft_source_urls: list[str],
) -> None:
    """Store a completed `research_task`'s draft on its job row (design doc
    §2.4) -- the draft lives here, keyed by `job_id`, until `research_commit`
    reads it back. Does not itself change `status`/`finished_at`; the task
    calls `mark_finished` separately, matching every other task's own
    bookkeeping split."""
    await conn.execute(
        "UPDATE research_jobs SET draft_title = ?, draft_body = ?, draft_source_urls = ? "
        "WHERE id = ?",
        (draft_title, draft_body, json.dumps(draft_source_urls), job_id),
    )
    await conn.commit()


async def record_result_note(conn: aiosqlite.Connection, job_id: int, note_id: int) -> None:
    """Set `result_note_id` on an already-`succeeded` job, without touching
    `status`/`finished_at`/`error_message` -- distinct from `mark_finished`,
    which is the task-completion bookkeeping call and requires a terminal
    status. `research_commit` calls this after `research_task` has already
    completed the job; repurposing `mark_finished` here would need to
    re-derive values it has no business re-deciding."""
    await conn.execute(
        "UPDATE research_jobs SET result_note_id = ? WHERE id = ?", (note_id, job_id)
    )
    await conn.commit()


async def get_by_id(conn: aiosqlite.Connection, job_id: int) -> ResearchJobRow | None:
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM research_jobs WHERE id = ?",  # noqa: S608
        (job_id,),
    )
    row = await cursor.fetchone()
    return _row_to_job(row) if row is not None else None


async def get_by_huey_task_id(
    conn: aiosqlite.Connection, huey_task_id: str
) -> ResearchJobRow | None:
    """`research_task` (design doc §2.4) is dispatched before its own
    `research_jobs` row exists (the row's id isn't known until after
    dispatch returns a huey task id, mirroring `duplicates_scan`/
    `reindex_start`'s existing insert-after-dispatch order in
    `athena.mcp_server.job_tools`) -- so the task looks itself up by its own
    huey-assigned id (via `@huey.task(context=True)`'s injected `task.id`)
    rather than being passed a `job_id` argument it cannot yet have."""
    cursor = await conn.execute(
        f"SELECT {_COLUMNS} FROM research_jobs WHERE huey_task_id = ?",  # noqa: S608
        (huey_task_id,),
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


class DispatchLimitError(Exception):
    """`job_type` has already reached its configured daily dispatch
    ceiling (docs/design/production-hardening.md §2.2, generalizing
    `athena.llm.summarize`'s identical daily-call-ceiling pattern from
    Phase 9 to close `SECURITY_MODEL.md` TB-1's DoS-via-unbounded-
    dispatch finding for `research_start`/`reindex_start`)."""


async def count_dispatched_today(conn: aiosqlite.Connection, *, job_type: str) -> int:
    today_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM research_jobs WHERE job_type = ? AND created_at LIKE ?",
        (job_type, f"{today_prefix}%"),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def check_daily_dispatch_limit(
    conn: aiosqlite.Connection, *, job_type: str, max_per_day: int
) -> None:
    """Raises `DispatchLimitError` if `job_type` is already at its daily
    ceiling -- called by dispatching tools/CLI commands **before**
    enqueueing, so a refusal never itself contributes to the DoS surface
    it guards against (the same "check before, not after" discipline
    `athena.llm.summarize.summarize_text`'s call-ceiling check already
    established). Deliberately transport-agnostic -- lives in the
    repository layer so both the MCP tool (`research_tools.py`/
    `job_tools.py`) and the CLI (`athena research start`, which dispatches
    independently of the MCP tool layer) share exactly one check, never two
    subtly-different ones.
    """
    if await count_dispatched_today(conn, job_type=job_type) >= max_per_day:
        raise DispatchLimitError(
            f"daily dispatch limit ({max_per_day}) reached for job_type={job_type!r}"
        )


async def reserve_dispatch_slot(
    conn: aiosqlite.Connection,
    *,
    job_type: str,
    max_per_day: int,
    huey_task_id: str,
    created_at: str,
    query: str | None = None,
    requested_by: str | None = None,
) -> int:
    """Atomic replacement for "`check_daily_dispatch_limit` then `insert`"
    (docs/design/production-hardening.md §2.2's dispatch-ceiling mitigation)
    -- closes the check-then-act race (CWE-367) the pair had when run as two
    separate statements, each on its own connection: `check_daily_dispatch_
    limit`'s `SELECT COUNT(*)` and the later `INSERT` could both read the
    same stale count under concurrent callers, letting more than
    `max_per_day` dispatches through (confirmed: 8 concurrent calls against
    a ceiling of 1 let 2-8 through across repeated runs before this fix).

    Issues `BEGIN IMMEDIATE` first -- unlike SQLite's default deferred
    `BEGIN`, this takes a RESERVED write lock immediately, so a second
    concurrent caller blocks (up to `busy_timeout`) on ITS OWN `BEGIN
    IMMEDIATE` until the first caller's transaction commits or rolls back,
    rather than both callers independently reading the pre-dispatch count.
    Every reader and writer of `research_jobs` within the ceiling check must
    go through this one write-locking entry point to actually close the
    race; the plain `count_dispatched_today`-based `check_daily_dispatch_
    limit` above still exists for callers that only need an advisory/
    reporting count, not an enforced reservation.

    Raises `DispatchLimitError` (after a clean `ROLLBACK`) if `job_type` is
    already at `max_per_day`; otherwise inserts the new `research_jobs` row
    and commits, returning its id -- the same row `insert()` would have
    produced, just atomically reserved against the count check.
    """
    await conn.execute("BEGIN IMMEDIATE")
    try:
        if await count_dispatched_today(conn, job_type=job_type) >= max_per_day:
            raise DispatchLimitError(
                f"daily dispatch limit ({max_per_day}) reached for job_type={job_type!r}"
            )
        job_id = await _insert_without_commit(
            conn,
            huey_task_id=huey_task_id,
            job_type=job_type,
            created_at=created_at,
            query=query,
            requested_by=requested_by,
        )
    except BaseException:
        await conn.execute("ROLLBACK")
        raise
    else:
        await conn.execute("COMMIT")
        return job_id
