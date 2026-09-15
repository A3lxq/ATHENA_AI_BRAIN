# ATHENA AI-BRAIN

> **The LLM proposes. ATHENA AI-BRAIN validates. The vault remains the source of truth.**

ATHENA AI-BRAIN is a vendor-agnostic, event-driven AI Knowledge Operating
System built around an Obsidian vault. The vault is the authoritative
knowledge store; ATHENA AI-BRAIN reads it, watches it, indexes it, retrieves
from it, and creates/updates knowledge in it through controlled,
provenance-recording workflows — exposed to compatible AI clients through
one unified MCP server. It does not become a canonical copy of the user's
knowledge, and it stays a separate project from the vault it operates on.

## Quickstart

1. **Install.** ATHENA AI-BRAIN installs into a fixed, XDG-convention path
   (`~/.local/share/athena`). See
   [`deployment/README.md`](deployment/README.md)'s "Installing ATHENA
   AI-BRAIN itself" section for the exact clone/venv/`pip install -e .`
   steps.
2. **Configure.** Set the environment variables ATHENA AI-BRAIN reads at
   startup. At minimum:

   ```bash
   export ATHENA_VAULT_DIR=/path/to/your/obsidian/vault
   export ATHENA_HUEY_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
   ```

   There are several dozen more `ATHENA_*` variables covering Qdrant, Git
   automation, LLM providers, and rate limits, each with a sane local-first
   default — the complete, current list lives in
   [`src/athena/config.py`](src/athena/config.py)'s `load_config()`
   docstring (and will move to a dedicated Operations Guide reference table
   as that section grows).
3. **Initialize the database.**

   ```bash
   athena migrate
   ```
4. **Confirm health.**

   ```bash
   athena doctor
   ```
5. **Wire into an MCP client.** Point your MCP client (Claude Code, Claude
   Desktop, etc.) at ATHENA AI-BRAIN's sandboxed launch script rather than
   invoking Python directly — see
   [`deployment/README.md`](deployment/README.md)'s "Wiring the bubblewrap
   script into an MCP client" section for the details.

## CLI reference

Every top-level command family `athena` currently supports (run `athena
<family> --help` for full flag documentation):

| Command | What it does |
|---|---|
| `athena doctor` | Run diagnostics and report overall system health. |
| `athena version` | Print the installed ATHENA AI-BRAIN version. |
| `athena migrate` | Apply pending database migrations. |
| `athena ingest bootstrap` | One-time full-vault ingestion into the metadata database. |
| `athena ingest reconcile` | Run one on-demand reconciliation pass against the vault. |
| `athena index bootstrap` | Index every ingested note not yet indexed in Qdrant. |
| `athena retrieval evaluate` | Run the retrieval evaluation corpus and print recall/precision/MRR/latency metrics. |
| `athena duplicates scan\|list\|resolve\|merge` | Detect, review, and merge likely-duplicate notes. |
| `athena lifecycle stale-sweep` | Flag long-untouched active/verified notes as stale. |
| `athena research start\|commit` | Dispatch a background web-research job, then preview or write its draft into the vault. |
| `athena git status\|log\|commit\|push` | Inspect and drive the vault's Git repository (commit/push require an explicit confirmation flag). |
| `athena llm summarize` | Summarize a vault note via the configured default LLM provider. |
| `athena bench index\|retrieval\|mutation` | Benchmark indexing throughput, retrieval latency, and note-write/auto-commit round-trip time. |

## Architecture & design docs

For the full design rationale, data model, event model, and every
implementation-phase design document:

- [`docs/00_MASTER_PROJECT_SPECIFICATION.md`](docs/00_MASTER_PROJECT_SPECIFICATION.md) — the project's settled vision, scope, and principles.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system architecture.
- [`docs/design/`](docs/design/) — one design document per phase/feature, each with its own research, interfaces, failure modes, and test strategy.

## License

ATHENA AI-BRAIN is distributed under a custom personal-use license: free to
use and modify for personal, non-commercial purposes, with any distributed
modification required to either be contributed back to this repository or
kept strictly private. See [`LICENSE`](LICENSE) for the full terms.

## Contributing / Development

This package began as the ground-up development specification for ATHENA
AI-BRAIN, and the process it describes still governs how the project is
extended:

1. Read `CLAUDE.md` first — it defines the operating rules for any session
   working on this codebase.
2. Claude Code sessions must follow `docs/DEVELOPMENT_CONSTITUTION.md`.
3. Phase 0 (requirements, architecture, technology research) must be
   complete before implementation begins on a new area of scope.
4. Research technologies and write research artifacts before selecting
   them (`docs/research/`).
5. Use ADRs (`docs/adr/`) for every significant technical decision.
6. Implement bottom-up and test every layer before moving upward.
7. End every session by updating the continuity files (`CURRENT_STATE.md`,
   `NEXT_SESSION.md`, `CHANGELOG.md`, `SESSION_LOG.md`, a session file under
   `docs/sessions/`) — the repository is the project's memory, not the
   chat.
