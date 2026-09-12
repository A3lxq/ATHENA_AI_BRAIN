"""Tests for `athena.mcp_server.git_tools` (docs/design/git-automation.md
§2.5/§7) -- against real, throwaway Git repositories, reusing `tests/git/
conftest.py`'s `init_repo`/`commit_all` helpers rather than mocking, matching
this project's established practice for Git-layer tests.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import aiosqlite
import pytest
from tests.git.conftest import commit_all, init_repo

from athena.config import AthenaConfig
from athena.mcp_server import _runtime, git_tools
from athena.safety.paths import VaultRoot


def _config(tmp_path: Path, vault_root: VaultRoot) -> AthenaConfig:
    return AthenaConfig(
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
    )


def _patch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vault_root: VaultRoot
) -> None:
    config = _config(tmp_path, vault_root)
    monkeypatch.setattr(_runtime, "config", config)
    monkeypatch.setattr(_runtime, "require_vault_root", lambda: vault_root)


@pytest.fixture
def repo_vault_dir(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    init_repo(path)
    return path


@pytest.fixture
def repo_vault_root(repo_vault_dir: Path) -> VaultRoot:
    return VaultRoot.initialize(repo_vault_dir)


@pytest.fixture
def non_repo_vault_dir(tmp_path: Path) -> Path:
    path = tmp_path / "plain_dir"
    path.mkdir()
    return path


@pytest.fixture
def non_repo_vault_root(non_repo_vault_dir: Path) -> VaultRoot:
    return VaultRoot.initialize(non_repo_vault_dir)


# --- git_status --------------------------------------------------------


async def test_git_status_not_a_git_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, non_repo_vault_root: VaultRoot
) -> None:
    _patch(monkeypatch, tmp_path, non_repo_vault_root)

    result = await git_tools.git_status()

    assert "not a Git repository" in result


async def test_git_status_reports_a_clean_working_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_status()

    assert "working tree: clean" in result


async def test_git_status_reports_a_dirty_working_tree_and_changed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    (repo_vault_dir / "b.md").write_text("b\n", encoding="utf-8")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_status()

    assert "working tree: dirty" in result
    assert "b.md" in result


async def test_git_status_omits_ahead_behind_without_upstream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_status()

    assert "ahead" not in result


# --- git_log -------------------------------------------------------------


async def test_git_log_not_a_git_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, non_repo_vault_root: VaultRoot
) -> None:
    _patch(monkeypatch, tmp_path, non_repo_vault_root)

    result = await git_tools.git_log()

    assert "not a Git repository" in result


async def test_git_log_empty_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo_vault_root: VaultRoot
) -> None:
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_log()

    assert "no commit history" in result


async def test_git_log_lists_real_commits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "first commit")
    (repo_vault_dir / "b.md").write_text("b\n", encoding="utf-8")
    commit_all(repo_vault_dir, "second commit")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_log()
    lines = result.splitlines()

    assert len(lines) == 2
    assert "second commit" in lines[0]
    assert "first commit" in lines[1]


async def test_git_log_respects_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    for i in range(3):
        (repo_vault_dir / f"n{i}.md").write_text(f"{i}\n", encoding="utf-8")
        commit_all(repo_vault_dir, f"commit {i}")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_log(limit=1)

    assert len(result.splitlines()) == 1
    assert "commit 2" in result


# --- note_history ----------------------------------------------------------


async def test_note_history_not_a_git_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, non_repo_vault_root: VaultRoot
) -> None:
    _patch(monkeypatch, tmp_path, non_repo_vault_root)

    result = await git_tools.note_history("a.md")

    assert "not a Git repository" in result


async def test_note_history_no_commits_for_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.note_history("never-existed.md")

    assert "no commit history" in result
    assert "never-existed.md" in result


async def test_note_history_lists_real_commits_for_a_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "create a.md")
    (repo_vault_dir / "a.md").write_text("a changed\n", encoding="utf-8")
    commit_all(repo_vault_dir, "update a.md")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.note_history("a.md")
    lines = result.splitlines()

    assert len(lines) == 2
    assert "update a.md" in lines[0]
    assert "create a.md" in lines[1]


# --- git_commit ------------------------------------------------------------


async def test_git_commit_not_a_git_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, non_repo_vault_root: VaultRoot
) -> None:
    _patch(monkeypatch, tmp_path, non_repo_vault_root)

    result = await git_tools.git_commit()

    assert "not a Git repository" in result


async def test_git_commit_dry_run_default_is_a_real_commit_not_a_preview(
    conn: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    """Asserting the default explicitly (docs/TESTING_STRATEGY.md's
    convention): unlike `research_commit` (which defaults `dry_run=True`),
    `git_commit`'s own design (docs/design/git-automation.md §2.5) defaults
    `dry_run=False` -- omitting the parameter performs a REAL commit. This is
    a deliberate, documented asymmetry (git_commit is the least-destructive,
    non-MRTR-gated mutation category this server exposes), not an oversight
    -- worth asserting explicitly since it's easy to assume every dry_run
    parameter in this codebase defaults the same way.
    """
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    (repo_vault_dir / "b.md").write_text("b\n", encoding="utf-8")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_commit()

    assert "committed" in result
    assert "dry run" not in result
    assert (repo_vault_dir / "b.md").exists()  # committed, not deleted -- content is unaffected


async def test_git_commit_dry_run_true_makes_no_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    (repo_vault_dir / "b.md").write_text("b\n", encoding="utf-8")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_commit(dry_run=True)

    assert "[dry run]" in result
    status = subprocess.run(
        ["git", "status", "--porcelain"],  # noqa: S607
        cwd=repo_vault_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "b.md" in status.stdout  # still untracked -- nothing was committed


async def test_git_commit_records_a_commit_completed_event(
    conn: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    (repo_vault_dir / "b.md").write_text("b\n", encoding="utf-8")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_commit(message="add b.md")

    assert "committed" in result

    cursor = await conn.execute(
        "SELECT source, payload_json FROM events WHERE event_type = 'git.commit_completed'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "mcp_tool_call"
    payload = json.loads(row[1])
    assert payload["message"] == "add b.md"
    assert payload["push_status"] == "not_attempted"
    assert "b.md" in payload["files_changed"]


async def test_git_commit_nothing_to_commit(
    conn: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    result = await git_tools.git_commit()

    assert result == "nothing to commit"


async def test_git_commit_auto_generates_a_message_when_none_given(
    conn: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_vault_dir: Path,
    repo_vault_root: VaultRoot,
) -> None:
    (repo_vault_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_vault_dir, "initial commit")
    (repo_vault_dir / "b.md").write_text("b\n", encoding="utf-8")
    (repo_vault_dir / "c.md").write_text("c\n", encoding="utf-8")
    _patch(monkeypatch, tmp_path, repo_vault_root)

    await git_tools.git_commit()

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],  # noqa: S607
        cwd=repo_vault_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "manual commit via git_commit: 2 file(s) changed"
