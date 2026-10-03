"""Reconciliation backstop (design doc §2.7, ADR-0009 decision 5,
docs/EVENT_MODEL.md §4.2).

Compares vault-on-disk state against indexed state and re-triggers ingestion
for every discrepancy -- the backstop for events dropped during downtime or
queue overflow.

Deviation from the design doc's literal text, flagged rather than silent:

1. The design doc describes reconciliation as "re-enqueuing" the same job
   via Huey's queue. This implementation instead calls `ingest_note()`
   directly and synchronously for each path, the same simplification
   `athena.vault.bootstrap` makes and for the same reason (design doc
   §2.7 itself only requires that reconciliation "never mutates the index
   itself, only closes triggering gaps" -- calling the identical idempotent
   function directly satisfies that without an injectable enqueue callback
   for a queue hop that buys nothing at Phase 2's scale).
2. `discrepancy_type` cannot be determined cheaply *before* calling
   `ingest_note()`, because the only hash comparable to `notes.content_hash`
   is the post-secret-scan, post-parse body hash -- there is no cheaper
   proxy (e.g. an on-disk raw-content hash column) in the current schema.
   This implementation therefore calls `ingest_note()` for every discovered
   file every reconciliation pass and classifies the discrepancy from its
   *outcome* afterward, rather than pre-filtering. This means every file
   gets re-scanned for secrets on every reconciliation sweep, not only
   genuinely changed ones -- an accepted, flagged Phase 2 performance cost
   (`docs/design/migration-runner-and-vault-ingestion.md` §8's "do not
   optimize prematurely" posture), to be revisited once vault size and
   reconciliation duration are actually measured, not guessed at now.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

import aiosqlite
from huey import Huey
from qdrant_client import QdrantClient

from athena.db.repository import events as events_repo
from athena.db.repository import notes as notes_repo
from athena.indexing.index_note import index_note
from athena.safety.paths import VaultRoot
from athena.vault.bootstrap import iter_markdown_files
from athena.vault.ingest import IngestOutcome, ingest_note

__all__ = ["ReconciliationSummary", "reconcile_vault"]

logger = logging.getLogger(__name__)

# ingest_note's outcome, mapped to EVENT_MODEL.md §1.6's closed
# discrepancy_type enum (missing_from_index | missing_from_disk |
# hash_mismatch). Outcomes not listed here ("noop", "blocked",
# "scan_error") are not reconciliation discrepancies in that taxonomy's
# terms -- "noop" means nothing was actually wrong, and the other two
# already get their own job.failed/job.completed events from ingest_note
# itself.
_OUTCOME_TO_DISCREPANCY_TYPE: dict[IngestOutcome, str] = {
    "created": "missing_from_index",
    "updated": "hash_mismatch",
    "moved": "hash_mismatch",
    "deleted": "missing_from_disk",
}


@dataclass(frozen=True)
class ReconciliationSummary:
    correlation_id: str
    paths_scanned: int
    discrepancies_found: int
    jobs_enqueued: int
    duration_ms: int


async def reconcile_vault(
    conn: aiosqlite.Connection,
    huey: Huey,
    vault_root: VaultRoot,
    *,
    block_on_high_confidence_secrets: bool = False,
    qdrant_client: QdrantClient | None = None,
) -> ReconciliationSummary:
    correlation_id = str(uuid4())
    start = perf_counter()

    on_disk: dict[str, str] = {}
    paths_scanned = 0
    for path in iter_markdown_files(vault_root):
        paths_scanned += 1
        vault_relative = path.relative_to(vault_root.path).as_posix()
        on_disk[vault_relative] = str(path)

    discrepancies_found = 0
    jobs_enqueued = 0

    async def _reconcile_one(vault_relative: str, absolute_path: str) -> None:
        nonlocal discrepancies_found, jobs_enqueued
        try:
            result = await ingest_note(
                conn,
                huey,
                vault_root,
                absolute_path,
                correlation_id=correlation_id,
                block_on_high_confidence_secrets=block_on_high_confidence_secrets,
                changed_by="reconciliation",
            )
        except Exception:
            # One bad note (e.g. invalid UTF-8, raising past ingest_note's
            # OSError handling) must never abort the rest of the
            # reconciliation sweep -- same best-effort posture as the
            # `index_note` call below, and the same philosophy already
            # established for `run_research`'s per-URL handling.
            logger.exception(
                "ingestion failed for path=%s during reconciliation", vault_relative
            )
            return
        jobs_enqueued += 1
        discrepancy_type = _OUTCOME_TO_DISCREPANCY_TYPE.get(result.outcome)
        if discrepancy_type is not None:
            discrepancies_found += 1
            await events_repo.append_event(
                conn,
                event_type="reconciliation.discrepancy_found",
                source="reconciliation_job",
                correlation_id=correlation_id,
                causation_id=None,
                payload={
                    "path": vault_relative,
                    "discrepancy_type": discrepancy_type,
                    "note_id": result.note_id,
                },
            )

        # Per docs/design/indexing-pipeline.md §2.6: chained directly, same
        # optional/best-effort posture as athena.vault.bootstrap -- a
        # per-note indexing failure never aborts the rest of the sweep,
        # since index_note already records the failure itself.
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
                logger.exception(
                    "indexing failed for note_id=%s during reconciliation", result.note_id
                )

    for vault_relative, absolute_path in on_disk.items():
        await _reconcile_one(vault_relative, absolute_path)

    # Notes indexed as active but missing from disk entirely -- ingest_note's
    # own vanished-path handling (design doc §2.5) does the actual tombstone.
    active_paths = await notes_repo.list_active_paths(conn)
    for missing_path in active_paths - set(on_disk):
        await _reconcile_one(missing_path, str(vault_root.path / missing_path))

    duration_ms = int((perf_counter() - start) * 1000)
    finished_at = datetime.now(UTC).isoformat()
    await events_repo.append_event(
        conn,
        event_type="reconciliation.completed",
        source="reconciliation_job",
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "scan_started_at": finished_at,
            "scan_finished_at": finished_at,
            "paths_scanned": paths_scanned,
            "discrepancies_found": discrepancies_found,
            "jobs_enqueued": jobs_enqueued,
            "duration_ms": duration_ms,
        },
    )

    return ReconciliationSummary(
        correlation_id=correlation_id,
        paths_scanned=paths_scanned,
        discrepancies_found=discrepancies_found,
        jobs_enqueued=jobs_enqueued,
        duration_ms=duration_ms,
    )
