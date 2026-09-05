# Design: Unified MCP Server (Phase 6)

## 0. Research performed before this design

ADR-0007 (accepted 2026-08-24) already decided the tool contract this design
implements. Per this project's standing practice of re-verifying any
library/API choice at implementation time rather than trusting older
research indefinitely (every prior phase found *something* had drifted),
the MCP Python SDK specifically was re-checked now, eleven days later —
and it had.

**The official `mcp` package is now at 2.1.1** (ADR-0007's research cited
v2.0.0). Installed and verified directly against the real package (not
just documentation) rather than assumed:

- **The server class is `MCPServer`** (`from mcp.server import MCPServer`)
  — ADR-0007's research used generic "FastMCP-style" language; the actual
  v2 rewrite renamed the bundled class away from `FastMCP` entirely (that
  name now belongs to an unrelated third-party package, `fastmcp`, from
  PrefectHQ — a larger, separately-versioned framework with composition/
  proxying/OpenAPI features ATHENA AI-BRAIN doesn't need. This design uses
  the official `mcp` package only, consistent with ADR-0003's "no heavy
  frameworks" bias — confirmed by checking `pip show mcp`, not by
  assuming the two packages are interchangeable).
- **`mcp` ships a `py.typed` marker** — no `mypy` override needed (unlike
  `huey`/`datasketch`), confirmed by checking the installed package
  directly.
- **Elicitation (MRTR confirmation) is `await ctx.elicit(message: str,
  schema: type[BaseModel])`**, returning an `ElicitationResult` whose
  `.action` is `"accept"`/`"decline"`/`"cancel"` and whose `.data` (only
  populated on `"accept"`) is a validated instance of the schema class.
  Verified directly against the installed `Context.elicit` signature.
  **The schema must be flat/primitive fields only** (`str`/`int`/`float`/
  `bool`/`Literal`) — a nested model raises `TypeError` before reaching the
  client, confirmed in the SDK's own documentation. This shapes every
  MRTR confirmation schema in §2 below: each is a single flat model with
  one or two primitive fields (e.g. an exact-path echo), never a nested
  structure.
- **A tool handler receives a `Context` by type annotation, not parameter
  name** (`ctx: Context` or any other name — the SDK inspects the
  annotation), confirmed against the installed `mcp.server.mcpserver.
  Context` class. `async def` tool handlers are awaited directly by the
  SDK's own event loop; `mcp.run(transport="stdio")` (the default) blocks
  and runs that loop itself — so tool implementations call `await` on
  ATHENA AI-BRAIN's existing async repository/business-logic layer
  directly, never through `asyncio.run()` (which cannot nest inside an
  already-running loop, unlike every CLI `_cmd_*`/`worker.run_*` function
  today, which specifically needs `asyncio.run()` because argparse/Huey's
  own entry points are synchronous).
- **`mcp.tool()` accepts a `ToolAnnotations` object** with exactly the
  fields ADR-0007's research already named (`read_only_hint`,
  `destructive_hint`, `idempotent_hint`, `open_world_hint`), confirmed by
  inspecting the installed `mcp.types.ToolAnnotations` model directly.
  ADR-0007's own point stands unchanged by this version bump: these remain
  informational only, never the actual enforcement mechanism.
- **The `io.modelcontextprotocol/tasks` extension is still not implemented
  by the Python SDK** as of 2.1.1 (confirmed via the SDK's own GitHub
  releases and the extension's own tracking issue) — ADR-0007's interim
  `job_status`/`job_cancel` shim decision stands unchanged.
- **A new, concrete, empirically-verified finding not in ADR-0007's
  original research**: `mcp.run()`'s stdio transport uses stdout as the
  wire — any stray `print()` or library write to stdout during a tool call
  would corrupt the JSON-RPC message stream. Verified this is *not* a
  problem for ATHENA AI-BRAIN's existing code by directly testing it, not
  assuming it: `athena.logging_setup.configure_logging` already defaults
  to `sys.stderr` (confirmed by reading the module — no change needed);
  and a live test monkey-patching `sys.stdout` to raise on any non-empty
  write, then calling `athena.indexing.embedding.embed_dense()` for the
  first time (which triggers a HuggingFace Hub download, an
  unauthenticated-request warning, and a `tqdm` progress bar — exactly the
  kind of third-party output most likely to leak to stdout), produced zero
  stdout writes. This is now a standing constraint documented in §5/§6, not
  a one-time check: any *future* dependency added to a code path the MCP
  server calls must be checked the same way before being trusted.

**A second, more significant finding: three tool families in ADR-0007's
table depend on infrastructure that doesn't exist yet**, discovered by
checking the actual codebase rather than assuming the ADR's table was
immediately buildable in full:

- `note_summarize` needs ADR-0003's "small Protocol-based multi-provider
  LLM adapter" — not built; it's Phase 9's own explicit scope
  ("Multi-LLM: provider abstraction, cloud providers, local providers").
- `note_history`, `git_status`, `git_log`, `git_commit` need ADR-0005's Git
  automation module — not built; it's Phase 8's own explicit scope
  ("Git Automation: safe commit workflow, push policy... conflict
  detection"). Confirmed by searching `src/athena` for any Git-wrapping
  code: none exists.
- `research_start`, `research_commit` need actual research-workflow logic
  (web fetching, source extraction, Markdown generation) — not built; it's
  Phase 7's own explicit scope. Only a thin `research_jobs` tracking table
  exists today (job bookkeeping, no workflow).

Per CLAUDE.md rule 20 ("do not begin implementation merely because a
component is obvious") and the phase-discipline article, this design does
**not** stub these out with fake/partial implementations to hit ADR-0007's
full table in one pass. §1 makes the resulting scope cut explicit.

## 1. Purpose & Scope

Implements ADR-0007's MCP tool contract, **except** the three deferred
families named in §0 (`note_summarize`; `note_history`/`git_status`/
`git_log`/`git_commit`; `research_start`/`research_commit`) — each is
listed in §8 as blocked on a specific future phase, not silently dropped.
Every other row in ADR-0007's table ships in this design, each as a thin
wrapper over an existing (Phases 1-5) or newly-added internal
business-logic function, never containing business logic itself
(CLAUDE.md rule 15).

**In scope:**
- The unified server (`athena.mcp_server`, `python -m athena.mcp_server`,
  resolving the placeholder `deployment/bubblewrap/athena-mcp-launch.sh`
  has referenced since Phase 1).
- The `vault://{path}` resource plus `note_read` (model-driven fallback).
- `vault_search`, `note_related`, `note_duplicates`, `duplicates_scan`,
  `note_provenance`, `vault_status`, `system_diagnostics`.
- `job_status`/`job_cancel` (the interim tasks-shim ADR-0007 already
  decided on).
- `note_create`, `note_update`, `note_link`, `note_move`, `note_delete`
  (destructive, MRTR), `note_merge` (destructive, MRTR).
- `reindex_start` (task-backed).
- Structured content envelopes and the other named defense-in-depth
  mitigations for the retrieved-content/instruction-conflation risk
  ADR-0007 already flagged as unsolved at the protocol level.

**Out of scope, deferred (see §8):** `note_summarize` (Phase 9),
`note_history`/`git_status`/`git_log`/`git_commit` (Phase 8),
`research_start`/`research_commit` (Phase 7). Also out of scope: OAuth/
remote-transport auth (`MCPServer`'s `auth_server_provider`/`token_verifier`
params) — the only transport this design uses is stdio, per the existing
bubblewrap-sandboxed local-process deployment model; no network-exposed
transport is being stood up.

## 2. Responsibilities

Each tool is a thin wrapper. Where the wrapped function already exists
(Phases 1-5), it's named without repeating its own design; where it's new,
its shape is given here and its signature in §3.

### 2.1 Read-only tools

- **`vault://{path}` (Resource)** and **`note_read`**: both resolve
  `path` via `athena.safety.paths.resolve_vault_path(..., PathMode.
  EXISTING)` (never trusting the SDK's own `resource_security` path-
  traversal guard as a substitute for ATHENA AI-BRAIN's established vault
  boundary — that guard covers the SDK's own URI-template routing, not
  ATHENA AI-BRAIN's filesystem semantics), then return the note's parsed
  body/frontmatter via the existing `athena.safety.content.
  parse_note_safely`.
- **`vault_search`**: wraps `athena.retrieval.search.search()` directly —
  no new logic, this tool is the entire reason Phase 4 built that
  function's signature the way it did.
- **`note_related`**: wraps `athena.intelligence.related.find_related()`
  directly.
- **`note_duplicates`**: candidates similar to *one* given note — reuses
  `athena.intelligence.duplicates.scan_for_duplicates(..., note_ids=
  [note_id])`, which already supports exactly this narrowing (Phase 5's
  own design: "`note_ids` narrows which notes are scanned *from*"), then
  filters the returned candidates to ones involving `note_id`. Not a new
  business-logic function — a query-shape choice at the tool layer.
- **`duplicates_scan`** (task-backed): dispatched to Huey, wrapping the
  same `scan_for_duplicates` across the whole vault. Returns a job handle
  immediately (§2.3); the underlying work happens via a new
  `duplicates_scan_task` in `athena.worker`, mirroring `index_note_task`'s
  existing `@huey.task` pattern.
- **`note_provenance`**: new, thin repository read —
  `athena.db.repository.provenance.get_activities_for_note(conn, note_id)
  -> list[ProvenanceRow]`, a straightforward `SELECT * FROM provenance
  WHERE note_id = ?` alongside the existing `insert_activity`/
  `insert_source`/`get_lineage` in the same module. Returns the note's own
  PROV activity history (what produced it, human-edited, source URLs) —
  distinct from `get_lineage`, which answers the supersession-graph
  question instead.
- **`vault_status`**: new, thin aggregation —
  `athena.db.repository.notes` already has `list_ids_needing_index`
  (index freshness) and a plain `COUNT(*)` gives total/active note counts;
  `athena.db.repository.research_jobs` needs a new
  `count_by_status(conn) -> dict[str, int]` for queue depth. No new
  business logic beyond composing three existing/near-existing queries
  into one `VaultStatus` dataclass — this tool intentionally does not
  duplicate `system_diagnostics`' health-check role (§2.2's next item);
  it answers "what's the state of my knowledge," not "is the
  infrastructure healthy."
- **`system_diagnostics`**: wraps `athena.diagnostics.run_doctor()`
  directly — built in Phase 1, exercised by the CLI's `doctor` command
  ever since, unchanged here.

### 2.2 The interim tasks shim

`job_status`/`job_cancel` mirror the vocabulary ADR-0007 chose to match
the eventual official `io.modelcontextprotocol/tasks` extension
(`working`/`input_required`/`completed`/`failed`/`cancelled`) against
Huey's actual job semantics, using the `research_jobs` table (already
tracking `status IN ('queued','running','succeeded','failed',
'cancelled')`, already linked to `huey_task_id`) as the source of truth
rather than querying Huey's own storage directly — the DB row is
queryable independent of whether the worker process is even running,
which a raw Huey result lookup is not guaranteed to be.

- **`job_status(job_id)`**: new
  `athena.db.repository.research_jobs.get_by_id(conn, job_id) ->
  ResearchJobRow | None`, mapped to the tasks-extension vocabulary
  (`queued`/`running` → `working`, `succeeded` → `completed`,
  `failed` → `failed`, `cancelled` → `cancelled`; ATHENA AI-BRAIN has no
  `input_required` state today, since none of its task-backed operations
  pause for mid-job input).
- **`job_cancel(job_id)`**: looks up the row's `huey_task_id`, calls
  `huey.revoke_by_id(huey_task_id)` (confirmed present on the installed
  `SqliteHuey` instance), then marks the `research_jobs` row `'cancelled'`
  via a new `athena.db.repository.research_jobs.mark_cancelled`. Revoking
  a task already running does not interrupt it mid-execution (Huey's own
  documented behavior — revocation prevents a *future* execution, it is
  not a kill signal) — the tool's response says so explicitly rather than
  implying an immediate stop.

### 2.3 Mutating, non-destructive tools

- **`note_create`**: the first MCP-driven tool to write a *new* file into
  the vault. Resolves the target path via `resolve_vault_path(...,
  PathMode.CREATE)`, fails outright (no overwrite) if it already exists,
  writes the file, then calls the existing
  `athena.vault.lifecycle.create_note()` to record it — the same
  write-then-record ordering `athena.intelligence.merge.merge_notes`
  already established in Phase 5 (content write is the fallible step,
  done first; if it fails, nothing in the DB changes).
- **`note_update`**: supports `dry_run` (returns a diff without applying,
  per the reference-server precedent ADR-0007 already cited) and two
  modes — patch (append/replace a named section, non-destructive) or full
  overwrite (destructive, requires the same MRTR confirmation as
  `note_delete`, §2.4, since a full overwrite can silently discard
  content exactly as a delete would). Writes via `resolve_vault_path(...,
  PathMode.EXISTING)`, then `athena.vault.lifecycle.
  update_note_content()`.
- **`note_link`**: ADR-0007's own description — "a thin wrapper over
  `note_update`'s patch path" — implemented as exactly that, not a
  separate code path.
- **`note_move`**: collapses move+rename (ADR-0007's decision, direct
  reference-server precedent). Requires MRTR confirmation only if the
  destination path already exists (§2.4) — a non-existent destination
  proceeds without confirmation, per ADR-0007's own "the gate is
  conditional, not blanket" language (already written into
  `TESTING_STRATEGY.md`'s MCP contract-test expectations, quoted almost
  verbatim there). Resolves the source via `PathMode.EXISTING` and the
  destination via `PathMode.CREATE`, moves the file, then
  `athena.vault.lifecycle.move_note()`.
- **`reindex_start`** (task-backed): dispatches to a new
  `reindex_task` in `athena.worker`, wrapping the existing
  `athena.indexing.index_note.index_bootstrap()` (or a single-note
  `index_note()` call when a specific note is named) — no new indexing
  logic, Phase 3 already built all of it.

### 2.4 Destructive tools (MRTR-gated)

Both tools below share one structural rule, independent of ADR-0007's own
requirement: **the business-logic function they wrap is never called
before `ctx.elicit()` returns `action == "accept"` with the expected
confirmation data.** This is a single-call rejection, not a best-effort
check — mirrors `athena.intelligence.merge.merge_notes`'s own "only
reachable from a `'confirmed'` candidate" gate from Phase 5, applied here
at the MCP tool layer instead of the repository layer.

- **`note_delete`**: elicits with a flat schema requiring the caller to
  re-type the exact vault-relative path being deleted
  (`ConfirmDeleteNote(confirm_path: str)`); rejects if
  `result.data.confirm_path != note.path` even on `"accept"` (a client
  that fabricates or guesses an acceptance without actually surfacing the
  prompt to a human gains nothing — the path must match). On confirmed
  match: deletes the vault file (via `resolve_vault_path(...,
  PathMode.EXISTING)`, `Path.unlink()`) *and* calls the existing
  `athena.vault.lifecycle.delete_note()` soft-delete tombstone — unlike
  Phase 5's merge (which deliberately leaves the absorbed file on disk,
  since the operation's own purpose is "these are the same note, keep
  one"), an explicit user-facing delete request is expected to actually
  remove the file; the DB tombstone still exists afterward purely for
  provenance/history continuity (CLAUDE.md rule 24), recoverable via Git
  history exactly as `DATA_MODEL.md`'s own soft-delete rationale already
  argues.
- **`note_merge`**: only reachable after a `'confirmed'` `duplicate_
  candidates` row exists for the pair (Phase 5's own gate, unchanged) —
  elicits with `ConfirmMergeNotes(confirm_keep_path: str)` as an
  *additional* confirmation layer at the MCP boundary specifically
  (TESTING_STRATEGY.md's already-written expectation: "`note_merge` is
  unreachable without prior `note_duplicates`/`duplicates_scan`
  context" — the elicitation adds a second, human-facing gate on top of
  that data-level one, since a model could otherwise call `note_merge`
  immediately after silently calling `note_duplicates`/`resolve_
  duplicate` itself in the same turn with no human ever having seen a
  prompt). On confirmed match: calls `athena.intelligence.merge.
  merge_notes()` directly — no new merge logic, Phase 5 already built all
  of it.
- **`note_update`'s full-overwrite mode** shares this same elicitation
  gate (`ConfirmOverwriteNote(confirm_path: str)`), for the reason given
  in §2.3.

## 3. Interfaces

```python
# athena/mcp_server/__main__.py (or athena/mcp_server.py if the whole
# server fits in one module — decided at implementation time based on
# actual size, not pre-guessed here)
def build_server(config: AthenaConfig) -> MCPServer: ...
def main() -> None: ...  # mcp.run(transport="stdio")

# athena/db/repository/provenance.py (extension)
@dataclass(frozen=True)
class ProvenanceRow:
    id: int
    note_id: int
    activity_type: str
    provider: str | None
    model: str | None
    human_edited: bool
    supersedes_note_id: int | None
    occurred_at: str

async def get_activities_for_note(
    conn: aiosqlite.Connection, note_id: int
) -> list[ProvenanceRow]: ...

# athena/db/repository/research_jobs.py (extension)
@dataclass(frozen=True)
class ResearchJobRow:
    id: int
    huey_task_id: str
    job_type: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None

async def get_by_id(conn: aiosqlite.Connection, job_id: int) -> ResearchJobRow | None: ...
async def mark_cancelled(conn: aiosqlite.Connection, job_id: int) -> None: ...
async def count_by_status(conn: aiosqlite.Connection) -> dict[str, int]: ...

# athena/mcp_server/vault_status.py (new, small)
@dataclass(frozen=True)
class VaultStatus:
    total_notes: int
    notes_needing_index: int
    jobs_by_status: dict[str, int]
    last_reconcile_at: str | None

async def get_vault_status(conn: aiosqlite.Connection) -> VaultStatus: ...

# athena/worker.py (extensions, same shape as existing *_task functions)
@huey.task()
def duplicates_scan_task(correlation_id: str) -> None: ...

@huey.task()
def reindex_task(note_id: int | None, correlation_id: str) -> None: ...
```

The rest of the server's own code (tool functions themselves) is
intentionally not pre-specified signature-by-signature here — each is a
thin `@mcp.tool()`-decorated wrapper whose body is 3-10 lines calling one
of the functions above or an already-existing Phase 1-5 function, and
writing out every one in this design doc would just be restating ADR-0007's
table a second time.

## 4. Dependencies

One new dependency: `mcp>=2.1.1` (§0). No other new third-party libraries.

## 5. Failure Modes

| Scenario | Mechanism | Result |
|---|---|---|
| A tool call raises an unhandled exception | The SDK's own error handling converts it to a tool-call error response | Never a raw traceback leaked to the client as if it were tool output; every tool's own internal `try`/`except` still applies where Phases 1-5 already established graceful-degradation behavior (e.g. Qdrant unreachable → `vault_search` degrades exactly as `athena.retrieval.search` already does) |
| Any dependency invoked during a tool call writes to stdout | Confirmed empirically not to happen today (§0) for the highest-risk case (first-time model loading); no code in this design calls `print()` | Would corrupt the stdio JSON-RPC stream if it ever did — flagged as a standing constraint on any future dependency addition, not something this design can guarantee forever |
| `ctx.elicit()` returns `"decline"` or `"cancel"` for `note_delete`/`note_merge`/overwrite-mode `note_update` | The tool returns a clear "not confirmed, nothing changed" result | The wrapped business-logic function is never called — no partial mutation |
| `ctx.elicit()` returns `"accept"` but the echoed path/data doesn't match | Explicit equality check after `"accept"`, independent of the SDK's own accept/decline signal | Rejected as if declined — a model fabricating an acceptance response gains nothing without correct data |
| `job_cancel` is called on an already-completed or already-running job | `huey.revoke_by_id` on a completed job is a no-op; on a running job it prevents future re-execution but does not interrupt it | The tool's response states the actual effect (not interrupted if already running), never implies an immediate stop that didn't happen |
| `duplicates_scan`/`reindex_start` dispatched while Qdrant is unreachable | Same degradation each already has (Phase 5's three-signal fallback; Phase 3's clean-failure-with-`index_state='failed'`) | No new failure mode introduced at the MCP layer — the task-backed tool just returns a job handle immediately either way; the job's own eventual status (`job_status`) reflects the real outcome |

## 6. Security Considerations

**What this closes.** This is the first code path where a model-driven
client can create, modify, move, or delete vault content — the exact
threat surface `SECURITY_MODEL.md` and ADR-0007 both named as needing
layered, application-level defense since no protocol-level solution
exists. This design's layers, concretely:

1. **Server-side path validation independent of model intent** — every
   tool resolves paths through `athena.safety.paths.resolve_vault_path`,
   the same boundary Phases 1-5 already enforce everywhere else; the MCP
   layer gets no separate, weaker path-handling code of its own.
2. **MRTR confirmation with data verification, not just an accept signal**
   — §2.4's exact-path-echo check after `"accept"` is the concrete
   mechanism satisfying ADR-0007's "confirmation is the real backstop,
   not annotations" rationale.
3. **Tool annotations are set accurately** (`destructive_hint=True` on
   `note_delete`/`note_merge`, `read_only_hint=True` on every §2.1 tool)
   as a client-side display aid, explicitly not relied on for anything —
   consistent with ADR-0007's own documented position that these are
   informational only.
4. **Structured content envelopes**: `note_read`/`vault://{path}` return
   note body and metadata as separate, clearly-labeled fields, with a
   server-authored (never note-authored) note in the tool/resource
   description stating that body content is retrieved data, not
   instructions — a structural signal per ADR-0007's own §7, not an
   enforced guarantee (none exists at the protocol level, confirmed
   again in §0).
5. **No new secret-scanning gap**: `note_create`/`note_update` write
   content a model supplies directly (not vault-derived), so Phase 2's
   pre-ingestion secret scan (which runs on *ingestion* of vault-resident
   files, i.e. after something is already on disk) doesn't automatically
   cover this path. Flagged honestly as a residual gap in §8, not silently
   assumed covered by existing infrastructure that wasn't built for this
   entry point.

**Residual risk — stated honestly, not claimed solved:**
- **The retrieved-content/instruction-conflation risk remains
  categorically unsolved** at the protocol level (§0, ADR-0007 §3/§7) —
  this design's mitigations are the same defense-in-depth posture ADR-0007
  already named, not a new solution.
- **`note_create`/`note_update` content is not secret-scanned before being
  written** (see point 5 above) — a real gap, flagged for a follow-up
  design decision (run `athena.security.secrets.scan_note_for_secrets`
  synchronously in the tool handler before writing, most likely — not
  built in this pass since it changes those tools' latency/failure-mode
  profile and deserves its own explicit review rather than being bundled
  in here).
- **stdio transport has no MCP-level auth** — this is deliberate (§1's
  scope cut) since the process is already sandboxed by bubblewrap
  (Phase 1) and launched locally, not network-exposed; a future
  network transport (`sse`/`streamable-http`) would need the
  `auth_server_provider`/`token_verifier` machinery this design
  explicitly doesn't touch.

## 7. Test Strategy

Extends `TESTING_STRATEGY.md`'s MCP Server / Tool Contract section, which
already specifies most of this design's expected behavior in advance
(written back in Phase 0, now finally exercisable):

- **Structural**: every registered tool is backed by a business-logic
  function independently callable/testable with zero MCP context —
  enumerate the server's registered tools and assert each one's
  implementation is a thin call into an already-unit-tested function, not
  inline logic.
- **Contract/behavior** (already written, now implemented against): a
  `note_delete` call without a prior confirming elicitation round-trip
  never deletes anything (simulate the client returning `"decline"`);
  `note_merge` is unreachable without a prior `'confirmed'` candidate;
  `note_move` targeting an existing destination requires confirmation, a
  non-existent destination does not; `note_create` targeting an existing
  path fails without overwriting (byte-identical original content
  after); `note_update`'s patch mode never silently falls through to full
  overwrite; `dry_run=true` produces zero observable side effects on
  every dry-run-capable tool; task-backed tools return a job handle
  before the underlying work necessarily completes; `job_status`/
  `job_cancel` correctly mirror real `research_jobs` state; a structural
  test enumerating the full registered tool list asserts no Git
  force-push/hard-reset/branch-delete tool exists (trivially true right
  now since no Git tools exist at all yet — the assertion still earns its
  place once Phase 8 adds `git_status`/`git_log`/`git_commit`, so it's
  written now rather than deferred).
- **Security**: a `note_delete` request whose path echo doesn't exactly
  match the target is rejected; a planted injection-style string in a
  fixture vault note is exercised end-to-end through
  `note_read`/`vault_search`, asserting it never alters ATHENA AI-BRAIN's
  own tool-call behavior; every mutating/destructive tool has a negative
  test attempting to skip its required confirmation; read-only tools
  never expose secrets/connection strings in their output.
- **Stdout-safety regression guard**: the exact monkeypatch-`sys.stdout`
  technique used in §0's research, turned into a standing test that runs
  against every tool handler that touches embedding/Qdrant/HuggingFace
  Hub code paths — not just checked once during design.
- **Integration, needs a real MCP client or the SDK's own test client**:
  a full tool-call round-trip (list tools → call `vault_search` → call
  `note_read` on a result) against a real fixture vault + migrated
  SQLite + embedded Qdrant, asserting the actual JSON-RPC-level response
  shape, not just the wrapped function's return value.

## 8. Open Items Carried Forward

- **`note_summarize`** — blocked on Phase 9's multi-LLM provider adapter;
  not built, not stubbed.
- **`note_history`, `git_status`, `git_log`, `git_commit`** — blocked on
  Phase 8's Git automation module; not built, not stubbed.
- **`research_start`, `research_commit`** — blocked on Phase 7's research-
  workflow logic; not built, not stubbed.
- **Secret-scanning `note_create`/`note_update` content before writing**
  (§6) — a real, named gap, not solved in this pass.
- **OAuth/remote-transport auth** — untouched; stdio-only for now (§1).
- **`note_summarize`'s eventual opt-in/configuration gate question**
  (ADR-0007's own still-open question) carries forward unchanged, now
  additionally blocked on Phase 9 existing at all.
- **Whether the optional heuristic injection-pattern scanning ADR-0007's
  research named (§7 point 4 there) should be built now or stays
  deferred** — this design does not build it (matches ADR-0007's own
  "not decided" framing), flagged again rather than silently dropped.
- **Live MCP integration testing** may hit the same Docker/Qdrant
  environment blocker every prior phase has — any test needing a live
  Qdrant server for a full `vault_search` round-trip will need the same
  `skip`-marking discipline, not a silent omission.

## Sources Cited

- [mcp — PyPI](https://pypi.org/project/mcp/)
- [modelcontextprotocol/python-sdk — GitHub](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Python SDK docs — Elicitation](https://py.sdk.modelcontextprotocol.io/handlers/elicitation/)
- [MCP Python SDK docs — Context](https://py.sdk.modelcontextprotocol.io/handlers/context/)
- [MCP Python SDK docs — Running your server](https://py.sdk.modelcontextprotocol.io/run/)
- [SEP-2663: Tasks Extension](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2663)
- Direct inspection of the installed `mcp` 2.1.1 package (`MCPServer.__init__`, `Context.elicit`, `mcp.tool`/`mcp.resource`/`mcp.run` signatures, `ToolAnnotations` fields, `py.typed` marker) and of the installed `huey` package (`revoke_by_id`), in this repository's own `.venv`.
