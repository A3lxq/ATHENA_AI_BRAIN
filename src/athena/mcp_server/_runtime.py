"""Shared server-lifetime state for the MCP server (docs/design/mcp-server.md).

Mirrors `athena.worker`'s own lazy-singleton pattern rather than
introducing a second, divergent way of managing config/Qdrant-client
lifetime -- the two processes (Huey worker, MCP server) are siblings, not
one built on top of the other.
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from athena.config import AthenaConfig, load_config
from athena.safety.paths import VaultRoot

config: AthenaConfig = load_config()

_qdrant_client: QdrantClient | None = None


def get_qdrant_client() -> QdrantClient:
    """Lazy, process-lifetime singleton. Deliberately a plain, unvalidated
    `QdrantClient` -- not `athena.worker`'s `_get_qdrant_client`, which
    eagerly runs `ensure_collection` (a write-side, indexing-time concern).
    Every read tool built on top of this already tolerates a missing
    collection or an unreachable server via the same degradation paths
    Phase 4/5 already established (`athena.retrieval.search`,
    `athena.intelligence.duplicates`/`related`); this mirrors
    `athena.worker.run_retrieval_evaluate`'s own reasoning for the same
    choice.
    """
    global _qdrant_client
    if _qdrant_client is None:
        _qdrant_client = QdrantClient(url=config.qdrant_url)
    return _qdrant_client


def require_vault_root() -> VaultRoot:
    if config.vault_root is None:
        raise RuntimeError(
            "ATHENA_VAULT_DIR is not set -- the MCP server cannot operate without a "
            "configured vault"
        )
    return VaultRoot.initialize(config.vault_root)
