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
10. `docs/design/indexing-pipeline.md` (Phase 3)
11. `docs/design/retrieval-pipeline.md` (Phase 4 — §8 records the zero-results finding's re-verification and the embedded-mode filter bug's non-reproduction on a real server)
12. `docs/design/knowledge-intelligence.md` (Phase 5)
13. `docs/design/mcp-server.md` (Phase 6)
14. `docs/design/research-ingestion.md` (Phase 7 — new this session; §0 has the SSRF/trafilatura research, §6 the security considerations)
15. `docs/sessions/2026-09-10_docker-qdrant-resolution.md`, `docs/sessions/2026-09-10_phase7-research-ingestion.md` (this session's own record)

## Objective

**Phase 0 through Phase 7 are all implemented and tested. Phase 7's work has not been committed to git yet** (the prior same-day Docker/Qdrant-resolution session's work is already committed and pushed as `c080a9f`).

**What changed this session:**

- Drafted and got explicit acceptance for `docs/design/research-ingestion.md`, covering: an SSRF-safe fetcher, `trafilatura`-based extraction, a `research_jobs` schema extension, the `research_start`/`research_commit` MCP tools (MRTR-gated commit), the Huey task, and CLI wiring.
- Implemented `athena.research.fetch` (the SSRF-safe fetcher — built directly, the highest-risk code this phase produced), `athena.research.extract` (the `trafilatura` wrapper — delegated to a parallel agent), `athena.research.workflow` (`run_research`/`commit_draft` — built directly), migration `0005_research_drafts.sql`, `athena.mcp_server.research_tools` (built directly), `athena.worker.research_task`, and `athena research {start,commit}` CLI subcommands.
- 481/481 tests passing (55 new), mypy --strict clean, ruff clean.
- **Live-verified against the real internet and the real Qdrant server** (not just mocks): a real `athena research start` → real `huey_consumer` → real `athena research commit --commit` round trip against `https://example.com/` produced a real vault note, correctly indexed. Separately confirmed the SSRF protection live: a batch mixing a cloud-metadata URL and a legitimate URL correctly refused the former and completed with only the latter.
- New dependencies added to `pyproject.toml`: `httpx>=0.28.1` (already a transitive dependency; now a direct one), `trafilatura>=2.2.0`, `lxml>=6.1.3` (pinned past CVE-2026-41066).

**Phase 7's work has not been committed** — awaiting explicit user go-ahead (per standing practice: never commit without a separate, explicit request). The earlier same-day Docker/Qdrant-resolution session's test fixes are already committed and pushed (`c080a9f`).

## Real findings from this session (verify-before-trust discipline)

1. **A real, previously-unnoticed schema/design gap between two already-accepted documents**: `docs/EVENT_MODEL.md` already specified that `research.job_completed` carries a `draft_handle`, but no column anywhere ever held the draft itself. Closed with migration 0005 (`research_jobs.draft_title`/`draft_body`/`draft_source_urls`); `draft_handle` is simply `str(job_id)`, no new identifier concept.
2. **A real CVE found during research, not assumed**: CVE-2026-41066 (lxml XXE, CVSS 7.5, GHSA-vfmq-68hx-4jfw) in `trafilatura`'s dependency chain. `trafilatura` 2.2.0's own minimum (`lxml>=6.1.1`) already postdates the fix, but `lxml>=6.1.3` is pinned explicitly anyway, matching the project's established "pin past the fix, don't just rely on a transitive minimum" discipline from `sentence-transformers`/CVE-2026-68770.
3. **A real API-shape finding about `trafilatura` 2.2.0, verified empirically, not assumed**: `extract(html, ..., with_metadata=True, output_format="markdown")` embeds title/author/date/etc. as a YAML front-matter block ahead of the Markdown body, which would need re-parsing to keep `markdown_body` clean. Calling `extract()` with `with_metadata=False` for the body and `extract_metadata()` separately for title/author/date is cleaner (returns a typed `Document` with `str | None` fields, nothing to re-parse) — this is what `athena.research.extract` does. Also found empirically: a nav-only/boilerplate page does **not** reliably return `None` from `extract()` (trafilatura falls back to "best available text"); only a genuinely empty body, or boilerplate with no `<nav>` at all, reliably returns `None`.
4. **A genuine dispatch-ordering constraint, resolved with Huey's `context=True`, not a job_id parameter**: `research_task` needs to write onto its own `research_jobs` row, but (mirroring `duplicates_scan`/`reindex_start`'s existing insert-after-dispatch order) its `job_id` isn't known until *after* dispatch returns a huey task id and the MCP tool layer inserts the row. Solved by having the task receive its own `Task` instance via `@huey.task(context=True)` and look itself up by `huey_task_id` at execution time — raising (letting `retries=3, retry_delay=10` handle it) if the row hasn't landed yet. This is a genuine, if narrow, best-effort race; benign in practice since the dispatching insert is a fast, synchronous DB write that will normally land well within the first retry's 10-second delay.
5. **An environment finding, independently verified rather than trusted from a sub-agent's own characterization**: this `.venv` has both `httpx` 0.28.1 (used) and a separate `httpx2`/`httpcore2` 2.12.0 (which `mcp` 2.1.1 actually depends on) genuinely installed. A research sub-agent called `httpx2` "fabricated/non-existent" — checked directly via `pip show`/`import` and confirmed both packages are real and importable. Decision: use the well-proven `httpx` 0.28.1 for the new SSRF-critical fetcher code regardless, since the SSRF research itself was verified against real `httpx` documentation and `httpx` was already a working transitive dependency.
6. **No trustworthy, `httpx`-native, off-the-shelf SSRF-guard package exists** (`requests-hardened` is `requests`-only; smaller standalone packages have low adoption or are unmaintained) — hand-rolling the ~150-line `athena.research.fetch` module was the right call, consistent with ADR-0003's own stated bias toward hand-rolling over adding a dependency for something this bounded.

## What is genuinely still missing before Phase 8 starts

1. **`fastembed` revision-pinning**, **`watchdog` supply-chain review** — both still open, unaffected by this session.
2. **The retrieval-evaluation corpus** still ships at 10 notes/17 questions — unblocked since the Docker/Qdrant session, not yet expanded.
3. **The duplicate-detection default thresholds are untuned against real vault data** — likewise unblocked, not yet tuned.
4. **`note_create`/`note_update` content is still not secret-scanned before being written** — Phase 7 closed this gap for `research_commit` specifically (reuses `athena.security.secrets` exactly as `ingest_note` does), but the original Phase 6 gap for the other two write tools remains open.
5. **`note_summarize`, `note_history`/`git_status`/`git_log`/`git_commit`** — still deliberately deferred, blocked on Phase 9/8 infrastructure respectively. (`research_start`/`research_commit` are done now — Phase 7 built them.)
6. **A full wire-level elicitation round-trip integration test** — still not built for any MRTR-gated tool, `research_commit` included.
7. **Rate-limiting/depth-limiting on `research_start`/`reindex_start` job dispatch** — a known, not-newly-introduced DoS surface, flagged again in the Phase 7 design doc §6, not built.
8. **PDF/binary content extraction, autonomous web search, and LLM-driven multi-source synthesis** — all explicitly out of scope for Phase 7; autonomous search and synthesis are architecturally deferred (search: no design for it exists; synthesis: Phase 9's multi-LLM adapter).
9. Real install/venv path decision for the deployment configs (`deployment/README.md`'s "Open items") — still the only remaining blocker before the systemd/bubblewrap configs are actually usable.
10. Adding `secret_findings_list`/`secret_finding_resolve` to ADR-0007's MCP tool contract table (still open from ADR-0011).
11. **The Qdrant container is still a manually-started, unmanaged Docker container** (no systemd unit), unaffected by this session.

## Do not

- assume `research_start` performs its own web search — it takes explicit caller-provided URLs; the calling model/client is expected to have already found them via its own tools,
- call `trafilatura.fetch_url()`/`fetch_response()` anywhere — `athena.research.extract` operates on already-fetched HTML only; the one audited network-egress point for this whole feature is `athena.research.fetch.fetch_url`,
- pass a real hostname to the HTTP transport after DNS-validating it separately — that reintroduces the DNS-rebinding TOCTOU gap `_PinnedTransport` exists to close; the validated IP itself must be what the connection is made to,
- assume `research_commit`'s `dry_run=True` default is an enforced gate on its own — it is an overridable default; the actual enforcement for a non-dry-run commit is the MRTR elicitation requiring an exact topic re-type, which a model cannot fabricate on its own,
- call `research_task` expecting a `job_id` argument — it has none; it receives its own `Task` via `context=True` and looks itself up by `huey_task_id`, raising (by design, for Huey's retry to handle) if its own `research_jobs` row hasn't been inserted yet,
- treat a `None` return from `extract_article` as an error — a paywalled/JS-only/genuinely-empty page returning nothing meaningful is a normal, expected, common outcome,
- assume `note_create`/`note_update` are now secret-scanned just because `research_commit` is — Phase 7 only closed this gap for its own new write path,
- run `git remote set-url`/`git config` in this environment on the user's behalf — check `git remote -v` before assuming the local `origin` remote has or hasn't already been fixed,
- commit Phase 7's work without checking with the user first — awaiting explicit go-ahead by design (the earlier same-day Docker/Qdrant-resolution work is already committed and pushed as `c080a9f`, so don't re-commit or duplicate it either).
