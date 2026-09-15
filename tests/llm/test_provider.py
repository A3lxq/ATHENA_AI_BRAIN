"""Tests for `athena.llm.provider` (docs/design/multi-llm.md §7).

Every SDK client is mocked at the boundary this project's own tests never
need live third-party credentials or network calls to run -- each mock's
call shape is asserted against the real, `inspect.signature()`-verified
call shapes recorded in the design doc's §0 research (right method, right
argument names), so a drift in a provider's own call construction would
still be caught even though the underlying SDK response is faked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from athena.llm.provider import (
    AnthropicProvider,
    GoogleProvider,
    LLMProviderError,
    LLMTimeoutError,
    OllamaProvider,
    OpenAIProvider,
)


async def test_openai_provider_calls_responses_create_and_extracts_output_text() -> None:
    provider = OpenAIProvider(api_key="k", model="gpt-5.6-terra")
    provider._client.responses.create = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(output_text="a summary")
    )

    result = await provider.complete(system="sys", prompt="p", max_tokens=100, timeout_s=5.0)

    assert result == "a summary"
    provider._client.responses.create.assert_awaited_once_with(
        model="gpt-5.6-terra", input="p", instructions="sys", max_output_tokens=100
    )


async def test_anthropic_provider_calls_messages_create_and_extracts_text_block() -> None:
    provider = AnthropicProvider(api_key="k", model="claude-sonnet-5")
    provider._client.messages.create = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(content=[SimpleNamespace(text="a summary")])
    )

    result = await provider.complete(system="sys", prompt="p", max_tokens=100, timeout_s=5.0)

    assert result == "a summary"
    provider._client.messages.create.assert_awaited_once_with(
        model="claude-sonnet-5",
        max_tokens=100,
        system="sys",
        messages=[{"role": "user", "content": "p"}],
    )


async def test_anthropic_provider_raises_when_no_text_block_present() -> None:
    provider = AnthropicProvider(api_key="k", model="claude-sonnet-5")
    provider._client.messages.create = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(content=[SimpleNamespace(type="tool_use")])
    )

    with pytest.raises(LLMProviderError, match="no text block"):
        await provider.complete(system="sys", prompt="p", max_tokens=100, timeout_s=5.0)


async def test_google_provider_calls_generate_content_and_extracts_text() -> None:
    provider = GoogleProvider(api_key="k", model="gemini-3.8-flash")
    provider._client.aio.models.generate_content = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(text="a summary")
    )

    result = await provider.complete(system="sys", prompt="p", max_tokens=100, timeout_s=5.0)

    assert result == "a summary"
    provider._client.aio.models.generate_content.assert_awaited_once()
    _, kwargs = provider._client.aio.models.generate_content.call_args
    assert kwargs["model"] == "gemini-3.8-flash"
    assert kwargs["contents"] == "p"


async def test_google_provider_raises_on_empty_text() -> None:
    provider = GoogleProvider(api_key="k", model="gemini-3.8-flash")
    provider._client.aio.models.generate_content = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(text=None)
    )

    with pytest.raises(LLMProviderError, match="no text"):
        await provider.complete(system="sys", prompt="p", max_tokens=100, timeout_s=5.0)


async def test_ollama_provider_calls_chat_and_extracts_message_content() -> None:
    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3.1")
    provider._client.chat = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(message=SimpleNamespace(content="a summary"))
    )

    result = await provider.complete(system="sys", prompt="p", max_tokens=100, timeout_s=5.0)

    assert result == "a summary"
    provider._client.chat.assert_awaited_once_with(
        model="llama3.1",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "p"},
        ],
    )


async def test_provider_call_exceeding_timeout_raises_llm_timeout_error() -> None:
    provider = OpenAIProvider(api_key="k", model="gpt-5.6-terra")

    async def _slow(**_kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(10)
        return SimpleNamespace(output_text="too late")

    provider._client.responses.create = AsyncMock(side_effect=_slow)  # type: ignore[method-assign]

    with pytest.raises(LLMTimeoutError):
        await provider.complete(system="sys", prompt="p", max_tokens=100, timeout_s=0.01)


async def test_provider_sdk_exception_is_wrapped_as_llm_provider_error() -> None:
    provider = AnthropicProvider(api_key="k", model="claude-sonnet-5")
    provider._client.messages.create = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("auth failed")
    )

    with pytest.raises(LLMProviderError, match="auth failed"):
        await provider.complete(system="sys", prompt="p", max_tokens=100, timeout_s=5.0)
