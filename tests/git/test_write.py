"""Tests for `athena.git.write` (docs/design/git-automation.md §7)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import aiosqlite
from tests.git.conftest import commit_all, init_repo

from athena.git.wrapper import GitFailureKind
from athena.git.write import auto_commit_mutation, commit_paths, push
from athena.safety.paths import VaultRoot


async def test_commit_paths_creates_a_real_commit(
    vault_root_with_commit: VaultRoot, repo_dir_with_commit: Path
) -> None:
    (repo_dir_with_commit / "note.md").write_text("hello\n", encoding="utf-8")

    result = await commit_paths(vault_root_with_commit, ["note.md"], "add note.md")

    assert result.committed is True
    assert result.sha is not None
    assert result.failure_kind is GitFailureKind.NONE

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo_dir_with_commit,
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "add note.md"


async def test_commit_paths_scopes_the_commit_to_exactly_the_given_paths(
    vault_root_with_commit: VaultRoot, repo_dir_with_commit: Path
) -> None:
    (repo_dir_with_commit / "wanted.md").write_text("wanted\n", encoding="utf-8")
    (repo_dir_with_commit / "unrelated.md").write_text("unrelated\n", encoding="utf-8")

    result = await commit_paths(vault_root_with_commit, ["wanted.md"], "add wanted.md only")

    assert result.committed is True
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir_with_commit,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "unrelated.md" in status.stdout
    assert "wanted.md" not in status.stdout


async def test_commit_paths_nothing_to_commit(vault_root_with_commit: VaultRoot) -> None:
    result = await commit_paths(vault_root_with_commit, ["README.md"], "no-op commit")

    assert result.committed is False
    assert result.failure_kind is GitFailureKind.NOTHING_TO_COMMIT


async def test_commit_paths_dry_run_writes_nothing(
    vault_root_with_commit: VaultRoot, repo_dir_with_commit: Path
) -> None:
    (repo_dir_with_commit / "note.md").write_text("hello\n", encoding="utf-8")

    result = await commit_paths(
        vault_root_with_commit, ["note.md"], "would add note.md", dry_run=True
    )

    assert result.committed is False
    assert result.sha is None
    assert result.dry_run_preview is not None
    assert "would add note.md" in result.dry_run_preview

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir_with_commit,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "note.md" in status.stdout  # still untracked, never staged/committed


async def test_commit_paths_handles_a_deleted_file(
    vault_root_with_commit: VaultRoot, repo_dir_with_commit: Path
) -> None:
    (repo_dir_with_commit / "README.md").unlink()

    result = await commit_paths(vault_root_with_commit, ["README.md"], "delete README.md")

    assert result.committed is True


async def test_push_to_a_real_local_bare_remote(tmp_path: Path) -> None:
    bare_dir = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare_dir)],
        check=True,
        capture_output=True,
    )

    repo_dir = tmp_path / "vault"
    init_repo(repo_dir)
    (repo_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_dir, "first commit")
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare_dir)],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    result = await push(VaultRoot.initialize(repo_dir), remote="origin")

    assert result.pushed is True
    assert result.failure_kind is GitFailureKind.NONE


async def test_push_dry_run_does_not_actually_push(tmp_path: Path) -> None:
    bare_dir = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare_dir)],
        check=True,
        capture_output=True,
    )

    repo_dir = tmp_path / "vault"
    init_repo(repo_dir)
    (repo_dir / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_dir, "first commit")
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare_dir)],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    result = await push(VaultRoot.initialize(repo_dir), remote="origin", dry_run=True)

    assert result.pushed is False
    assert result.dry_run_preview is not None

    # A bare repo with nothing pushed to it yet has no refs at all.
    show_ref = subprocess.run(["git", "show-ref"], cwd=bare_dir, capture_output=True, text=True)
    assert show_ref.stdout.strip() == ""


async def test_push_rejects_non_fast_forward(tmp_path: Path) -> None:
    bare_dir = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare_dir)],
        check=True,
        capture_output=True,
    )

    repo_a = tmp_path / "repo_a"
    init_repo(repo_a)
    (repo_a / "a.md").write_text("a\n", encoding="utf-8")
    commit_all(repo_a, "first commit")
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare_dir)],
        cwd=repo_a,
        check=True,
        capture_output=True,
    )
    first_push = await push(VaultRoot.initialize(repo_a), remote="origin")
    assert first_push.pushed is True

    repo_b = tmp_path / "repo_b"
    subprocess.run(
        ["git", "clone", "-q", str(bare_dir), str(repo_b)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=repo_b, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo_b,
        check=True,
        capture_output=True,
    )
    (repo_b / "b.md").write_text("b\n", encoding="utf-8")
    commit_all(repo_b, "diverging commit from repo_b")

    (repo_a / "c.md").write_text("c\n", encoding="utf-8")
    commit_all(repo_a, "diverging commit from repo_a")
    second_push = await push(VaultRoot.initialize(repo_a), remote="origin")
    assert second_push.pushed is True

    rejected_push = await push(VaultRoot.initialize(repo_b), remote="origin")

    assert rejected_push.pushed is False
    assert rejected_push.failure_kind is GitFailureKind.NON_FAST_FORWARD


async def test_auto_commit_mutation_commits_and_records_an_event(
    conn: aiosqlite.Connection, vault_root_with_commit: VaultRoot, repo_dir_with_commit: Path
) -> None:
    (repo_dir_with_commit / "note.md").write_text("hello\n", encoding="utf-8")

    await auto_commit_mutation(
        conn, vault_root_with_commit, paths=["note.md"], operation="note_create", detail="note.md"
    )

    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo_dir_with_commit,
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "note_create: note.md"

    cursor = await conn.execute(
        "SELECT event_type, source, payload_json FROM events "
        "WHERE event_type = 'git.commit_completed'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[1] == "git_operation"
    payload = json.loads(row[2])
    assert payload["files_changed"] == ["note.md"]
    assert payload["message"] == "note_create: note.md"
    assert payload["push_status"] == "not_attempted"


async def test_auto_commit_mutation_no_ops_when_disabled(
    conn: aiosqlite.Connection, vault_root_with_commit: VaultRoot, repo_dir_with_commit: Path
) -> None:
    (repo_dir_with_commit / "note.md").write_text("hello\n", encoding="utf-8")

    await auto_commit_mutation(
        conn,
        vault_root_with_commit,
        paths=["note.md"],
        operation="note_create",
        detail="note.md",
        enabled=False,
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir_with_commit,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "note.md" in status.stdout  # still untracked -- nothing was committed
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'git.commit_completed'"
    )
    assert (await cursor.fetchone())[0] == 0


async def test_auto_commit_mutation_no_ops_when_not_a_git_repository(
    conn: aiosqlite.Connection, non_repo_vault_root: VaultRoot
) -> None:
    # Must not raise even though there's no repository to commit into.
    await auto_commit_mutation(
        conn, non_repo_vault_root, paths=["note.md"], operation="note_create", detail="note.md"
    )

    cursor = await conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'git.commit_completed'"
    )
    assert (await cursor.fetchone())[0] == 0


async def test_auto_commit_mutation_never_raises_on_nothing_to_commit(
    conn: aiosqlite.Connection, vault_root_with_commit: VaultRoot
) -> None:
    await auto_commit_mutation(
        conn,
        vault_root_with_commit,
        paths=["README.md"],
        operation="note_update",
        detail="README.md",
    )

    cursor = await conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'git.commit_completed'"
    )
    assert (await cursor.fetchone())[0] == 0
