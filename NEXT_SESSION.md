# ATHENA AI-BRAIN — Next Session

## Start Here

Read, in order:

1. `CLAUDE.md`
2. `docs/DEVELOPMENT_CONSTITUTION.md`
3. `CURRENT_STATE.md`
4. `docs/00_MASTER_PROJECT_SPECIFICATION.md`
5. `docs/ARCHITECTURE.md`
6. `docs/adr/0001-*.md` through `docs/adr/0011-*.md` (all Accepted — especially `0007-mcp-tool-contract.md`, the contract Phase 6 implements)
7. `docs/DATA_MODEL.md`, `docs/EVENT_MODEL.md`, `docs/SECURITY_MODEL.md`, `docs/LONGEVITY_NOTES.md`
8. `docs/design/vault-safety-boundary.md`, `docs/design/os-level-process-sandboxing.md`, `docs/design/storage-runtime-hardening.md`, `docs/design/pre-ingestion-secret-scanning.md`
9. `docs/design/migration-runner-and-vault-ingestion.md` (Phase 2)
10. `docs/design/indexing-pipeline.md` (Phase 3)
11. `docs/design/retrieval-pipeline.md` (Phase 4 — read §8's degradation finding before touching `athena.retrieval`)
12. `docs/design/knowledge-intelligence.md` (Phase 5)
13. `docs/design/mcp-server.md` (Phase 6 — implemented this session; read this before touching `athena.mcp_server`, especially §0's SDK-behavior findings and §1's deferred-tool list)
14. `docs/sessions/2026-09-04_knowledge-intelligence.md`, `docs/sessions/2026-09-05_mcp-server.md` (this session's own record)

## Objective

**Phase 0 through Phase 5 are fully closed and committed. Phase 6 (MCP) is now implemented and tested.** What exists as real, tested code as of this session:

- **The unified MCP server**: `athena.mcp_server` (`python -m athena.mcp_server`), built on the official `mcp` SDK 2.1.1 — 17 tools + 1 resource, each a thin wrapper over Phases 1-5 code.
- `athena.mcp_server.read_tools`: `vault_search`, `note_read`/`vault://{path}` resource, `note_related`, `note_duplicates`, `note_provenance`, `vault_status`, `system_diagnostics` — all read-only.
- `athena.mcp_server.job_tools`: `duplicates_scan`, `reindex_start` (task-backed), `job_status`, `job_cancel` (the interim tasks-extension shim ADR-0007 called for).
- `athena.mcp_server.write_tools`: `note_create`, `note_move` — non-destructive mutations.
- `athena.mcp_server.mutation_tools`: `note_update` (patch — always safe, append-only; overwrite — destructive, MRTR-gated), `note_link` (wraps patch mode), `note_delete`, `note_merge` (both destructive, MRTR-gated) — built directly by the orchestrator, not delegated, same as Phase 5's merge engine.
- `athena.mcp_server._runtime`/`vault_status.py`: shared config/Qdrant-client singleton and the `vault_status` aggregation, mirroring `athena.worker`'s own lazy-singleton pattern.
- `athena.db.repository.provenance`/`research_jobs`/`notes` extended with `get_activities_for_note`, `get_by_id`/`mark_cancelled`/`count_by_status`, `count_active`. `athena.worker` gained `duplicates_scan_task`/`reindex_task`.
- **Deliberately deferred, not stubbed**: `note_summarize` (Phase 9), `note_history`/`git_status`/`git_log`/`git_commit` (Phase 8), `research_start`/`research_commit` (Phase 7) — none of that infrastructure exists in the codebase yet.

413/413 tests passing (57 new this session, 5 correctly `skip`-marked pending Docker access, unchanged from before this phase), mypy --strict clean, ruff clean. **Nothing from this session has been committed to git yet** — Phases 1-5 and the full rename were already committed and pushed in prior sessions (`a4050d3`/`d97840d`, `aa76ce7`, `561f8d4`, `3cc946e`, `93a195a`, `cf1ece4`); this session's Phase 6 work is still untracked, awaiting explicit user go-ahead.

## Real findings from this implementation session (verify-before-trust discipline)

1. **The `mcp` SDK moved from v2.0.0 (ADR-0007's own research) to 2.1.1**, and the server class was renamed from the generic "FastMCP-style" language ADR-0007 used to the concrete `MCPServer` — confirmed by installing the package and inspecting it directly, not by trusting the ADR's 11-day-old research. The unrelated third-party `fastmcp` package (PrefectHQ) was deliberately NOT used.
2. **A real, empirically-confirmed constraint on every async tool handler**: `mcp.run()`'s stdio transport uses stdout as the wire. Verified `athena.logging_setup` already defaults to stderr and that loading the embedding model for the first time (HuggingFace Hub warnings, a `tqdm` progress bar) never writes to stdout, by monkey-patching `sys.stdout` to raise during a real `embed_dense()` call.
3. **`athena.diagnostics.run_doctor()` internally calls `asyncio.run()`, which crashes when invoked from an already-running async tool-handler event loop** (`RuntimeError: asyncio.run() cannot be called from a running event loop`) — found while building `system_diagnostics`, fixed via `asyncio.to_thread(run_doctor, config)`. This is a general constraint: any *existing* synchronous Phase 1-5 function that itself calls `asyncio.run()` internally will hit this the same way if wrapped directly in an MCP tool handler — check for this pattern before wrapping any function not already covered by this session's tools.
4. **A significant, protocol-level finding, confirmed via a real client/server round trip (not assumed from documentation)**: an MCP tool that *raises* an exception has its message replaced by the SDK's own generic `"Error executing tool <name>"` wrapper (`Tool.run()` wraps any exception as `UnexpectedToolError`, and `_handle_call_tool` returns `str()` of that wrapper, discarding the original exception's message) before it ever reaches the client. `note_read` originally raised `ValueError` with a helpful message; this was silently lost until caught by an actual integration test. **Every tool in this server now returns a plain string for expected/user-facing error conditions instead of raising** — this is now the established, load-bearing convention for any future tool added to this server, not a stylistic preference.
5. **A real security-relevant SDK behavior found while building `note_create`**: `athena.safety.paths.resolve_vault_path(path, vault_root, PathMode.CREATE)` does **not** raise for an already-existing target — by design (CREATE mode exists for legitimately-not-yet-existing paths), it silently resolves to the existing file. `note_create`'s overwrite guard is therefore an explicit check, and the actual write uses `os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW)` (atomic create-or-fail) rather than a check-then-write race; `note_move` similarly uses `os.link`+`os.unlink` rather than `Path.rename()` (which would silently replace an existing destination).
6. **A parallel agent building `duplicates.py` in Phase 5 had already failed once on a session rate limit; this session, all three parallel agents (read_tools, job_tools, write_tools) completed successfully** and each found a genuine, non-trivial bug or gap in the process (items 3 and 5 above, plus job_tools' own discovery that `@huey.task()`-decorated functions return a `huey.api.Result` whose `.id` is the real task id needed for `job_status`/`job_cancel`).

## What is genuinely still missing before Phase 6/7 are "done"

1. **Resolve the Docker-access blocker** — unchanged since Phase 3; 5 real, correct integration tests remain `skip`-marked across Phases 3-4.
2. **Phase 4's zero-results-on-full-degradation gap** (`docs/design/retrieval-pipeline.md` §8) — still open, unaffected by Phase 5/6's work.
3. **`fastembed` revision-pinning**, **`watchdog` supply-chain review** — both still open, unchanged since earlier phases.
4. **The retrieval-evaluation corpus** still ships at 10 notes/17 questions, not `TESTING_STRATEGY.md`'s 30-60 target.
5. **Status promotion beyond `'draft' -> 'active'`** — still only that one transition (Phase 5); unaffected by Phase 6.
6. **The duplicate-detection default thresholds are untuned against real vault data** — unaffected by Phase 6, still flagged in the knowledge-intelligence design doc §8.
7. **`note_create`/`note_update` content is not secret-scanned before being written** — a real, named gap in `docs/design/mcp-server.md` §6/§8: Phase 2's pre-ingestion scan runs on already-on-disk vault files, not this new model-driven write path. Flagged, not solved.
8. **`note_summarize`, `note_history`/`git_status`/`git_log`/`git_commit`, `research_start`/`research_commit`** — deliberately deferred MCP tools, blocked on Phase 9/8/7 infrastructure respectively. Do not attempt to stub these; build the real infrastructure first when those phases arrive.
9. **A full wire-level elicitation round-trip integration test** (a real `ClientSession` actually responding to a live `note_delete`/`note_merge` confirmation prompt over the protocol, rather than a duck-typed fake `Context`) was not built — the elicitation logic and the protocol dispatch are each fully tested separately, just not combined into one test. A reasonable, moderate-effort follow-up.
10. Real install/venv path decision for the deployment configs (`deployment/README.md`'s "Open items") — the only remaining blocker before `athena-mcp-launch.sh`/`athena-huey-worker.service` are actually usable; both entry points they launch now exist as real code.
11. Adding `secret_findings_list`/`secret_finding_resolve` to ADR-0007's MCP tool contract table (still open from ADR-0011).
12. `note_update`'s patch mode always appends (never surgically replaces an existing named section) — a deliberate simplification for safety/simplicity, not a bug, but worth reconsidering if a future need for true section-replacement emerges.

## Do not

- silently alter accepted architecture — Phase 4's zero-results degradation gap and Phase 5's untuned thresholds remain documented, not patched, for exactly this reason,
- have any MCP tool `raise` an exception for an expected/user-facing error condition — return a clear string instead (finding #4 above); a raised exception's message is discarded by the SDK before reaching the client,
- wrap a Phase 1-5 function directly in an MCP tool handler without checking whether it internally calls `asyncio.run()` itself (finding #3 above) — it will crash inside the server's already-running event loop,
- assume `resolve_vault_path(..., PathMode.CREATE)` raises for an existing target — it does not; any future create-style tool needs its own explicit overwrite guard (finding #5 above),
- use `Path.rename()`/plain `Path.write_text()` for a "must not overwrite/must not silently replace" filesystem operation anywhere in `athena.mcp_server` — use the same `os.open(O_CREAT|O_EXCL|O_NOFOLLOW)`/`os.link`+`os.unlink` pattern `write_tools.py` already established,
- stub `note_summarize`/`note_history`/`git_status`/`git_log`/`git_commit`/`research_start`/`research_commit` to "complete" ADR-0007's table — they are deliberately deferred pending Phase 7/8/9 infrastructure that doesn't exist,
- assume `merge_notes`/`note_merge` can be called directly on a `'pending'` duplicate candidate — it's a hard rejection by design (Master Spec §10),
- assume the 5 skipped Qdrant integration tests pass just because the rest of the suite does — they haven't been run at all in this environment,
- run `git remote set-url`/`git config` in this environment on the user's behalf — check `git remote -v` before assuming the local `origin` remote has or hasn't already been fixed,
- commit the current Phase 6 work without checking with the user first (nothing has been committed yet by design).
