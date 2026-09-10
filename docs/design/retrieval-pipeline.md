# Design: Retrieval Pipeline (Hybrid Search, Fusion, Reranking, Context)

- **Date:** 2026-09-02
- **Author:** Claude Code (ATHENA AI-BRAIN Phase 4)
- **Status:** Design — implements ADR-0003 (RAG orchestration), ADR-0008 (reranker choice); realizes `docs/ROADMAP.md` Phase 4 ("Retrieval") deliverables: hybrid retrieval, filters, ranking, reranking, context builder, retrieval evaluation suite. Resolves `docs/SECURITY_MODEL.md` P1 item 10 (FTS5 query-syntax injection), left open since Phase 2/3.
- **Depends on / informs:** `docs/design/indexing-pipeline.md` (this design's direct predecessor — reads what that one wrote); `docs/DATA_MODEL.md` §2.8/§2.9/§3/§5; `docs/TESTING_STRATEGY.md`'s retrieval-evaluation-corpus section; `docs/SECURITY_MODEL.md` TB-7

## 0. Research performed before this design

Two technical areas were verified against current primary sources and, in one case, against real installed-package behavior rather than documentation alone:

**Qdrant hybrid dense+sparse query API** (`qdrant-client` 1.19.0, verified directly against the installed package plus live tests against embedded Qdrant). The correct current call is `client.query_points(collection_name=..., prefetch=[...], query=models.FusionQuery(fusion=models.Fusion.RRF), limit=..., with_payload=True)`, where `prefetch` holds one `models.Prefetch` per named vector (`"dense"`, `"minicoil"`). Qdrant's own current documentation states RRF is "the safe default" absent an evaluation set or trusted score priors — directly true for ATHENA AI-BRAIN, which has no retrieval-evaluation corpus yet (this design builds a starter one, §2.6) — so **RRF, not DBSF**, is used for the within-Qdrant dense+sparse fusion, matching ADR-0003's own ordering. **A real bug was found empirically, not assumed**: in embedded (`:memory:`) mode, a filter placed only on the outer `query_points(query_filter=...)` was silently ignored, while the identical filter on each `Prefetch.filter` correctly applied — consistent with the already-known local-mode parity bug ADR-0006 cites (qdrant-client#713). Whether real server mode has the same gap could not be verified in this environment (no live server reachable, per the indexing design's own §0/§8 finding, still unresolved). **Resolution: always set the filter on every `Prefetch`, never rely on the outer `query_filter` alone** — cheap, and removes dependence on undocumented, version-sensitive behavior either way.

**SQLite FTS5 query-string safety**, resolving `SECURITY_MODEL.md` P1 item 10, verified empirically against real SQLite (not just documentation prose, which turned out to give a subtly wrong impression on one point). Confirmed: wrapping a term in `"..."` forces literal-string interpretation; an embedded `"` is escaped by doubling it (`""`). **A documentation-derived assumption was tested and found wrong, then corrected**: two quoted strings joined by whitespace (`"quick" "brown"`) do *not* concatenate into an adjacency-required phrase — they behave as implicit AND, identical to unquoted `quick brown`. Phrase-adjacency requires either the `+` operator or multiple words inside *one* quoted string. The verified-safe construction is: split untrusted input into words, double any embedded `"` in each word, wrap each word individually in `"..."`, join with spaces — confirmed by testing that quoting the literal word `"OR"` returns it as a literal search term rather than invoking the `OR` operator. Also confirmed: SQL parameter binding (`WHERE chunks_fts MATCH ?`) prevents SQL injection as usual but does **nothing** to prevent FTS5 grammar injection, since FTS5 parses its own grammar from the bound string's contents — the quoting step is required in addition to, not instead of, parameter binding.

**Reranker revision pinning**, resolved directly (same process as Phase 3's BGE-M3 pin): `BAAI/bge-reranker-v2-m3`'s current HEAD commit `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`, cross-checked via `huggingface_hub.HfApi().model_info(...)` and the raw HTTP API directly. Confirmed against the installed `sentence-transformers` 6.0.1 source that `CrossEncoder.__init__` accepts `revision=`; also found a real, small API-name drift versus older documentation the earlier embeddings research cited: the constructor parameter is `activation_fn`, not `default_activation_function` (renamed at some point pre-6.0). `CrossEncoder.rank(query, documents, top_k=...)` returns `list[{"corpus_id": int, "score": float, "text": str}]`, sorted, and is the interface this design uses rather than the lower-level `.predict()`.

## 1. Purpose & Scope

This design covers everything from "a note's chunks are indexed" (Phase 3's endpoint) to "a ranked, reranked, cited context is ready to hand to an LLM." Concretely:

1. **Keyword search** (`athena.retrieval.keyword_search`) — safe FTS5 querying against `chunks_fts` and `notes_fts`, resolving the query-injection gap Phase 2/3 both explicitly deferred.
2. **Vector search** (`athena.retrieval.vector_search`) — Qdrant hybrid dense+sparse query via `query_points`/`Prefetch`/`FusionQuery`.
3. **Cross-store fusion** (`athena.retrieval.fusion`) — a hand-written Reciprocal Rank Fusion (RRF) combining three rank lists (Qdrant hybrid, `chunks_fts`, `notes_fts`) into one chunk ranking.
4. **Reranking** (`athena.retrieval.reranking`) — `BAAI/bge-reranker-v2-m3` via `CrossEncoder.rank()`, revision-pinned.
5. **Context builder** (`athena.retrieval.context`) — assembles a token-budgeted, cited context string from the final ranked chunks.
6. **Filters** — tags/folder/status, applied as real SQL predicates against `notes` for the keyword legs and as `Prefetch.filter` for the vector leg — never pushed into FTS5 `MATCH` syntax.
7. **The orchestrator** (`athena.retrieval.search`) — the single business-logic entry point (`search(...)`) a future Phase 6 `vault_search` MCP tool will call, per CLAUDE.md rule 15's transport-decoupling requirement.
8. **A starter retrieval-evaluation corpus and harness** (`athena.retrieval.evaluation`) — per `TESTING_STRATEGY.md`'s spec (Recall@K/Precision@K/MRR/nDCG@10, latency), built at a smaller starting scale than that document's 30-60 note target (§2.6 explains why and flags the gap explicitly, not silently).

**Explicitly NOT in scope** (later phases, per `docs/ROADMAP.md`'s own boundaries):

- Duplicate detection, `note_related` "semantic neighbor" queries as a distinct feature, MinHash/near-duplicate signals — Phase 5. This design's vector/fusion primitives are reusable there later, but Phase 5 owns wiring them into that feature.
- The `vault_search`/`note_related` MCP tools themselves — Phase 6. This design produces the callable business logic; it does not touch MCP.
- Any LLM call using the built context (summarization, synthesis, chat) — that's `note_summarize`/`research_start`, Phase 7/9, and out of scope here; this design stops at "context string is ready."
- Query expansion, spell-correction, or any query-rewriting step — not requested by any accepted document; keeping scope to what ADR-0003/ROADMAP actually call for.

## 2. Responsibilities

### 2.1 Keyword search (`athena.retrieval.keyword_search`)

- `sanitize_fts5_query(raw: str) -> str` — the verified-safe construction from §0: split on whitespace, double embedded `"` per word, wrap each word in `"..."`, join with spaces. Empty input after stripping returns an empty string (callers must treat this as "no keyword leg," not query FTS5 with an empty `MATCH`, which raises).
- `search_chunks(conn, query, *, tags=None, folder=None, status=None, limit=50) -> list[KeywordHit]` — queries `chunks_fts MATCH ?` with the sanitized query, ranked by `bm25(chunks_fts)` (lower is better, per DATA_MODEL.md §2.9's own documented convention — this function returns results already sorted best-first, inverting that convention internally so callers never have to remember it). Tag/folder/status filters are applied via a `JOIN notes ON notes.id = chunks.note_id WHERE notes.deleted_at IS NULL AND ...` — ordinary parameterized SQL predicates on real columns, never FTS5 syntax. `KeywordHit` carries `chunk_id`, `note_id`, `rank` (1-based position in this result list, not the raw bm25 score — see §2.3 for why fusion only ever uses rank).
- `search_notes(conn, query, *, tags=None, folder=None, status=None, limit=50) -> list[NoteTitleHit]` — the same pattern against `notes_fts` (title/tag matches). `NoteTitleHit` carries `note_id` and `rank`; it has no `chunk_id` of its own (title matches aren't chunk-scoped) — §2.3 resolves this by mapping each hit to that note's first chunk (`chunk_index = 0`) as a representative proxy for fusion purposes only. A note with zero indexed chunks (not yet indexed, or `index_state != 'current'`) is excluded from this mapping, not crashed on.

### 2.2 Vector search (`athena.retrieval.vector_search`)

- `search(client, query_text, *, tags=None, folder=None, status=None, limit=50) -> list[VectorHit]`:
  1. Embed `query_text` via `athena.indexing.embedding.embed_dense`/`embed_sparse` (reused unchanged — a query is embedded exactly like a chunk, no separate query-encoder exists for BGE-M3/miniCOIL).
  2. Build one `models.Filter` from `tags`/`folder`/`status` (matching `DATA_MODEL.md` §3's payload field names) and set it on **every** `Prefetch`, per §0's empirically-found local-mode gap — never only on the outer query.
  3. `client.query_points(collection_name=COLLECTION_ALIAS, prefetch=[Prefetch(query=dense_vec, using="dense", limit=limit, filter=f), Prefetch(query=SparseVector(...), using="minicoil", limit=limit, filter=f)], query=FusionQuery(fusion=Fusion.RRF), query_filter=f, limit=limit, with_payload=True)`.
  4. Map `result.points` to `VectorHit(note_id, chunk_id=payload.get("chunk_id"), qdrant_point_id=str(point.id), rank)` — `payload["chunk_id"]` is `None` for any point upserted before this design existed (Phase 3's `upsert_chunks` deliberately excluded it, per that design's own documented trade-off); such hits are still usable for note-level context but cannot be joined back to a specific `chunks` row. Flagged in §8, not silently patched over here.

### 2.3 Cross-store fusion (`athena.retrieval.fusion`)

- A hand-written Reciprocal Rank Fusion, not `ranx` (ADR-0003 left this choice open) — the formula is a few lines (`score(d) = Σ 1/(k + rank_i(d))` across whichever input lists contain `d`, `k=60`, the constant from the original RRF paper and Qdrant's own default), and adding a dependency for it would contradict "small composable modules" for no real benefit.
- `fuse(vector_hits, chunk_keyword_hits, note_title_hits, *, k=60) -> list[FusedResult]`:
  - Resolves every hit to a `chunk_id` first: `vector_hits`/`chunk_keyword_hits` already have one; each `note_title_hits` entry is mapped to that note's `chunk_index=0` chunk (a small repository lookup, `chunks.get_first_chunk_id_for_note`, added for this purpose — §3).
  - Computes the RRF score per distinct `chunk_id` by summing `1/(k + rank)` across every list that contains it (a chunk appearing in two or three lists scores higher than one appearing in only one — RRF's whole point).
  - Returns `FusedResult(chunk_id, note_id, score)`, sorted descending by score.
- This is a three-way fusion (vector, chunk-keyword, note-title), directly implementing `DATA_MODEL.md` §5's already-accepted description of `vault_search`: "`notes_fts` + `chunks_fts` (keyword leg), fused with the Qdrant vector leg." No new architectural decision is being made here — this design specifies the previously-unspecified mechanics of an already-accepted three-way relationship.

### 2.4 Reranking (`athena.retrieval.reranking`)

- One process-lifetime `CrossEncoder("BAAI/bge-reranker-v2-m3", revision=_RERANKER_REVISION, activation_fn=torch.nn.Sigmoid())` singleton, lazily constructed on first use — same pattern as `athena.indexing.embedding`'s dense/sparse model singletons.
- `rerank(query_text, candidates: list[RerankCandidate], *, top_k=10) -> list[RerankedResult]` where `RerankCandidate` carries `chunk_id`/`chunk_text` (read from the `chunks` table for the fused top-N, not re-derived from disk — the whole point of retaining chunk text independent of vectors, per ADR-0008's own stated rationale). Calls `CrossEncoder.rank(query_text, [c.chunk_text for c in candidates], top_k=top_k)` and maps `corpus_id` (a list index) back to the original `chunk_id`.
- Reranks only the fusion's top-N (default 20, configurable) — never the full candidate pool — since a cross-encoder is quadratically more expensive per pair than the bi-encoder retrieval that already narrowed the field; this is the standard two-stage retrieve-then-rerank shape, not a novel design choice.

### 2.5 Context builder (`athena.retrieval.context`)

- `build_context(conn, reranked: list[RerankedResult], *, max_tokens=4096) -> ContextResult` where `ContextResult` carries the assembled `text` (each chunk prefixed with a `[Source: {note_path}]` citation line, per Master Spec's provenance requirements applied to retrieval output) and the list of `note_id`s actually included (for a future caller to attach as `research_commit`/similar provenance, not this design's concern to consume).
- Token budgeting uses each chunk's own stored `token_count` (from `chunks.token_count`, populated by Phase 3's chunker) as a cheap proxy — greedily includes chunks in reranked order until the budget is exhausted, never truncates a chunk mid-text (skips a chunk that would exceed the remaining budget and tries the next one, rather than cutting it off, since a truncated chunk's meaning can be worse than omitting it).

### 2.6 Retrieval evaluation suite (`athena.retrieval.evaluation`)

`TESTING_STRATEGY.md` specifies a hand-curated 30-60 note corpus drawn from real-vault-shaped content, 2-5 questions per note, hand-labeled relevance judgments, tracking Recall@K/Precision@K (K=3,5,10)/MRR/nDCG@10 and p50/p95 latency, re-run in CI on every change touching chunking/embedding/fusion/reranking.

**This design builds the harness and metrics computation in full, but ships a smaller starter corpus (10-12 notes) than that 30-60 target — an explicit, flagged scope reduction, not a silent shortfall.** Building 30-60 realistic, hand-labeled question/answer pairs is a substantial manual-curation effort disproportionate to what a single implementation pass should absorb alongside the actual retrieval code; the harness itself (fixture format, metric functions, CI wiring shape) is the load-bearing deliverable, and it is designed to scale to the full corpus size with zero code changes — only more fixture files. The starter corpus reuses the three real content shapes from `DATA_MODEL.md` §0 (ChatGPT-style, Qwen-style, OWASP-style) as its basis, consistent with every other test fixture already built across Phases 1-3.

- `Question` / `RelevanceJudgment` fixture dataclasses, stored as versioned JSON/YAML files under `tests/retrieval/fixtures/eval_corpus/` (per `TESTING_STRATEGY.md`'s "store as versioned fixtures in Git, not generated at runtime").
- `run_evaluation(corpus, search_fn) -> EvaluationReport` computing Recall@K/Precision@K (K=3,5,10), MRR, nDCG@10 (where graded judgments exist) and latency percentiles.
- Exposed as `athena retrieval evaluate` (CLI) rather than only a pytest fixture, so it can be run standalone against a real vault/Qdrant setup, not only the synthetic starter corpus — the design doc's own test strategy (§7) distinguishes "does the harness compute correct numbers against a fixed synthetic input" (pure unit test, no Qdrant needed) from "does the pipeline actually retrieve well against a real corpus" (needs a live Qdrant server, currently blocked in this environment, same as Phase 3).

## 3. Interfaces

```python
# athena/retrieval/keyword_search.py
def sanitize_fts5_query(raw: str) -> str: ...

@dataclass(frozen=True)
class KeywordHit:
    chunk_id: int
    note_id: int
    rank: int  # 1-based

@dataclass(frozen=True)
class NoteTitleHit:
    note_id: int
    rank: int

async def search_chunks(
    conn: aiosqlite.Connection, query: str, *,
    tags: list[str] | None = None, folder: str | None = None, status: str | None = None,
    limit: int = 50,
) -> list[KeywordHit]: ...

async def search_notes(
    conn: aiosqlite.Connection, query: str, *,
    tags: list[str] | None = None, folder: str | None = None, status: str | None = None,
    limit: int = 50,
) -> list[NoteTitleHit]: ...

# athena/retrieval/vector_search.py
@dataclass(frozen=True)
class VectorHit:
    chunk_id: int | None  # None if the point predates chunk_id being in the payload (§2.2, §8)
    note_id: int
    qdrant_point_id: str
    rank: int

def search(
    client: QdrantClient, query_text: str, *,
    tags: list[str] | None = None, folder: str | None = None, status: str | None = None,
    limit: int = 50,
) -> list[VectorHit]: ...

# athena/retrieval/fusion.py
@dataclass(frozen=True)
class FusedResult:
    chunk_id: int
    note_id: int
    score: float

async def fuse(
    conn: aiosqlite.Connection,
    vector_hits: list[VectorHit], chunk_keyword_hits: list[KeywordHit],
    note_title_hits: list[NoteTitleHit], *, k: int = 60,
) -> list[FusedResult]: ...

# athena/retrieval/reranking.py
@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: int
    chunk_text: str

@dataclass(frozen=True)
class RerankedResult:
    chunk_id: int
    score: float

def rerank(query_text: str, candidates: list[RerankCandidate], *, top_k: int = 10) -> list[RerankedResult]: ...

# athena/retrieval/context.py
@dataclass(frozen=True)
class ContextResult:
    text: str
    note_ids: list[int]

async def build_context(
    conn: aiosqlite.Connection, reranked: list[RerankedResult], *, max_tokens: int = 4096,
) -> ContextResult: ...

# athena/retrieval/search.py -- the orchestrator
async def search(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, query_text: str, *,
    tags: list[str] | None = None, folder: str | None = None, status: str | None = None,
    fusion_pool_size: int = 50, rerank_pool_size: int = 20, top_k: int = 10,
    max_context_tokens: int = 4096,
) -> ContextResult:
    """vector_search + keyword_search (chunks + notes) -> fuse -> rerank top-N -> build_context.
    No MCP dependency (CLAUDE.md rule 15) -- a future Phase 6 vault_search tool calls this directly."""
```

## 4. Dependencies

No new packages: `sentence-transformers` (already installed, `CrossEncoder`), `qdrant-client` (already installed), stdlib `sqlite3`/`aiosqlite` FTS5 access (already used). This phase is pure composition over what Phases 1-3 already installed and verified — consistent with the project's consistent "don't install ahead of need" posture finally paying off here (no reranker/fusion-specific dependency was pulled in during Phase 3, exactly because it wasn't needed until now).

## 5. Failure Modes

| Scenario | Mechanism | Result |
|---|---|---|
| Empty or whitespace-only query text | `sanitize_fts5_query` returns `""` | `search_chunks`/`search_notes` skip the FTS5 query entirely (return `[]`) rather than passing an empty `MATCH` string, which SQLite's FTS5 rejects with a syntax error |
| Query text containing only FTS5 operator words (e.g. `"OR NOT AND"`) | Per-word quoting (§0/§2.1) | Treated as three literal search terms, not operators — returns notes/chunks that literally contain those words, never a syntax error or unintended boolean logic |
| Qdrant unreachable at query time | `vector_search.search` raises | The orchestrator (`search()`) catches this specifically and falls back to keyword-only fusion (two-way RRF: `chunks_fts` + `notes_fts`, no vector leg) rather than failing the whole request — degraded-but-functional search beats no search, mirroring `TESTING_STRATEGY.md`'s own stated expectation ("Qdrant unreachable at query time returns a clear degraded-service response... optionally FTS5-only fallback... never an unhandled exception") |
| A `VectorHit` has `chunk_id=None` (pre-Phase-4 Qdrant point, §2.2) | `fuse()` skips it from the chunk-level fusion (cannot join to a `chunks` row) but logs a count at WARN | Does not crash; slightly undercounts old points until they're naturally re-indexed (`ingest_note` → `index_note` on next content change) and get a real `chunk_id`-bearing point |
| A note matched via `notes_fts` has zero chunks (never indexed, or `index_state != 'current'`) | `search_notes`'s chunk-mapping step (§2.1) finds no `chunk_index=0` row | That hit is dropped from the note-title leg, not mapped to a nonexistent chunk or crashed on |
| Reranking a candidate whose `chunks.chunk_text` was hard-deleted between fusion and reranking (a genuinely rare race — `note_delete` doesn't exist as an MCP tool yet, Phase 6) | `reranking.rerank`'s candidate-loading step | Missing candidates are silently excluded from the reranked list rather than raising — the fused ranking already tolerates gaps, since RRF doesn't assume every input list is complete |
| `max_tokens` in `build_context` is smaller than even the single best-ranked chunk | Greedy inclusion loop (§2.5) | Returns an empty-text `ContextResult` with an empty `note_ids` list rather than truncating the chunk — an empty context is a legitimate, honest signal to the caller; a truncated one could silently mislead |

## 6. Security Considerations

**What this closes.** `SECURITY_MODEL.md` P1 item 10 (FTS5 query-syntax injection, TB-7) is resolved for the first time with a verified-safe, tested quoting mechanism — not just a documented principle. This closes the gap for both genuinely untrusted input (a future MCP client's search query, once Phase 6 exists) and the "note-derived text used in internal automated queries" case the threat model specifically named (this design's `search_notes`/`search_chunks` are the only FTS5 `MATCH` call sites in the codebase after this change — a future structural/grep-based CI check, mirroring the one `docs/design/vault-safety-boundary.md` §3.3 already established for path safety, could enforce that no other module ever builds a `MATCH` string without going through `sanitize_fts5_query` first; flagged as a good follow-up, not built in this pass).

**Residual risk — stated honestly:**

- **Chunk text is embedded in the assembled context verbatim, including any residual content an upstream secret-scan pass might have missed** (defense-in-depth, not a new gap this design introduces — the same trust boundary Phase 2/3's pre-ingestion scanning already targets).
- **The local-mode Qdrant filter bug found in §0 was not confirmed against a real server** — this design's mitigation (filter on every `Prefetch`) is a defensive posture adopted specifically *because* the real-server behavior couldn't be verified in this environment, not because the bug is confirmed present there too. Flagged for re-verification once Docker access is resolved (§8).
- **Reranking doesn't re-apply the tags/folder/status filter** — it operates purely on the fused candidate set, which was already filtered at the vector/keyword stage. If a filter's SQL/Qdrant-side application ever has a bug, reranking would not catch a filter-bypassed result; this is an accepted trust relationship (rerank trusts fusion, fusion trusts its inputs), not a defense-in-depth gap this design chooses to duplicate.
- **No rate limiting or cost ceiling exists on the reranker or embedding calls** — `SECURITY_MODEL.md` P1 item 8 already names this generally for LLM-provider calls; the local BGE-M3/reranker calls made by this design are not LLM-provider API calls (no external cost), so that item doesn't directly apply, but a pathological query pattern (extremely long input) could still be a local CPU-exhaustion vector — not addressed here, flagged as a possible follow-up once real usage patterns are observed (`do not optimize prematurely`).

## 7. Test Strategy

Extends `TESTING_STRATEGY.md`'s "RAG Pipeline" and "Qdrant Integration" sections' retrieval-facing parts (already specified there, before this design existed).

**FTS5 query safety (`athena.retrieval.keyword_search`) — pure unit, no DB needed for the sanitizer itself:**
- `sanitize_fts5_query("OR NOT AND")` — assert the literal words are searchable as terms (integration-level: apply the sanitized string in a real `MATCH` query against a fixture DB and confirm it doesn't raise and doesn't behave as boolean operators).
- A raw string containing an embedded `"` — assert no `OperationalError: unterminated string`.
- Empty/whitespace-only input — assert `search_chunks`/`search_notes` return `[]` without ever issuing a `MATCH` query.
- Tag/folder/status filters applied via `search_chunks` — assert they're real SQL `WHERE` predicates against `notes` (a structural/code-reading assertion, not just behavioral) and correctly exclude non-matching notes.

**Vector search (`athena.retrieval.vector_search`) — unit against `QdrantClient(":memory:")` (non-fusion-critical shape assertions per ADR-0006) plus integration marked `skip` (real server required, same Docker blocker as Phase 3):**
- Unit: `Prefetch`/`FusionQuery` request construction has a filter set on *every* prefetch, not only the outer query (a structural assertion on the built request object, catching a regression of §0's own finding before it ever reaches a live server).
- Integration (skipped): a filtered hybrid query against a real populated collection excludes non-matching points; `VectorHit.chunk_id` correctly reflects the payload's `chunk_id` when present and `None` when absent (a fixture point upserted without one, simulating a pre-Phase-4 point).

**Cross-store fusion (`athena.retrieval.fusion`) — pure unit, synthetic rank lists, no DB/Qdrant needed (per `TESTING_STRATEGY.md`'s own explicit "RRF module produces the mathematically expected fused ranking against synthetic rank lists"):**
- A chunk appearing at rank 1 in two of three lists outscores one appearing at rank 1 in only one list.
- A chunk appearing in all three lists outscores one appearing in only two, regardless of individual ranks.
- Empty input lists (e.g. Qdrant unreachable, vector list empty) still produce a valid fused ranking from the remaining lists.
- `note_title_hits` correctly map to their note's first chunk; a note with zero chunks is dropped, not crashed on.

**Reranking (`athena.retrieval.reranking`) — real model, no Qdrant/DB needed:**
- An obviously-relevant passage ranks above an obviously-irrelevant one for a given query (the coarse sanity check `TESTING_STRATEGY.md` already specifies — real quality measurement is the evaluation corpus's job).
- `RerankedResult`'s `chunk_id` correctly round-trips through `CrossEncoder.rank()`'s `corpus_id` index mapping (an off-by-one or reordering bug here would silently corrupt every result while still "looking like" it ranked something).
- `revision` is the pinned hash, not `"main"` (the same structural pinning-regression test Phase 3 already established for the embedding model).

**Context builder (`athena.retrieval.context`):**
- Greedy token-budget inclusion stops before exceeding `max_tokens`, using each chunk's real stored `token_count`.
- A `max_tokens` smaller than the first chunk produces an empty `ContextResult`, not a truncated chunk.
- Citations (`[Source: {note_path}]`) are present and correctly attributed per included chunk.

**Orchestrator (`athena.retrieval.search`) — integration, needs real Qdrant for the full path (skipped, Docker blocker) but the degraded-fallback path is fully testable without one:**
- Qdrant raising during `vector_search.search` — assert the orchestrator falls back to keyword-only fusion and still returns a `ContextResult`, never propagating the exception (this is the one orchestrator-level test that *doesn't* need a real server — it needs Qdrant to fail, which an unreachable `:memory:`-adjacent misconfiguration or a mock can simulate without Docker).

**Evaluation harness (`athena.retrieval.evaluation`) — the harness itself is testable without a real vault:**
- Recall@K/Precision@K/MRR/nDCG@10 computed correctly against a synthetic, hand-constructed set of judgments and rankings with known-correct expected metric values (a pure math unit test, no retrieval pipeline involved).
- The starter 10-12 note corpus runs end-to-end against the orchestrator and produces a report (integration, needs real Qdrant — skipped, same blocker).
- `athena retrieval evaluate` CLI command runs the harness and prints a report, exit-coding non-zero on a configurable regression threshold (per `TESTING_STRATEGY.md`'s CI-gating recommendation) — this wiring itself is unit-testable (mock the search function) independent of a live retrieval stack.

## 8. Open Items Carried Forward

- **Confirmed live: in a fully Qdrant-down environment, keyword-only degradation currently returns zero results, not degraded-quality results.** Verified end-to-end against the eval corpus (`athena migrate` → `ingest bootstrap` → `retrieval evaluate`, Qdrant unreachable throughout): all 17 questions hit the documented fallback (§5 row "Qdrant unreachable at query time"), and the report showed `recall@k`/`precision@k`/`mrr`/`ndcg@10`/`unanswerable_top1_false_positive_rate` all `0.000` — not just reduced, literally zero hits surfaced for every question, including the 3 deliberately-unanswerable ones (no false positives either, because nothing came back at all). Root cause is the *composition* of two individually-correct, already-documented mechanisms (§5 rows 3 and 6): `index_note()` only ever writes `chunks` rows after a successful Qdrant upsert, so a fully-down Qdrant means zero notes have any chunks; and `fusion.fuse()` correctly drops any `notes_fts` title hit whose note has no chunk to anchor a `chunk_id` to (`get_first_chunk_id_for_note` returns `None`). Each mechanism alone is sound and already covered by a test — but together, in a scenario where *no* note has ever been successfully indexed, they compose into a 100% miss rate rather than the "degraded-but-functional" search row 184 describes. This is a real gap between the design's stated failure-mode expectation and its verified emergent behavior, not a coding bug in either mechanism — flagged here rather than silently patched, since fixing it (e.g. anchoring note-title hits to a synthetic non-chunk result, or a repository-level content preview instead of requiring a `chunks` row) is itself a design decision this doc didn't make and shouldn't make retroactively. Candidate follow-up for a future phase or a small addendum ADR, not an in-place code change to this accepted design. **This finding is specifically about the Qdrant-never-reachable scenario, confirmed still accurate 2026-09-10**; it does not describe a general pipeline defect — see the next item.
- **RESOLVED 2026-09-10 (Docker access restored that session)**: re-ran the same eval corpus fully indexed against a real, live, pinned-version Qdrant server (`docker run qdrant/qdrant:v1.19.1`, 127.0.0.1-only per ADR-0006). Retrieval genuinely works end-to-end once Qdrant is actually reachable and notes are actually indexed: `recall@3/5/10: 0.357`, `precision@3: 0.119`, `mrr: 0.357`, `ndcg@10: 0.357`, `unanswerable_top1_false_positive_rate: 0.000` — real, non-degenerate, non-zero numbers (not tuned or expected to be high against only a 10-note starter corpus, but genuinely functioning, not the zero-results failure mode above). All 5 previously `skip`-marked Qdrant-integration tests across Phases 3-4 now pass against the real server, including confirming (§0's own flagged unconfirmed item) that the embedded-mode outer-query-filter-only bug does **not** reproduce on a real server — the defensive per-`Prefetch` filter mitigation is kept regardless, unchanged.
- **RESOLVED 2026-09-10**: the real Qdrant server does **not** have the local-mode filter-on-outer-query-only bug found in `:memory:` mode — confirmed by directly querying a real server (`docker run qdrant/qdrant:v1.19.1`, Docker access having been resolved that session) with a filter set only on the outer `query_filter` and no per-`Prefetch` filter: it correctly excluded a non-matching point, unlike embedded mode. This design's "filter on every prefetch" mitigation is kept regardless (defense-in-depth against a future version/mode change), per the original reasoning — it was never conditioned on this bug being confirmed present on a real server, only mitigated defensively either way.
- **`VectorHit.chunk_id=None` for pre-Phase-4 Qdrant points** — not a bug introduced here, but a real consequence of Phase 3's own documented `upsert_chunks` trade-off. Self-heals as notes are naturally re-indexed; no backfill migration is proposed in this design (would require re-deriving payloads for existing points, a separate, narrowly-scoped follow-up if the gap proves to matter in practice).
- **The retrieval-evaluation corpus ships at 10-12 notes, not `TESTING_STRATEGY.md`'s 30-60 target** — explicitly flagged (§2.6), not silently under-delivered. Scaling up is pure fixture-authoring work against an already-built harness, a reasonable follow-up task rather than a design or implementation gap.
- **A structural/grep-based CI check enforcing "only `sanitize_fts5_query`-passed strings ever reach a `MATCH` expression"** was recommended (§6) but not built in this design pass — flagged as a good, cheap follow-up mirroring an existing precedent (`vault-safety-boundary.md`'s own such check for path safety).
- **Local CPU-exhaustion risk from pathological reranker/embedding input length** — noted (§6) but not mitigated; revisit if real usage patterns show it matters, not preemptively.

## Sources Cited

- `docs/DATA_MODEL.md` §2.8, §2.9, §3, §5 — the schema and MCP-tool-mapping this design implements against, unchanged
- `docs/TESTING_STRATEGY.md` — RAG Pipeline / Qdrant Integration sections, the retrieval-evaluation-corpus specification (extended, not duplicated, above)
- `docs/adr/0003-rag-orchestration-approach.md`, `docs/adr/0008-embeddings-model-choice.md`
- `docs/SECURITY_MODEL.md` TB-7, P1 item 10 — directly resolved by this design
- `docs/design/indexing-pipeline.md` — this design's direct predecessor; §2.2's `chunk_id`-omission trade-off is inherited and worked around here, not re-litigated
- Qdrant hybrid queries documentation (`qdrant.tech/documentation/concepts/hybrid-queries/`) and direct inspection of the installed `qdrant-client` 1.19.0 source — checked 2026-09-02
- SQLite FTS5 documentation (`sqlite.org/fts5.html` §3) plus direct empirical verification against a real SQLite instance (the phrase-adjacency correction, §0) — checked 2026-09-02
- `huggingface_hub`/HTTP API resolution of `BAAI/bge-reranker-v2-m3`'s current revision hash, cross-checked two ways — checked 2026-09-02
