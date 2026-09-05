from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from athena.config import AthenaConfig
from athena.db.repository import notes as notes_repo
from athena.mcp_server import _runtime, write_tools
from athena.safety.paths import VaultRoot


@pytest.fixture(autouse=True)
def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vault_dir: Path, vault_root: VaultRoot
) -> None:
    """Point the shared `_runtime` module at this test's `conn`/`vault_dir`
    fixtures, mirroring how a real MCP server process would have `_runtime.
    config` set once at startup -- built directly rather than via
    `load_config()` since no environment variables are set in tests."""
    config = AthenaConfig(
        vault_root=vault_dir,
        data_dir=tmp_path,
        db_path=tmp_path / "athena.db",
        huey_db_path=tmp_path / "huey.db",
        huey_serializer_secret=None,
        secret_scanner_block_on_high_confidence=False,
        qdrant_url="http://127.0.0.1:6333",
        log_level="INFO",
    )
    monkeypatch.setattr(_runtime, "config", config)
    monkeypatch.setattr(_runtime, "require_vault_root", lambda: vault_root)


# --- note_create -------------------------------------------------------


async def test_note_create_writes_file_and_notes_row(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    result = await write_tools.note_create("topic.md", "# Topic\n\nBody text.", title="Topic")

    assert "note created" in result
    assert (vault_dir / "topic.md").read_text(encoding="utf-8") == "# Topic\n\nBody text."

    note = await notes_repo.get_by_path(conn, "topic.md")
    assert note is not None
    assert note.title == "Topic"
    assert note.origin == "human"
    assert note.folder is None
    assert f"note_id={note.id}" in result


async def test_note_create_derives_title_from_filename_when_not_given(
    conn: aiosqlite.Connection,
) -> None:
    await write_tools.note_create("my-note.md", "content")

    note = await notes_repo.get_by_path(conn, "my-note.md")
    assert note is not None
    assert note.title == "my-note"


async def test_note_create_creates_parent_directories(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    result = await write_tools.note_create("sub/folder/note.md", "nested content")

    assert "note created" in result
    assert (vault_dir / "sub" / "folder" / "note.md").read_text(encoding="utf-8") == (
        "nested content"
    )
    note = await notes_repo.get_by_path(conn, "sub/folder/note.md")
    assert note is not None
    assert note.folder == "sub"


async def test_note_create_refuses_to_overwrite_existing_path(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    first = await write_tools.note_create("dup.md", "original content")
    assert "note created" in first

    second = await write_tools.note_create("dup.md", "malicious replacement content")

    assert "already exists" in second
    assert "not overwritten" in second
    assert (vault_dir / "dup.md").read_text(encoding="utf-8") == "original content"

    # Only one notes row exists for this path -- the second call never
    # reached athena.vault.lifecycle.create_note.
    note = await notes_repo.get_by_path(conn, "dup.md")
    assert note is not None


async def test_note_create_refuses_when_file_preexists_outside_the_tool(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    """Exercises the exact gotcha this module's docstring names:
    resolve_vault_path(..., PathMode.CREATE) does not raise for an
    already-existing target -- it silently resolves to it. The tool's own
    explicit os.O_EXCL guard must still catch this."""
    (vault_dir / "preexisting.md").write_text("already here", encoding="utf-8")

    result = await write_tools.note_create("preexisting.md", "new content")

    assert "already exists" in result
    assert (vault_dir / "preexisting.md").read_text(encoding="utf-8") == "already here"
    note = await notes_repo.get_by_path(conn, "preexisting.md")
    assert note is None


# --- note_move -----------------------------------------------------------


async def test_note_move_moves_file_and_updates_path(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    await write_tools.note_create("old.md", "content to move")

    result = await write_tools.note_move("old.md", "new.md")

    assert "note moved" in result
    assert not (vault_dir / "old.md").exists()
    assert (vault_dir / "new.md").read_text(encoding="utf-8") == "content to move"

    assert await notes_repo.get_by_path(conn, "old.md") is None
    moved = await notes_repo.get_by_path(conn, "new.md")
    assert moved is not None
    assert moved.content_hash


async def test_note_move_refuses_when_destination_exists(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    await write_tools.note_create("source.md", "source content")
    await write_tools.note_create("dest.md", "dest content -- must not be touched")

    result = await write_tools.note_move("source.md", "dest.md")

    assert "destination already exists" in result
    assert (vault_dir / "source.md").read_text(encoding="utf-8") == "source content"
    assert (vault_dir / "dest.md").read_text(encoding="utf-8") == (
        "dest content -- must not be touched"
    )
    # The notes table still shows the original, unmoved state.
    assert (await notes_repo.get_by_path(conn, "source.md")) is not None
    dest = await notes_repo.get_by_path(conn, "dest.md")
    assert dest is not None
    assert dest.content_hash != (await notes_repo.get_by_path(conn, "source.md")).content_hash  # type: ignore[union-attr]


async def test_note_move_returns_clear_message_when_source_not_recorded(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    # A file exists on disk but was never recorded via note_create/ingest.
    (vault_dir / "untracked.md").write_text("not in the db", encoding="utf-8")

    result = await write_tools.note_move("untracked.md", "elsewhere.md")

    assert "no such note" in result
    assert (vault_dir / "untracked.md").exists()
    assert not (vault_dir / "elsewhere.md").exists()
