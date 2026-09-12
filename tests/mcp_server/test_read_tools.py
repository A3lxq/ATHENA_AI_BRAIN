from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import aiosqlite
import pytest
from qdrant_client import QdrantClient
from tests.git.conftest import commit_all, init_repo

from athena.config import AthenaConfig
from athena.db.repository import chunks as chunks_repo
from athena.db.repository import notes as notes_repo
from athena.db.repository.provenance import insert_activity
from athena.indexing.chunking import Chunk
from athena.indexing.embedding import SparseVector
from athena.indexing.qdrant_store import upsert_chunks
from athena.mcp_server import _runtime, read_tools
from athena.safety.paths import VaultRoot


@pytest.fixture
def patched_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_dir: Path,
    vault_root: VaultRoot,
    qdrant_client: QdrantClient,
) -> Iterator[AthenaConfig]:
    """Point `_runtime`'s module-level state at this test's fixtures rather
    than the real environment-loaded config -- `db_path` deliberately
    matches the `conn` fixture's own path (`tmp_path / "athena.db"`) so a
    tool opening its own connection via `open_connection` sees whatever the
    test wrote through `conn`."""
    config = AthenaConfig(
        vault_root=vault_dir,
        data_dir=tmp_path,
        db_path=tmp_path / "athena.db",
        huey_db_path=tmp_path / "huey.db",
        huey_serializer_secret="test-secret",  # noqa: S106 -- test fixture
        secret_scanner_block_on_high_confidence=False,
        qdrant_url="http://127.0.0.1:6333",
        log_level="INFO",
        git_auto_commit_enabled=True,
        git_auto_push_enabled=False,
        git_push_interval_minutes=60,
        git_command_timeout_s=5.0,
    )
    monkeypatch.setattr(_runtime, "config", config)
    monkeypatch.setattr(_runtime, "get_qdrant_client", lambda: qdrant_client)
    monkeypatch.setattr(_runtime, "require_vault_root", lambda: vault_root)
    yield config


async def _make_note(
    conn: aiosqlite.Connection, path: str = "a.md", *, content_hash: str = "h1"
) -> int:
    return await notes_repo.insert(
        conn,
        path=path,
        title=path,
        origin="human",
        provider=None,
        folder=None,
        content_hash=content_hash,
        created_at="2026-09-05T00:00:00+00:00",
    )


async def _index_note(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    *,
    note_id: int,
    text: str,
    dense_vector: list[float],
) -> str:
    """Insert a chunk row and a matching Qdrant point for `note_id`,
    following the same pattern tests/intelligence/conftest.py and
    tests/retrieval/test_vector_search.py already establish."""
    (point_id,) = upsert_chunks(
        qdrant_client,
        note_id=note_id,
        chunks=[Chunk(text=text, chunk_index=0, token_count=len(text.split()))],
        dense_vectors=[dense_vector],
        sparse_vectors=[SparseVector(indices=[1, 2], values=[0.5, 0.5])],
        payload_fields={
            "note_path": f"note-{note_id}.md",
            "tags": [],
            "folder": None,
            "status": "active",
            "origin": "human",
            "provider": None,
            "embedding_model_version": "test@1",
        },
    )
    await chunks_repo.insert(
        conn,
        note_id=note_id,
        chunk_index=0,
        chunk_text=text,
        content_hash=f"chunk-{note_id}",
        qdrant_point_id=point_id,
        embedding_model_version="test@1",
        token_count=len(text.split()),
        created_at="2026-09-05T00:00:00+00:00",
    )
    return point_id


# --- vault_search -------------------------------------------------------


async def test_vault_search_returns_text_containing_indexed_note_content(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    patched_runtime: AthenaConfig,
) -> None:
    note_id = await _make_note(conn, "hybrid.md")
    await _index_note(
        conn,
        qdrant_client,
        note_id=note_id,
        text="hybrid search over dense and sparse vectors",
        dense_vector=[0.1] * 1024,
    )

    result_text = await read_tools.vault_search("hybrid search")

    assert "hybrid search over dense and sparse vectors" in result_text
    assert "hybrid.md" in result_text


# --- note_read / vault:// resource ---------------------------------------


async def test_note_read_returns_metadata_and_body(
    vault_dir: Path, patched_runtime: AthenaConfig
) -> None:
    (vault_dir / "note.md").write_text(
        "---\ntitle: My Note\n---\nThis is the body.\n", encoding="utf-8"
    )

    result_text = await read_tools.note_read("note.md")

    assert "This is the body." in result_text
    assert "My Note" in result_text
    assert result_text.startswith("[Metadata:")


async def test_note_read_rejects_nonexistent_path_with_a_clear_message(
    patched_runtime: AthenaConfig,
) -> None:
    # Returns (never raises) a clear string -- an MCP tool that *raises* has
    # its message replaced by a generic "Error executing tool X" wrapper
    # before it reaches the client (verified in
    # tests/mcp_server/test_server_integration.py), so every tool in this
    # server returns error strings for expected conditions instead.
    result = await read_tools.note_read("does-not-exist.md")

    assert "cannot read vault note" in result


async def test_note_read_rejects_traversal_with_a_clear_message_not_a_raw_exception(
    patched_runtime: AthenaConfig,
) -> None:
    result = await read_tools.note_read("../outside.md")

    assert "cannot read vault note" in result


async def test_vault_resource_matches_note_read_for_same_path(
    vault_dir: Path, patched_runtime: AthenaConfig
) -> None:
    (vault_dir / "shared.md").write_text("Just a plain note.\n", encoding="utf-8")

    via_tool = await read_tools.note_read("shared.md")
    via_resource = await read_tools.read_vault_resource("shared.md")

    assert via_tool == via_resource


# --- note_related ---------------------------------------------------------


async def test_note_related_returns_no_results_message_when_note_has_no_chunks(
    conn: aiosqlite.Connection, patched_runtime: AthenaConfig
) -> None:
    note_id = await _make_note(conn, "never-indexed.md")

    result_text = await read_tools.note_related(note_id)

    assert result_text == "No related notes found."


async def test_note_related_formats_related_notes(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    patched_runtime: AthenaConfig,
) -> None:
    note_a = await _make_note(conn, "a.md", content_hash="ha")
    note_b = await _make_note(conn, "b.md", content_hash="hb")
    await _index_note(conn, qdrant_client, note_id=note_a, text="note a", dense_vector=[0.1] * 1024)
    await _index_note(conn, qdrant_client, note_id=note_b, text="note b", dense_vector=[0.1] * 1024)

    result_text = await read_tools.note_related(note_a)

    assert "b.md" in result_text
    assert "score=" in result_text


# --- note_duplicates -------------------------------------------------------


async def test_note_duplicates_returns_no_results_message_on_empty_db(
    conn: aiosqlite.Connection, patched_runtime: AthenaConfig
) -> None:
    result_text = await read_tools.note_duplicates(999)

    assert result_text == "No duplicate candidates found."


async def test_note_duplicates_finds_exact_content_hash_match(
    conn: aiosqlite.Connection,
    vault_dir: Path,
    patched_runtime: AthenaConfig,
) -> None:
    (vault_dir / "dup-a.md").write_text("identical content\n", encoding="utf-8")
    (vault_dir / "dup-b.md").write_text("identical content\n", encoding="utf-8")
    note_a = await _make_note(conn, "dup-a.md", content_hash="same-hash")
    note_b = await _make_note(conn, "dup-b.md", content_hash="same-hash")

    result_text = await read_tools.note_duplicates(note_a)

    assert f"note_a_id={min(note_a, note_b)}" in result_text
    assert f"note_b_id={max(note_a, note_b)}" in result_text
    assert "combined_score=1.000" in result_text


# --- note_provenance --------------------------------------------------------


async def test_note_provenance_returns_no_results_message_when_empty(
    conn: aiosqlite.Connection, patched_runtime: AthenaConfig
) -> None:
    note_id = await _make_note(conn)

    result_text = await read_tools.note_provenance(note_id)

    assert result_text == "No provenance history found."


async def test_note_provenance_formats_recorded_activity(
    conn: aiosqlite.Connection, patched_runtime: AthenaConfig
) -> None:
    note_id = await _make_note(conn)
    await insert_activity(
        conn,
        note_id=note_id,
        activity_type="ingested",
        provider="anthropic",
        model="claude",
        human_edited=False,
        occurred_at="2026-09-05T00:00:00+00:00",
        recorded_at="2026-09-05T00:00:00+00:00",
    )

    result_text = await read_tools.note_provenance(note_id)

    assert "activity_type=ingested" in result_text
    assert "provider=anthropic" in result_text
    assert "human_edited=False" in result_text


# --- vault_status -----------------------------------------------------------


async def test_vault_status_on_empty_db(
    conn: aiosqlite.Connection, patched_runtime: AthenaConfig
) -> None:
    result_text = await read_tools.vault_status()

    assert "Total active notes: 0" in result_text
    assert "Notes needing index: 0" in result_text
    # vault_dir is a plain (non-Git) directory in this fixture -- git fields
    # degrade gracefully rather than raising.
    assert "Git commits ahead of upstream: not available" in result_text
    assert "Git last commit: not available" in result_text


async def test_vault_status_reflects_a_created_note(
    conn: aiosqlite.Connection, patched_runtime: AthenaConfig
) -> None:
    await _make_note(conn)

    result_text = await read_tools.vault_status()

    assert "Total active notes: 1" in result_text
    assert "Notes needing index: 1" in result_text


async def test_vault_status_reports_git_last_commit_for_a_real_repo(
    conn: aiosqlite.Connection, vault_dir: Path, patched_runtime: AthenaConfig
) -> None:
    init_repo(vault_dir)
    (vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(vault_dir, "initial commit")

    result_text = await read_tools.vault_status()

    assert "Git last commit:" in result_text
    assert "initial commit" in result_text


async def test_vault_status_works_without_a_configured_vault(
    conn: aiosqlite.Connection, patched_runtime: AthenaConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise() -> None:
        raise RuntimeError("ATHENA_VAULT_DIR is not set")

    monkeypatch.setattr(_runtime, "require_vault_root", _raise)

    result_text = await read_tools.vault_status()

    assert "Total active notes: 0" in result_text
    assert "Git commits ahead of upstream: not available" in result_text
    assert "Git last commit: not available" in result_text


# --- system_diagnostics ------------------------------------------------------


async def test_system_diagnostics_produces_readable_non_crashing_report(
    patched_runtime: AthenaConfig,
) -> None:
    result_text = await read_tools.system_diagnostics()

    assert "Overall status:" in result_text
    assert "python_version" in result_text
