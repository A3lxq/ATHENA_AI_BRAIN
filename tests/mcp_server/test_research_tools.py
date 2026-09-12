"""Tests for `athena.mcp_server.research_tools` (docs/design/
research-ingestion.md §2.5/§7).

Mirrors `test_job_tools.py`'s `_fresh_worker_module` pattern (a real
`athena.worker` module dispatches `research_task` against a real Huey
instance) combined with `test_mutation_tools.py`'s `_FakeElicitContext`
pattern (a duck-typed `ctx.elicit()` stand-in, since `research_commit`'s
MRTR gate is the same "accept is necessary but not sufficient" structure
`note_delete`/`note_merge` already established).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

import aiosqlite
import pytest
from huey import SqliteHuey
from mcp.server.mcpserver import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from qdrant_client import QdrantClient

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations
from athena.indexing.qdrant_store import ensure_collection
from athena.safety.paths import VaultRoot

os.environ.setdefault("ATHENA_HUEY_SECRET", "collection-time-placeholder-secret")  # noqa: S105
os.environ.setdefault("ATHENA_DATA_DIR", tempfile.mkdtemp())

from athena.mcp_server import _runtime, research_tools  # noqa: E402


class _FakeElicitContext:
    def __init__(
        self, result: AcceptedElicitation[Any] | DeclinedElicitation | CancelledElicitation
    ) -> None:
        self._result = result

    async def elicit(self, *, message: str, schema: type[Any]) -> Any:
        return self._result


def _fresh_worker_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, vault_dir: Path
) -> ModuleType:
    monkeypatch.setenv("ATHENA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ATHENA_HUEY_SECRET", "a-real-test-secret")  # noqa: S105 -- test fixture
    monkeypatch.setenv("ATHENA_VAULT_DIR", str(vault_dir))

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
def vault_dir(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.fixture
def vault_root(vault_dir: Path) -> VaultRoot:
    return VaultRoot.initialize(vault_dir)


@pytest.fixture
def worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vault_dir: Path, vault_root: VaultRoot
) -> ModuleType:
    """A freshly-imported `athena.worker` module, with `_runtime` patched to
    read/write the same database and vault this fixture just set up --
    matching `test_job_tools.py`'s `worker` fixture, plus the
    vault-root/Qdrant-client patches `test_mutation_tools.py`'s
    `_patch_runtime` applies, since `research_commit` (unlike
    `job_tools`'s dispatch-only tools) needs both.
    """
    module = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)
    monkeypatch.setattr(_runtime, "config", module._config)
    monkeypatch.setattr(_runtime, "require_vault_root", lambda: vault_root)

    huey = SqliteHuey(name="athena-test", filename=str(tmp_path / "test-huey.db"))
    qdrant_client = QdrantClient(":memory:")
    ensure_collection(qdrant_client, huey)
    monkeypatch.setattr(_runtime, "get_qdrant_client", lambda: qdrant_client)

    return module


async def _make_completed_job(
    worker: ModuleType, *, title: str, body: str, urls: list[str] | None = None
) -> int:
    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo

    async with open_connection(worker._config.db_path) as conn:
        job_id = await research_jobs_repo.insert(
            conn,
            huey_task_id=f"fake-task-{title}",
            job_type="research_start",
            query=title,
            created_at="2026-09-10T00:00:00+00:00",
        )
        await research_jobs_repo.record_draft(
            conn, job_id, draft_title=title, draft_body=body, draft_source_urls=urls or []
        )
        await research_jobs_repo.mark_finished(
            conn, job_id, status="succeeded", finished_at="2026-09-10T00:01:00+00:00"
        )
    return job_id


# --- research_start ---------------------------------------------------


async def test_research_start_creates_a_research_job_row_and_returns_a_lookup_id(
    worker: ModuleType,
) -> None:
    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo

    response = await research_tools.research_start(["https://a.example/"], topic="My Topic")

    assert "job dispatched" in response
    job_id = int(response.rsplit("=", 1)[1])

    async with open_connection(worker._config.db_path) as conn:
        row = await research_jobs_repo.get_by_id(conn, job_id)
    assert row is not None
    assert row.job_type == "research_start"
    assert row.status == "queued"


# --- research_commit: dry_run default and preview ----------------------


async def test_research_commit_defaults_to_dry_run_true(
    worker: ModuleType, vault_dir: Path
) -> None:
    job_id = await _make_completed_job(worker, title="Preview Topic", body="preview body")

    ctx = _FakeElicitContext(CancelledElicitation())  # never consulted for dry_run=True
    response = await research_tools.research_commit(job_id, ctx)

    assert "[dry run]" in response
    assert list(vault_dir.rglob("*.md")) == []


async def test_research_commit_no_such_job(worker: ModuleType) -> None:
    ctx = _FakeElicitContext(CancelledElicitation())

    response = await research_tools.research_commit(999_999, ctx)

    assert "no such job" in response


async def test_research_commit_job_with_no_draft_yet(worker: ModuleType) -> None:
    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo

    async with open_connection(worker._config.db_path) as conn:
        job_id = await research_jobs_repo.insert(
            conn,
            huey_task_id="fake-task-nodraft",
            job_type="research_start",
            created_at="2026-09-10T00:00:00+00:00",
        )

    ctx = _FakeElicitContext(CancelledElicitation())
    response = await research_tools.research_commit(job_id, ctx)

    assert "no draft yet" in response


# --- research_commit: MRTR gate -----------------------------------------


async def test_research_commit_non_dry_run_requires_confirmation(
    worker: ModuleType, vault_dir: Path
) -> None:
    job_id = await _make_completed_job(worker, title="Gated Topic", body="gated body")

    ctx = _FakeElicitContext(DeclinedElicitation())
    response = await research_tools.research_commit(job_id, ctx, dry_run=False)

    assert "not confirmed" in response
    assert list(vault_dir.rglob("*.md")) == []


async def test_research_commit_non_dry_run_with_mismatched_confirmation_is_rejected(
    worker: ModuleType, vault_dir: Path
) -> None:
    job_id = await _make_completed_job(worker, title="Gated Topic 2", body="gated body")

    ctx = _FakeElicitContext(
        AcceptedElicitation(
            data=research_tools.ConfirmResearchCommit(confirm_topic="the wrong topic")
        )
    )
    response = await research_tools.research_commit(job_id, ctx, dry_run=False)

    assert "not confirmed" in response
    assert list(vault_dir.rglob("*.md")) == []


async def test_research_commit_non_dry_run_with_correct_confirmation_writes_the_note(
    worker: ModuleType, vault_dir: Path
) -> None:
    job_id = await _make_completed_job(
        worker, title="Confirmed Topic", body="confirmed body", urls=["https://a.example/"]
    )

    ctx = _FakeElicitContext(
        AcceptedElicitation(
            data=research_tools.ConfirmResearchCommit(confirm_topic="Confirmed Topic")
        )
    )
    response = await research_tools.research_commit(job_id, ctx, dry_run=False)

    assert "committed" in response
    assert (vault_dir / "research" / "confirmed-topic.md").read_text(
        encoding="utf-8"
    ) == "confirmed body"


def test_register_applies_tool_annotations() -> None:
    from mcp.server import MCPServer

    mcp = MCPServer(name="test")

    research_tools.register(mcp)

    registered = asyncio.run(mcp.list_tools())
    registered_by_name = {tool.name: tool for tool in registered}
    assert {"research_start", "research_commit"} <= registered_by_name.keys()

    for name in ("research_start", "research_commit"):
        annotations = registered_by_name[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
