"""One-time (but safely re-runnable) full-vault ingestion (design doc §2.6).

Needed because the real vault already contains an existing corpus
(docs/DATA_MODEL.md §0) that predates ATHENA AI-BRAIN and did not arrive via
individual filesystem events.

Calls `ingest_note()` directly and synchronously for each discovered file,
rather than enqueuing through Huey's queue: bootstrap is a one-shot,
CLI-invoked operation the user runs and waits on (`athena ingest
bootstrap`), not a fire-and-forget background job -- there is no requirement
here for the queue hop's concurrency/retry machinery, and idempotency
(design doc §2.4 step 6) already makes bootstrap safe to overlap with a live
watcher or to re-run against an already-ingested vault.

Per docs/design/indexing-pipeline.md §2.6: when `qdrant_client` is supplied,
each successful ingest is chained directly into `index_note()` (the Phase 3
counterpart), not enqueued -- the same one-shot/low-scale reasoning already
applied above. `qdrant_client` is optional so bootstrap remains usable for
metadata-only ingestion when indexing isn't wanted or Qdrant isn't reachable
(e.g. this development environment's current Docker-access blocker, §0/§8
of that design doc) -- a per-note indexing failure is caught and logged,
never aborting the rest of the bootstrap run, since `index_note` itself
already records the failure in `notes.index_state` before re-raising.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import aiosqlite
from huey import Huey
from qdrant_client import QdrantClient

from athena.db.repository import events as events_repo
from athena.db.repository import research_jobs as research_jobs_repo
from athena.indexing.index_note import index_note
from athena.safety.paths import VaultRoot
from athena.vault.ingest import ingest_note

__all__ = ["BootstrapSummary", "bootstrap_ingest_vault"]

logger = logging.getLogger(__name__)

_EXCLUDED_DIR_NAMES = frozenset({".git", ".obsidian"})


@dataclass(frozen=True)
class BootstrapSummary:
    correlation_id: str
    notes_ingested: int
    notes_skipped: int
    notes_failed: int
    outcome_counts: dict[str, int]
    duration_ms: int


def iter_markdown_files(vault_root: VaultRoot) -> Iterator[Path]:
    """Every `.md` file under `vault_root`, excluding `.git`/`.obsidian` and
    never following symlinks (mirrors the vault-safety-boundary design's own
    rationale for refusing in-vault symlinks -- see docs/design/vault-safety-
    boundary.md §5 -- applied here at the directory-walk level)."""
    for dirpath, dirnames, filenames in os.walk(vault_root.path, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            candidate = Path(dirpath) / filename
            try:
                is_symlink = candidate.is_symlink()
            except OSError:
                # An inaccessible ancestor directory can make `is_symlink()`
                # raise instead of returning a boolean -- see
                # `athena.safety.paths._check_no_symlinks_in_chain`'s
                # docstring for the full Python 3.12-vs-3.13+ behavior
                # difference this guards against (fixed there in commit
                # 99d6aa1). This is the whole-vault walk's own instance of
                # the same call, backing both `athena ingest bootstrap` and
                # the reconciliation safety net -- it must degrade by
                # skipping this one entry, not by crashing the entire walk
                # it's supposed to make resilient.
                continue
            if not is_symlink:
                yield candidate


async def bootstrap_ingest_vault(
    conn: aiosqlite.Connection,
    huey: Huey,
    vault_root: VaultRoot,
    *,
    block_on_high_confidence_secrets: bool = False,
    qdrant_client: QdrantClient | None = None,
) -> BootstrapSummary:
    correlation_id = str(uuid4())
    started_at = datetime.now(UTC).isoformat()
    start = perf_counter()

    job_id = await research_jobs_repo.insert(
        conn,
        huey_task_id=f"bootstrap:{correlation_id}",
        job_type="ingestion",
        created_at=started_at,
        requested_by="cli",
    )
    await research_jobs_repo.mark_started(conn, job_id, started_at)

    outcome_counts: dict[str, int] = {}
    notes_failed = 0
    for path in iter_markdown_files(vault_root):
        try:
            result = await ingest_note(
                conn,
                huey,
                vault_root,
                str(path),
                correlation_id=correlation_id,
                block_on_high_confidence_secrets=block_on_high_confidence_secrets,
                changed_by="bootstrap",
            )
        except Exception:
            # One bad note (e.g. one this project's own security-audit
            # found can raise UnicodeDecodeError past ingest_note's OSError
            # handling) must never abort the whole bootstrap run -- same
            # best-effort posture as the `index_note` call just below, and
            # the same philosophy already established for `run_research`'s
            # per-URL handling.
            logger.exception("ingestion failed for path=%s during bootstrap", path)
            notes_failed += 1
            continue
        outcome_counts[result.outcome] = outcome_counts.get(result.outcome, 0) + 1
        if result.outcome == "scan_error":
            notes_failed += 1

        if (
            qdrant_client is not None
            and result.outcome in {"created", "updated"}
            and result.note_id is not None
        ):
            try:
                await index_note(
                    conn,
                    qdrant_client,
                    vault_root,
                    result.note_id,
                    correlation_id=correlation_id,
                    causation_id=None,
                )
            except Exception:
                logger.exception("indexing failed for note_id=%s during bootstrap", result.note_id)

    duration_ms = int((perf_counter() - start) * 1000)
    finished_at = datetime.now(UTC).isoformat()
    finish_status = (
        "failed" if notes_failed and not outcome_counts.get("created") else "succeeded"
    )
    await research_jobs_repo.mark_finished(
        conn, job_id, status=finish_status, finished_at=finished_at
    )

    notes_ingested = outcome_counts.get("created", 0) + outcome_counts.get("updated", 0)
    notes_skipped = outcome_counts.get("noop", 0) + outcome_counts.get("blocked", 0)
    await events_repo.append_event(
        conn,
        event_type="ingestion.job_completed",
        source="huey_job",
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "notes_ingested": notes_ingested,
            "notes_skipped": notes_skipped,
            "duration_ms": duration_ms,
        },
    )

    return BootstrapSummary(
        correlation_id=correlation_id,
        notes_ingested=notes_ingested,
        notes_skipped=notes_skipped,
        notes_failed=notes_failed,
        outcome_counts=outcome_counts,
        duration_ms=duration_ms,
    )
