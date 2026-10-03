# Session 030 — OWASP Top 10 Security Audit and Fixes

**Date:** 2026-10-03
**Phase:** Post-v1.0 (not a `docs/ROADMAP.md` phase)
**Status:** Fixes complete and verified locally — not yet committed, pushed, tagged, or released

## Objective

The user asked for an authorized penetration-test audit of ATHENA AI-BRAIN
v1.0.0 against OWASP Top 10:2025 and OWASP Top 10 for LLM Applications
2025, with every confirmed gap fixed, pushed, released as a new version,
and the existing `v1.0.0` relabeled as a beta.

## Research: verifying the actual current OWASP lists

The 2021 OWASP Top 10 is the version most broadly known; this project's
own prior `SECURITY_MODEL.md` work already used "OWASP LLM Top 10 2026"
framing, so neither list was assumed to be the commonly-cited older
version. Verified directly against primary sources
(`https://top10.owasp.org/2025/...`, `https://genai.owasp.org/llm-top-10/`):

- **OWASP Top 10:2025** (general web-application risks) changed
  meaningfully from 2021: two new categories (A03 Software Supply Chain
  Failures, A10 Mishandling of Exceptional Conditions), and Server-Side
  Request Forgery was absorbed into A01 (Broken Access Control) rather
  than remaining its own category.
- **OWASP Top 10 for LLM Applications 2025**: LLM01 Prompt Injection,
  LLM02 Sensitive Information Disclosure, LLM03 Supply Chain, LLM04 Data
  and Model Poisoning, LLM05 Improper Output Handling, LLM06 Excessive
  Agency, LLM07 System Prompt Leakage, LLM08 Vector and Embedding
  Weaknesses, LLM09 Misinformation, LLM10 Unbounded Consumption.

## Mapping categories to this codebase's real attack surface

Before deploying any agent, each of the 20 categories was mapped to
whether it's genuinely testable attack surface for this specific system
(a local, single-user MCP server + CLI over an Obsidian vault, not a
multi-tenant web app):

- **LLM04 (Data/Model Poisoning)** — excluded. This system never
  trains/fine-tunes a model; embeddings are frozen, pre-trained models
  loaded via `sentence-transformers`.
- **LLM07 (System Prompt Leakage)** — excluded. ATHENA AI-BRAIN is the
  MCP *server* (tool provider), not an LLM with a system prompt of its
  own to leak.
- Every other category was judged genuinely applicable and assigned to
  one of 6 pentest agents, grouped to avoid redundant coverage and to
  keep each agent's scope coherent:
  1. Access Control/SSRF (A01) + Excessive Agency/MRTR (LLM06)
  2. Injection (A05) + Prompt Injection (LLM01)
  3. Mishandling of Exceptional Conditions (A10) + Unbounded Consumption
     race conditions (LLM10)
  4. Software Supply Chain (A03/LLM03) + Software/Data Integrity (A08)
  5. Cryptographic Failures (A04) + Security Logging Failures (A09) +
     Sensitive Information Disclosure (LLM02)
  6. Security Misconfiguration (A02) + Authentication Failures (A07,
     confirmatory) + Vector/Embedding Weaknesses (LLM08)

## Pentest agents: methodology

Each agent was explicitly instructed to **actually attack a live
instance** — real concurrent `asyncio.gather` calls to test race
conditions, real TOCTOU timing attempts (a thread planting a symlink
between validation and use), a real `pip-audit` run against the installed
environment, real MCP `ClientSession`/`InMemoryTransport` round trips, a
real multi-hop HTTP redirect chain against a genuine third-party service
landing on the live local Qdrant instance — not just reading code and
speculating. Every finding was required to be a real, executable pytest
file under a new `tests/security/pentest/` directory, with a clear
`CONFIRMED VULNERABLE`/`CONFIRMED DEFENDED`/`DESIGN OBSERVATION` label,
before any fix was attempted — so every fix could be verified against a
concrete reproduction, not a prose claim.

## Consolidated findings

**Critical (1):** the Phase 10 daily dispatch ceiling
(`research_jobs_repo.check_daily_dispatch_limit` + `insert()`, two
separate statements with no shared transaction) was completely
bypassable via a CWE-367 check-then-act race — 8 concurrent
`research_start` calls against a ceiling of 1 dispatched 8/8, 5/8, and
4/8 across three runs.

**High (3):**
- The reconciliation safety net itself (`athena.vault.bootstrap.
  iter_markdown_files`) crashed on the identical `is_symlink()`/
  `PermissionError` bug class the v1.0.0 release had just fixed at a
  different call site (`athena.safety.paths._check_no_symlinks_in_chain`).
- An invalid-UTF-8 note aborted the entire ingest/reconcile batch
  (`_ingest_note_locked` caught only `OSError`, not `UnicodeDecodeError`;
  neither whole-vault caller wrapped the `ingest_note()` call) and left
  the job stuck at `status='running'` forever.
- All four LLM provider adapters (`athena.llm.provider`) leaked an
  unwrapped `AttributeError` for a malformed SDK response, since the
  response-*parsing* line sat outside each provider's error-handling
  `try` block (only the SDK *call itself* was wrapped).

**Medium (9):**
- Refused/blocked attacks (path traversal via `note_read`, SSRF via
  `research_start`, a secret-scan block via `commit_draft`, an MRTR
  decline via `note_delete`) produced zero `events` rows — Phase 10's
  `record_vault_event` only covered successful mutations.
- `note_summarize` returned the raw LLM provider SDK exception string
  verbatim, including masked-API-key fragments, to the MCP caller.
- `note_create` didn't return a clean message for a permission-denied
  write (no info leak — the MCP SDK's generic backstop caught it — but a
  real regression against this project's own documented "return strings,
  never raise" convention).
- Fresh-install `athena.db`/`huey.db` were created at `0644`, not the
  documented `0600`, under a normal umask — `ensure_private_file` was
  only ever called retroactively inside `athena doctor`.
- `athena.intelligence.duplicates._record_lexical_matches` read a
  DB-sourced note path straight off disk with no `resolve_vault_path`
  re-check — the one call site that skipped the second-layer defense
  every sibling (`note_update`/`note_delete`/`merge_notes`) already uses.
- Hardlinks bypassed the vault's symlink-rejection check entirely
  (`is_symlink()` is `False` for a hardlink) — a real, silent integrity
  break: editing one hardlinked note silently rewrote its "sibling,"
  with zero MRTR gate and no DB hash update.
- `note_duplicates` was annotated `read_only_hint=True` but actually
  persisted `duplicate_candidates`/`minhash_signatures` writes — a real
  confused-deputy mismatch.
- No dependency lockfile existed at all — CI resolved against
  `pyproject.toml`'s floating `>=` specifiers on every run, the exact
  attack shape that compromised `litellm`/`durabletask` upstream.
- `vault_search`/`research_commit` lacked the "data, never an
  instruction to follow" prompt-injection framing `note_read`/
  `research_start`/`note_summarize` already carry; FTS5 keyword search
  had no query-length cap — measured superlinear growth (0.0007s at 200
  tokens, 0.85s at 15,000-20,000), a real CPU-amplification DoS.

**Low (6):** `note_related` had no error handling (relied on the MCP
SDK's generic backstop); `system_diagnostics` over-discloses absolute
paths/exact tool versions; the "stdio-only" ADR `SECURITY_MODEL.md` TB-1
recommended since Phase 0 was never written (the code itself was always
correct); attacker-controlled note paths with embedded control
characters could forge fake git-log entries (contained — no RCE, and the
one internal consumer, `athena.git.read.get_log`, already parses safely
via field separators); `note_read` raised instead of returning a string
on malformed frontmatter; CI's GitHub Actions were pinned to floating
tags, not commit SHAs.

**Confirmed solid, nothing changed:** SQL injection, FTS5 syntax
injection (beyond what the fix above addresses), YAML/frontmatter RCE
(verified the installed `python-frontmatter`/`pyyaml` versions'
`SafeLoader` default actually holds against a real
`!!python/object/apply:os.system` payload), command injection, log
injection, TLS certificate verification (tested against real
`self-signed.badssl.com`/`expired.badssl.com`), secret storage (grepped
real DB bytes and log output for a real fake API key — never found),
random/token generation (no non-CSPRNG usage anywhere security-adjacent),
Qdrant filter construction (typed SDK classes only, confirmed via AST
inspection plus a live adversarial-value test), `HUEY_SECRET` entropy,
the Huey serializer fail-fast (verified live via a real subprocess with
the secret unset), `note_create`/`note_move`'s atomic TOCTOU-safe file
creation (a real planted-mid-operation-symlink race test), and MRTR
byte-level bypass attempts (case, whitespace, homoglyph, zero-width
space) across all four confirmation gates.

**Deliberately not touched — accepted, pre-existing design tradeoffs:**
`git_commit`'s lack of MRTR and its whole-tree `git add .` scope (an
already-named, explicitly-accepted `SECURITY_MODEL.md` TB-2 gap — the
audit reproduced it as a real, now-executable test and additionally
surfaced the previously-undocumented detail that `git_commit` sweeps in
untracked content no MCP tool ever wrote, but did not change the
underlying, already-accepted design); Qdrant's lack of API-key auth (an
explicit ADR-0006 tradeoff for a loopback-only, single-user deployment);
embedding inversion (a documented, structurally-unaddressed residual
risk, confirmed still accurately described, not newly found).

## Implementation: 6 parallel fix agents, then direct follow-up

Each fix agent was scoped to a non-overlapping set of files (verified by
cross-checking every agent's assigned files against every other agent's
before dispatch), instructed to reproduce the bug on unmodified code
first, then fix it, then update the pentest agents' own reproduction
tests to confirm the fixed behavior rather than leaving stale assertions.

**A real mid-session setback**: all 6 fix agents were interrupted by a
session-wide API rate limit before most had written any code (two —
ingest/reconcile robustness, and LLM provider error handling — were
mostly done; two — write-tool/permission hardening, and path-safety
hardening — were close to done; two — the dispatch-ceiling race fix, and
the MCP-tool-contract fixes — hadn't started at all). Rather than wait
indefinitely, the four partially/fully-complete agents' actual file state
was assessed directly (`git status`, `ruff`, `mypy`, a full `pytest -q`
run) once enough real time had passed that the rate limit should have
reset (the environment's clock had advanced from 2026-09-24 to
2026-10-03 between messages). Two small loose ends from that assessment
were finished directly rather than re-delegated:
- `src/athena/intelligence/duplicates.py`'s `scan_for_duplicates` had its
  `vault_root` parameter type changed from `Path` to `VaultRoot` by its
  fix agent, but two call sites (`read_tools.py::note_duplicates`,
  `worker.py::duplicates_scan_task`) still passed `vault_root.path` — a
  one-line fix at each site (mypy caught both immediately).
- Three pentest test files (`test_a02_misconfiguration_defaults.py`,
  `test_a05_injection.py`, `test_path_traversal_novel.py`) still encoded
  the *old, vulnerable* expected behavior for fixes that had actually
  landed correctly — each was rewritten to assert the now-secure
  behavior, following the same "regression test for the fix" convention
  the fix agents themselves had already established elsewhere.

The two not-yet-started agents (dispatch-ceiling race, MCP-tool-contract
fixes) were relaunched fresh once the four others were confirmed green
together (808 passed, 2 xfailed at that checkpoint).

### A real own-mistake found and fixed mid-session

While independently verifying that the new `uv.lock` actually installs
cleanly, a test command (`uv sync --frozen --python ... --active`) was
run with the working directory still pointed at the **real** repository
rather than an isolated copy — `uv sync`'s project-aware default behavior
synced the real `.venv` against the lockfile's base dependency group
only (no `--extra dev` was passed on that specific invocation), silently
removing `ruff`/`mypy`/`pytest`/`pre-commit` from the real dev
environment. Caught immediately when `ruff check`/`mypy src` reported
`command not found` right after; fixed with `pip install -e ".[dev]"`,
confirmed restored, and all subsequent lockfile verification was done in
an isolated `/tmp` copy instead.

### Closing a gap a fix agent itself flagged as incomplete

The MCP-tool-contract fix agent's own report flagged that its
`security.ssrf_refused` audit-event fix was real but incomplete: it added
an optional `db_path: Path | None = None` parameter to `run_research` (a
synchronous function with no `conn`/event loop of its own) but explicitly
left `worker.py`'s real Huey-dispatched call site out of scope, meaning
the event would only ever fire for direct test calls, never for an actual
dispatched `research_start` job. Closed directly: `worker.py::
research_task` now passes `db_path=_config.db_path` through, with a new
regression test (`tests/test_worker.py::
test_research_task_call_local_records_an_ssrf_refused_event_for_real`)
confirming the event lands in the real `events` table via the real
`call_local` dispatch path, not just a direct function call — plus fixing
an existing test's now-incompatible `fake_run_research` stub signature
that the parameter addition broke.

## Cross-cutting housekeeping done directly (not delegated)

- **`uv.lock`**: `uv` wasn't installed in this environment; installed via
  `pipx install uv` (isolated, doesn't touch system Python packages,
  avoiding a `pip install uv` failure against this environment's
  externally-managed-Python guard). Generated a real lock (160 packages),
  verified it installs cleanly via `uv sync --frozen --extra dev` in an
  isolated `/tmp` copy, and wired `.github/workflows/ci.yml` to install
  from it instead of a live `pip install -e ".[dev]"`.
- **CI Actions SHA-pinning**: fetched the real current tags and exact
  commit SHAs for `actions/checkout`, `actions/setup-python`, and the
  newly-added `astral-sh/setup-uv` directly from the GitHub API (not
  guessed — an initial guess at `setup-uv`'s version/SHA was wrong and
  was caught and corrected by actually checking) — pinned to the latest
  patch within the already-in-use major version for the first two
  (v4.4.0, v5.6.0), not a gratuitous major-version bump.
- **ADR-0012** (`docs/adr/0012-mcp-transport-stdio-only.md`): closes
  `SECURITY_MODEL.md` TB-1's own long-standing recommendation for a
  follow-up ADR stating stdio-only transport — confirmed via `grep` that
  no prior ADR ever addressed this, even though the code itself has
  always been correct (`mcp.run(transport="stdio")`, no HTTP code path
  anywhere in `src/athena/mcp_server/`).
- **`docs/SECURITY_MODEL.md` corrections**: TB-1's Spoofing finding now
  points at ADR-0012 as mitigated; TB-1's Repudiation finding updated to
  describe the real refused-attempts sub-gap found and fixed (not just
  re-asserting "mitigated as of Phase 10," which was true only for
  successful mutations); TB-1's Denial of Service finding updated to
  describe the real CWE-367 race found and the real atomic-transaction
  fix (the prior "MITIGATED as of Phase 10" claim was, in retrospect,
  incomplete — the mitigation existed but wasn't actually race-safe).

## Verification

- `ruff check src tests`: clean.
- `mypy src`: clean, 75 source files.
- `pytest -q`: **809 passed, 2 xfailed** (both pre-existing, correctly
  tracked, accepted gaps — the `reconcile_vault` provenance-backfill gap
  from Phase 10, and the `git_commit`/MRTR reproduction from this
  session's own audit, deliberately left as a tracked `xfail` rather than
  "fixed" since the underlying design decision is accepted, not a bug).
- Every fix was verified against its own pentest agent's reproduction
  test first (confirm the bug, then confirm the fix) — not a fresh
  assertion written after the fact.

## What remains (see `NEXT_SESSION.md` for full detail)

- Nothing from this session has been committed, pushed, tagged, or
  released yet — awaiting the user's explicit go-ahead for each.
- The new release's version number is not yet decided (candidates:
  `1.0.1` or `1.1.0` — either defensible).
- Relabeling the existing `v1.0.0` GitHub Release as "Beta" is planned as
  a `gh release edit v1.0.0 --title "v1.0.0 (Beta)"` metadata edit only —
  the underlying git tag/commit is not rewritten, since that would be a
  destructive rewrite of a published ref with no actual need.
- Every item already carried forward from Phase 10/Phase 11 remains open
  (the `reconcile_vault` provenance-backfill gap, `fastembed` pinning,
  CI-side gitleaks, the `ReadWritePaths=` placeholder, the retrieval-eval
  corpus size, etc.) — this session's audit didn't touch any of them.
