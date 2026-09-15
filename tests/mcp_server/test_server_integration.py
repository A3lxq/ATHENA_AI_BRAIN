"""Integration tests: a real MCP client talking to the real server over the
SDK's in-memory transport (no stdio process needed) -- asserting the actual
JSON-RPC-level response shape, not just a wrapped function's return value
(docs/design/mcp-server.md §7).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import aiosqlite
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from qdrant_client import QdrantClient

from athena.config import AthenaConfig
from athena.db.repository import notes as notes_repo

# `athena.mcp_server.server` imports `job_tools`, which imports `athena.worker`
# eagerly at module scope, which builds a real Huey instance at ITS module
# scope, which hard-fails without `ATHENA_HUEY_SECRET` set (see
# tests/mcp_server/test_job_tools.py's own module docstring for the full
# explanation) -- this placeholder only needs to satisfy that one-time
# import; the tools exercised in this file never touch `athena.worker`.
os.environ.setdefault("ATHENA_HUEY_SECRET", "collection-time-placeholder-secret")  # noqa: S105
os.environ.setdefault("ATHENA_DATA_DIR", tempfile.mkdtemp())

from athena.mcp_server import _runtime  # noqa: E402
from athena.mcp_server.server import build_server  # noqa: E402
from athena.safety.paths import VaultRoot  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_root: VaultRoot,
    qdrant_client: QdrantClient,
) -> None:
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


async def test_list_tools_and_call_note_read_over_a_real_client_session(
    conn: aiosqlite.Connection, vault_dir: Path
) -> None:
    # Requesting the `conn` fixture (from tests/mcp_server/conftest.py) runs
    # migrations against the same db_path _patch_runtime pointed _runtime.config
    # at, before the tool-under-test opens its own connection to that file.
    (vault_dir / "a.md").write_text("Hello from a real integration test.\n", encoding="utf-8")
    await notes_repo.insert(
        conn, path="a.md", title="A", origin="human", provider=None,
        folder=None, content_hash="h1", created_at="2026-09-05T00:00:00+00:00",
    )

    mcp = build_server()
    async with InMemoryTransport(mcp) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init_result = await session.initialize()
            assert init_result.server_info.name == "athena"

            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            assert "note_read" in tool_names
            assert "note_delete" in tool_names
            # 17 (docs/design/mcp-server.md) + research_start/research_commit (Phase 7)
            # + git_status/git_log/note_history/git_commit (Phase 8) + note_summarize (Phase 9)
            assert len(tool_names) == 24

            call_result = await session.call_tool("note_read", {"path": "a.md"})
            assert not call_result.is_error
            text_blocks = [b.text for b in call_result.content if hasattr(b, "text")]
            assert any("Hello from a real integration test" in t for t in text_blocks)


async def test_calling_note_read_on_a_missing_note_returns_a_clear_message_not_a_crash(
    vault_dir: Path,
) -> None:
    """`note_read` returns a clear string (never raises) for a missing note,
    so this is actually the "normal, non-error" path per docs/design/
    mcp-server.md's own design -- deliberately, after a real finding here:
    an MCP tool that *raises* has its message replaced by the SDK's own
    generic `"Error executing tool <name>"` wrapper before it reaches the
    client (`Tool.run()` wraps any raised exception as `UnexpectedToolError`,
    and `_handle_call_tool` returns `str(that wrapper)`, not the original
    exception's message) -- confirmed by intentionally reverting this
    module's fix and observing exactly that generic text over a real
    client/server round trip. Every tool in this server returns error
    strings for expected conditions instead of raising, specifically to
    avoid this.
    """
    mcp = build_server()
    async with InMemoryTransport(mcp) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            call_result = await session.call_tool("note_read", {"path": "does-not-exist.md"})

            assert not call_result.is_error
            text_blocks = [b.text for b in call_result.content if hasattr(b, "text")]
            assert any("cannot read vault note" in t for t in text_blocks)
