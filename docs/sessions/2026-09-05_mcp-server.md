# Session 023 — Unified MCP Server (Phase 6)

**Date:** 2026-09-05
**Phase:** 6 (MCP), building on Phases 1-5
**Status:** Complete — no git commit made yet

## Objective

Following "start phase 6 mcp," re-verified ADR-0007's already-accepted MCP
tool contract against the currently-installed SDK (rather than trusting
11-day-old research), drafted and got acceptance ("yes i accept") for
`docs/design/mcp-server.md`, then implemented it using the same "shared
foundation first, then parallel agents for independent modules, then direct
implementation of the highest-risk pieces" pattern that worked for every
prior phase — with all three parallel agents completing successfully this
time (Phase 5 had one rate-limit failure).

## Research performed before designing

- **The `mcp` SDK moved from v2.0.0 (ADR-0007's research) to 2.1.1.**
  Installed it and verified directly against the real package rather than
  trusting the ADR's language: the server class is `MCPServer` (`from
  mcp.server import MCPServer`), not the generic "FastMCP-style" name
  ADR-0007's research used — the actual `FastMCP` name now belongs to an
  unrelated third-party package (`fastmcp`, PrefectHQ), never used here.
  `mcp` ships a `py.typed` marker (no mypy override needed, unlike `huey`/
  `datasketch`).
- **Elicitation (MRTR confirmation)**: `await ctx.elicit(message: str,
  schema: type[BaseModel]) -> ElicitationResult`, `.action` is
  `"accept"`/`"decline"`/`"cancel"`, `.data` only populated on accept.
  Confirmed the schema must be flat/primitive fields only (a nested model
  raises `TypeError`). Verified by directly inspecting the installed
  `Context.elicit` signature and constructing real `AcceptedElicitation`/
  `DeclinedElicitation`/`CancelledElicitation` instances.
- **A new, concrete, empirically-verified finding not in ADR-0007's
  original research**: `mcp.run()`'s stdio transport uses stdout as the
  wire. Verified this is not a problem for the existing codebase by
  monkey-patching `sys.stdout` to raise on any write, then calling
  `athena.indexing.embedding.embed_dense()` for the first time (triggering
  a HuggingFace Hub download, an unauthenticated-request warning, and a
  `tqdm` progress bar) — zero stdout writes occurred.
- **A second, more significant finding**: three tool families in
  ADR-0007's table depend on infrastructure that doesn't exist —
  `note_summarize` needs Phase 9's multi-LLM adapter, `note_history`/
  `git_status`/`git_log`/`git_commit` need Phase 8's Git automation module
  (confirmed absent by searching the codebase), `research_start`/
  `research_commit` need Phase 7's research-workflow logic (only a thin
  job-tracking table exists). Deferred rather than stubbed.
- `huey.revoke_by_id(task_id)` confirmed present on the installed
  `SqliteHuey` for `job_cancel`.

## What was built

### Shared foundation (done directly, before parallel agents)

- `athena.db.repository.provenance`: `ProvenanceRow`/`get_activities_for_note`.
- `athena.db.repository.research_jobs`: `ResearchJobRow`, `get_by_id`,
  `mark_cancelled`, `count_by_status`.
- `athena.db.repository.notes`: `count_active`.
- `athena.mcp_server._runtime`: shared config/Qdrant-client singleton,
  mirroring `athena.worker`'s own lazy-singleton pattern (a plain,
  unvalidated Qdrant client, not the worker's `ensure_collection`-eager
  one — every read tool already tolerates a missing collection via
  Phase 4/5's own degradation paths).
- `athena.mcp_server.vault_status`: the `VaultStatus` aggregation
  (deliberately dropped a `last_reconcile_at` field from the design doc's
  sketch — no cheap existing query for it, and adding events-table
  querying just for one field wasn't justified).
- `athena/mcp_server/`, `tests/mcp_server/` package skeletons and shared
  test fixtures (`conn`, `huey`, `vault_dir`, `vault_root`, `qdrant_client`
  — the same shape every prior phase's test fixtures used).

### Parallel agents (narrow, non-overlapping scope, each required to
install/test/mypy/ruff itself before reporting) — all three succeeded

1. **Read-only tools** (`athena/mcp_server/read_tools.py`) — `vault_search`,
   `note_read`/`vault://{path}`, `note_related`, `note_duplicates`,
   `note_provenance`, `vault_status`, `system_diagnostics`. **Found and
   fixed a real bug**: `athena.diagnostics.run_doctor()` internally calls
   `asyncio.run()` for its schema-version check, which crashes
   (`RuntimeError: asyncio.run() cannot be called from a running event
   loop`) when invoked from `system_diagnostics`'s own already-running
   async tool-handler loop — verified empirically, fixed via
   `asyncio.to_thread(run_doctor, config)`.
2. **Job-dispatch tools** (`athena/mcp_server/job_tools.py` + two new
   `athena.worker` task functions) — `duplicates_scan`, `reindex_start`,
   `job_status`, `job_cancel`. Confirmed empirically that calling a
   `@huey.task()`-decorated function returns a `huey.api.Result` whose
   `.id` is the real Huey task id. Found and worked around a real
   import-ordering trap: eagerly importing `athena.worker` (required,
   since this whole module dispatches to it) transitively triggers
   `athena.worker`'s own module-level `build_huey()`, which hard-fails
   without `ATHENA_HUEY_SECRET` — at test-*collection* time, before any
   fixture runs. Fixed with an `os.environ.setdefault(...)` placeholder
   before the import, then swapping in a correctly-configured fresh
   `athena.worker` module per test (the same pattern `tests/test_worker.py`
   already established).
3. **Note create/move tools** (`athena/mcp_server/write_tools.py`) —
   `note_create`, `note_move`. **Found a real, security-relevant SDK gap**:
   `resolve_vault_path(path, vault_root, PathMode.CREATE)` does not raise
   for an already-existing target — by design, CREATE mode exists for
   legitimately-not-yet-existing paths, so it silently resolves to the
   existing file instead. The overwrite guard therefore has to be an
   explicit check, not a caught exception; the agent went further than the
   literal task instructions and closed the resulting TOCTOU race with
   `os.open(path, O_CREAT | O_EXCL | O_NOFOLLOW)` (atomic create-or-fail)
   for `note_create` and `os.link`+`os.unlink` (rather than `Path.rename()`,
   which would silently replace an existing destination) for `note_move`
   — flagged clearly in its own report rather than silently deviating.

### Direct implementation (highest-risk destructive code, mirroring how
Phase 5's merge engine was hand-built rather than delegated)

- `athena/mcp_server/mutation_tools.py` — `note_update` (patch mode always
  appends, never destructive, no confirmation needed; overwrite mode
  replaces the whole body, MRTR-gated), `note_link` (thin wrapper over
  patch mode), `note_delete`, `note_merge` (wraps Phase 5's `merge_notes`,
  adding a second human-facing confirmation gate on top of the existing
  data-level `'confirmed'`-candidate requirement). Every destructive path
  shares one rule: `ctx.elicit()` returning `"accept"` is necessary but not
  sufficient — the echoed confirmation data must also exactly match the
  real target, or the operation is rejected exactly as if declined.
  Tested via a minimal duck-typed fake `Context` (only `elicit()` is ever
  called) returning real SDK `AcceptedElicitation`/`DeclinedElicitation`/
  `CancelledElicitation` instances — no full server/client round trip
  needed to test the confirmation logic itself.
- `athena/mcp_server/server.py`/`__main__.py` — `build_server()` wires all
  four tool modules' `register(mcp)` functions onto one `MCPServer`
  instance; `main()` configures logging (already stderr-safe) and calls
  `mcp.run(transport="stdio")`.

## A real finding caught only by genuine integration testing, not unit tests

Building a real `tests/mcp_server/test_server_integration.py` (a real
`ClientSession` connected to the real server via the SDK's in-memory
transport — no stdio process needed) surfaced something the unit tests,
which called tool functions directly as plain Python functions, could not:
**an MCP tool that *raises* an exception has its message discarded before
reaching the client.** `Tool.run()` wraps any raised exception as
`UnexpectedToolError("Error executing tool <name>")`, and the protocol
handler (`_handle_call_tool`, read directly from SDK source to confirm)
returns `str()` of *that wrapper* — not the original exception's message —
in the `CallToolResult` sent back over the wire. `note_read`'s first draft
raised `ValueError(f"cannot read vault note at {path!r}: {exc}")`; a real
client received only `"Error executing tool note_read"`, with the actually
useful part silently gone.

Fixed by changing `note_read`/`_read_note` to *return* the clear string
instead of raising — matching the convention every other tool in this
server had already independently landed on (job_tools/write_tools/
mutation_tools all return plain strings for expected error conditions).
This is now the server's one load-bearing rule for any future tool: never
raise for an expected/user-facing condition, always return a string.

Also confirmed, by reading `MCPServer._handle_call_tool`'s source directly:
an *unexpected* (genuinely buggy) exception is still safely caught and
converted to `CallToolResult(is_error=True)` — never a raw traceback over
the wire — so the design doc's own failure-mode claim about this holds,
just not in the way originally assumed (the message content, not the
crash-safety, was the actual gap).

## Quality gates

- `pytest`: 413/413 passing (57 new this session), 5 correctly
  `skip`-marked (unchanged from before this phase) pending Docker/Qdrant
  access
- `mypy --strict` across all of `src/`: clean
- `ruff check`: clean across the whole repo
- Live verification, not just unit tests: built the real server
  (`build_server()`) and confirmed all 17 tools + 1 resource template
  registered with the exact annotations the design specified
  (`read_only_hint`/`destructive_hint` correctly `True`/`False`/`None`
  per tool); called `system_diagnostics`/`note_read`/`vault_status`
  through the server's real `call_tool` dispatch against a real migrated
  SQLite database with an actually-ingested note; ran a full
  `ClientSession` round trip over the SDK's in-memory transport
  (`initialize` → `list_tools` → `call_tool`), confirming the real
  JSON-RPC-level response shape, not just a wrapped function's return
  value.

## What remains (see `NEXT_SESSION.md` for full detail)

- `note_summarize`, `note_history`/`git_status`/`git_log`/`git_commit`,
  `research_start`/`research_commit` — deferred, blocked on Phase 9/8/7.
- Secret-scanning `note_create`/`note_update` content before writing — a
  real, named gap (design doc §6/§8), not solved in this pass.
- A full wire-level elicitation round-trip test (a real client actually
  responding to a live confirmation prompt) — the logic and the protocol
  dispatch are each fully tested separately, not combined into one test.
- Everything already carried forward from Phases 3-5 (Docker/Qdrant access,
  Phase 4's zero-results degradation gap, untuned duplicate-detection
  thresholds) is unaffected by this session and still open.
- This session's work has not been committed to git — awaiting explicit
  user go-ahead, per standing practice established in every prior phase.
