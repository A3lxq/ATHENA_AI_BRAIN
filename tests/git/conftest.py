"""Real, throwaway Git repositories for `athena.git` tests -- per
docs/design/git-automation.md §7, subprocess behavior is tested for real,
never mocked, matching this project's practice for Qdrant/detect-secrets.

Local `user.name`/`user.email` are set explicitly on every repo created
here rather than relying on ambient global git config -- tests must be
hermetic regardless of what (if anything) is configured globally on the
machine running them.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from athena.db.migrate import DEFAULT_MIGRATIONS_DIR, apply_pending_migrations
from athena.safety.paths import VaultRoot


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await aiosqlite.connect(tmp_path / "athena.db")
    await apply_pending_migrations(connection, DEFAULT_MIGRATIONS_DIR)
    yield connection
    await connection.close()


def _run(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["init", "-q", "-b", "main"], cwd=path)
    _run(["config", "user.name", "Test User"], cwd=path)
    _run(["config", "user.email", "test@example.invalid"], cwd=path)


def commit_all(path: Path, message: str) -> str:
    _run(["add", "-A"], cwd=path)
    _run(["commit", "-q", "-m", message], cwd=path)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    init_repo(path)
    return path


@pytest.fixture
def vault_root(repo_dir: Path) -> VaultRoot:
    return VaultRoot.initialize(repo_dir)


@pytest.fixture
def repo_dir_with_commit(repo_dir: Path) -> Path:
    (repo_dir / "README.md").write_text("initial\n", encoding="utf-8")
    commit_all(repo_dir, "initial commit")
    return repo_dir


@pytest.fixture
def vault_root_with_commit(repo_dir_with_commit: Path) -> VaultRoot:
    return VaultRoot.initialize(repo_dir_with_commit)


@pytest.fixture
def non_repo_dir(tmp_path: Path) -> Path:
    path = tmp_path / "plain_dir"
    path.mkdir()
    return path


@pytest.fixture
def non_repo_vault_root(non_repo_dir: Path) -> VaultRoot:
    return VaultRoot.initialize(non_repo_dir)
