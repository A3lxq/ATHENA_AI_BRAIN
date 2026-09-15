"""Draft assembly and commit orchestration (docs/design/research-ingestion.md
§2.3/§2.4).

`run_research` is the Huey task body: fetches and extracts each URL
independently (one bad URL never aborts the rest of the batch, matching this
codebase's established "degrade, don't crash whole operations" philosophy --
see `athena.retrieval.search`'s Qdrant-unreachable handling for the same
pattern applied elsewhere). `commit_draft` reads a previously-produced draft
back from its `research_jobs` row and, only when `dry_run=False`, writes it
into the vault -- reusing the exact write-then-scan-then-record ordering
`athena.vault.ingest.ingest_note` already established, and the same
crash-safety ordering (filesystem write before any database write)
`athena.intelligence.merge.merge_notes` established in Phase 5.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from qdrant_client import QdrantClient

from athena.db.repository import events as events_repo
from athena.db.repository import notes as notes_repo
from athena.db.repository import provenance as provenance_repo
from athena.db.repository import research_jobs as research_jobs_repo
from athena.db.repository import secret_findings as secret_findings_repo
from athena.git.write import auto_commit_mutation
from athena.indexing.index_note import index_note
from athena.research.extract import extract_article
from athena.research.fetch import FetchRefused, fetch_url
from athena.safety.paths import PathMode, VaultPathError, VaultRoot, resolve_vault_path
from athena.security.secrets import (
    SecretFinding,
    SecretScanResult,
    redact_high_confidence_spans,
    scan_note_for_secrets,
)
from athena.vault.lifecycle import create_note

__all__ = ["ResearchDraft", "CommitResult", "run_research", "commit_draft"]

logger = logging.getLogger(__name__)

_NO_CONTENT_BODY = "(no content could be extracted from any of the provided URLs)"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _folder_name_for(vault_relative_path: str) -> str:
    """Duplicated from `athena.vault.ingest`/`athena.mcp_server.write_tools`'s
    own private helper of the same name -- a small amount of duplicated
    logic beats coupling two unrelated modules, per that module's own
    established reasoning."""
    parts = Path(vault_relative_path).parts
    return parts[0] if len(parts) > 1 else ""


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "untitled"


def _has_blocking_finding(findings: list[SecretFinding]) -> bool:
    return any(f.confidence == "high" and not f.allowlisted for f in findings)


async def _persist_secret_scan_result(
    conn: aiosqlite.Connection, note_id: int, scan_result: SecretScanResult, detected_at: str
) -> None:
    """Duplicated from `athena.vault.ingest._persist_secret_scan_result` --
    same reasoning as `_folder_name_for` above."""
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


@dataclass(frozen=True)
class ResearchDraft:
    title: str
    body: str
    succeeded_urls: list[str]
    failed_urls: list[str]


def run_research(urls: list[str], topic: str) -> ResearchDraft:
    """Fetch and extract each of `urls` independently, assembling one
    Markdown draft with a `## Source: {url}` heading per successfully
    extracted article. Never raises for an individual URL's failure --
    `FetchRefused` (an SSRF-blocked URL), any other fetch-level exception
    (timeout, connection error), and `extract_article` returning `None`
    (no meaningful content -- a paywalled or JS-only page, an expected,
    common outcome per design doc §5) are all recorded as a failed URL and
    the batch continues.
    """
    succeeded_urls: list[str] = []
    failed_urls: list[str] = []
    sections: list[str] = []

    for url in urls:
        try:
            page = fetch_url(url)
        except FetchRefused:
            logger.warning("research URL refused by SSRF protections: %s", url)
            failed_urls.append(url)
            continue
        except Exception:
            logger.warning("failed to fetch research URL: %s", url, exc_info=True)
            failed_urls.append(url)
            continue

        try:
            article = extract_article(page.html, page.url)
        except Exception:
            logger.warning("failed to extract article content from: %s", url, exc_info=True)
            failed_urls.append(url)
            continue

        if article is None:
            logger.info("no meaningful content extracted from: %s", url)
            failed_urls.append(url)
            continue

        succeeded_urls.append(url)
        heading = article.title or url
        sections.append(f"## Source: {url}\n\n### {heading}\n\n{article.markdown_body}")

    body = "\n\n".join(sections) if sections else _NO_CONTENT_BODY
    return ResearchDraft(
        title=topic, body=body, succeeded_urls=succeeded_urls, failed_urls=failed_urls
    )


@dataclass(frozen=True)
class CommitResult:
    note_id: int | None
    """`None` when `dry_run=True` -- nothing was written."""
    preview_title: str
    preview_body: str


async def commit_draft(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    vault_root: VaultRoot,
    job_id: int,
    *,
    dry_run: bool,
    target_path: str | None,
    committed_by: str,
    block_on_high_confidence_secrets: bool = False,
    secret_scan_timeout_s: float = 5.0,
    git_auto_commit_enabled: bool = True,
    git_command_timeout_s: float = 30.0,
) -> CommitResult:
    """Read job `job_id`'s draft back and, when `dry_run=False`, write it
    into the vault. Raises `ValueError` for a job with no draft yet (still
    running, or failed before producing one) -- a caller error, not a
    degraded-but-successful outcome.

    Ordering mirrors `athena.vault.ingest.ingest_note`: the vault file write
    (the one genuinely fallible step) happens first; the file is then
    scanned for secrets on disk and redacted in place if needed, exactly as
    `ingest_note` already does for every other note this codebase creates --
    closing the gap `docs/design/mcp-server.md` flagged as unsolved for
    `note_create`/`note_update`. Only once that has all landed does the
    database write (`create_note`, provenance, `research_jobs.result_note_id`)
    commit. Re-indexing the new note is best-effort, matching `merge_notes`'s
    own precedent -- an indexing failure never undoes a successful content
    write.
    """
    job = await research_jobs_repo.get_by_id(conn, job_id)
    if job is None:
        raise ValueError(f"no such research job: job_id={job_id}")
    if job.draft_body is None:
        raise ValueError(
            f"research job {job_id} has no draft yet (status={job.status!r})"
        )

    title = job.draft_title or f"Research {job_id}"
    body = job.draft_body
    source_urls = job.draft_source_urls or []

    if dry_run:
        return CommitResult(note_id=None, preview_title=title, preview_body=body)

    resolved_target = target_path or f"research/{_slugify(title)}.md"
    try:
        safe_path = resolve_vault_path(resolved_target, vault_root, PathMode.CREATE)
    except VaultPathError as exc:
        raise ValueError(f"cannot write research note at {resolved_target!r}: {exc}") from exc

    safe_path.path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            safe_path.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644
        )
    except FileExistsError as exc:
        raise ValueError(f"target path already exists: {resolved_target!r}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)

    scan_result = scan_note_for_secrets(safe_path.path, timeout_s=secret_scan_timeout_s)
    if scan_result.status == "scan_error":
        logger.warning(
            "secret scan error committing research job_id=%s: %s", job_id, scan_result.error
        )
    elif block_on_high_confidence_secrets and _has_blocking_finding(scan_result.findings):
        safe_path.path.unlink()
        raise ValueError(
            f"commit blocked: high-confidence secret finding in research draft for "
            f"job_id={job_id}"
        )
    elif scan_result.findings:
        redacted_body = redact_high_confidence_spans(body, scan_result.findings)
        if redacted_body != body:
            safe_path.path.write_text(redacted_body, encoding="utf-8")
            body = redacted_body

    vault_relative_path = safe_path.path.relative_to(vault_root.path).as_posix()
    folder = _folder_name_for(vault_relative_path) or None
    content_hash = _content_hash(body)
    now = _now()

    note_id = await create_note(
        conn,
        path=vault_relative_path,
        title=title,
        origin="web_research",
        provider=None,
        folder=folder,
        content_hash=content_hash,
        created_at=now,
        changed_by=committed_by,
    )
    await _persist_secret_scan_result(conn, note_id, scan_result, detected_at=now)

    provenance_id = await provenance_repo.insert_activity(
        conn,
        note_id=note_id,
        activity_type="web_research",
        provider=None,
        model=None,
        human_edited=False,
        occurred_at=now,
        recorded_at=now,
        research_job_id=job_id,
        transformation_notes=f"web research draft committed: {title!r}",
    )
    for url in source_urls:
        await provenance_repo.insert_source(
            conn, provenance_id=provenance_id, url=url, accessed_at=now
        )

    try:
        await index_note(
            conn,
            qdrant_client,
            vault_root,
            note_id,
            correlation_id=f"research_commit:{job_id}",
            causation_id=None,
        )
    except Exception:
        logger.warning(
            "indexing newly-committed research note_id=%s failed; note is re-indexable "
            "later via `athena index bootstrap`/reconciliation",
            note_id,
            exc_info=True,
        )

    await research_jobs_repo.record_result_note(conn, job_id, note_id)

    await auto_commit_mutation(
        conn,
        vault_root,
        paths=[vault_relative_path],
        operation="research_commit",
        detail=vault_relative_path,
        enabled=git_auto_commit_enabled,
        timeout_s=git_command_timeout_s,
    )
    await events_repo.record_vault_event(
        conn,
        event_type="vault.note_created",
        payload={"note_id": note_id, "path": vault_relative_path, "content_hash": content_hash},
    )

    return CommitResult(note_id=note_id, preview_title=title, preview_body=body)
