"""ATHENA AI-BRAIN command-line entry point.

Per `docs/ROADMAP.md` Phase 1: a minimal CLI plus the `doctor` diagnostics
command. Subcommands grow here as later phases add real functionality;
nothing here talks to the vault/Qdrant/Huey directly — it only wires
`athena.config`/`athena.diagnostics` together for a human at a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

from athena import __version__
from athena.config import load_config
from athena.diagnostics import DoctorReport, run_doctor
from athena.logging_setup import configure_logging

if TYPE_CHECKING:
    # Only for module-level helpers' type annotations (`_require_vault_root_
    # for_cli`, `_init_bench_git_repo`) -- this module's stated policy is
    # lazy imports inside command functions for anything not needed by every
    # subcommand, and TYPE_CHECKING-guarded imports never execute at runtime.
    from pathlib import Path

    from athena.config import AthenaConfig
    from athena.safety.paths import VaultRoot

_STATUS_SYMBOL = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}


def _print_doctor_report(report: DoctorReport) -> None:
    for check in report.checks:
        print(f"{_STATUS_SYMBOL[check.status]} {check.name}: {check.message}")
    print()
    print(f"Overall: {report.overall}")


def _cmd_doctor(_args: argparse.Namespace) -> int:
    config = load_config()
    report = run_doctor(config)
    _print_doctor_report(report)
    return report.exit_code


def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"athena {__version__}")
    return 0


def _cmd_migrate(_args: argparse.Namespace) -> int:
    # Imported lazily: aiosqlite-only, no huey/vault requirement, but keeping
    # it out of the top-level import list matches this module's existing
    # policy of not pulling in a subcommand's dependencies until it's used.
    from athena.db.connection import open_connection
    from athena.db.migrate import (
        DEFAULT_MIGRATIONS_DIR,
        MigrationError,
        MigrationRecord,
        apply_pending_migrations,
    )

    config = load_config()

    async def _run() -> list[MigrationRecord]:
        async with open_connection(config.db_path) as conn:
            return await apply_pending_migrations(conn, DEFAULT_MIGRATIONS_DIR)

    try:
        records = asyncio.run(_run())
    except MigrationError as exc:
        print(f"[FAIL] {exc}")
        return 1

    for record in records:
        print(f"[ok]   {record.filename} (version {record.version}, applied {record.applied_at})")
    print(f"\nSchema at version {records[-1].version if records else 0}.")
    return 0


def _cmd_ingest_bootstrap(_args: argparse.Namespace) -> int:
    # athena.worker constructs a Huey instance and hard-fails at import
    # time if ATHENA_HUEY_SECRET is misconfigured (design doc §2.10) --
    # imported here, not at module top level, so `doctor`/`version`/`migrate`
    # never require a Huey secret just to run.
    from athena.worker import run_bootstrap

    summary = run_bootstrap()
    print(f"notes_ingested={summary.notes_ingested} notes_skipped={summary.notes_skipped} ")
    print(f"notes_failed={summary.notes_failed} duration_ms={summary.duration_ms}")
    print(f"outcome_counts={summary.outcome_counts}")
    return 1 if summary.notes_failed else 0


def _cmd_ingest_reconcile(_args: argparse.Namespace) -> int:
    from athena.worker import run_reconcile

    summary = run_reconcile()
    print(
        f"paths_scanned={summary.paths_scanned} "
        f"discrepancies_found={summary.discrepancies_found} "
        f"jobs_enqueued={summary.jobs_enqueued} duration_ms={summary.duration_ms}"
    )
    return 0


def _cmd_index_bootstrap(_args: argparse.Namespace) -> int:
    from athena.worker import run_index_bootstrap

    # Unlike ingest bootstrap/reconcile, this command has no meaningful
    # metadata-only fallback -- it IS the indexing command -- so a Qdrant
    # connection failure is reported as a clean CLI error, not degraded
    # silently or left to leak a raw traceback (design doc §6/§8).
    try:
        summary = run_index_bootstrap()
    except Exception as exc:
        print(f"[FAIL] unable to reach Qdrant: {exc}")
        return 1
    print(f"notes_indexed={summary.notes_indexed} notes_failed={summary.notes_failed}")
    return 1 if summary.notes_failed else 0


def _cmd_retrieval_evaluate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from athena.worker import run_retrieval_evaluate

    corpus_dir = Path(args.corpus) if args.corpus else None
    report = run_retrieval_evaluate(corpus_dir=corpus_dir)

    print(f"questions: {report.num_questions} ({report.num_answerable} answerable)")
    for k, value in report.recall_at_k.items():
        print(f"recall@{k}: {value:.3f}")
    for k, value in report.precision_at_k.items():
        print(f"precision@{k}: {value:.3f}")
    print(f"mrr: {report.mrr:.3f}")
    print(f"ndcg@10: {report.ndcg_at_10:.3f}")
    print(
        "unanswerable_top1_false_positive_rate: "
        f"{report.unanswerable_top1_false_positive_rate:.3f}"
    )
    print(f"latency p50={report.p50_latency_ms:.1f}ms p95={report.p95_latency_ms:.1f}ms")
    # No regression-gating threshold in this pass -- an explicit, flagged
    # scope reduction (design doc §2.6/§8), not an oversight: this command
    # always exits 0 on a successful run, reporting numbers for a human (or
    # a future CI step) to compare, rather than gating the build itself.
    return 0


def _cmd_duplicates_scan(args: argparse.Namespace) -> int:
    from athena.worker import run_duplicates_scan

    candidates = run_duplicates_scan(threshold=args.threshold)
    print(f"candidates: {len(candidates)}")
    for candidate in candidates:
        print(
            f"  id={candidate.id} notes=({candidate.note_a_id}, {candidate.note_b_id}) "
            f"method={candidate.detection_method} combined_score={candidate.combined_score:.3f} "
            f"status={candidate.status}"
        )
    return 0


def _cmd_duplicates_list(args: argparse.Namespace) -> int:
    from athena.worker import run_duplicates_list

    candidates = run_duplicates_list(status=args.status)
    print(f"{args.status}: {len(candidates)}")
    for candidate in candidates:
        print(
            f"  id={candidate.id} notes=({candidate.note_a_id}, {candidate.note_b_id}) "
            f"method={candidate.detection_method} combined_score={candidate.combined_score:.3f}"
        )
    return 0


def _cmd_duplicates_resolve(args: argparse.Namespace) -> int:
    from athena.worker import run_duplicates_resolve

    resolution = "confirmed" if args.confirm else "rejected"
    try:
        run_duplicates_resolve(
            candidate_id=args.candidate_id, resolution=resolution, resolved_by="cli"
        )
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"candidate {args.candidate_id}: {resolution}")
    return 0


def _cmd_duplicates_merge(args: argparse.Namespace) -> int:
    from athena.worker import run_duplicates_list, run_duplicates_merge

    # The candidate id names the pair; --keep picks which of its two notes
    # survives. Looking the candidate up (rather than taking both note ids
    # directly on the command line) keeps the CLI's contract tied to a
    # specific reviewed candidate, not just any two arbitrary note ids.
    candidate = next(
        (c for c in run_duplicates_list(status="confirmed") if c.id == args.candidate_id), None
    )
    if candidate is None:
        print(f"[FAIL] no 'confirmed' duplicate candidate with id={args.candidate_id}")
        return 1
    if args.keep not in (candidate.note_a_id, candidate.note_b_id):
        print(
            f"[FAIL] --keep {args.keep} is not one of candidate {args.candidate_id}'s notes "
            f"({candidate.note_a_id}, {candidate.note_b_id})"
        )
        return 1
    absorb_note_id = (
        candidate.note_b_id if args.keep == candidate.note_a_id else candidate.note_a_id
    )

    try:
        result = run_duplicates_merge(
            keep_note_id=args.keep, absorb_note_id=absorb_note_id, merged_by="cli"
        )
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"merged note_id={result.absorbed_note_id} into note_id={result.kept_note_id}")
    return 0


def _cmd_lifecycle_stale_sweep(args: argparse.Namespace) -> int:
    from athena.worker import run_stale_sweep

    summary = run_stale_sweep(stale_after_days=args.stale_after_days)
    print(
        f"notes_flagged={summary.notes_flagged} "
        f"notes_skipped_duplicate_pending={summary.notes_skipped_duplicate_pending}"
    )
    return 0


def _cmd_research_start(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime
    from uuid import uuid4

    from athena.db.connection import open_connection
    from athena.db.repository import research_jobs as research_jobs_repo

    config = load_config()

    async def _check_dispatch_limit() -> None:
        async with open_connection(config.db_path) as conn:
            await research_jobs_repo.check_daily_dispatch_limit(
                conn,
                job_type="research_start",
                max_per_day=config.research_max_dispatches_per_day,
            )

    try:
        asyncio.run(_check_dispatch_limit())
    except research_jobs_repo.DispatchLimitError as exc:
        print(f"[FAIL] {exc}")
        return 1

    # athena.worker constructs a Huey instance at import time (same reason
    # every other worker-dispatching command imports lazily here) -- deferred
    # until after the dispatch-limit check above passes, so a refused
    # dispatch never requires a configured Huey secret either.
    import athena.worker

    correlation_id = str(uuid4())
    result = athena.worker.research_task(args.url, args.topic, correlation_id)

    async def _insert() -> int:
        async with open_connection(config.db_path) as conn:
            return await research_jobs_repo.insert(
                conn,
                huey_task_id=result.id,
                job_type="research_start",
                query=args.topic,
                created_at=datetime.now(UTC).isoformat(),
            )

    job_id = asyncio.run(_insert())
    print(f"job dispatched: job_id={job_id}")
    return 0


def _cmd_research_commit(args: argparse.Namespace) -> int:
    from athena.worker import run_research_commit

    try:
        result = run_research_commit(
            job_id=args.job_id, dry_run=not args.commit, target_path=args.target_path
        )
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 1

    if not args.commit:
        print(f"[dry run] title={result.preview_title!r}")
        print(result.preview_body)
        print("\nRe-run with --commit to actually write this note into the vault.")
    else:
        print(f"committed: note_id={result.note_id}")
    return 0


_NOT_A_GIT_REPOSITORY_MESSAGE = (
    "[FAIL] vault is not a Git repository -- run `git init` in the vault directory "
    "to enable Git automation"
)


def _require_vault_root_for_cli(config: AthenaConfig) -> VaultRoot | None:
    from athena.safety.paths import VaultRoot

    if config.vault_root is None:
        print("[FAIL] ATHENA_VAULT_DIR is not set -- no vault is configured")
        return None
    return VaultRoot.initialize(config.vault_root)


def _cmd_git_status(_args: argparse.Namespace) -> int:
    # A direct athena.git.read bridge, mirroring _cmd_migrate's own
    # asyncio.run() bridge -- no Huey/worker.py involvement and no
    # ATHENA_HUEY_SECRET requirement for a read-only Git status check.
    from athena.git.read import GitStatus, get_status, is_git_repository

    config = load_config()
    vault_root = _require_vault_root_for_cli(config)
    if vault_root is None:
        return 1

    async def _run() -> GitStatus | None:
        if not await is_git_repository(vault_root, timeout_s=config.git_command_timeout_s):
            return None
        return await get_status(vault_root, timeout_s=config.git_command_timeout_s)

    status = asyncio.run(_run())
    if status is None:
        print(_NOT_A_GIT_REPOSITORY_MESSAGE)
        return 1

    print(f"working tree: {'clean' if status.is_clean else 'dirty'}")
    if status.changed_paths:
        print("changed paths:")
        for path in status.changed_paths:
            print(f"  {path}")
    if status.ahead_behind is not None:
        print(
            f"ahead {status.ahead_behind.ahead}, behind {status.ahead_behind.behind} "
            "(relative to upstream)"
        )
    return 0


def _cmd_git_log(args: argparse.Namespace) -> int:
    from athena.git.read import GitLogEntry, get_log, is_git_repository

    config = load_config()
    vault_root = _require_vault_root_for_cli(config)
    if vault_root is None:
        return 1

    async def _run() -> list[GitLogEntry] | None:
        if not await is_git_repository(vault_root, timeout_s=config.git_command_timeout_s):
            return None
        return await get_log(vault_root, limit=args.limit, timeout_s=config.git_command_timeout_s)

    entries = asyncio.run(_run())
    if entries is None:
        print(_NOT_A_GIT_REPOSITORY_MESSAGE)
        return 1

    if not entries:
        print("no commit history yet")
        return 0
    for entry in entries:
        print(f"{entry.sha[:8]}  {entry.date}  {entry.subject} ({entry.author})")
    return 0


def _cmd_git_commit(args: argparse.Namespace) -> int:
    from athena.git.read import get_status, is_git_repository
    from athena.git.wrapper import GitFailureKind
    from athena.git.write import CommitResult, commit_paths

    config = load_config()
    vault_root = _require_vault_root_for_cli(config)
    if vault_root is None:
        return 1
    dry_run = not args.commit

    async def _run() -> CommitResult | None:
        if not await is_git_repository(vault_root, timeout_s=config.git_command_timeout_s):
            return None
        status = await get_status(vault_root, timeout_s=config.git_command_timeout_s)
        message = args.message or (
            f"manual commit via git_commit: {len(status.changed_paths)} file(s) changed"
        )
        return await commit_paths(
            vault_root, ["."], message, dry_run=dry_run, timeout_s=config.git_command_timeout_s
        )

    result = asyncio.run(_run())
    if result is None:
        print(_NOT_A_GIT_REPOSITORY_MESSAGE)
        return 1

    if dry_run:
        print(f"[dry run] {result.dry_run_preview}")
        print("\nRe-run with --commit to actually write this commit.")
        return 0

    if not result.committed:
        if result.failure_kind is GitFailureKind.NOTHING_TO_COMMIT:
            print("nothing to commit")
            return 0
        print(f"[FAIL] commit failed: {result.failure_kind.value}")
        return 1

    print(f"committed: sha={result.sha}")
    return 0


def _cmd_git_push(args: argparse.Namespace) -> int:
    from athena.git.read import is_git_repository
    from athena.git.write import PushResult, push

    config = load_config()
    vault_root = _require_vault_root_for_cli(config)
    if vault_root is None:
        return 1
    dry_run = not args.push

    async def _run() -> PushResult | None:
        if not await is_git_repository(vault_root, timeout_s=config.git_command_timeout_s):
            return None
        return await push(vault_root, dry_run=dry_run, timeout_s=config.git_command_timeout_s)

    result = asyncio.run(_run())
    if result is None:
        print(_NOT_A_GIT_REPOSITORY_MESSAGE)
        return 1

    if dry_run:
        print(f"[dry run] {result.dry_run_preview}")
        print("\nRe-run with --push to actually push.")
        return 0

    if not result.pushed:
        print(f"[FAIL] push failed: {result.failure_kind.value}")
        return 1

    print("pushed successfully")
    return 0


def _cmd_llm_summarize(args: argparse.Namespace) -> int:
    # A direct athena.llm bridge, mirroring _cmd_git_status's own
    # asyncio.run() bridge -- no Huey/worker.py involvement.
    from athena.db.connection import open_connection
    from athena.llm.provider import LLMProviderError, LLMTimeoutError
    from athena.llm.summarize import (
        LLMCallLimitError,
        LLMDisabledError,
        LLMNotConfiguredError,
        summarize_text,
    )
    from athena.safety.content import parse_note_safely
    from athena.safety.paths import PathMode, VaultPathError, resolve_vault_path

    config = load_config()
    vault_root = _require_vault_root_for_cli(config)
    if vault_root is None:
        return 1

    try:
        safe_path = resolve_vault_path(args.path, vault_root, PathMode.EXISTING)
    except VaultPathError as exc:
        print(f"[FAIL] cannot read vault note at {args.path!r}: {exc}")
        return 1

    raw_text = safe_path.path.read_text(encoding="utf-8")
    vault_relative_path = safe_path.path.relative_to(vault_root.path).as_posix()
    folder = vault_relative_path.rsplit("/", 1)[0] if "/" in vault_relative_path else ""
    parsed = parse_note_safely(raw_text, folder_name=folder)

    async def _run() -> str:
        async with open_connection(config.db_path) as conn:
            return await summarize_text(
                conn, parsed.body, config=config, source_label=vault_relative_path
            )

    try:
        summary = asyncio.run(_run())
    except (LLMDisabledError, LLMNotConfiguredError, LLMCallLimitError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    except LLMTimeoutError as exc:
        print(f"[FAIL] summarization timed out: {exc}")
        return 1
    except LLMProviderError as exc:
        print(f"[FAIL] summarization failed: {exc}")
        return 1

    print(summary)
    return 0


def _cmd_bench_index(_args: argparse.Namespace) -> int:
    # A real ingest+index pass against the configured vault, reusing
    # `athena.worker.run_bootstrap`/`run_index_bootstrap` exactly as
    # `athena ingest bootstrap`/`athena index bootstrap` already do --
    # imported lazily for the same ATHENA_HUEY_SECRET reason as those two
    # commands. No `--corpus` flag: both functions only ever read
    # `config.vault_root` (there is no parameter to redirect them at an
    # arbitrary directory), so this benchmarks whatever `ATHENA_VAULT_DIR`
    # is currently configured to, matching the design doc's §2.4 fallback
    # ("simplify to always benchmark against the configured vault" when
    # `--corpus` doesn't cleanly compose).
    import time

    start = time.perf_counter()
    try:
        # The import itself is inside this `try` -- not just the calls --
        # because `athena.worker` hard-fails at import time if
        # ATHENA_HUEY_SECRET is misconfigured (design doc §2.10); that must
        # produce the same clean `[FAIL]` here as an unconfigured vault or
        # an unreachable Qdrant, never a raw traceback (design doc §5's
        # failure-mode table).
        from athena.worker import run_bootstrap, run_index_bootstrap

        bootstrap_summary = run_bootstrap()
        index_summary = run_index_bootstrap()
    except Exception as exc:
        print(f"[FAIL] bench index run did not complete: {exc}")
        return 1
    duration_s = time.perf_counter() - start

    notes_per_sec = index_summary.notes_indexed / duration_s if duration_s > 0 else 0.0

    print("=== Index Benchmark ===")
    print(
        f"ingest: notes_ingested={bootstrap_summary.notes_ingested} "
        f"notes_skipped={bootstrap_summary.notes_skipped} "
        f"notes_failed={bootstrap_summary.notes_failed}"
    )
    print(
        f"index: notes_indexed={index_summary.notes_indexed} "
        f"notes_failed={index_summary.notes_failed}"
    )
    print(f"total_duration_s={duration_s:.3f}")
    print(f"notes_per_sec={notes_per_sec:.2f}")
    # "Report, don't gate" (design doc §2.4, matching `retrieval evaluate`'s
    # own already-accepted philosophy): this only fails if the run itself
    # didn't complete (the `except` above), never on the numbers produced.
    return 0


def _cmd_bench_retrieval(args: argparse.Namespace) -> int:
    # A thin wrapper around `run_retrieval_evaluate` -- same --corpus
    # handling as `_cmd_retrieval_evaluate`, no new measurement logic, just
    # benchmark-oriented framing around the exact same figures.
    from pathlib import Path

    from athena.worker import run_retrieval_evaluate

    corpus_dir = Path(args.corpus) if args.corpus else None
    report = run_retrieval_evaluate(corpus_dir=corpus_dir)

    print("=== Retrieval Benchmark ===")
    print(f"questions: {report.num_questions} ({report.num_answerable} answerable)")
    for k, value in report.recall_at_k.items():
        print(f"recall@{k}: {value:.3f}")
    for k, value in report.precision_at_k.items():
        print(f"precision@{k}: {value:.3f}")
    print(f"mrr: {report.mrr:.3f}")
    print(f"ndcg@10: {report.ndcg_at_10:.3f}")
    print(
        "unanswerable_top1_false_positive_rate: "
        f"{report.unanswerable_top1_false_positive_rate:.3f}"
    )
    print(f"latency p50={report.p50_latency_ms:.1f}ms p95={report.p95_latency_ms:.1f}ms")
    return 0


def _init_bench_git_repo(path: Path) -> None:
    """Initialize a throwaway Git repository for `athena bench mutation`.

    Deliberately duplicates `tests/git/conftest.py`'s `init_repo` fixture
    (same `git init`/`git config user.name`/`git config user.email`
    sequence) rather than importing it: this module ships as part of
    `athena`'s installed package, and reaching into `tests/` from `src/` at
    runtime would make a production CLI command depend on the test tree
    being present on disk. Four duplicated lines here are cheaper than that
    coupling.
    """
    import shutil
    import subprocess

    # A resolved, absolute path (not the bare "git" string) is passed to
    # subprocess.run -- matching `athena.git.wrapper.run_git`'s own
    # argument-list-only, never-shell discipline (ADR-0005). Every argument
    # below is a fixed literal (no caller/network input reaches this list),
    # the same false-positive shape this codebase already silences with an
    # explicit `noqa` elsewhere (e.g. `athena.db.repository.notes`'s S608
    # comments) rather than broadening a per-file ignore.
    git_executable = shutil.which("git")
    if git_executable is None:
        raise RuntimeError("git executable not found on PATH")

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 -- fixed argv, no shell, no untrusted input
        [git_executable, "init", "-q", "-b", "main"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603 -- fixed argv, no shell, no untrusted input
        [git_executable, "config", "user.name", "Athena Bench"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(  # noqa: S603 -- fixed argv, no shell, no untrusted input
        [git_executable, "config", "user.email", "athena-bench@example.invalid"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _cmd_bench_mutation(args: argparse.Namespace) -> int:
    # No existing command measures exactly this -- a real filesystem write
    # + `athena.git.write.commit_paths` round trip (the same sequence
    # `athena.mcp_server.write_tools.note_create` performs, minus the
    # database bookkeeping, which isn't the thing worth timing here)
    # against a real throwaway git-repo vault in a temp directory.
    import tempfile
    import time
    from pathlib import Path

    from athena.git.write import commit_paths
    from athena.safety.paths import VaultRoot

    if args.iterations < 1:
        print("[FAIL] --iterations must be at least 1")
        return 1

    config = load_config()

    async def _run(vault_dir: Path) -> list[float]:
        vault_root = VaultRoot.initialize(vault_dir)
        durations: list[float] = []
        for i in range(args.iterations):
            start = time.perf_counter()
            rel_path = f"bench-note-{i:04d}.md"
            (vault_dir / rel_path).write_text(f"bench note {i}\n", encoding="utf-8")
            await commit_paths(
                vault_root,
                [rel_path],
                f"bench: note {i}",
                timeout_s=config.git_command_timeout_s,
            )
            durations.append(time.perf_counter() - start)
        return durations

    with tempfile.TemporaryDirectory(prefix="athena-bench-mutation-") as tmp_dir:
        vault_dir = Path(tmp_dir) / "vault"
        _init_bench_git_repo(vault_dir)
        durations = asyncio.run(_run(vault_dir))

    durations_ms = sorted(d * 1000 for d in durations)
    mean_ms = sum(durations_ms) / len(durations_ms)
    p95_ms = durations_ms[int(0.95 * len(durations_ms))]

    print("=== Mutation Benchmark ===")
    print(f"iterations={args.iterations}")
    print(f"mean={mean_ms:.2f}ms p95={p95_ms:.2f}ms")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="athena", description="ATHENA AI-BRAIN CLI")
    parser.add_argument(
        "--log-level",
        default=None,
        help="override ATHENA_LOG_LEVEL for this invocation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="run diagnostics")
    doctor_parser.set_defaults(func=_cmd_doctor)

    version_parser = subparsers.add_parser("version", help="print the ATHENA AI-BRAIN version")
    version_parser.set_defaults(func=_cmd_version)

    migrate_parser = subparsers.add_parser("migrate", help="apply pending database migrations")
    migrate_parser.set_defaults(func=_cmd_migrate)

    ingest_parser = subparsers.add_parser("ingest", help="vault ingestion commands")
    ingest_subparsers = ingest_parser.add_subparsers(dest="ingest_command", required=True)

    bootstrap_parser = ingest_subparsers.add_parser(
        "bootstrap", help="one-time full-vault ingestion"
    )
    bootstrap_parser.set_defaults(func=_cmd_ingest_bootstrap)

    reconcile_parser = ingest_subparsers.add_parser(
        "reconcile", help="run one on-demand reconciliation pass"
    )
    reconcile_parser.set_defaults(func=_cmd_ingest_reconcile)

    index_parser = subparsers.add_parser("index", help="indexing pipeline commands")
    index_subparsers = index_parser.add_subparsers(dest="index_command", required=True)

    index_bootstrap_parser = index_subparsers.add_parser(
        "bootstrap", help="index every note not currently indexed"
    )
    index_bootstrap_parser.set_defaults(func=_cmd_index_bootstrap)

    retrieval_parser = subparsers.add_parser("retrieval", help="retrieval pipeline commands")
    retrieval_subparsers = retrieval_parser.add_subparsers(
        dest="retrieval_command", required=True
    )

    evaluate_parser = retrieval_subparsers.add_parser(
        "evaluate", help="run the retrieval evaluation corpus and print metrics"
    )
    evaluate_parser.add_argument(
        "--corpus",
        default=None,
        help=(
            "path to a corpus directory containing questions.json "
            "(default: the bundled starter corpus)"
        ),
    )
    evaluate_parser.set_defaults(func=_cmd_retrieval_evaluate)

    duplicates_parser = subparsers.add_parser("duplicates", help="duplicate detection and merge")
    duplicates_subparsers = duplicates_parser.add_subparsers(
        dest="duplicates_command", required=True
    )

    duplicates_scan_parser = duplicates_subparsers.add_parser(
        "scan", help="scan active notes for likely duplicates"
    )
    duplicates_scan_parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="minimum combined score to record a candidate (default: 0.5)",
    )
    duplicates_scan_parser.set_defaults(func=_cmd_duplicates_scan)

    duplicates_list_parser = duplicates_subparsers.add_parser(
        "list", help="list duplicate candidates by status"
    )
    duplicates_list_parser.add_argument(
        "--status",
        default="pending",
        choices=["pending", "confirmed", "rejected", "merged"],
        help="candidate status to list (default: pending)",
    )
    duplicates_list_parser.set_defaults(func=_cmd_duplicates_list)

    duplicates_resolve_parser = duplicates_subparsers.add_parser(
        "resolve", help="confirm or reject a pending duplicate candidate"
    )
    duplicates_resolve_parser.add_argument("candidate_id", type=int)
    resolve_group = duplicates_resolve_parser.add_mutually_exclusive_group(required=True)
    resolve_group.add_argument("--confirm", action="store_true")
    resolve_group.add_argument("--reject", action="store_true")
    duplicates_resolve_parser.set_defaults(func=_cmd_duplicates_resolve)

    duplicates_merge_parser = duplicates_subparsers.add_parser(
        "merge", help="merge a confirmed duplicate candidate's two notes"
    )
    duplicates_merge_parser.add_argument("candidate_id", type=int)
    duplicates_merge_parser.add_argument(
        "--keep", type=int, required=True, help="note_id to keep; the other note is absorbed"
    )
    duplicates_merge_parser.set_defaults(func=_cmd_duplicates_merge)

    lifecycle_parser = subparsers.add_parser("lifecycle", help="knowledge lifecycle commands")
    lifecycle_subparsers = lifecycle_parser.add_subparsers(
        dest="lifecycle_command", required=True
    )

    stale_sweep_parser = lifecycle_subparsers.add_parser(
        "stale-sweep", help="flag long-untouched active/verified notes as stale"
    )
    stale_sweep_parser.add_argument(
        "--stale-after-days",
        type=int,
        default=180,
        dest="stale_after_days",
        help="days without an update before a note is flagged stale (default: 180)",
    )
    stale_sweep_parser.set_defaults(func=_cmd_lifecycle_stale_sweep)

    research_parser = subparsers.add_parser("research", help="web research and ingestion")
    research_subparsers = research_parser.add_subparsers(dest="research_command", required=True)

    research_start_parser = research_subparsers.add_parser(
        "start", help="dispatch a background web-research job"
    )
    research_start_parser.add_argument(
        "--url",
        dest="url",
        action="append",
        required=True,
        help="a URL to fetch (repeatable)",
    )
    research_start_parser.add_argument("--topic", required=True, help="the research topic/title")
    research_start_parser.set_defaults(func=_cmd_research_start)

    research_commit_parser = research_subparsers.add_parser(
        "commit", help="preview or write a completed research job's draft into the vault"
    )
    research_commit_parser.add_argument("job_id", type=int)
    research_commit_parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "actually write the note into the vault (an explicit flag, not merely omitting "
            "--dry-run -- the CLI has no elicitation mechanism to confirm otherwise)"
        ),
    )
    research_commit_parser.add_argument(
        "--target-path",
        dest="target_path",
        default=None,
        help="vault-relative path for the new note (default: a slug of the topic under research/)",
    )
    research_commit_parser.set_defaults(func=_cmd_research_commit)

    git_parser = subparsers.add_parser("git", help="vault Git automation commands")
    git_subparsers = git_parser.add_subparsers(dest="git_command", required=True)

    git_status_parser = git_subparsers.add_parser(
        "status", help="show the vault repository's working-tree state"
    )
    git_status_parser.set_defaults(func=_cmd_git_status)

    git_log_parser = git_subparsers.add_parser(
        "log", help="show the vault repository's recent commit history"
    )
    git_log_parser.add_argument(
        "--limit", type=int, default=20, help="maximum number of commits to show (default: 20)"
    )
    git_log_parser.set_defaults(func=_cmd_git_log)

    git_commit_parser = git_subparsers.add_parser(
        "commit", help="commit everything currently dirty in the vault repository"
    )
    git_commit_parser.add_argument(
        "--message", default=None, help="commit message (default: an auto-generated one)"
    )
    git_commit_group = git_commit_parser.add_mutually_exclusive_group()
    git_commit_group.add_argument(
        "--dry-run",
        dest="commit",
        action="store_false",
        help="preview only, write nothing (default)",
    )
    git_commit_group.add_argument(
        "--commit",
        dest="commit",
        action="store_true",
        help=(
            "actually write the commit (an explicit flag, not merely omitting --dry-run -- "
            "the CLI has no elicitation mechanism to confirm otherwise)"
        ),
    )
    git_commit_parser.set_defaults(func=_cmd_git_commit, commit=False)

    git_push_parser = git_subparsers.add_parser(
        "push", help="push the vault repository's current branch to its remote"
    )
    git_push_group = git_push_parser.add_mutually_exclusive_group()
    git_push_group.add_argument(
        "--dry-run",
        dest="push",
        action="store_false",
        help="preview only, push nothing (default)",
    )
    git_push_group.add_argument(
        "--push",
        dest="push",
        action="store_true",
        help=(
            "actually push (an explicit flag, not merely omitting --dry-run -- the CLI has "
            "no elicitation mechanism to confirm otherwise)"
        ),
    )
    git_push_parser.set_defaults(func=_cmd_git_push, push=False)

    llm_parser = subparsers.add_parser("llm", help="LLM-backed commands")
    llm_subparsers = llm_parser.add_subparsers(dest="llm_command", required=True)

    llm_summarize_parser = llm_subparsers.add_parser(
        "summarize", help="summarize a vault note via the configured default LLM provider"
    )
    llm_summarize_parser.add_argument("path", help="vault-relative path to the note")
    llm_summarize_parser.set_defaults(func=_cmd_llm_summarize)

    bench_parser = subparsers.add_parser("bench", help="performance benchmarking commands")
    bench_subparsers = bench_parser.add_subparsers(dest="bench_command", required=True)

    bench_index_parser = bench_subparsers.add_parser(
        "index", help="benchmark a full ingest+index pass against the configured vault"
    )
    bench_index_parser.set_defaults(func=_cmd_bench_index)

    bench_retrieval_parser = bench_subparsers.add_parser(
        "retrieval",
        help="benchmark retrieval latency (thin wrapper around `retrieval evaluate`)",
    )
    bench_retrieval_parser.add_argument(
        "--corpus",
        default=None,
        help=(
            "path to a corpus directory containing questions.json "
            "(default: the bundled starter corpus)"
        ),
    )
    bench_retrieval_parser.set_defaults(func=_cmd_bench_retrieval)

    bench_mutation_parser = bench_subparsers.add_parser(
        "mutation",
        help="benchmark a note-write -> auto-commit round trip against a throwaway vault",
    )
    bench_mutation_parser.add_argument(
        "--iterations",
        type=int,
        default=20,
        help="number of write+commit round trips to measure (default: 20)",
    )
    bench_mutation_parser.set_defaults(func=_cmd_bench_mutation)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(args.log_level or config.log_level)

    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
