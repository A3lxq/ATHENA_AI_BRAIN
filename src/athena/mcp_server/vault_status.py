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
from athena.git.read import get_ahead_behind, get_log, is_git_repository
from athena.safety.paths import VaultRoot

__all__ = ["VaultStatus", "get_vault_status"]


@dataclass(frozen=True)
class VaultStatus:
    total_notes: int
    notes_needing_index: int
    jobs_by_status: dict[str, int]
    git_ahead_by: int | None
    git_last_commit: str | None


async def _git_fields(vault_root: VaultRoot | None) -> tuple[int | None, str | None]:
    """Live `athena.git.read` queries, never persisted/cached (docs/design/
    git-automation.md §2.7) -- a cached value would go stale exactly when it
    matters most (e.g. right after a manual `git push` outside ATHENA
    AI-BRAIN entirely). Defensive: no vault configured, no Git repository,
    or no commits/upstream all just leave both fields `None`, never an
    exception bubbling out of this function."""
    if vault_root is None:
        return None, None
    try:
        if not await is_git_repository(vault_root):
            return None, None

        ahead_behind = await get_ahead_behind(vault_root)
        git_ahead_by = ahead_behind.ahead if ahead_behind is not None else None

        entries = await get_log(vault_root, limit=1)
        git_last_commit = f"{entries[0].sha[:8]} {entries[0].subject}" if entries else None
    except Exception:
        return None, None
    return git_ahead_by, git_last_commit


async def get_vault_status(conn: aiosqlite.Connection, vault_root: VaultRoot | None) -> VaultStatus:
    total_notes = await notes_repo.count_active(conn)
    notes_needing_index = len(await notes_repo.list_ids_needing_index(conn))
    jobs_by_status = await research_jobs_repo.count_by_status(conn)
    git_ahead_by, git_last_commit = await _git_fields(vault_root)
    return VaultStatus(
        total_notes=total_notes,
        notes_needing_index=notes_needing_index,
        jobs_by_status=jobs_by_status,
        git_ahead_by=git_ahead_by,
        git_last_commit=git_last_commit,
    )
