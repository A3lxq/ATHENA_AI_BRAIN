# Session 028 — Phase 10: Production Hardening

**Date:** 2026-09-15
**Phase:** 10 (Production Hardening)
**Status:** Complete — no git commit made yet (Phase 9's work is already committed and pushed as `18d2359`)

## Objective

Following "deploy multiple agents to finish the rest of the phases," close
the genuinely-open items surfaced by a direct audit of this project's own
codebase against its own already-written design docs (`SECURITY_MODEL.md`,
`EVENT_MODEL.md`, `TESTING_STRATEGY.md`) — not fresh external research.
This is the ROADMAP's Phase 10 (security testing, performance benchmarks,
failure recovery, observability, documentation, release process).

## Housekeeping before Phase 10 began

- Confirmed Phase 8's verification (561/561 tests) was already done and
  fixed a one-character typo in the local `origin` remote URL
  (`ATHENA_AI_GRAIN` → `ATHENA_AI_BRAIN`) that had been silently blocking
  pushes. Committed and pushed Phase 8 (`64bc1c8`).
- Committed and pushed Phase 9 (`18d2359`).
- Mid-session, the user gave a standing instruction: the project's license
  should restrict use to personal/non-commercial purposes, and any
  modifications by other users must either be contributed back to the
  parent repository or not be published/distributed at all. Created a new
  `LICENSE` file (none existed at the repo root before this session)
  implementing this — custom, plain-language, explicitly self-labeled as
  non-standard/not lawyer-reviewed.

## Research (audit, not external research)

Rather than researching new libraries or providers, this phase's "research"
was a direct codebase audit against already-written project documents:

- `grep -rn "aget_result" src/` — confirmed TB-13's premise in
  `SECURITY_MODEL.md` (a blocking bridge call from asyncio into Huey) is
  stale: `aget_result()` is never called anywhere. Every task-backed MCP
  tool dispatches fire-and-forget and the caller polls status separately.
- `grep -rln "events_repo" src/athena/mcp_server/*.py` — confirmed
  `write_tools.py`/`mutation_tools.py` never call `events_repo.
  append_event`, only `auto_commit_mutation`'s own `git.commit_completed`
  event fires — a real, thinner-than-specified audit trail versus
  `EVENT_MODEL.md`'s already-written mapping table.
- `grep -n "max_calls_per_day\|rate.limit" src/athena/mcp_server/
  research_tools.py src/athena/mcp_server/job_tools.py` — confirmed no
  rate-limiting exists on `research_start`/`reindex_start` dispatch,
  despite `note_summarize`'s Phase 9 daily-call-ceiling precedent existing
  right next to it.
- `find .github -type f` — confirmed no CI pipeline exists at all.
- Read `deployment/README.md`'s "Open items" section — confirmed the
  venv-path placeholder (`%h/athena/.venv`) has blocked the systemd/
  bubblewrap configs since Phase 1.
- Confirmed via direct file reads that `fastembed` still has no
  revision-pinning mechanism (an accepted, carried-forward limitation, not
  a Phase 10 deliverable).

## Design

`docs/design/production-hardening.md` drafted covering: vault event
emission for the 6 mutating tools (§2.1), a generalized daily dispatch
ceiling for `research_start`/`reindex_start` (§2.2), an adversarial
security regression suite (§2.3), an `athena bench` CLI (§2.4),
failure-recovery regression tests (§2.5), the deployment venv-path
resolution (§2.6), documentation (README/Operations Guide) (§2.7), and a
CI pipeline plus release checklist (§2.8). Explicitly out of scope: a
metrics/APM stack, general rate-limiting infrastructure beyond the
cost-ceiling pattern, a multi-OS/Python CI matrix, enterprise-scale load
testing, Qdrant's own file permissions (Docker-managed), and the actual
v1.0 git tag (Phase 11's job).

Presented to the user via `AskUserQuestion` and accepted ("Yes, accept and
implement with agents (Recommended)") before any implementation began.

## Implementation

Built a small shared foundation directly first (the pattern every prior
multi-agent phase has used — highest-risk/most-shared code built directly,
then parallelized against a stable interface):

- `athena.db.repository.events.record_vault_event` — never raises, mints
  its own event_id/correlation_id/occurred_at via the existing
  `append_event` unless overridden. Deliberately placed in the
  transport-agnostic repository layer, not `athena.mcp_server`, mirroring
  why `athena.git.write.auto_commit_mutation` isn't `mcp_server`-scoped
  either (`athena.research.workflow.commit_draft` needs it too, and is
  called from both the MCP tool and — indirectly — the CLI).
- `athena.db.repository.research_jobs.check_daily_dispatch_limit`/
  `DispatchLimitError`/`count_dispatched_today` — same placement reasoning:
  `athena.cli._cmd_research_start` dispatches independently of the MCP
  tool layer entirely (confirmed via `grep`), so both callers need one
  shared check, not two subtly-different ones.
- Two new `AthenaConfig` fields (`research_max_dispatches_per_day` default
  50, `reindex_max_dispatches_per_day` default 20) plus 9 existing test
  files patched to include them in their `AthenaConfig(...)` literals.

Then dispatched 7 parallel agents against this fixed foundation, each with
a fully self-contained prompt (exact before/after code context, exact
interface signatures, exact verification commands):

- **Agent A** — wired `record_vault_event` into `note_create`, `note_move`
  (`write_tools.py`) and `note_update`/`note_link`/`note_delete`/
  `note_merge` (`mutation_tools.py`), right after each tool's existing
  `auto_commit_mutation` call. Added one test per call site plus an
  "unaffected on failure" test per file, mirroring Phase 8's identical
  test pattern for `auto_commit_mutation`. 606 tests passing after this
  agent, ruff/mypy clean.
- **Agent B** — wired `check_daily_dispatch_limit` into `research_start`
  (`research_tools.py`), `reindex_start` (`job_tools.py`), and
  `_cmd_research_start` (`cli.py`), each checked *before* dispatch,
  refusing cleanly (never raising) at the ceiling. Deferred `cli.py`'s
  `athena.worker` import until after the check passes, simplifying its own
  CLI test (no `ATHENA_HUEY_SECRET` needed for the refusal path). 610
  passing, 1 failure flagged as out-of-scope (Agent D's concurrent `bench`
  work) — did not reproduce in the final combined run.
- **Agent C** — built `tests/security/test_adversarial.py` (24 tests, plus
  a new `tests/security/conftest.py`): real, non-mocked attacks — path
  traversal/symlink escape against every path-taking MCP tool, FTS5
  query-syntax injection against `vault_search`, an SSRF cross-check
  against `run_research` (not just `fetch_url`, confirming the Phase 7
  defense is wired into the higher-level workflow), a secret-shaped-
  content check against `commit_draft`'s real secret-scan wiring, and a
  `.gitleaks.toml` config-drift guard (parses the TOML directly, no
  `gitleaks` binary needed). **No bugs found** — every defense behaved
  exactly as its own docstrings/design docs claim.
- **Agent D** — built `athena bench {index,retrieval,mutation}`. `bench
  index`/`bench retrieval` reuse existing worker machinery (`run_bootstrap`
  /`run_index_bootstrap`/`run_retrieval_evaluate`) directly, no new
  measurement logic. `bench mutation` (new logic) builds a throwaway
  git-repo vault and times a real note-write + `commit_paths` round trip.
  Dropped `--corpus` for `bench index` (neither worker function accepts a
  redirect target) and reimplemented a 4-line `git init` sequence rather
  than importing from `tests/` into `src/`. Found and worked around a
  pre-existing caveat: `athena.worker`'s module-level state freezes at
  whichever test imports it first in a full-suite run, so switched its
  `bench index`/`bench retrieval` tests to parser-only wiring checks
  rather than calling `main()` directly. Live-verified: `mean=10.95ms
  p95=11.29ms` for a real 3-iteration `bench mutation` run.
- **Agent E** — extended `tests/vault/test_reconcile.py` and added
  `tests/db/test_concurrency.py`. **Found a real, previously-unverified
  gap and correctly left it as a tracked, visible `xfail`, not fixed**
  (see "Real findings" below) — per instructions, since production-code
  fixes were out of scope for this test-only task and other agents were
  concurrently touching related files. Also proved real SQLite writes
  under contention either succeed after a bounded wait or fail with a
  bounded `OperationalError`, never hang forever.
- **Agent F** — resolved the deployment venv-path placeholder
  (`~/.local/share/athena/.venv`, XDG `share` convention, distinct from
  `~/.local/state/athena`'s existing runtime-state usage) in both the
  systemd unit and the bubblewrap script; updated `deployment/README.md`;
  rewrote `README.md` (quickstart, full CLI-family reference table,
  architecture links, a `LICENSE` summary, preserved the original
  Contributing/Development section); wrote a new `docs/OPERATIONS_GUIDE.md`
  (install, systemd worker, Qdrant setup, backup guidance citing
  `SECURITY_MODEL.md`'s embedding-inversion finding, a troubleshooting
  table of four real gotchas each cited to its source design doc).
- **Agent G** — wrote this project's first-ever CI pipeline,
  `.github/workflows/ci.yml` (ruff check/mypy/pytest against
  `ubuntu-latest`/Python 3.12, a real `qdrant/qdrant:v1.19.1` GitHub
  Actions service container for the two test files that need a live
  server — confirmed via grep that only `tests/indexing/test_qdrant_store.
  py` and `tests/retrieval/test_vector_search.py` need one, both
  hardcoding `127.0.0.1:6333` directly, no env var needed), and
  `docs/RELEASE_CHECKLIST.md` (an 8-step checklist for Phase 11's future
  use, explicitly noting that pushing a release tag requires the user's
  go-ahead per this project's established practice).

### One small follow-up done directly, beyond the 7 agents' scope

While writing the agent prompts, noticed `EVENT_MODEL.md`'s mapping table
specifies `research_commit` should also emit `vault.note_created`, but the
design doc's own §2.1 summary table only listed the six `write_tools.py`/
`mutation_tools.py`-owned tools — an inconsistency in my own design doc.
Tracked it rather than immediately fixing it (to avoid distracting from
writing the agent prompts), then closed it directly after all 7 agents
completed and full verification passed: added `record_vault_event(...,
event_type="vault.note_created", ...)` to `athena.research.workflow.
commit_draft`'s existing `auto_commit_mutation` call site, plus one new
test (`test_research_commit_records_a_vault_note_created_event`) confirming
the event lands in the real `events` table with the correct payload.

### Two stale `SECURITY_MODEL.md` findings corrected

Updated the document itself (corrections left in place alongside the
original findings, not deleted, so the reasoning trail stays visible):

- **TB-13** (Huey sync-core/async-bridge boundary): its premise doesn't
  hold for the as-built system. `grep -rn "aget_result" src/` confirms
  `aget_result()` is never called anywhere — every task-backed tool
  dispatches fire-and-forget and the caller polls separately via
  `job_status`. A safer pattern than ADR-0002 originally envisioned,
  arrived at organically during Phase 6/7 implementation.
- **TB-1's Repudiation and DoS findings**: both now marked mitigated,
  pointing at Phase 10's `record_vault_event`/`check_daily_dispatch_limit`
  work respectively.

## Verification

Independently re-verified all 7 agents' combined output after they
completed, matching the established pattern from Phases 7-9 (never trust a
building agent's self-report alone):

- `ruff check src tests` — clean.
- `mypy src` — `Success: no issues found in 75 source files`.
- Full suite `pytest -q` — **638 passed, 1 xfailed** (the tracked
  reconciliation gap, not a regression), no failures, no file collisions
  between any of the 7 agents' outputs.
- After the direct `research_commit` follow-up: **639 passed, 1 xfailed**,
  re-confirmed clean on ruff/mypy/pytest.

## Real findings from this session (verify-before-trust discipline)

1. **A real, previously-unverified gap in `reconcile_vault`, found by
   Agent E**: its discrepancy detection is purely `notes.content_hash`-vs-
   disk based (`_ingest_note_locked`'s existing-hash-matches short-circuit
   to `"noop"`). A worker crash *after* the `notes` row is written but
   *before* `_persist_secret_scan_result`/provenance/tags persist is
   invisible to reconciliation — it reports zero discrepancies and never
   backfills the missing rows. Empirically confirmed
   (`discrepancies_found=0`, provenance activities remain `[]` after
   reconciliation) before being written up as a tracked, visible `xfail`
   rather than a silent gap — matching `TESTING_STRATEGY.md`'s own
   established convention for this. **Not fixed this session** (test-only
   scope for §2.5, and several other agents were concurrently touching
   related files) — a real fix would extend `reconcile_vault` to also
   detect notes with no provenance activity, not just content-hash
   mismatches.
2. **TB-13 in `SECURITY_MODEL.md` was stale**, corrected as described
   above.
3. **`athena.worker`'s module-level state freezes at whichever test
   imports it first in a full-suite run** — a pre-existing caveat
   (already documented in `test_duplicates_requires_a_subcommand`'s own
   comment), rediscovered by Agent D when a `bench index` CLI test passed
   in isolation but failed in the full suite for this reason. Worked
   around by testing `build_parser()` wiring instead of calling `main()`
   directly for the worker-backed `bench` subcommands.
4. **`EVENT_MODEL.md`'s `research_commit` → `vault.note_created` gap**,
   closed directly (see above).

## Quality gates

- `pytest`: 639/639 passing, 1 `xfailed` (tracked, documented), 0 skipped.
- `mypy --strict` across `src/`: clean.
- `ruff check` across `src/` and `tests/`: clean.
- Live verification: `athena bench mutation --iterations 3` against a real
  scratch vault produced real, non-zero timing output.

## What remains (see `NEXT_SESSION.md` for full detail)

- Nothing from this session has been committed to git yet — awaiting
  explicit user go-ahead. Phase 9's work is already committed and pushed
  as `18d2359`.
- The `reconcile_vault` provenance-backfill gap (finding 1 above) is real
  and tracked, not fixed.
- The `ReadWritePaths=` vault-path templating placeholder in the
  deployment configs is still open — Phase 10 resolved only the venv-path
  placeholder.
- CI runs ruff/mypy/pytest but not gitleaks itself.
- Every phase named in `docs/ROADMAP.md` through Phase 10 is now
  implemented. Phase 11 (v1.0) is next per the roadmap, not started this
  session, and needs explicit scope confirmation with the user before
  work begins (unlike every other phase, it hasn't been discussed with
  them yet).
- The licensing interpretation (contribute-back-or-keep-private) baked
  into the new `LICENSE` file has not yet been explicitly confirmed back
  with the user as the correct reading of their intent — should be
  surfaced clearly, not just assumed correct.
