"""The `Protocol`-based multi-provider LLM adapter (docs/design/
multi-llm.md §2.1), implementing ADR-0003's deferred decision.

Every implementation wraps exactly one official SDK's genuinely-async
client (`AsyncOpenAI`/`AsyncAnthropic`/`google.genai`'s `.aio` namespace/
`ollama.AsyncClient` -- all confirmed httpx-based, not sync-wrapped-in-
async facades, during this design's research) and is safe to call directly
from an already-running async MCP tool handler. Every call is still
wrapped in `asyncio.wait_for(..., timeout=timeout_s)` uniformly here,
rather than relying on each SDK's own inconsistently-shaped native timeout
parameter (Ollama's `chat()` exposes none at the per-call level at all) --
defense in depth, matching `athena.git.wrapper`'s own belt-and-suspenders
timeout enforcement.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Protocol

from anthropic import AsyncAnthropic
from google import genai
from google.genai.types import GenerateContentConfig
from ollama import AsyncClient as AsyncOllamaClient
from openai import AsyncOpenAI

__all__ = [
    "LLMTimeoutError",
    "LLMProviderError",
    "LLMProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    "GoogleProvider",
    "OllamaProvider",
]


class LLMTimeoutError(Exception):
    """A provider call did not complete within `timeout_s`."""


class LLMProviderError(Exception):
    """A provider SDK raised -- the original exception is chained via
    `raise ... from exc`, never swallowed, but callers of this module never
    need to know four different SDKs' four different exception hierarchies."""


class LLMProvider(Protocol):
    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, timeout_s: float
    ) -> str: ...


async def _with_timeout[T](coro: Awaitable[T], *, timeout_s: float) -> T:
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except TimeoutError as exc:
        raise LLMTimeoutError(f"provider call exceeded {timeout_s}s") from exc


class OpenAIProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def complete(self, *, system: str, prompt: str, max_tokens: int, timeout_s: float) -> str:
        try:
            response = await _with_timeout(
                self._client.responses.create(
                    model=self._model,
                    input=prompt,
                    instructions=system,
                    max_output_tokens=max_tokens,
                ),
                timeout_s=timeout_s,
            )
        except LLMTimeoutError:
            raise
        except Exception as exc:
            raise LLMProviderError(f"OpenAI call failed: {exc}") from exc
        return str(response.output_text)


class AnthropicProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def complete(self, *, system: str, prompt: str, max_tokens: int, timeout_s: float) -> str:
        try:
            message = await _with_timeout(
                self._client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout_s=timeout_s,
            )
        except LLMTimeoutError:
            raise
        except Exception as exc:
            raise LLMProviderError(f"Anthropic call failed: {exc}") from exc

        for block in message.content:
            if hasattr(block, "text"):
                return str(block.text)
        raise LLMProviderError("Anthropic response contained no text block")


class GoogleProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def complete(self, *, system: str, prompt: str, max_tokens: int, timeout_s: float) -> str:
        try:
            response = await _with_timeout(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=GenerateContentConfig(
                        system_instruction=system, max_output_tokens=max_tokens
                    ),
                ),
                timeout_s=timeout_s,
            )
        except LLMTimeoutError:
            raise
        except Exception as exc:
            raise LLMProviderError(f"Google call failed: {exc}") from exc
        text = response.text
        if text is None:
            raise LLMProviderError("Google response contained no text")
        return str(text)


class OllamaProvider:
    def __init__(self, base_url: str, model: str) -> None:
        self._client = AsyncOllamaClient(host=base_url)
        self._model = model

    async def complete(self, *, system: str, prompt: str, max_tokens: int, timeout_s: float) -> str:
        # No `max_tokens` equivalent is passed -- `options.num_predict`
        # exists but is model/deployment-specific enough that this design
        # leaves it at the server's own default rather than guessing a
        # value (docs/design/multi-llm.md §2.1).
        try:
            response = await _with_timeout(
                self._client.chat(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                ),
                timeout_s=timeout_s,
            )
        except LLMTimeoutError:
            raise
        except Exception as exc:
            raise LLMProviderError(f"Ollama call failed: {exc}") from exc
        return str(response.message.content)
