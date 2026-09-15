# Release Checklist

A short, concrete checklist for cutting an ATHENA AI-BRAIN release. Written
for Phase 11's use (per `docs/design/production-hardening.md` §2.8) — not
exercised by Phase 10 itself.

## Steps

1. **CI is green on `main`.**
   Confirm the `CI` workflow (`.github/workflows/ci.yml`) passed on the
   latest commit on `main` — check the Actions tab or `gh run list --branch
   main --limit 1`.

2. **Run the three gates locally too** (redundant with CI, but a human
   sanity check before tagging):
   - [ ] `ruff check src tests`
   - [ ] `mypy src`
   - [ ] `pytest -q`

3. **`CHANGELOG.md` has an entry for this release.**
   `CHANGELOG.md` currently keeps all work under a single `## Unreleased`
   heading with an `### Added` bullet list per phase. Before tagging, turn
   the `## Unreleased` heading into a dated version heading (e.g. `##
   [0.2.0] - 2026-MM-DD`) covering everything being released, and start a
   fresh empty `## Unreleased` section above it for whatever comes next.

4. **`CURRENT_STATE.md` reflects the current state accurately.**
   `CURRENT_STATE.md` tracks the current phase (`## Current Phase`) and a
   narrative `## Current Status` section describing what's implemented,
   tested, and verified. Confirm both sections describe the code actually
   being released — not a stale prior phase.

5. **Bump `pyproject.toml`'s `version` field.**
   Currently `"0.1.0"`. Bump it to the new release version, following
   semantic versioning (`MAJOR.MINOR.PATCH`).

6. **Commit the version bump.**
   Include the `CHANGELOG.md` and `CURRENT_STATE.md` updates from steps 3-4
   in the same commit (or a small preceding commit) so the tag lands on a
   commit that is internally consistent.

7. **Tag the release.**
   ```
   git tag -a vX.Y.Z -m "vX.Y.Z: <one-line summary>"
   git push origin vX.Y.Z
   ```
   **Pushing a tag is a real, externally-visible action.** Per this
   project's established practice (`CLAUDE.md` rule 22/23 — never execute
   destructive or externally-visible operations without explicit user
   intent, never auto-push unreviewed changes), only run `git push origin
   vX.Y.Z` after the user has explicitly said to go ahead. Creating the
   local tag (`git tag -a`) is reversible and fine to do first; the `push`
   is the step that requires the go-ahead.

8. **(Optional) Create a GitHub Release.**
   ```
   gh release create vX.Y.Z --notes-from-tag
   ```
   or equivalent, once the tag is pushed.
