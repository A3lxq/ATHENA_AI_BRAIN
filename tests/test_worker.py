"""Smoke tests for athena.worker.

worker.py constructs its module-level `huey` instance (and hard-fails via
assert_safe_job_serializer) at import time, keyed off `load_config()`'s
environment snapshot -- so every test here sets the environment first, then
imports the module fresh (evicting any cached import) rather than relying on
whatever config happened to be active when some other test last imported it.

worker.py itself never runs migrations (that's the explicit `athena
migrate` CLI step, not automatic) -- every test that calls a worker function
touching `config.db_path` must apply migrations first, exactly as a real
deployment would need to run `athena migrate` before `athena ingest
bootstrap`.
"""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType

import aiosqlite
import pytest

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations


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


def test_build_huey_hard_fails_without_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Get a valid module imported first (so `from athena.worker import
    # build_huey` below doesn't itself trip over the module's own top-level
    # `huey = build_huey(_config)` call with no secret configured), then
    # test build_huey directly against a deliberately misconfigured object.
    _fresh_worker_module(tmp_path, monkeypatch)
    from athena.config import AthenaConfig
    from athena.hardening.serializer import SerializerMisconfigured
    from athena.worker import build_huey

    config = AthenaConfig(
        vault_root=None,
        data_dir=tmp_path,
        db_path=tmp_path / "athena.db",
        huey_db_path=tmp_path / "huey.db",
        huey_serializer_secret=None,
        secret_scanner_block_on_high_confidence=False,
        qdrant_url="http://127.0.0.1:6333",
        log_level="INFO",
    )
    with pytest.raises(SerializerMisconfigured):
        build_huey(config)


def test_module_registers_task_and_startup_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _fresh_worker_module(tmp_path, monkeypatch)

    registered = worker.huey._registry._registry  # type: ignore[attr-defined]
    assert any(key.endswith("ingest_note_task") for key in registered)
    assert any(key.endswith("reconcile_vault_task") for key in registered)
    assert any(key.endswith("stale_sweep_task") for key in registered)
    assert "_worker_startup" in worker.huey._startup


def test_run_bootstrap_ingests_the_configured_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "note.md").write_text("hello\n", encoding="utf-8")

    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)

    summary = worker.run_bootstrap()

    assert summary.notes_ingested == 1


def test_run_reconcile_against_configured_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "note.md").write_text("hello\n", encoding="utf-8")

    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)

    summary = worker.run_reconcile()

    assert summary.paths_scanned == 1
    assert summary.discrepancies_found == 1


def test_run_bootstrap_without_vault_configured_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=None)

    with pytest.raises(RuntimeError, match="ATHENA_VAULT_DIR"):
        worker.run_bootstrap()


def test_ingest_note_task_call_local_performs_real_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`call_local` bypasses Huey's queue and runs the task function's body
    synchronously, in-process -- the closest thing to an end-to-end
    exercise of the actual registered task without a running consumer."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    note_path = vault_dir / "note.md"
    note_path.write_text("hello\n", encoding="utf-8")

    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)

    worker.ingest_note_task.call_local(str(note_path), "c1", None)

    from athena.db.connection import open_connection
    from athena.db.repository import notes as notes_repo

    async def _check() -> None:
        async with open_connection(worker._config.db_path) as conn:
            row = await notes_repo.get_by_path(conn, "note.md")
            assert row is not None

    asyncio.run(_check())


def test_run_duplicates_scan_and_list_and_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "a.md").write_text("identical body text\n", encoding="utf-8")
    (vault_dir / "b.md").write_text("identical body text\n", encoding="utf-8")

    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)
    worker.run_bootstrap()

    candidates = worker.run_duplicates_scan()
    assert len(candidates) == 1

    pending = worker.run_duplicates_list(status="pending")
    assert len(pending) == 1

    worker.run_duplicates_resolve(
        candidate_id=pending[0].id, resolution="confirmed", resolved_by="user"
    )

    confirmed = worker.run_duplicates_list(status="confirmed")
    assert len(confirmed) == 1
    assert confirmed[0].id == pending[0].id


def test_run_duplicates_merge_combines_the_two_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_text = " ".join(f"word{i}" for i in range(200))
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "keep.md").write_text(shared_text + "\n", encoding="utf-8")
    (vault_dir / "absorb.md").write_text(
        shared_text.replace("word5", "wordFIVE") + "\n", encoding="utf-8"
    )

    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)
    worker.run_bootstrap()

    from athena.db.connection import open_connection
    from athena.db.repository import notes as notes_repo

    async def _ids() -> tuple[int, int]:
        async with open_connection(worker._config.db_path) as conn:
            keep_row = await notes_repo.get_by_path(conn, "keep.md")
            absorb_row = await notes_repo.get_by_path(conn, "absorb.md")
            assert keep_row is not None
            assert absorb_row is not None
            return keep_row.id, absorb_row.id

    keep_id, absorb_id = asyncio.run(_ids())

    candidates = worker.run_duplicates_scan(threshold=0.0)
    match = next(c for c in candidates if {c.note_a_id, c.note_b_id} == {keep_id, absorb_id})
    worker.run_duplicates_resolve(
        candidate_id=match.id, resolution="confirmed", resolved_by="user"
    )

    result = worker.run_duplicates_merge(
        keep_note_id=keep_id, absorb_note_id=absorb_id, merged_by="user"
    )

    assert result.kept_note_id == keep_id
    merged_text = (vault_dir / "keep.md").read_text(encoding="utf-8")
    assert "wordFIVE" in merged_text


def test_run_stale_sweep_against_configured_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "note.md").write_text("hello\n", encoding="utf-8")

    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)
    worker.run_bootstrap()

    summary = worker.run_stale_sweep(stale_after_days=180)

    # A freshly-ingested note (status='draft', just created) is not
    # 'active'/'verified' yet, so this run has nothing to flag -- the point
    # of this test is that the wiring runs end-to-end without error.
    assert summary.notes_flagged == 0


def test_research_task_call_local_records_a_draft_and_marks_the_job_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`context=True`'s injected `task` kwarg is only wired up by Huey's real
    `execute()` path, not `call_local` (which calls the raw function
    directly) -- so this test supplies a minimal stand-in with just the
    `.id` attribute `research_task` actually reads, and pre-inserts the
    `research_jobs` row `call_local`'s task would otherwise have to look
    itself up by, exactly as a real dispatched job's row would already
    exist by the time a real consumer picks it up."""
    from types import SimpleNamespace

    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo
    from athena.research.workflow import ResearchDraft

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)

    async def _insert() -> int:
        async with open_connection(worker._config.db_path) as conn:
            return await research_jobs_repo.insert(
                conn, huey_task_id="fake-huey-id", job_type="research_start",
                query="Topic", created_at="2026-09-10T00:00:00+00:00",
            )

    job_id = asyncio.run(_insert())

    def fake_run_research(urls: list[str], topic: str) -> ResearchDraft:
        return ResearchDraft(
            title=topic, body="drafted body", succeeded_urls=urls, failed_urls=[]
        )

    monkeypatch.setattr(worker, "run_research", fake_run_research)

    worker.research_task.call_local(
        ["https://a.example/"], "Topic", "corr-1", task=SimpleNamespace(id="fake-huey-id")
    )

    async def _check() -> None:
        async with open_connection(worker._config.db_path) as conn:
            row = await research_jobs_repo.get_by_id(conn, job_id)
            assert row is not None
            assert row.draft_body == "drafted body"
            assert row.draft_source_urls == ["https://a.example/"]
            assert row.status == "succeeded"

    asyncio.run(_check())


def test_research_task_call_local_raises_if_its_job_row_does_not_exist_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The benign dispatch-then-insert race described in `research_task`'s
    own docstring: if the `research_jobs` row isn't there yet, this must
    raise (letting Huey's `retries=3, retry_delay=10` handle it), not
    silently no-op."""
    from types import SimpleNamespace

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)

    with pytest.raises(RuntimeError, match="no research_jobs row"):
        worker.research_task.call_local(
            ["https://a.example/"], "Topic", "corr-1",
            task=SimpleNamespace(id="no-such-huey-id"),
        )


def test_run_research_commit_dry_run_and_real_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    worker = _fresh_worker_module(tmp_path, monkeypatch, vault_dir=vault_dir)

    async def _insert() -> int:
        async with open_connection(worker._config.db_path) as conn:
            job_id = await research_jobs_repo.insert(
                conn, huey_task_id="fake-huey-id-2", job_type="research_start",
                query="CLI Topic", created_at="2026-09-10T00:00:00+00:00",
            )
            await research_jobs_repo.record_draft(
                conn, job_id, draft_title="CLI Topic", draft_body="cli body",
                draft_source_urls=["https://a.example/"],
            )
            await research_jobs_repo.mark_finished(
                conn, job_id, status="succeeded", finished_at="2026-09-10T00:01:00+00:00"
            )
            return job_id

    job_id = asyncio.run(_insert())

    preview = worker.run_research_commit(job_id=job_id, dry_run=True)
    assert preview.note_id is None
    assert preview.preview_body == "cli body"
    assert list(vault_dir.rglob("*.md")) == []

    result = worker.run_research_commit(job_id=job_id, dry_run=False)
    assert result.note_id is not None
    assert (vault_dir / "research" / "cli-topic.md").read_text(encoding="utf-8") == "cli body"
