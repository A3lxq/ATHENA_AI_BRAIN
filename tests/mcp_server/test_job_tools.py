"""Tests for `athena.mcp_server.job_tools` (docs/design/mcp-server.md §2.2/§2.3).

`duplicates_scan`/`reindex_start`/`job_status`/`job_cancel` all read
`athena.mcp_server._runtime.config` and dispatch through `athena.worker`, so
every test here needs both: a real, freshly-imported `athena.worker` module
(matching `tests/test_worker.py`'s `_fresh_worker_module` pattern -- a real
Huey instance keyed off monkeypatched `ATHENA_DATA_DIR`/`ATHENA_HUEY_SECRET`
env vars, imported fresh via `sys.modules.pop` since `athena.worker` builds
its module-level `huey` at import time), and `_runtime.config` monkeypatched
to point at the same `db_path`/`data_dir` so `job_tools`'s own `open_
connection(_runtime.config.db_path)` calls see the same database the fresh
worker module just migrated.

`job_tools` looks up `athena.worker.<name>` and `_runtime.config` via
late-bound attribute access rather than `from ... import ...` specifically
so this swap works -- see `job_tools.py`'s own module docstring.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from types import ModuleType

import aiosqlite
import pytest

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations

# `job_tools.py` imports `athena.worker` eagerly at module scope (by design
# -- see its own module docstring), and `athena.worker` builds a real Huey
# instance (`build_huey`) at ITS module scope too, which hard-fails without
# `ATHENA_HUEY_SECRET` set. That makes `athena.worker` (and transitively
# `job_tools`) unimportable at collection time unless some secret/data-dir
# is already in the environment. This placeholder import only has to
# succeed once, here, at collection time -- every test below immediately
# replaces it with a freshly-imported `athena.worker` module pointed at its
# own `tmp_path` via the `worker` fixture, which `job_tools` then picks up
# through its own late-bound `athena.worker.<name>` attribute access.
os.environ.setdefault("ATHENA_HUEY_SECRET", "collection-time-placeholder-secret")  # noqa: S105
os.environ.setdefault("ATHENA_DATA_DIR", tempfile.mkdtemp())

from athena.mcp_server import _runtime, job_tools  # noqa: E402


def _fresh_worker_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, vault_dir: Path | None = None
) -> ModuleType:
    monkeypatch.setenv("ATHENA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ATHENA_HUEY_SECRET", "a-real-test-secret")  # noqa: S105 -- test fixture
    if vault_dir is not None:
        monkeypatch.setenv("ATHENA_VAULT_DIR", str(vault_dir))
    else:
        monkeypatch.delenv("ATHENA_VAULT_DIR", raising=False)

    sys.modules.pop("athena.worker", None)
    worker = import_module("athena.worker")

    async def _migrate() -> None:
        conn = await aiosqlite.connect(worker._config.db_path)
        try:
            await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)
        finally:
            await conn.close()

    asyncio.run(_migrate())
    return worker


@pytest.fixture
def worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """A freshly-imported `athena.worker` module, with `_runtime.config`
    monkeypatched to the same config so `job_tools` reads/writes the same
    `research_jobs` database this fixture just migrated."""
    module = _fresh_worker_module(tmp_path, monkeypatch)
    monkeypatch.setattr(_runtime, "config", module._config)
    return module


async def _insert_job(worker: ModuleType, *, status: str) -> int:
    """Insert a `research_jobs` row directly via the repository, then move
    it to `status` by calling the same repository functions `job_tools`
    itself would eventually call -- avoids needing a running Huey consumer
    to exercise `job_status`'s status-mapping for every terminal state."""
    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo

    async with open_connection(worker._config.db_path) as conn:
        job_id = await research_jobs_repo.insert(
            conn,
            huey_task_id="fake-task-id",
            job_type="duplicates_scan",
            created_at="2026-09-05T00:00:00+00:00",
        )
        if status == "queued":
            pass
        elif status == "running":
            await research_jobs_repo.mark_started(
                conn, job_id, started_at="2026-09-05T00:01:00+00:00"
            )
        elif status in {"succeeded", "failed"}:
            await research_jobs_repo.mark_finished(
                conn,
                job_id,
                status=status,
                finished_at="2026-09-05T00:02:00+00:00",
                error_message="boom" if status == "failed" else None,
            )
        elif status == "cancelled":
            await research_jobs_repo.mark_cancelled(
                conn, job_id, cancelled_at="2026-09-05T00:02:00+00:00"
            )
        else:
            raise ValueError(status)
        return job_id


async def test_duplicates_scan_creates_a_research_job_row_and_returns_a_lookup_id(
    worker: ModuleType,
) -> None:
    response = await job_tools.duplicates_scan()

    assert "job dispatched" in response
    job_id = int(response.rsplit("=", 1)[1])

    status_response = await job_tools.job_status(job_id)
    assert f"job_id={job_id}" in status_response
    assert "job_type=duplicates_scan" in status_response
    assert "status=working" in status_response  # freshly enqueued -> 'queued' -> 'working'


async def test_reindex_start_creates_a_research_job_row_and_returns_a_lookup_id(
    worker: ModuleType,
) -> None:
    response = await job_tools.reindex_start(note_id=None)

    assert "job dispatched" in response
    job_id = int(response.rsplit("=", 1)[1])

    status_response = await job_tools.job_status(job_id)
    assert f"job_id={job_id}" in status_response
    assert "job_type=reindex_start" in status_response
    assert "status=working" in status_response


async def test_reindex_start_accepts_a_specific_note_id(worker: ModuleType) -> None:
    response = await job_tools.reindex_start(note_id=42)

    assert "job dispatched" in response


async def test_reindex_start_refuses_cleanly_at_the_daily_dispatch_ceiling(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo

    # AthenaConfig is a frozen dataclass -- swap in a replacement `_runtime.config`
    # with the ceiling forced to 0, rather than mutating the existing instance.
    monkeypatch.setattr(
        _runtime, "config", dataclasses.replace(worker._config, reindex_max_dispatches_per_day=0)
    )

    async with open_connection(worker._config.db_path) as conn:
        before = await research_jobs_repo.count_dispatched_today(conn, job_type="reindex_start")

    response = await job_tools.reindex_start(note_id=None)

    assert "daily dispatch limit" in response

    async with open_connection(worker._config.db_path) as conn:
        after = await research_jobs_repo.count_dispatched_today(conn, job_type="reindex_start")
    assert after == before


async def test_job_status_on_a_nonexistent_job_returns_a_clear_message_not_an_exception(
    worker: ModuleType,
) -> None:
    response = await job_tools.job_status(999999)

    assert "no such job" in response
    assert "999999" in response


@pytest.mark.parametrize(
    ("research_jobs_status", "expected_tasks_vocab"),
    [
        ("queued", "working"),
        ("running", "working"),
        ("succeeded", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
async def test_job_status_maps_research_jobs_status_to_tasks_extension_vocabulary(
    worker: ModuleType, research_jobs_status: str, expected_tasks_vocab: str
) -> None:
    job_id = await _insert_job(worker, status=research_jobs_status)

    response = await job_tools.job_status(job_id)

    assert f"status={expected_tasks_vocab}" in response
    assert f"raw_status={research_jobs_status}" in response


async def test_job_status_surfaces_the_error_message_for_a_failed_job(worker: ModuleType) -> None:
    job_id = await _insert_job(worker, status="failed")

    response = await job_tools.job_status(job_id)

    assert "boom" in response


async def test_job_cancel_marks_an_existing_job_cancelled_in_the_db(worker: ModuleType) -> None:
    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo

    dispatch_response = await job_tools.duplicates_scan()
    job_id = int(dispatch_response.rsplit("=", 1)[1])

    cancel_response = await job_tools.job_cancel(job_id)

    async with open_connection(worker._config.db_path) as conn:
        row = await research_jobs_repo.get_by_id(conn, job_id)
    assert row is not None
    assert row.status == "cancelled"

    # Per design doc §2.2/§6: the response must not imply an immediate,
    # guaranteed interruption of in-progress work -- only that revocation
    # prevents a future run and does not stop one already underway.
    assert "does not interrupt" in cancel_response
    assert "mid-execution" in cancel_response


async def test_job_cancel_on_a_nonexistent_job_returns_a_clear_message_not_an_exception(
    worker: ModuleType,
) -> None:
    response = await job_tools.job_cancel(999999)

    assert "no such job" in response
    assert "999999" in response


def test_register_applies_tool_annotations() -> None:
    from mcp.server import MCPServer

    mcp = MCPServer(name="test")

    job_tools.register(mcp)

    tool_names = {"duplicates_scan", "reindex_start", "job_status", "job_cancel"}
    registered = asyncio.run(mcp.list_tools())
    registered_by_name = {tool.name: tool for tool in registered}
    assert tool_names <= registered_by_name.keys()

    assert registered_by_name["job_status"].annotations is not None
    assert registered_by_name["job_status"].annotations.read_only_hint is True

    for name in ("duplicates_scan", "reindex_start", "job_cancel"):
        annotations = registered_by_name[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
