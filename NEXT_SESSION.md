# ATHENA AI-BRAIN — Next Session

## Start Here

Read, in order:

1. `CLAUDE.md`
2. `docs/DEVELOPMENT_CONSTITUTION.md`
3. `CURRENT_STATE.md`
4. `docs/00_MASTER_PROJECT_SPECIFICATION.md`
5. `docs/ARCHITECTURE.md`
6. `docs/adr/0001-*.md` through `docs/adr/0011-*.md` (all Accepted)
7. `docs/DATA_MODEL.md`, `docs/EVENT_MODEL.md`, `docs/SECURITY_MODEL.md`, `docs/LONGEVITY_NOTES.md`, `docs/GIT_WORKFLOW.md`
8. `docs/design/vault-safety-boundary.md`, `docs/design/os-level-process-sandboxing.md`, `docs/design/storage-runtime-hardening.md`, `docs/design/pre-ingestion-secret-scanning.md`
9. `docs/design/migration-runner-and-vault-ingestion.md` (Phase 2)
10. `docs/design/indexing-pipeline.md` (Phase 3)
11. `docs/design/retrieval-pipeline.md` (Phase 4)
12. `docs/design/knowledge-intelligence.md` (Phase 5)
13. `docs/design/mcp-server.md` (Phase 6)
14. `docs/design/research-ingestion.md` (Phase 7)
15. `docs/design/git-automation.md` (Phase 8)
16. `docs/design/multi-llm.md` (Phase 9 — new this session; §0 has the SDK/litellm research, §6 the security considerations)
17. `docs/sessions/2026-09-12_phase8-git-automation.md`, `docs/sessions/2026-09-13_phase9-multi-llm.md` (this session's own record)

## Objective

**Phase 0 through Phase 9 are all implemented and tested — every phase named in `docs/ROADMAP.md` through Phase 9 now exists.** Phase 8's work is already committed and pushed (`64bc1c8`); Phase 9's work has not been committed yet.

**What changed this session:**

- Drafted and got explicit acceptance for `docs/design/multi-llm.md`, resolving ADR-0003's deferred multi-provider LLM adapter decision and ADR-0007's `note_summarize` tool — the last tool in ADR-0007's original contract table left unbuilt.
- Re-verified `litellm`'s security posture directly against GitHub's advisory database rather than trusting ADR-0003's 3-week-old characterization: found it has gotten *worse*, not better (a 2026-03-25 supply-chain compromise plus a sustained run of critical proxy CVEs through the year) — reaffirmed the hand-rolled `Protocol`-based adapter decision.
- Verified every provider SDK's real call shape (`AsyncOpenAI.responses.create`, `AsyncAnthropic.messages.create`, `google.genai`'s `.aio.models.generate_content`, `ollama.AsyncClient.chat`) via `inspect.signature()` against the actually-installed packages, and confirmed all four are genuinely async (safe to call directly from an async MCP tool handler, unlike the sync-`asyncio.run()`-inside-async-handler bug Phase 6 found and fixed).
- Built `athena.llm.provider` (the four provider implementations + a uniform `asyncio.wait_for` timeout wrapper) and `athena.llm.summarize` (the opt-in gate, provider resolution, daily call ceiling, and audit logging orchestration) directly, in one pass (the surface area didn't warrant parallel agents this time).
- Built `athena.mcp_server.llm_tools` (`note_summarize`) and `athena llm summarize` (CLI), both gated behind the same `summarize_text` entry point.
- Found and fixed a real bug during testing: `config.ollama_base_url` always has a truthy default, unlike the other three providers' `None`-unless-set API key fields — this would have made Ollama look "configured" in every environment, including ones with no local server at all. Fixed with a live reachability check for the still-default URL (an explicit override is still trusted outright, matching the design doc's own already-anticipated fix).
- 589/589 tests passing (28 new), mypy --strict clean, ruff clean.
- **Live-verified against a real local LLM, not just mocks**: pulled a small Ollama model (`qwen2.5:0.5b`) since this environment's two pre-existing local models were both 35B-parameter and impractically slow on CPU-only hardware — ran a real summarization through both the CLI and a real in-memory MCP `ClientSession`, confirmed the response framing, the audit event's correct (content-free) payload, and both the opt-in gate and the daily call ceiling refusing cleanly for real. The test-pulled model was removed afterward.

**Nothing from this session has been committed** — awaiting explicit user go-ahead. Phase 8's work is already committed/pushed as `64bc1c8`, Phase 7's as `9b914dc`.

## Real findings from this session (verify-before-trust discipline)

1. **`litellm`'s security posture, re-checked against primary sources rather than trusted from an 18-day-old ADR**: the GitHub advisory list for `BerriAI/litellm` shows 10 advisories for 2026 alone, the majority proxy-scoped (SSRF, a Critical host-header auth bypass, an MCP auth bypass, a sandbox escape, a second Critical SQL injection) — a sustained pattern across the whole year, not the single resolved incident ADR-0003 originally cited. The supply-chain compromise (GHSA-5mg7-485q-xm76) would have affected any installation regardless of proxy usage. Reaffirmed: hand-roll.
2. **A real, empirically-verified async-safety finding**: all four provider SDKs' async clients (`AsyncOpenAI`, `AsyncAnthropic`, `google.genai`'s `.aio` namespace, `ollama.AsyncClient`) are genuinely async (httpx-based), confirmed by direct inspection — unlike Phase 6's `run_doctor()` bug (an internal *synchronous* `asyncio.run()` call), calling these directly from an async MCP tool handler needs no `asyncio.to_thread` wrapping. What still matters: each SDK's per-call timeout parameter shape differs (Ollama's `chat()` has none at all), so every call is wrapped in `asyncio.wait_for(..., timeout=...)` uniformly at the adapter layer regardless.
3. **A real bug found by a failing test, not by inspection**: `_configured_providers` initially treated `config.ollama_base_url` as "configured" simply because the field had a value — but that field always has a truthy default (`http://localhost:11434`), unlike the other three providers' `None`-unless-set fields. A test expecting "no provider configured" instead got a real (failing) attempt to call the real local Ollama server this dev environment happens to have running. Fixed: an explicitly-overridden URL is trusted outright; a still-default URL counts as configured only if a live reachability check (a real HTTP call to `/api/tags`, checked fresh each time, never cached) actually succeeds.
4. **Current model names, verified directly against each provider's own live documentation, not guessed**: OpenAI's `gpt-6-astra`/`gpt-5.6-terra`/`gpt-5.6-luna` tiering (from `developers.openai.com/api/docs/models`) and Google's `gemini-3.8-flash` (from `ai.google.dev/gemini-api/docs/models/gemini-3.8-flash`) were both confirmed via direct web fetches against primary-source pages before being used as this design's default model values — an initial round of search results surfaced what looked like low-quality SEO-aggregator content for the same claims, which was deliberately not trusted until corroborated by the providers' own doc pages.
5. **Ollama's own local resource reality**: this environment already had two local models pulled (`qwen3.6:latest`, `ornith:35b-q8_0`), both ~35B parameters running on 100%-CPU inference (no GPU detected) — genuinely too slow (30s+ per call, sometimes much more) for a practical live-verification pass. A small model (`qwen2.5:0.5b`) was pulled specifically for this session's live check and removed afterward; the two pre-existing models were left untouched.

## What is genuinely still missing before Phase 10 starts

Every phase named in `docs/ROADMAP.md` (Phases 1 through 9) is now implemented. Remaining open items, none of them phase-blocking:

1. **`fastembed` revision-pinning**, **`watchdog` supply-chain review** — both still open.
2. **The retrieval-evaluation corpus** still ships at 10 notes/17 questions — unblocked since the Docker/Qdrant session, still not expanded.
3. **The duplicate-detection default thresholds are untuned against real vault data** — likewise unblocked, not yet tuned.
4. **`note_create`/`note_update` content is still not secret-scanned before being written** — the original Phase 6 gap, unaffected by Phase 9.
5. **A full wire-level elicitation round-trip integration test** — still not built for any MRTR-gated tool.
6. **`git pull`/sync workflow**, **merge-conflict resolution tooling** — both explicitly out of scope for Phase 8, the latter permanently.
7. **CI-side `gitleaks` scanning** and **a structural "only `run_git`/only official-SDK-calls" CI check** — reasonable follow-ups, not built.
8. **Model routing** and **a real per-provider dollar-cost ceiling** — both explicitly judged not-yet-justified for Phase 9's single call shape; revisit if a second LLM-calling use case emerges.
9. **Multi-source synthesis** (combining several notes/research drafts via an LLM) — explicitly out of scope for both Phase 7 and Phase 9; a reasonable future enhancement once a real use case names the exact tool contract for it.
10. Real install/venv path decision for the deployment configs — still the only remaining blocker before the systemd/bubblewrap configs are actually usable.
11. Adding `secret_findings_list`/`secret_finding_resolve` to ADR-0007's MCP tool contract table (still open from ADR-0011).
12. **The Qdrant container is still a manually-started, unmanaged Docker container** (no systemd unit).
13. **`merge_notes` leaves the absorbed note's file physically in the vault** after a merge (a Phase 5 behavior, found while wiring Phase 8's auto-commit) — flagged, not fixed.
14. **Phase 10 — Production Hardening** itself has not been researched or designed yet; it's the next phase per the roadmap once the above is triaged with the user.

## Do not

- treat "the config field has a value" as "the provider is configured" for Ollama specifically — `ollama_base_url` always has a truthy default; only an explicit override or a live reachability check counts (finding 3 above),
- assume any provider SDK needs `asyncio.to_thread` wrapping the way `run_doctor()` did — all four are genuinely async; the thing that still matters is the uniform `asyncio.wait_for` timeout, not blocking-call safety,
- call `athena.llm.provider`'s `fetch_url()`/SDK-native fetch equivalents directly, or add a second network-egress path for LLM calls — every provider call goes through exactly one `complete()` method per provider, always timeout-wrapped,
- assume `note_summarize` is MRTR-gated — it isn't, by design (read-only, non-destructive per ADR-0007's classification); the opt-in gate (`ATHENA_LLM_ENABLED`) and the daily call ceiling are the controls, not a confirmation prompt,
- log or persist prompt/response content anywhere in `athena.llm` — only `source_label`/provider/model/`truncated` ever reach the `events` table or a log line,
- treat the per-provider default model names (`gpt-5.6-terra`, `gemini-3.8-flash`, `claude-sonnet-5`, `llama3.1`) as pinned commitments — they will drift; `ATHENA_LLM_MODEL` always overrides,
- run `git remote set-url`/`git config` in this environment on the user's behalf without being asked — check `git remote -v` before assuming the local `origin` remote is correct (a real typo, `ATHENA_AI_GRAIN`, was found and fixed this way in the prior session; don't assume it can't happen again),
- commit this session's Phase 9 work without checking with the user first — nothing has been committed yet by design (Phase 8's `64bc1c8` and Phase 7's `9b914dc` are already pushed, don't re-commit or duplicate them).
