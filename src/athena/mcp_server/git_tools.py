"""Git-backed MCP tools (docs/design/git-automation.md §2.5), resolving the
last of ADR-0007's placeholder tool-contract rows: `git_status`, `git_log`,
`note_history`, `git_commit`.

`git_commit` here is the STANDALONE, caller-invoked tool -- distinct from
`athena.git.write.auto_commit_mutation`, the narrowly-scoped best-effort
helper the mutating note tools (`note_create`/`note_update`/etc., wired up
elsewhere in this phase) call automatically as their own final step. This
module's `git_commit` stages and commits *everything* currently dirty in the
vault (`git add .`, not a caller-supplied path list), since a standalone call
has no single triggering mutation to scope a commit to. Per design doc §2.5,
`git_commit` is `destructive_hint=False`/`idempotent_hint=False` and is NOT
MRTR-gated (no `ctx.elicit` confirmation) -- it is the least destructive
mutation category this server exposes: it can only ever *add* a commit
object, never rewrite or discard history.
"""

from __future__ import annotations

from uuid import uuid4

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from athena.db.connection import open_connection
from athena.db.repository import events as events_repo
from athena.git import read as git_read
from athena.git import write as git_write
from athena.git.read import GitLogEntry
from athena.git.wrapper import GitFailureKind
from athena.mcp_server import _runtime

__all__ = ["git_status", "git_log", "note_history", "git_commit", "register"]

_NOT_A_REPOSITORY_MESSAGE = (
    "vault is not a Git repository -- run `git init` in the vault directory to enable "
    "Git automation"
)


def _format_log_entries(entries: list[GitLogEntry]) -> str:
    return "\n".join(
        f"{entry.sha[:8]}  {entry.date}  {entry.subject} ({entry.author})" for entry in entries
    )


async def git_status() -> str:
    """Report the vault repository's working-tree state: clean or dirty,
    the changed paths if any, and ahead/behind counts relative to the
    upstream branch (omitted if no upstream is configured).

    Returns a clear "vault is not a Git repository" message (not an error)
    if the configured vault has no Git repository yet.
    """
    vault_root = _runtime.require_vault_root()
    timeout_s = _runtime.config.git_command_timeout_s
    if not await git_read.is_git_repository(vault_root, timeout_s=timeout_s):
        return _NOT_A_REPOSITORY_MESSAGE

    status = await git_read.get_status(vault_root, timeout_s=timeout_s)
    lines = [f"working tree: {'clean' if status.is_clean else 'dirty'}"]
    if status.changed_paths:
        lines.append("changed paths:")
        lines.extend(f"  {path}" for path in status.changed_paths)
    if status.ahead_behind is not None:
        lines.append(
            f"ahead {status.ahead_behind.ahead}, behind {status.ahead_behind.behind} "
            "(relative to upstream)"
        )
    return "\n".join(lines)


async def git_log(limit: int = 20) -> str:
    """List the vault repository's most recent commits (newest first, up to
    `limit`), one per line: `short-sha  date  subject (author)`.

    Returns a clear "no commit history" message (not an error) if the
    repository has no commits yet, and a "vault is not a Git repository"
    message if the vault has no Git repository at all.
    """
    vault_root = _runtime.require_vault_root()
    timeout_s = _runtime.config.git_command_timeout_s
    if not await git_read.is_git_repository(vault_root, timeout_s=timeout_s):
        return _NOT_A_REPOSITORY_MESSAGE

    entries = await git_read.get_log(vault_root, limit=limit, timeout_s=timeout_s)
    if not entries:
        return "no commit history yet"
    return _format_log_entries(entries)


async def note_history(path: str, limit: int = 20) -> str:
    """List the Git commit history for a single vault note by its
    vault-relative `path` (follows renames), one line per commit:
    `short-sha  date  subject (author)`.

    Returns a clear "no history" message (not an error) for a path with no
    commits yet -- a normal outcome, e.g. a note created while auto-commit
    was disabled -- and a "vault is not a Git repository" message if the
    vault has no Git repository at all.
    """
    vault_root = _runtime.require_vault_root()
    timeout_s = _runtime.config.git_command_timeout_s
    if not await git_read.is_git_repository(vault_root, timeout_s=timeout_s):
        return _NOT_A_REPOSITORY_MESSAGE

    entries = await git_read.get_path_history(vault_root, path, limit=limit, timeout_s=timeout_s)
    if not entries:
        return f"no commit history found for {path!r}"
    return _format_log_entries(entries)


async def git_commit(message: str | None = None, dry_run: bool = False) -> str:
    """Stage and commit EVERYTHING currently dirty in the vault repository
    (equivalent to `git add .` followed by `git commit`) -- the standalone,
    all-dirty-files commit tool, distinct from the automatic per-mutation
    commit every note-writing tool already performs on its own.

    `dry_run=True` (the default) previews the would-be diff and commit
    message without writing anything. Uses `message` if given, otherwise an
    auto-generated one naming how many files changed. Returns a clear
    "nothing to commit" message (not an error) if the working tree is
    already clean, and a "vault is not a Git repository" message if the
    vault has no Git repository at all.
    """
    vault_root = _runtime.require_vault_root()
    timeout_s = _runtime.config.git_command_timeout_s
    if not await git_read.is_git_repository(vault_root, timeout_s=timeout_s):
        return _NOT_A_REPOSITORY_MESSAGE

    status = await git_read.get_status(vault_root, timeout_s=timeout_s)
    message_to_use = (
        message
        if message is not None
        else f"manual commit via git_commit: {len(status.changed_paths)} file(s) changed"
    )

    result = await git_write.commit_paths(
        vault_root, ["."], message_to_use, dry_run=dry_run, timeout_s=timeout_s
    )

    if dry_run:
        return f"[dry run] {result.dry_run_preview}"

    if not result.committed:
        if result.failure_kind is GitFailureKind.NOTHING_TO_COMMIT:
            return "nothing to commit"
        return f"commit failed: {result.failure_kind.value}"

    async with open_connection(_runtime.config.db_path) as conn:
        await events_repo.append_event(
            conn,
            event_type="git.commit_completed",
            source="mcp_tool_call",
            correlation_id=str(uuid4()),
            payload={
                "commit_sha": result.sha,
                "files_changed": status.changed_paths,
                "message": message_to_use,
                "push_status": "not_attempted",
            },
        )
    return f"committed: sha={result.sha}"


def register(mcp: MCPServer) -> None:
    """Register every tool in this module onto `mcp`."""
    read_only = ToolAnnotations(read_only_hint=True)
    mcp.tool(annotations=read_only)(git_status)
    mcp.tool(annotations=read_only)(git_log)
    mcp.tool(annotations=read_only)(note_history)
    mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False
        )
    )(git_commit)
