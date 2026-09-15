from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from mcp.server.mcpserver import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from qdrant_client import QdrantClient
from tests.git.conftest import commit_all, init_repo

from athena.db.repository import duplicates as duplicates_repo
from athena.db.repository import notes as notes_repo
from athena.git.read import get_log
from athena.mcp_server import _runtime, mutation_tools
from athena.safety.paths import VaultRoot


class _FakeElicitContext:
    """A minimal stand-in for `mcp.server.mcpserver.Context` -- the tool
    functions here only ever call `ctx.elicit(...)`, so a duck-typed object
    with just that one async method is sufficient, avoiding the need for a
    full server/client round trip to unit-test the confirmation logic.
    """

    def __init__(
        self, result: AcceptedElicitation[Any] | DeclinedElicitation | CancelledElicitation
    ) -> None:
        self._result = result

    async def elicit(self, *, message: str, schema: type[Any]) -> Any:
        return self._result


def _write(vault_dir: Path, relative: str, text: str) -> Path:
    path = vault_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def _make_note(conn: aiosqlite.Connection, path: str, content_hash: str = "h") -> int:
    return await notes_repo.insert(
        conn, path=path, title=path, origin="human", provider=None,
        folder=None, content_hash=content_hash, created_at="2026-09-05T00:00:00+00:00",
    )


@pytest.fixture(autouse=True)
def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_root: VaultRoot,
    qdrant_client: QdrantClient,
) -> None:
    from athena.config import AthenaConfig

    config = AthenaConfig(
        vault_root=str(vault_root.path),
        data_dir=tmp_path,
        db_path=tmp_path / "athena.db",
        huey_db_path=tmp_path / "huey.db",
        huey_serializer_secret="test-secret",  # noqa: S106 -- test fixture
        secret_scanner_block_on_high_confidence=False,
        qdrant_url="http://127.0.0.1:1",
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
    )
    monkeypatch.setattr(_runtime, "config", config)
    monkeypatch.setattr(_runtime, "require_vault_root", lambda: vault_root)
    monkeypatch.setattr(_runtime, "get_qdrant_client", lambda: qdrant_client)


async def test_note_update_patch_mode_appends_without_confirmation(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    ctx = _FakeElicitContext(CancelledElicitation())  # never consulted for patch mode
    result = await mutation_tools.note_update("a.md", "new appended text", ctx, mode="patch")

    assert "updated" in result
    body = (vault_dir / "a.md").read_text(encoding="utf-8")
    assert "original content" in body
    assert "new appended text" in body


async def test_note_update_patch_dry_run_makes_no_changes(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    ctx = _FakeElicitContext(CancelledElicitation())
    result = await mutation_tools.note_update(
        "a.md", "would-be text", ctx, mode="patch", dry_run=True
    )

    assert "dry run" in result
    assert (vault_dir / "a.md").read_text(encoding="utf-8") == "original content"


async def test_note_update_overwrite_requires_confirmation(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    ctx = _FakeElicitContext(DeclinedElicitation())
    result = await mutation_tools.note_update("a.md", "replacement", ctx, mode="overwrite")

    assert "not confirmed" in result
    assert (vault_dir / "a.md").read_text(encoding="utf-8") == "original content"


async def test_note_update_overwrite_with_mismatched_confirmation_is_rejected(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    ctx = _FakeElicitContext(
        AcceptedElicitation(data=mutation_tools.ConfirmOverwriteNote(confirm_path="wrong.md"))
    )
    result = await mutation_tools.note_update("a.md", "replacement", ctx, mode="overwrite")

    assert "not confirmed" in result
    assert (vault_dir / "a.md").read_text(encoding="utf-8") == "original content"


async def test_note_update_overwrite_with_correct_confirmation_replaces_content(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    ctx = _FakeElicitContext(
        AcceptedElicitation(data=mutation_tools.ConfirmOverwriteNote(confirm_path="a.md"))
    )
    result = await mutation_tools.note_update("a.md", "replacement", ctx, mode="overwrite")

    assert "updated" in result
    assert (vault_dir / "a.md").read_text(encoding="utf-8") == "replacement"


async def test_note_link_appends_a_wikilink(conn: aiosqlite.Connection, vault_dir: Path) -> None:
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    result = await mutation_tools.note_link("a.md", "b")

    assert "linked" in result
    body = (vault_dir / "a.md").read_text(encoding="utf-8")
    assert "[[b]]" in body


async def test_note_delete_requires_confirmation(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md", "content")
    await _make_note(conn, "a.md")
    await conn.close()

    ctx = _FakeElicitContext(DeclinedElicitation())
    result = await mutation_tools.note_delete("a.md", ctx)

    assert "not confirmed" in result
    assert (vault_dir / "a.md").exists()


async def test_note_delete_with_correct_confirmation_removes_file_and_tombstones(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md", "content")
    note_id = await _make_note(conn, "a.md")

    ctx = _FakeElicitContext(
        AcceptedElicitation(data=mutation_tools.ConfirmDeleteNote(confirm_path="a.md"))
    )
    result = await mutation_tools.note_delete("a.md", ctx)

    assert "deleted" in result
    assert not (vault_dir / "a.md").exists()
    row = await notes_repo.get_by_id(conn, note_id)
    assert row is not None
    assert row.deleted_at is not None
    await conn.close()


async def test_note_delete_with_mismatched_confirmation_is_rejected(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "a.md", "content")
    await _make_note(conn, "a.md")
    await conn.close()

    ctx = _FakeElicitContext(
        AcceptedElicitation(data=mutation_tools.ConfirmDeleteNote(confirm_path="wrong.md"))
    )
    result = await mutation_tools.note_delete("a.md", ctx)

    assert "not confirmed" in result
    assert (vault_dir / "a.md").exists()


async def test_note_merge_rejects_a_pending_candidate(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    _write(vault_dir, "keep.md", "keep content")
    _write(vault_dir, "absorb.md", "absorb content")
    keep_id = await _make_note(conn, "keep.md", content_hash="hk")
    absorb_id = await _make_note(conn, "absorb.md", content_hash="ha")
    candidate_id = await duplicates_repo.upsert_candidate(
        conn, note_a_id=keep_id, note_b_id=absorb_id, detection_method="content_hash",
        lexical_score=None, semantic_score=None, metadata_match_score=None,
        combined_score=1.0, detected_at="t0",
    )
    await conn.close()

    ctx = _FakeElicitContext(CancelledElicitation())
    result = await mutation_tools.note_merge(candidate_id, "keep.md", ctx)

    assert "no 'confirmed'" in result


async def test_note_merge_with_correct_confirmation_merges(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    shared_text = " ".join(f"word{i}" for i in range(50))
    _write(vault_dir, "keep.md", shared_text)
    _write(vault_dir, "absorb.md", shared_text.replace("word5", "wordFIVE"))
    keep_id = await _make_note(conn, "keep.md", content_hash="hk")
    absorb_id = await _make_note(conn, "absorb.md", content_hash="ha")
    candidate_id = await duplicates_repo.upsert_candidate(
        conn, note_a_id=keep_id, note_b_id=absorb_id, detection_method="minhash_lsh",
        lexical_score=0.8, semantic_score=None, metadata_match_score=None,
        combined_score=0.8, detected_at="t0",
    )
    await duplicates_repo.update_resolution(
        conn, candidate_id, status="confirmed", resolved_at="t1", resolved_by="user"
    )
    await conn.close()

    ctx = _FakeElicitContext(
        AcceptedElicitation(data=mutation_tools.ConfirmMergeNotes(confirm_keep_path="keep.md"))
    )
    result = await mutation_tools.note_merge(candidate_id, "keep.md", ctx)

    assert "merged" in result
    merged_text = (vault_dir / "keep.md").read_text(encoding="utf-8")
    assert "wordFIVE" in merged_text


# --- auto-commit wiring (docs/design/git-automation.md §2.4) --------------


async def test_note_update_auto_commits_when_vault_is_a_git_repo(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot
) -> None:
    init_repo(vault_dir)
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    ctx = _FakeElicitContext(CancelledElicitation())
    result = await mutation_tools.note_update("a.md", "new appended text", ctx, mode="patch")

    assert "updated" in result
    log = await get_log(vault_root)
    assert len(log) == 1
    assert log[0].subject == "note_update:patch: a.md"


async def test_note_link_auto_commits_when_vault_is_a_git_repo(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot
) -> None:
    init_repo(vault_dir)
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    result = await mutation_tools.note_link("a.md", "b")

    assert "linked" in result
    log = await get_log(vault_root)
    assert len(log) == 1
    assert log[0].subject == "note_link: a.md"


async def test_note_delete_auto_commits_when_vault_is_a_git_repo(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot
) -> None:
    init_repo(vault_dir)
    _write(vault_dir, "a.md", "content")
    # `a.md` must already be tracked/committed for `git add` on the
    # now-deleted path to be recognized as a staged removal rather than
    # failing with "pathspec did not match any files" -- a real gotcha
    # found while writing this test, since `note_delete` unlinks the file
    # from disk *before* `auto_commit_mutation` ever runs `git add`.
    commit_all(vault_dir, "initial commit")
    await _make_note(conn, "a.md")

    ctx = _FakeElicitContext(
        AcceptedElicitation(data=mutation_tools.ConfirmDeleteNote(confirm_path="a.md"))
    )
    result = await mutation_tools.note_delete("a.md", ctx)

    assert "deleted" in result
    await conn.close()
    log = await get_log(vault_root)
    assert len(log) == 2
    assert log[0].subject == "note_delete: a.md"


async def test_note_merge_auto_commits_when_vault_is_a_git_repo(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot
) -> None:
    init_repo(vault_dir)
    shared_text = " ".join(f"word{i}" for i in range(50))
    _write(vault_dir, "keep.md", shared_text)
    _write(vault_dir, "absorb.md", shared_text.replace("word5", "wordFIVE"))
    keep_id = await _make_note(conn, "keep.md", content_hash="hk")
    absorb_id = await _make_note(conn, "absorb.md", content_hash="ha")
    candidate_id = await duplicates_repo.upsert_candidate(
        conn, note_a_id=keep_id, note_b_id=absorb_id, detection_method="minhash_lsh",
        lexical_score=0.8, semantic_score=None, metadata_match_score=None,
        combined_score=0.8, detected_at="t0",
    )
    await duplicates_repo.update_resolution(
        conn, candidate_id, status="confirmed", resolved_at="t1", resolved_by="user"
    )
    await conn.close()

    ctx = _FakeElicitContext(
        AcceptedElicitation(data=mutation_tools.ConfirmMergeNotes(confirm_keep_path="keep.md"))
    )
    result = await mutation_tools.note_merge(candidate_id, "keep.md", ctx)

    assert "merged" in result
    log = await get_log(vault_root)
    assert len(log) == 1
    assert log[0].subject == "note_merge: keep.md"


async def test_note_update_succeeds_when_auto_commit_disabled_even_in_a_git_repo(
    conn: aiosqlite.Connection,
    vault_dir: Path,
    vault_root: VaultRoot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_repo(vault_dir)
    _write(vault_dir, "a.md", "original content")
    await _make_note(conn, "a.md")
    await conn.close()

    from athena.config import AthenaConfig

    disabled_config = AthenaConfig(
        vault_root=str(vault_root.path),
        data_dir=tmp_path,
        db_path=tmp_path / "athena.db",
        huey_db_path=tmp_path / "huey.db",
        huey_serializer_secret="test-secret",  # noqa: S106 -- test fixture
        secret_scanner_block_on_high_confidence=False,
        qdrant_url="http://127.0.0.1:1",
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
    )
    monkeypatch.setattr(_runtime, "config", disabled_config)

    ctx = _FakeElicitContext(CancelledElicitation())
    result = await mutation_tools.note_update("a.md", "new appended text", ctx, mode="patch")

    assert "updated" in result
    body = (vault_dir / "a.md").read_text(encoding="utf-8")
    assert "new appended text" in body
    log = await get_log(vault_root)
    assert log == []
