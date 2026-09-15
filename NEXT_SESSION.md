# ATHENA AI-BRAIN — Next Session

## Start Here

Read, in order:

1. `CLAUDE.md`
2. `docs/DEVELOPMENT_CONSTITUTION.md`
3. `CURRENT_STATE.md`
4. `docs/00_MASTER_PROJECT_SPECIFICATION.md`
5. `docs/ARCHITECTURE.md`
6. `docs/adr/0001-*.md` through `docs/adr/0011-*.md` (all Accepted)
7. `docs/DATA_MODEL.md`, `docs/EVENT_MODEL.md`, `docs/SECURITY_MODEL.md`, `docs/LONGEVITY_NOTES.md`, `docs/GIT_WORKFLOW.md`
8. `docs/design/vault-safety-boundary.md`, `docs/design/os-level-process-sandboxing.md`, `docs/design/storage-runtime-hardening.md`, `docs/design/pre-ingestion-secret-scanning.md`
9. `docs/design/migration-runner-and-vault-ingestion.md` (Phase 2)
10. `docs/design/indexing-pipeline.md` (Phase 3)
11. `docs/design/retrieval-pipeline.md` (Phase 4)
12. `docs/design/knowledge-intelligence.md` (Phase 5)
13. `docs/design/mcp-server.md` (Phase 6)
14. `docs/design/research-ingestion.md` (Phase 7)
15. `docs/design/git-automation.md` (Phase 8)
16. `docs/design/multi-llm.md` (Phase 9)
17. `docs/design/production-hardening.md` (Phase 10 — new this session)
18. `docs/RELEASE_CHECKLIST.md`, `docs/OPERATIONS_GUIDE.md` (both new this session, Phase 11's own future tools)
19. `docs/sessions/2026-09-13_phase9-multi-llm.md`, `docs/sessions/2026-09-15_phase10-production-hardening.md` (this session's own record)

## Objective

**Phase 0 through Phase 10 are all implemented and tested — every phase named in `docs/ROADMAP.md` through Phase 10 now exists.** Phase 9's work is already committed and pushed (`18d2359`); Phase 10's work has not been committed yet.

**What changed this session:**

- Drafted `docs/design/production-hardening.md` directly from a codebase audit (not fresh external research — checking this project's own already-written design docs against what was actually built), presented it, and got explicit acceptance ("Yes, accept and implement with agents").
- Built the shared foundation directly (`athena.db.repository.events.record_vault_event`, `athena.db.repository.research_jobs.check_daily_dispatch_limit`/`DispatchLimitError`, two new `AthenaConfig` fields), then dispatched 7 parallel agents against it:
  - **Agent A**: wired `vault.*`/`dedup.*` event emission into all 6 mutating vault MCP tools (`write_tools.py`/`mutation_tools.py`), closing a real Repudiation gap.
  - **Agent B**: wired the daily dispatch ceiling into `research_start`/`reindex_start`, at both the MCP tool layer and the CLI's independent dispatch path (`cli.py`'s `_cmd_research_start`), closing a real DoS gap.
  - **Agent C**: built `tests/security/test_adversarial.py` (24 tests, path traversal/symlink escape, FTS5 injection, SSRF cross-check, secret-scan-in-commit-draft check, gitleaks config-drift guard) — found no bugs, every defense held.
  - **Agent D**: built `athena bench {index,retrieval,mutation}`, live-verified against a real throwaway vault.
  - **Agent E**: built failure-recovery tests (`tests/vault/test_reconcile.py` extended, `tests/db/test_concurrency.py` new) — **found a real, previously-unverified gap** and correctly left it as a tracked `xfail` instead of fixing it (see below).
  - **Agent F**: resolved the deployment venv-path placeholder, rewrote `README.md`, wrote `docs/OPERATIONS_GUIDE.md`.
  - **Agent G**: wrote the project's first-ever CI pipeline (`.github/workflows/ci.yml`) and `docs/RELEASE_CHECKLIST.md`.
- Independently verified all 7 agents' combined output myself after they completed: full `pytest`/`ruff`/`mypy` pass, confirmed no file collisions.
- Did one small follow-up directly, beyond any of the 7 agents' assigned scope: added `vault.note_created` emission to `research_commit`/`commit_draft`, closing a gap `EVENT_MODEL.md`'s mapping table specifies but my own design doc's §2.1 summary table had missed.
- Corrected two stale findings in `SECURITY_MODEL.md` itself: TB-13's `aget_result()` blocking-bridge scenario doesn't apply to the as-built system (`aget_result()` is never called anywhere — confirmed via `grep`); TB-1's Repudiation and DoS findings are now marked mitigated, pointing at this phase's work.
- Mid-session, the user gave a standing instruction about licensing: the project should be personal-use only, and modifications by others must either be contributed back to the parent repository or not published/distributed at all. Added a new `LICENSE` file implementing this (custom, plain-language, explicitly not lawyer-reviewed) — no `LICENSE` existed at the repo root before this session.
- 639/639 tests passing (1 xfailed — see "Real findings" below, not a regression), mypy --strict clean, ruff clean.

**Nothing from this session has been committed** — awaiting explicit user go-ahead. Phase 9's work is already committed/pushed as `18d2359`, Phase 8's as `64bc1c8`.

## Real findings from this session (verify-before-trust discipline)

1. **A real, previously-unverified gap in `reconcile_vault`, found by Agent E and left as a tracked `xfail`, not fixed** (test-only scope for this phase, and several other agents were concurrently touching related files): its discrepancy detection is purely `notes.content_hash`-vs-disk based. A worker crash *after* the `notes` row is written (content hash already correct) but *before* `_persist_secret_scan_result`/provenance/tags are persisted is invisible to reconciliation — it reports zero discrepancies and never backfills the missing rows. This means `TESTING_STRATEGY.md`'s "independent second safety net" claim is not fully true for this specific crash window. See `tests/vault/test_reconcile.py::test_reconcile_backfills_provenance_after_crash_between_note_write_and_provenance_write`'s `xfail` reason string for the exact mechanism. **A real fix would extend `reconcile_vault` to also detect notes with no provenance activity, not just content-hash mismatches** — not attempted this session, flagged for whenever reconciliation logic is next touched.
2. **TB-13 in `SECURITY_MODEL.md` was stale and has been corrected in the document itself**: it assumed the system blocks on Huey's `aget_result()`, but `grep -rn "aget_result" src/` confirms it is never called anywhere — every task-backed MCP tool dispatches its Huey task fire-and-forget and the caller polls status separately via `job_status`. This is a safer pattern than ADR-0002 originally envisioned, arrived at organically during Phase 6/7 implementation, not by deliberate threat-driven design. The correction is left in place alongside the original finding (not deleted), so the reasoning trail stays visible.
3. **`athena.worker`'s module-level state (`_config`/`huey`) freezes at whichever test imports it first in a full suite run** — this is a pre-existing caveat (already documented in `test_duplicates_requires_a_subcommand`'s own comment), but Agent D rediscovered it the hard way: a `bench index` CLI test that passed in isolation failed in the full suite for this reason, and was rewritten to test only `build_parser()` wiring instead of calling `main()` directly. Worth remembering if a future CLI test for a worker-backed command behaves inconsistently between isolated and full-suite runs.
4. **No new provider/model/library research this session** — Phase 10 was explicitly scoped as closing already-identified internal gaps via direct codebase audit, not new external research, per the accepted design doc's own framing.

## What is genuinely still missing before Phase 11 starts

Every phase named in `docs/ROADMAP.md` (Phases 1 through 10) is now implemented. Remaining open items, none of them phase-blocking:

1. **The `reconcile_vault` provenance-backfill gap** (finding 1 above) — a real, tracked, visible `xfail`, not silently ignored.
2. **`fastembed` revision-pinning**, **`watchdog` supply-chain review** — both still open, unaffected by Phase 10.
3. The retrieval-evaluation corpus still ships at 10 notes/17 questions.
4. The duplicate-detection default thresholds are still untuned against real vault data.
5. **`note_create`/`note_update` content is still not secret-scanned before being written** — the original Phase 6 gap, unaffected by Phase 10 (Phase 7 closed this only for `research_commit`'s write path, and it still gets its independent secret-scan-in-commit-draft coverage confirmed live by Agent C's adversarial suite).
6. A full wire-level elicitation round-trip integration test — still not built for any MRTR-gated tool.
7. `git pull`/sync workflow, merge-conflict resolution tooling — both explicitly out of scope for Phase 8, the latter permanently.
8. **CI runs ruff/mypy/pytest but not gitleaks itself** — a reasonable follow-up, not built this session.
9. Model routing and a real per-provider dollar-cost ceiling for the LLM adapter — still explicitly judged not-yet-justified.
10. Multi-source synthesis (combining several notes/research drafts via an LLM) — still explicitly out of scope.
11. Adding `secret_findings_list`/`secret_finding_resolve` to ADR-0007's MCP tool contract table — still open from ADR-0011.
12. The Qdrant container is still a manually-started, unmanaged Docker container (no systemd unit) — the new Operations Guide documents the manual command precisely, but doesn't automate it.
13. `merge_notes` still leaves the absorbed note's file physically in the vault after a merge — flagged, not fixed, since Phase 5.
14. **The `ReadWritePaths=` vault-path templating placeholder in the bubblewrap/systemd deployment configs is still open** — Phase 10 resolved only the venv-path placeholder, not this one.
15. **Phase 11 — v1.0 — has not been researched, designed, or confirmed with the user in detail yet.** Per `docs/ROADMAP.md`, this should be a lighter wrap-up than a new-feature phase: a final consistency pass across continuity docs, closing or accurately documenting all known open items above, a version bump in `pyproject.toml` (currently `0.1.0`), and cutting a git tag using `docs/RELEASE_CHECKLIST.md` (new this session). It may not need a full Article-2 design document since it's a milestone/wrap-up rather than new functionality — but get explicit confirmation from the user on scope before starting, since this hasn't been discussed with them yet.
16. **The licensing interpretation should be surfaced explicitly to the user, not just assumed correct.** The user's instruction ("personal use only... any updates made by other users to be updated in the parent repository or do not update it at all") was interpreted as: modifications must either be contributed back to the parent repo, or kept strictly private (never independently redistributed/forked). This interpretation is baked into the new `LICENSE` file's wording but has not yet been explicitly confirmed back with the user as the correct reading of their intent.

## Do not

- treat Phase 10's `check_daily_dispatch_limit`/`record_vault_event` as `athena.mcp_server`-scoped helpers — they deliberately live in `athena.db.repository` (transport-agnostic), because the CLI's `_cmd_research_start` and `athena.research.workflow.commit_draft` both need them independently of the MCP tool layer, exactly like `athena.git.write.auto_commit_mutation` already established,
- assume `reconcile_vault` is a complete safety net for every kind of partial-crash state — it only catches content-hash-vs-disk mismatches; the provenance-backfill gap (finding 1 above) is real and currently only guarded by a tracked `xfail`, not a fix,
- fix the `xfail`'d reconciliation gap as a "quick win" without first checking whether it's actually in scope for whatever phase/task prompted looking at it — it was deliberately left as test-only work this session, not because it's unimportant, but because Phase 10 was scoped as test-only for §2.5 and several other agents were concurrently touching related files,
- run `git remote set-url`/`git config` in this environment on the user's behalf without being asked,
- commit this session's Phase 10 work without checking with the user first — nothing has been committed yet by design (Phase 9's `18d2359` and Phase 8's `64bc1c8` are already pushed, don't re-commit or duplicate them),
- start Phase 11 without first getting explicit user confirmation on its scope — it hasn't been discussed with them in detail yet, unlike every other phase which went through the design-doc-and-acceptance rhythm before implementation began.
