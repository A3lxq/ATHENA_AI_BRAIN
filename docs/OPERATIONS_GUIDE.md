# ATHENA AI-BRAIN — Operations Guide

A consolidated, operator-facing guide to installing, running, backing up,
and troubleshooting ATHENA AI-BRAIN. This document cross-references the
places detail already lives (`deployment/README.md`, design docs) rather
than duplicating them — treat those as the source of truth when this guide
and one of them appear to disagree.

## Install & first run

1. **Install** at ATHENA AI-BRAIN's fixed, documented path
   (`~/.local/share/athena`). Follow
   [`deployment/README.md`](../deployment/README.md)'s "Installing ATHENA
   AI-BRAIN itself" section for the exact clone/venv/`pip install -e .`
   commands — do not improvise a different install location, since both the
   systemd unit and the bubblewrap launch script are hardcoded to it.
2. **Configure** the required `ATHENA_*` environment variables
   (`ATHENA_VAULT_DIR`, `ATHENA_HUEY_SECRET`, and any provider/behavior
   overrides you need). The complete, current list is documented in
   [`src/athena/config.py`](../src/athena/config.py)'s `load_config()`
   docstring — treat that docstring as the authoritative reference; this
   guide does not re-list every variable.
3. **Initialize the database:**

   ```bash
   athena migrate
   ```
4. **Confirm health:**

   ```bash
   athena doctor
   ```

   A `[FAIL]` result names the specific check that failed (e.g. vault not
   configured, Qdrant unreachable) — resolve it before proceeding to
   ingestion/indexing.

## Enabling the systemd worker unit

ATHENA AI-BRAIN's Huey background worker (research jobs, indexing,
duplicate scans, etc.) runs as a `systemd --user` unit. The full,
step-by-step install/enable/verify procedure — including the
`systemd-analyze security` regression check and the `loginctl
enable-linger` step for keeping the worker running after logout — is
already documented in
[`deployment/README.md`](../deployment/README.md)'s "Installing the Huey
worker systemd unit" section; follow it directly rather than a duplicate
copy here. Before enabling, confirm both placeholder items that section's
"Open items" list tracks: the venv/install path (resolved — see that file)
and the vault path in `ReadWritePaths=` (still open — must be confirmed
against your real `ATHENA_VAULT_DIR` per vault).

## Qdrant setup

ATHENA AI-BRAIN's vector index runs in Qdrant, deployed via Docker per
ADR-0006 (localhost-only binding, no exposed auth surface needed). Start it
with the exact, pinned-version command this project's own sessions have
used:

```bash
docker run -d --name athena-qdrant \
  -p 127.0.0.1:6333:6333 -p 127.0.0.1:6334:6334 \
  -v athena_qdrant_storage:/qdrant/storage \
  --restart unless-stopped \
  qdrant/qdrant:v1.19.1
```

This binds both the REST (6333) and gRPC (6334) ports to loopback only,
stores data in a named Docker volume (`athena_qdrant_storage`, not a bind
mount — relevant for the backup guidance below), and restarts the
container automatically unless you explicitly stop it. Confirm it's
healthy:

```bash
curl http://127.0.0.1:6333/healthz
```

or simply run `athena doctor`, which includes a `qdrant_reachable` check.
As of this writing the container is started manually per host — it is not
yet itself managed by a systemd unit, so it will not survive a host reboot
without one (a known, flagged gap, not this guide's scope to close).

## Backup guidance

ATHENA AI-BRAIN has two things worth backing up, with different mechanisms:

**The vault.** The vault's own Git history — via Phase 8's `athena.git`
module and its auto-commit/auto-push automation
(`ATHENA_GIT_AUTO_COMMIT`/`ATHENA_GIT_AUTO_PUSH`, `athena git push`) — is
the primary backup mechanism. Every mutating tool call auto-commits by
default; enabling auto-push and/or a configured remote gives you an
off-host copy of that history.

**Qdrant's data volume.** This is not optional plumbing to skip. Per
`docs/SECURITY_MODEL.md`'s embedding-inversion finding: dense embeddings
are not an opaque index — active research shows they can be inverted to
reconstruct substantial portions of the original source text, and since
ATHENA AI-BRAIN uses an open-weight embedding model (BGE-M3, ADR-0008), an
attacker who obtains the Qdrant data directory also has trivial white-box
access to the exact encoder used, making inversion easier than in most
published (closed-API) research. In plain terms: **Qdrant's on-disk data is
functionally a second copy of your vault's content** and must be backed up
and access-controlled under the same posture as the vault itself, not
treated as disposable derived data.

Back up the named volume with the standard Docker volume backup pattern:

```bash
docker run --rm \
  -v athena_qdrant_storage:/data \
  -v "$(pwd)":/backup \
  alpine tar czf /backup/qdrant-backup.tar.gz /data
```

This runs a throwaway Alpine container that mounts the `athena_qdrant_storage`
volume read-write at `/data` and your current directory at `/backup`, then
tars and gzips the volume's contents to `qdrant-backup.tar.gz` in your
current directory. Store that archive with the same access controls
(encryption at rest, restricted permissions) you'd apply to a vault backup.

## Troubleshooting

| Symptom / gotcha | Detail |
|---|---|
| `git` subprocess calls behave oddly when a ref/branch name looks like a flag | `--end-of-options` must be inserted immediately before the pathspec/ref arguments, not at the front of the argv — anything placed after it is read as a literal positional argument, including what would otherwise parse as `-b <branch>`. See [`docs/design/git-automation.md`](design/git-automation.md) §0. |
| First `athena git push` to a new branch fails with "no upstream configured" | `push()` wraps `git push <remote> --end-of-options` without `--set-upstream`/`-u`; a branch with no upstream configured fails cleanly with git's own error rather than guessing a remote/branch to set. Run `git push -u origin <branch>` once, manually, outside ATHENA AI-BRAIN, then subsequent `athena git push` calls work normally. See [`docs/design/git-automation.md`](design/git-automation.md) (failure-modes table). |
| An LLM provider you didn't explicitly select gets used for `llm summarize` | If `ATHENA_LLM_PROVIDER` is unset, Ollama counts as "configured" merely because its default `http://localhost:11434` is reachable at call time — not because you set an API key for it, the way the three cloud providers require. A local Ollama server running for an unrelated reason can silently become the auto-selected provider. Set `ATHENA_LLM_PROVIDER` explicitly if you don't want this. See [`docs/design/multi-llm.md`](design/multi-llm.md) §2.3. |
| A path-accepting helper written against `resolve_vault_path(..., PathMode.CREATE)` overwrites an existing file unexpectedly | `PathMode.CREATE` only validates that the path is lexically/structurally safe — it does **not** raise merely because the target already exists. Non-existence enforcement (refuse to overwrite, or gate behind confirmation) is each caller's own responsibility, checked separately and enforced atomically at the actual write (`O_CREAT | O_EXCL`), not guaranteed by path resolution alone. See [`docs/design/mcp-server.md`](design/mcp-server.md) §2.3 and [`docs/design/vault-safety-boundary.md`](design/vault-safety-boundary.md) §"The CREATE-mode wrinkle". |
