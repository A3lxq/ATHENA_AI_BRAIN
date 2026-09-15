# Design: Production Hardening (Phase 10)

## 0. Audit performed before this design

Unlike every prior phase, Phase 10's "research" is not primarily external
library research — it is a direct audit of this project's own accumulated,
already-decided-but-not-yet-closed items: `docs/SECURITY_MODEL.md`'s
UNMITIGATED findings (all dated 2026-08-27, Phase 0), and the "Not Yet
Completed"/open-items lists `CURRENT_STATE.md` and `CHANGELOG.md` have
carried forward, sometimes unchanged, across Phases 1-9. This section
records what was actually checked against the real, as-built code —
distinguishing genuinely-open gaps from stale claims — before deciding
what this phase builds.

**A real, previously-uncorrected stale finding, checked directly against
the code**: `SECURITY_MODEL.md`'s TB-13 (§ "Huey's sync-core/async-bridge
(`aget_result()`) is an unaddressed DoS chokepoint") assumes the
as-built system blocks on Huey's `aget_result()` somewhere in the MCP
request path. Checked with `grep -rn "aget_result" src/` — **`aget_result()`
is never called anywhere in this codebase.** Every task-backed MCP tool
(`duplicates_scan`, `reindex_start`, `research_start`) dispatches via a
direct, fire-and-forget Huey task call (e.g. `athena.worker.research_task(
...)`, which enqueues and immediately returns a `Result` handle with just
an `.id` — never awaited or blocked on) and returns a job handle
synchronously; the caller polls `job_status` separately. This is a
different, and safer, integration pattern than the one ADR-0002 originally
described, arrived at organically across Phases 6-9 without ever being
named as a deliberate architectural decision. **TB-13's specific threat
scenario does not apply to the system as actually built** — this phase
corrects `SECURITY_MODEL.md` to say so explicitly, the same "verify before
recording as resolved" discipline this project applied to the Phase 5/6
"blocked pending Docker access" correction, rather than leaving a stale
UNMITIGATED flag standing indefinitely.

**A real, still-genuinely-open gap, confirmed by checking actual event
emission, not assumed from the design doc's own aspirational text**:
`EVENT_MODEL.md`'s MCP-tool-to-event mapping table specifies that
`note_create`/`note_update`/`note_move`/`note_delete`/`note_merge` each
emit their own semantic event (`vault.note_created`, etc.) before chaining
into indexing/git-commit. Checked with `grep -rln "events_repo" src/athena/
mcp_server/*.py` — only `git_tools.py` (Phase 8) and `research_tools.py`/
`job_tools.py` (dispatch-tracking, not semantic events) actually call
`events_repo.append_event`. **`write_tools.py`/`mutation_tools.py` never
emit their own `vault.*` events** — the only audit trail for these six
tools today is the auto-commit step's `git.commit_completed` event
(Phase 8), which fires *after* the mutation and only when the vault is a
Git repository with auto-commit enabled. `SECURITY_MODEL.md`'s Repudiation
finding ("job-queue-dispatched actions... leave no equivalent record") is
therefore still accurate for this specific gap and this phase closes it.

**A confirmed, still-open, previously-repeatedly-flagged gap**: no
rate-limiting or per-day dispatch ceiling exists on `research_start`/
`reindex_start` — confirmed via `grep -n "max_calls_per_day\|rate.limit"
src/athena/mcp_server/research_tools.py src/athena/mcp_server/job_tools.py`
returning nothing. `SECURITY_MODEL.md`'s TB-1 DoS finding named this
specifically ("a malicious/buggy client can enqueue unbounded
`research_start`/`reindex_start` jobs... OWASP LLM06:2026 'Unbounded
Consumption'"), and Phase 9 already built exactly this mitigation pattern
for `note_summarize` (a daily call ceiling backed by the `events` table,
checked pre-call). This phase generalizes that already-proven pattern to
the two job-dispatch tools it was originally scoped for but never reached.

**A confirmed structural, not-closeable-by-more-code gap**: `fastembed`
(miniCOIL sparse embeddings, ADR-0008) still has no revision-pinning
mechanism — `src/athena/indexing/embedding.py`'s own code comment already
documents this was checked directly against the installed library's
`load()` signature, which accepts no `revision` parameter at all. This is
an external library limitation, not something this phase can fix by
writing more ATHENA AI-BRAIN code — carried forward as an accepted,
documented risk (§8), not a deliverable.

**Deployment readiness, re-checked directly**: `deployment/README.md`'s
own "Open items" section has said the same thing since Phase 1 — a
placeholder venv/install path (`%h/athena/.venv`) blocking the
systemd/bubblewrap configs from ever being enabled for real. Still true
today; this phase resolves it with a concrete decision (§2.6).

**No CI pipeline exists at all** — confirmed via `find .github -type f`
returning nothing. Every quality gate this project has relied on (pytest,
`mypy --strict`, `ruff check`) has been run manually, by hand, every
session. This is a real, if unglamorous, production-hardening gap:
nothing currently prevents a regression from reaching `main`.

## 1. Purpose & Scope

Implements `docs/ROADMAP.md`'s Phase 10 checklist (security testing,
performance benchmarks, failure recovery, observability, documentation,
release process) by closing the genuinely-open items identified in §0,
not by inventing new scope. Per master spec §14 (Local-First Principle)
and this project's demonstrated preference for small, purpose-built code
over heavyweight frameworks, Phase 10 stays proportionate to a
single-user, local-first tool — it does **not** attempt to become an
enterprise observability/APM/rate-limiting platform.

**In scope:**
- **Security**: correct `SECURITY_MODEL.md`'s stale TB-13 entry (§0); emit
  real `vault.*` semantic events from every mutating MCP tool, closing the
  Repudiation gap (§2.1); a daily dispatch ceiling for
  `research_start`/`reindex_start`, generalizing Phase 9's pattern (§2.2);
  a real, executed adversarial test pass exercising the path-traversal,
  FTS5-injection, and SSRF defenses already built, as regression tests
  rather than only design-time claims (§2.3).
- **Performance**: a benchmarking CLI command measuring indexing
  throughput and MCP-mutation round-trip latency, reusing the retrieval
  evaluation harness's existing p50/p95 latency tracking for the retrieval
  side rather than rebuilding it (§2.4).
- **Failure recovery**: real (not just documented) tests for worker-crash
  mid-job recovery via reconciliation, and `SQLITE_BUSY`/`busy_timeout`
  behavior under real concurrent access (§2.5).
- **Observability**: the `vault.*` event emission above is this phase's
  primary observability deliverable (an audit trail queryable via the
  `events` table); a structured-logging audit confirming no secret/API-key/
  prompt content reaches a persisted log line anywhere in the codebase,
  not just in `athena.llm` (§2.6).
- **Documentation**: a real, user-facing `README.md` rewrite (the current
  one is Phase 0's dev-pack bootstrap instructions, not usable
  documentation for someone installing this project) and a consolidated
  Operations Guide covering install/deploy/backup/recovery (§2.7).
- **Release process**: a GitHub Actions CI workflow running the exact
  three gates every session has run by hand (§2.8); resolving the
  deployment venv-path placeholder with a concrete decision (§2.6, shared
  with documentation); a release checklist (§2.8).

**Out of scope, explicitly, with reasoning:**
- **A metrics/APM stack** (Prometheus, OpenTelemetry, Grafana) — this is a
  single-user, local-first tool (master spec §14); the `events` table plus
  `vault_status`/`system_diagnostics` already provide proportionate
  observability. A future multi-user or hosted deployment mode would
  justify revisiting this; nothing in the current architecture does.
- **General-purpose rate-limiting infrastructure** (token buckets, sliding
  windows, distributed limiters) — `SECURITY_MODEL.md` item 6 itself
  already specifies the correct scope is "a simple cost-ceiling config
  value," which is exactly what Phase 9 built and what §2.2 generalizes;
  building more than that would be the premature-optimization CLAUDE.md
  rule 20 warns against.
- **Load/stress testing at scale** (concurrent multi-client benchmarking,
  thousand-note synthetic corpora) — this project's stated design center
  is one user's vault; the performance benchmarks in §2.4 measure real,
  proportionate numbers (this project's actual eval corpus, actual note
  counts) rather than simulating enterprise scale that doesn't match the
  Local-First Principle.
- **Qdrant's own on-disk file permissions** — Qdrant runs inside Docker
  (ADR-0006); its data volume's host-side permissions are a Docker/host
  concern outside `athena.hardening`'s reach. The embedding-inversion
  finding (`SECURITY_MODEL.md`) is addressed here by *documenting* the
  required posture (backup/permission parity with the vault itself) in
  the Operations Guide, not by writing code that can't actually control a
  container-managed volume.
- **A v1.0 git tag** — that is Phase 11's own, explicit deliverable per
  `docs/ROADMAP.md` ("A stable, documented, tested AI Knowledge Operating
  System"); Phase 10 makes the codebase ready for it but does not itself
  cut the release.

## 2. Responsibilities

### 2.1 Semantic `vault.*` event emission (closes the Repudiation gap)

Add `events_repo.append_event` calls, matching `EVENT_MODEL.md`'s
already-specified envelope and event-type names exactly, to:

| Tool | Event | Payload |
|---|---|---|
| `note_create` | `vault.note_created` | `path`, `origin="human"` |
| `note_update` (both modes) | `vault.note_modified` | `path`, `mode` |
| `note_link` | `vault.note_modified` | `path` (a link append is still a content modification) |
| `note_move` | `vault.note_moved` | `old_path`, `new_path` |
| `note_delete` | `vault.note_deleted` | `path` |
| `note_merge` | `dedup.merge_completed` | `surviving_note_id`, `superseded_note_ids`, matching `EVENT_MODEL.md` §1's exact field names |

Each event's `correlation_id` is freshly minted per call (matching
`auto_commit_mutation`'s own pattern from Phase 8), and its `causation_id`
is `None` at the MCP-call boundary (no upstream event exists yet — these
tools are where a new correlation chain begins, exactly like
`ingest_note`'s own filesystem-triggered chain begins at `fs.path_changed`).
Recorded via a small shared helper (`athena.mcp_server._events.
record_vault_event`, mirroring `athena.git.write.auto_commit_mutation`'s
own "one shared helper every call site uses" shape) so the six call sites
don't each hand-rebuild the same four-line pattern. Never raises — an
event-recording failure must not make the triggering mutation appear to
fail, the same guarantee `auto_commit_mutation` already provides.

### 2.2 Daily dispatch ceiling for `research_start`/`reindex_start`

Reuses Phase 9's exact pattern (`athena.llm.summarize`'s pre-call `events`-
table count check) rather than inventing a new mechanism: a new, small
`athena.mcp_server._rate_limit.check_daily_dispatch_limit(conn, *,
job_type, max_per_day) -> None` helper (raising `DispatchLimitError` if
already at the ceiling), called by `research_start`/`reindex_start` before
enqueueing, counting today's `research_jobs` rows by `job_type` and
`created_at` date (no new `events` row needed here — `research_jobs`
already records exactly what's needed, unlike LLM calls which had no
existing table to reuse). New `AthenaConfig` fields:
`research_max_dispatches_per_day`/`reindex_max_dispatches_per_day`
(sane, generous defaults — e.g. 50/20 — since this ceiling exists to catch
runaway/malicious loops, not to constrain normal single-user use).

### 2.3 Adversarial regression test pass

Not new production code — a new, consolidated test module
(`tests/security/test_adversarial.py`) that actually *executes* the attack
patterns this project's design docs and threat model have discussed but
not necessarily exercised as a named, single regression suite:
- Path traversal via `../../etc/passwd`-style and symlink-escape attempts
  against every MCP tool taking a `path` parameter (not just
  `athena.safety.paths`' own unit tests — an integration-level pass
  through the actual tool functions).
- FTS5 query-syntax injection strings (`"unbalanced quote`, leading `NOT`,
  `NEAR()` abuse) through `vault_search`.
- SSRF attempts (already covered by `tests/research/test_fetch.py`, this
  suite adds only a cross-reference/summary rather than duplicating them)
  through `research_start`'s real URL parameter.
- Secret-shaped content through `note_create`/`research_commit`, asserting
  redaction actually occurs end-to-end via the MCP layer (not just at the
  lower `athena.security.secrets`/`athena.vault.ingest` layer already
  tested).
- A planted `EXAMPLE`-suffixed and a planted realistic-looking secret
  through `git_commit`, confirming `.gitleaks.toml`'s allowlist still
  behaves correctly against the current ruleset (a regression guard for
  the manual verification Phase 8 already did once, now automated).

### 2.4 Performance benchmarking

`athena bench` CLI command (new subcommand family): `athena bench index
[--corpus PATH]` runs a full ingest+index pass over a benchmark corpus
(defaults to the existing retrieval-eval corpus, `tests/retrieval/
fixtures/eval_corpus/`) and reports notes/sec and total duration; `athena
bench retrieval` is a thin wrapper printing `retrieval evaluate`'s
existing p50/p95 latency figures with clearer benchmark-oriented framing
(no new measurement logic — reuses `athena.retrieval.evaluation` exactly);
`athena bench mutation` measures a real `note_create` → auto-commit
round-trip's wall-clock time against a real throwaway git-repo vault, N
iterations, reporting mean/p95. Results printed to stdout, not persisted
anywhere new (no new schema) — a human (or CI, later) compares numbers
across runs by eye or by redirecting output, matching `retrieval
evaluate`'s own already-accepted "report, don't gate" philosophy (Phase 4).

### 2.5 Failure-recovery tests

- **Worker-crash recovery**: a real test that starts an ingest/index job,
  kills the simulated worker process mid-operation (or, more practically
  given this project's fire-and-forget task dispatch, simulates a crash by
  truncating/interrupting the job before its final DB commit), then runs
  `athena ingest reconcile` and asserts the note reaches a consistent
  state — exercising the reconciliation job as the "independent second
  safety net" `TESTING_STRATEGY.md` already describes, for real, not just
  as a documented intention.
- **`SQLITE_BUSY` behavior**: a real test opening two connections against
  the same metadata DB, holding a write lock on one while attempting a
  write on the other, asserting the `busy_timeout` PRAGMA (already set,
  per `docs/db/migrate.py`'s mandatory-pragmas check) bounds the wait
  rather than hanging indefinitely.

### 2.6 Deployment path resolution + Operations Guide

Resolves `deployment/README.md`'s standing placeholder with a concrete,
documented decision: install via a fixed, documented path
(`~/.local/share/athena/.venv`, matching the XDG Base Directory convention
this project's `DEFAULT_DATA_DIR` already follows for state, per
`athena.config`'s existing `~/.local/state/athena` choice) rather than the
placeholder `~/athena/.venv`. Updates both `athena-huey-worker.service`'s
`ExecStart=` and `athena-mcp-launch.sh`'s `VENV` variable to this real
path, and documents the install steps (`git clone` + `uv`/`pip install -e
.` at that path) in a new `docs/OPERATIONS_GUIDE.md` covering: install,
`athena migrate`/`doctor` first-run, enabling the systemd worker unit
(cross-referencing `deployment/README.md`'s already-written steps, not
duplicating them), the Qdrant Docker setup (per ADR-0006, already run
manually every session so far — documented here as a repeatable
procedure), backup guidance (the vault's own git history plus the
embedding-inversion finding's recommendation to back up Qdrant's data
volume under the same protection as the vault), and a troubleshooting
section built from this project's own accumulated real findings (the
`--end-of-options` placement gotcha, the `git push --set-upstream`
gotcha, the Ollama-always-configured gotcha — each already documented in
its own design doc, cross-referenced here rather than re-explained).

### 2.7 README rewrite

Replaces the current dev-pack-bootstrap `README.md` with real, user-facing
documentation: what ATHENA AI-BRAIN is (one paragraph, reusing the
already-settled framing), a quickstart (install → configure `ATHENA_*`
env vars → `athena migrate` → `athena doctor` → wire into an MCP client),
a CLI command reference table (every `athena <family> <command>` this
project now has: `doctor`, `version`, `migrate`, `ingest`, `index`,
`retrieval`, `duplicates`, `lifecycle`, `research`, `git`, `llm`, `bench`),
a link to `docs/OPERATIONS_GUIDE.md` for deployment detail, and a link to
`docs/00_MASTER_PROJECT_SPECIFICATION.md`/`docs/ARCHITECTURE.md` for
anyone wanting the full design rationale. The existing dev-pack-bootstrap
content (CLAUDE.md-first, ADR discipline, etc.) moves to a clearly-labeled
"Contributing / Development" section rather than being deleted — it's
still true and useful for anyone extending this project, just not what a
first-time reader installing the tool needs at the top of the file.

### 2.8 CI + release checklist

`.github/workflows/ci.yml`: on every push/PR to `main`, run (in order)
`ruff check`, `mypy --strict`, `pytest` — the exact three gates this
project has run by hand every session, now enforced automatically. Uses a
matrix of exactly one Python version (3.12, this project's documented
minimum, per `pyproject.toml`'s `requires-python`) — no multi-version
matrix, since this project targets one specific, already-chosen runtime,
not a published library needing broad compatibility. Does **not** attempt
to stand up a real Qdrant service in CI for the still-`skip`-eligible-in-
theory-but-currently-always-run real-server tests (Phase 8's Docker/Qdrant
resolution session made every test in this suite runnable without a
`skip` marker by using embedded `:memory:` clients almost everywhere, with
a small number of real-server tests — CI standing up a real Qdrant
container via a GitHub Actions service container is a reasonable, bounded
addition, using the exact same pinned `qdrant/qdrant:v1.19.1` image this
project's own `docker run` commands already use every session). A short
`docs/RELEASE_CHECKLIST.md`: run the three gates locally (redundant with
CI, but a human sanity check before tagging), confirm `CHANGELOG.md`/
`CURRENT_STATE.md` reflect the release, bump `pyproject.toml`'s
`version`, tag, push the tag — written for Phase 11's actual use, not
exercised by Phase 10 itself.

## 3. Schema Change

**None new.** §2.1 reuses the existing `events` table (ADR-0010) for real
semantic events it should have been recording since Phase 6; §2.2 reuses
the existing `research_jobs` table (already tracking `created_at`/
`job_type`) for the dispatch-ceiling count query, needing no new column.
This is the fourth phase in a row to extend observability without a new
table — continued confirmation that ADR-0010's original minimal-schema
design keeps scaling.

## 4. Interfaces

```python
# athena/mcp_server/_events.py (new, small, shared helper)
async def record_vault_event(
    conn: aiosqlite.Connection, *, event_type: str, payload: dict[str, object]
) -> None: ...

# athena/mcp_server/_rate_limit.py (new)
class DispatchLimitError(Exception): ...

async def check_daily_dispatch_limit(
    conn: aiosqlite.Connection, *, job_type: str, max_per_day: int
) -> None: ...  # raises DispatchLimitError if already at the ceiling
```

`AthenaConfig` gains: `research_max_dispatches_per_day: int` (env
`ATHENA_RESEARCH_MAX_DISPATCHES_PER_DAY`, default `50`),
`reindex_max_dispatches_per_day: int` (env
`ATHENA_REINDEX_MAX_DISPATCHES_PER_DAY`, default `20`).

CLI: `athena bench {index,retrieval,mutation}`.

## 5. Failure Modes

| Scenario | Mechanism | Result |
|---|---|---|
| `record_vault_event` itself raises (a DB error mid-write) | Caught inside the helper, matching `auto_commit_mutation`'s pattern | Logged, never propagated — the triggering mutation still returns success |
| `research_start`/`reindex_start` dispatch ceiling reached | `check_daily_dispatch_limit` raises `DispatchLimitError` before enqueueing | Clear "daily dispatch limit reached" message; no Huey task is ever enqueued, so this cannot itself contribute to the DoS surface it guards against |
| `athena bench` run against an unmigrated/unindexed vault | Existing `athena migrate`/`index bootstrap` preconditions, unchanged | Clear `[FAIL]` message pointing at the missing step, matching every other CLI command's existing error style |
| CI's Qdrant service container fails to start | GitHub Actions' own service-container health-check retry, standard behavior | The workflow run fails clearly at the service-startup step, not with a confusing mid-test connection error |

## 6. Security Considerations

**What this closes.** The Repudiation gap (§2.1) — every vault mutation
now has an independent audit record, not just the auto-commit's incidental
one. TB-1's DoS-via-unbounded-dispatch finding, for the two tools it was
always meant to cover (§2.2). TB-13's stale claim, corrected to match the
system as actually built (§0) — a documentation-accuracy fix, not a code
change, but a real one: an inaccurate UNMITIGATED flag left standing
indefinitely is itself a small trust-in-documentation cost this project
has consistently avoided elsewhere. A regression suite that actually
*executes* several previously only-design-time-verified defenses (§2.3).

**What this does not close, stated honestly:**
- **The core TB-2 prompt-injection confirmation gap remains open** — this
  phase does not revisit ADR-0007's destructive/non-destructive
  classification or add new confirmation gates; it adds audit trail and
  dispatch ceilings, which are detection/containment controls, not
  prevention.
- **Qdrant's own data-at-rest posture remains outside this project's
  direct control** (§1) — documented, not code-mitigated.
- **`fastembed` revision-pinning remains impossible** with the currently-
  installed library version (§0) — an accepted, external-library-imposed
  risk, re-flagged rather than silently dropped.
- **CI does not run on every possible Python version/OS** — a deliberate,
  proportionate scope cut (§2.8), not an oversight; revisit if this
  project is ever published as a broadly-installed package rather than a
  personal tool.

## 7. Test Strategy

- `tests/mcp_server/test_write_tools.py`/`test_mutation_tools.py`: extend
  with assertions that each tool's new `vault.*`/`dedup.*` event lands in
  the `events` table with the correct type/payload, mirroring how Phase 8
  added auto-commit assertions to these same files.
- `tests/mcp_server/test_research_tools.py`/`test_job_tools.py`: new tests
  for the dispatch ceiling (at-ceiling refusal before any enqueue,
  confirmed by asserting no new Huey task/`research_jobs` row appears).
- `tests/security/test_adversarial.py` (new): the consolidated attack-
  pattern regression suite (§2.3), run against real fixture vaults/DBs,
  no mocking of the defenses under test.
- `tests/test_cli.py`: `athena bench` subcommands smoke-tested (argument
  wiring, clear failure on missing preconditions) — not asserting specific
  performance numbers, which would make the test suite flaky/environment-
  dependent; only that the commands run and report something.
- `tests/vault/test_reconcile.py` (extended) and a new `tests/db/
  test_concurrency.py`: the two failure-recovery tests from §2.5, against
  real temp SQLite files, no mocking.
- CI itself is its own test of the CI config — a real PR/push exercising
  the new workflow is the actual verification, alongside a local
  `act`-style dry run if practical during implementation.

## 8. Open Items Carried Forward

- **`fastembed` revision-pinning** — impossible with the current library
  version; revisit if/when fastembed adds support.
- **The retrieval-evaluation corpus** still ships below
  `TESTING_STRATEGY.md`'s 30-60 note target — unblocked since the Docker/
  Qdrant resolution, still not expanded; a reasonable Phase 11 or
  post-v1.0 follow-up, not blocking this phase.
- **The duplicate-detection default thresholds** remain untuned against
  real vault data — same status.
- **`note_create`/`note_update` content secret-scanning** — still the
  original Phase 6 gap; this phase adds audit *events* for these tools
  but does not add secret-scanning to them (a distinct, already-separately-
  tracked item, not silently folded in here).
- **A full wire-level elicitation round-trip integration test** — still
  not built for any MRTR-gated tool.
- **Model routing / a real per-provider dollar-cost ceiling** for
  `athena.llm` — still explicitly out of scope (Phase 9 §1/§8, unchanged).
- **`merge_notes` leaving the absorbed note's file physically in the
  vault** after a merge — still flagged, not fixed (a Phase 5 behavior
  found during Phase 8).
- **A multi-OS/multi-Python CI matrix** — deliberately out of scope
  (§2.8/§6).

## Sources Cited

- `docs/SECURITY_MODEL.md` (every UNMITIGATED item audited in §0)
- `docs/EVENT_MODEL.md` (the already-specified `vault.*` event mapping
  this phase actually implements)
- `docs/adr/0002-job-queue-architecture.md` (the `aget_result()` context
  corrected in §0)
- `docs/TESTING_STRATEGY.md` (the Recovery-section test descriptions §2.5
  makes real)
- `deployment/README.md` (the standing venv-path placeholder resolved in
  §2.6)
- Direct inspection of this repository's own source (`grep` audits listed
  in §0) — the primary "research" this phase's design is grounded in
