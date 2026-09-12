# Design: Research & Ingestion (Phase 7)

## 0. Research performed before this design

Per CLAUDE.md rule 21 ("security-sensitive functionality must be
threat-modeled before implementation") and `SECURITY_MODEL.md`'s own
explicit, standing instruction from Phase 0 (checklist item 16: "Design
SSRF protections... into the web-research ingestion feature's design doc
*before* it's implemented" — TB-4), this design's research focused first on
the SSRF threat this phase introduces, since it is the **first code in this
project's history that makes an outbound network request to a URL a model
or user supplies**. A general-purpose research fork was used; findings
below are cross-checked against primary sources, not trusted secondhand.

**Web content extraction: `trafilatura` 2.2.0.** Confirmed current
(`pypi.org/pypi/trafilatura/json`), Apache 2.0, no published security
advisories (`github.com/adbar/trafilatura/security/advisories` — zero
entries), actively maintained. Its `extract(html, url=..., output_format=
"markdown", with_metadata=True)` API takes **already-fetched HTML**, not a
URL — it never makes its own network call when used this way. Its own
`fetch_url()`/`fetch_response()` convenience functions exist but are
**deliberately not used** by this design, since routing any fetch through
code this project doesn't control would create a second, unaudited
network-egress path parallel to the SSRF-safe fetcher this design builds —
defeating the point of building one at all.

**A real, dated, sourced dependency finding**: `lxml` (a `trafilatura`
dependency) had **CVE-2026-41066 / GHSA-vfmq-68hx-4jfw** (XXE, CVSS 7.5),
fixed in 6.1.0, with a related hardening in 6.1.3 (released 2026-09-02).
`trafilatura` 2.2.0 already requires `lxml>=6.1.1`, which postdates the
fix, but this design pins `lxml>=6.1.3` explicitly in `pyproject.toml`
anyway — the same "pin past the fix, don't just rely on a transitive
minimum" discipline Phase 3 applied to `sentence-transformers` after
CVE-2026-68770.

**SSRF-safe fetching — the core research question, verified against the
OWASP SSRF Prevention Cheat Sheet and `httpx`'s own current documentation
(`python-httpx.org`), not general advice.** The attack surface: a model or
user supplies a URL like `http://169.254.169.254/...` (cloud metadata),
`http://127.0.0.1:PORT/...` or an internal `10.x`/`172.16-31.x`/`192.168.x`
address, a non-HTTP scheme (`file://`), or a hostname that resolves to a
public IP at validation time but a private one at connect time (**DNS
rebinding** — the real TOCTOU gap a naive "resolve, check, then pass the
hostname to the HTTP client" implementation has, since the client
re-resolves DNS itself at connect time). Confirmed:

- `httpx` does **not** follow redirects by default (`follow_redirects`
  defaults to `False`) — the safe default this design relies on; a
  redirect must be walked manually, re-validating each hop, never with
  `follow_redirects=True`.
- **No trustworthy, actively-maintained, `httpx`-native SSRF-guard package
  exists.** `requests-hardened` (well-maintained, current) is
  `requests`-only; several small standalone packages have low adoption or
  are explicitly unmaintained. Hand-rolling is the right call here,
  consistent with ADR-0003/ADR-0005's stated bias, and the actual logic is
  small and boundable (~60-90 lines): scheme allowlist → resolve **both**
  A and AAAA records → reject via `ipaddress.ip_address(ip).is_private`/
  `.is_loopback`/`.is_link_local`/`.is_reserved`/`.is_multicast`/
  `.is_unspecified` over every resolved address (not just the first) →
  unwrap IPv4-mapped IPv6 addresses before checking (`::ffff:10.0.0.1`
  must not slip past a check that only inspects the outer address family)
  → **pin the connection to the validated IP**, not the hostname, by
  subclassing `httpx.HTTPTransport`/`AsyncHTTPTransport`, rewriting
  `request.url.copy_with(host=validated_ip)` inside `handle_request`, and
  setting `request.extensions["sni_hostname"]` to the *original* hostname
  so TLS SNI/certificate validation still checks the real domain, not the
  raw IP. This is what actually closes the DNS-rebinding gap — validating
  a hostname and then handing the *hostname* to the client would let a
  second, different DNS answer through at connect time.
- Fetch defaults: explicit `httpx.Timeout(connect=5.0, read=10.0,
  write=5.0, pool=5.0)` (not the bare default); stream the response body
  with an enforced ~10MB cap (never trust `Content-Length` alone); a
  `text/html`/`application/xhtml+xml` content-type allowlist; an explicit
  User-Agent string; a small hard cap (5) on manually-walked redirect hops
  even though each hop is independently re-validated.

**An environment observation, not a security finding about this design**:
this project's `.venv` has `httpx2`/`httpcore2` (2.12.0) installed
alongside the classic `httpx` 0.28.1 — the `mcp` SDK depends on `httpx2`,
not `httpx`. Both are real, installed, importable packages, but their
relationship (an actual httpx v2 rewrite vs. some other lineage) wasn't
independently confirmed and doesn't matter for this design: this phase
builds its own SSRF-safe fetcher using the classic **`httpx`** package
directly (0.28.1, already a proven, working transitive dependency used
elsewhere in this project's stack, e.g. via `qdrant-client`), whose API
this design's research verified directly against `python-httpx.org`. This
avoids taking on a second, less-familiar HTTP client for a
security-critical code path.

**Fallback HTML-to-Markdown**: `markdownify` 1.2.3, current, actively
maintained, no CVEs found, pure string transformation (no network calls) —
kept as a fallback path only, not the primary one, since `trafilatura`'s
own `output_format="markdown"` is confirmed to exist and is preferred.

## 1. Purpose & Scope

Implements `docs/ROADMAP.md`'s Phase 7 checklist and resolves ADR-0007's
`research_start`/`research_commit` tools, which existed only as MRTR-free,
undermined-by-design placeholders in the tool contract until this design
gives them a real implementation.

**A scope boundary established by already-accepted architecture, not
invented here**: this design does **not** call any external LLM provider.
`athena.db`'s own `provenance.activity_type` CHECK constraint already
distinguishes `'web_research'` from `'ai_synthesis'`/`'summarization'` —
Phase 0 already drew this line. `note_summarize` (the one tool ADR-0007
names as needing "ADR-0003's Protocol-based multi-provider LLM adapter")
was already deferred to Phase 9 in `docs/design/mcp-server.md` §1/§8. This
design's "research" is **mechanical, not generative**: given a topic and
one or more URLs a caller (a model, already having used its own tools to
find them — this design does not build a web *search* capability, only web
*fetching*) has already identified, fetch each page, extract its clean
article content, assemble it into one Markdown draft with per-source
attribution, and stage it for review before writing anything to the vault.
An LLM synthesizing multiple sources into one coherent narrative is
explicitly Phase 9's job, once the adapter exists.

**In scope:**
- SSRF-safe URL fetching (`athena.research.fetch`).
- Article content extraction (`athena.research.extract`).
- Draft assembly, secret-scanning, and vault-write orchestration
  (`athena.research.workflow`).
- A schema addition holding draft content between `research_start`'s job
  completion and `research_commit`'s actual write (§3).
- `athena.worker.research_task` (task-backed, matching `duplicates_scan_task`/
  `reindex_task`'s existing pattern).
- MCP tools `research_start`/`research_commit`, finally replacing
  ADR-0007's placeholder rows, including an MRTR confirmation gate on
  `research_commit` that Phase 0's `SECURITY_MODEL.md` explicitly
  recommended (TB-2: "`research_commit`'s `dry_run=true` default... is an
  overridable default, not an enforced gate... consider requiring the
  *client* (not the model) to explicitly re-confirm before a non-dry-run
  commit").
- CLI: `athena research start`/`athena research commit`, matching every
  prior phase's CLI-alongside-MCP pattern.

**Out of scope:**
- Autonomous web search (deciding *what* to fetch) — the caller supplies
  URLs.
- LLM-driven synthesis/summarization — Phase 9.
- PDF/binary content extraction — this design's content-type allowlist
  (`text/html`/`application/xhtml+xml` only) explicitly excludes it; a
  reasonable, separately-scoped follow-up.
- Any change to the actual Git-commit chaining `note_create` et al. don't
  yet have either (Phase 8) — `research_commit` writes and indexes a note
  exactly as Phase 6's `note_create` does, no more, no less.

## 2. Responsibilities

### 2.1 SSRF-safe fetching (`athena.research.fetch`)

`fetch_url(url: str) -> FetchedPage` — the single, audited network-egress
point for this entire feature. Implements §0's mitigation exactly: scheme
allowlist, dual-stack DNS resolution and validation, IP-pinned connection
via a custom transport preserving SNI, and a manually-walked, re-validated,
capped redirect chain. Raises a typed `FetchRefused` (distinct from a
generic network failure) when a URL or any redirect hop fails validation —
callers must be able to tell "this was blocked for safety" apart from
"the network failed," since only the latter is worth retrying.

### 2.2 Article extraction (`athena.research.extract`)

`extract_article(html: str, url: str) -> ExtractedArticle` — a thin
wrapper over `trafilatura.extract(html, url=url, output_format="markdown",
with_metadata=True)`, never trafilatura's own fetching functions (§0).
Returns `None` (not an exception) when extraction yields no meaningful
content — a paywalled, JS-only, or genuinely empty page is an expected,
common outcome, not a bug.

### 2.3 Draft assembly and commit (`athena.research.workflow`)

`run_research(urls: list[str], topic: str) -> ResearchDraft` — the Huey
task body (§2.4): fetches and extracts each URL independently (one URL
failing does not abort the others — a partial draft from 2 of 3 sources is
still useful, per this codebase's established "degrade, don't crash whole
operations" philosophy from every prior phase's Qdrant-unreachable
handling), assembles one Markdown body with a `## Source: {url}` heading
per successfully-extracted article (mirroring the `## Merged from`
heading pattern `athena.intelligence.merge.merge_notes` already
established), and returns a `ResearchDraft` (title derived from `topic`,
body, and the list of URLs that actually succeeded vs. failed).

`commit_draft(conn, qdrant_client, vault_root, job_id, *, dry_run, target_path, committed_by) -> CommitResult` —
reads the draft back from the `research_jobs` row (§3), and when
`dry_run=False`:
1. Resolves `target_path` via `resolve_vault_path(..., PathMode.CREATE)`
   and writes the draft body — the same overwrite-guarded, fallible-step-
   first ordering `athena.mcp_server.write_tools.note_create` already
   established (write before any database write).
2. **Scans the just-written file for secrets** (`athena.security.secrets.
   scan_note_for_secrets`), exactly as `athena.vault.ingest.ingest_note`
   already does for every other note this codebase creates — closing the
   gap `docs/design/mcp-server.md` §6/§8 explicitly flagged as unsolved
   for `note_create`/`note_update` ("content is not secret-scanned before
   being written"). Web-fetched content is if anything a *stronger*
   candidate for this scan than model-authored content, since it can
   contain anything the source page happened to leak. High-confidence
   findings are redacted in place (`redact_high_confidence_spans`,
   rewriting the file) unless `secret_scanner_block_on_high_confidence` is
   configured, matching `ingest_note`'s own existing policy exactly — no
   new policy invented here.
3. Calls `athena.vault.lifecycle.create_note(..., origin="web_research")` —
   `notes.origin`'s CHECK constraint already includes `'web_research'`
   (Phase 0's schema), so no migration is needed for this part.
4. Writes one `provenance` activity (`activity_type="web_research"`) plus
   one `provenance_sources` row per URL that actually contributed to the
   draft (title/URL/accessed-at) — using tables Phase 2 already built for
   exactly this.
5. Chains into `index_note()`, best-effort (logged, not fatal on failure) —
   the same convention `athena.intelligence.merge.merge_notes` established
   in Phase 5 for exactly the same reason (indexing failure shouldn't undo
   a successful content write; the note is naturally re-indexable later).
6. Records `result_note_id` on the `research_jobs` row.

When `dry_run=True` (the default, per `TESTING_STRATEGY.md`'s
already-written expectation), returns a preview (title, body, source URLs)
without touching the filesystem or database at all.

### 2.4 The Huey task (`athena.worker.research_task`)

`research_task(job_id: int, urls: list[str], topic: str, correlation_id: str) -> None` —
matches `duplicates_scan_task`/`reindex_task`'s exact existing shape and
conventions: calls `run_research()`, writes the resulting draft's
title/body/source-URL-status back onto the `research_jobs` row, and marks
the job `succeeded`/`failed` via the existing `mark_started`/
`mark_finished` functions. Never writes to the vault itself — that's
`research_commit`'s job, and only after an explicit human/client
confirmation.

### 2.5 MCP tools (`athena.mcp_server.research_tools`)

- **`research_start(urls: list[str], topic: str) -> str`** (task-backed):
  same dispatch pattern as `duplicates_scan`/`reindex_start` (insert a
  `research_jobs` row, enqueue `research_task`, return a job handle
  immediately). `read_only_hint=False`, `destructive_hint=False`.
- **`research_commit(job_id: int, ctx: Context, *, dry_run: bool = True,
  target_path: str | None = None) -> str`**: when `dry_run=True` (the
  default — `TESTING_STRATEGY.md`'s already-written expectation, now
  actually enforced by a real default value, not just documented),
  returns the preview from §2.3 with no side effects. When `dry_run=False`,
  **requires an MRTR elicitation confirmation before committing** — a
  flat `ConfirmResearchCommit(confirm_topic: str)` schema, re-typing the
  research topic, checked for an exact match after `"accept"` — the exact
  same "accept is necessary but not sufficient" pattern
  `athena.mcp_server.mutation_tools` already established for
  `note_delete`/`note_merge`/overwrite-mode `note_update`. This directly
  implements `SECURITY_MODEL.md` TB-2's standing recommendation: a model
  acting on injected instructions can set `dry_run=False` in its own
  tool-call arguments, but it cannot fabricate a human's confirmation
  response to an elicitation prompt it never actually surfaced.
  `destructive_hint=False` (nothing is deleted or overwritten — a fresh
  note is created, guarded the same way `note_create` already is), but the
  MRTR gate is still applied per the security finding above, independent
  of the annotation.

### 2.6 CLI (`athena research start`/`athena research commit`)

Matches every prior phase's CLI-alongside-MCP pattern (`athena duplicates
scan`, `athena lifecycle stale-sweep`, etc.): `athena research start
--url URL [--url URL ...] --topic "..."` prints the dispatched job id;
`athena research commit JOB_ID [--dry-run/--commit] [--target-path PATH]`
prints the preview or the commit result. The CLI's own commit path has no
elicitation mechanism available (no MCP `Context`) — it requires an
explicit `--commit` flag (not merely omitting `--dry-run`) as its own
confirmation gate, printed alongside a warning, mirroring how CLAUDE.md
rule 22 ("never execute destructive filesystem or Git operations without
explicit user intent") is already satisfied elsewhere in this CLI by
requiring an explicit flag rather than inferring intent from a default.

## 3. Schema Change

One new migration (`0005_research_drafts.sql`) — the only schema gap found
during this design: `research_jobs` currently has no column to hold draft
content between `research_start`'s job completion and `research_commit`'s
read of it (`docs/EVENT_MODEL.md` §5 already specified that
`research_start`'s completion payload carries a `draft_handle`, not a
`note_id`, but never specified where the draft itself lives — a real,
previously-unnoticed gap between two already-accepted documents, not a
contradiction of either).

```sql
ALTER TABLE research_jobs ADD COLUMN draft_title TEXT;
ALTER TABLE research_jobs ADD COLUMN draft_body TEXT;
ALTER TABLE research_jobs ADD COLUMN draft_source_urls TEXT;  -- JSON array
```

`draft_handle` (as named in the event model) is simply `str(job_id)` — the
`research_jobs.id` already uniquely identifies the draft; no new identifier
concept is introduced. No new table: a draft is 1:1 with the job that
produced it, so extending the existing row is proportionate (CLAUDE.md
rule 19, "prefer small, composable modules... [not] monolithic," cuts the
other way once complexity would actually be added — a separate
`research_drafts` table for a strict 1:1 relationship would be the
over-engineered choice here, not the composable one).

## 4. Interfaces

```python
# athena/research/fetch.py
@dataclass(frozen=True)
class FetchedPage:
    url: str            # the URL actually fetched (post-redirect)
    html: str
    content_type: str

class FetchRefused(Exception):
    """Raised when a URL or redirect hop fails SSRF validation -- distinct
    from a network-level failure, which is a plain exception the caller
    should treat as retryable/transient."""

def fetch_url(url: str, *, timeout: httpx.Timeout | None = None) -> FetchedPage: ...

# athena/research/extract.py
@dataclass(frozen=True)
class ExtractedArticle:
    title: str | None
    markdown_body: str
    author: str | None
    date: str | None

def extract_article(html: str, url: str) -> ExtractedArticle | None: ...

# athena/research/workflow.py
@dataclass(frozen=True)
class ResearchDraft:
    title: str
    body: str
    succeeded_urls: list[str]
    failed_urls: list[str]

def run_research(urls: list[str], topic: str) -> ResearchDraft: ...

@dataclass(frozen=True)
class CommitResult:
    note_id: int | None       # None when dry_run=True
    preview_title: str
    preview_body: str

async def commit_draft(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    vault_root: VaultRoot,
    job_id: int,
    *,
    dry_run: bool,
    target_path: str | None,
    committed_by: str,
) -> CommitResult: ...

# athena/worker.py (extension)
@huey.task(retries=3, retry_delay=10)
def research_task(job_id: int, urls: list[str], topic: str, correlation_id: str) -> None: ...
```

## 5. Failure Modes

| Scenario | Mechanism | Result |
|---|---|---|
| A supplied URL (or a redirect hop) resolves to a private/reserved/loopback address | `fetch_url` raises `FetchRefused` before connecting | That one URL is skipped (logged), the rest of the batch still runs — one bad URL doesn't abort the whole research job |
| A page returns non-HTML content, exceeds the size cap, or times out | `fetch_url` raises a plain exception | Same as above: skipped, batch continues |
| `extract_article` finds no meaningful content (paywall, JS-only page) | Returns `None`, not an exception | Skipped, batch continues; if *every* URL fails, the job completes with an empty draft and a clear "no content extracted" status, not a silent success |
| `research_commit` called with `dry_run=False` but the elicitation is declined/cancelled/mismatched | Same "accept is necessary but not sufficient" gate as `note_delete`/`note_merge` | Nothing is written; the tool returns a clear "not confirmed" string |
| The draft's target path already exists | Same overwrite guard `note_create` already established (`os.open(O_CREAT\|O_EXCL\|O_NOFOLLOW)`) | Rejected, no overwrite, clear message |
| A committed draft contains a high-confidence secret pattern | `scan_note_for_secrets`/`redact_high_confidence_spans`, same policy as `ingest_note` | Redacted before the note is recorded, or blocked entirely if `secret_scanner_block_on_high_confidence` is set — never silently indexed with the secret intact |
| Post-commit indexing fails (Qdrant unreachable) | Same best-effort pattern `merge_notes` established | The note is still created and committed; `index_state='failed'`, naturally re-indexable later via `athena index bootstrap` |

## 6. Security Considerations

**What this closes.** This is the direct implementation of
`SECURITY_MODEL.md` TB-4's standing requirement (item 16: design SSRF
protections before implementing web-research ingestion) — see §0 for the
concrete mechanism. It also closes TB-2's specific `research_commit`
recommendation (client-side re-confirmation before a non-dry-run commit,
§2.5) and extends the secret-scanning coverage gap `docs/design/
mcp-server.md` flagged for `note_create`/`note_update` to this new,
arguably higher-risk write path (§2.3 step 2).

**Residual risk — stated honestly:**
- **The SSRF validator protects against *network*-layer targeting of
  internal/reserved addresses; it does not (and cannot) protect against a
  *legitimate*, publicly-reachable page whose content is itself malicious
  or an injection payload.** A fetched page's extracted text flows into
  the vault exactly like any other note content — the same retrieved-
  content/instruction-conflation residual risk ADR-0007/`SECURITY_MODEL.md`
  already named generally applies here with no new mitigation invented;
  the structured-content-envelope/server-authored-description defenses
  Phase 6 already put in place for `note_read`/`vault_search` are the
  relevant existing layer, unchanged.
- **DoS via unbounded `research_start` job dispatch** was flagged
  UNMITIGATED in `SECURITY_MODEL.md` TB-1 generally (also naming
  `reindex_start`, already shipped in Phase 6 with the same gap). This
  design does not add rate-limiting/job-depth-limiting for `research_start`
  specifically — consistent with "don't optimize prematurely," but flagged
  again here since this design is the first to make that theoretical
  DoS surface concretely reachable with a real, resource-consuming (network
  I/O) job body rather than a purely local one.
- **A committed research note's provenance says `activity_type=
  'web_research'`, not `'ai_synthesis'`** — accurately reflecting that no
  LLM touched this content, per §1's scope boundary. If Phase 9 later adds
  LLM-driven synthesis on top of fetched research, that will need its own
  provenance activity (`'ai_synthesis'`), layered on top of this one, not
  a retroactive change to what this design records.

## 7. Test Strategy

- **SSRF validator — the highest-priority test surface, per rule 9 and
  §0's own research call-out**: rejects `file://`/`ftp://`/other non-HTTP
  schemes; rejects a URL whose hostname resolves to a private (`10.x`,
  `172.16-31.x`, `192.168.x`), loopback (`127.x`, `::1`), link-local
  (`169.254.x.x` — the cloud-metadata-blocking case specifically), or
  reserved/multicast/unspecified address, across **both** A and AAAA
  records; rejects an IPv4-mapped-IPv6-encoded private address
  (`::ffff:10.0.0.1`); rejects a redirect chain whose *second* hop targets
  an internal address even though the first hop was public (the DNS-
  rebinding-shaped test, using a mocked resolver/transport rather than a
  real rebinding attack); a real, actually-reachable public URL succeeds
  end-to-end (an integration test, network-dependent, marked accordingly).
- **Extraction**: a real HTML fixture page produces non-empty Markdown
  with a recognizable title; a paywalled/near-empty fixture returns `None`,
  not an exception.
- **Workflow**: one failing URL among several does not abort the draft;
  `commit_draft(dry_run=True)` never touches the filesystem or database
  (parametrized alongside every other `dry_run`-capable tool per
  `TESTING_STRATEGY.md`'s existing convention); `commit_draft(dry_run=False)`
  produces the expected `notes`/`provenance`/`provenance_sources` rows and
  a real vault file; a planted high-confidence secret pattern in a fixture
  page's content is redacted before the note is recorded.
- **MCP tools**: `research_commit` with `dry_run=False` is rejected without
  a matching elicitation, exactly mirroring the existing negative tests for
  `note_delete`/`note_merge`; `research_start` returns a job handle before
  the underlying fetch necessarily completes (task-backed contract,
  already specified in `TESTING_STRATEGY.md`).

## 8. Open Items Carried Forward

- **DoS/rate-limiting on `research_start` dispatch** — flagged (§6), not
  built, consistent with "measure before optimizing."
- **PDF/binary content extraction** — explicitly out of scope (§1); a
  reasonable, separately-scoped follow-up if real usage demands it.
- **Autonomous web search** (deciding *what* to fetch, not just fetching a
  given URL) — explicitly out of scope; the calling model/client is
  expected to supply URLs it already found via its own tools.
- **LLM-driven synthesis of multiple sources into one narrative** — Phase
  9's job once the multi-provider adapter exists; this design's draft
  assembly is mechanical concatenation with per-source headings, not
  synthesis.
- **Git-commit chaining after `research_commit`** — Phase 8's job, same as
  every other mutating tool in this codebase today.

## Sources Cited

- [trafilatura — PyPI JSON API](https://pypi.org/pypi/trafilatura/json)
- [trafilatura — Python usage docs](https://trafilatura.readthedocs.io/en/latest/usage-python.html)
- [trafilatura — GitHub security advisories](https://github.com/adbar/trafilatura/security/advisories)
- [markdownify — PyPI](https://pypi.org/project/markdownify/)
- [GHSA-vfmq-68hx-4jfw — lxml XXE](https://github.com/advisories/GHSA-vfmq-68hx-4jfw)
- [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
- [httpx — Advanced: Transports](https://www.python-httpx.org/advanced/transports/)
- [httpx — API Reference](https://www.python-httpx.org/api/)
- `docs/SECURITY_MODEL.md` TB-1, TB-2, TB-4, and checklist item 16 (the standing requirement this design directly satisfies)
- `docs/EVENT_MODEL.md` §5's `research_start`/`research_commit` event mapping
