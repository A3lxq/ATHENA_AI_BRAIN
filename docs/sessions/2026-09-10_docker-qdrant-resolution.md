# Session 024 — Docker/Qdrant Access Blocker Resolved

**Date:** 2026-09-10
**Phase:** between 6 (MCP) and 7 (Research & Ingestion)
**Status:** Complete — no git commit made yet

## Objective

Following "resolve the Docker/Qdrant access blocker first" (offered as one
of two options after Phase 6's completion), checked whether Docker access
had already been resolved, found it had (the user had added their account
to the `docker` group since the last session touching this), stood up a
real Qdrant server per ADR-0006, and used it to close out every
"pending Docker access" item this project has carried since Phase 3 — not
just running the previously-skipped tests, but actually re-verifying the
open architectural questions they existed to answer.

## What was done

### Environment check and Qdrant setup

- `groups`/`id` confirmed the current user is already a member of the
  `docker` group; `docker ps` succeeded. No `sudo usermod` was needed this
  session — it had already happened.
- Checked Docker Hub's API directly (`curl .../v2/repositories/qdrant/
  qdrant/tags?...`) rather than trusting a web-search summary, which had
  named `v1.18.2` as current — the real, current stable release (by
  `last_updated`) is **`v1.19.1`**, confirmed directly.
- Started a real, pinned-version, localhost-only Qdrant server per
  ADR-0006's exact spec:
  ```
  docker run -d --name athena-qdrant \
    -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
    -v athena_qdrant_storage:/qdrant/storage \
    --restart unless-stopped \
    qdrant/qdrant:v1.19.1
  ```
  Confirmed healthy via `/healthz` and the root endpoint (`{"version":
  "1.19.1", ...}`).

### Unskipping and running the previously-blocked tests

Removed the `@pytest.mark.skip(reason=_SKIP_REASON)` decorators from all 5
integration tests (4 in `tests/indexing/test_qdrant_store.py`, 1 in
`tests/retrieval/test_vector_search.py`) and ran them for real for the
first time. **2 of 5 failed on the first run** — both real bugs, neither a
regression in the code under test:

1. **Cross-test pollution.** These tests share one persistent real
   collection across the whole suite run — unlike the `:memory:` tests
   above them in the same files, which each get a fresh, isolated client.
   `test_status_filter_excludes_archived_points_against_real_server`
   expected 1 hit and got 3, including a duplicate `note_id=1` result —
   leftover points from earlier tests in the same run that never cleaned
   up after themselves (their own assertions query by exact point ID, so
   the leftovers never affected *their* correctness, only this later
   test's broad `search()` call). Fixed by adding a
   `_clear_real_collection()` helper (`client.delete(..., points_selector=
   models.FilterSelector(filter=models.Filter()))`, wiping all points
   without recreating the collection/alias) called at the start of every
   real-server test. Confirmed the fix is actually idempotent by running
   the full pair of test files twice in a row.
2. **A real test bug, not a code bug.**
   `test_ensure_collection_alias_resolves_against_real_server` called
   `client.get_collection_aliases(COLLECTION_ALIAS)` — passing the
   *alias* name where the Qdrant API expects a *collection* name.
   Confirmed directly against the real server that this silently returns
   an empty list (`[]`), not an error — a genuine, worth-remembering API
   gotcha, verified by calling it both ways side by side. Fixed to use
   `client.get_aliases()` (list every alias, no args) filtered by
   `alias_name`, the exact pattern `test_ensure_collection_is_idempotent`
   two tests above it in the same file already used correctly.

After both fixes: **17/17 pass** in `test_qdrant_store.py` +
`test_vector_search.py` together, confirmed repeatable across two
consecutive runs.

### Re-verifying previously-unconfirmed open items with real data

- **The embedded-mode filter bug (Phase 4 §0's own flagged-unconfirmed
  item)**: directly tested whether a filter set only on the outer
  `query_filter` (not on each `Prefetch`) is silently ignored against a
  real server, the way it was in `:memory:` mode. It is not — the real
  server correctly excluded a non-matching point using only the outer
  filter. `docs/design/retrieval-pipeline.md` §8 updated to record this;
  the defensive per-`Prefetch` mitigation in `vector_search.search()`
  stays unchanged (cheap, and protects against future version/mode
  differences — this finding doesn't obsolete the reasoning for keeping
  it).
- **Phase 4 §8's "zero-results degradation" finding**: this was previously
  verified only against a Qdrant-*down* scenario. This session ingested
  and **fully indexed** the real 10-note/17-question eval corpus against
  the live server (`athena migrate` → `ingest bootstrap` → `index
  bootstrap` → `retrieval evaluate`), confirming via direct database
  inspection that all 10 notes reached `index_state='current'` with 21
  real chunks. The evaluation then produced real, working, non-zero
  metrics (`recall@3/5/10: 0.357`, `precision@3: 0.119`, `mrr: 0.357`,
  `ndcg@10: 0.357`, `unanswerable_top1_false_positive_rate: 0.000`) —
  confirming the original zero-results finding was specifically about the
  Qdrant-never-reachable case, not a defect in the retrieval pipeline
  itself. `docs/design/retrieval-pipeline.md` §8 updated with this result.
- `athena doctor`'s `qdrant_reachable` check was manually re-run and now
  reports `[ok]` for the first time.

### Correcting two inaccurate design-doc claims

While updating the Phase 5/6 design docs' own "Docker access blocked"
open items, checked whether either package's tests were actually
skip-marked (`grep -rn "pytest.mark.skip" tests/intelligence/
tests/mcp_server/`) before writing a "resolved" note — found zero matches
in both. Neither `docs/design/knowledge-intelligence.md` nor
`docs/design/mcp-server.md`'s claim that their own integration tests were
"blocked pending Docker access" was ever true; both packages' tests used
embedded `:memory:` Qdrant clients successfully from the start (Phase 4's
own established pattern). Corrected both design docs to say so plainly,
rather than writing a "resolved" note for a blocker that never existed.

## Quality gates

- `pytest`: **418/418 passing, 0 skipped** — up from 413 passing / 5
  skipped, and the first time this project's test suite has had zero
  skips across its entire history.
- `mypy --strict` across all of `src/`: clean.
- `ruff check`: clean across the whole repo (two files needed their now-
  unused `import pytest` removed after the skip decorators came out).
- Live verification, not just the test suite: `athena doctor` confirmed
  `qdrant_reachable: [ok]`; a full `migrate`/`ingest bootstrap`/`index
  bootstrap`/`retrieval evaluate` run against the real eval corpus
  produced real metrics as described above.

## What remains (see `NEXT_SESSION.md` for full detail)

- The Qdrant container is a manually-started, unmanaged `docker run` — not
  yet the systemd-managed setup ADR-0006 ultimately describes for a real
  deployment; fine for development, flagged so a future session doesn't
  assume production-readiness from "it's running," and doesn't survive a
  host reboot without a systemd unit managing it.
- The retrieval-evaluation corpus (still 10 notes/17 questions) and the
  duplicate-detection default thresholds are now genuinely unblocked to
  expand/tune against real data — neither was touched this session, since
  the objective here was resolving the blocker and re-verifying existing
  findings, not doing new tuning work.
- Everything else carried forward from Phase 6 (`fastembed` pinning,
  `watchdog` review, the three deferred MCP tool families, secret-scanning
  the MCP write path) is unaffected by this session and still open.
- This session's work (the test fixes, design-doc corrections) has not
  been committed to git — awaiting explicit user go-ahead, per standing
  practice established in every prior phase.
- Phase 7 (Research & Ingestion) is next per the roadmap, not started this
  session.
