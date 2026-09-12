"""The one subprocess primitive every Git operation in this codebase goes
through (docs/design/git-automation.md §2.1), implementing ADR-0005's
decision: argument-list-only `asyncio.create_subprocess_exec`, never
`shell=True`, with a hand-written exit-code/stderr failure taxonomy.

`GIT_TERMINAL_PROMPT=0` is set on every invocation (in addition to
`timeout_s`, defense in depth) so a `push`/`fetch` against a remote
requiring interactive credentials fails fast and cleanly (git itself
reports it as an auth failure) rather than hanging on a prompt nothing
will ever answer -- the correct, git-native mechanism for this, not
something the timeout alone should have to catch.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "GitFailureKind",
    "GitResult",
    "run_git",
    "with_end_of_options",
]

logger = logging.getLogger(__name__)


class GitFailureKind(Enum):
    """A best-effort, non-exhaustive classification (docs/design/
    git-automation.md §2.1) -- exit-code-first, stderr-substring-matching
    second. Never used to drive further command construction; purely for
    display and caller branching (e.g. "was this nothing-to-commit, or a
    real failure")."""

    NONE = "none"
    NOTHING_TO_COMMIT = "nothing_to_commit"
    MERGE_CONFLICT = "merge_conflict"
    NON_FAST_FORWARD = "non_fast_forward"
    AUTH_FAILURE = "auth_failure"
    NETWORK_FAILURE = "network_failure"
    NOT_A_REPOSITORY = "not_a_repository"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GitResult:
    exit_code: int
    stdout: str
    stderr: str
    failure_kind: GitFailureKind

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


_NOTHING_TO_COMMIT_MARKERS = ("nothing to commit", "nothing added to commit")
_MERGE_CONFLICT_MARKERS = ("conflict", "automatic merge failed")
_NON_FAST_FORWARD_MARKERS = ("non-fast-forward", "fetch first", "rejected")
_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "permission denied (publickey)",
    "could not read username",
    "could not read password",
    "403",
)
_NETWORK_FAILURE_MARKERS = (
    "could not resolve host",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "unable to access",
)
_NOT_A_REPOSITORY_MARKERS = ("not a git repository",)


def classify_git_failure(exit_code: int, stderr: str, stdout: str = "") -> GitFailureKind:
    """Classify a completed `git` invocation's outcome. Called with the
    real (lowercased) stdout/stderr text purely as data to pattern-match
    against -- never `eval`'d, executed, or re-embedded into another
    command (docs/design/git-automation.md §6).

    A real, empirically-verified quirk this function accounts for: `git
    commit` on a path with no actual diff prints "nothing to commit,
    working tree clean" to **stdout**, with an *empty* stderr, unlike
    every other failure case here (auth/network/conflict messages are all
    on stderr, the conventional channel for git's own error reporting).
    Checking stdout only for this one marker, not the others, avoids
    accidentally matching porcelain/diff output that legitimately contains
    unrelated text on stdout for other commands.
    """
    if exit_code == 0:
        return GitFailureKind.NONE

    lowered_stderr = stderr.lower()
    lowered_stdout = stdout.lower()

    def _any(markers: tuple[str, ...], text: str) -> bool:
        return any(marker in text for marker in markers)

    if _any(_NOT_A_REPOSITORY_MARKERS, lowered_stderr):
        return GitFailureKind.NOT_A_REPOSITORY
    if _any(_NOTHING_TO_COMMIT_MARKERS, lowered_stdout):
        return GitFailureKind.NOTHING_TO_COMMIT
    if _any(_MERGE_CONFLICT_MARKERS, lowered_stderr):
        return GitFailureKind.MERGE_CONFLICT
    if _any(_NON_FAST_FORWARD_MARKERS, lowered_stderr):
        return GitFailureKind.NON_FAST_FORWARD
    if _any(_AUTH_FAILURE_MARKERS, lowered_stderr):
        return GitFailureKind.AUTH_FAILURE
    if _any(_NETWORK_FAILURE_MARKERS, lowered_stderr):
        return GitFailureKind.NETWORK_FAILURE
    return GitFailureKind.UNKNOWN


def with_end_of_options(fixed_args: list[str], variable_args: list[str]) -> list[str]:
    """Insert `--end-of-options` immediately before `variable_args` (paths,
    refs, branch names -- anything that could originate from untrusted or
    dynamic input), never at the front of the whole argv.

    A real, empirically-verified placement gotcha (docs/design/
    git-automation.md §0): `--end-of-options` makes git treat every token
    AFTER it as positional, including tokens that would otherwise have been
    parsed as flags. Placing it before `fixed_args` (the trusted,
    hand-written subcommand/flags portion) would break the command itself,
    not just fail to protect anything -- so it always goes exactly between
    the two.
    """
    return [*fixed_args, "--end-of-options", *variable_args]


async def run_git(args: list[str], *, cwd: Path, timeout_s: float) -> GitResult:
    """Run `git <args>` in `cwd`, capturing stdout/stderr and classifying
    the outcome. Never raises for a failed git invocation (a nonzero exit
    is an ordinary, expected outcome for many callers -- e.g. `nothing to
    commit`) -- only a genuine inability to even start the subprocess
    propagates as an exception.

    Kills the whole process group (not just the immediate child) on
    timeout: `git` can spawn credential-helper or pager subprocesses that
    would otherwise survive the parent's death and keep holding the vault
    repository's lock files.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout_s
        )
    except TimeoutError:
        if process.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)
        logger.warning("git %s timed out after %.1fs in %s", args, timeout_s, cwd)
        return GitResult(
            exit_code=-1, stdout="", stderr="", failure_kind=GitFailureKind.TIMEOUT
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    exit_code = process.returncode if process.returncode is not None else -1

    return GitResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        failure_kind=classify_git_failure(exit_code, stderr, stdout),
    )
