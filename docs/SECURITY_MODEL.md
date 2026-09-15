# ATHENA AI-BRAIN — Security Model

## Security principle

Treat external input as untrusted.

This includes:
- web pages,
- Markdown,
- Git repositories,
- AI-generated content,
- retrieved chunks,
- MCP requests.

## Key threats

### Prompt injection

A retrieved note may contain instructions attempting to manipulate an LLM.

Mitigation:
- clearly separate retrieved data from system instructions,
- provenance,
- trust metadata,
- validation,
- avoid executing instructions found in documents.

### Path traversal

Never allow arbitrary paths to escape configured vault roots.

### Secrets

Do not index API keys, passwords, tokens, SSH private keys, or credential files.

A future secret scanner should run before ingestion and Git commit.

### Git safety

Never construct shell commands from untrusted text.

Prefer structured subprocess arguments.

### Tool abuse

MCP tools that mutate the vault or Git must have explicit permission boundaries.

### Web ingestion

Web content must be treated as untrusted source material.

Do not execute downloaded content merely because it was retrieved during research.

## Security review

Every feature that handles external input must include a threat model and security tests.

---

## Threat Model (Phase 0 exit-criteria deliverable — adversarially reviewed, 2026-08-27)

**Scope:** ATHENA AI-BRAIN's accepted Phase 0 architecture (MCP server ↔ business logic ↔ SQLite/Qdrant/Huey/Git ↔ filesystem vault ↔ external LLM providers), per ADR-0001 through ADR-0009 (all Accepted), `00_MASTER_PROJECT_SPECIFICATION.md` §15, `DEVELOPMENT_CONSTITUTION.md` Article 9, `ARCHITECTURE.md`, `DATA_MODEL.md`, and `EVENT_MODEL.md`.
**Framework:** STRIDE, applied per trust boundary, with each LLM/RAG-specific scenario cross-referenced to its OWASP Top 10 for LLM Applications 2026 category (LLM01–LLM10, published 2026-08-04, confirmed current).
**Review process:** drafted 2026-08-25, then adversarially red-team reviewed against primary sources and the project's own ADRs on 2026-08-26 — this section reflects the reviewed, corrected version. Findings the review could not verify are marked as such; findings the review corrected are noted inline.

### Executive Summary

ATHENA AI-BRAIN's accepted ADRs already correctly identify and mitigate the "obvious" infrastructure-level risks: subprocess argument injection (ADR-0005), pickle deserialization in the job queue (ADR-0002), Qdrant's no-auth default (ADR-0006), SQL injection (ADR-0004), and a known-CVE'd RAG framework (ADR-0003/LangChain). These are sound, evidence-based decisions and this document does not relitigate them.

The genuinely open risk is structural, not a missing library choice: **ATHENA AI-BRAIN's MCP tool contract (ADR-0007) gates confirmation only on tools classified `destructive` (`note_delete`, and `note_merge`/`note_move` under narrower, ADR-0007-specific conditions — see the note on mechanism strength below). Every other mutating tool — `note_create`, `note_update`, `note_link`, `research_start`, `research_commit`, `git_commit` — requires no confirmation.** Since no protocol-level mechanism separates "retrieved data" from "instructions" (confirmed directly against the current official MCP Security Best Practices document, which has no section on this threat), a prompt-injection payload hidden in a retrieved note cannot make the calling LLM delete files without a human echoing back a path via MRTR — but it *can* make the calling LLM silently create new notes, edit existing ones, kick off a research job with attacker-chosen queries, or trigger a Git commit that launders the injected content into version history, all with zero confirmation. **Important calibration**: as of Phase 0, this is best read as an architecturally-required design constraint that must be correctly closed *before* Phase 2's web-ingestion feature ships — not as an actively exploitable Phase 1 condition, since planting a malicious note today already requires local vault write access, at which point an attacker has simpler paths available than routing through an LLM. It does not change the recommended remediations, which are correctly proactive.

Ten other risks are real, currently absent from every ADR, and should be closed before Phase 1 exits security review:
1. **No concrete path-traversal/symlink-safety mechanism is committed anywhere** (this document states the principle; no ADR names `pathlib.resolve(strict=True)` + ancestor-check, or a TOCTOU-safe pattern).
2. **No OS-level sandboxing is considered anywhere as a backstop for (1).** ATHENA AI-BRAIN's entire path-traversal defense is application-level Python logic; if that logic has a bug — and path-traversal bugs recur even in mature, reviewed software — there is no OS-level backstop, since the MCP server and Huey workers run with the full privilege of the user's own login. This is the single most significant structural gap found in this review pass.
3. **MCP transport (stdio vs. HTTP) is never decided in ADR-0007.** If ATHENA AI-BRAIN ever adds HTTP transport, the full OAuth attack surface documented in the current MCP spec (confused deputy, token passthrough, SSRF-via-discovery, mix-up attacks, state-handle hijacking) becomes live and is addressed by zero ATHENA AI-BRAIN documentation today.
4. **Qdrant's stored vectors are not an inert index — they are an invertible copy of the vault**, and BGE-M3 being an open-weight model means an attacker who steals the Qdrant data directory also has trivial white-box access to the exact encoder used, an easier inversion setting than most published (black-box/closed-API) inversion research.
5. **The pre-ingestion secret scanner is named as "future" above** but has no ADR, no design, and no Phase 1 commitment — only the pre-*commit* gitleaks hook (ADR-0005) is actually decided, which fires too late (after content is already indexed into SQLite/Qdrant).
6. **No rate limiting or per-session/day cost ceiling exists on any tool that calls an external LLM provider** (`note_summarize`, and implicitly `research_start`) — for a single user with one metered API key, the correct scope is a simple cost-ceiling config value, not general-purpose rate-limiting infrastructure.
7. **Huey's sync-core/async-bridge (`aget_result()`, ADR-0002) is an unaddressed DoS chokepoint**: a hung or slow bridged call (a stalled external-LLM call, or an injection/burst-triggered pile of jobs) can stall ATHENA AI-BRAIN's entire asyncio event loop, not just delay background work, if the bridge isn't given its own bounded executor and timeout.
8. **SQLite FTS5's embedded query grammar is a distinct risk from SQL injection**, which ADR-0004's "parameterized queries" mitigation does not cover — a note-derived title/tag concatenated unquoted into a `MATCH` expression could suppress or skew what internal automated queries (dedup, related-notes) return.
9. **`chonkie` (ADR-0003) never received the same CVE/maintainer-trust scrutiny GitPython/Dulwich/LiteLLM did**, despite touching 100% of ingested vault content before embedding.
10. **Qdrant's collection-alias swap mechanism is unspecified** — if implemented as a non-atomic check-then-create sequence rather than Qdrant's atomic alias-update API, a crash-restart race or overlapping reindex trigger could leave search silently pointed at a stale/orphaned collection.

2026 threat intelligence corroborates that this is not theoretical caution: over 40 CVEs were disclosed against MCP implementations in the first four months of 2026 alone (independently confirmed across multiple trackers), real production incidents have already occurred (the Supabase MCP `service_role`-key exfiltration via a poisoned support ticket — the canonical "lethal trifecta" case study — and a real, CVSS ~6.3 Kong Konnect MCP indirect-prompt-injection CVE, formally classified MCP06 rather than "confused deputy" though sometimes informally described that way), PyPI supply-chain campaigns in H1 2026 outpaced all of 2025 by 2.6× in campaign count and 4.5× in package volume, and the LiteLLM compromise ADR-0003 already cites is now precisely documented (two backdoored PyPI releases, one using a `.pth`-file interpreter-startup hook — a mechanism worth naming explicitly, since it defeats "we only import what we use" reasoning).

### Trust Boundaries

| # | Boundary | Untrusted input | Already governed by |
|---|---|---|---|
| TB-1 | MCP client ↔ MCP server | Tool call arguments, resource requests, elicitation responses | ADR-0007 |
| TB-2 | Retrieved vault content ↔ calling LLM | Note bodies/frontmatter surfaced via `vault_search`, `note_read`, the `vault://` resource | ADR-0007 (partial) |
| TB-3 | Filesystem vault ↔ ingestion/event pipeline | Paths, symlinks, filenames, YAML frontmatter, Markdown bodies from `watchdog` events | ADR-0009 (principle only, no mechanism) |
| TB-4 | Web-ingested research content ↔ vault | Fetched web pages/URLs (future feature per master spec §6, §15) | Not yet designed |
| TB-5 | Git remote/collaborators ↔ local vault | Pulled commits, merge content, remote refs | ADR-0005 (mutation path only) |
| TB-6 | Subprocess boundary | `git` CLI stdout/stderr/exit codes, `gitleaks` JSON output | ADR-0005 |
| TB-7 | SQLite / Huey job store (incl. FTS5 query grammar) | Job payloads (serialization), FTS5 query text | ADR-0002, ADR-0004 (partial — FTS5 grammar not covered) |
| TB-8 | Qdrant vector store (incl. collection-alias mutation) | Local network exposure, stored vector payloads, alias swap operations | ADR-0006 (partial — alias-swap atomicity not specified) |
| TB-9 | External LLM provider APIs | Model responses fed back into ATHENA AI-BRAIN; outbound prompt content | ADR-0003 (adapter only) |
| TB-10 | Embedding/reranker model artifacts | `sentence-transformers` model weights pulled from Hugging Face Hub | Not addressed |
| TB-11 | Secrets & configuration | API keys, `.env`, provider credentials | Principle only, above |
| TB-12 | Python dependency supply chain | Every third-party package at install and interpreter-startup time | ADR-0001/0003/0005 (partial, per-package) |
| TB-13 | Huey sync-core/async-bridge boundary | Bridged calls from asyncio into Huey's synchronous execution model | ADR-0002 (integration risk flagged, not security risk) |

### STRIDE-Based Threat Enumeration

#### TB-1: MCP Client ↔ MCP Server

**Spoofing.** A malicious or compromised MCP *host* issues tool calls indistinguishable from a trusted one — MCP's 2026-07-28 spec is stateless with no protocol session concept beyond whatever handle a server mints itself. *Severity: Medium.* If ATHENA AI-BRAIN runs stdio-only (the transport ADR-0007 leaves undecided), the OS process boundary is the actual authentication mechanism. **UNMITIGATED as a stated decision** — ADR-0007 never commits to a transport. **Remediation:** a follow-up ADR should explicitly state stdio-only for Phase 1; any future handle-based state (job IDs, elicitation confirmations) must be non-deterministic, bound to caller, never treated as authentication on its own.

**Tampering.** Tool-description "rug pull" (a server changing a tool's description post-approval) is not directly applicable — ATHENA AI-BRAIN is a single, self-authored server with no third-party MCP aggregation. The mechanism is relevant in reverse: a compromised transitive dependency executing at interpreter-startup time (TB-12's `.pth`-file mechanism) could tamper with ATHENA AI-BRAIN's own tool descriptions or business logic before its code runs. *Severity: Low likelihood, High impact.* **Gap:** no ADR addresses integrity-verification of ATHENA AI-BRAIN's own installed package tree.

**Repudiation.** No mutating tool call is required by any ADR to produce an audit log entry independent of the Git commit trail. Git commits (ADR-0005/0007) provide some provenance for vault mutations, but job-queue-dispatched actions and LLM-calling read paths (`note_summarize`) leave no equivalent record. *Severity: Medium.* **MITIGATED as of Phase 10** (`docs/design/production-hardening.md` §2.1): every mutating vault tool (`note_create`, `note_update`, `note_link`, `note_move`, `note_delete`, `note_merge`) and `research_commit` now emit their own semantic `vault.*`/`dedup.*` event via `athena.db.repository.events.record_vault_event`, independent of the Git commit trail, matching `EVENT_MODEL.md`'s originally-specified mapping table. `note_summarize` still leaves no equivalent record, since it is read-only and produces no vault mutation to attribute — this is an accepted scope boundary, not a residual gap.

**Information Disclosure.** Tool annotations (`readOnlyHint`, `destructiveHint`) are documented by the MCP project itself as informational only — ADR-0007 already correctly notes this and does not rely on them for enforcement. No gap here.

**Denial of Service.** A malicious/buggy client can enqueue unbounded `research_start`/`reindex_start` jobs faster than Huey's single-worker SQLite backend can drain them, or call `note_summarize` in a loop, driving unbounded external-LLM API spend — OWASP LLM06:2026 "Unbounded Consumption." *Severity: High for a solo user specifically* — this is one person's own credit card, not an abstracted enterprise "denial of wallet" line item, so the personal stakes are more acute than the generic OWASP framing suggests. **MITIGATED as of Phase 10** (`docs/design/production-hardening.md` §2.2): `note_summarize`'s daily-call-ceiling pattern (Phase 9) has been generalized to `research_start`/`reindex_start` via `athena.db.repository.research_jobs.check_daily_dispatch_limit`, enforced both at the MCP tool layer and in the CLI's independent `research start` dispatch path, each configurable (`ATHENA_RESEARCH_MAX_DISPATCHES_PER_DAY`/`ATHENA_REINDEX_MAX_DISPATCHES_PER_DAY`, defaulting to 50/20 respectively).

**Elevation of Privilege.** MCP tool calls execute with the full privilege of the ATHENA AI-BRAIN server process — no MCP-level RBAC exists, and the spec's OAuth-scope authorization model is only meaningful over HTTP transport (undecided, see Spoofing above). Genuinely mitigated only where ADR-0007 excludes an operation from the MCP surface entirely (force-push, hard reset, branch delete) — a stronger guarantee than any confirmation gate.

#### TB-2: Retrieved Vault Content ↔ Calling LLM (the core RAG injection surface)

This is the boundary ADR-0003/0007 already name as having no protocol-level defense.

**Concrete scenario (Tampering / Elevation of Privilege — OWASP LLM01:2026 Prompt Injection):** A note engineered to rank highly for plausible future `vault_search` queries (the "adversarial passage" pattern — as few as five crafted documents in a multi-million-document corpus achieved 90% targeted attack success in published research) contains, in an HTML comment or invisible Unicode, directives such as instructing the calling LLM to create notes, kick off a research job, or trigger a Git commit. Because the LLM cannot structurally distinguish retrieved data from instructions, it may act on the embedded directive when a legitimate, unrelated query later retrieves the note.

**What actually stops this, per ADR-0007, and what doesn't — precision matters here:**
- `note_delete`: **stopped** with a specifically strong mechanism — MRTR elicitation requiring the client to echo the exact target path.
- `note_move` (when overwriting an existing destination) and `note_merge`: ADR-0007 requires "elicitation confirm," but does **not** specify the same "exact path echo" mechanism `note_delete` gets — these are real but comparatively less-specified confirmation requirements, not an equivalent guarantee. (Correction from the initial draft, which had flattened all three into one description.)
- `note_create`, `note_update`, `note_link`, `research_start`, `git_commit`: **not stopped** — classified "mutating" but not "destructive," and ADR-0007's confirmation requirement is scoped to the destructive class only.
- `research_commit`'s `dry_run=true` default: **this is a default parameter value, not a protocol-level confirmation gate.** Nothing stops an LLM acting on injected instructions from simply calling `research_commit` with `dry_run=False` explicitly — the calling model chooses its own arguments. This is categorically weaker than MRTR elicitation, which requires an actual round-trip from the client/human, not just the model selecting a kwarg. (Correction: an earlier draft of this analysis inconsistently called this "adequately mitigated" in one place while calling the same class of gap "not stopped" elsewhere — the accurate, uniform position is the one stated here.)
- `note_summarize` (`openWorldHint=true`, calls an external LLM provider): **not stopped**, and ADR-0007 explicitly leaves as an open question whether this even needs an opt-in gate — injected content could trigger exfiltration of other note content to a third-party API with zero confirmation today.

**Severity and likelihood, calibrated to ATHENA AI-BRAIN's actual Phase 0/1 context:** the underlying design gap is real and High-severity in the abstract, but the realistic *current* attack surface is narrow: planting a malicious note today requires an attacker who already has local filesystem write access to the vault — at which point simpler attacks (reading `~/.ssh` directly) don't need to route through an LLM at all. This finding is best understood as **an architecturally-required design constraint that must be correctly closed before Phase 2's web-ingestion feature (TB-4) or any shared/synced-vault feature (TB-5) ships** — not as an actively exploitable Phase 1 condition. This distinction affects how the finding should be read, not the remediations, which remain correctly proactive design requirements now, before those features exist, per Constitution Article 2 ("design before coding").

**Recommendation (Phase 1 design decision, not yet made by any ADR):**
1. Widen confirmation, or add a distinct "requires-review" classification, for mutating tools whose arguments plausibly derive from LLM reasoning over retrieved content — since the server can't know whether a call was user-directed or injection-directed, require `research_commit` and any auto-triggered `git_commit` following an LLM-driven write to carry a structured provenance field naming which retrieved chunks informed the write, logged for audit even if not blocked. For `research_commit` specifically, consider requiring the *client* (not the model) to explicitly re-confirm before a non-dry-run commit, closing the kwarg-override gap noted above.
2. Resolve ADR-0007's open question: gate `note_summarize` behind explicit user opt-in, not an implicit always-on capability.
3. Build the heuristic injection-pattern scanner ADR-0007 leaves open — flag (not silently strip) invisible/zero-width Unicode, imperative-verb HTML comments, or anomalous whitespace padding, surfaced as a `trust_signal` field, explicitly acknowledged as heuristic and bypassable, not a real boundary.
4. Wrap retrieved content in a structurally distinct, clearly-delimited envelope in every tool response — weak against a sufficiently capable model but cheap, standard defense-in-depth.

#### TB-3: Filesystem Vault ↔ Ingestion/Event Pipeline

**Tampering (path traversal / symlink attack — OWASP LLM05:2026 Data and Model Poisoning at the ingestion layer; classic CWE-22/CWE-59).** A note-move/note-create call, or a crafted symlink placed inside the vault by any process with vault write access (including a malicious dependency, TB-12), provides a path that after resolution escapes the configured vault root. Current best practice: canonicalize the vault root once at startup; for every incoming path, call `Path.resolve(strict=True)` (the `strict=True` flag closes a TOCTOU race where a malicious symlink is created between a check and the actual file operation) and verify the vault root is a member of the resolved path's `.parents` sequence (an ancestor check, more precise than string-prefix comparison, which incorrectly accepts sibling directories sharing a prefix like `/vault` vs. `/vault-backup`). **Status: UNMITIGATED as a concrete mechanism** — a Phase 1 P0 item.

**No OS-level backstop exists for the above.** ATHENA AI-BRAIN's entire path-traversal defense is application-level Python logic; if it has a bug, the MCP server and Huey worker processes run with the full privilege of the user's own login — access to their entire home directory, SSH keys, browser profiles, and everything else, with nothing to contain a mistake. This is cheap to close with unit-file hardening (systemd `DynamicUser=yes`, `ProtectHome=` scoped to read-only except the vault path, `ReadWritePaths=` narrowed to the vault plus ATHENA AI-BRAIN's own data directories, `NoNewPrivileges=yes`, `ProtectSystem=strict`) and requires no code changes — it is explicitly a backstop for "the path-canonicalization fix has a bug," not a replacement for fixing the logic itself. **UNMITIGATED, and the single most significant structural gap identified in this review.**

**Tampering (malicious YAML frontmatter).** `python-frontmatter`'s default loader behavior (`yaml.safe_load` vs. `yaml.load`) must be verified before it touches any vault content — the *principle* is stated repeatedly (CLAUDE.md, this document, ADR-0004) but no ADR confirms the specific library's actual default. **Status: PARTIALLY mitigated** (principle only).

**Denial of Service (inotify exhaustion / event flood).** ADR-0009 documents the `fs.inotify.max_user_watches` sysctl prerequisite and light-debouncing design — well covered. Residual: a burst of filesystem writes could overflow the debounce/reconciliation pipeline faster than Huey's single worker drains it. *Severity: Low* (ADR-0009's reconciliation backstop bounds the damage to "temporarily stale index," not data loss) — adequately mitigated, flagged for completeness.

**On file-permission hardening for the databases (see TB-7/TB-8):** the rationale for `0600`/`0700` permissions is defense-in-depth against *any other local process* (a compromised browser extension, a malicious dependency of an unrelated tool, a future multi-user scenario) — not a claim that other human users share this Kali machine today, which no project document supports. The remediation is cheap and correct regardless; the justification should not overstate the current threat.

#### TB-4: Web-Ingested Research Content ↔ Vault (future feature)

Not yet designed, appropriately — recorded here so Phase 1/2 design addresses it before the feature ships, per treating future-feature source material as untrusted now.

**Spoofing/Tampering (SSRF — OWASP LLM05:2026, overlapping the SSRF class the official MCP spec treats as first-class).** A future `research_start` web-fetch implementation, if it doesn't apply the same SSRF controls the current MCP spec recommends for OAuth-metadata discovery (block private/reserved IP ranges, enforce HTTPS, don't blindly follow redirects, avoid hand-rolled IP-range parsing vulnerable to octal/hex/IPv4-mapped-IPv6 encoding tricks), could be induced to fetch internal-network or cloud-metadata addresses via a URL chained in from TB-2. **Recommendation:** write this SSRF-hardening requirement into the design document for `research_start`'s web-fetch implementation *before* any code is written, per Constitution Article 2.

**Tampering (poisoned research output written to vault).** `research_commit`'s `dry_run=true` default is a real control giving a human review checkpoint — see TB-2's correction above regarding its actual strength (an overridable default, not an enforced gate). The residual risk beyond that is whether a human reviewer actually reads the diff, a process/UX concern outside this document's scope.

#### TB-5: Git Remote/Collaborators ↔ Local Vault

**Tampering.** If the vault is ever configured with a Git remote (master spec §12 doesn't foreclose multi-machine use), a pull/merge could bring in content authored elsewhere, re-entering the TB-2 injection surface via a path that bypasses ATHENA AI-BRAIN's event-driven ingestion assumptions (ADR-0009's reconciliation job would eventually index it, with no additional scrutiny). **Status: not addressed by any ADR.** *Severity: Low today* (single-user local-first is the stated design center), but should be an explicit statement here rather than a silent gap.

**Denial of Service / Tampering (merge conflicts).** ADR-0005 correctly defers to `git`'s own CLI rather than reimplementing merge logic — directly informed by Dulwich's real merge-driver CVE (CVE-2026-42563). No further gap.

#### TB-6: Subprocess Boundary (`git` CLI, `gitleaks`)

Well-covered by ADR-0005's argument-list-only + `--`/`--end-of-options` + allow-listing design, informed by real 2026 CVEs in the libraries it rejected (GitPython CVE-2026-42215, Dulwich CVE-2026-42563 — both independently confirmed against public advisories). Residual: subprocess *output* parsing (stderr text used for the failure taxonomy) is semi-untrusted if attacker-controlled content could echo back into it — a parsing-robustness concern (don't `eval`/exec anything derived from stderr) rather than a live injection path, but worth stating explicitly since no ADR does.

#### TB-7: SQLite / Huey Job Store (including FTS5 query grammar)

**Tampering (deserialization — OWASP LLM04:2026 Supply Chain / CWE-502).** ADR-0002's swap of Huey's default pickle serializer for `SignedSerializer`/JSON is correct. **Residual gap:** no enforcement mechanism stops a future code change or misconfiguration from silently reverting to pickle. **Recommendation:** a startup assertion that fails loudly if the configured serializer is ever the pickle default — converting a configuration decision into a structurally enforced invariant.

**Tampering (SQL injection).** ADR-0004 commits to parameterized queries throughout — correct, no gap.

**Tampering (FTS5 query-syntax injection — new finding, distinct from SQL injection).** A bound SQL parameter passed into an FTS5 `MATCH` expression is still parsed by FTS5's *own* mini-language (`AND`/`OR`/`NOT`, `NEAR()`, column filters like `title:`, prefix `*`, phrase-quoting rules). A user's own search string with an unbalanced `"` or a leading `NOT`/`-` can throw a syntax error; more significantly, if any *note-derived* text (a title or tag) is ever concatenated into an internal automated `MATCH` query (e.g. for "related notes" or dedup lookups) rather than passed as a fully-quoted literal, a maliciously-crafted title could suppress or skew what a system-internal query returns — a distinct poisoning vector from prompt injection, since it targets ATHENA AI-BRAIN's own retrieval logic rather than an LLM. **Recommendation:** always wrap literal terms in FTS5 string-quoting (doubling internal quotes) before they reach `MATCH`, rather than trusting raw user- or note-derived text to be well-formed FTS5 query syntax. **UNMITIGATED**, not addressed by any ADR.

**Information Disclosure (file permissions).** Both the metadata SQLite file and Huey's separate job-store file (ADR-0004's deliberate separation) will contain the full text of vault content (for FTS5) and job payloads as plaintext on disk. No ADR states a required file-permission mode (e.g. `0600` owner-only) for either file, or for the vault directory. *Severity: Medium* — cheap to close; see TB-3's note on the correct defense-in-depth framing for this. **UNMITIGATED.**

#### TB-8: Qdrant Vector Store (including collection-alias mutation)

**Information Disclosure (embedding inversion — OWASP LLM09:2026 "Vector and Embedding Weaknesses").** ADR-0006's `127.0.0.1`-only binding correctly closes the "Qdrant exposed to the network with no auth" risk (a real, actively-exploited pattern — unauthenticated Chroma/Qdrant/Milvus instances are routinely found via Shodan/Censys). **What no ADR addresses: the vectors themselves are not an opaque index.** Confirmed, active 2026 research demonstrates dense embeddings can be inverted — via optimization-based or learned-generative-model attacks — to reconstruct substantial portions of source text with high fidelity. **This is a specifically easier attack against ATHENA AI-BRAIN than most published research assumes**, because BGE-M3 (ADR-0008) is an open-weight, MIT-licensed model: an attacker who obtains the Qdrant data directory also has trivial white-box access to the exact encoder used, a strictly easier setting than the closed-API/black-box scenarios most inversion research targets. This means Qdrant's on-disk data directory is functionally a second copy of the vault's content and should be governed by the same backup-encryption and file-permission posture as the vault itself. *Severity: Medium* (requires local disk/backup access, consistent with the local-first threat model, but the *consequence* — full content reconstruction — is more severe than "vector database" intuitively suggests). **UNMITIGATED as a stated risk anywhere in current docs.**

**Tampering (collection-alias race — new finding).** ADR-0008 mandates collection access exclusively via an alias, but no ADR specifies *how* the alias swap during a model migration/reindex is implemented. If implemented as a non-atomic "check if alias exists → create/point it" sequence rather than Qdrant's atomic multi-action alias-update API, a crash-restart race or two overlapping reindex triggers could race to create/repoint the same alias, leaving search silently pointed at a stale or orphaned collection. **Recommendation:** mandate Qdrant's atomic `update_collection_aliases` call (a single request, never separate create-then-swap calls) plus a `huey.lock_task`-style mutual-exclusion guard around any alias-mutating operation, analogous to what ADR-0009 already requires for per-path indexing. **UNMITIGATED.**

**Denial of Service.** Not specifically addressed, but low-severity given local-only binding and single-user scale; Docker's `--restart unless-stopped` (ADR-0006) provides reasonable resilience.

#### TB-9: External LLM Provider APIs

**Information Disclosure (data exfiltration via `note_summarize`/`research_start`).** Every external LLM call sends vault content off-box by design. Tying to TB-2: if a prompt-injection payload can trigger `note_summarize` on arbitrary notes without confirmation, the external-LLM-call boundary becomes the actual exfiltration channel. Reinforces TB-2's recommendation to gate `note_summarize` behind explicit opt-in.

**Tampering (provider response trust).** ADR-0003's `Protocol`-based adapter (over full LiteLLM adoption) is the right call given LiteLLM's own 2026 compromise (TB-12) and separate exploited SQL-injection CVE. Worth stating explicitly: LLM provider responses must be treated as untrusted data when they flow back into any tool result (e.g. a `note_summarize` result written via `research_commit`) — the same TB-2 conflation risk one hop downstream, where `research_commit`'s `dry_run` default is doing double duty (guarding both untrusted web content and untrusted-in-the-injection-sense model output) — with the same overridable-default caveat noted in TB-2.

#### TB-10: Embedding/Reranker Model Artifacts

**Tampering (supply chain — OWASP LLM04:2026).** ADR-0008 commits to `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3` pulled via `sentence-transformers` from the Hugging Face Hub. Model weight files are, structurally, an untrusted artifact download exactly like a PyPI package, but no ADR discusses model-artifact integrity (hash pinning, verifying a specific revision rather than tracking a mutable `main` branch). *Severity: Low-Medium* — a less mature/observed attack class than PyPI compromise as of 2026, but structurally identical in mechanism (a repo-account compromise), and pinning a revision hash costs nothing. **UNMITIGATED**, not previously flagged anywhere.

#### TB-11: Secrets & Configuration

**Information Disclosure.** The principle (don't index API keys/credential files) is stated above and in CLAUDE.md/master spec §3, but the *mechanism* for how ATHENA AI-BRAIN holds provider API keys at runtime (OS keyring vs. `.env` vs. environment variable) is not decided by any ADR. Matters concretely for: (a) confirming `.env` is excluded from vault-watching scope (trivially true given the separation principle, but worth a positive confirmation rather than an assumption); (b) auditing that logging never captures API keys in Huey job payloads, SQLite audit rows, or debug-level outbound-LLM-request logs. **Status: principle-level only** — a Phase 1 design item, not necessarily a blocker.

#### TB-12: Python Dependency Supply Chain

Covered in depth below, as an ongoing operational practice rather than a one-time ADR decision. `chonkie` (ADR-0003) is added here explicitly: it touches 100% of ingested vault content before embedding, yet never received the CVE/maintainer-trust/release-cadence scrutiny GitPython, Dulwich, and LiteLLM each explicitly got during earlier Phase 0 research. It belongs in the same high-scrutiny tier.

#### TB-13: Huey Sync-Core/Async-Bridge Boundary (new)

ADR-0002 flags `aget_result()` — the bridge from ATHENA AI-BRAIN's asyncio runtime into Huey's synchronous job-execution core — as an integration risk to validate early in Phase 1, but no document treats it as a *security*-relevant boundary. **Concrete scenario:** a hung or slow bridged call (a stalled external-LLM call inside `research_start`/`note_summarize`, or a burst of jobs from the TB-1 DoS scenario) blocking on `aget_result()` inside a limited or shared thread pool can stall ATHENA AI-BRAIN's *entire* asyncio event loop — making the MCP server unresponsive to *all* concurrent tool calls, not merely delaying the one background job. No ADR specifies a timeout/cancellation policy for bridged calls or a dedicated bounded executor. **Recommendation:** bound every `aget_result()` call with an explicit timeout; run the bridge on its own dedicated executor isolated from other event-loop work, so a hung external-LLM call cannot starve MCP responsiveness. **UNMITIGATED.**

**Correction (Phase 10 audit, `docs/design/production-hardening.md` §0):** this finding's premise doesn't hold for the as-built system. `grep -rn "aget_result" src/` confirms `aget_result()` is never called anywhere in the codebase — every task-backed MCP tool (`research_start`, `reindex_start`, `duplicates_scan`) dispatches its Huey task fire-and-forget (e.g. `athena.worker.research_task(...)` is called directly and its `Result` handle returned immediately, never awaited), inserts a `research_jobs` row, and returns; the caller polls status separately via `job_status`. There is no blocking bridge call on the request path at all, so the specific stall scenario this entry describes cannot occur as designed. This is a safer pattern than ADR-0002 originally envisioned, arrived at organically during Phase 6/7 implementation rather than by deliberate threat-driven design. Left here, corrected rather than deleted, so the original reasoning remains visible — the underlying caution about bridging sync and async code was reasonable even though this specific bridging point turned out not to exist in practice.

### Dependency / Supply-Chain Security

The 2026 threat landscape validates ADR-0001/0003/0005's existing wariness and argues for treating supply-chain risk as continuous, not a Phase 0 checkbox:

- **Scale of the current threat, confirmed against the primary source:** H1 2026 produced 37 malicious-package campaigns and 497 indexed malicious packages across npm/PyPI — 2.6× the campaign count and 4.5× the package volume of all of 2025 combined (figures verified directly against the cited source; other trackers report different absolute counts due to differing methodology, which doesn't undermine this citation).
- **The LiteLLM compromise is precisely documented and confirmed**, directly informing ATHENA AI-BRAIN's pinning strategy. Two backdoored PyPI releases (1.82.7, 1.82.8) shipped 2026-03-24. Version 1.82.8's payload used a `.pth` file executing at Python interpreter startup, *before* any application import runs — meaning "we only narrowly/pinned-use litellm" is necessary but not sufficient: pinning doesn't help if the pinned version is itself compromised, and this mechanism specifically defeats "we don't import the vulnerable module" reasoning. Part of the "TeamPCP" campaign, chained through Trivy, an npm worm, Checkmarx/OpenVSX, LiteLLM, and Telnyx in a single week.
- **A second confirmed 2026 example reinforces the pattern:** Microsoft's own `durabletask` PyPI package was compromised 2026-05-19 via stolen publishing credentials (three malicious versions in a 35-minute window), harvesting credentials from AWS/Azure/GCP/Kubernetes/password managers — demonstrating that even a well-resourced, reputable maintainer's package can be compromised.

**Recommended operational practices (not one-time checks):**
1. **Dependency pinning via lockfile** (`uv.lock`) with exact-version pins for the full resolved tree, not just top-level dependencies.
2. **`pip-audit` (or equivalent) run in CI on every dependency change**, not just at initial setup — the direct analogue of the `gitleaks` pre-commit discipline already established for secrets.
3. **Manual diff review on every version bump of LLM-adjacent/high-privilege packages** (`litellm` if adopted, `sentence-transformers`, `qdrant-client`, `huey`, **`chonkie`**, any Git-automation library) — a higher-scrutiny tier than a pure-Python formatting utility.
4. **Delay-on-publish discipline**: avoid auto-upgrading to a version published in the last 24–72 hours, since 2026 campaigns show exploit windows often measured in hours.
5. **`.pth`-file/site-initialization awareness**: a periodic check of `site-packages/*.pth` contents against what's expected (most legitimate packages don't ship one) — cheap, concrete detection for the exact mechanism the LiteLLM compromise used.
6. **Monitor the Astral/`uv`/`ruff` acquisition risk** already flagged in ADR-0001 (OpenAI's 2026-03-19 acquisition) — "watch, don't block," in the same continuous-review cadence as the above.
7. **Model-weight pinning** (TB-10): pin `sentence-transformers` model loads to a specific Hugging Face revision hash, not a mutable branch.
8. **Extend `gitleaks`'s scope** beyond the pre-*commit* hook (ADR-0005) to run as a pre-*ingestion* check before vault content is written into SQLite/Qdrant — closes the gap this document names as "future" and currently has no ADR behind it.
9. **Give `chonkie` the same research treatment** GitPython/Dulwich/LiteLLM received: CVE history, maintainer trust, release cadence, startup-time code-execution risk — before Phase 1 relies on it for 100% of ingestion.

### Prioritized Remediation Checklist

**P0 — should block Phase 1 exit / must be resolved before any real vault is pointed at ATHENA AI-BRAIN:**
1. Decide and document the concrete path-traversal/symlink-safety mechanism (TB-3): canonicalize vault root once at startup; `Path.resolve(strict=True)` + ancestor (`.parents`) check on every incoming path; reject rather than silently clamp.
2. Close the confirmation gap on non-destructive *mutating* tools reachable via injected content (TB-2): gate `note_summarize` behind explicit user opt-in (resolves ADR-0007's open question); add structured provenance logging on any `research_commit`/`git_commit` following an LLM-reasoning step over retrieved content; consider requiring client-side (not model-side) re-confirmation before a non-dry-run `research_commit`.
3. Verify `python-frontmatter`'s YAML loading uses `yaml.safe_load` (or replace/wrap it if not) before it touches any vault content (TB-3).
4. Add a startup assertion that Huey's configured serializer is not the pickle default (TB-7) — an enforced invariant, not a convention.
5. Add file-permission hardening (owner-only, `0600`/`0700`) for the metadata SQLite file, the Huey job-store file, and the Qdrant data directory (TB-7, TB-8) — treat Qdrant snapshots as sensitive artifacts given the embedding-inversion finding.
6. Design and implement the pre-*ingestion* secret scanner named as "future" above (TB-4/TB-12 item 8) — pre-commit gitleaks alone fires too late.

**P1 — high priority, Phase 1 implementation:**
7. Explicitly decide MCP transport (stdio-only recommended for Phase 1) in a follow-up ADR; if HTTP is ever added, implement the full current-spec OAuth security controls before exposing it (TB-1).
8. Add a simple per-session/day cost-ceiling configuration value on any tool that calls an external LLM provider or dispatches jobs — scoped appropriately to a single user with one API key, not general-purpose rate-limiting infrastructure (TB-1, TB-9).
9. **Run the MCP server and/or Huey worker processes under a hardened systemd unit** (`DynamicUser=yes`, `ProtectHome=` scoped to read-only except the vault path, `ReadWritePaths=` narrowed to the vault and ATHENA AI-BRAIN's own data directories, `NoNewPrivileges=yes`, `ProtectSystem=strict`) as a defense-in-depth backstop for item 1 — explicitly framed as "what happens if the path-canonicalization fix has a bug," not a replacement for it (TB-3).
10. Wrap literal terms in FTS5 string-quoting before they reach any `MATCH` expression, especially for note-derived text used in internal automated queries (TB-7).
11. Mandate Qdrant's atomic `update_collection_aliases` call (never separate create-then-swap) plus a `huey.lock_task`-style mutual-exclusion guard around alias mutation (TB-8).
12. Research `chonkie`'s CVE history, maintainer trust, and release cadence to the same standard applied to GitPython/Dulwich/LiteLLM (TB-12).
13. Build the heuristic injection-pattern scanner ADR-0007 leaves open, as a flagged, logged, non-blocking signal (TB-2).
14. Set up `pip-audit` (or equivalent) in CI as a continuous check; add manual-review discipline for high-privilege dependency version bumps (TB-12).
15. Pin embedding/reranker model loads to a specific Hugging Face revision hash rather than a mutable branch (TB-10).
16. Design SSRF protections (private-IP blocking, HTTPS enforcement, no blind redirect-following) into the web-research ingestion feature's design doc *before* it's implemented (TB-4).
17. Decide and document the runtime secrets-handling mechanism (OS keyring vs. `.env`-only) and confirm outbound-LLM-request logging never captures API keys or full prompt content at a persisted log level (TB-11).
18. Bound every Huey `aget_result()` call with an explicit timeout and run the async bridge on its own dedicated executor, isolated from other event-loop work (TB-13).

**P2 — medium priority, should land during Phase 1 but not launch-blocking:**
19. Add structured audit logging for every mutating MCP tool call (who/what/when/provenance), addressing the Repudiation gap at TB-1.
20. Surface provenance/trust metadata (master spec §9) to the calling LLM in tool results, so `draft`/unverified-lifecycle content (master spec §11) can be structurally distinguished from `verified` content.
21. State explicitly whether pulled/merged Git remote content is subject to the same untrusted-input treatment as locally-created notes (TB-5), even if multi-machine use isn't imminent.
22. Add a periodic `site-packages/*.pth` content check as a concrete detection control against the LiteLLM-style compromise mechanism (TB-12).

**P3 — lower priority / ongoing monitoring, already correctly flagged by existing ADRs:**
23. Continue monitoring the Astral/OpenAI tooling-acquisition risk (ADR-0001) on its existing "watch, don't block" cadence.
24. Execute the Qdrant snapshot-before-upgrade runbook (ADR-0006, `GIT_WORKFLOW.md`) when the first version upgrade occurs.
25. Revisit the embeddings-model choice within the 6–12 month window ADR-0008 already commits to.

### References

- MCP Security Best Practices — modelcontextprotocol.io (2026-07-28 spec revision).
- OWASP Top 10 for LLM Applications 2026 (genai.owasp.org, published 2026-08-04) — confirmed current and correctly applied.
- MCP Security Statistics/Vulnerabilities 2026 (Practical DevSecOps, policyascode.dev) — 40+ CVEs disclosed against MCP implementations in 2026.
- MCP Security Notification: Tool Poisoning Attacks — Invariant Labs (2025), and follow-on 2026 corroboration.
- Simon Willison, "Model Context Protocol has prompt injection security problems" (2025-04-09).
- PoisonedRAG-style corpus-poisoning research; Document Injection research on RAG pipelines.
- Datadog Security Labs — LiteLLM/Telnyx PyPI compromise, TeamPCP campaign (confirmed against primary source).
- StepSecurity — Microsoft `durabletask` PyPI compromise (confirmed).
- Phoenix Security — 2026 H1 supply-chain campaign statistics (confirmed against primary source, verbatim figures).
- OpenStack Security Guidelines / HackerOne — path traversal prevention (`Path.resolve(strict=True)`, ancestor-based checks).
- Mend.io / GenAI Security Project — embedding-inversion research (confirmed as a real, active research line: Song & Raghunathan 2020 → Vec2Text/GEIA 2023 → ALGEN 2025 → Zero2Text 2026).
- ADR-0001 through ADR-0009, `ARCHITECTURE.md`, `DATA_MODEL.md`, `EVENT_MODEL.md` — internal primary sources.
