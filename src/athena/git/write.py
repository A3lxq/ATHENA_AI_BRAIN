"""Commit and push -- the only two mutating Git operations this codebase
performs (docs/design/git-automation.md §2.3), plus `auto_commit_mutation`,
the shared best-effort helper every mutating MCP tool calls as its final
step.

No function in this module can force-push, hard-reset, or rewrite history
-- this is an API-level guarantee (docs/design/git-automation.md §1), not
merely an unexposed capability: there is no parameter anywhere in this
module that reaches `--force`/`--force-with-lease`, `reset --hard`, or a
branch-delete invocation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

import aiosqlite

from athena.db.repository import events as events_repo
from athena.git.read import is_git_repository
from athena.git.wrapper import GitFailureKind, run_git, with_end_of_options
from athena.safety.paths import VaultRoot

__all__ = ["CommitResult", "PushResult", "commit_paths", "push", "auto_commit_mutation"]

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_PUSH_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    sha: str | None
    failure_kind: GitFailureKind
    dry_run_preview: str | None


@dataclass(frozen=True)
class PushResult:
    pushed: bool
    failure_kind: GitFailureKind
    dry_run_preview: str | None


async def commit_paths(
    vault_root: VaultRoot,
    paths: list[str],
    message: str,
    *,
    dry_run: bool = False,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> CommitResult:
    """Stage and commit exactly `paths` -- never `git add -A`/`git add .`
    here (that's `git_commit`'s standalone-tool exception, not this
    function's job). Scoping the commit to `paths` via `git commit --
    <paths>` means unrelated dirty state elsewhere in the working tree is
    left untouched, matching `docs/GIT_WORKFLOW.md`'s "small, meaningful,
    one logical change" commit policy.

    Returns `failure_kind=NOTHING_TO_COMMIT` (not an exception) when
    `paths` have no actual diff to commit -- a legitimate, common outcome,
    not a caller error.
    """
    if dry_run:
        diff_result = await run_git(
            with_end_of_options(["diff", "HEAD"], paths), cwd=vault_root.path, timeout_s=timeout_s
        )
        preview_body = diff_result.stdout if diff_result.ok else ""
        preview = f"would commit with message: {message!r}\n\n{preview_body}"
        return CommitResult(
            committed=False, sha=None, failure_kind=GitFailureKind.NONE, dry_run_preview=preview
        )

    add_result = await run_git(
        with_end_of_options(["add"], paths), cwd=vault_root.path, timeout_s=timeout_s
    )
    if not add_result.ok:
        return CommitResult(
            committed=False, sha=None, failure_kind=add_result.failure_kind, dry_run_preview=None
        )

    commit_result = await run_git(
        with_end_of_options(["commit", "-m", message], paths),
        cwd=vault_root.path,
        timeout_s=timeout_s,
    )
    if not commit_result.ok:
        return CommitResult(
            committed=False,
            sha=None,
            failure_kind=commit_result.failure_kind,
            dry_run_preview=None,
        )

    sha_result = await run_git(["rev-parse", "HEAD"], cwd=vault_root.path, timeout_s=timeout_s)
    sha = sha_result.stdout.strip() if sha_result.ok else None
    return CommitResult(
        committed=True, sha=sha, failure_kind=GitFailureKind.NONE, dry_run_preview=None
    )


async def push(
    vault_root: VaultRoot,
    *,
    remote: str = "origin",
    dry_run: bool = False,
    timeout_s: float = _DEFAULT_PUSH_TIMEOUT_S,
) -> PushResult:
    """Push the currently checked-out branch to `remote`. Never accepts a
    branch-name parameter from the caller (docs/design/git-automation.md
    §0) -- the branch pushed is always whatever `git rev-parse --abbrev-ref
    HEAD` reports (read from the repository itself, never caller input),
    closing off caller-supplied-ref injection entirely. Never `--force`.

    Always passes `--set-upstream`: a real finding during implementation --
    `git push --set-upstream <remote>` (relying on the current branch by
    omission) fails with "no upstream branch" even on a brand-new remote;
    `--set-upstream` only works combined with an *explicit* branch name.
    Without this, a freshly-added remote would need a one-time manual
    `git push -u origin <branch>` before this module's push ever worked --
    an undocumented prerequisite this design doesn't want to impose.
    `--set-upstream` on an already-tracked branch is a harmless no-op
    re-affirmation, not a repeated side effect.
    """
    branch_result = await run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=vault_root.path, timeout_s=timeout_s
    )
    if not branch_result.ok:
        return PushResult(
            pushed=False, failure_kind=branch_result.failure_kind, dry_run_preview=None
        )
    branch = branch_result.stdout.strip()

    args = ["push"]
    if dry_run:
        args.append("--dry-run")
    args.extend(["--set-upstream", remote])
    result = await run_git(
        with_end_of_options(args, [branch]), cwd=vault_root.path, timeout_s=timeout_s
    )

    if dry_run:
        return PushResult(
            pushed=False,
            failure_kind=result.failure_kind,
            dry_run_preview=result.stderr or result.stdout,
        )
    return PushResult(pushed=result.ok, failure_kind=result.failure_kind, dry_run_preview=None)


async def auto_commit_mutation(
    conn: aiosqlite.Connection,
    vault_root: VaultRoot,
    *,
    paths: list[str],
    operation: str,
    detail: str,
    enabled: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> None:
    """The shared, best-effort step every mutating MCP tool calls last
    (docs/design/git-automation.md §2.4). Never raises -- an auto-commit
    failure must never make the triggering `note_update`/`research_commit`/
    etc. call appear to fail from the caller's perspective, per
    `docs/GIT_WORKFLOW.md`'s explicit requirement. Mirrors the exact
    best-effort pattern `athena.intelligence.merge.merge_notes`/
    `athena.research.workflow.commit_draft` already use for their own
    non-critical trailing steps.

    `enabled` is `config.git_auto_commit_enabled` -- callers pass it
    through rather than this function reading config itself, matching
    every other module in this codebase that takes config values as
    explicit parameters instead of importing `athena.config` directly.
    `False` no-ops immediately, logged at debug level, before any git
    subprocess call.

    Records a `git.commit_completed` event (docs/EVENT_MODEL.md §1.2) on a
    successful commit -- the "structured provenance field" naming which
    operation produced the commit that `docs/SECURITY_MODEL.md`'s action
    item 2 asked for, carried in the commit message and this event's
    payload rather than a new schema.
    """
    if not enabled:
        logger.debug("auto-commit disabled -- skipping for %r", operation)
        return

    try:
        if not await is_git_repository(vault_root, timeout_s=timeout_s):
            logger.info(
                "vault is not a Git repository -- skipping auto-commit for %r "
                "(run `git init` in the vault directory to enable Git automation)",
                operation,
            )
            return

        message = f"{operation}: {detail}"
        result = await commit_paths(vault_root, paths, message, timeout_s=timeout_s)

        if not result.committed:
            if result.failure_kind is GitFailureKind.NOTHING_TO_COMMIT:
                logger.debug("auto-commit for %r: nothing to commit", operation)
            else:
                logger.warning(
                    "auto-commit for %r did not complete: %s", operation, result.failure_kind
                )
            return

        await events_repo.append_event(
            conn,
            event_type="git.commit_completed",
            source="git_operation",
            correlation_id=str(uuid4()),
            payload={
                "commit_sha": result.sha,
                "files_changed": paths,
                "message": message,
                "push_status": "not_attempted",
            },
        )
    except Exception:
        logger.warning("auto_commit_mutation raised unexpectedly for %r", operation, exc_info=True)
