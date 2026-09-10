# Design: Knowledge Intelligence (Duplicate Detection, Merge, Lifecycle, Lineage)

## 0. Research performed before this design

Per CLAUDE.md rule 1 ("research before implementation") and this project's
running practice of re-verifying any library/API choice at implementation
time rather than trusting Phase-0-era research indefinitely (Phase 3 found a
`sentence-transformers` CVE and a `chonkie` repo rename this way; Phase 4
found a Qdrant filter bug and an FTS5 phrase-concatenation error the same
way):

**`datasketch` (MinHash/MinHashLSH for lexical near-duplicate detection —
already named in `DATA_MODEL.md` §2.6's `duplicate_candidates.detection_method`
CHECK constraint and the `note_minhash_signatures` table, both written back
in Phase 0).** Current version is 2.0.0 (released 2026-07-05), actively
maintained, no known CVEs found. **A real, version-specific gotcha that
directly affects this design's schema**: 2.0.0 changed `MinHash`'s default
permutation `scheme` from the legacy scheme to `"affine32"`, and hash values
are *not* comparable across schemes — "MinHash created with different
schemes cannot be compared, merged, or unioned." Since
`note_minhash_signatures.signature` is a persisted `BLOB` meant to survive
process restarts and library upgrades, this design pins `scheme="affine32"`
**explicitly** in every `MinHash(...)` construction, rather than relying on
whatever the installed version's default happens to be — so a future
`datasketch` major-version bump changing the default again can't silently
make every stored signature incomparable with newly-computed ones. Also:
`LeanMinHash.serialize()`/`.deserialize()` (not plain `pickle.dumps(MinHash(...))`)
is the persistence mechanism — a purpose-built, compact, version-stable byte
format, not a general Python pickle (avoids the same class of fragility
`ADR-0002` already rejected pickle for with Huey's serializer).
`MinHashLSH` itself is confirmed in-process/ephemeral (no built-in disk
persistence without an optional Redis/Cassandra backend this project isn't
using) — confirming `note_minhash_signatures`' own schema comment that the
LSH index must be rebuilt from persisted signatures at job start, not
assumed to survive a restart.

**Qdrant "query by existing point ID" (the semantic-similarity leg of
duplicate detection, and the entire mechanism behind "related notes")**:
confirmed via the official API reference (`api.qdrant.tech`) that
`query_points`'s `query` field accepts an existing point ID directly — the
server resolves the stored vector internally — rather than requiring the
client to `retrieve()` the vector first and re-submit it. Excluding the
query point itself from its own results is a `must_not`/`has_id` filter
condition, not a separate parameter:

```python
client.query_points(
    collection_name=COLLECTION_ALIAS,
    query=qdrant_point_id,
    query_filter=models.Filter(
        must_not=[models.HasIdCondition(has_id=[qdrant_point_id])]
    ),
    using="dense",
    limit=limit,
)
```

This avoids an unnecessary round-trip and, more importantly, avoids a
race/staleness class of bug: retrieve-then-resubmit could theoretically
query against a vector that a concurrent re-index has already superseded,
where querying by ID always resolves against whatever is currently stored.

**Metadata-match similarity (`duplicate_candidates.metadata_match_score`)**:
no new dependency needed. Python's standard-library `difflib.SequenceMatcher`
gives an adequate normalized path/title similarity ratio for this signal —
it is explicitly one signal among four (content hash, MinHash-LSH, cosine
similarity, metadata match), not the sole or authoritative one, so a
fuzzy-matching library's extra precision (e.g. `rapidfuzz`) isn't
justified against `CLAUDE.md`'s "prefer small, composable modules" and "do
not add dependencies you don't need" guidance. `rapidfuzz` is noted for a
future revisit if the metadata-match signal proves too weak against real
duplicate-review results.

## 1. Purpose & Scope

Implements `docs/ROADMAP.md` Phase 5 (Knowledge Intelligence):
duplicate detection, a merge engine, provenance/lineage query surfaces,
related-notes suggestions, confidence/status lifecycle rules, and stale-
knowledge workflows. Builds entirely on schema and infrastructure already
in place: `duplicate_candidates`/`note_minhash_signatures`
(`DATA_MODEL.md` §2.6, migration 0001), `provenance`/`provenance_sources`/
`provenance_derivations`/`note_lifecycle_history` (§2.4-2.5, migration
0001, already populated by Phase 2's `ingest_note`/lifecycle service), the
`research_jobs.job_type` enum's pre-existing `'duplicates_scan'` and
`'stale_sweep'` members (migration 0001 — Phase 0 already anticipated
these jobs existing), Phase 3's embeddings/Qdrant collection, and Phase 4's
retrieval infrastructure.

**Explicitly out of scope for this design** (per Master Spec §10's "merge
policies must be explicit and testable" and §12's git-safety
requirements, and matching the established pattern of deferring the MCP
surface):
- **No MCP tools.** `note_duplicates`/`note_merge`/`duplicates_scan` as
  MCP tool contracts are ADR-0007/Phase 6's job. This design builds the
  underlying engine plus a CLI surface (`athena duplicates scan`,
  `athena duplicates list`, `athena duplicates merge`,
  `athena lifecycle stale-sweep`), mirroring exactly how Phase 3/4 built
  `ai index bootstrap`/`athena retrieval evaluate` ahead of any MCP tool
  existing to call them.
- **No automatic merging.** Master Spec §10 is explicit: "a high similarity
  score must not automatically imply that two notes are semantically
  interchangeable." Every merge in this design requires an explicit,
  separate confirming action (a second CLI invocation naming the specific
  candidate) — never a side effect of the scan itself. This also satisfies
  CLAUDE.md rule 22 ("never execute destructive filesystem or Git
  operations without explicit user intent") since a merge rewrites vault
  content.
- **No automatic status promotion.** See §2.4 — this design proposes an
  explicit, narrow set of machine-triggerable transitions and leaves
  everything else manual, rather than inventing a full promotion policy
  Master Spec §11 explicitly deferred ("exact states and transition rules
  will be designed later" — this design treats itself as only a partial
  answer to that, flagged in §8).
- **Git commit/backup workflow** — Phase 8's job; a merge changes vault
  files and the database, but does not itself commit to Git.

## 2. Responsibilities

### 2.1 Duplicate detection (`athena.intelligence.duplicates`)

`scan_for_duplicates(conn, qdrant_client, *, note_ids=None) -> list[DuplicateCandidate]`
— computes all four signals for the given notes (or every non-deleted note
if `note_ids` is omitted) against every other note, and upserts
`duplicate_candidates` rows:

- **Exact** (`content_hash`): a direct `GROUP BY content_hash HAVING
  COUNT(*) > 1` query against `notes` — cheapest signal, checked first;
  an exact hash match still goes through the same `duplicate_candidates`
  row (`detection_method='content_hash'`, `combined_score=1.0`) rather than
  a special-cased short-circuit, so the review/merge UI has one uniform
  surface regardless of which signal(s) fired.
- **Lexical** (`minhash_lsh`): `LeanMinHash(scheme="affine32", num_perm=128)`
  built from a shingled (word-3-gram) representation of each note's plain
  text (frontmatter stripped), persisted to `note_minhash_signatures`;
  `MinHashLSH(threshold=0.5, num_perm=128)` rebuilt in-process from all
  persisted signatures at job start, queried per note to find lexical
  candidates, Jaccard estimate stored as `lexical_score`.
- **Semantic** (`cosine_similarity`): for each note's first chunk's Qdrant
  point (mirroring `fusion.py`'s existing "first chunk as representative
  proxy" pattern from Phase 4, since duplicate detection operates at
  note granularity, not chunk granularity), query by point ID (§0) with
  `score_threshold=0.85`, self-excluded; `semantic_score` is the returned
  cosine score. **Notes with no chunks (never successfully indexed) are
  skipped for this signal only** — the other three signals still run —
  logged at INFO, not silently dropped from the scan entirely, distinct
  from Phase 4's harsher §8 finding since here it degrades one signal out
  of four rather than the whole feature.
- **Metadata** (`metadata_match_score`): `difflib.SequenceMatcher` ratio
  over normalized (lowercased, extension-stripped) filename/title pairs.
- **Combined score**: `combined_score = max(lexical_score or 0,
  semantic_score or 0)` when either fired, further boosted to `1.0` on an
  exact content-hash match — metadata match alone never promotes a pair to
  `duplicate_candidates` (too weak/noisy per §0), it only annotates
  candidates the other three signals already surfaced. A pair is inserted
  into `duplicate_candidates` (`status='pending'`) only if `combined_score`
  clears a scan-wide threshold (default `0.5`, configurable), keeping
  `detection_method='combined'` when more than one signal fired.
- Wired as a Huey job (`research_jobs.job_type='duplicates_scan'`, the
  enum value Phase 0 already reserved) — `run_duplicates_scan()` in
  `athena.worker`, following the same `run_bootstrap`/`run_index_bootstrap`
  pattern.

### 2.2 Review and merge (`athena.intelligence.merge`)

`list_pending_duplicates(conn) -> list[DuplicateCandidate]` and
`resolve_duplicate(conn, candidate_id, *, resolution, resolved_by,
resolution_note=None) -> None` where `resolution` is `'confirmed'`,
`'rejected'`, or `'merged'` — writes `duplicate_candidates.status`/
`resolved_at`/`resolved_by`/`resolution_note`.

`merge_notes(conn, vault_root, *, keep_note_id, absorb_note_id,
merged_by) -> MergeResult` — the actual merge operation, only reachable
after a candidate has been explicitly marked `'confirmed'` first (never
directly from `'pending'` — a two-step confirmation gate, matching
`TESTING_STRATEGY.md`'s own already-written expectation that `note_merge`
requires prior duplicate-scan context):

1. Reads both notes' current content from the vault.
2. Appends `absorb_note_id`'s content to `keep_note_id`'s file under a
   `## Merged from <absorb path>` heading (preserves both texts rather than
   attempting automatic content fusion — Master Spec §10's "must not
   automatically imply... interchangeable" applies doubly hard to actually
   discarding text) and re-parses/re-ingests `keep_note_id` through the
   existing `ingest_note()`/`index_note()` pipeline so its chunks/embeddings
   reflect the merged content.
3. Tombstones `absorb_note_id` (`notes.deleted_at`, matching the existing
   soft-delete convention — Git already gives content-level recovery per
   `DATA_MODEL.md`'s own reasoning for that convention) and transitions its
   status to `'superseded'` via the existing `transition_status()`.
4. Writes one `provenance` row (`activity_type='merge'`) for `keep_note_id`
   with `supersedes_note_id=absorb_note_id`, and a `provenance_derivations`
   row recording `absorb_note_id` as a source — using the tables Phase 2
   already built for exactly this PROV-modeled case.
5. Marks the originating `duplicate_candidates` row `status='merged'`.
6. Runs inside one SQLite transaction plus the vault file write; if the
   file write fails, the transaction rolls back — no partial state where
   the DB thinks a merge happened but the vault file doesn't reflect it
   (mirrors Phase 3's "zero partial rows on failure" discipline).

CLI: `athena duplicates scan`, `athena duplicates list [--status pending]`,
`athena duplicates resolve <id> --confirm|--reject`,
`athena duplicates merge <id> --keep <note_id>`.

### 2.3 Related notes (`athena.intelligence.related`)

`find_related(conn, qdrant_client, note_id, *, limit=5) ->
list[RelatedNote]` — an on-demand (not persisted, no new table) query
reusing the exact same "query by point ID + self-exclusion" mechanism as
§2.1's semantic-duplicate leg, but at a much lower `score_threshold`
(default `0.5` vs. duplicate detection's `0.85`) since the intent here is
"topically similar," not "possibly the same note." Deliberately not
persisted: unlike duplicate candidates (which need a review workflow and
audit trail), related-notes results are cheap to recompute on demand and
go stale the moment either note's content changes, so persisting them
would just be another cache-invalidation problem for no real benefit.

### 2.4 Lifecycle / confidence transitions (extends `athena.vault.lifecycle`)

Master Spec §11 explicitly defers "exact states and transition rules" —
this design deliberately does **not** attempt to fully resolve that in one
pass. It proposes exactly one new, narrow, machine-triggered transition
rule, and leaves everything else manual (via the already-existing
`transition_status()`, callable from a future MCP tool in Phase 6):

- **Proposed rule**: a note transitions `'draft' -> 'active'` automatically
  the first time `index_note()` succeeds for it (i.e., it has at least one
  real, embedded, searchable chunk) — `'draft'` meaning "known to
  ATHENA AI-BRAIN, not yet semantically searchable" per `ingest.py`'s own
  existing docstring language, which already describes exactly this
  semantic without the transition being wired up yet. This is the single
  most concrete, low-risk interpretation of "draft" already implicit in
  the existing code and comments — not a new policy invented from nothing.
- **Everything else stays manual**: `'active' -> 'verified'` (implies a
  human/agent judgment call this design has no basis to automate),
  `-> 'stale'` (see §2.5 — proposed as semi-automatic, flagged, not
  silently decided), `-> 'archived'` (an explicit user/agent decision).
  `confidence` (the `REAL 0.0-1.0` column) is left entirely
  application/caller-set in this phase — no automatic confidence scoring
  is proposed, since nothing in this design produces a principled
  confidence estimate (a reranker score or duplicate-similarity score is
  not the same thing as "how much do we trust this note's content," and
  conflating them would be inventing unreviewed policy).
- **This is presented for explicit accept/reject as part of this design's
  acceptance**, per CLAUDE.md rule 10 ("do not silently redesign accepted
  architecture") — flagged distinctly in the summary asked of the user,
  not buried in implementation.

### 2.5 Stale-knowledge workflow (`run_stale_sweep` job)

**Proposed policy** (also flagged for explicit accept/reject, same
reasoning as §2.4): a note transitions `'active'`/`'verified' -> 'stale'`
if it has not been updated (`notes.updated_at`) or promoted through
`note_lifecycle_history` in more than a configurable window (default
**180 days**), *and* it is not already `'superseded'`/`'archived'`, *and*
it has no unresolved `duplicate_candidates` row pointing at a newer note
(a note actively in the merge-review queue isn't independently marked
stale — that would just be noise on top of the duplicate-review signal
already covering it). Wired as `research_jobs.job_type='stale_sweep'`
(the enum value Phase 0 already reserved), a periodic Huey job
(`@huey.periodic_task`, matching ADR-0009's existing periodic-job pattern
for reconciliation) plus `athena lifecycle stale-sweep` as an
on-demand CLI equivalent for testing/manual runs. Every transition still
goes through `transition_status()` and writes `note_lifecycle_history`
(`changed_by='job:stale_sweep'`) — no schema bypass, no silent field
mutation.

### 2.6 Lineage query surface (`athena.db.repository.provenance` extensions)

`get_lineage(conn, note_id) -> LineageGraph` — walks `provenance`/
`provenance_sources`/`provenance_derivations` to answer "what did this
note come from" (ancestor chain via `supersedes_note_id` and
`provenance_derivations`) and "what did this note become" (descendant
chain via `superseded_by_note_id`), returning a small typed graph
structure rather than raw rows. Purely additive read-side work over
already-populated tables — no new schema, no new writer logic beyond
what §2.2's merge already writes.

## 3. Interfaces

```python
# athena/intelligence/duplicates.py
@dataclass(frozen=True)
class DuplicateCandidate:
    id: int
    note_a_id: int
    note_b_id: int
    detection_method: str
    lexical_score: float | None
    semantic_score: float | None
    metadata_match_score: float | None
    combined_score: float
    status: str

async def scan_for_duplicates(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    vault_root: Path,
    *,
    note_ids: list[int] | None = None,
    threshold: float = 0.5,
) -> list[DuplicateCandidate]: ...

# athena/intelligence/merge.py
async def list_pending_duplicates(conn: aiosqlite.Connection) -> list[DuplicateCandidate]: ...

async def resolve_duplicate(
    conn: aiosqlite.Connection,
    candidate_id: int,
    *,
    resolution: Literal["confirmed", "rejected"],
    resolved_by: str,
    resolution_note: str | None = None,
) -> None: ...

@dataclass(frozen=True)
class MergeResult:
    kept_note_id: int
    absorbed_note_id: int
    provenance_id: int

async def merge_notes(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    vault_root: Path,
    *,
    keep_note_id: int,
    absorb_note_id: int,
    merged_by: str,
) -> MergeResult: ...

# athena/intelligence/related.py
@dataclass(frozen=True)
class RelatedNote:
    note_id: int
    note_path: str
    score: float

async def find_related(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    note_id: int,
    *,
    limit: int = 5,
    score_threshold: float = 0.5,
) -> list[RelatedNote]: ...

# athena/intelligence/lifecycle.py
async def promote_on_first_index(conn: aiosqlite.Connection, note_id: int) -> None: ...

async def run_stale_sweep(
    conn: aiosqlite.Connection,
    *,
    stale_after_days: int = 180,
) -> StaleSweepSummary: ...

# athena/db/repository/provenance.py (extension)
@dataclass(frozen=True)
class LineageGraph:
    note_id: int
    ancestors: list[LineageEdge]
    descendants: list[LineageEdge]

async def get_lineage(conn: aiosqlite.Connection, note_id: int) -> LineageGraph: ...
```

## 4. Dependencies

One new dependency: `datasketch>=2.0.0` (§0). No other new third-party
libraries — metadata matching uses the standard-library `difflib`;
semantic similarity and related-notes reuse the existing `qdrant-client`
already installed since Phase 3.

## 5. Failure Modes

| Scenario | Mechanism | Result |
|---|---|---|
| A note has no chunks (never indexed) during a duplicate scan | §2.1's semantic-leg skip | That note's semantic signal is skipped (logged INFO); lexical/exact/metadata signals still run for it — not dropped from the scan entirely |
| Qdrant unreachable during a duplicate scan | Same `_vector_search_or_degrade`-style catch as Phase 4's orchestrator | The scan degrades to lexical+exact+metadata only (three signals, not four), logged at WARNING; never crashes the whole scan job |
| `merge_notes` succeeds in the DB but the vault file write fails (disk full, permission error) | One transaction wrapping both, explicit rollback on file-write failure | No partial state: either both the yaml/db and the vault file reflect the merge, or neither does |
| A `duplicate_candidates` row's `note_a_id`/`note_b_id` references a note deleted since the scan ran | `list_pending_duplicates` joins against `notes` and filters `deleted_at IS NULL` on both sides | Stale candidates referencing an already-deleted note are excluded from the review list, not surfaced as mergeable |
| `merge_notes` called on a candidate not yet `'confirmed'` | An explicit status check at the top of `merge_notes`, raising a typed error | Rejected outright — the two-step confirm-then-merge gate can't be bypassed by calling `merge_notes` directly |
| `datasketch`'s `MinHash` default scheme changes again in a future major version | Every `MinHash(...)` call in this codebase pins `scheme="affine32"` explicitly (§0) | A `datasketch` upgrade cannot silently make old and new signatures incomparable; comparing across an intentional scheme change still requires an explicit migration, which is a visible, deliberate step, not a silent corruption |

## 6. Security Considerations

**What this touches.** `merge_notes` is the first code in ATHENA AI-BRAIN
that rewrites vault file *content* as a side effect of automated analysis
(everything before this either reads the vault or writes brand-new files
via the existing lifecycle service's `create_note`/`update_note_content`,
both already vault-safety-boundary-checked). `merge_notes` reuses
`athena.safety.paths`/`athena.vault.lifecycle`'s existing
`update_note_content` rather than writing to the filesystem directly, so
it inherits the same path-traversal/symlink protections `vault-safety-
boundary.md` already established — no new file-write code path is
introduced.

**Residual risk — stated honestly:**
- **No re-scan of merged content for secrets before it's written.** The
  original ingestion of both source notes already ran
  `athena.security.secrets` at ingest time; re-running it on the
  concatenated merge result is not done in this pass, since the merge only
  recombines already-scanned/already-redacted text rather than introducing
  new content. Flagged as a good defense-in-depth addition if a future
  review wants it, not built here (no new information is being
  introduced, so the marginal risk is low).
- **The stale-sweep and first-index-promotion transitions run as
  `changed_by='job:...'`, with no human confirmation step** — this is a
  deliberate, narrow exception to CLAUDE.md rule 22's "explicit user
  intent" requirement, justified because neither transition deletes or
  rewrites content (only a status label), and both are fully reversible
  via the same `transition_status()` call. `merge_notes` (which does
  rewrite content) is not exempted — it always requires the explicit
  two-step confirm-then-merge CLI flow.
- **`difflib.SequenceMatcher`'s metadata-match signal is O(n²) in note
  count with no indexing** — acceptable at the vault's current scale
  (tens to low hundreds of notes) but flagged as a scaling concern before
  a much larger vault, consistent with "do not optimize prematurely;
  measure before optimizing."

## 7. Test Strategy

Extends `TESTING_STRATEGY.md`'s already-written duplicate-detection
expectations (§"RAG Pipeline" already specifies "duplicate detection flags
near-identical notes... and does not flag unrelated ones," using the real
vault's `Grok-_04.md`/`Grok-_04(1).md`-style pairs as a fixture source)
and its MCP-contract section's `note_merge`/`duplicates_scan` behavioral
expectations (reinterpreted here as CLI/engine-level tests, since the MCP
surface itself doesn't exist until Phase 6):

**Duplicate detection — unit, no live Qdrant needed for exact/lexical/metadata:**
- Two notes with identical `content_hash` are flagged `detection_method='content_hash'`, `combined_score=1.0`.
- Two notes differing by one character produce a high `lexical_score` via MinHash-LSH; two unrelated notes do not clear the LSH threshold.
- `MinHash` signatures persist and reload correctly via `LeanMinHash.serialize`/`.deserialize`, round-tripping to the same Jaccard estimate.
- A note with no chunks is skipped for the semantic signal only, confirmed via a structural check that the other three signals still ran (not that the whole scan skipped it).
- `combined_score` thresholding: a pair scoring just below threshold is not inserted into `duplicate_candidates`; just above, it is.

**Duplicate detection — integration, needs real Qdrant (skip-marked, same Docker blocker as every prior phase):**
- Two notes with near-identical but not identical text produce a high `semantic_score` via the query-by-point-ID mechanism, correctly excluding the queried point itself from its own results.

**Merge engine — integration against a real temp vault + migrated SQLite:**
- `merge_notes` on a `'confirmed'` candidate: the kept note's file contains both texts under the `## Merged from` heading; the absorbed note is tombstoned and `'superseded'`; a `provenance`/`provenance_derivations` row is written; the `duplicate_candidates` row is `'merged'`.
- `merge_notes` called on a `'pending'` (not yet confirmed) candidate is rejected.
- A simulated vault-file-write failure mid-merge leaves the database unchanged (transaction rollback verified by re-reading the candidate's status is still `'confirmed'`, not `'merged'`).
- `get_lineage` after a merge correctly reports the absorbed note as an ancestor of the kept note.

**Related notes:**
- `find_related` excludes the queried note itself from its own results (regression test for the exact bug class §0's research call-out exists to prevent).
- A `score_threshold` higher than any actual similarity in a small fixture set returns an empty list, not an error.

**Lifecycle/stale-sweep:**
- A note's first successful `index_note()` call transitions it `'draft' -> 'active'`, recorded in `note_lifecycle_history` with `changed_by='job:index_note'` (or the calling job's identity).
- `run_stale_sweep` flags a note older than the configured window and not superseded/archived/duplicate-pending; does not flag a recently-updated note, an already-`'archived'` note, or a note with a pending duplicate candidate pointing at it.

## 8. Open Items Carried Forward

- **§2.4 and §2.5's proposed transition rules are explicitly presented for
  accept/reject as part of this design's review** — they are this design's
  best concrete interpretation of Master Spec §11's deferred lifecycle
  rules, not a claim that the lifecycle question is now fully closed.
  Rejecting either proposal doesn't block the rest of this design; the
  corresponding job/CLI command would simply not be built this phase.
- **No MCP tools** — `note_duplicates`/`note_merge`/`duplicates_scan` as
  callable MCP tools remain Phase 6's job; this design only builds the
  engine and CLI they'll eventually wrap, matching every prior phase's
  build-ahead-of-MCP pattern.
- **`difflib`-based metadata matching may prove too weak or too noisy in
  practice** — no real vault data exists yet to validate the default
  thresholds (`0.5` scan threshold, `0.85` duplicate semantic threshold,
  `0.5` related-notes threshold) against; flagged for tuning once this
  runs against the real vault, not treated as load-bearing constants.
- **Correction, 2026-09-10**: this item as originally written overstated the
  gap. On review, no test in `tests/intelligence/` was ever actually
  `pytest.mark.skip`-marked — the semantic-duplicate and related-notes
  tests all used embedded (`:memory:`) Qdrant clients successfully from the
  start (Phase 4's own established pattern), so there was nothing here
  genuinely blocked on live Docker/Qdrant access. Docker access was
  separately resolved 2026-09-10 (see the indexing-pipeline and
  retrieval-pipeline design docs' own §8 for the phases that actually did
  have real skip-marked tests), and the full suite (including this
  module's) passes unaffected either way.
- **Secret re-scanning of merged content** (§6) — flagged as a reasonable
  defense-in-depth addition, not built in this pass.

## Sources Cited

- [datasketch — PyPI](https://pypi.org/project/datasketch/)
- [datasketch — MinHash documentation](https://ekzhu.com/datasketch/minhash.html)
- [datasketch — MinHashLSH documentation](https://ekzhu.com/datasketch/lsh.html)
- [datasketch — GitHub (ekzhu/datasketch)](https://github.com/ekzhu/datasketch)
- [Qdrant — Query points API reference](https://api.qdrant.tech/api-reference/search/query-points)
