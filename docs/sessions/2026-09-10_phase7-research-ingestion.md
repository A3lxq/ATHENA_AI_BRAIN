# Session 025 — Phase 7: Research & Ingestion

**Date:** 2026-09-10
**Phase:** 7 (Research & Ingestion)
**Status:** Complete — no git commit made yet (the earlier same-day Docker/Qdrant-resolution session's work is already committed and pushed as `c080a9f`)

## Objective

Following "start phase 7 research and ingestion" (this session, after the
Docker/Qdrant blocker was resolved earlier the same day), design and
implement web research ingestion: fetch caller-supplied URLs, extract clean
article content, assemble a Markdown draft with provenance, and — only
after explicit confirmation — write it into the vault. This resolves the
`research_start`/`research_commit` MCP tools ADR-0007 named but every prior
phase had to defer (no infrastructure existed for them until now).

## Research

- Confirmed, via cross-referencing already-accepted architecture (the
  `provenance.activity_type` CHECK distinguishing `'web_research'` from
  `'ai_synthesis'`/`'summarization'`, and ADR-0003/ADR-0007/the Phase 6
  design doc's own deferral of `note_summarize`'s LLM adapter to Phase 9),
  that this phase is pure fetch+extract+format+provenance+validate+store,
  with **no LLM synthesis** — Phase 9's job once the multi-provider adapter
  exists.
- Confirmed `research_start` takes explicit caller-supplied URLs, not
  autonomous search — it's task-backed (Huey-dispatched), and a detached
  background job can't rely on a live client's own search tools; nothing in
  the accepted architecture describes a search-API integration.
- `trafilatura` 2.2.0 (Apache 2.0, no open security advisories) for
  HTML→Markdown extraction, confirmed to operate on already-fetched HTML
  only when used via `extract()`/`extract_metadata()` — its own
  `fetch_url()`/`fetch_response()` deliberately never used, since that would
  open a second, unaudited network-egress path.
- **A real, dated CVE found in `trafilatura`'s dependency chain**:
  CVE-2026-41066 / GHSA-vfmq-68hx-4jfw (lxml XXE, CVSS 7.5), fixed in
  lxml 6.1.0, hardened further in 6.1.3 (2026-09-02). `lxml>=6.1.3` pinned
  explicitly in `pyproject.toml`, past `trafilatura`'s own lower minimum —
  the same "pin past the fix" discipline applied to `sentence-transformers`/
  CVE-2026-68770 in Phase 3.
- **SSRF (the central security question)**, verified against the OWASP SSRF
  Prevention Cheat Sheet and `httpx`'s own current docs: no trustworthy,
  actively-maintained, `httpx`-native SSRF-guard package exists
  (`requests-hardened` is `requests`-only). The mitigation pattern —
  scheme allowlist → resolve every A/AAAA record → reject any
  private/loopback/link-local/reserved/multicast/unspecified address
  (unwrapping IPv4-mapped IPv6 first) → **pin the TCP connection to the
  validated IP** via a custom `httpx.HTTPTransport` (closing the
  DNS-rebinding TOCTOU gap a validate-then-hand-off-the-hostname
  implementation would leave open) while preserving TLS SNI/cert checks
  against the real hostname via the `sni_hostname` extension → no automatic
  redirect-following, manual re-validated hop-walking capped at 5 — was
  confirmed hand-rollable at a bounded size (~150 lines), consistent with
  ADR-0003's stated bias.
- **An environment observation, independently verified rather than trusted
  from a sub-agent's own claim**: this `.venv` has both real `httpx` 0.28.1
  and a separate, also-real `httpx2`/`httpcore2` 2.12.0 (which `mcp` 2.1.1
  depends on). A research sub-agent called `httpx2` "fabricated" — checked
  directly via `pip show`/`import` and confirmed both packages are genuinely
  installed and importable. Decision: build the new SSRF-critical fetcher
  against the well-proven `httpx` 0.28.1, since the SSRF research itself was
  verified against real `httpx` documentation.
- **A real, previously-unnoticed gap found between two already-accepted
  documents**: `docs/EVENT_MODEL.md` already specifies that
  `research.job_completed`'s payload carries a `draft_handle`, but no
  column anywhere in the schema ever held the draft content itself.
  Required a new migration (0005) — not a design flaw in either prior
  document, just something neither one's own scope happened to close.

## Design

`docs/design/research-ingestion.md` drafted covering: the SSRF-safe fetcher
(§2.1, with the security considerations required by `SECURITY_MODEL.md`
checklist item 16 spelled out in full in §6), `trafilatura` extraction
(§2.2), draft assembly and commit orchestration (§2.3), the `research_jobs`
schema extension (§3), the `research_start`/`research_commit` MCP tools with
an MRTR gate on non-dry-run commits (§2.5, directly implementing
`SECURITY_MODEL.md` TB-2's standing recommendation), the Huey task (§2.4),
and CLI wiring (§2.6). Presented to the user and accepted ("yes i accept, go
ahead with implementation") before any implementation code was written.

## Implementation

Built directly (highest-risk/most security-critical code, per this
project's established practice):
- `athena.research.fetch` — the SSRF-safe fetcher.
- `athena.research.workflow` — `run_research`/`commit_draft` orchestration,
  reusing `athena.vault.ingest`'s exact write-then-scan-then-redact
  secret-scanning ordering and `athena.intelligence.merge`'s crash-safety
  ordering (filesystem write before any database write).
- `athena.mcp_server.research_tools` — `research_start`/`research_commit`,
  MRTR-gated.
- `athena.worker.research_task`, `run_research_commit`, CLI wiring
  (`athena research {start,commit}`).
- Migration `0005_research_drafts.sql` and the `research_jobs` repository
  extensions (`record_draft`, `record_result_note`, `get_by_huey_task_id`).

Delegated to a parallel agent: `athena.research.extract` (the `trafilatura`
wrapper). The agent verified empirically (not assumed) that
`extract(..., with_metadata=True, output_format="markdown")` embeds a YAML
front-matter block ahead of the body that would need re-parsing, and chose
instead to call `extract()` (`with_metadata=False`) for the body plus
`extract_metadata()` separately for title/author/date — cleaner, nothing to
re-parse. Also found empirically that a nav-only/boilerplate page does not
reliably return `None` (trafilatura falls back to "best available text");
only a genuinely empty body, or boilerplate with no `<nav>` at all,
reliably returns `None`.

### A genuine dispatch-ordering constraint, found and resolved during implementation

`research_task` needs to write its own draft onto its `research_jobs` row,
but — mirroring `duplicates_scan`/`reindex_start`'s existing
insert-after-dispatch order in `athena.mcp_server.job_tools` — its `job_id`
isn't known until *after* Huey dispatch returns a task id and the MCP tool
layer inserts the row keyed to it. Resolved using Huey's `@huey.task(
context=True)`, which injects the task's own `Task` instance (with `.id`)
as a `task` kwarg; `research_task` looks itself up by `huey_task_id` at
execution time rather than receiving `job_id` as an argument it cannot yet
have. If the row hasn't landed yet (a fast worker beating the dispatcher's
own DB insert), this raises, letting Huey's `retries=3, retry_delay=10`
handle the benign race.

## Verification

- 481/481 tests passing (55 new this session: `tests/research/{test_fetch,
  test_extract,test_workflow}.py`, `tests/mcp_server/test_research_tools.py`,
  additions to `tests/test_worker.py` and
  `tests/db/repository/test_research_jobs.py`), mypy --strict clean, ruff
  clean.
- **Live end-to-end verification, not just mocks**: with the real Qdrant
  server (from the earlier same-day Docker/Qdrant-resolution session) still
  running, ran a real scratch vault through `athena migrate` → `athena
  research start --url https://example.com/ --topic "Example Domain
  Research"` → a real `huey_consumer` process → `athena research commit 1`
  (dry-run preview) → `athena research commit 1 --commit`. Confirmed via
  direct database inspection: the vault file was written with the real
  extracted content, `notes.origin='web_research'`,
  `notes.secret_scan_status='clean'`, `notes.index_state='current'` (real
  indexing against the real Qdrant server succeeded), a `provenance` row
  linked to the `research_jobs` row via `research_job_id`, and a
  `provenance_sources` row recording the real source URL.
- **Separately verified the SSRF protection live, in the actual dispatched/
  consumed pipeline, not just in unit tests**: dispatched a second job with
  one cloud-metadata URL (`http://169.254.169.254/latest/meta-data/`) and
  one legitimate URL (`https://example.org/`) in the same batch. After the
  consumer ran, the job completed `succeeded` with `draft_source_urls`
  containing only the legitimate URL — the metadata URL was refused for
  real, and the batch degraded gracefully rather than failing outright.

## Quality gates

- `pytest`: 481/481 passing, 0 skipped.
- `mypy --strict` across `src/`: clean (one real typing gap found and fixed:
  `socket.getaddrinfo`'s `sockaddr[0]` is typed `str | int` by typeshed;
  narrowed explicitly with `str(...)` rather than widening the surrounding
  type).
- `ruff check`: clean.
- Live CLI/consumer verification as described above.

## What remains (see `NEXT_SESSION.md` for full detail)

- Phase 7's work has not been committed to git yet — awaiting explicit
  user go-ahead (the earlier same-day Docker/Qdrant-resolution session's
  work is already committed and pushed as `c080a9f`).
- Rate-limiting/depth-limiting on `research_start` job dispatch — a known,
  not-newly-introduced DoS surface (shared with `reindex_start`), flagged
  again in the design doc §6, not built in this pass.
- `note_create`/`note_update` are still not secret-scanned before writing —
  Phase 7 closed this gap only for its own new `research_commit` path.
- PDF/binary extraction, autonomous web search, and LLM-driven multi-source
  synthesis remain explicitly out of scope.
- Phase 8 (Git Automation) is next per the roadmap, not started this
  session.
