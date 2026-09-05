"""`vault_status` tool support (docs/design/mcp-server.md §2.1).

Answers "what's the state of my knowledge" -- composing existing/near-
existing queries into one summary -- distinct from `system_diagnostics`
(`athena.diagnostics.run_doctor`), which answers "is the infrastructure
healthy." Deliberately does not duplicate that tool's role.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from athena.db.repository import notes as notes_repo
from athena.db.repository import research_jobs as research_jobs_repo

__all__ = ["VaultStatus", "get_vault_status"]


@dataclass(frozen=True)
class VaultStatus:
    total_notes: int
    notes_needing_index: int
    jobs_by_status: dict[str, int]


async def get_vault_status(conn: aiosqlite.Connection) -> VaultStatus:
    total_notes = await notes_repo.count_active(conn)
    notes_needing_index = len(await notes_repo.list_ids_needing_index(conn))
    jobs_by_status = await research_jobs_repo.count_by_status(conn)
    return VaultStatus(
        total_notes=total_notes,
        notes_needing_index=notes_needing_index,
        jobs_by_status=jobs_by_status,
    )
