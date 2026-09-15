"""Orchestration for LLM-backed summarization (docs/design/multi-llm.md
§2.2/§2.3) -- the single entry point both `note_summarize` (MCP) and
`athena llm summarize` (CLI) call.

Resolves the opt-in gate, provider selection, the daily call ceiling, and
audit logging in one place, so neither caller has to duplicate this
sequencing (CLAUDE.md rule 15: internal modules stay decoupled from MCP
transport, exercised here since the CLI is the second, non-MCP caller).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

import aiosqlite
import httpx

from athena.config import AthenaConfig
from athena.db.repository import events as events_repo
from athena.llm.provider import (
    AnthropicProvider,
    GoogleProvider,
    LLMProvider,
    OllamaProvider,
    OpenAIProvider,
)

__all__ = [
    "LLMDisabledError",
    "LLMNotConfiguredError",
    "LLMCallLimitError",
    "resolve_provider",
    "summarize_text",
]

logger = logging.getLogger(__name__)

_SUMMARIZE_EVENT_TYPE = "llm.summarize_completed"
_SUMMARIZE_SYSTEM_PROMPT = (
    "Summarize the following note content concisely. Output only the summary, no preamble."
)

_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-5.6-terra",
    "anthropic": "claude-sonnet-5",
    "google": "gemini-3.8-flash",
    "ollama": "llama3.1",
}

_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"


class LLMDisabledError(Exception):
    """`config.llm_enabled` is `False` -- the standing opt-in gate
    (docs/design/multi-llm.md §1/§6, resolving ADR-0007's open question)."""


class LLMNotConfiguredError(Exception):
    """No provider is resolvable: zero configured, or multiple configured
    with no explicit `ATHENA_LLM_PROVIDER` default."""


class LLMCallLimitError(Exception):
    """`config.llm_max_calls_per_day` has already been reached today."""


async def _is_ollama_reachable(base_url: str, timeout_s: float = 1.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(f"{base_url}/api/tags")
            return response.status_code == 200
    except Exception:
        return False


async def _configured_providers(config: AthenaConfig) -> list[str]:
    """A real finding during testing: `config.ollama_base_url` always has a
    truthy default value (unlike the other three providers' `None`-unless-
    set API key fields), so treating "the field is set" as "configured"
    would make Ollama look configured in every environment, including ones
    with no local Ollama server at all -- surfacing as a confusing
    connection-refused/404 error instead of the clean "not configured"
    message this design promises (docs/design/multi-llm.md §2.3's own text
    already anticipated exactly this and specified the fix: only an
    *explicitly overridden* `ATHENA_OLLAMA_BASE_URL` counts as configured
    outright; the still-default URL counts only if actually reachable right
    now, checked live rather than cached, since a local server's
    availability can change between calls).
    """
    providers = []
    if config.openai_api_key:
        providers.append("openai")
    if config.anthropic_api_key:
        providers.append("anthropic")
    if config.google_api_key:
        providers.append("google")
    if config.ollama_base_url != _OLLAMA_DEFAULT_BASE_URL or await _is_ollama_reachable(
        config.ollama_base_url
    ):
        providers.append("ollama")
    return providers


def _build_provider(name: str, config: AthenaConfig) -> LLMProvider:
    model = config.llm_default_model or _DEFAULT_MODELS[name]
    if name == "openai":
        if not config.openai_api_key:
            raise LLMNotConfiguredError("ATHENA_OPENAI_API_KEY is not set")
        return OpenAIProvider(api_key=config.openai_api_key, model=model)
    if name == "anthropic":
        if not config.anthropic_api_key:
            raise LLMNotConfiguredError("ATHENA_ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(api_key=config.anthropic_api_key, model=model)
    if name == "google":
        if not config.google_api_key:
            raise LLMNotConfiguredError("ATHENA_GOOGLE_API_KEY is not set")
        return GoogleProvider(api_key=config.google_api_key, model=model)
    if name == "ollama":
        return OllamaProvider(base_url=config.ollama_base_url, model=model)
    raise LLMNotConfiguredError(
        f"unknown provider {name!r}; must be one of 'openai', 'anthropic', 'google', 'ollama'"
    )


async def _resolve_provider_name(config: AthenaConfig) -> str:
    """Per docs/design/multi-llm.md §2.3: an explicit `ATHENA_LLM_PROVIDER`
    always wins; otherwise exactly one configured provider is required --
    zero or multiple configured providers with no explicit default is
    refused outright, never guessed."""
    if config.llm_default_provider:
        return config.llm_default_provider

    configured = await _configured_providers(config)
    if len(configured) == 1:
        return configured[0]
    if not configured:
        raise LLMNotConfiguredError(
            "no LLM provider is configured -- set ATHENA_OPENAI_API_KEY, "
            "ATHENA_ANTHROPIC_API_KEY, ATHENA_GOOGLE_API_KEY, or run a local Ollama server"
        )
    raise LLMNotConfiguredError(
        f"multiple providers are configured ({', '.join(configured)}) with no "
        "explicit default -- set ATHENA_LLM_PROVIDER to choose one"
    )


async def resolve_provider(config: AthenaConfig) -> LLMProvider:
    """Resolve and construct the configured default provider."""
    name = await _resolve_provider_name(config)
    return _build_provider(name, config)


async def _calls_today(conn: aiosqlite.Connection) -> int:
    today_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ? AND occurred_at LIKE ?",
        (_SUMMARIZE_EVENT_TYPE, f"{today_prefix}%"),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def summarize_text(
    conn: aiosqlite.Connection,
    text: str,
    *,
    config: AthenaConfig,
    source_label: str,
    max_tokens: int = 512,
) -> str:
    """Summarize `text` via the configured default LLM provider.

    Raises `LLMDisabledError`/`LLMNotConfiguredError`/`LLMCallLimitError`
    for each of this design's three gates (§2.2); any provider-level
    failure propagates as `athena.llm.provider.LLMTimeoutError`/
    `LLMProviderError`. Callers (the MCP tool, the CLI command) catch all
    of these and return a clear message rather than letting them propagate
    further -- this function itself does not swallow anything.

    Never logs or persists the prompt/response text (docs/SECURITY_MODEL.md
    item 17) -- only `source_label`/provider/model/whether the completion
    was truncated are recorded in the audit event.
    """
    if not config.llm_enabled:
        raise LLMDisabledError(
            "LLM features are disabled -- set ATHENA_LLM_ENABLED=true and configure a provider"
        )

    provider_name = await _resolve_provider_name(config)
    provider = _build_provider(provider_name, config)

    calls_today = await _calls_today(conn)
    if calls_today >= config.llm_max_calls_per_day:
        raise LLMCallLimitError(
            f"daily LLM call limit ({config.llm_max_calls_per_day}) reached"
        )

    summary = await provider.complete(
        system=_SUMMARIZE_SYSTEM_PROMPT,
        prompt=text,
        max_tokens=max_tokens,
        timeout_s=config.llm_call_timeout_s,
    )
    if not summary.strip():
        raise LLMNotConfiguredError("provider returned an empty summary")

    model = config.llm_default_model or _DEFAULT_MODELS.get(provider_name, "unknown")
    truncated = len(summary) >= max_tokens * 4  # a rough, non-precise heuristic, logged only
    await events_repo.append_event(
        conn,
        event_type=_SUMMARIZE_EVENT_TYPE,
        source="mcp_tool_call",
        correlation_id=str(uuid4()),
        payload={
            "source_label": source_label,
            "provider": provider_name,
            "model": model,
            "truncated": truncated,
        },
    )
    return summary
