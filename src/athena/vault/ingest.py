"""The idempotent per-path ingestion job (docs/design/migration-runner-and-
vault-ingestion.md §2.4/§2.5).

Callable from three sources -- the watcher, the bootstrap walk, and the
reconciliation job -- one code path, multiple triggers, per
docs/EVENT_MODEL.md §6 recommendation 7. Deliberately stops short of
chunking/embedding (Phase 3's `index_note()`, not built here -- see the
design doc §1): a note can be fully ingested (metadata/provenance/lifecycle
recorded) with `notes.last_indexed_at` still NULL, correctly signaling
"known to ATHENA AI-BRAIN, not yet semantically searchable."
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite
from huey import Huey
from huey.exceptions import TaskLockedException

from athena.db.repository import events as events_repo
from athena.db.repository import notes as notes_repo
from athena.db.repository import provenance as provenance_repo
from athena.db.repository import secret_findings as secret_findings_repo
from athena.db.repository import tags as tags_repo
from athena.safety.content import (
    FrontmatterParseError,
    FrontmatterTooLargeError,
    NoteShape,
    ParsedNote,
    parse_note_safely,
)
from athena.safety.paths import PathMode, VaultPathError, VaultRoot, resolve_vault_path
from athena.security.secrets import (
    SecretFinding,
    SecretScanResult,
    redact_high_confidence_spans,
    scan_note_for_secrets,
)
from athena.vault import lifecycle
from athena.vault.provenance_inference import infer_origin

__all__ = ["IngestOutcome", "IngestResult", "ingest_note"]

logger = logging.getLogger(__name__)

_ORIGIN_CHECK_VALUES = frozenset(
    {"human", "ai_generated", "web_research", "imported", "merged"}
)

IngestOutcome = Literal[
    "created", "updated", "moved", "deleted", "noop", "blocked", "scan_error"
]


@dataclass(frozen=True)
class IngestResult:
    outcome: IngestOutcome
    note_id: int | None
    detail: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _folder_name_for(vault_relative_path: str) -> str:
    parts = Path(vault_relative_path).parts
    return parts[0] if len(parts) > 1 else ""


def _title_for(vault_relative_path: str, parsed: ParsedNote) -> str:
    frontmatter_title = parsed.metadata.get("title")
    if isinstance(frontmatter_title, str) and frontmatter_title.strip():
        return frontmatter_title.strip()
    return Path(vault_relative_path).stem


def _origin_and_provider(folder_name: str, parsed: ParsedNote) -> tuple[str, str | None]:
    """Prefer explicit frontmatter fields over folder-based inference where
    both are present (design doc §2.4 step d) -- frontmatter is a stronger,
    author-supplied signal than a legacy-corpus heuristic."""
    if parsed.shape is NoteShape.FRONTMATTER:
        frontmatter_origin = parsed.metadata.get("origin")
        if frontmatter_origin in _ORIGIN_CHECK_VALUES:
            return frontmatter_origin, parsed.metadata.get("provider")
    return infer_origin(folder_name, parsed.shape)


def _has_blocking_finding(findings: list[SecretFinding]) -> bool:
    return any(f.confidence == "high" and not f.allowlisted for f in findings)


async def _persist_secret_scan_result(
    conn: aiosqlite.Connection, note_id: int, scan_result: SecretScanResult, detected_at: str
) -> None:
    """Wires ADR-0011's schema into a real caller (design doc §2.4 step 7b):
    one `note_secret_findings` row per finding (high AND low confidence --
    the schema records both, only high-confidence non-allowlisted findings
    are actually redacted from the stored text), and `notes.secret_scan_status`
    set to match `scan_result.status` ('clean'/'flagged') every ingestion --
    including resetting a previously-'flagged' note back to 'clean' if the
    offending content was since removed or allowlisted.
    """
    await secret_findings_repo.delete_findings_for_note(conn, note_id)
    for finding in scan_result.findings:
        await secret_findings_repo.insert_finding(
            conn,
            note_id=note_id,
            plugin_type=finding.plugin_type,
            line_number=finding.line_number,
            confidence=finding.confidence,
            secret_hash=finding.secret_hash,
            redacted=finding.confidence == "high" and not finding.allowlisted,
            detected_at=detected_at,
        )
    await notes_repo.update_secret_scan_status(conn, note_id, secret_scan_status=scan_result.status)


async def _append(
    conn: aiosqlite.Connection,
    *,
    event_type: str,
    source: str,
    correlation_id: str,
    causation_id: str | None,
    payload: dict[str, object],
    idempotency_key: str | None = None,
) -> str:
    return await events_repo.append_event(
        conn,
        event_type=event_type,
        source=source,
        correlation_id=correlation_id,
        causation_id=causation_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )


async def _handle_vanished_path(
    conn: aiosqlite.Connection,
    vault_root: VaultRoot,
    vault_relative: str,
    *,
    correlation_id: str,
    causation_id: str | None,
) -> IngestResult:
    """design doc §2.5: a trigger for a path that no longer exists on disk.
    Either a genuine deletion, or the "delete half" of a move whose "create
    half" another job already handled -- distinguished by whether some
    *other* currently-existing active note now carries this note's hash."""
    existing = await notes_repo.get_by_path(conn, vault_relative)
    if existing is None or existing.deleted_at is not None:
        return IngestResult("noop", None, "no active note recorded at this path")

    candidates = await notes_repo.find_by_content_hash(conn, existing.content_hash)
    for candidate in candidates:
        if candidate.id == existing.id:
            continue
        if (vault_root.path / candidate.path).exists():
            # The "create" half of this move already ran (or will run) and
            # re-pathed the note under its own id -- nothing left to do here.
            return IngestResult("noop", existing.id, "delete half of an already-handled move")

    await lifecycle.delete_note(conn, existing.id, deleted_at=_now())
    await _append(
        conn,
        event_type="vault.note_deleted",
        source="huey_job",
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload={
            "note_id": existing.id,
            "path": vault_relative,
            "last_known_content_hash": existing.content_hash,
        },
    )
    await _append(
        conn,
        event_type="job.completed",
        source="huey_job",
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload={"job_type": "ingestion", "noop": False},
    )
    return IngestResult("deleted", existing.id)


async def _ingest_note_locked(
    conn: aiosqlite.Connection,
    vault_root: VaultRoot,
    raw_path: str,
    *,
    correlation_id: str,
    causation_id: str | None,
    block_on_high_confidence_secrets: bool,
    secret_scan_timeout_s: float,
    changed_by: str,
) -> IngestResult:
    started_event_id = await _append(
        conn,
        event_type="job.started",
        source="huey_job",
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload={"job_type": "ingestion", "path": raw_path},
    )

    try:
        safe_path = resolve_vault_path(raw_path, vault_root, PathMode.MAYBE_EXISTING)
    except VaultPathError as exc:
        await _append(
            conn,
            event_type="job.failed",
            source="huey_job",
            correlation_id=correlation_id,
            causation_id=started_event_id,
            payload={"job_type": "ingestion", "retry_count": 0, "last_error": str(exc)},
        )
        return IngestResult("scan_error", None, str(exc))

    vault_relative = (
        safe_path.path.relative_to(vault_root.path).as_posix()
        if safe_path.path != vault_root.path
        else ""
    )

    if not safe_path.path.exists():
        return await _handle_vanished_path(
            conn,
            vault_root,
            vault_relative,
            correlation_id=correlation_id,
            causation_id=started_event_id,
        )

    try:
        raw_text = safe_path.path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError subclass, not an OSError
        # subclass -- a `.md` file containing invalid UTF-8 bytes must be
        # treated as a clean per-note ingestion failure (same outcome/event
        # shape as an OSError from the read), not allowed to propagate and
        # abort the whole-vault callers (bootstrap/reconciliation) that loop
        # over `ingest_note()`.
        await _append(
            conn,
            event_type="job.failed",
            source="huey_job",
            correlation_id=correlation_id,
            causation_id=started_event_id,
            payload={"job_type": "ingestion", "retry_count": 0, "last_error": str(exc)},
        )
        return IngestResult("scan_error", None, str(exc))

    allowlist = await secret_findings_repo.get_allowlisted_hashes(conn)
    scan_result = scan_note_for_secrets(
        safe_path.path, timeout_s=secret_scan_timeout_s, allowlist=allowlist
    )
    if scan_result.status == "scan_error":
        await _append(
            conn,
            event_type="job.failed",
            source="huey_job",
            correlation_id=correlation_id,
            causation_id=started_event_id,
            payload={"job_type": "ingestion", "retry_count": 0, "last_error": scan_result.error},
        )
        return IngestResult("scan_error", None, scan_result.error)

    if block_on_high_confidence_secrets and _has_blocking_finding(scan_result.findings):
        logger.warning(
            "ingestion blocked by high-confidence secret finding (block-on-high mode): %s",
            vault_relative,
        )
        await _append(
            conn,
            event_type="job.completed",
            source="huey_job",
            correlation_id=correlation_id,
            causation_id=started_event_id,
            payload={"job_type": "ingestion", "noop": True, "blocked": True},
        )
        return IngestResult(
            "blocked", None, "high-confidence secret finding, block-on-high enabled"
        )

    text_for_parsing = (
        redact_high_confidence_spans(raw_text, scan_result.findings)
        if scan_result.findings
        else raw_text
    )
    folder_name = _folder_name_for(vault_relative)

    try:
        parsed = parse_note_safely(text_for_parsing, folder_name=folder_name)
    except (FrontmatterTooLargeError, FrontmatterParseError) as exc:
        logger.warning(
            "malformed frontmatter, ingesting as plain body: %s (%s)", vault_relative, exc
        )
        parsed = ParsedNote(
            metadata={},
            body=text_for_parsing,
            shape=NoteShape.PLAIN,
            source_url=None,
            provider_hint=None,
            parse_warning=str(exc),
        )

    content_hash = _content_hash(parsed.body)
    existing = await notes_repo.get_by_path(conn, vault_relative)

    if existing is not None and existing.deleted_at is None:
        if existing.content_hash == content_hash:
            await _append(
                conn,
                event_type="job.completed",
                source="huey_job",
                correlation_id=correlation_id,
                causation_id=started_event_id,
                payload={"job_type": "ingestion", "noop": True},
            )
            return IngestResult("noop", existing.id)

    now = _now()
    origin, provider = _origin_and_provider(folder_name, parsed)

    if existing is None:
        # Move detection (design doc §2.5): a brand-new path whose content
        # hash matches an active note whose own path has vanished.
        for candidate in await notes_repo.find_by_content_hash(conn, content_hash):
            candidate_abs = vault_root.path / candidate.path
            if candidate.path != vault_relative and not candidate_abs.exists():
                await lifecycle.move_note(
                    conn, candidate.id, new_path=vault_relative, updated_at=now
                )
                await _append(
                    conn,
                    event_type="vault.note_moved",
                    source="huey_job",
                    correlation_id=correlation_id,
                    causation_id=started_event_id,
                    payload={
                        "note_id": candidate.id,
                        "old_path": candidate.path,
                        "new_path": vault_relative,
                        "content_hash": content_hash,
                    },
                )
                await _append(
                    conn,
                    event_type="job.completed",
                    source="huey_job",
                    correlation_id=correlation_id,
                    causation_id=started_event_id,
                    payload={"job_type": "ingestion", "noop": False},
                )
                return IngestResult("moved", candidate.id)

        note_id = await lifecycle.create_note(
            conn,
            path=vault_relative,
            title=_title_for(vault_relative, parsed),
            origin=origin,
            provider=provider,
            folder=folder_name or None,
            content_hash=content_hash,
            created_at=now,
            changed_by=changed_by,
        )
        event_type = "vault.note_created"
        outcome: IngestOutcome = "created"
    else:
        await lifecycle.update_note_content(
            conn, existing.id, content_hash=content_hash, updated_at=now
        )
        note_id = existing.id
        event_type = "vault.note_modified"
        outcome = "updated"

    await _persist_secret_scan_result(conn, note_id, scan_result, now)

    provenance_id = await provenance_repo.insert_activity(
        conn,
        note_id=note_id,
        activity_type="ingested",
        provider=provider,
        model=None,
        human_edited=False,
        occurred_at=now,
        recorded_at=now,
        transformation_notes=(
            "source URL recovered from legacy '> From:' line" if parsed.source_url else None
        ),
    )
    if parsed.source_url is not None:
        await provenance_repo.insert_source(
            conn, provenance_id=provenance_id, url=parsed.source_url
        )

    frontmatter_tags = (
        parsed.metadata.get("tags") if parsed.shape is NoteShape.FRONTMATTER else None
    )
    if isinstance(frontmatter_tags, list):
        for raw_tag in frontmatter_tags:
            if not isinstance(raw_tag, str) or not raw_tag.strip():
                continue
            normalized = raw_tag.strip().lower()
            tag_id = await tags_repo.get_or_create(conn, normalized, raw_tag.strip())
            await tags_repo.attach(conn, note_id, tag_id)

    await _append(
        conn,
        event_type=event_type,
        source="huey_job",
        correlation_id=correlation_id,
        causation_id=started_event_id,
        payload={"note_id": note_id, "path": vault_relative, "content_hash": content_hash},
    )
    await _append(
        conn,
        event_type="job.completed",
        source="huey_job",
        correlation_id=correlation_id,
        causation_id=started_event_id,
        payload={"job_type": "ingestion", "noop": False},
    )
    return IngestResult(outcome, note_id)


async def ingest_note(
    conn: aiosqlite.Connection,
    huey: Huey,
    vault_root: VaultRoot,
    raw_path: str,
    *,
    correlation_id: str,
    causation_id: str | None = None,
    block_on_high_confidence_secrets: bool = False,
    secret_scan_timeout_s: float = 5.0,
    changed_by: str = "system",
) -> IngestResult:
    """Idempotently ingest (or detect the deletion/move of) the note at
    `raw_path`. Safe to call repeatedly and concurrently for the same or
    different paths -- see the module docstring and design doc §2.4/§2.5.

    Locked via `huey.lock_task` keyed on the normalized path (ADR-0009
    decision 4): a concurrent call for the same path does not block, it
    returns `IngestResult("noop", ...)` immediately (Huey's `TaskLock` is
    fail-fast, not blocking -- verified against the installed huey API).
    Whichever call actually acquires the lock re-derives current truth from
    disk, so which one wins is safe by construction (ADR-0009's core
    idempotency philosophy).
    """
    try:
        with huey.lock_task(f"ingest:{raw_path}"):
            return await _ingest_note_locked(
                conn,
                vault_root,
                raw_path,
                correlation_id=correlation_id,
                causation_id=causation_id,
                block_on_high_confidence_secrets=block_on_high_confidence_secrets,
                secret_scan_timeout_s=secret_scan_timeout_s,
                changed_by=changed_by,
            )
    except TaskLockedException:
        return IngestResult("noop", None, "already in progress for this path")
