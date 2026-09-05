"""Mutating, non-destructive MCP tools (docs/design/mcp-server.md §2.3).

`note_create`/`note_move` are the first MCP-driven tools that write to the
vault filesystem. Both follow the same crash-safety ordering
`athena.intelligence.merge.merge_notes` already established in Phase 5: the
vault filesystem write (the genuinely fallible step -- disk full,
permission error, a concurrent writer) happens first; the database write
that records it only happens after that succeeds. If the filesystem step
fails, nothing in the database changes -- no partial state to roll back.

Neither tool is destructive (`destructive_hint=False`): `note_create` only
ever adds a new file, never overwrites one, and `note_move` loses no
content -- it relocates it. Neither is idempotent either
(`idempotent_hint=False`): calling `note_create` twice for the same path
must fail the second time rather than silently succeeding, and calling
`note_move` twice with the same arguments fails the second time too (the
source no longer exists at its original path).

A real gotcha found while implementing this, worth stating precisely: per
`athena.safety.paths`, `resolve_vault_path(..., PathMode.CREATE)` does
*not* raise when the target already exists on disk. Read `_resolve_create_
mode`'s own docstring/implementation: CREATE mode walks upward to the
nearest existing ancestor and, if every path component already exists
(i.e. the whole target is already there), the "not-yet-existing remainder"
is simply empty -- `resolve_vault_path` returns a `SafeVaultPath` pointing
at the already-existing file, silently. It never raises `PathNotFoundError`
or any other `VaultPathError` for "target already exists" under CREATE
mode -- that's not what the mode validates. So the overwrite guard for both
tools below is an explicit, separate check (an atomic `os.O_CREAT |
os.O_EXCL | os.O_NOFOLLOW` open for `note_create`; a non-clobbering
`os.link`+`os.unlink` pair, plus an upfront `.exists()` check for a clear
early message, for `note_move`'s destination) -- never inferred from a
caught exception. This also closes the residual TOCTOU gap `_resolve_
create_mode`'s own docstring names as the caller's obligation ("the actual
create must use `os.open(path, O_CREAT | O_EXCL | O_NOFOLLOW)`"): a plain
`Path.write_text()`/`Path.rename()` after only a `.exists()` check would
leave a race window in which something planted at the target between the
check and the write/rename is silently clobbered (POSIX `rename()` in
particular replaces an existing destination without complaint) -- the
atomic primitives used here fail instead of clobbering if that race is
lost.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from athena.db.connection import open_connection
from athena.db.repository import notes as notes_repo
from athena.mcp_server import _runtime
from athena.safety.paths import PathMode, VaultPathError, resolve_vault_path
from athena.vault.lifecycle import create_note, move_note

__all__ = ["note_create", "note_move", "register"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _folder_name_for(vault_relative_path: str) -> str:
    """The top-level folder name under the vault root, or "" for a note
    directly at the vault root -- duplicated from `athena.mcp_server.
    read_tools`'s own private helper of the same name (itself duplicated
    from `athena.indexing.index_note`/`athena.vault.ingest`), per that
    module's "a small amount of duplicated logic beats coupling two
    unrelated modules" reasoning.
    """
    parts = Path(vault_relative_path).parts
    return parts[0] if len(parts) > 1 else ""


async def note_create(path: str, content: str, title: str | None = None) -> str:
    """Create a brand-new vault note at `path` with the given `content`.

    Never overwrites an existing file -- if `path` already resolves to an
    existing note, this returns a clear message and writes nothing. Parent
    directories are created as needed (they must still resolve inside the
    vault, which `resolve_vault_path` already guarantees for the whole
    target path). `title` defaults to the filename (without extension) if
    not given. The note is recorded with `origin="human"`: an MCP-client
    -created note is human/model-directed content reaching the vault
    through an explicit tool call, not ATHENA AI-BRAIN's own synthesis.
    """
    vault_root = _runtime.require_vault_root()
    try:
        safe_path = resolve_vault_path(path, vault_root, PathMode.CREATE)
    except VaultPathError as exc:
        raise ValueError(f"cannot create vault note at {path!r}: {exc}") from exc

    safe_path.path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic create-or-fail -- never overwrite. See the module docstring's
    # "real gotcha" note on why this can't be a plain `.exists()` check
    # followed by `write_text()`.
    try:
        fd = os.open(
            safe_path.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644
        )
    except FileExistsError:
        return f"note already exists at {path!r}; not overwritten"

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)

    vault_relative_path = safe_path.path.relative_to(vault_root.path).as_posix()
    folder = _folder_name_for(vault_relative_path) or None
    resolved_title = title or Path(vault_relative_path).stem
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    now = _now()

    async with open_connection(_runtime.config.db_path) as conn:
        note_id = await create_note(
            conn,
            path=vault_relative_path,
            title=resolved_title,
            origin="human",
            provider=None,
            folder=folder,
            content_hash=content_hash,
            created_at=now,
            changed_by="mcp:note_create",
        )

    return f"note created: note_id={note_id} path={vault_relative_path!r}"


async def note_move(path: str, new_path: str) -> str:
    """Move (or rename) the vault note at `path` to `new_path`.

    Refuses -- no filesystem operation performed, nothing recorded -- if
    `path` has no corresponding `notes` row, or if `new_path` already
    exists on disk. This module does not implement the MRTR confirmation
    flow for a pre-existing destination (that needs a `Context` parameter
    and elicitation schemas, built separately); it fails closed instead,
    which is always a safe, non-destructive response to give.
    """
    vault_root = _runtime.require_vault_root()

    async with open_connection(_runtime.config.db_path) as conn:
        note = await notes_repo.get_by_path(conn, path)
        if note is None:
            return f"no such note recorded for path {path!r}; nothing moved"

        try:
            safe_path_old = resolve_vault_path(path, vault_root, PathMode.EXISTING)
        except VaultPathError as exc:
            raise ValueError(f"cannot move vault note at {path!r}: {exc}") from exc
        try:
            safe_path_new = resolve_vault_path(new_path, vault_root, PathMode.CREATE)
        except VaultPathError as exc:
            raise ValueError(f"cannot move vault note to {new_path!r}: {exc}") from exc

        if safe_path_new.path.exists():
            return (
                f"destination already exists at {new_path!r}; move not performed "
                "(confirmation required to overwrite, not available in this tool)"
            )

        safe_path_new.path.parent.mkdir(parents=True, exist_ok=True)

        # Non-clobbering move: hard-link the new path, then remove the old
        # one -- `os.link` fails atomically with FileExistsError instead of
        # silently replacing the destination (unlike `Path.rename()`, which
        # on POSIX would clobber a target planted between the `.exists()`
        # check above and the move itself).
        try:
            os.link(safe_path_old.path, safe_path_new.path)
        except FileExistsError:
            return (
                f"destination already exists at {new_path!r}; move not performed "
                "(confirmation required to overwrite, not available in this tool)"
            )
        safe_path_old.path.unlink()

        new_vault_relative_path = safe_path_new.path.relative_to(vault_root.path).as_posix()
        now = _now()
        await move_note(conn, note.id, new_path=new_vault_relative_path, updated_at=now)

    return f"note moved: note_id={note.id} path={new_vault_relative_path!r}"


def register(mcp: MCPServer) -> None:
    """Register this module's tools on `mcp`."""
    not_destructive_not_idempotent = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False
    )
    mcp.tool(annotations=not_destructive_not_idempotent)(note_create)
    mcp.tool(annotations=not_destructive_not_idempotent)(note_move)
