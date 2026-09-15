"""Tests for `athena.llm.summarize` (docs/design/multi-llm.md §7)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from athena.config import AthenaConfig
from athena.llm import summarize as summarize_module
from athena.llm.summarize import (
    LLMCallLimitError,
    LLMDisabledError,
    LLMNotConfiguredError,
    summarize_text,
)


def _make_config(tmp_path: Path, **overrides: object) -> AthenaConfig:
    defaults: dict[str, object] = dict(
        vault_root=None,
        data_dir=tmp_path,
        db_path=tmp_path / "athena.db",
        huey_db_path=tmp_path / "huey.db",
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
        llm_max_calls_per_day=3,
        research_max_dispatches_per_day=50,
        reindex_max_dispatches_per_day=20,
    )
    defaults.update(overrides)
    return AthenaConfig(**defaults)  # type: ignore[arg-type]


class _FakeProvider:
    def __init__(self, response: str = "a real summary") -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def complete(self, *, system: str, prompt: str, max_tokens: int, timeout_s: float) -> str:
        self.calls.append(
            {"system": system, "prompt": prompt, "max_tokens": max_tokens, "timeout_s": timeout_s}
        )
        return self.response


async def test_summarize_text_disabled_by_default(
    conn: aiosqlite.Connection, tmp_path: Path
) -> None:
    config = _make_config(tmp_path)
    assert config.llm_enabled is False  # asserting the actual default, not just the parameter

    with pytest.raises(LLMDisabledError):
        await summarize_text(conn, "some note text", config=config, source_label="a.md")


async def test_summarize_text_raises_when_no_provider_configured(
    conn: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `ollama_base_url` always has a truthy default (unlike the other three
    # providers' None-unless-set API key fields), so a still-default URL
    # only counts as "configured" if actually reachable (docs/design/
    # multi-llm.md §2.3) -- forcing the reachability check itself, rather
    # than overriding `ollama_base_url` (which would instead be treated as
    # an *explicit* override and trusted outright, the wrong branch for
    # this test), keeps this test's outcome independent of whether the
    # machine running it happens to have a real local Ollama server.
    monkeypatch.setattr(summarize_module, "_is_ollama_reachable", AsyncMock(return_value=False))
    config = _make_config(tmp_path, llm_enabled=True)

    with pytest.raises(LLMNotConfiguredError, match="no LLM provider"):
        await summarize_text(conn, "text", config=config, source_label="a.md")


async def test_summarize_text_raises_when_multiple_providers_configured_with_no_default(
    conn: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(summarize_module, "_is_ollama_reachable", AsyncMock(return_value=False))
    config = _make_config(
        tmp_path,
        llm_enabled=True,
        openai_api_key="sk-test",  # noqa: S106 -- test fixture
        anthropic_api_key="sk-ant-test",  # noqa: S106 -- test fixture
    )

    with pytest.raises(LLMNotConfiguredError, match="multiple providers"):
        await summarize_text(conn, "text", config=config, source_label="a.md")


async def test_summarize_text_call_ceiling(
    conn: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(
        tmp_path, llm_enabled=True, llm_default_provider="ollama", llm_max_calls_per_day=2
    )
    fake_provider = _FakeProvider()
    monkeypatch.setattr(summarize_module, "_build_provider", lambda *_a, **_kw: fake_provider)

    # One call under the ceiling -- succeeds.
    result = await summarize_text(conn, "text one", config=config, source_label="a.md")
    assert result == "a real summary"

    # At the ceiling now (1 event recorded, max is 2) -- one more succeeds.
    result = await summarize_text(conn, "text two", config=config, source_label="b.md")
    assert result == "a real summary"

    # Ceiling reached -- refused before any provider call.
    calls_before = len(fake_provider.calls)
    with pytest.raises(LLMCallLimitError, match="daily LLM call limit"):
        await summarize_text(conn, "text three", config=config, source_label="c.md")
    assert len(fake_provider.calls) == calls_before  # no provider call was attempted


async def test_summarize_text_records_an_audit_event_without_prompt_or_response_content(
    conn: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(tmp_path, llm_enabled=True, llm_default_provider="ollama")
    fake_provider = _FakeProvider(response="the actual generated summary text")
    monkeypatch.setattr(summarize_module, "_build_provider", lambda *_a, **_kw: fake_provider)

    secret_looking_note_text = (
        "this note contains PRIVATE-SECRET-XYZ that must never be logged"  # noqa: S105
    )
    await summarize_text(
        conn, secret_looking_note_text, config=config, source_label="private-note.md"
    )

    cursor = await conn.execute(
        "SELECT event_type, source, payload_json FROM events "
        "WHERE event_type = 'llm.summarize_completed'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    event_type, source, payload_json = rows[0]
    assert event_type == "llm.summarize_completed"
    assert source == "mcp_tool_call"

    payload = json.loads(payload_json)
    assert payload["source_label"] == "private-note.md"
    assert payload["provider"] == "ollama"
    assert "model" in payload
    assert "truncated" in payload

    # The real assertion: no prompt/response content anywhere in the payload.
    assert "PRIVATE-SECRET-XYZ" not in payload_json
    assert "the actual generated summary text" not in payload_json


async def test_summarize_text_raises_on_empty_provider_response(
    conn: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(tmp_path, llm_enabled=True, llm_default_provider="ollama")
    monkeypatch.setattr(
        summarize_module, "_build_provider", lambda *_a, **_kw: _FakeProvider(response="   ")
    )

    with pytest.raises(LLMNotConfiguredError, match="empty summary"):
        await summarize_text(conn, "text", config=config, source_label="a.md")


async def test_is_ollama_reachable_against_a_real_local_server_or_a_real_refusal() -> None:
    """A real, no-mocking check against whatever Ollama state actually
    exists on the machine running this test -- both outcomes are asserted
    to be internally consistent (a reachable server answers `/api/tags`
    with 200; an unreachable one raises and is caught, returning False),
    rather than assuming either state."""
    import socket

    from athena.llm.summarize import _OLLAMA_DEFAULT_BASE_URL, _is_ollama_reachable

    reachable = await _is_ollama_reachable(_OLLAMA_DEFAULT_BASE_URL, timeout_s=2.0)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        actually_listening = probe.connect_ex(("127.0.0.1", 11434)) == 0

    assert reachable == actually_listening


async def test_summarize_text_uses_explicit_default_provider_over_ambiguity(
    conn: aiosqlite.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ATHENA_LLM_PROVIDER wins even when multiple providers
    are configured -- no ambiguity error in that case."""
    config = _make_config(
        tmp_path,
        llm_enabled=True,
        llm_default_provider="anthropic",
        openai_api_key="sk-test",  # noqa: S106 -- test fixture
        anthropic_api_key="sk-ant-test",  # noqa: S106 -- test fixture
    )
    fake_provider = _FakeProvider()
    built_with: list[str] = []

    def _fake_build(name: str, _config: AthenaConfig) -> _FakeProvider:
        built_with.append(name)
        return fake_provider

    monkeypatch.setattr(summarize_module, "_build_provider", _fake_build)

    await summarize_text(conn, "text", config=config, source_label="a.md")

    assert built_with == ["anthropic"]
