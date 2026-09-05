from __future__ import annotations

import aiosqlite

from athena.db.repository import notes as notes_repo
from athena.mcp_server.vault_status import VaultStatus, get_vault_status


async def test_get_vault_status_on_an_empty_db(conn: aiosqlite.Connection) -> None:
    status = await get_vault_status(conn)

    assert status == VaultStatus(total_notes=0, notes_needing_index=0, jobs_by_status={})


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

    status = await get_vault_status(conn)

    assert status.total_notes == 1
    assert status.notes_needing_index == 1  # the one active note defaults to index_state='stale'
