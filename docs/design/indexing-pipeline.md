# Design: Indexing Pipeline (Chunking, Embedding, Vector/Keyword Index)

- **Date:** 2026-09-02
- **Author:** Claude Code (ATHENA AI-BRAIN Phase 3)
- **Status:** Design — implements ADR-0003 (RAG orchestration), ADR-0006 (Qdrant deployment), ADR-0008 (embeddings/sparse/reranker model choice); realizes `docs/ROADMAP.md` Phase 3 ("Indexing") deliverables: structure-aware parsing, semantic chunking, embedding abstraction, vector index, full-text/keyword index, metadata index, incremental indexing
- **Depends on / informs:** `docs/DATA_MODEL.md` §2.8 (`chunks` table), §2.9 (FTS5, already migrated), §3 (Qdrant payload schema); `docs/EVENT_MODEL.md` §3.4, §4.1; `docs/design/migration-runner-and-vault-ingestion.md` (this design's direct predecessor — `ingest_note()` stops exactly where this one begins); `docs/SECURITY_MODEL.md` TB-8, TB-10, TB-12, P1 items 11/12/15

## 0. Research performed before this design (Constitution Article 1)

Three technology areas were re-verified against current primary sources rather than relying on Phase 0's research from 2026-08-24, since a library's security posture and API surface can move in ~2 weeks and this design is about to rely on all three for real:

**`chonkie`** — clean bill of health. Current version 1.7.0, actively maintained (org-backed, `chonkie.ai` domain, 39+ contributors, weekly commits), zero CVEs found (OSV.dev + GitHub Advisory DB, checked 2026-09-02). Base install pulls no heavy ML dependencies (`chonkie-core`/`tokie` ship prebuilt wheels for all target platforms — no `.pth`-hook or arbitrary-build-script risk of the kind found in the LiteLLM compromise `SECURITY_MODEL.md` already cites). **Two corrections to carry forward**: (1) the canonical repo is now `github.com/feyninc/chonkie` — PyPI's own metadata still points at `chonkie-inc/chonkie`, which now 301-redirects after a benign org rename; cite the new name in any future document. (2) `RecursiveChunker.from_recipe("markdown")` — the obvious way to get heading-aware chunking — makes a live network call to Hugging Face Hub to fetch recipe JSON at runtime, in tension with ATHENA AI-BRAIN's local-first posture. §2.2 below specifies hand-constructing `RecursiveRules` locally instead. This satisfies `SECURITY_MODEL.md` P1 item 12 ("give chonkie the same research treatment GitPython/Dulwich/LiteLLM received") for the first time.

**Embeddings/reranker/sparse-vector stack** — model IDs and APIs from ADR-0008 are all still current (`BAAI/bge-m3`, 1024-dim, no `trust_remote_code` needed; `BAAI/bge-reranker-v2-m3` via `CrossEncoder`; `Qdrant/minicoil-v1` via `fastembed`'s `SparseTextEmbedding`, current since fastembed 0.7.0). One important **new requirement found, not in ADR-0008**: a Qdrant collection's sparse-vector config for miniCOIL must set `modifier=models.Modifier.IDF` — omitting it silently produces meaningless vectors, not an error. `qdrant-client` v1.19.0's alias/payload-index API matches what ADR-0006/0008 assumed.

**A real, since-resolved CVE was found and directly verified, not assumed.** `sentence-transformers` had a critical (CVSS 9.8) vulnerability, CVE-2026-68770: `import_module_class`'s trust-gate implicitly trusted any local-directory `model_name_or_path`, regardless of `trust_remote_code=False`, allowing RCE via a planted `modeling_*.py` referenced from a model's `modules.json`. **Verified directly against the current GitHub source** (`sentence_transformers/util/misc.py` at tag `v6.0.1`): this bypass was closed in **v6.0.0** (released 2026-08-18, three weeks after the CVE's 2026-07-31 publish date) — PR #3935 removed the local-path trust bypass entirely; the function now raises `ValueError` unless `trust_remote_code=True`, unconditionally. The CVE tracker's "no fixed version" claim is stale. **Resolution: pin `sentence-transformers>=6.0.0` in `pyproject.toml`** — this alone closes the vulnerability class regardless of which specific model is loaded (BGE-M3 itself doesn't need `trust_remote_code` at all, since it uses standard `sentence_transformers.*` module classes, not custom modeling code).

**Environment finding, not a library issue**: this development environment's current user is not a member of the `docker` group (`docker info` returns "permission denied"), and non-interactive `sudo` is unavailable in this session. **Live Qdrant integration testing is blocked** until the user either adds their account to the `docker` group and restarts their session, or runs the relevant integration tests themselves. This does not block drafting this design or writing/unit-testing the code that doesn't require a running Qdrant server — it does block the fusion-critical integration tests ADR-0006/`TESTING_STRATEGY.md` require before this phase can be called verified end-to-end. Flagged explicitly in §8.

## 1. Purpose & Scope

This design covers the second half of the pipeline `docs/design/migration-runner-and-vault-ingestion.md` deliberately split off (§1 of that document): everything from "a note's metadata is recorded" to "a note is semantically searchable." Concretely:

1. **Chunking** (`athena.indexing.chunking`) — structure-aware Markdown splitting via `chonkie`.
2. **Embedding** (`athena.indexing.embedding`) — dense vectors (BGE-M3) and sparse vectors (miniCOIL).
3. **Qdrant store management** (`athena.indexing.qdrant_store`) — collection/alias lifecycle, point upsert/delete, payload indexes.
4. **The indexing job** (`athena.indexing.index_note`) — the idempotent per-note job that ties the above together, chained after `ingest_note()`'s success, resolving the `notes.last_indexed_at`/`index_state` contract Phase 2 left unset.
5. **`notes.index_state`/`last_index_error` schema addition** — deliberately deferred by the Phase 2 design (§1/§8 of that document); resolved here, by the design that actually owns the job which sets them.

**Explicitly NOT in scope** (Phase 4 — "Retrieval," per `docs/ROADMAP.md`'s own phase boundary):

- Query-time hybrid fusion (RRF/DBSF), reranking (`bge-reranker-v2-m3`/`CrossEncoder`), or context construction. The reranker model from ADR-0008 is not installed or used by this design at all — it's a query-time concern, not an indexing-time one. Installing it now would be exactly the "install large dependencies before they're needed" the project has consistently avoided.
- `vault_search`/`note_related`/any MCP tool — Phase 6.
- Duplicate detection (`datasketch` MinHash-LSH, semantic similarity via Qdrant) — Phase 5. This design does not touch `duplicate_candidates`/`note_minhash_signatures`.
- FTS5 *querying* (`MATCH` expressions, string-quoting per `SECURITY_MODEL.md` P1 item 10) — that's a retrieval-time concern; this design only *writes* to `chunks`/`chunks_fts` (already fully wired by Phase 2's migration 0001 triggers) and never issues a `MATCH` query itself.
- The retrieval-evaluation corpus (`TESTING_STRATEGY.md`'s 30-60 note hand-labeled set) — that measures retrieval quality, which doesn't exist until Phase 4.

## 2. Responsibilities

### 2.1 `notes.index_state`/`last_index_error` (migration 0004)

Resolves the Phase 2 design's deferred item. Per `docs/EVENT_MODEL.md` §4.1, orthogonal to the content-lifecycle `status` field (same pattern `secret_scan_status` already established in migration 0003):

```sql
ALTER TABLE notes ADD COLUMN index_state TEXT NOT NULL DEFAULT 'stale'
    CHECK (index_state IN ('stale', 'current', 'failed'));
ALTER TABLE notes ADD COLUMN last_index_error TEXT;
```

`'stale'` (not `'current'`) is the default: a note that has never been indexed is, definitionally, stale — this makes the default meaningful rather than a placeholder, and lets a query for "notes needing indexing" simply be `WHERE index_state != 'current'`, covering both never-indexed and previously-failed notes in one predicate.

**Judgment call flagged for review, not silently made**: unlike ADR-0010 (events table) and ADR-0011 (secret-scan schema), this schema addition is not given its own ADR. Reasoning: `EVENT_MODEL.md` §4.1 — an already-accepted Phase 0 exit-criteria deliverable — already specified this exact column pair, its values, and its rationale in detail; this design doc merely implements an already-reviewed recommendation, the same relationship ADR-0011 had to `docs/design/pre-ingestion-secret-scanning.md` (except that design doc's schema was genuinely new, unreviewed state, which is why *it* got ADR-0011). If this judgment is wrong, the fix is cheap: draft a one-paragraph ADR before merging, not before designing.

### 2.2 Chunking (`athena.indexing.chunking`)

- Wraps `chonkie.RecursiveChunker`, constructed with a **hand-built `RecursiveRules`** for Markdown headings rather than `from_recipe("markdown")` (§0's local-first finding) — verify the exact `RecursiveRules`/`RecursiveLevel` constructor arguments against the installed `chonkie` version during implementation (its dataclass shape is not re-verified in this design pass; the `from_recipe` avoidance is the load-bearing decision, not the exact rule syntax).
- Input is always `parsed.body` from `athena.safety.content.parse_note_safely` — this module never sees raw frontmatter, consistent with Phase 2's parsing already stripping it.
- Chunk size default: 512 tokens, ~50-token overlap — reasonable starting points per `chonkie`'s own defaults being similar order of magnitude; **empirical Phase 3 tuning input, not researched-and-final**, same posture already taken for the debounce window and reconciliation interval in Phase 2.
- **No custom pre-splitting on ATHENA AI-BRAIN's real conversational-turn headers** (`# you asked`, `### USER`) is built in this design: those are ordinary ATX-style Markdown headers, and `RecursiveRules`' heading-boundary logic should treat them as natural split points already. This must be verified with a concrete test against real-shaped fixtures (§7) before trusting it in production — if the empirical test shows chonkie's generic heading detection doesn't split on `###`-level headers the way ATHENA AI-BRAIN needs, a custom pre-split step will need to be added as a follow-up, not assumed away now.
- Interface: `chunk_note(body: str) -> list[Chunk]`, where `Chunk` is a frozen dataclass (`text: str`, `chunk_index: int`, `token_count: int | None`).
- An over-long single "chunk" (chonkie's own splitter should prevent this, but defensively) exceeding BGE-M3's 8192-token limit is truncated, not silently dropped — logged at WARN, per `TESTING_STRATEGY.md`'s explicit test case for this.

### 2.3 Embedding (`athena.indexing.embedding`)

- **Dense**: one process-lifetime `sentence_transformers.SentenceTransformer("BAAI/bge-m3", revision=<pinned commit hash>)` instance, lazily constructed on first use (not at module import time — loading is expensive, and not every process that imports this module needs it, e.g. the CLI's `--help`). `revision` is pinned to a specific HF commit hash, not `"main"` — resolves `SECURITY_MODEL.md` P1 item 15 ("pin embedding/reranker model loads to a specific Hugging Face revision hash rather than a mutable branch") for the first time.
- **Sparse**: one process-lifetime `fastembed.SparseTextEmbedding(model_name="Qdrant/minicoil-v1")` instance, same lazy-construction and revision-pinning posture (fastembed's own model-pinning mechanism — likely a model snapshot hash in its cache metadata — needs verification against the installed version during implementation; the *requirement* to pin is what this design mandates, not a specific API call not yet verified).
- Interface: `embed_dense(texts: list[str]) -> list[list[float]]`, `embed_sparse(texts: list[str]) -> list[SparseVector]` (batched, not one-call-per-chunk, for throughput).
- `embedding_model_version` stored per-chunk (already a `chunks` column, DATA_MODEL.md §2.8) is a string like `"bge-m3@<revision-hash-prefix>"` — allows a rolling re-embed to filter old-vs-new during a future migration cutover, per DATA_MODEL.md §4's own stated rationale for this column.

### 2.4 Qdrant store (`athena.indexing.qdrant_store`)

- **Collection bootstrap** (`ensure_collection(client) -> None`, idempotent, called once at worker/CLI startup): creates the versioned collection (e.g. `athena_chunks_bge_m3_v1`) if absent, with:
  - dense named vector `"dense"`: size 1024, distance Cosine
  - sparse named vector `"minicoil"`: `models.SparseVectorParams(modifier=models.Modifier.IDF)` — the easy-to-miss requirement from §0
  - payload indexes on `tags` (keyword), `folder` (keyword), `status` (keyword), `note_id` (integer), `embedding_model_version` (keyword) — per DATA_MODEL.md §3's already-specified list
  - Then atomically points the alias (`athena_chunks`) at it via `client.update_collection_aliases(change_aliases_operations=[...])` — **a single atomic call, never a separate check-then-create-then-repoint sequence** — resolving `SECURITY_MODEL.md` P1 item 11 (the alias-race finding) for the first time. All application code addresses the collection exclusively through the alias name; the versioned name only appears inside `ensure_collection`.
  - Alias mutation itself is wrapped in a `huey.lock_task("qdrant-alias-mutation")` guard, per the same P1 item's second half — prevents two overlapping bootstrap/migration-cutover attempts from racing.
- **Upsert**: `upsert_chunks(client, note_id, chunks, dense_vectors, sparse_vectors, payload_fields) -> list[str]` — returns the minted `qdrant_point_id` (UUID4) per chunk, per DATA_MODEL.md §2.8's `chunks.qdrant_point_id` contract.
- **Delete**: `delete_points_for_note(client, note_id) -> None` — via the `note_id` payload index (a filtered delete, not point-id enumeration), used both by re-indexing (delete-then-reinsert, simplest correct approach for a note whose chunk count changed) and by `note_delete` in a future Phase 6 MCP tool.
- All functions take an already-constructed `QdrantClient` — this module does not own client lifecycle, matching `athena.db.connection`'s same pattern for SQLite.

### 2.5 The indexing job (`athena.indexing.index_note`)

`index_note(conn, qdrant_client, note_id, *, correlation_id, causation_id) -> IndexResult`:

1. Look up the `notes` row by id. If `deleted_at` is set, this is a stale trigger for a since-deleted note — no-op (a genuine `note_delete` path, once it exists in Phase 6, is responsible for calling `delete_points_for_note` itself; this job never runs against a tombstoned note).
2. **Idempotency check**: compare `chunks` rows' aggregate content (or a simpler per-note stored signal — see Open Questions §9, since `notes.content_hash` already changing was what triggered this job in the first place, re-reading it here would just re-confirm what the caller already knows) — in practice, since this job is only ever triggered right after `ingest_note()` reports `created`/`updated` (§2.6), no additional hash check is needed here; the trigger itself *is* the change signal, avoiding the double-check anti-pattern.
3. Read the note's current body from disk again (via `athena.safety.paths.resolve_vault_path` + `parse_note_safely`, exactly as `ingest_note()` did) — **not** from a cached copy, since chunk text must reflect exactly what was last recorded in `notes.content_hash`. Re-deriving from disk, never diffing, mirrors ADR-0009's core idempotency philosophy applied one layer up.
4. `chunk_note(parsed.body)`.
5. `embed_dense`/`embed_sparse` on the chunk texts (batched).
6. `delete_points_for_note` (clears any prior chunk set for this note — simplest correct handling of a note whose chunk count changed between versions) **then** `upsert_chunks` — Qdrant write happens before the SQLite write, so a crash between them leaves Qdrant slightly ahead, never SQLite falsely marked current (see §5's ordering rationale, mirroring Phase 2's "last write is the commit marker" pattern).
7. Delete-then-reinsert the note's `chunks` rows in SQLite (the FTS5 triggers from migration 0001 sync `chunks_fts` automatically).
8. Update `notes.index_state='current'`, `notes.last_indexed_at=now`, `notes.last_index_error=NULL`, `notes.chunk_count=len(chunks)` — this is the actual commit marker, the last write of the whole job.
9. Append `index.update_completed` (per `docs/EVENT_MODEL.md` §1.4's already-specified payload: `note_id`, `path`, `content_hash`, `chunk_count`, `index_version`, `duration_ms`).

On any exception in steps 3-8: catch, set `notes.index_state='failed'` + `last_index_error=str(exc)` (best-effort — if the failure is itself a SQLite connectivity problem, this write may also fail, in which case Huey's own retry/backoff is the fallback, per `EVENT_MODEL.md` §4.1's already-specified recovery path), append `job.failed`, re-raise so Huey's `@huey.task(retries=...)` retry policy governs.

### 2.6 Chaining after `ingest_note()`

`athena.worker.ingest_note_task` (Phase 2, existing) gains one addition: after `ingest_note()` returns, if `result.outcome in {"created", "updated"}`, enqueue `index_note_task(result.note_id, correlation_id, causation_id=<ingest's completion event id>)` — the same "one code path, multiple triggers, but this trigger is itself downstream of another job's completion" pattern `EVENT_MODEL.md` §3.4 originally described as one fused step; Phase 2 split it into two chained jobs instead of one monolithic one, and this is where the chain reconnects. `ingest.py` itself remains free of any chonkie/Qdrant/sentence-transformers import — the chaining lives in `worker.py`, preserving Phase 2's explicit scope boundary.

Bootstrap and reconcile (`athena.vault.bootstrap`/`reconcile`) get the same treatment: after each `ingest_note()` call reporting `created`/`updated`, call `index_note()` directly (synchronously) — consistent with those modules' existing "call directly, don't queue" pattern from Phase 2, for the same one-shot/low-scale reasoning already documented there.

## 3. Interfaces

```python
# athena/indexing/chunking.py
@dataclass(frozen=True)
class Chunk:
    text: str
    chunk_index: int
    token_count: int | None

def chunk_note(body: str, *, chunk_size: int = 512, chunk_overlap: int = 50) -> list[Chunk]: ...

# athena/indexing/embedding.py
@dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]

EMBEDDING_MODEL_VERSION: str  # e.g. "bge-m3@<revision-prefix>", computed once at module load

def embed_dense(texts: list[str]) -> list[list[float]]: ...
def embed_sparse(texts: list[str]) -> list[SparseVector]: ...

# athena/indexing/qdrant_store.py
COLLECTION_ALIAS: str = "athena_chunks"

def ensure_collection(client: QdrantClient, huey: Huey) -> None: ...
def upsert_chunks(
    client: QdrantClient, *, note_id: int, chunks: list[Chunk],
    dense_vectors: list[list[float]], sparse_vectors: list[SparseVector],
    payload_fields: dict[str, Any],
) -> list[str]: ...  # returns qdrant_point_id per chunk, same order as `chunks`
def delete_points_for_note(client: QdrantClient, note_id: int) -> None: ...

# athena/indexing/index_note.py
@dataclass(frozen=True)
class IndexResult:
    outcome: Literal["indexed", "noop", "failed"]
    note_id: int
    chunk_count: int

async def index_note(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot,
    note_id: int, *, correlation_id: str, causation_id: str | None,
) -> IndexResult: ...
```

## 4. Dependencies

New `pyproject.toml` dependencies, all pinned to at least the version verified in §0:

- `chonkie>=1.7.0` — no extras needed for `RecursiveChunker` (confirmed base-install-only per §0).
- `sentence-transformers>=6.0.0` — **the lower bound is a security requirement, not a preference**; see §0's CVE finding.
- `fastembed` — sparse vectors via ONNX Runtime (no torch overlap with sentence-transformers' stack, per §0 — the two libraries pull genuinely separate heavy runtimes, a real ~3-5GB combined install cost, confirmed and accepted here rather than discovered as a surprise later).
- `qdrant-client>=1.16.0` — the lower bound closes CVE-2026-25628 (arbitrary file write, server-side but the client version gate is the simplest place to enforce a floor); cross-references ADR-0006's own "pin image tag" requirement, which must independently pin the **server** image to ≥1.16.0 as well (a deployment-config change, not a Python dependency — flagged for the docker-compose/run command that doesn't exist yet, see §8).

Reused, unchanged: `athena.safety.paths`, `athena.safety.content` (Phase 1), `athena.db.repository.notes` (extended with `update_index_state`, a new small function analogous to Phase 2's `update_secret_scan_status`).

## 5. Failure Modes

| Scenario | Mechanism | Result |
|---|---|---|
| Qdrant unreachable during upsert | `upsert_chunks` raises | `index_note` catches at the job level, sets `index_state='failed'`, re-raises for Huey retry — SQLite `chunks` rows are never written before the Qdrant upsert succeeds (§2.5 step 6 ordering), so the dual-write consistency risk `TESTING_STRATEGY.md` explicitly calls out (SQLite marked "indexed" while Qdrant upsert failed) cannot occur in this direction |
| Process crash between Qdrant upsert succeeding and the SQLite `chunks`/`notes.index_state` write | Ordering in §2.5 | Qdrant has points for this note; SQLite still shows the old (or no) `chunks` rows and `index_state` never reached `'current'`. The *next* trigger for this note (a fresh ingest, or a future Phase 3 reconciliation-equivalent — see §9) re-runs `index_note` from scratch, which deletes-then-reinserts Qdrant points before touching SQLite again — self-healing by construction, the same pattern Phase 2's `_handle_vanished_path`/`EVENT_MODEL.md` §4.3 already established, applied one layer up |
| A chunk exceeds BGE-M3's 8192-token limit | `chunk_note`'s defensive truncation (§2.2) | Truncated, logged at WARN, indexing continues — never silently dropped, never a job failure |
| `chonkie`'s heading-boundary detection doesn't respect ATHENA AI-BRAIN's real turn-header shapes as well as assumed | Empirical test (§7) | **Flagged as a real risk, not asserted safe** — if the test fails, a custom pre-split step on `# you asked`/`### USER`-style headers before handing text to `chonkie` becomes a required follow-up, not a hypothetical one |
| Two overlapping alias-mutation attempts (e.g. bootstrap running twice concurrently) | `huey.lock_task` guard around `ensure_collection`'s alias-mutating branch | Second attempt fails fast (per Huey's `TaskLock` being fail-fast, verified in Phase 2) rather than racing — consistent with how Phase 2 already uses the identical mechanism for per-path ingestion locking |
| `sentence-transformers`/`fastembed` model download fails (no network, HF Hub down) | Propagates as an exception from `embed_dense`/`embed_sparse` | `index_note` job fails cleanly, retried per Huey policy — no special handling beyond the generic failure path, since this is indistinguishable from any other transient dependency failure |
| Docker/Qdrant genuinely not running in this environment (§0's current blocker) | N/A — this is a deployment-readiness gap, not a code failure mode | `ensure_collection`/every Qdrant call fails with a connection error; `athena doctor` should report this clearly (§6) rather than every indexing job failing with an opaque stack trace |

## 6. CLI / Doctor Additions

- `athena doctor` gains a `qdrant_reachable` check: attempts `QdrantClient(url=...).get_collections()` with a short timeout; `warn` (not `fail`) if unreachable, since Qdrant is a separate deployment concern from ATHENA AI-BRAIN's own process health, mirroring how `bwrap_available`/`docker_available` are already `warn`-level, optional checks rather than hard failures.
- `athena index bootstrap` — a one-time pass indexing every note with `index_state != 'current'`, the Phase 3 counterpart to `athena ingest bootstrap`, needed for the same reason: the initial corpus (already ingested by Phase 2, `index_state='stale'` by migration default) needs an explicit first indexing pass, not just newly-changed notes going forward.
- Qdrant connection config: `ATHENA_QDRANT_URL` (default `http://127.0.0.1:6333`, matching ADR-0006's binding), added to `athena.config`.

## 7. Test Strategy

Extends `TESTING_STRATEGY.md`'s "RAG Pipeline" and "Qdrant Integration" sections (already specified there, before this design existed) rather than duplicating them.

**Chunking (`athena.indexing.chunking`) — pure unit, no Qdrant/Huey/DB:**
- A note using ATHENA AI-BRAIN's real `# you asked`/`### USER` turn-header shapes (fixtures already exist in `tests/safety/test_content.py` and `tests/vault/test_ingest.py` — reuse, don't reinvent) — assert chunk boundaries actually fall on those headers. **This is the empirical test §2.2/§5 flags as not yet proven** — its result determines whether a follow-up custom pre-splitter is needed.
- A code-fence-containing chunk is never split mid-fence.
- Empty/whitespace-only/single-word body — no crash, returns zero or one trivial chunk.
- A synthetically oversized single "paragraph" (no natural split points) exceeding 8192 tokens — assert truncation, not a raised exception or silent drop.

**Embedding (`athena.indexing.embedding`) — unit, model downloads required (mark slow/network-dependent):**
- Encoding the same text twice produces identical vectors (determinism).
- Output dimension is exactly 1024 for dense.
- `EMBEDDING_MODEL_VERSION` string actually contains the pinned revision, not `"main"` or a mutable ref — a structural test guarding against the pinning requirement regressing silently.

**Qdrant store (`athena.indexing.qdrant_store`) — split per ADR-0006's own testing rule:**
- Unit, `QdrantClient(":memory:")` permitted (non-fusion-critical): collection-config construction produces the expected request shape.
- Integration, **requires a real Qdrant server — currently blocked in this environment per §0/§8**: `ensure_collection` is idempotent (calling twice doesn't error or duplicate); the alias actually resolves to the versioned collection; `upsert_chunks`/`delete_points_for_note` round-trip correctly; a payload-index-based delete only removes the target note's points, not others'; sparse vector round-trip with the IDF modifier actually set (query and confirm non-trivial results, not just "upsert didn't error").
- Contract (can run without a live server): a CI check flags any test using `:memory:` while also asserting alias-repoint or sparse-vector-specific behavior — the same contract check `TESTING_STRATEGY.md` already specifies for hybrid fusion, extended to cover this design's own Qdrant-specific behaviors.

**`index_note` job — integration, needs real Qdrant (blocked, see §8) plus a migrated temp SQLite DB (same fixture pattern as `tests/vault/test_ingest.py`):**
- End-to-end: ingest a note, confirm `index_note` produces `chunks` rows, Qdrant points, and `notes.index_state='current'`.
- Crash-recovery simulation: interrupt between Qdrant upsert and the SQLite write (mock the SQLite write to raise), assert re-running `index_note` from scratch converges to consistent state, no duplicate Qdrant points (the delete-then-reinsert in step 6 already guarantees this — the test proves it, doesn't just assert the code looks right).
- A note's `chunks` count shrinking between versions (fewer chunks in v2 than v1) — assert the old extra points/rows are actually gone, not orphaned (proves delete-then-reinsert, not a naive upsert-only approach).
- Failure path: mock `embed_dense` to raise — assert `index_state='failed'`, `last_index_error` populated, `job.failed` emitted, no partial `chunks` rows written.

**Chaining (`athena.worker`):**
- `ingest_note_task`'s `call_local` (Phase 2's own established testing pattern) with a genuinely new note — assert `index_note_task` was also invoked (mock/spy on the enqueue call, since asserting the *queued* task actually ran would require a live consumer).
- A `noop` ingest outcome — assert `index_note_task` is *not* enqueued (idempotency extends across the chain, not just within each job).

## 8. Open Items Carried Forward

- **RESOLVED 2026-09-10**: Docker/Qdrant access was blocked (§0) through Phase 6; resolved that session (the user added their account to the `docker` group). All 4 integration tests in §7 marked "requires a real Qdrant server" now pass against a real, pinned-version, 127.0.0.1-only Qdrant server (`docker run qdrant/qdrant:v1.19.1`, per ADR-0006), no longer `skip`-marked.
- **The Qdrant *server* image tag** (ADR-0006 mandates pinning, never `:latest`) needs to be pinned to ≥1.16.0 in whatever docker-compose/run configuration eventually exists — no such configuration exists yet (ADR-0006 §"Consequences" deferred Compose/systemd authoring to "Phase 1 implementation," which didn't happen; this design doesn't create it either, flagged as a gap for whoever stands up the actual Qdrant instance).
- **`RecursiveRules`' exact constructor shape** must be verified against the installed `chonkie` version at implementation time — this design mandates avoiding `from_recipe()`'s network call but does not pin the exact replacement syntax, consistent with "verify, don't assume" applied to a detail this research pass didn't drill into.
- **`fastembed`'s own model-revision-pinning mechanism** needs verification during implementation — the *requirement* to pin (§2.3, §5's cross-reference to `SECURITY_MODEL.md` P1 #15) is firm; the exact API to do so for a `fastembed` sparse model (as opposed to `sentence-transformers`' well-documented `revision=` kwarg) was not verified in this research pass.
- **Vault language composition** — ADR-0008's own open item ("should Phase 1 include an explicit vault-language-composition measurement step before finalizing miniCOIL vs. BM25 fallback") was never actually done. Still open; this design proceeds with miniCOIL per the accepted ADR, but the fallback should be kept genuinely easy to switch to (the sparse-vector abstraction in §2.3/§3 is deliberately swappable for exactly this reason) rather than treated as foreclosed.
- **Chonkie's actual heading-boundary behavior against ATHENA AI-BRAIN's real turn-header shapes** — asserted as *likely* fine in §2.2 based on general ATX-header reasoning, not yet empirically proven. The test in §7 is the actual verification; until it's run, this remains a stated assumption, not a confirmed fact.

## Sources Cited

- `docs/DATA_MODEL.md` §2.8, §2.9, §3 — the schema this design writes to, unchanged
- `docs/EVENT_MODEL.md` §1.4, §3.4, §4.1 — `index.update_completed` payload, the original (later split) chunk/embed job description, `index_state`/`last_index_error` rationale
- `docs/adr/0003-rag-orchestration-approach.md`, `0006-qdrant-deployment-mode.md`, `0008-embeddings-model-choice.md`
- `docs/SECURITY_MODEL.md` TB-8, TB-10, TB-12, P1 items 11/12/15 — all directly addressed by this design
- `docs/design/migration-runner-and-vault-ingestion.md` — this design's direct predecessor and scope boundary
- [PyPI: chonkie](https://pypi.org/pypi/chonkie/json), [github.com/feyninc/chonkie](https://github.com/feyninc/chonkie) — checked 2026-09-02
- [huggingface.co/BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3), [sbert.net cross-encoder pretrained models](https://sbert.net/docs/cross_encoder/pretrained_models.html), [qdrant.tech fastembed-minicoil docs](https://qdrant.tech/documentation/fastembed/fastembed-minicoil/), [PyPI: qdrant-client](https://pypi.org/project/qdrant-client/), [PyPI: sentence-transformers](https://pypi.org/project/sentence-transformers/) — checked 2026-09-02
- `sentence_transformers/util/misc.py` at tag `v6.0.1` and the `v6.0.0` release notes, both fetched directly from `github.com/UKPLab/sentence-transformers` — checked 2026-09-02, the direct-source verification resolving the CVE-2026-68770 status
