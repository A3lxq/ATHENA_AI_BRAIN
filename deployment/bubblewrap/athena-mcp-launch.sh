#!/usr/bin/env bash
# athena-mcp-launch.sh — sandboxed launcher for ATHENA AI-BRAIN's stdio MCP server.
#
# Implements docs/design/os-level-process-sandboxing.md §3.4 (bubblewrap
# wrapper). This script is meant to be referenced as the `command` in an MCP
# client's server configuration (Claude Code / Claude Desktop) — see
# ../README.md for how to wire that up.
#
# Verified 2026-08-27 against `bwrap --version` (bubblewrap 0.11.1) and
# `bwrap --help`/`man bwrap` in this environment, and empirically tested by
# running the exact flag set below (against throwaway test paths) end to
# end with `bwrap ... -- /usr/bin/env`, confirming it actually executes.
# Every flag used here exists, unchanged, in 0.11.1:
#   --clearenv --setenv --unshare-pid --unshare-uts --unshare-cgroup
#   --die-with-parent --ro-bind --ro-bind-try --perms --dir --bind --proc
#   --dev --tmpfs --share-net
#
# One discrepancy worth noting (no script change required): `bwrap --help`'s
# one-line summary for --share-net says "(can only combine with
# --unshare-all)", which reads as if --share-net requires --unshare-all.
# `man bwrap` gives the fuller, accurate description: "Retain the network
# namespace, overriding an earlier --unshare-all or --unshare-net." Since
# this script never passes --unshare-net or --unshare-all in the first
# place, the network namespace is already shared with the host by bwrap's
# default behavior; --share-net here is a harmless, explicit no-op that
# documents the intent (see design §3.4's own rationale: outbound HTTPS to
# LLM providers and loopback access to Qdrant both require the host network
# namespace). Confirmed empirically: this exact flag combination executed
# without any usage/argument error.

set -euo pipefail

VAULT_DIR="${ATHENA_VAULT_DIR:?set ATHENA_VAULT_DIR}"
STATE_DIR="${HOME}/.local/state/athena"
MODEL_CACHE="${HOME}/.cache/huggingface"

# --- PLACEHOLDER: install location does not exist yet ---
# This project's actual venv/install location for Phase 1 has not been
# finalized (see docs/design/os-level-process-sandboxing.md "Open Questions
# Carried Forward" — vault/venv path templating is not yet designed as an
# install-time step). Update VENV to the real, verified path once it exists.
VENV="${HOME}/.local/share/athena/.venv"

# `athena.mcp_server` (docs/design/mcp-server.md, Phase 6) now exists as a
# real, tested `python -m athena.mcp_server` entry point. The VENV
# placeholder above is the only remaining blocker before this script is
# actually usable end-to-end.

exec bwrap \
  --clearenv \
  --setenv PATH "/usr/bin:/bin" \
  --setenv ATHENA_VAULT_DIR "$VAULT_DIR" \
  --unshare-pid \
  --unshare-uts \
  --unshare-cgroup \
  --die-with-parent \
  --ro-bind /usr /usr \
  --ro-bind /bin /bin \
  --ro-bind /lib /lib \
  --ro-bind-try /lib64 /lib64 \
  --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --ro-bind /etc/ssl /etc/ssl \
  --dir /home/"$USER" \
  --perms 0000 --dir "$HOME"/.ssh \
  --perms 0000 --dir "$HOME"/.aws \
  --perms 0000 --dir "$HOME"/.gnupg \
  --bind "$VAULT_DIR" "$VAULT_DIR" \
  --bind "$STATE_DIR" "$STATE_DIR" \
  --ro-bind "$MODEL_CACHE" "$MODEL_CACHE" \
  --ro-bind "$VENV" "$VENV" \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --share-net \
  -- "$VENV/bin/python" -m athena.mcp_server
