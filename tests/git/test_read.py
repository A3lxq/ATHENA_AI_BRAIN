"""Tests for `athena.git.read` (docs/design/git-automation.md §7)."""

from __future__ import annotations

from pathlib import Path

from tests.git.conftest import commit_all, init_repo

from athena.git.read import (
    get_ahead_behind,
    get_log,
    get_path_history,
    get_status,
    is_git_repository,
)
from athena.safety.paths import VaultRoot


async def test_is_git_repository_true_for_a_real_repo(vault_root: VaultRoot) -> None:
    assert await is_git_repository(vault_root) is True


async def test_is_git_repository_false_for_a_plain_directory(
    non_repo_vault_root: VaultRoot,
) -> None:
    assert await is_git_repository(non_repo_vault_root) is False


async def test_get_status_clean_repo(vault_root_with_commit: VaultRoot) -> None:
    status = await get_status(vault_root_with_commit)

    assert status.is_clean is True
    assert status.changed_paths == []


async def test_get_status_reports_untracked_and_modified_paths(
    vault_root_with_commit: VaultRoot, repo_dir_with_commit: Path
) -> None:
    (repo_dir_with_commit / "new.md").write_text("new note\n", encoding="utf-8")
    (repo_dir_with_commit / "README.md").write_text("changed\n", encoding="utf-8")

    status = await get_status(vault_root_with_commit)

    assert status.is_clean is False
    assert set(status.changed_paths) == {"new.md", "README.md"}


async def test_get_ahead_behind_none_without_upstream(vault_root_with_commit: VaultRoot) -> None:
    assert await get_ahead_behind(vault_root_with_commit) is None


async def test_get_ahead_behind_with_a_real_diverged_upstream(tmp_path: Path) -> None:
    origin_dir = tmp_path / "origin"
    init_repo(origin_dir)
    (origin_dir / "README.md").write_text("origin initial\n", encoding="utf-8")
    commit_all(origin_dir, "origin initial")

    clone_dir = tmp_path / "clone"
    import subprocess

    subprocess.run(
        ["git", "clone", "-q", str(origin_dir), str(clone_dir)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=clone_dir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=clone_dir,
        check=True,
        capture_output=True,
    )

    (clone_dir / "local.md").write_text("local only\n", encoding="utf-8")
    commit_all(clone_dir, "local-only commit")

    (origin_dir / "remote.md").write_text("remote only\n", encoding="utf-8")
    commit_all(origin_dir, "remote-only commit")
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone_dir, check=True, capture_output=True)

    ahead_behind = await get_ahead_behind(VaultRoot.initialize(clone_dir))

    assert ahead_behind is not None
    assert ahead_behind.ahead == 1
    assert ahead_behind.behind == 1


async def test_get_log_returns_entries_in_reverse_chronological_order(
    vault_root: VaultRoot, repo_dir: Path
) -> None:
    (repo_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_dir, "first commit")
    (repo_dir / "b.md").write_text("b\n", encoding="utf-8")
    commit_all(repo_dir, "second commit")

    entries = await get_log(vault_root)

    assert len(entries) == 2
    assert entries[0].subject == "second commit"
    assert entries[1].subject == "first commit"
    assert len(entries[0].sha) == 40


async def test_get_log_respects_limit(vault_root: VaultRoot, repo_dir: Path) -> None:
    for i in range(5):
        (repo_dir / f"n{i}.md").write_text(f"{i}\n", encoding="utf-8")
        commit_all(repo_dir, f"commit {i}")

    entries = await get_log(vault_root, limit=2)

    assert len(entries) == 2
    assert entries[0].subject == "commit 4"


async def test_get_path_history_follows_a_rename(vault_root: VaultRoot, repo_dir: Path) -> None:
    original = repo_dir / "original.md"
    original.write_text("x" * 200 + "\n", encoding="utf-8")
    commit_all(repo_dir, "create original")

    renamed = repo_dir / "renamed.md"
    original.rename(renamed)
    commit_all(repo_dir, "rename to renamed.md")

    history = await get_path_history(vault_root, "renamed.md")

    assert len(history) == 2
    assert history[0].subject == "rename to renamed.md"
    assert history[1].subject == "create original"


async def test_get_path_history_empty_for_a_path_with_no_commits(
    vault_root_with_commit: VaultRoot,
) -> None:
    history = await get_path_history(vault_root_with_commit, "never-existed.md")

    assert history == []
