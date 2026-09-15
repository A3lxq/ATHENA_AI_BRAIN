from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await aiosqlite.connect(tmp_path / "athena.db")
    await apply_pending_migrations(connection, DEFAULT_MIGRATIONS_DIR)
    yield connection
    await connection.close()
