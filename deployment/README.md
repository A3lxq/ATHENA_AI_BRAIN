# ATHENA AI-BRAIN Deployment Artifacts

Status: **Configuration artifacts, not yet a fully working deployment for a
specific vault.** The venv/install-path placeholder both files used to
contain is now resolved (§"Installing ATHENA AI-BRAIN itself" below); the
vault-path placeholder in the systemd unit's `ReadWritePaths=` is still open
(see "Open items" below). Read this whole file before using either artifact.

Full design rationale, threat model, and test strategy for everything here:
[`docs/design/os-level-process-sandboxing.md`](../docs/design/os-level-process-sandboxing.md).
This README is operational instructions only — it does not repeat that
document's reasoning.

## What these two artifacts are

ATHENA AI-BRAIN has two processes with structurally different lifecycles (design
doc §2), so each gets a different sandboxing mechanism:

| Process | Launched by | Sandboxed with | File |
|---|---|---|---|
| **MCP server** (stdio) | The MCP client itself (Claude Code / Claude Desktop), as a child process | `bwrap` (bubblewrap) wrapper script | `bubblewrap/athena-mcp-launch.sh` |
| **Huey worker** | `systemd --user`, independent background daemon | `systemd --user` unit hardening directives | `systemd/athena-huey-worker.service` |

The MCP server can't be a systemd unit because the MCP client needs to own
its stdin/stdout directly for protocol framing (design doc §2). The Huey
worker is a genuine long-running daemon, which is exactly what `systemd
--user` is for. Both processes reach the same internal business-logic layer
(`docs/ARCHITECTURE.md` §2), so their filesystem access needs are ~90%
identical — see the design doc §3.4 for why the directive blocks look so
similar across the two files.

## Installing ATHENA AI-BRAIN itself

Both artifacts below assume ATHENA AI-BRAIN is installed at a fixed,
documented path following the XDG Base Directory convention this project's
own `athena.config.DEFAULT_DATA_DIR` already uses for runtime state
(`~/.local/state/athena`): the venv and a checkout of the source live under
`~/.local/share/athena`, XDG's directory for installed application data
(deliberately a different XDG directory than `.local/state`, not a
copy-paste of it). Do this once, before installing either the systemd unit
or the bubblewrap script:

```bash
mkdir -p ~/.local/share/athena
git clone https://github.com/A3lxq/ATHENA_AI_BRAIN.git ~/.local/share/athena/src
cd ~/.local/share/athena/src
python3 -m venv ~/.local/share/athena/.venv
~/.local/share/athena/.venv/bin/pip install -e .
```

This is the real, resolved path both `athena-huey-worker.service`'s
`ExecStart=` and `athena-mcp-launch.sh`'s `VENV` variable now point at (see
"Open items" below).

## Installing the Huey worker systemd unit

1. Copy the unit file into your user systemd directory:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp deployment/systemd/athena-huey-worker.service ~/.config/systemd/user/
   ```

2. **Before enabling it**, confirm the copied file's `ExecStart=` line
   (`%h/.local/share/athena/.venv/bin/python -m athena.worker`) matches
   where you actually installed ATHENA AI-BRAIN in the step above. The
   venv-path placeholder this section used to warn about is now resolved
   (see "Open items" below) — the vault-path placeholder in
   `ReadWritePaths=` is a separate, still-open item and does still need
   confirming before enabling.

3. Reload and enable:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now athena-huey-worker.service
   ```

4. Check status and logs:

   ```bash
   systemctl --user status athena-huey-worker.service
   journalctl --user -u athena-huey-worker.service -f
   ```

5. Optional — keep the worker running after logout (design doc §2's "System
   unit vs. user unit" section):

   ```bash
   loginctl enable-linger "$USER"
   ```

6. Optional regression check, per the design doc's §7.1 test #3 — run before
   any deploy and track the score over time:

   ```bash
   systemd-analyze security athena-huey-worker.service
   ```

## Wiring the bubblewrap script into an MCP client

`bubblewrap/athena-mcp-launch.sh` is meant to be the `command` an MCP
client invokes to start ATHENA AI-BRAIN's MCP server, instead of invoking Python
directly. The exact config file format varies by client (Claude Code's and
Claude Desktop's MCP server configuration formats differ from each other and
change over time), so this is described conceptually rather than as a
copy-paste snippet:

- Point the client's MCP server entry's `command` at the **absolute path**
  to `athena-mcp-launch.sh` (not at the Python interpreter or module
  directly) — this is what makes the server process actually start inside
  the bubblewrap sandbox rather than unsandboxed.
- Pass no `args` beyond what the script itself already hardcodes; the script
  takes no CLI arguments (it reads configuration from environment variables
  — see below).
- Set `ATHENA_VAULT_DIR` in the environment the MCP client uses to launch
  the server (client configs typically support an `env` map for exactly
  this). The script fails fast (`set -euo pipefail` + `${ATHENA_VAULT_DIR:?...}`)
  if this is missing, rather than silently running unsandboxed or against
  the wrong path.
- Do not rely on the client inheriting your interactive shell's environment
  — the script itself calls `--clearenv` inside the sandbox, by design
  (design doc §3.4: prevents leaking inherited API keys / SSH agent socket
  paths into the sandboxed process), so anything the sandboxed process needs
  must be passed explicitly via `--setenv` inside the script, not assumed to
  arrive from outside.
- This project's own MCP server (`athena.mcp_server`, `python -m
  athena.mcp_server`) now exists (Phase 6, docs/design/mcp-server.md), and
  the venv/install-path placeholder is now resolved (see "Open items"
  below) — the remaining blocker before this wiring can be enabled for a
  specific vault is confirming the vault path itself, not the install path.

## Required environment variables

| Variable | Required by | Purpose |
|---|---|---|
| `ATHENA_VAULT_DIR` | `athena-mcp-launch.sh` | Absolute path to the Obsidian vault; bind-mounted read-write into the sandbox and passed through as the same env var inside it. Script aborts if unset. |
| `HOME` | both artifacts | Used to derive `STATE_DIR` (`$HOME/.local/state/athena`), `MODEL_CACHE` (`$HOME/.cache/huggingface`), and the `.ssh`/`.aws`/`.gnupg` lockout paths in the bubblewrap script; `%h` in the systemd unit resolves the same way for that process. Always set by the OS/login session — not something you need to export yourself. |

No other environment variables are read by either script. Anything else the
MCP server process needs at runtime must be added explicitly as a
`--setenv` line in the bubblewrap script (it will not be inherited — see
above).

## Open items — placeholders that must be updated before real use

Both entry points these artifacts launch now exist as real, tested code
(`athena.worker` since Phase 2, `athena.mcp_server` since Phase 6). One of
the two standing deployment placeholders is now resolved; the other is
still genuinely open:

1. **Venv/install path — RESOLVED.** `athena-huey-worker.service`'s
   `ExecStart=` and `athena-mcp-launch.sh`'s `VENV` variable both now point
   at `~/.local/share/athena/.venv` (`%h/.local/share/athena/.venv/bin/
   python` in the systemd unit), a fixed, documented path following the XDG
   Base Directory convention this project's own `athena.config.
   DEFAULT_DATA_DIR` already uses for runtime state (`~/.local/state/
   athena`) — `.local/share` is XDG's directory for installed application
   data, deliberately distinct from `.local/state`. See "Installing ATHENA
   AI-BRAIN itself" above for the install steps that create this path.
2. **Vault path placeholder — still open.** The systemd unit's
   `ReadWritePaths=` uses
   `%h/ObsidianVault` as a stand-in; confirm this against wherever the vault
   actually ends up living (per CLAUDE.md rule 13, the vault stays separate
   from ATHENA AI-BRAIN's own repo/install location) before enabling the unit.

## Known open questions (carried forward from the design document)

These are unresolved as of the design document and are **not** decided by
these deployment artifacts — copied here so they aren't lost between
sessions (per CLAUDE.md's session-continuity rules):

- Exact vault path templating mechanism for `ReadWritePaths=` — an
  install-time unit-generation step is implied but not yet designed.
- Whether `~/.gitconfig` access is actually needed by the Git Automation
  Module once built — an empirical Phase 1 check, not a design-time
  decision. If it is needed, `~/.gitconfig` is **not** covered by default
  under this unit's `ProtectHome=yes` and must be added as a narrowly-scoped
  `ReadOnlyPaths=` exception, or the module should be changed to run with
  `GIT_CONFIG_GLOBAL=/dev/null` plus explicit `-c` flags instead.
- Whether `MemoryDenyWriteExecute=yes` (currently commented out in the
  systemd unit) survives contact with the actual embedding pipeline
  (sentence-transformers / PyTorch) — empirical, gated behind the design
  doc's §7.1 test #2. Do not enable it without running that test first.
- Whether the systemd unit duplication implied by having near-identical
  directive blocks across the MCP-server and Huey-worker configurations
  should be refactored into a systemd template unit (`athena@.service`)
  during Phase 1 implementation — a code-organization choice, not a
  security-relevant one.

## Verification notes from authoring these files (2026-08-27)

- Environment: systemd 259 (259.5-0ubuntu3.4, Ubuntu-based), bubblewrap
  0.11.1, Docker 29.1.3.
- Every systemd directive in `athena-huey-worker.service` was checked
  against this environment's `man systemd.exec` and confirmed to exist
  under the same name with the same meaning as in systemd 257 (the version
  the design document researched against). No renames or removals found.
- Every `bwrap` flag in `athena-mcp-launch.sh` was checked against `bwrap
  --help`/`man bwrap` in this environment and confirmed to exist in
  0.11.1, and the full flag combination was test-executed end to end
  (`bwrap ... -- /usr/bin/env`, against disposable test directories) to
  confirm it actually runs rather than only parsing. See the comment block
  at the top of the script for one minor documentation discrepancy noticed
  in `bwrap --help`'s text for `--share-net` (no functional impact — the
  flag was verified to work exactly as the design intended).
- A cross-manager ordering limitation was found and documented as a comment
  in the systemd unit: `After=network-online.target docker.service` in a
  `systemd --user` unit does not order against those units the way it would
  in a system-level unit, because the user and system managers are separate
  systemd instances. This is not a directive error — it just doesn't
  provide the ordering guarantee it visually suggests. `Restart=on-failure`
  + `RestartSec=5` is the actual mechanism giving resilience against
  Docker/network not being ready yet.
