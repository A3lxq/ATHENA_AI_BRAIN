# ATHENA AI-BRAIN — Next Session

## Start Here

Read, in order:

1. `CLAUDE.md`
2. `docs/DEVELOPMENT_CONSTITUTION.md`
3. `CURRENT_STATE.md`
4. `docs/00_MASTER_PROJECT_SPECIFICATION.md`
5. `docs/ARCHITECTURE.md`
6. `docs/adr/0001-*.md` through `docs/adr/0011-*.md` (all Accepted)
7. `docs/DATA_MODEL.md`, `docs/EVENT_MODEL.md`, `docs/SECURITY_MODEL.md`, `docs/LONGEVITY_NOTES.md`
8. `docs/design/vault-safety-boundary.md`, `docs/design/os-level-process-sandboxing.md`, `docs/design/storage-runtime-hardening.md`, `docs/design/pre-ingestion-secret-scanning.md`
9. `docs/design/migration-runner-and-vault-ingestion.md` (Phase 2)
10. `docs/design/indexing-pipeline.md` (Phase 3 — §8 now records the Docker blocker's resolution)
11. `docs/design/retrieval-pipeline.md` (Phase 4 — §8 now records the zero-results finding's re-verification against a real indexed vault, and the embedded-mode filter bug's non-reproduction on a real server)
12. `docs/design/knowledge-intelligence.md` (Phase 5)
13. `docs/design/mcp-server.md` (Phase 6)
14. `docs/sessions/2026-09-05_mcp-server.md`, `docs/sessions/2026-09-10_docker-qdrant-resolution.md` (this session's own record)

## Objective

**Phase 0 through Phase 6 are fully implemented, tested, and committed (`a5749a8`).** This session's work was not a new phase — it resolved the standing Docker/Qdrant environment blocker that had followed every phase since Phase 3, and used the now-available real server to close out several previously-unverifiable open items.

**What changed this session:**

- The development environment's user account was added to the `docker` group (done by the user, outside this session — this session found it already resolved when checked). A real, pinned-version, `127.0.0.1`-only Qdrant server is now running: `docker run --name athena-qdrant -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 -v athena_qdrant_storage:/qdrant/storage --restart unless-stopped qdrant/qdrant:v1.19.1` (per ADR-0006). `v1.19.1` was confirmed as the actual current stable release directly against the Docker Hub API — a web-search summary had claimed a stale, incorrect version.
- **All 5 previously `skip`-marked Qdrant integration tests (Phases 3-4) now run and pass.** The full suite is **418/418 passing, 0 skipped** — the first time this project has had zero skips.
- Two real bugs were found and fixed in the process of actually running those tests for the first time (see "Real findings" below).
- Two genuinely open items from Phase 4 were resolved with real data instead of staying permanently unconfirmed: the embedded-mode filter bug does not reproduce on a real server; the "zero-results degradation" finding is confirmed specific to the Qdrant-never-reachable scenario (a fully-indexed real vault retrieves correctly).
- Two inaccurate "blocked pending Docker access" claims in the Phase 5/6 design docs were corrected on review — neither package's tests were ever actually skip-marked.

**Nothing from this session has been committed to git yet** — Phases 1-6 were already committed and pushed in prior sessions (`a4050d3`/`d97840d`, `aa76ce7`, `561f8d4`, `3cc946e`, `93a195a`, `cf1ece4`, `a5749a8`); this session's test fixes and doc corrections are still untracked, awaiting explicit user go-ahead.

## Real findings from this session (verify-before-trust discipline)

1. **A real cross-test pollution bug, findable only by actually running against a real, persistent server**: the 5 real-Qdrant tests share one collection across the whole suite run (unlike the `:memory:` tests, which each get fresh isolated state for free). A broad `search()` call in one test picked up leftover points an earlier test had left behind, producing a false `assert 3 == 1` failure with nothing wrong in the code under test. Fixed by clearing all points from the collection (`client.delete(..., points_selector=models.FilterSelector(filter=models.Filter()))`) at the start of each real-server test — a pattern any *future* real-server test in this codebase must also follow, not just these five.
2. **A real test bug, not a code bug**: one test called `client.get_collection_aliases(COLLECTION_ALIAS)`, passing the *alias* name where the Qdrant API expects a *collection* name. Confirmed directly against the real server: this silently returns an empty list rather than erroring — a genuine, worth-remembering Qdrant API gotcha. Fixed to use `client.get_aliases()` (list-all, then filter by `alias_name`), the same correct pattern an adjacent, already-passing test in the same file already used.
3. **A real, previously-unconfirmed open item from Phase 4, resolved**: the embedded (`:memory:`) mode bug where a filter set only on the outer `query_filter` (not on each `Prefetch`) was silently ignored does **not** reproduce against a real server — confirmed by direct testing (upserted two points differing only by `status`, queried with an outer-only filter, got the correct single result back). The defensive "filter on every `Prefetch`" mitigation in `athena.retrieval.vector_search.search()` is kept regardless (cheap, and protects against future version/mode differences) — this finding doesn't change the code, only confirms what was previously an open question.
4. **Phase 4 §8's "zero-results degradation" finding, re-verified against a fully-indexed real vault, not just the Qdrant-down scenario**: ran the real 10-note/17-question eval corpus through `migrate` → `ingest bootstrap` → `index bootstrap` → `retrieval evaluate` with Qdrant genuinely reachable the whole time. All 10 notes indexed for real (21 chunks); the evaluation produced real, non-zero, non-degenerate metrics (`recall@3/5/10: 0.357`, `mrr: 0.357`, `ndcg@10: 0.357`, `unanswerable_top1_false_positive_rate: 0.000`). This confirms the original zero-results finding was specifically about the Qdrant-never-reachable scenario, not a general defect in the retrieval pipeline — the pipeline genuinely works once its dependencies are actually available.
5. **Two design-doc claims turned out to be inaccurate on review**: `docs/design/knowledge-intelligence.md` and `docs/design/mcp-server.md` both claimed live Qdrant integration testing was "blocked pending Docker access" for their own test suites. Checking directly (`grep -rn "pytest.mark.skip" tests/intelligence/ tests/mcp_server/`) found zero matches — every test in both packages already used embedded `:memory:` Qdrant clients successfully from the start (Phase 4's own established, working pattern). Corrected rather than left inaccurate now that it was checked.

## What is genuinely still missing before Phase 7 starts

1. **`fastembed` revision-pinning**, **`watchdog` supply-chain review** — both still open, unaffected by this session.
2. **The retrieval-evaluation corpus** still ships at 10 notes/17 questions, not `TESTING_STRATEGY.md`'s 30-60 target — now genuinely unblocked to expand, since Qdrant access is no longer the obstacle.
3. **The duplicate-detection default thresholds are untuned against real vault data** — likewise now unblocked to actually tune, not just flagged.
4. **`note_create`/`note_update` content is not secret-scanned before being written** — still open, `docs/design/mcp-server.md` §6/§8.
5. **`note_summarize`, `note_history`/`git_status`/`git_log`/`git_commit`, `research_start`/`research_commit`** — still deliberately deferred, blocked on Phase 9/8/7 infrastructure respectively.
6. **A full wire-level elicitation round-trip integration test** — still not built, still a reasonable moderate-effort follow-up.
7. Real install/venv path decision for the deployment configs (`deployment/README.md`'s "Open items") — the only remaining blocker before the systemd/bubblewrap configs are actually usable.
8. Adding `secret_findings_list`/`secret_finding_resolve` to ADR-0007's MCP tool contract table (still open from ADR-0011).
9. **The Qdrant container itself is a manually-started, unmanaged Docker container** (`docker run ...`, not the systemd-managed setup ADR-0006 ultimately describes for a real deployment) — fine for this development environment, but flagged so a future session doesn't assume production-readiness from "it's running."

## Do not

- assume Docker/Qdrant access is still blocked in this environment — it was resolved this session; check `docker ps`/`curl http://127.0.0.1:6333/healthz` before assuming otherwise, since the container is not guaranteed to survive a host reboot (no systemd unit manages it yet, see item 9 above),
- write a new test against the real Qdrant server without clearing the collection first (finding #1 above) — cross-test pollution is real and reproducible, not a one-off fluke,
- call `get_collection_aliases()` with an alias name expecting it to resolve like a collection name (finding #2 above) — use `get_aliases()` + filter instead,
- treat the embedded-mode `:memory:` filter bug as necessarily present on a real server — confirmed it is not (finding #3), though the defensive per-`Prefetch` mitigation stays regardless,
- assume "keyword-only degradation returns zero results" describes general pipeline brokenness — it's specific to the Qdrant-never-reachable case; a real, working, indexed vault retrieves correctly (finding #4),
- have any MCP tool `raise` an exception for an expected/user-facing error condition — return a clear string instead; a raised exception's message is discarded by the SDK before reaching the client (a Phase 6 finding, still load-bearing),
- wrap a Phase 1-5 function directly in an MCP tool handler without checking whether it internally calls `asyncio.run()` itself — it will crash inside the server's already-running event loop (a Phase 6 finding, still load-bearing),
- stub `note_summarize`/`note_history`/`git_status`/`git_log`/`git_commit`/`research_start`/`research_commit` to "complete" ADR-0007's table — they are deliberately deferred pending Phase 7/8/9 infrastructure that doesn't exist,
- assume `merge_notes`/`note_merge` can be called directly on a `'pending'` duplicate candidate — it's a hard rejection by design (Master Spec §10),
- run `git remote set-url`/`git config` in this environment on the user's behalf — check `git remote -v` before assuming the local `origin` remote has or hasn't already been fixed,
- commit this session's work without checking with the user first (nothing has been committed yet by design).
