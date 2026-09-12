# Session 026 — Phase 8: Git Automation

**Date:** 2026-09-12
**Phase:** 8 (Git Automation)
**Status:** Complete — no git commit made yet (Phase 7's work is already committed and pushed as `9b914dc`)

## Objective

Following "start phase 8 git automation and deploy necessary agents to
finish this," design and implement Git automation for the Obsidian vault:
safe commit workflow, push policy, backup workflow, conflict detection,
recovery, per `docs/ROADMAP.md`'s Phase 8 checklist. This resolves ADR-0005
(accepted 2026-08-24, deferred module design to implementation time) and
the four Git-related MCP tools (`note_history`, `git_status`, `git_log`,
`git_commit`) ADR-0007 had named but every prior phase had to defer.

## Research

Most of the substantive decision-making for this phase was already made at
Phase 0 (ADR-0005: purpose-built `git` CLI subprocess wrapper, never
GitPython/Dulwich as the load-bearing layer, no destructive operations
exposed) and in `docs/GIT_WORKFLOW.md`'s already-written operational
runbooks (the exact auto-commit/push policy this phase implements almost
verbatim). This session's own research focused on implementation-time
verification and the specific open items ADR-0005 left for later:

- **Git version and `--end-of-options` behavior verified directly** against
  the installed git 2.53.0 (well past ADR-0005's 2.43.1 floor). Found a
  concrete placement gotcha: `--end-of-options` must sit immediately before
  the pathspec/ref arguments, not at the front of the whole argv, since
  everything after it is treated as positional — a flag placed after it
  (e.g. `-b <branch>`) is swallowed as a literal pathspec instead of parsed.
- **Dulwich (ADR-0005's optional read-side convenience): declined**, an
  explicit decision this design makes rather than leaving unresolved —
  every operation this phase needs is a single simple subprocess call, and
  a second Git-access code path would add dependency/audit surface for no
  functional gain, matching ADR-0005's own "no convenience cost" rationale.
- **Vault auto-`git init`: declined.** Even though not destructive, silently
  creating version-control state in a user's personal vault directory
  without being asked is presumptuous. `athena doctor` gains a
  `vault_git_repo` warn-not-fail check instead.
- **A genuine, previously-flagged gap, checked rather than assumed
  resolved**: ADR-0005's "Consequences" section required
  `.pre-commit-config.yaml` + gitleaks be set up as part of Phase 1.
  Checked directly (`find . -iname ".pre-commit*"`, `which pre-commit
  gitleaks`) — neither existed anywhere in this environment. Closed in this
  pass.

## Design

`docs/design/git-automation.md` drafted covering: the core subprocess
primitive and failure taxonomy (§2.1), read-only operations (§2.2),
commit/push and the shared auto-commit helper (§2.3), the seven auto-commit
integration points (§2.4), the four MCP tools (§2.5), the periodic
auto-push task (§2.6), `vault_status`/`athena doctor` extensions (§2.7),
security considerations addressing `SECURITY_MODEL.md` TB-5/TB-6/action
item 2 (§6), and the full interface contract (§4). No schema change is
needed — `git.commit_completed`'s audit trail reuses the existing `events`
table (ADR-0010), exactly like every other domain event in this project.

Presented to the user and accepted ("all good if it fits well ans works
like butter") before any implementation code was written.

## Implementation

**Built directly first** (highest-risk subprocess/mutation code, per this
project's established practice), to fix a stable interface before
parallelizing:
- `athena.git.wrapper` — `run_git`, `GitFailureKind`, `with_end_of_options`,
  `classify_git_failure`.
- `athena.git.read` — `is_git_repository`, `get_status`, `get_ahead_behind`,
  `get_log`, `get_path_history`.
- `athena.git.write` — `commit_paths`, `push`, `auto_commit_mutation`.
- `AthenaConfig` gained `git_auto_commit_enabled`/`git_auto_push_enabled`/
  `git_push_interval_minutes`/`git_command_timeout_s`.
- `athena.worker.git_push_task` (periodic, off by default) +
  `run_git_push`.
- `.pre-commit-config.yaml` + `.gitleaks.toml` for this software
  repository's own commit hygiene.

**Two real bugs found and fixed while building and testing the foundation
directly, before any agent touched the code:**
1. `classify_git_failure` was stderr-only; a real test run showed `git
   commit`'s "nothing to commit, working tree clean" failure prints to
   **stdout** with an **empty stderr** — the opposite channel from
   auth/network/conflict messages. Fixed to check stdout specifically (and
   only) for this one marker.
2. `push()` initially passed just `<remote>`; a real test against a fresh
   local bare remote failed with "The current branch main has no upstream
   branch" even though `--set-upstream` was included — `--set-upstream`
   only works combined with an *explicit* branch name. Fixed by reading the
   real current branch via `git rev-parse --abbrev-ref HEAD` (never
   caller-supplied) and always passing it alongside `--set-upstream`.

**Deployed two parallel agents** against the now-fixed `athena.git`
interface, per the user's explicit request to deploy agents to finish this
phase:
- **Agent 1** (auto-commit wiring): added `auto_commit_mutation` calls to
  `note_create`/`note_move` (`write_tools.py`), `note_update`/`note_link`/
  `note_delete`/`note_merge` (`mutation_tools.py`), and `commit_draft`'s
  write path (`research/workflow.py`, plus threading the two new config
  values through `research_tools.py` and `worker.py`'s `run_research_commit`).
  Found a real test-ordering gotcha (a note must already be tracked/committed
  before `note_delete` can stage its removal — `git add` on a
  never-tracked, now-deleted path fails with "pathspec did not match") and
  a real pre-existing-but-newly-relevant fact (`merge_notes` never deletes
  the absorbed note's file from disk, only soft-deletes the DB row — so
  `note_merge`'s auto-commit correctly scopes to `[keep_path]` only).
- **Agent 2** (MCP/CLI/diagnostics surface): built
  `athena.mcp_server.git_tools` (`git_status`/`git_log`/`note_history`/
  `git_commit`), registered it in `server.py`, added `athena doctor`'s
  `vault_git_repo` check, extended `vault_status`/`get_vault_status` with
  `git_ahead_by`/`git_last_commit`, and built `athena git
  {status,log,commit,push}` CLI commands (each with its own direct
  `asyncio.run()` bridge, no Huey/worker involvement, matching `_cmd_migrate`'s
  precedent). Noted that `git_commit`'s MCP default (`dry_run=False`) is the
  deliberate opposite of `research_commit`'s (`dry_run=True`), and wrote an
  explicit test asserting it.

Both agents ran with zero file overlap (verified by design before
dispatch) and their combined output passed the full suite with no
regressions when each checked independently.

## Verification

- 561/561 tests passing (39 new this session across `tests/git/` (42 direct
  tests for the foundation) plus the two agents' additions), mypy --strict
  clean, ruff clean.
- **`.pre-commit-config.yaml`/`.gitleaks.toml` verified for real, not just
  written**: downloaded the pinned gitleaks v8.30.1 binary directly and ran
  it against this repo's real tracked git history (`gitleaks git .` — zero
  false positives across 10 commits); confirmed the `.gitleaks.toml`
  allowlist correctly suppresses a planted test private key
  (`tests/security/test_secrets.py`) that gitleaks' own default ruleset
  does flag on a plain filesystem scan; then installed the real
  `pre-commit` framework and ran the actual hook end-to-end
  (`pre-commit run gitleaks --all-files`), confirming it passes cleanly on
  this repo and genuinely **blocks** a planted realistic-looking (non-
  `EXAMPLE`) AWS secret while correctly ignoring this project's existing
  `EXAMPLE`-suffixed test fixtures.
- **Live end-to-end verification, independently re-run** (not just trusted
  from the two building agents' own summaries) in a fresh scratch vault
  with a real local bare Git remote:
  - `athena migrate` → `athena doctor` showed `vault_git_repo: ok`.
  - A real in-memory MCP `ClientSession` called `note_create` then
    `note_update` — both auto-committed for real; `git_status` reported
    clean immediately after; `note_history`/`git_log` showed both commits
    with the correct structured messages (`note_create: hello.md`,
    `note_update:patch: hello.md`).
  - `athena git commit` (no flag) correctly previewed a real diff without
    writing; `athena git commit --commit --message "..."` produced a real
    commit.
  - `athena git push` (no flag) correctly previewed `[new branch] main ->
    main` without pushing; `athena git push --push` pushed for real;
    `athena git status` afterward correctly reported `ahead 0, behind 0`.
  - `vault_status` (via MCP) correctly surfaced `Git commits ahead of
    upstream: 0` and `Git last commit: <sha> <message>`.

## Quality gates

- `pytest`: 561/561 passing, 0 skipped.
- `mypy --strict` across `src/`: clean.
- `ruff check`: clean (required one addition to `pyproject.toml`'s
  per-file-ignores: `tests/git/**/*.py` and `tests/test_worker.py` needed
  S603/S607 exempted, since both legitimately invoke the real `git` binary
  by a fixed, hand-written argv to set up throwaway test repositories).
- Live CLI/MCP verification as described above, run independently by the
  orchestrating session, not solely inherited from the building agents.

## What remains (see `NEXT_SESSION.md` for full detail)

- Nothing from this session has been committed to git yet — awaiting
  explicit user go-ahead. Phase 7's work is already committed and pushed
  as `9b914dc`.
- `git pull`/sync workflow and merge-conflict *resolution* tooling remain
  explicitly out of scope, permanently for the latter.
- CI-side `gitleaks` scanning (beyond the local pre-commit hook) and a
  structural "only `run_git` is ever used" CI check are reasonable
  follow-ups, not built.
- `merge_notes` (Phase 5) leaving the absorbed note's file physically in
  the vault after a merge was found while wiring this phase's auto-commit
  — flagged, not fixed (out of Phase 8's scope).
- `note_summarize` is now the sole remaining explicitly deferred MCP tool
  from ADR-0007's original contract table, blocked on Phase 9's multi-LLM
  adapter.
- Phase 9 (Multi-LLM) is next per the roadmap, not started this session.
