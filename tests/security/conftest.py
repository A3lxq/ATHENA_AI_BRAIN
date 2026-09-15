"""Shared fixtures for `tests/security/` -- the exact `conn`/`huey`/
`vault_dir`/`vault_root`/`qdrant_client` convention already established by
`tests/mcp_server/conftest.py` (and mirrored by `tests/git/conftest.py`),
copied here rather than imported cross-package so this directory stays a
self-contained, independently-runnable test package like every other
fixture-owning test directory in this suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest
from huey import SqliteHuey
from qdrant_client import QdrantClient

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations
from athena.indexing.qdrant_store import ensure_collection
from athena.safety.paths import VaultRoot


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await aiosqlite.connect(tmp_path / "athena.db")
    await apply_pending_migrations(connection, DEFAULT_MIGRATIONS_DIR)
    yield connection
    await connection.close()


@pytest.fixture
def huey(tmp_path: Path) -> SqliteHuey:
    return SqliteHuey(name="athena-test", filename=str(tmp_path / "huey.db"))


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.fixture
def vault_root(vault_dir: Path) -> VaultRoot:
    return VaultRoot.initialize(vault_dir)


@pytest.fixture
def qdrant_client(huey: SqliteHuey) -> QdrantClient:
    client = QdrantClient(":memory:")
    ensure_collection(client, huey)
    return client
