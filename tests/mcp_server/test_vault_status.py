from __future__ import annotations

from pathlib import Path

import aiosqlite
from tests.git.conftest import commit_all, init_repo

from athena.db.repository import notes as notes_repo
from athena.mcp_server.vault_status import VaultStatus, get_vault_status
from athena.safety.paths import VaultRoot


async def test_get_vault_status_on_an_empty_db_with_no_vault_configured(
    conn: aiosqlite.Connection,
) -> None:
    status = await get_vault_status(conn, None)

    assert status == VaultStatus(
        total_notes=0,
        notes_needing_index=0,
        jobs_by_status={},
        git_ahead_by=None,
        git_last_commit=None,
    )


async def test_get_vault_status_counts_notes_and_index_state(conn: aiosqlite.Connection) -> None:
    await notes_repo.insert(
        conn, path="a.md", title="A", origin="human", provider=None,
        folder=None, content_hash="h1", created_at="2026-09-05T00:00:00+00:00",
    )
    deleted_id = await notes_repo.insert(
        conn, path="b.md", title="B", origin="human", provider=None,
        folder=None, content_hash="h2", created_at="2026-09-05T00:00:00+00:00",
    )
    await notes_repo.soft_delete(conn, deleted_id, deleted_at="2026-09-05T01:00:00+00:00")

    status = await get_vault_status(conn, None)

    assert status.total_notes == 1
    assert status.notes_needing_index == 1  # the one active note defaults to index_state='stale'


async def test_get_vault_status_git_fields_none_for_a_non_git_vault(
    conn: aiosqlite.Connection, tmp_path: Path
) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    vault_root = VaultRoot.initialize(vault_dir)

    status = await get_vault_status(conn, vault_root)

    assert status.git_ahead_by is None
    assert status.git_last_commit is None


async def test_get_vault_status_git_fields_populated_for_a_real_repo(
    conn: aiosqlite.Connection, tmp_path: Path
) -> None:
    vault_dir = tmp_path / "vault"
    init_repo(vault_dir)
    (vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(vault_dir, "note_create: a.md")
    vault_root = VaultRoot.initialize(vault_dir)

    status = await get_vault_status(conn, vault_root)

    assert status.git_ahead_by is None  # no upstream configured
    assert status.git_last_commit is not None
    assert "note_create: a.md" in status.git_last_commit
