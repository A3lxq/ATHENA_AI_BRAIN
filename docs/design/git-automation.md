# Design: Git Automation (Phase 8)

## 0. Research performed before this design

Most of the substantive research and decision-making for this phase already
happened at Phase 0 and is **not re-opened here** — per CLAUDE.md rule 10
("do not silently redesign accepted architecture"), this design implements
ADR-0005 and `docs/GIT_WORKFLOW.md`'s already-detailed operational runbooks
rather than re-deriving them. What follows is this design's own,
implementation-time research: verifying the accepted decisions against the
real, currently-installed tooling, and resolving the specific open items
ADR-0005 left for "implementation time."

**Git version, verified directly, not assumed.** `git --version` on this
environment reports **2.53.0**, well past the 2.43.1 floor ADR-0005 flagged
for `--end-of-options` support on `checkout`/`reset`. Directly tested:
`git log --end-of-options`, `git status --end-of-options`, and `git
checkout --end-of-options -b <ref>` all behave as documented — **with one
concrete, worth-recording placement gotcha**: `--end-of-options` must be
inserted immediately before the pathspec/ref arguments, not at the front of
the whole argv, since git treats every token *after* it as positional,
including anything that would otherwise have been parsed as a flag (a
`-b <branch>` placed after `--end-of-options` is read as two literal
pathspecs, not a flag+value). This module's `--end-of-options` insertion
point is therefore always argument-list-position-aware, never a blanket
prefix.

**Scope boundary, resolved from ADR-0005's own deferred question**: "whether
the subprocess wrapper module should be designed now or deferred until the
MCP tool contract design settles the exact Git operations ATHENA AI-BRAIN
needs to expose" — this is now resolved, since ADR-0007's tool contract
table already names the exact four tools (`note_history`, `git_status`,
`git_log`, `git_commit`) and explicitly excludes force-push/hard-reset/
branch-delete from the MCP surface entirely. This design's interfaces (§4)
are driven directly by that already-settled table, exactly as ADR-0005
recommended.

**Dulwich: not adopted, a decision this design makes explicitly.** ADR-0005
left Dulwich (`--pure` mode) as an *optional* read-side convenience for
status/diff/log. This design declines that option: every operation this
phase needs (status, log/show, ahead/behind counts, commit, push) is a
single, simple subprocess call with well-understood output — matching
ADR-0005's own rationale #4 ("no library provides dry-run or failure-mode
taxonomy for free anyway... no convenience cost to choosing the subprocess
approach"). Adding a second Git-access code path (subprocess for writes,
Dulwich for reads) would mean two things to keep behaviorally consistent
and two dependency surfaces to audit, for zero functional gain at this
phase's scope. If a future phase needs genuinely complex read-side parsing
(e.g. diffing binary attachments), Dulwich remains available to reconsider
then, per ADR-0005's own text — not foreclosed, just not exercised now.

**Vault-repo auto-`git init`: explicitly decided against.** The Git
automation this phase builds operates on **the Obsidian vault's own Git
repository** (`docs/GIT_WORKFLOW.md`'s "Repository model": the vault has,
or should have, its own separate repo from ATHENA AI-BRAIN's software
repo). This design does **not** have ATHENA AI-BRAIN silently run `git
init` inside a user's vault the first time a mutating tool fires. Even
though `git init` is not itself destructive, silently creating version-
control state inside a personal, possibly-already-managed-some-other-way
directory is a presumptuous side effect a user did not ask for — consistent
with the spirit of CLAUDE.md rule 22 even though the letter only names
"destructive" operations. Instead: `athena doctor` gains a `vault_git_repo`
check (`warn`, not `fail`, mirroring `qdrant_reachable`'s posture for an
optional-but-recommended piece of infrastructure) reporting whether the
configured vault root is (or is inside) a Git repository, and every
auto-commit/`git_commit` call path degrades to a clear, logged no-op
("vault is not a Git repository — skipping commit; run `git init` in the
vault directory to enable Git automation") rather than failing the
triggering mutation or attempting to initialize one itself.

**`.pre-commit-config.yaml`/gitleaks for ATHENA AI-BRAIN's own software
repository: a genuine, previously-flagged gap, closed in this pass.**
ADR-0005's own "Consequences" section states plainly: "The `pre-commit`
framework with `gitleaks` must be set up as part of Phase 1 to satisfy the
'never commit secrets' requirement." Checked directly: no
`.pre-commit-config.yaml` exists in this repository, and neither
`pre-commit` nor `gitleaks` is installed. This is a real, honestly-tracked
gap (this project does not pretend a "must be done" item was done when it
wasn't) — closed as part of this phase, per `docs/GIT_WORKFLOW.md`'s
already-written "gitleaks pre-commit setup" runbook: a pinned-release
`gitleaks` pre-commit hook plus a narrowly-scoped `.gitleaks.toml`
allowlist for this repo's own known-fine secret-shaped test fixtures (e.g.
`_AWS_EXAMPLE_KEY` in `tests/vault/test_ingest.py`/
`tests/research/test_workflow.py`), so the hook doesn't block a legitimate
commit touching those files. This is entirely independent of the vault's
own automated commits below — it's a development-workflow control for
people committing to *this* repository, not something that runs against
the vault at runtime.

## 1. Purpose & Scope

Implements `docs/ROADMAP.md`'s Phase 8 checklist (safe commit workflow,
push policy, backup workflow, conflict detection, recovery) by building
ADR-0005's subprocess wrapper and wiring it into `docs/GIT_WORKFLOW.md`'s
already-specified "ATHENA AI-BRAIN's own automated Git commit/push policy"
runbook, and resolves ADR-0007's four Git-related MCP tools
(`note_history`, `git_status`, `git_log`, `git_commit`), the last of the
tool families this project's design docs had left as placeholders.

**In scope:**
- `athena.git.wrapper` — the one subprocess primitive every Git operation
  in this codebase goes through: argv-only, never `shell=True`, position-
  aware `--end-of-options` insertion, a hand-written failure taxonomy.
- `athena.git.read` — status, log/show, ahead/behind counts, path history.
- `athena.git.write` — commit and push, both dry-run-capable; the shared
  `auto_commit_mutation()` helper every mutating MCP tool calls as its
  final, best-effort step.
- MCP tools: `note_history`, `git_status`, `git_log` (read-only),
  `git_commit` (mutating, non-destructive, standalone — distinct from the
  auto-invoked helper above).
- Auto-commit wiring into every existing mutating tool named in
  `GIT_WORKFLOW.md`: `note_create`, `note_update` (both modes), `note_link`,
  `note_move`, `note_delete`, `note_merge`, and `research_commit`'s write
  step.
- A periodic, bounded-retry auto-push Huey task, off by default.
- `athena doctor` gains `vault_git_repo`; `vault_status` gains push-pending/
  last-commit reporting.
- `.pre-commit-config.yaml` + `.gitleaks.toml` for this software repository
  (closing ADR-0005's Phase-1-era gap, §0).

**Out of scope, explicitly, per ADR-0007/CLAUDE.md rules 22-23:**
- Force-push, hard reset, branch deletion, history rewriting, or any other
  destructive Git operation — **not implemented anywhere in this module**,
  not merely unexposed via MCP. There is no function in `athena.git.write`
  that can perform one; this is an API-level guarantee, not a convention.
- Merge-conflict *auto-resolution* — a conflict is detected and reported
  (via the failure taxonomy), never resolved automatically.
- Branch creation/switching, tags, or any multi-branch workflow — this
  phase operates entirely on whatever branch is already checked out.
- Automatic `git init` in the vault (§0).

## 2. Responsibilities

### 2.1 `athena.git.wrapper` — the core subprocess primitive

```python
async def run_git(
    args: list[str], *, cwd: Path, timeout_s: float
) -> GitResult: ...
```

The single function every other piece of this module calls. Uses
`asyncio.create_subprocess_exec("git", *args, cwd=cwd, ...)` — argument
list only, matching ADR-0005's decision exactly; never `shell=True`,
never string-interpolated into a shell command. Captures stdout/stderr/
exit code, enforces `timeout_s` (killing the process group on expiry —
a hung `git push` waiting on a credential prompt must not hang the calling
MCP tool call or Huey task forever), and classifies the result via
`classify_git_failure()` into a `GitFailureKind`:

```python
class GitFailureKind(Enum):
    NONE = "none"                    # exit 0
    NOTHING_TO_COMMIT = "nothing_to_commit"
    MERGE_CONFLICT = "merge_conflict"
    NON_FAST_FORWARD = "non_fast_forward"   # push rejected, remote has newer commits
    AUTH_FAILURE = "auth_failure"
    NETWORK_FAILURE = "network_failure"
    NOT_A_REPOSITORY = "not_a_repository"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"
```

Classification is exit-code-first, stderr-substring-matching second (e.g.
`"CONFLICT"`/`"Merge conflict"` for `MERGE_CONFLICT`, `"non-fast-forward"`/
`"fetch first"` for `NON_FAST_FORWARD`, `"Authentication failed"`/
`"Permission denied (publickey)"` for `AUTH_FAILURE`, `"Could not resolve
host"`/`"Connection timed out"` for `NETWORK_FAILURE`) — **best-effort, not
exhaustive**, explicitly documented as such (§5). Stderr text is *never*
`eval`'d, executed, or used to construct another command — it is treated
purely as display/classification data (§6, closing the residual `SECURITY_
MODEL.md` TB-6 note on subprocess-output parsing robustness).

**`--end-of-options` insertion**: a small helper,
`_with_end_of_options(fixed_args: list[str], variable_args: list[str]) ->
list[str]`, that returns `[*fixed_args, "--end-of-options", *variable_args]`
— always called with the *fixed* (trusted, hand-written) portion of a
command separate from the *variable* (pathspec/ref) portion, per §0's
placement finding. Every call site in `read.py`/`write.py` that accepts a
path or ref uses this helper; none hand-assembles argv with a variable
component inlined among flags.

### 2.2 `athena.git.read` — read-only operations

```python
async def get_status(vault_root: VaultRoot) -> GitStatus: ...
async def get_log(vault_root: VaultRoot, *, limit: int = 20) -> list[GitLogEntry]: ...
async def get_path_history(vault_root: VaultRoot, path: str, *, limit: int = 20) -> list[GitLogEntry]: ...
async def get_ahead_behind(vault_root: VaultRoot) -> AheadBehind | None: ...
async def is_git_repository(vault_root: VaultRoot) -> bool: ...
```

`get_status` wraps `git status --porcelain=v1 --end-of-options` (the stable,
machine-parseable porcelain format — never plain `git status`, whose human-
readable format is not a stable parsing contract). `get_log`/
`get_path_history` wrap `git log --pretty=format:...` (a fixed, explicit
field format — sha/author/date/subject — never free-text parsing of the
default log output); `get_path_history` adds `--follow -- <path>` (through
`_with_end_of_options`) to track a note across renames, directly serving
`note_history`. `get_ahead_behind` wraps `git rev-list --left-right --count
@{u}...HEAD`, returning `None` (not raising) when no upstream is configured
— a vault repo with a local-only history and no remote yet is a normal,
expected state, not an error. `is_git_repository` wraps `git rev-parse
--is-inside-work-tree`, used by `athena.diagnostics`'s new check and by
every write-path caller before attempting a commit.

### 2.3 `athena.git.write` — commit, push, and the auto-commit hook

```python
async def commit_paths(
    vault_root: VaultRoot, paths: list[str], message: str, *, dry_run: bool
) -> CommitResult: ...

async def push(
    vault_root: VaultRoot, *, remote: str, dry_run: bool
) -> PushResult: ...

async def auto_commit_mutation(
    vault_root: VaultRoot, *, paths: list[str], operation: str, detail: str
) -> None: ...
```

`commit_paths` stages **exactly the given paths** (`git add --end-of-options
<paths>`, never `git add -A`/`git add .` for the auto-invoked path — see
below for the one exception) then commits (`git commit -m <message>
--end-of-options <paths>`, scoping the commit to those paths even if other
unrelated changes happen to be sitting in the working tree, matching
`GIT_WORKFLOW.md`'s "small, meaningful, one logical change" policy). Returns
`GitFailureKind.NOTHING_TO_COMMIT` (not an exception) when the paths have no
actual diff to commit — a legitimate, common outcome (e.g. a `note_update`
patch-mode append that produced byte-identical content some other way).
`dry_run=True` runs `git diff --end-of-options <paths>` (or `--cached` as
appropriate) plus reports the would-be commit message, writing nothing.

`push` wraps `git push <remote> --end-of-options` on whatever branch is
currently checked out (never a caller-supplied branch name — closing off
that whole parameter-injection class per §0). `dry_run=True` uses `git push
--dry-run`. Never `--force`/`--force-with-lease` — there is no parameter
that enables it.

`auto_commit_mutation` is the shared helper every mutating MCP tool calls
as its **final, best-effort** step (§2.4): checks `is_git_repository()`
first (no-op with a logged message if not, §0), builds a structured message
(`f"{operation}: {detail}"`, e.g. `"note_update: patch-mode append to
'Project Ideas.md'"`, matching `GIT_WORKFLOW.md`'s example format), and
calls `commit_paths`. Failures (including `NOTHING_TO_COMMIT`, which is not
really a failure) are logged, never raised — an auto-commit failure must
never make the triggering `note_update`/`research_commit`/etc. call appear
to fail from the caller's perspective, per `GIT_WORKFLOW.md`'s explicit
requirement. This mirrors the exact best-effort-indexing pattern
`athena.intelligence.merge.merge_notes`/`athena.research.workflow.
commit_draft` already established for their own post-write, non-critical
steps.

### 2.4 Auto-commit integration points

Per `GIT_WORKFLOW.md`'s "Auto-commit: enabled by default, narrowly scoped"
policy, `auto_commit_mutation()` is called, as the last line before
returning, from:

| Tool | `paths` | `operation` |
|---|---|---|
| `note_create` | the new note's path | `"note_create"` |
| `note_update` (patch/overwrite) | the note's path | `"note_update:{mode}"` |
| `note_link` | the note's path | `"note_link"` |
| `note_move` | old + new paths | `"note_move"` |
| `note_delete` | the note's path | `"note_delete"` |
| `note_merge` | keep-note + absorb-note paths | `"note_merge"` |
| `research_commit` (non-dry-run only) | the new note's path | `"research_commit"` |

Gated on `config.git_auto_commit_enabled` (default `True`, per
`GIT_WORKFLOW.md`: "Auto-commit can reasonably default on (local-only,
non-destructive)"). When disabled, `auto_commit_mutation` no-ops
immediately, logged at debug level.

### 2.5 MCP tools (`athena.mcp_server.git_tools`)

- **`git_status() -> str`**: working-tree state (porcelain summary,
  human-formatted) plus ahead/behind counts if an upstream is configured.
  `read_only_hint=True`.
- **`git_log(limit: int = 20) -> str`** / **`note_history(path: str, limit:
  int = 20) -> str`**: formatted commit list; `note_history` returns a
  clear "no history" message (not an error) for a path with no commits yet
  (e.g. a note created but not yet auto-committed because auto-commit is
  disabled). `read_only_hint=True`.
- **`git_commit(message: str | None = None, dry_run: bool = False) -> str`**:
  the standalone tool ADR-0007 names alongside the auto-invoked helper —
  stages and commits **everything currently dirty** in the vault (`git add
  --end-of-options .`, the one deliberate exception to "exactly the given
  paths," since a standalone call has no single triggering mutation to
  scope to), with either a caller-supplied `message` or an auto-generated
  one (`"manual commit via git_commit: {n} file(s) changed"`).
  `destructive_hint=False`, `idempotent_hint=False` — not MRTR-gated (it is
  the least destructive category of mutation this server exposes: it can
  only ever *add* a commit object, never rewrite or discard history), but
  every call is logged with full detail per `SECURITY_MODEL.md`'s
  recommendation to add structured provenance logging around any
  auto-triggered `git_commit` following LLM reasoning over retrieved
  content (§6).

### 2.6 Periodic auto-push (`athena.worker.git_push_task`)

`@huey.periodic_task(crontab(minute=f"*/{config.git_push_interval_minutes}"))`
(default 60), matching the existing `reconcile_vault_task`/`stale_sweep_
task` periodic pattern. No-ops immediately if `config.git_auto_push_
enabled` is `False` (the default, per `GIT_WORKFLOW.md`: "auto-push touches
an external system and defaults off"). When enabled: calls `push()`;
`huey.task(retries=3, retry_delay=30, retry_backoff=2)` gives the "bounded
retry with backoff, then a standing... status" behavior `GIT_WORKFLOW.md`
requires, rather than either an unbounded retry loop or a single silent
attempt. A final failure after retries is logged, not raised further —
`vault_status`'s live `get_ahead_behind()` query (§2.7) is always available
to surface "N commits ahead, not yet pushed" independent of whether the
last push attempt itself succeeded.

### 2.7 `vault_status` / `athena doctor` extensions

`vault_status` gains `git_ahead_by: int | None` and `git_last_commit: str |
None` (short sha + subject), both from live `athena.git.read` queries
(never persisted/cached — cheap to compute, and a cached value would go
stale exactly when it matters most, e.g. right after a manual `git push`
outside ATHENA AI-BRAIN entirely). `athena doctor` gains `vault_git_repo`
(`warn` if the configured vault isn't a Git repository yet, `ok` if it is —
mirroring `qdrant_reachable`'s warn-not-fail posture for optional-but-
recommended infrastructure).

## 3. Schema Change

**None.** `git.commit_completed`'s audit trail (per `EVENT_MODEL.md` §1.2:
`commit_sha`, `files_changed[]`, `message`, `push_status`) is recorded as an
`events` table row via the existing `athena.db.repository.events.
append_event` function (Phase 2/ADR-0010), exactly as every other domain
event in this project already is — no new table. `research_jobs.job_type`
already includes `'git_backup'` (migration 0001), available if a future
phase wants a job-tracked (rather than fire-and-forget periodic) push
model; this phase's periodic task doesn't need it, since push
success/failure is fully observable via the `events` table and live
`git rev-list` queries without a `research_jobs` row per attempt.

## 4. Interfaces

```python
# athena/git/wrapper.py
class GitFailureKind(Enum):
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

async def run_git(args: list[str], *, cwd: Path, timeout_s: float) -> GitResult: ...

# athena/git/read.py
@dataclass(frozen=True)
class GitStatus:
    is_clean: bool
    changed_paths: list[str]        # porcelain-parsed, path only
    ahead_behind: AheadBehind | None

@dataclass(frozen=True)
class AheadBehind:
    ahead: int
    behind: int

@dataclass(frozen=True)
class GitLogEntry:
    sha: str
    author: str
    date: str
    subject: str

async def is_git_repository(vault_root: VaultRoot) -> bool: ...
async def get_status(vault_root: VaultRoot) -> GitStatus: ...
async def get_ahead_behind(vault_root: VaultRoot) -> AheadBehind | None: ...
async def get_log(vault_root: VaultRoot, *, limit: int = 20) -> list[GitLogEntry]: ...
async def get_path_history(
    vault_root: VaultRoot, path: str, *, limit: int = 20
) -> list[GitLogEntry]: ...

# athena/git/write.py
@dataclass(frozen=True)
class CommitResult:
    committed: bool                 # False for NOTHING_TO_COMMIT
    sha: str | None
    failure_kind: GitFailureKind
    dry_run_preview: str | None     # diff/message preview when dry_run=True

@dataclass(frozen=True)
class PushResult:
    pushed: bool
    failure_kind: GitFailureKind
    dry_run_preview: str | None

async def commit_paths(
    vault_root: VaultRoot, paths: list[str], message: str, *, dry_run: bool = False
) -> CommitResult: ...
async def push(vault_root: VaultRoot, *, remote: str = "origin", dry_run: bool = False) -> PushResult: ...
async def auto_commit_mutation(
    vault_root: VaultRoot, *, paths: list[str], operation: str, detail: str
) -> None: ...
```

`AthenaConfig` gains: `git_auto_commit_enabled: bool` (env
`ATHENA_GIT_AUTO_COMMIT`, default `True`), `git_auto_push_enabled: bool`
(env `ATHENA_GIT_AUTO_PUSH`, default `False`), `git_push_interval_minutes:
int` (env `ATHENA_GIT_PUSH_INTERVAL_MINUTES`, default `60`),
`git_command_timeout_s: float` (env `ATHENA_GIT_COMMAND_TIMEOUT_S`, default
`30.0`; `push` uses `max(timeout_s, 60.0)` internally, since a legitimate
push over a slow connection reasonably takes longer than a local `status`/
`commit`).

CLI: `athena git status` / `athena git log [--limit N]` / `athena git
commit [--message MSG] [--dry-run/--commit]` / `athena git push [--dry-run/
--push]` — the last two require an explicit `--commit`/`--push` flag
(mirroring `research commit`'s `--commit` gate), since the CLI, like
`research commit`, has no elicitation mechanism to fall back on.

## 5. Failure Modes

| Scenario | Mechanism | Result |
|---|---|---|
| Vault is not (yet) a Git repository | `is_git_repository()` returns `False` | Auto-commit no-ops (logged); `git_status`/`git_log`/`note_history`/`git_commit` return a clear "not a git repository" message; `athena doctor`'s `vault_git_repo` check reports `warn` |
| No upstream remote configured | `get_ahead_behind()` returns `None`; `push()` classifies as `GitFailureKind.UNKNOWN` with the actual git error surfaced (git's own "no upstream configured" message) | `vault_status` omits ahead/behind rather than showing a false zero; `git push` (standalone/periodic) fails cleanly with the real reason, not a crash |
| Nothing to commit (paths have no diff) | `commit_paths` returns `GitFailureKind.NOTHING_TO_COMMIT`, not raised | Auto-commit logs at debug level and returns; standalone `git_commit` reports "nothing to commit" plainly |
| Merge conflict on push (remote has diverged) | `git push` fails non-fast-forward; classified `NON_FAST_FORWARD` | Push task logs and stops (no auto-merge/auto-rebase attempted — out of scope §1); `vault_status`'s ahead/behind still reports the real, growing divergence for a human to notice and resolve manually |
| Auth failure (no configured credentials/SSH key) | stderr-pattern-matched to `AUTH_FAILURE` | Logged clearly with the real git stderr; periodic push task's bounded retry does not help here (retrying won't fix missing credentials) but does not loop forever either |
| Network failure (remote unreachable) | stderr-pattern-matched to `NETWORK_FAILURE` | Bounded retry (`retries=3, retry_backoff=2`) gives a real transient-failure a chance to clear; a persistent outage still stops after 3 attempts, not forever |
| A `git` subprocess call exceeds `timeout_s` | `run_git`'s own timeout kill | Classified `TIMEOUT`; never hangs the calling MCP tool/Huey task indefinitely (a real risk for `push`, which can otherwise block on an interactive credential prompt) |
| `auto_commit_mutation` itself raises unexpectedly | Caught at the call site (mirroring `merge_notes`'s `except Exception: logger.warning(..., exc_info=True)` pattern) | The triggering mutation (`note_update`, etc.) still completes and returns success to its caller — a git-layer problem never masks a successful vault write |

## 6. Security Considerations

**What this closes.** Implements ADR-0005 in full (argv-only subprocess
execution, `--`/`--end-of-options` insertion at the correct position,
hand-written failure taxonomy, no destructive operations implemented at
all) and closes the standing Phase-1-era gap around this software repo's
own `pre-commit`/`gitleaks` setup (§0). Also acts on
`docs/SECURITY_MODEL.md`'s action item 2 for `git_commit` specifically:
every auto-invoked commit carries a structured, machine-generated message
naming the exact triggering operation and affected path(s) (§2.4's table),
giving a human reviewing `git log` (or a future audit tool reading the
`events` table's `git.commit_completed` rows) a queryable answer to "what
MCP call produced this commit" — the "structured provenance field" the
security model asked for, implemented as the commit message plus the
`events` row rather than a new schema.

**Residual risk, stated honestly, matching `SECURITY_MODEL.md`'s own
practice of naming what is *not* solved:**
- **This does not close the underlying TB-2 prompt-injection gap.** A model
  acting on injected content in a retrieved note can still cause
  `note_create`/`note_update`/`research_commit` to run with no
  confirmation (that gap belongs to those tools' own designs, not this
  one), and this phase's auto-commit *faithfully preserves* whatever those
  tools did into Git history with a clear message — it does not add a new
  confirmation gate of its own, since `git_commit`'s own risk profile
  (append-only, non-destructive) doesn't warrant one per ADR-0007's
  destructive/non-destructive split. What this phase *does* add is
  traceability: an injected write now leaves an unambiguous, timestamped,
  operation-labeled Git history entry, rather than the vault's Git history
  offering no record at all — a stated, deliberate scope boundary, not an
  oversight.
- **TB-5 (pulled/merged remote content), addressed here as `SECURITY_MODEL.
  md` item 21 asked**: this phase builds no `git pull`/`git fetch`
  functionality at all (out of scope §1 — this phase is commit/push only,
  matching `GIT_WORKFLOW.md`'s "backup workflow," not a sync workflow). If
  a future phase adds pulling, content arriving that way should be treated
  exactly as untrusted as a locally-created note (ADR-0009's reconciliation
  job already would re-index it with no special exemption) — stated here
  explicitly per the security model's own request, even though this phase
  doesn't build the pull path itself.
- **Stderr-text parsing (TB-6's residual note) is display/classification
  only** — `classify_git_failure()` never `eval`s, execs, or re-embeds
  stderr content into another command; a maliciously-crafted commit message
  or file path that ends up echoed in stderr can at most cause a
  *misclassification* (falling through to `UNKNOWN`), never code execution.
- **The periodic auto-push task, if enabled, is itself a scheduled,
  unattended network operation** — `SECURITY_MODEL.md` TB-1's general
  concern about unbounded background job dispatch doesn't apply here
  (it's a single periodic task, not caller-triggered), but a compromised or
  misconfigured remote URL would still receive whatever the vault contains.
  This is inherent to enabling push automation at all and is exactly why
  `GIT_WORKFLOW.md` specifies auto-push must default off — this design
  keeps that default, does not relax it.

## 7. Test Strategy

- **`athena.git.wrapper`**: `run_git` against a real, throwaway temp-dir
  Git repository (not mocked — subprocess behavior is exactly the kind of
  thing worth testing for real, matching this project's practice for
  Qdrant/detect-secrets) for: a successful command, a nonzero-exit command,
  a timeout (a deliberately slow git operation or a stub script), and each
  `GitFailureKind` classification case constructed via a real failing git
  invocation (a real merge conflict, a real rejected non-fast-forward push
  between two local throwaway repos, a real auth failure against a bogus
  `file://` or unreachable URL) wherever practically reproducible without
  live network dependency; `--end-of-options` placement verified against a
  path/branch name starting with a dash.
- **`athena.git.read`**: real temp repos — clean vs. dirty status, ahead/
  behind with and without a configured upstream, log/path-history including
  a renamed file (`--follow` correctness), `is_git_repository` against both
  a real repo and a plain (non-git) directory.
- **`athena.git.write`**: real temp repos — a real commit lands the exact
  expected content and message; `NOTHING_TO_COMMIT` for a no-diff path;
  dry-run performs no write and returns an accurate preview; `push` against
  a real local bare-repo remote (fully offline, no live network needed) for
  both success and non-fast-forward rejection; `auto_commit_mutation`
  never raises even when the underlying commit fails.
- **Auto-commit integration**: for each of the seven call sites (§2.4), a
  test confirming the mutation's own success/return value is unaffected by
  a forced git-layer failure (monkeypatched), and confirming a real commit
  actually lands when git automation is healthy — extending each tool's
  existing test file rather than a new, disconnected test suite.
- **MCP tools**: `git_status`/`git_log`/`note_history`/`git_commit` each
  against a real temp vault+repo; `note_history` for a path with zero
  commits returns a clear message, not an error; `git_commit`'s dry-run
  default-and-behavior matches `TESTING_STRATEGY.md`'s existing convention
  for every other `dry_run`-capable tool.
- **Periodic push task**: `call_local`-style direct invocation (matching
  `test_worker.py`'s existing pattern) confirming the config toggle's
  no-op-when-disabled behavior, and a real push attempt against a local
  bare-repo remote when enabled.

## 8. Open Items Carried Forward

- Merge-conflict *resolution* tooling (surfacing a conflict clearly is in
  scope; resolving one is not, and isn't planned for any phase per
  `GIT_WORKFLOW.md`'s explicit non-goal).
- A `git pull`/sync workflow, and the untrusted-content handling it would
  need (§6) — not built, explicitly named as a future consideration.
- Branch/tag management, multi-branch workflows — out of scope, no phase
  currently plans this for the *vault* repository (the *software*
  repository's own branching policy is already documented in
  `GIT_WORKFLOW.md` and doesn't need new tooling).
- CI-side `gitleaks` scanning (full history/PR-diff, defense-in-depth
  beyond the local pre-commit hook per `GIT_WORKFLOW.md`'s own
  recommendation) — not set up in this pass; flagged, not silently assumed
  done because the local hook exists.
- A structural CI check enforcing "only `run_git`'s argv-list path is ever
  used, never a shell-interpolated git invocation" — a reasonable follow-up
  in the same spirit as the already-flagged `sanitize_fts5_query` CI-check
  idea from Phase 4, not built here.

## Sources Cited

- `docs/adr/0005-git-automation-library.md` (the accepted decision this
  design implements)
- `docs/GIT_WORKFLOW.md` (the accepted operational runbooks this design
  operationalizes into code)
- `docs/adr/0007-mcp-tool-contract.md` (the four Git-related tool
  definitions this design's interfaces are driven by)
- `docs/EVENT_MODEL.md` §1.2/§6 (the `git.repository_changes_detected`/
  `git.commit_completed` event definitions and the MCP-tool-to-event
  mapping table)
- `docs/SECURITY_MODEL.md` TB-5, TB-6, action item 2, and item 21 (the
  standing recommendations this design directly addresses or explicitly
  scopes out with stated reasoning)
- Direct verification against the installed `git 2.53.0` on this
  environment (§0)
