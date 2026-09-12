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
15. `docs/design/git-automation.md` (Phase 8 — new this session; §0 has the git-behavior research, §6 the security considerations)
16. `docs/sessions/2026-09-10_phase7-research-ingestion.md`, `docs/sessions/2026-09-12_phase8-git-automation.md` (this session's own record)

## Objective

**Phase 0 through Phase 8 are all implemented and tested. Phase 7's work is already committed and pushed (`9b914dc`); Phase 8's work has not been committed yet.**

**What changed this session:**

- Drafted and got explicit acceptance for `docs/design/git-automation.md`, covering: the ADR-0005 subprocess wrapper, the `docs/GIT_WORKFLOW.md` auto-commit/push policy, and ADR-0007's four Git MCP tools.
- Built `athena.git.{wrapper,read,write}` directly (the highest-risk subprocess/mutation code) — a purpose-built `git` CLI wrapper: argv-only `asyncio.create_subprocess_exec`, `--end-of-options` insertion, a hand-written failure taxonomy, commit/push (the only two mutating operations, with no code path anywhere that can force-push/hard-reset/rewrite history), and `auto_commit_mutation` (the shared best-effort helper).
- Deployed two parallel agents against the now-fixed `athena.git` interface: one wired `auto_commit_mutation` into all 7 existing mutating MCP tools (`note_create`, `note_update`, `note_link`, `note_move`, `note_delete`, `note_merge`, `research_commit`); the other built `athena.mcp_server.git_tools` (`git_status`/`git_log`/`note_history`/`git_commit`), CLI wiring (`athena git {status,log,commit,push}`), `athena doctor`'s new `vault_git_repo` check, and `vault_status`'s new `git_ahead_by`/`git_last_commit` fields.
- Added a periodic, off-by-default, bounded-retry `git_push_task` to `athena.worker`.
- Closed a real, previously-flagged Phase-1-era gap: set up `.pre-commit-config.yaml` + `.gitleaks.toml` for **this software repository's own** commit hygiene (ADR-0005 required this in Phase 1; it was never actually done until now) — verified for real by downloading the pinned gitleaks binary and running it directly, then installing and running the actual `pre-commit` hook end-to-end, confirming it genuinely blocks a planted realistic secret.
- 561/561 tests passing (39 new), mypy --strict clean, ruff clean.
- **Live-verified independently** (not just trusted from the two building agents' own summaries): a real scratch vault + real local bare Git remote — `note_create`/`note_update` via a real in-memory MCP `ClientSession` both auto-committed for real; `athena git commit`/`athena git push` (CLI) correctly previewed by default and only wrote/pushed with an explicit flag; `athena doctor` showed `vault_git_repo: ok`; `vault_status` surfaced the new git fields correctly.

**Nothing from this session has been committed** — awaiting explicit user go-ahead (per standing practice: never commit without a separate, explicit request). Phase 7's work is already committed/pushed as `9b914dc`, and the earlier Docker/Qdrant-resolution session's work as `c080a9f`.

## Real findings from this session (verify-before-trust discipline)

1. **A real, empirically-verified `--end-of-options` placement gotcha, confirmed against the installed git 2.53.0**: the marker must sit immediately before the pathspec/ref list, never at the front of the whole argv — git treats every token after it as positional, so a real flag placed after it (e.g. `git checkout --end-of-options -b <ref>`) gets swallowed as a literal pathspec instead of parsed as a flag. `athena.git.wrapper.with_end_of_options` encodes this correctly (fixed args, then the marker, then variable args); every call site in `read.py`/`write.py` uses it, never a hand-assembled argv with a variable component inlined among flags.
2. **A second real git quirk, found by an actual failing test, not assumed**: `git commit` failing with "nothing to commit, working tree clean" prints that message to **stdout** with an **empty stderr** — the opposite channel from every other failure case (auth/network/conflict are all on stderr). `classify_git_failure` was initially stderr-only and misclassified this as `UNKNOWN`; fixed to check stdout specifically (and only) for this one marker.
3. **`git push --set-upstream <remote>` (relying on the current branch by omission) fails with "no upstream branch" even against a brand-new remote** — `--set-upstream` only works combined with an *explicit* branch name. Without this, a freshly-`git remote add`-ed vault would need an undocumented one-time manual `git push -u origin <branch>` before this module's `push()` ever worked. Fixed: `push()` reads the current branch via `git rev-parse --abbrev-ref HEAD` (never caller-supplied) and always passes it explicitly alongside `--set-upstream` — a harmless no-op re-affirmation once a branch is already tracked.
4. **A real, previously-flagged gap that turned out to still be open, checked rather than assumed fixed**: ADR-0005's own "Consequences" section required `.pre-commit-config.yaml` + gitleaks be set up in Phase 1. Checked directly — neither the config file nor the `pre-commit`/`gitleaks` tools existed anywhere in this environment. Closed this session, and verified genuinely working (not just written): the pinned gitleaks v8.30.1 binary was downloaded and run directly against this repo's real tracked git history (zero false positives; the `.gitleaks.toml` allowlist correctly suppresses a planted test private key in `tests/security/test_secrets.py` that gitleaks' own default ruleset does flag on a filesystem scan), then `pre-commit` itself was installed and the real hook run end-to-end, confirmed to genuinely block a planted realistic-looking (non-`EXAMPLE`) AWS secret while correctly ignoring this project's existing `EXAMPLE`-suffixed test fixtures.
5. **A genuine dispatch-ordering-adjacent design decision, made explicitly rather than left implicit**: Dulwich (ADR-0005's optional, non-load-bearing read-side convenience) was deliberately NOT adopted — every operation this phase needs is a single, simple subprocess call, and adding a second Git-access code path would mean two things to keep behaviorally consistent for zero functional gain. Documented as a considered "no," not an oversight.
6. **ATHENA AI-BRAIN deliberately never auto-`git init`s the vault** — even though `git init` isn't destructive, silently creating version-control state inside a user's personal directory without being asked is presumptuous. `athena doctor`'s new `vault_git_repo` check (`warn`, not `fail`) and every auto-commit call path degrade to a clear, logged no-op instead.
7. **`git_commit`'s MCP-tool default is `dry_run=False`** (a real commit happens if the parameter is omitted) — the opposite of `research_commit`'s `dry_run=True` default. This is intentional (append-only, non-destructive, not MRTR-gated per ADR-0007's classification), but easy to get backwards by copying the `research_commit` pattern; there's an explicit test asserting this exact default. The CLI layer, unlike the MCP tool, still requires an explicit `--commit`/`--push` flag (mirroring `research commit`'s CLI precedent), since the CLI has no elicitation fallback — CLI and MCP-tool defaults intentionally differ here.
8. **`merge_notes` (Phase 5) does not delete the absorbed note's file from disk** — only soft-deletes the DB row and transitions status to `'superseded'`; the physical file remains in the vault. Found while wiring `note_merge`'s auto-commit (which correctly only commits `[keep_path]`, never the absorbed path) — not a Phase 8 bug, a pre-existing Phase 5 behavior worth knowing: a merge leaves a stale, superseded file lingering in the vault unless something else cleans it up. Not fixed in this pass (out of Phase 8's scope), just flagged.

## What is genuinely still missing before Phase 9 starts

1. **`fastembed` revision-pinning**, **`watchdog` supply-chain review** — both still open, unaffected by this session.
2. **The retrieval-evaluation corpus** still ships at 10 notes/17 questions — unblocked since the Docker/Qdrant session, still not expanded.
3. **The duplicate-detection default thresholds are untuned against real vault data** — likewise unblocked, not yet tuned.
4. **`note_create`/`note_update` content is still not secret-scanned before being written** — unaffected by Phase 8; still the original Phase 6 gap.
5. **`note_summarize`** — the sole remaining explicitly deferred MCP tool, blocked on Phase 9's multi-LLM adapter.
6. **A full wire-level elicitation round-trip integration test** — still not built for any MRTR-gated tool.
7. **`git pull`/sync workflow** and the untrusted-content handling it would need — explicitly out of scope for Phase 8 (a "backup workflow," not a two-way sync); if a future phase adds pulling, treat arriving content exactly as untrusted as a locally-created note.
8. **Merge-conflict *resolution* tooling** — Phase 8 detects and reports a conflict but never auto-resolves one; a permanent, explicit non-goal.
9. **CI-side `gitleaks` scanning** (full history/PR-diff, beyond the local pre-commit hook) — not set up in this pass.
10. **A structural CI check enforcing "only `run_git`'s argv-list path is ever used"** — a reasonable follow-up, not built.
11. Real install/venv path decision for the deployment configs — still the only remaining blocker before the systemd/bubblewrap configs are actually usable.
12. Adding `secret_findings_list`/`secret_finding_resolve` to ADR-0007's MCP tool contract table (still open from ADR-0011).
13. **The Qdrant container is still a manually-started, unmanaged Docker container** (no systemd unit), unaffected by this session.
14. **`merge_notes` leaves the absorbed note's file physically in the vault** after a merge (finding 8 above) — flagged, not fixed; a reasonable follow-up for a future session.

## Do not

- place `--end-of-options` anywhere but immediately before the pathspec/ref list — always use `athena.git.wrapper.with_end_of_options(fixed_args, variable_args)`, never hand-assemble a git argv with a variable component inlined among flags,
- classify a `git commit` "nothing to commit" outcome by checking stderr alone — it's on stdout with an empty stderr; `classify_git_failure` already handles this, don't regress it,
- call `athena.git.write.push()` expecting it to work against a freshly-added remote without `--set-upstream` — it already always passes this (with the real current branch, never caller-supplied), don't remove it,
- have `athena.git.write` (or anything else) implement force-push, hard-reset, or history rewriting — this is an API-level guarantee, not a convention; don't add a `force` parameter to `push()` or a destructive helper to this module even if a future feature seems to want one — surface it as a human-operated CLI-only action instead, per CLAUDE.md rules 22-23,
- have any code path auto-`git init` a vault — check `is_git_repository()` and degrade to a clear, logged no-op instead,
- assume `note_create`/`note_update` are secret-scanned just because `research_commit` and (still) not `git_commit`-adjacent write paths are — they aren't; that's still the original Phase 6 gap,
- assume `merge_notes` deletes the absorbed note's file from disk — it doesn't (finding 8 above); don't design new logic that assumes a merged-away file is actually gone,
- run `git remote set-url`/`git config` in this environment on the user's behalf — check `git remote -v` before assuming the local `origin` remote has or hasn't already been fixed,
- commit this session's Phase 8 work without checking with the user first — nothing has been committed yet by design (Phase 7's `9b914dc` and the Docker/Qdrant session's `c080a9f` are already pushed, don't re-commit or duplicate them).
