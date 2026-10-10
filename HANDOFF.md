# ATHENA AI-BRAIN — Handoff (summary + continuation prompt)

Written 2026-10-10 so a fresh chat can resume with no memory of earlier
sessions. For deeper detail, the repository is the source of truth:
`CURRENT_STATE.md`, `NEXT_SESSION.md`, `SESSION_LOG.md`, `docs/sessions/`.

## Part 1 — Summary

### What it is
ATHENA AI-BRAIN is a vendor-agnostic, event-driven AI Knowledge Operating
System built around an Obsidian vault. It has no UI. It is a Python CLI
(`athena`) plus one unified MCP server (stdio only, ADR-0012) that an AI
client such as Claude Code calls. The vault stays the source of truth;
ATHENA indexes, searches and changes it under safety rules.

Core rule: the LLM proposes, ATHENA AI-BRAIN validates, the vault remains
the source of truth.

### Capabilities
- Hybrid search: SQLite FTS5 keyword search plus Qdrant semantic search
  (BGE-M3 embeddings), fused and reranked.
- Note create, update, link, move, delete and merge. Destructive operations
  need the caller to re-type the exact target (MRTR confirmation).
- Duplicate detection (4 signals), reviewed merges, stale-note sweep,
  related notes, provenance.
- Research: fetch URLs (SSRF-safe), extract to a draft, preview, then
  commit. Nothing enters the vault until committed.
- Git automation: auto-commit per change, status/log/commit/push. No
  force-push or hard reset anywhere.
- Optional LLM summarization (OpenAI, Anthropic, Google, Ollama), off by
  default, with a daily call limit.
- Operations: `athena doctor`, Huey job queue, audit events (including
  refused attacks), `athena bench`.

### Release state
- Latest release: **v1.1.1** (carries the new license; v1.1.0 is the previous release).
- **v1.0.0** was relabeled "v1.0.0 (Beta)" and marked a prerelease (the git
  tag itself is untouched).
- v1.1.1 (2026-10-10) is the first release under the new personal-use,
  no-modification license. v1.0.0 and v1.1.0 were published under the
  older contribute-back-or-keep-private terms.
- Tests: 809 passed, 2 expected failures (xfail). ruff and mypy clean.
- CI: GitHub Actions on ubuntu-latest, Python 3.12, installs from `uv.lock`.

### History in one paragraph
Phases 0-11 of `docs/ROADMAP.md` are done (architecture and ADRs, vault
engine, indexing, retrieval, duplicate detection, MCP server, research,
git automation, multi-LLM, production hardening, v1.0 release). After
v1.0.0, an authorized OWASP Top 10:2025 / OWASP LLM Top 10 2025
penetration-test audit (6 pentest agents, 6 fix agents) found and fixed 13
real issues, including a critical race that let the daily dispatch ceiling
be bypassed. Full detail: `docs/sessions/2026-10-03_owasp-security-audit-and-fixes.md`.

### License
Custom "personal use only, no modifications of any kind, no
redistribution, no commercial use" license (changed 2026-10-10). Not
lawyer-reviewed; the file says so.

### Known open items (none block use)
- `reconcile_vault` does not repair a crash that happens after a note row
  is written but before its provenance is saved (tracked as an xfail).
- `git_commit` stages everything dirty with no confirmation. Accepted
  design tradeoff (`SECURITY_MODEL.md` TB-2); do not change without asking.
- Qdrant has no API-key auth (loopback only, ADR-0006). Also accepted.
- `fastembed`/miniCOIL cannot be revision-pinned upstream.
- CI does not run gitleaks. The `ReadWritePaths=` vault-path placeholder in
  the systemd/bubblewrap configs is unresolved.
- Retrieval-eval set is small (10 notes / 17 questions); duplicate
  thresholds are untuned on real data.
- `note_create` / `note_update` content is not secret-scanned on write.
- Linux only. Windows users need WSL2 (git wrapper, permission hardening
  and sandboxing are POSIX-specific).

### Working rules to keep following (from `CLAUDE.md`)
- Research before implementing, design before coding, ADR per significant
  decision, tests for every change, keep docs in sync.
- Never commit, push, tag or edit a published release without the user's
  explicit go-ahead for that action.
- Verify claims against code or primary sources instead of assuming.
- Update `CURRENT_STATE.md`, `NEXT_SESSION.md`, `CHANGELOG.md`,
  `SESSION_LOG.md` and add a `docs/sessions/` file at the end of each
  meaningful session.

### Environment gotchas learned the hard way
- The dev machine runs Python 3.14 but the project targets 3.12 and CI
  pins 3.12. Stdlib behavior differs (for example `Path.is_symlink`), so
  trust CI over local runs for stdlib edge cases.
- Run tests with plain `pytest -q` as CI does; `python -m pytest` hides
  `sys.path` problems.
- Bump the version in both `pyproject.toml` and `src/athena/__init__.py`,
  then re-run `uv lock`.
- Never run `uv sync` inside the real repo without `--extra dev`; it
  removes ruff, mypy and pytest from `.venv`. Use an isolated copy to test
  lockfile installs. Restore with `pip install -e ".[dev]"`.
- Each shell call starts fresh, so activate `.venv` and `cd` every time.

## Part 2 — Continuation prompt (copy everything in the block)

```text
You are continuing work on ATHENA AI-BRAIN, a Python CLI + stdio MCP
server over an Obsidian vault, in /home/immanuvel/Claude_conversations/ATHENA_AI_BRAIN
(GitHub: A3lxq/ATHENA_AI_BRAIN, branch main).

Before doing anything, read in this order:
1. CLAUDE.md (operating rules, follow them strictly)
2. HANDOFF.md (summary of everything so far)
3. CURRENT_STATE.md and NEXT_SESSION.md
4. docs/SECURITY_MODEL.md and docs/sessions/2026-10-03_owasp-security-audit-and-fixes.md

Current state: latest release is v1.1.1 (personal use only, no
modifications license), v1.0.0 is relabeled a beta prerelease. 809 tests pass, 2 expected xfails, ruff and
mypy clean.

Rules:
- Do not commit, push, tag or edit releases unless I explicitly ask for
  that specific action.
- Verify before trusting: run `source .venv/bin/activate && ruff check src
  tests && mypy src && pytest -q` and confirm the numbers above first.
- Do not silently redesign accepted decisions (git_commit has no MRTR,
  Qdrant has no API key, stdio-only transport). Ask me first.
- For anything new: research, then a short design, then my approval, then
  implementation with tests. Update the continuity docs at the end.

First message back to me: confirm what you read, report the verified test
status, and list the open items from HANDOFF.md so I can choose what to do
next. My options include: the reconcile_vault crash-recovery gap, adding gitleaks to CI, expanding the
retrieval-eval set, or something new.
```
