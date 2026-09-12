"""Tests for `athena.git.wrapper` (docs/design/git-automation.md §7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from athena.git.wrapper import GitFailureKind, classify_git_failure, run_git, with_end_of_options


async def test_run_git_success(repo_dir_with_commit: Path) -> None:
    result = await run_git(["log", "--oneline"], cwd=repo_dir_with_commit, timeout_s=10.0)

    assert result.ok
    assert result.failure_kind is GitFailureKind.NONE
    assert "initial commit" in result.stdout


async def test_run_git_nonzero_exit_is_not_raised(repo_dir: Path) -> None:
    result = await run_git(["this-is-not-a-git-command"], cwd=repo_dir, timeout_s=10.0)

    assert not result.ok
    assert result.exit_code != 0


async def test_run_git_not_a_repository(tmp_path: Path) -> None:
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    result = await run_git(["status"], cwd=plain_dir, timeout_s=10.0)

    assert result.failure_kind is GitFailureKind.NOT_A_REPOSITORY


async def test_run_git_times_out(repo_dir_with_commit: Path) -> None:
    # A vanishingly small timeout against an otherwise-fast, real command --
    # exercises the real timeout/kill path without needing a genuinely slow
    # git operation.
    result = await run_git(["log"], cwd=repo_dir_with_commit, timeout_s=1e-9)

    assert result.failure_kind is GitFailureKind.TIMEOUT
    assert result.exit_code == -1


def test_classify_git_failure_nothing_to_commit_is_matched_on_stdout_not_stderr() -> None:
    # A real, empirically-verified git quirk: this specific message goes to
    # stdout with an EMPTY stderr, unlike every other failure case here.
    assert (
        classify_git_failure(1, "", "On branch main\nnothing to commit, working tree clean")
        is GitFailureKind.NOTHING_TO_COMMIT
    )


@pytest.mark.parametrize(
    ("exit_code", "stderr", "expected"),
    [
        (0, "", GitFailureKind.NONE),
        (1, "CONFLICT (content): Merge conflict in a.md", GitFailureKind.MERGE_CONFLICT),
        (1, "Automatic merge failed; fix conflicts", GitFailureKind.MERGE_CONFLICT),
        (1, "! [rejected] main -> main (non-fast-forward)", GitFailureKind.NON_FAST_FORWARD),
        (1, "hint: Updates were rejected, fetch first", GitFailureKind.NON_FAST_FORWARD),
        (128, "fatal: Authentication failed for 'https://example/'", GitFailureKind.AUTH_FAILURE),
        (128, "Permission denied (publickey).", GitFailureKind.AUTH_FAILURE),
        (128, "fatal: could not read Username for 'https://x'", GitFailureKind.AUTH_FAILURE),
        (
            128,
            "fatal: unable to access 'https://x/': Could not resolve host: x",
            GitFailureKind.NETWORK_FAILURE,
        ),
        (128, "ssh: connect to host x port 22: Connection refused", GitFailureKind.NETWORK_FAILURE),
        (
            128,
            "fatal: not a git repository (or any of the parent directories)",
            GitFailureKind.NOT_A_REPOSITORY,
        ),
        (1, "something entirely unrecognized happened", GitFailureKind.UNKNOWN),
    ],
)
def test_classify_git_failure(exit_code: int, stderr: str, expected: GitFailureKind) -> None:
    assert classify_git_failure(exit_code, stderr) is expected


def test_with_end_of_options_places_marker_between_fixed_and_variable() -> None:
    result = with_end_of_options(["commit", "-m", "msg"], ["-weird-path.md"])

    assert result == ["commit", "-m", "msg", "--end-of-options", "-weird-path.md"]


async def test_with_end_of_options_protects_a_dash_prefixed_path(repo_dir: Path) -> None:
    """A real, empirically-relevant case: a note whose filename happens to
    start with a dash must be treated as a pathspec, never as a flag."""
    weird_path = repo_dir / "-weird-branch-like-name.md"
    weird_path.write_text("hello\n", encoding="utf-8")

    add_result = await run_git(
        with_end_of_options(["add"], ["-weird-branch-like-name.md"]),
        cwd=repo_dir,
        timeout_s=10.0,
    )
    assert add_result.ok

    commit_all_result = await run_git(
        with_end_of_options(["commit", "-m", "add weird path"], ["-weird-branch-like-name.md"]),
        cwd=repo_dir,
        timeout_s=10.0,
    )
    assert commit_all_result.ok


async def test_run_git_kills_process_group_on_timeout_without_hanging(repo_dir: Path) -> None:
    """Confirms the timeout path actually returns promptly rather than
    blocking on the killed process -- a real regression this test would
    catch if the process-group kill silently failed."""
    import time

    start = time.monotonic()
    result = await run_git(
        ["-c", "core.pager=cat", "log"], cwd=repo_dir, timeout_s=1e-9
    )
    elapsed = time.monotonic() - start

    assert result.failure_kind is GitFailureKind.TIMEOUT
    assert elapsed < 10.0
