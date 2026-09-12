"""Read-only Git operations against the vault repository (docs/design/
git-automation.md §2.2). No function here can mutate anything -- every
call is a `status`/`log`/`rev-list`/`rev-parse` invocation.
"""

from __future__ import annotations

from dataclasses import dataclass

from athena.git.wrapper import run_git, with_end_of_options
from athena.safety.paths import VaultRoot

__all__ = [
    "AheadBehind",
    "GitLogEntry",
    "GitStatus",
    "is_git_repository",
    "get_status",
    "get_ahead_behind",
    "get_log",
    "get_path_history",
]

_LOG_FORMAT = "%H%x1f%an%x1f%ad%x1f%s"
"""Fixed, explicit field format (sha/author/date/subject) joined by ASCII
Unit Separator (0x1f) -- a byte no legitimate commit-message subject line
contains, unlike a comma/pipe/tab a subject could plausibly embed -- so
parsing is a single `.split("\\x1f")`, never free-text scanning of git's
default human-oriented log output (docs/design/git-automation.md §2.2)."""


@dataclass(frozen=True)
class AheadBehind:
    ahead: int
    behind: int


@dataclass(frozen=True)
class GitStatus:
    is_clean: bool
    changed_paths: list[str]
    ahead_behind: AheadBehind | None


@dataclass(frozen=True)
class GitLogEntry:
    sha: str
    author: str
    date: str
    subject: str


async def is_git_repository(vault_root: VaultRoot, *, timeout_s: float = 10.0) -> bool:
    result = await run_git(
        ["rev-parse", "--is-inside-work-tree"], cwd=vault_root.path, timeout_s=timeout_s
    )
    return result.ok and result.stdout.strip() == "true"


def _parse_porcelain_line(line: str) -> str:
    # "XY PATH" or "XY ORIG -> PATH" (renames) -- the path portion starts
    # after the two status chars and one space; a rename keeps only the
    # new (post-arrow) path, matching what a caller actually wants to act
    # on ("what path exists now").
    rest = line[3:]
    if " -> " in rest:
        return rest.split(" -> ", 1)[1]
    return rest


async def get_status(vault_root: VaultRoot, *, timeout_s: float = 10.0) -> GitStatus:
    result = await run_git(
        with_end_of_options(["status", "--porcelain=v1"], []),
        cwd=vault_root.path,
        timeout_s=timeout_s,
    )
    if not result.ok:
        return GitStatus(is_clean=True, changed_paths=[], ahead_behind=None)

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    changed_paths = [_parse_porcelain_line(line) for line in lines]
    ahead_behind = await get_ahead_behind(vault_root, timeout_s=timeout_s)
    return GitStatus(
        is_clean=not changed_paths, changed_paths=changed_paths, ahead_behind=ahead_behind
    )


async def get_ahead_behind(
    vault_root: VaultRoot, *, timeout_s: float = 10.0
) -> AheadBehind | None:
    """`None` (not an error) when no upstream is configured -- a vault
    repository with local-only history and no remote yet is a normal,
    expected state (docs/design/git-automation.md §5)."""
    result = await run_git(
        with_end_of_options(
            ["rev-list", "--left-right", "--count"], ["@{u}...HEAD"]
        ),
        cwd=vault_root.path,
        timeout_s=timeout_s,
    )
    if not result.ok:
        return None
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return None
    behind_str, ahead_str = parts
    return AheadBehind(ahead=int(ahead_str), behind=int(behind_str))


def _parse_log_output(stdout: str) -> list[GitLogEntry]:
    entries = []
    for line in stdout.split("\x1e"):
        line = line.strip("\n")
        if not line:
            continue
        fields = line.split("\x1f")
        if len(fields) != 4:
            continue
        sha, author, date, subject = fields
        entries.append(GitLogEntry(sha=sha, author=author, date=date, subject=subject))
    return entries


async def get_log(
    vault_root: VaultRoot, *, limit: int = 20, timeout_s: float = 10.0
) -> list[GitLogEntry]:
    result = await run_git(
        with_end_of_options(
            ["log", f"--pretty=format:{_LOG_FORMAT}%x1e", f"--max-count={limit}"], []
        ),
        cwd=vault_root.path,
        timeout_s=timeout_s,
    )
    if not result.ok:
        return []
    return _parse_log_output(result.stdout)


async def get_path_history(
    vault_root: VaultRoot, path: str, *, limit: int = 20, timeout_s: float = 10.0
) -> list[GitLogEntry]:
    """Uses `--follow` so a note's history survives a rename -- directly
    serves the `note_history` MCP tool (docs/design/git-automation.md
    §2.5)."""
    result = await run_git(
        with_end_of_options(
            [
                "log",
                "--follow",
                f"--pretty=format:{_LOG_FORMAT}%x1e",
                f"--max-count={limit}",
            ],
            [path],
        ),
        cwd=vault_root.path,
        timeout_s=timeout_s,
    )
    if not result.ok:
        return []
    return _parse_log_output(result.stdout)
