from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest
from tests.git.conftest import init_repo

from athena.config import AthenaConfig
from athena.db.repository import events as events_repo
from athena.db.repository import notes as notes_repo
from athena.git.read import get_log
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
        git_auto_commit_enabled=True,
        git_auto_push_enabled=False,
        git_push_interval_minutes=60,
        git_command_timeout_s=5.0,
        llm_enabled=False,
        llm_default_provider=None,
        llm_default_model=None,
        openai_api_key=None,
        anthropic_api_key=None,
        google_api_key=None,
        ollama_base_url="http://localhost:11434",
        llm_call_timeout_s=5.0,
        llm_max_calls_per_day=50,
        research_max_dispatches_per_day=50,
        reindex_max_dispatches_per_day=20,
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


# --- auto-commit wiring (docs/design/git-automation.md §2.4) --------------


async def test_note_create_auto_commits_when_vault_is_a_git_repo(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot
) -> None:
    init_repo(vault_dir)

    result = await write_tools.note_create("a.md", "content")

    assert "note created" in result
    log = await get_log(vault_root)
    assert len(log) == 1
    assert log[0].subject == "note_create: a.md"


async def test_note_move_auto_commits_when_vault_is_a_git_repo(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot
) -> None:
    init_repo(vault_dir)
    await write_tools.note_create("old.md", "content to move")

    result = await write_tools.note_move("old.md", "new.md")

    assert "note moved" in result
    log = await get_log(vault_root)
    assert len(log) == 2
    assert log[0].subject == "note_move: 'old.md' -> 'new.md'"


async def test_note_create_succeeds_unaffected_when_vault_is_not_a_git_repo(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    # vault_dir is a plain directory here -- no `init_repo` call -- exactly
    # the pre-existing-behavior baseline every other test in this file
    # already exercises; asserted explicitly to document the requirement.
    result = await write_tools.note_create("no-git.md", "content")

    assert "note created" in result
    assert (vault_dir / "no-git.md").read_text(encoding="utf-8") == "content"


async def test_note_create_succeeds_when_auto_commit_disabled_even_in_a_git_repo(
    conn: aiosqlite.Connection,
    vault_dir: Path,
    vault_root: VaultRoot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(vault_dir)
    disabled_config = AthenaConfig(
        vault_root=vault_dir,
        data_dir=_runtime.config.data_dir,
        db_path=_runtime.config.db_path,
        huey_db_path=_runtime.config.huey_db_path,
        huey_serializer_secret=None,
        secret_scanner_block_on_high_confidence=False,
        qdrant_url="http://127.0.0.1:6333",
        log_level="INFO",
        git_auto_commit_enabled=False,
        git_auto_push_enabled=False,
        git_push_interval_minutes=60,
        git_command_timeout_s=5.0,
        llm_enabled=False,
        llm_default_provider=None,
        llm_default_model=None,
        openai_api_key=None,
        anthropic_api_key=None,
        google_api_key=None,
        ollama_base_url="http://localhost:11434",
        llm_call_timeout_s=5.0,
        llm_max_calls_per_day=50,
        research_max_dispatches_per_day=50,
        reindex_max_dispatches_per_day=20,
    )
    monkeypatch.setattr(_runtime, "config", disabled_config)

    result = await write_tools.note_create("no-commit.md", "content")

    assert "note created" in result
    assert (vault_dir / "no-commit.md").read_text(encoding="utf-8") == "content"
    log = await get_log(vault_root)
    assert log == []


# --- vault.* event emission (docs/design/production-hardening.md §2.1) ----


async def test_note_create_records_a_vault_note_created_event(
    conn: aiosqlite.Connection,
) -> None:
    result = await write_tools.note_create("topic.md", "# Topic\n\nBody text.", title="Topic")
    note = await notes_repo.get_by_path(conn, "topic.md")
    assert note is not None

    cursor = await conn.execute(
        "SELECT event_type, payload_json FROM events WHERE event_type = 'vault.note_created'"
    )
    row = await cursor.fetchone()
    assert row is not None
    event_type, payload_json = row
    payload = json.loads(payload_json)
    assert event_type == "vault.note_created"
    assert payload["note_id"] == note.id
    assert payload["path"] == "topic.md"
    assert payload["content_hash"] == note.content_hash
    assert "note created" in result


async def test_note_move_records_a_vault_note_moved_event(conn: aiosqlite.Connection) -> None:
    await write_tools.note_create("old.md", "content to move")

    result = await write_tools.note_move("old.md", "new.md")
    moved = await notes_repo.get_by_path(conn, "new.md")
    assert moved is not None

    cursor = await conn.execute(
        "SELECT event_type, payload_json FROM events WHERE event_type = 'vault.note_moved'"
    )
    row = await cursor.fetchone()
    assert row is not None
    event_type, payload_json = row
    payload = json.loads(payload_json)
    assert event_type == "vault.note_moved"
    assert payload["note_id"] == moved.id
    assert payload["old_path"] == "old.md"
    assert payload["new_path"] == "new.md"
    assert "note moved" in result


async def test_note_create_succeeds_unaffected_when_event_recording_fails(
    conn: aiosqlite.Connection, vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise(*args: object, **kwargs: object) -> str:
        raise RuntimeError("simulated events-table failure")

    monkeypatch.setattr(events_repo, "append_event", _raise)

    result = await write_tools.note_create("no-event.md", "content")

    assert "note created" in result
    assert (vault_dir / "no-event.md").read_text(encoding="utf-8") == "content"
    note = await notes_repo.get_by_path(conn, "no-event.md")
    assert note is not None
