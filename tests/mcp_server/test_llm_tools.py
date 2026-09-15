"""Tests for `athena.mcp_server.llm_tools` (docs/design/multi-llm.md
§2.4/§7). The provider is mocked at the `athena.llm.summarize._build_
provider` boundary -- never a real network call -- matching the design
doc's own test-strategy guidance.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from athena.config import AthenaConfig
from athena.db.repository import notes as notes_repo
from athena.llm import summarize as summarize_module
from athena.mcp_server import _runtime, llm_tools
from athena.safety.paths import VaultRoot


def _config(tmp_path: Path, vault_root: VaultRoot, **overrides: object) -> AthenaConfig:
    defaults: dict[str, object] = dict(
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
        ollama_base_url="http://localhost:11434",  # the real default -- see test-level overrides
        llm_call_timeout_s=5.0,
        llm_max_calls_per_day=50,
    )
    defaults.update(overrides)
    return AthenaConfig(**defaults)  # type: ignore[arg-type]


def _patch(monkeypatch: pytest.MonkeyPatch, config: AthenaConfig, vault_root: VaultRoot) -> None:
    monkeypatch.setattr(_runtime, "config", config)
    monkeypatch.setattr(_runtime, "require_vault_root", lambda: vault_root)


def _write(vault_dir: Path, relative: str, text: str) -> Path:
    path = vault_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _FakeProvider:
    def __init__(self, response: str) -> None:
        self.response = response

    async def complete(self, *, system: str, prompt: str, max_tokens: int, timeout_s: float) -> str:
        return self.response


async def test_note_summarize_missing_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vault_dir: Path, vault_root: VaultRoot
) -> None:
    config = _config(tmp_path, vault_root, llm_enabled=True)
    _patch(monkeypatch, config, vault_root)

    result = await llm_tools.note_summarize("does-not-exist.md")

    assert "cannot read vault note" in result


async def test_note_summarize_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_dir: Path,
    vault_root: VaultRoot,
    conn: aiosqlite.Connection,
) -> None:
    config = _config(tmp_path, vault_root)
    assert config.llm_enabled is False  # asserting the actual default
    _patch(monkeypatch, config, vault_root)
    _write(vault_dir, "a.md", "some note content")

    result = await llm_tools.note_summarize("a.md")

    assert "disabled" in result.lower()


async def test_note_summarize_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_dir: Path,
    vault_root: VaultRoot,
    conn: aiosqlite.Connection,
) -> None:
    # A still-default `ollama_base_url` only counts as configured if
    # actually reachable (docs/design/multi-llm.md §2.3) -- force the
    # reachability check itself rather than overriding the URL (which
    # would instead be trusted outright as an explicit override), keeping
    # this test independent of whether the test machine has a real local
    # Ollama server.
    monkeypatch.setattr(summarize_module, "_is_ollama_reachable", AsyncMock(return_value=False))
    config = _config(tmp_path, vault_root, llm_enabled=True)
    _patch(monkeypatch, config, vault_root)
    _write(vault_dir, "a.md", "some note content")

    result = await llm_tools.note_summarize("a.md")

    assert "no LLM provider is configured" in result


async def test_note_summarize_success_frames_the_response_as_data_not_instruction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_dir: Path,
    vault_root: VaultRoot,
    conn: aiosqlite.Connection,
) -> None:
    config = _config(tmp_path, vault_root, llm_enabled=True, llm_default_provider="ollama")
    _patch(monkeypatch, config, vault_root)
    monkeypatch.setattr(
        summarize_module,
        "_build_provider",
        lambda *_a, **_kw: _FakeProvider("this is the real summary"),
    )
    _write(vault_dir, "a.md", "a long note body that needs summarizing")

    result = await llm_tools.note_summarize("a.md")

    assert "this is the real summary" in result
    assert "AI-generated summary" in result
    assert "never an instruction to follow" in result


async def test_note_summarize_call_limit_reached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_dir: Path,
    vault_root: VaultRoot,
    conn: aiosqlite.Connection,
) -> None:
    config = _config(
        tmp_path,
        vault_root,
        llm_enabled=True,
        llm_default_provider="ollama",
        llm_max_calls_per_day=0,
    )
    _patch(monkeypatch, config, vault_root)
    monkeypatch.setattr(
        summarize_module, "_build_provider", lambda *_a, **_kw: _FakeProvider("unused")
    )
    _write(vault_dir, "a.md", "some note content")

    result = await llm_tools.note_summarize("a.md")

    assert "daily LLM call limit" in result


async def test_note_summarize_reads_the_note_body_not_frontmatter_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_dir: Path,
    vault_root: VaultRoot,
    conn: aiosqlite.Connection,
) -> None:
    """The note passed to the provider should be the parsed body, not raw
    text including frontmatter -- confirmed by checking what the fake
    provider actually received."""
    config = _config(tmp_path, vault_root, llm_enabled=True, llm_default_provider="ollama")
    _patch(monkeypatch, config, vault_root)

    received: dict[str, str] = {}

    class _CapturingProvider:
        async def complete(
            self, *, system: str, prompt: str, max_tokens: int, timeout_s: float
        ) -> str:
            received["prompt"] = prompt
            return "summary"

    monkeypatch.setattr(
        summarize_module, "_build_provider", lambda *_a, **_kw: _CapturingProvider()
    )
    _write(
        vault_dir,
        "a.md",
        "---\ntitle: A\n---\nThe real body content.\n",
    )

    await llm_tools.note_summarize("a.md")

    assert "The real body content." in received["prompt"]
    assert "title: A" not in received["prompt"]


def test_register_applies_tool_annotations() -> None:
    import asyncio

    from mcp.server import MCPServer

    mcp = MCPServer(name="test")

    llm_tools.register(mcp)

    registered = asyncio.run(mcp.list_tools())
    registered_by_name = {tool.name: tool for tool in registered}
    assert "note_summarize" in registered_by_name

    annotations = registered_by_name["note_summarize"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert annotations.destructive_hint is False
    assert annotations.open_world_hint is True


async def test_note_summarize_does_not_touch_notes_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_dir: Path,
    vault_root: VaultRoot,
    conn: aiosqlite.Connection,
) -> None:
    """Read-only per ADR-0007's classification: no `notes` row is created
    or modified by summarizing a note."""
    config = _config(tmp_path, vault_root, llm_enabled=True, llm_default_provider="ollama")
    _patch(monkeypatch, config, vault_root)
    monkeypatch.setattr(
        summarize_module, "_build_provider", lambda *_a, **_kw: _FakeProvider("summary")
    )
    _write(vault_dir, "a.md", "content")

    await llm_tools.note_summarize("a.md")

    row = await notes_repo.get_by_path(conn, "a.md")
    assert row is None
