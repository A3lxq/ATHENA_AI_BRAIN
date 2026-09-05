"""Task-backed dispatch tools and the interim tasks-shim (docs/design/
mcp-server.md §2.2/§2.3).

`duplicates_scan`/`reindex_start` are thin dispatchers: they enqueue a Huey
job (`athena.worker.duplicates_scan_task`/`reindex_task`), record a
`research_jobs` row keyed to the real Huey task id, and return a job handle
immediately -- the underlying work happens independently of this call.

`job_status`/`job_cancel` mirror the vocabulary ADR-0007 chose to match the
eventual official `io.modelcontextprotocol/tasks` extension (`working`/
`completed`/`failed`/`cancelled`) against Huey's actual job semantics, using
`research_jobs` (already linked to `huey_task_id`) as the source of truth
rather than querying Huey's own storage directly -- the DB row is queryable
independent of whether the worker process is even running.

`athena.worker` is imported at module scope (not lazily, unlike `athena.
cli`'s convention for commands that don't always need it) -- dispatching to
it is this whole module's job. Every reference below is a late-bound
attribute access (`athena.worker.duplicates_scan_task(...)`, `athena.worker.
huey.revoke_by_id(...)`), never `from athena.worker import ...` -- this
lets tests swap in a freshly-imported worker module (matching `tests/
test_worker.py`'s `_fresh_worker_module` pattern, which does `sys.modules.
pop("athena.worker", None)` then re-imports) and have this module pick up
the swap automatically, since `athena.worker` is looked up as an attribute
of the `athena` package at call time, not bound once at import time.
Likewise `_runtime.config` is read fresh on every call rather than aliased,
so a test's `monkeypatch.setattr(_runtime, "config", ...)` takes effect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

import athena.worker
from athena.db.connection import open_connection
from athena.db.repository import research_jobs as research_jobs_repo
from athena.mcp_server import _runtime

__all__ = ["duplicates_scan", "reindex_start", "job_status", "job_cancel", "register"]

# ADR-0007's tasks-extension vocabulary. ATHENA AI-BRAIN has no
# `input_required` state today -- none of its task-backed operations pause
# for mid-job input.
_STATUS_TO_TASKS_VOCAB: dict[str, str] = {
    "queued": "working",
    "running": "working",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def duplicates_scan() -> str:
    """Dispatch a whole-vault duplicate scan as a background job.

    Returns a job handle immediately -- the scan itself runs asynchronously
    on the Huey worker. Call `job_status` with the returned job id to check
    progress, or `job_cancel` to cancel it. Not read-only (it has the side
    effect of enqueueing background work), but not destructive to vault
    content.
    """
    correlation_id = str(uuid4())
    result = athena.worker.duplicates_scan_task(correlation_id)
    async with open_connection(_runtime.config.db_path) as conn:
        job_id = await research_jobs_repo.insert(
            conn,
            huey_task_id=result.id,
            job_type="duplicates_scan",
            created_at=_now(),
        )
    return f"job dispatched: job_id={job_id}"


async def reindex_start(note_id: int | None = None) -> str:
    """Dispatch a re-index as a background job.

    If `note_id` is given, re-indexes just that note; otherwise re-runs the
    whole-vault index bootstrap pass. Returns a job handle immediately --
    call `job_status` with the returned job id to check progress. Not
    read-only (it has the side effect of enqueueing background work), but
    not destructive to vault content.
    """
    correlation_id = str(uuid4())
    result = athena.worker.reindex_task(note_id, correlation_id)
    async with open_connection(_runtime.config.db_path) as conn:
        job_id = await research_jobs_repo.insert(
            conn,
            huey_task_id=result.id,
            job_type="reindex_start",
            created_at=_now(),
        )
    return f"job dispatched: job_id={job_id}"


async def job_status(job_id: int) -> str:
    """Report the status of a previously-dispatched background job.

    Maps ATHENA AI-BRAIN's own `research_jobs` status vocabulary to the
    tasks-extension vocabulary (`queued`/`running` -> `working`,
    `succeeded` -> `completed`, `failed` -> `failed`, `cancelled` ->
    `cancelled`).
    """
    async with open_connection(_runtime.config.db_path) as conn:
        job = await research_jobs_repo.get_by_id(conn, job_id)
    if job is None:
        return f"no such job: job_id={job_id}"
    mapped_status = _STATUS_TO_TASKS_VOCAB.get(job.status, job.status)
    detail = f"; error={job.error_message}" if job.error_message else ""
    return (
        f"job_id={job_id} job_type={job.job_type} status={mapped_status}"
        f" (raw_status={job.status}){detail}"
    )


async def job_cancel(job_id: int) -> str:
    """Cancel a previously-dispatched background job.

    Revoking a job that has not started yet prevents it from ever running.
    Revoking a job that is already running does NOT interrupt it
    mid-execution -- this is Huey's own documented revocation behavior
    (prevents a future execution, not a kill signal), not a limitation of
    this tool. `job_status` will keep reporting the job's real outcome
    either way.
    """
    async with open_connection(_runtime.config.db_path) as conn:
        job = await research_jobs_repo.get_by_id(conn, job_id)
        if job is None:
            return f"no such job: job_id={job_id}"
        athena.worker.huey.revoke_by_id(job.huey_task_id)
        await research_jobs_repo.mark_cancelled(conn, job_id, cancelled_at=_now())
    return (
        f"job_id={job_id} cancelled: if it had not started yet, it will not run. "
        "If it was already running, revocation does not interrupt it mid-execution "
        "-- it will continue to completion; job_status reflects the real outcome."
    )


def register(mcp: MCPServer) -> None:
    """Register this module's tools on `mcp` (design doc §3). Annotations
    are informational client-side hints only (ADR-0007), never relied on
    for enforcement -- the real gate for `job_cancel` not implying an
    immediate stop is the response text above, not `destructive_hint`."""
    mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))(
        duplicates_scan
    )
    mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))(
        reindex_start
    )
    mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))(job_status)
    mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))(job_cancel)
