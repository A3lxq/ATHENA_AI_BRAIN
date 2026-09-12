"""The Huey worker entry point (design doc §2.10).

Resolves the placeholder `deployment/systemd/athena-huey-worker.service`'s
`ExecStart=` has referenced since Phase 1: run via
`huey_consumer.py athena.worker.huey`.

Constraint this module relies on and does not itself enforce: the filesystem
watcher is started from a `@huey.on_startup()` hook, which Huey's consumer
calls once per worker thread/process (verified against the installed huey
API, `huey/consumer.py`'s `Worker.initialize()`). `huey_consumer`'s default
worker count is 1, matching `deployment/systemd/athena-huey-worker.service`
(no `-w` flag), so the hook fires exactly once in the deployed configuration.
Running the consumer with `-w N` for N > 1 would start N redundant `Observer`
instances watching the same vault -- do not do that without first adding a
singleton guard here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from huey import SqliteHuey, crontab
from huey.serializer import SignedSerializer
from qdrant_client import QdrantClient

from athena.config import AthenaConfig, load_config
from athena.db.connection import open_connection
from athena.db.repository import duplicates as duplicates_repo
from athena.db.repository import events as events_repo
from athena.db.repository import research_jobs as research_jobs_repo
from athena.hardening.permissions import ensure_private_dir
from athena.hardening.serializer import SerializerMisconfigured, assert_safe_job_serializer
from athena.indexing.index_note import IndexBootstrapSummary, index_bootstrap, index_note
from athena.indexing.qdrant_store import ensure_collection
from athena.intelligence.duplicates import DuplicateCandidate, scan_for_duplicates
from athena.intelligence.lifecycle import StaleSweepSummary
from athena.intelligence.lifecycle import run_stale_sweep as _run_stale_sweep
from athena.intelligence.merge import MergeResult, merge_notes
from athena.intelligence.merge import list_pending_duplicates as _list_pending_duplicates
from athena.intelligence.merge import resolve_duplicate as _resolve_duplicate
from athena.research.workflow import CommitResult, ResearchDraft, commit_draft, run_research
from athena.retrieval.evaluation import (
    DEFAULT_CORPUS_DIR,
    EvaluationReport,
    load_corpus,
    run_evaluation,
)
from athena.retrieval.search import search_ranked_note_paths
from athena.safety.paths import VaultRoot
from athena.vault.bootstrap import BootstrapSummary, bootstrap_ingest_vault
from athena.vault.ingest import ingest_note
from athena.vault.reconcile import ReconciliationSummary, reconcile_vault
from athena.vault.watcher import VaultWatcher

__all__ = [
    "build_huey",
    "huey",
    "start_watcher",
    "ingest_note_task",
    "index_note_task",
    "reconcile_vault_task",
    "stale_sweep_task",
    "duplicates_scan_task",
    "reindex_task",
    "research_task",
]

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def build_huey(config: AthenaConfig) -> SqliteHuey:
    """Construct the one Huey instance every part of ATHENA AI-BRAIN's job/lock
    machinery must share -- CLI commands that need `huey.lock_task` (e.g.
    `athena ingest bootstrap`) call this with the same `config` rather than
    constructing their own, so locks are taken against the same underlying
    storage (`name`/`filename` pair) the real worker process uses.

    Hard-fails via `assert_safe_job_serializer` (Phase 1, unchanged) if
    misconfigured -- a worker (or CLI command sharing its lock store) must
    never run with an unauthenticated or empty-secret serializer. Checked
    before ever constructing `SignedSerializer`, not after: `SignedSerializer`
    itself raises huey's own `ConfigurationError` on an empty secret, which
    would otherwise leak a different, less specific exception type than the
    one every other misconfiguration in this codebase raises.
    """
    if not config.huey_serializer_secret:
        raise SerializerMisconfigured(
            "ATHENA_HUEY_SECRET is not set; refusing to construct a job queue "
            "with an unauthenticated or empty-secret serializer (ADR-0002)."
        )
    ensure_private_dir(config.data_dir)
    instance = SqliteHuey(
        name="athena",
        filename=str(config.huey_db_path),
        serializer=SignedSerializer(secret=config.huey_serializer_secret),
    )
    assert_safe_job_serializer(instance)
    return instance


_config = load_config()
huey = build_huey(_config)


_qdrant_client: QdrantClient | None = None


def _get_qdrant_client(config: AthenaConfig) -> QdrantClient:
    """Lazy, process-lifetime singleton -- constructed (and `ensure_collection`
    run) on first use, not at import time, matching `athena.indexing.
    embedding`'s lazy-model pattern. Requires a reachable Qdrant server
    (`ATHENA_QDRANT_URL`, default matching ADR-0006's binding); currently
    blocked in this development environment (design doc §0/§8) -- calling
    this in that environment fails cleanly with a connection error, which is
    the correct, expected behavior until Docker access is restored.
    """
    global _qdrant_client
    if _qdrant_client is None:
        client = QdrantClient(url=config.qdrant_url)
        ensure_collection(client, huey)
        _qdrant_client = client
    return _qdrant_client


def _require_vault_root(config: AthenaConfig) -> VaultRoot:
    if config.vault_root is None:
        raise RuntimeError(
            "ATHENA_VAULT_DIR is not set -- the worker cannot ingest without a configured vault"
        )
    return VaultRoot.initialize(config.vault_root)


def _on_settle(path: str) -> None:
    """`VaultWatcher`'s injected settle callback (design doc §2.3). Appends
    `fs.path_changed` (the root of a new correlation chain, per
    docs/EVENT_MODEL.md §3.2) and enqueues `ingest_note_task` -- synchronous,
    no asyncio bridge at this layer, per ADR-0009 decision 3: Huey's SQLite
    enqueue is just a parameterized DB write.
    """

    async def _append_and_get_ids() -> tuple[str, str]:
        correlation_id = str(uuid4())
        async with open_connection(_config.db_path) as conn:
            event_id = await events_repo.append_event(
                conn,
                event_type="fs.path_changed",
                source="filesystem_watcher",
                correlation_id=correlation_id,
                causation_id=None,
                payload={"path": path, "raw_event_kinds": []},
            )
        return correlation_id, event_id

    try:
        correlation_id, causation_id = asyncio.run(_append_and_get_ids())
    except Exception:
        logger.exception("failed to record fs.path_changed for %s", path)
        return
    ingest_note_task(path, correlation_id, causation_id)


@huey.task(retries=3, retry_delay=10)  # type: ignore[untyped-decorator]  # huey ships no py.typed
def ingest_note_task(path: str, correlation_id: str, causation_id: str | None) -> None:
    async def _run() -> tuple[str, int | None]:
        vault_root = _require_vault_root(_config)
        async with open_connection(_config.db_path) as conn:
            result = await ingest_note(
                conn,
                huey,
                vault_root,
                path,
                correlation_id=correlation_id,
                causation_id=causation_id,
                block_on_high_confidence_secrets=_config.secret_scanner_block_on_high_confidence,
            )
            return result.outcome, result.note_id

    outcome, note_id = asyncio.run(_run())
    # Chained via a normal (queued, not call_local) invocation -- design doc
    # §2.6. Enqueueing is a cheap SQLite write; the chained job's own
    # execution (and its Qdrant dependency) happens independently, whenever
    # the consumer next picks it up.
    if outcome in {"created", "updated"} and note_id is not None:
        index_note_task(note_id, correlation_id, causation_id)


@huey.task(retries=3, retry_delay=10)  # type: ignore[untyped-decorator]  # huey ships no py.typed
def index_note_task(note_id: int, correlation_id: str, causation_id: str | None) -> None:
    async def _run() -> None:
        vault_root = _require_vault_root(_config)
        qdrant_client = _get_qdrant_client(_config)
        async with open_connection(_config.db_path) as conn:
            await index_note(
                conn,
                qdrant_client,
                vault_root,
                note_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )

    asyncio.run(_run())


def _try_get_qdrant_client(config: AthenaConfig) -> QdrantClient | None:
    """Best-effort: bootstrap/reconcile must remain usable for metadata-only
    ingestion when Qdrant isn't reachable (this development environment's
    current Docker-access blocker, design doc §0/§8, included) -- a
    connection failure here degrades to "skip indexing this run" rather than
    aborting the whole CLI command."""
    try:
        return _get_qdrant_client(config)
    except Exception:
        logger.warning(
            "Qdrant unreachable at %s -- proceeding with metadata-only ingestion, "
            "no indexing this run.",
            config.qdrant_url,
        )
        return None


def run_bootstrap(config: AthenaConfig | None = None) -> BootstrapSummary:
    """Synchronous entry point for `athena ingest bootstrap` -- calls
    `bootstrap_ingest_vault` directly rather than through the task queue
    (see `athena.vault.bootstrap`'s own module docstring for why)."""
    active_config = config or _config
    vault_root = _require_vault_root(active_config)
    qdrant_client = _try_get_qdrant_client(active_config)

    async def _run() -> BootstrapSummary:
        async with open_connection(active_config.db_path) as conn:
            return await bootstrap_ingest_vault(
                conn,
                huey,
                vault_root,
                block_on_high_confidence_secrets=active_config.secret_scanner_block_on_high_confidence,
                qdrant_client=qdrant_client,
            )

    return asyncio.run(_run())


def run_index_bootstrap(config: AthenaConfig | None = None) -> IndexBootstrapSummary:
    """Synchronous entry point for `athena index bootstrap` (design doc
    §6) -- unlike `run_bootstrap`/`run_reconcile`, this genuinely requires
    Qdrant to be reachable (there is no meaningful "metadata-only" mode for
    an indexing-only command), so a connection failure here propagates
    rather than degrading silently."""
    active_config = config or _config
    vault_root = _require_vault_root(active_config)
    qdrant_client = _get_qdrant_client(active_config)

    async def _run() -> IndexBootstrapSummary:
        async with open_connection(active_config.db_path) as conn:
            return await index_bootstrap(
                conn, qdrant_client, vault_root, correlation_id=str(uuid4())
            )

    return asyncio.run(_run())


def run_reconcile(config: AthenaConfig | None = None) -> ReconciliationSummary:
    """Synchronous entry point for `athena ingest reconcile` (an on-demand
    pass outside the periodic schedule below)."""
    active_config = config or _config
    vault_root = _require_vault_root(active_config)
    qdrant_client = _try_get_qdrant_client(active_config)

    async def _run() -> ReconciliationSummary:
        async with open_connection(active_config.db_path) as conn:
            return await reconcile_vault(
                conn,
                huey,
                vault_root,
                block_on_high_confidence_secrets=active_config.secret_scanner_block_on_high_confidence,
                qdrant_client=qdrant_client,
            )

    return asyncio.run(_run())


def run_retrieval_evaluate(
    config: AthenaConfig | None = None, *, corpus_dir: Path | None = None
) -> EvaluationReport:
    """Synchronous entry point for `athena retrieval evaluate`. Uses a
    plain, unvalidated `QdrantClient` (not `_get_qdrant_client`'s eager
    `ensure_collection` check) -- if Qdrant is unreachable, `search()`'s own
    per-query degradation (keyword-only fusion) kicks in for each question
    rather than failing the whole command up front."""
    active_config = config or _config
    qdrant_client = QdrantClient(url=active_config.qdrant_url)
    corpus = load_corpus(corpus_dir or DEFAULT_CORPUS_DIR)

    async def _run() -> EvaluationReport:
        async with open_connection(active_config.db_path) as conn:

            async def search_fn(query_text: str) -> list[str]:
                return await search_ranked_note_paths(conn, qdrant_client, query_text)

            return await run_evaluation(corpus, search_fn)

    return asyncio.run(_run())


def run_duplicates_scan(
    config: AthenaConfig | None = None,
    *,
    note_ids: list[int] | None = None,
    threshold: float = 0.5,
) -> list[DuplicateCandidate]:
    """Synchronous entry point for `athena duplicates scan` (design doc §2.1).
    Best-effort Qdrant, like `run_bootstrap`/`run_reconcile` -- the scan's own
    semantic-signal degradation (`scan_for_duplicates`) already handles an
    unreachable server, so this never needs to fail the whole command up
    front over it."""
    active_config = config or _config
    vault_root = _require_vault_root(active_config)
    qdrant_client = _try_get_qdrant_client(active_config) or QdrantClient(
        url=active_config.qdrant_url
    )

    async def _run() -> list[DuplicateCandidate]:
        async with open_connection(active_config.db_path) as conn:
            return await scan_for_duplicates(
                conn, qdrant_client, vault_root.path, note_ids=note_ids, threshold=threshold
            )

    return asyncio.run(_run())


def run_duplicates_list(
    config: AthenaConfig | None = None, *, status: str = "pending"
) -> list[DuplicateCandidate]:
    """Synchronous entry point for `athena duplicates list`."""
    active_config = config or _config

    async def _run() -> list[DuplicateCandidate]:
        async with open_connection(active_config.db_path) as conn:
            if status == "pending":
                return await _list_pending_duplicates(conn)
            return await duplicates_repo.list_by_status(conn, status)

    return asyncio.run(_run())


def run_duplicates_resolve(
    config: AthenaConfig | None = None,
    *,
    candidate_id: int,
    resolution: str,
    resolved_by: str,
    resolution_note: str | None = None,
) -> None:
    """Synchronous entry point for `athena duplicates resolve`."""
    active_config = config or _config

    async def _run() -> None:
        async with open_connection(active_config.db_path) as conn:
            await _resolve_duplicate(
                conn,
                candidate_id,
                resolution=resolution,
                resolved_by=resolved_by,
                resolution_note=resolution_note,
            )

    asyncio.run(_run())


def run_duplicates_merge(
    config: AthenaConfig | None = None,
    *,
    keep_note_id: int,
    absorb_note_id: int,
    merged_by: str,
) -> MergeResult:
    """Synchronous entry point for `athena duplicates merge`. Requires a
    reachable Qdrant client (unlike the scan): re-indexing the merged note
    is best-effort internally (`merge_notes` itself degrades gracefully if
    Qdrant is unreachable during that step), but constructing the client at
    all needs a configured URL, same as `run_index_bootstrap`."""
    active_config = config or _config
    vault_root = _require_vault_root(active_config)
    qdrant_client = QdrantClient(url=active_config.qdrant_url)

    async def _run() -> MergeResult:
        async with open_connection(active_config.db_path) as conn:
            return await merge_notes(
                conn,
                qdrant_client,
                vault_root,
                keep_note_id=keep_note_id,
                absorb_note_id=absorb_note_id,
                merged_by=merged_by,
            )

    return asyncio.run(_run())


def run_stale_sweep(
    config: AthenaConfig | None = None, *, stale_after_days: int = 180
) -> StaleSweepSummary:
    """Synchronous entry point for `athena lifecycle stale-sweep` (design
    doc §2.5), also called from the periodic `stale_sweep_task` below."""
    active_config = config or _config

    async def _run() -> StaleSweepSummary:
        async with open_connection(active_config.db_path) as conn:
            return await _run_stale_sweep(conn, stale_after_days=stale_after_days)

    return asyncio.run(_run())


@huey.periodic_task(crontab(minute="0"))  # type: ignore[untyped-decorator]  # huey ships no py.typed
def reconcile_vault_task() -> None:  # pragma: no cover -- exercised via run_reconcile in tests
    run_reconcile(_config)


@huey.periodic_task(  # type: ignore[untyped-decorator]  # huey ships no py.typed
    crontab(minute="0", hour="3")
)
def stale_sweep_task() -> None:  # pragma: no cover -- exercised via run_stale_sweep in tests
    run_stale_sweep(_config)


@huey.task(retries=3, retry_delay=10)  # type: ignore[untyped-decorator]  # huey ships no py.typed
def duplicates_scan_task(correlation_id: str) -> None:
    """MCP `duplicates_scan` tool's task-backed counterpart (design doc
    §2.1/§2.3) -- a whole-vault scan (`note_ids=None`), matching `run_
    duplicates_scan`'s own best-effort Qdrant reasoning: `scan_for_
    duplicates`'s semantic signal already degrades gracefully if Qdrant is
    unreachable, so this never needs to fail the whole job over it. Unlike
    `ingest_note_task`, this task does not touch `research_jobs` itself --
    that bookkeeping (insert/mark_started/mark_finished) is the MCP tool
    layer's job when it *enqueues* this task, not this task's own job when
    it *runs*."""

    async def _run() -> list[DuplicateCandidate]:
        vault_root = _require_vault_root(_config)
        qdrant_client = _try_get_qdrant_client(_config) or QdrantClient(url=_config.qdrant_url)
        async with open_connection(_config.db_path) as conn:
            return await scan_for_duplicates(
                conn, qdrant_client, vault_root.path, note_ids=None
            )

    candidates = asyncio.run(_run())
    logger.info(
        "duplicates_scan_task(correlation_id=%s) found %d candidate(s)",
        correlation_id,
        len(candidates),
    )


@huey.task(retries=3, retry_delay=10)  # type: ignore[untyped-decorator]  # huey ships no py.typed
def reindex_task(note_id: int | None, correlation_id: str) -> None:
    """MCP `reindex_start` tool's task-backed counterpart (design doc
    §2.3). A specific `note_id` re-indexes just that note; `None` re-runs
    the whole-vault `index_bootstrap` pass. Requires a real, reachable
    Qdrant client (`_get_qdrant_client`, not the best-effort `_try_get_
    qdrant_client`) -- same reasoning as `run_index_bootstrap`: there is no
    meaningful metadata-only fallback for an operation whose entire purpose
    is indexing."""

    async def _run() -> None:
        vault_root = _require_vault_root(_config)
        qdrant_client = _get_qdrant_client(_config)
        async with open_connection(_config.db_path) as conn:
            if note_id is not None:
                await index_note(
                    conn,
                    qdrant_client,
                    vault_root,
                    note_id,
                    correlation_id=correlation_id,
                    causation_id=None,
                )
            else:
                await index_bootstrap(
                    conn, qdrant_client, vault_root, correlation_id=correlation_id
                )

    asyncio.run(_run())


@huey.task(  # type: ignore[untyped-decorator]  # huey ships no py.typed
    context=True, retries=3, retry_delay=10
)
def research_task(urls: list[str], topic: str, correlation_id: str, task: object = None) -> None:
    """MCP `research_start` tool's task-backed counterpart (design doc
    §2.4). Unlike `duplicates_scan_task`/`reindex_task`, this task DOES
    touch `research_jobs` itself: the draft it produces is per-job state
    only this task ever computes, so it cannot be left for the dispatching
    MCP tool call to record up front the way `job_type`/`query` are.

    `job_id` is deliberately not a parameter -- it isn't known until after
    dispatch returns a huey task id and the MCP tool layer inserts the
    `research_jobs` row keyed to it (mirroring `duplicates_scan`/
    `reindex_start`'s existing insert-after-dispatch order). Instead,
    `context=True` injects this task's own `Task` instance as `task`, and
    `task.id` is looked up against `research_jobs.huey_task_id` at
    execution time. If the row hasn't been inserted yet (the dispatching
    call is still mid-flight when a fast worker picks this up), this raises
    -- caught by huey's own `retries=3, retry_delay=10`, giving the
    dispatcher's own fast, synchronous insert plenty of time to land before
    the retry.

    Never writes to the vault -- only `commit_draft` does that, and only
    after an explicit `research_commit` call with a confirmed, non-dry-run
    request.
    """
    huey_task_id = task.id  # type: ignore[attr-defined]  # injected by context=True

    async def _find_job_id() -> int:
        async with open_connection(_config.db_path) as conn:
            job = await research_jobs_repo.get_by_huey_task_id(conn, huey_task_id)
            if job is None:
                raise RuntimeError(
                    f"no research_jobs row for huey_task_id={huey_task_id!r} yet "
                    "-- retrying"
                )
            return job.id

    job_id = asyncio.run(_find_job_id())

    draft: ResearchDraft = run_research(urls, topic)

    async def _record() -> None:
        async with open_connection(_config.db_path) as conn:
            await research_jobs_repo.record_draft(
                conn,
                job_id,
                draft_title=draft.title,
                draft_body=draft.body,
                draft_source_urls=draft.succeeded_urls,
            )
            await research_jobs_repo.mark_finished(
                conn, job_id, status="succeeded", finished_at=_now()
            )

    asyncio.run(_record())
    logger.info(
        "research_task(job_id=%s, correlation_id=%s) succeeded_urls=%d failed_urls=%d",
        job_id,
        correlation_id,
        len(draft.succeeded_urls),
        len(draft.failed_urls),
    )


def run_research_commit(
    config: AthenaConfig | None = None,
    *,
    job_id: int,
    dry_run: bool,
    target_path: str | None = None,
) -> CommitResult:
    """Synchronous entry point for `athena research commit` -- requires a
    reachable Qdrant client only when `dry_run=False` (a preview needs no
    indexing), matching `run_index_bootstrap`'s "no meaningful metadata-only
    mode" reasoning for the write path while a dry-run stays as cheap as
    `job_status`."""
    active_config = config or _config
    vault_root = _require_vault_root(active_config)
    qdrant_client = _get_qdrant_client(active_config) if not dry_run else QdrantClient(
        url=active_config.qdrant_url
    )

    async def _run() -> CommitResult:
        async with open_connection(active_config.db_path) as conn:
            return await commit_draft(
                conn,
                qdrant_client,
                vault_root,
                job_id,
                dry_run=dry_run,
                target_path=target_path,
                committed_by="cli",
                block_on_high_confidence_secrets=active_config.secret_scanner_block_on_high_confidence,
            )

    return asyncio.run(_run())


def start_watcher(config: AthenaConfig | None = None) -> VaultWatcher:
    """Start the filesystem watcher and run one reconciliation pass inline
    before the periodic schedule takes over (design doc §2.7's "both
    startup and periodic" resolution). Called from `_worker_startup` below,
    not from `huey_consumer` itself (which only knows about `@huey.task`-
    decorated functions)."""
    active_config = config or _config
    vault_root = _require_vault_root(active_config)

    watcher = VaultWatcher(str(vault_root.path), on_settle=_on_settle)
    watcher.start()

    try:
        run_reconcile(active_config)
    except Exception:
        logger.exception("startup reconciliation pass failed")

    return watcher


_watcher: VaultWatcher | None = None


@huey.on_startup()  # type: ignore[untyped-decorator]  # huey ships no py.typed
def _worker_startup() -> None:
    global _watcher
    _watcher = start_watcher(_config)
