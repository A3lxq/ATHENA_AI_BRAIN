"""Tests for `athena.research.workflow` (docs/design/research-ingestion.md
§2.3/§7).
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest
from qdrant_client import QdrantClient
from tests.git.conftest import init_repo

from athena.db.repository import notes as notes_repo
from athena.db.repository import provenance as provenance_repo
from athena.db.repository import research_jobs as research_jobs_repo
from athena.git.read import get_log
from athena.research.extract import ExtractedArticle
from athena.research.fetch import FetchedPage, FetchRefused
from athena.research.workflow import commit_draft, run_research
from athena.safety.paths import VaultRoot

_AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"  # AWS's own published example key, not a real credential


# --- run_research ---------------------------------------------------------


def test_run_research_assembles_a_draft_from_successfully_extracted_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_url(url: str) -> FetchedPage:
        return FetchedPage(url=url, html="<html>irrelevant</html>", content_type="text/html")

    def fake_extract_article(html: str, url: str) -> ExtractedArticle | None:
        return ExtractedArticle(
            title="A Real Article", markdown_body="some real content", author=None, date=None
        )

    monkeypatch.setattr("athena.research.workflow.fetch_url", fake_fetch_url)
    monkeypatch.setattr("athena.research.workflow.extract_article", fake_extract_article)

    draft = run_research(["https://a.example/"], topic="My Topic")

    assert draft.title == "My Topic"
    assert draft.succeeded_urls == ["https://a.example/"]
    assert draft.failed_urls == []
    assert "## Source: https://a.example/" in draft.body
    assert "some real content" in draft.body


def test_run_research_continues_past_a_refused_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch_url(url: str) -> FetchedPage:
        if url == "http://169.254.169.254/":
            raise FetchRefused("blocked")
        return FetchedPage(url=url, html="<html>ok</html>", content_type="text/html")

    def fake_extract_article(html: str, url: str) -> ExtractedArticle | None:
        return ExtractedArticle(title="OK", markdown_body="ok content", author=None, date=None)

    monkeypatch.setattr("athena.research.workflow.fetch_url", fake_fetch_url)
    monkeypatch.setattr("athena.research.workflow.extract_article", fake_extract_article)

    draft = run_research(
        ["http://169.254.169.254/", "https://good.example/"], topic="Mixed batch"
    )

    assert draft.succeeded_urls == ["https://good.example/"]
    assert draft.failed_urls == ["http://169.254.169.254/"]
    assert "ok content" in draft.body


def test_run_research_continues_past_extraction_returning_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_url(url: str) -> FetchedPage:
        return FetchedPage(url=url, html="<html></html>", content_type="text/html")

    def fake_extract_article(html: str, url: str) -> ExtractedArticle | None:
        return None

    monkeypatch.setattr("athena.research.workflow.fetch_url", fake_fetch_url)
    monkeypatch.setattr("athena.research.workflow.extract_article", fake_extract_article)

    draft = run_research(["https://empty.example/"], topic="Nothing here")

    assert draft.succeeded_urls == []
    assert draft.failed_urls == ["https://empty.example/"]
    assert draft.body == "(no content could be extracted from any of the provided URLs)"


# --- commit_draft: dry_run and error paths --------------------------------


async def test_commit_draft_raises_for_unknown_job(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot
) -> None:
    with pytest.raises(ValueError, match="no such research job"):
        await commit_draft(
            conn, qdrant_client, vault_root, 999_999,
            dry_run=True, target_path=None, committed_by="test",
        )


async def test_commit_draft_raises_when_job_has_no_draft_yet(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot
) -> None:
    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t1", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )

    with pytest.raises(ValueError, match="no draft yet"):
        await commit_draft(
            conn, qdrant_client, vault_root, job_id,
            dry_run=True, target_path=None, committed_by="test",
        )


async def test_commit_draft_dry_run_writes_nothing(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot, vault_dir: Path
) -> None:
    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t2", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )
    await research_jobs_repo.record_draft(
        conn, job_id, draft_title="Preview Title", draft_body="preview body",
        draft_source_urls=["https://a.example/"],
    )

    result = await commit_draft(
        conn, qdrant_client, vault_root, job_id,
        dry_run=True, target_path=None, committed_by="test",
    )

    assert result.note_id is None
    assert result.preview_title == "Preview Title"
    assert result.preview_body == "preview body"
    assert list(vault_dir.rglob("*.md")) == []
    row = await notes_repo.get_by_path(conn, "research/preview-title.md")
    assert row is None


# --- commit_draft: real commit ---------------------------------------------


async def test_commit_draft_writes_note_provenance_and_sources(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot, vault_dir: Path
) -> None:
    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t3", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )
    await research_jobs_repo.record_draft(
        conn, job_id, draft_title="What is RAG",
        draft_body="RAG is retrieval-augmented generation.",
        draft_source_urls=["https://a.example/", "https://b.example/"],
    )

    result = await commit_draft(
        conn, qdrant_client, vault_root, job_id,
        dry_run=False, target_path=None, committed_by="test",
    )

    assert result.note_id is not None
    note = await notes_repo.get_by_id(conn, result.note_id)
    assert note is not None
    assert note.origin == "web_research"
    assert note.path == "research/what-is-rag.md"
    assert (vault_dir / "research" / "what-is-rag.md").read_text(
        encoding="utf-8"
    ) == "RAG is retrieval-augmented generation."

    activities = await provenance_repo.get_activities_for_note(conn, result.note_id)
    assert len(activities) == 1
    assert activities[0].activity_type == "web_research"

    cursor = await conn.execute(
        "SELECT url FROM provenance_sources WHERE provenance_id = ? ORDER BY url",
        (activities[0].id,),
    )
    urls = {row[0] for row in await cursor.fetchall()}
    assert urls == {"https://a.example/", "https://b.example/"}

    cursor = await conn.execute(
        "SELECT result_note_id FROM research_jobs WHERE id = ?", (job_id,)
    )
    row = await cursor.fetchone()
    assert row == (result.note_id,)


async def test_commit_draft_respects_explicit_target_path(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot, vault_dir: Path
) -> None:
    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t4", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )
    await research_jobs_repo.record_draft(
        conn, job_id, draft_title="Topic", draft_body="body", draft_source_urls=[]
    )

    result = await commit_draft(
        conn, qdrant_client, vault_root, job_id,
        dry_run=False, target_path="custom/path.md", committed_by="test",
    )

    note = await notes_repo.get_by_id(conn, result.note_id)  # type: ignore[arg-type]
    assert note is not None
    assert note.path == "custom/path.md"


async def test_commit_draft_refuses_to_overwrite_existing_target(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot, vault_dir: Path
) -> None:
    existing = vault_dir / "research" / "topic.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("already here", encoding="utf-8")

    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t5", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )
    await research_jobs_repo.record_draft(
        conn, job_id, draft_title="Topic", draft_body="new body", draft_source_urls=[]
    )

    with pytest.raises(ValueError, match="already exists"):
        await commit_draft(
            conn, qdrant_client, vault_root, job_id,
            dry_run=False, target_path="research/topic.md", committed_by="test",
        )
    assert existing.read_text(encoding="utf-8") == "already here"


async def test_commit_draft_redacts_a_high_confidence_secret(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot, vault_dir: Path
) -> None:
    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t6", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )
    await research_jobs_repo.record_draft(
        conn, job_id, draft_title="Leaky Page", draft_body=f"aws_key = {_AWS_EXAMPLE_KEY}",
        draft_source_urls=["https://leaky.example/"],
    )

    result = await commit_draft(
        conn, qdrant_client, vault_root, job_id,
        dry_run=False, target_path=None, committed_by="test",
    )

    assert _AWS_EXAMPLE_KEY not in result.preview_body
    on_disk = (vault_dir / "research" / "leaky-page.md").read_text(encoding="utf-8")
    assert _AWS_EXAMPLE_KEY not in on_disk

    cursor = await conn.execute(
        "SELECT secret_scan_status FROM notes WHERE id = ?", (result.note_id,)
    )
    row = await cursor.fetchone()
    assert row == ("flagged",)


async def test_commit_draft_blocks_when_configured_to_block_on_high_confidence(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot, vault_dir: Path
) -> None:
    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t7", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )
    await research_jobs_repo.record_draft(
        conn, job_id, draft_title="Leaky Page 2", draft_body=f"aws_key = {_AWS_EXAMPLE_KEY}",
        draft_source_urls=[],
    )

    with pytest.raises(ValueError, match="commit blocked"):
        await commit_draft(
            conn, qdrant_client, vault_root, job_id,
            dry_run=False, target_path=None, committed_by="test",
            block_on_high_confidence_secrets=True,
        )
    assert not (vault_dir / "research" / "leaky-page-2.md").exists()
    row = await notes_repo.get_by_path(conn, "research/leaky-page-2.md")
    assert row is None


# --- commit_draft: auto-commit wiring (docs/design/git-automation.md §2.4) -


async def test_commit_draft_auto_commits_when_vault_is_a_git_repo(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot, vault_dir: Path
) -> None:
    init_repo(vault_dir)
    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t8", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )
    await research_jobs_repo.record_draft(
        conn, job_id, draft_title="Git Backed Topic", draft_body="some committed content",
        draft_source_urls=["https://a.example/"],
    )

    result = await commit_draft(
        conn, qdrant_client, vault_root, job_id,
        dry_run=False, target_path=None, committed_by="test",
        git_auto_commit_enabled=True,
    )

    assert result.note_id is not None
    log = await get_log(vault_root)
    assert len(log) == 1
    assert log[0].subject == "research_commit: research/git-backed-topic.md"


async def test_commit_draft_succeeds_when_auto_commit_disabled_even_in_a_git_repo(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, vault_root: VaultRoot, vault_dir: Path
) -> None:
    init_repo(vault_dir)
    job_id = await research_jobs_repo.insert(
        conn, huey_task_id="t9", job_type="research_start", created_at="2026-09-10T00:00:00+00:00"
    )
    await research_jobs_repo.record_draft(
        conn, job_id, draft_title="No Commit Topic", draft_body="uncommitted content",
        draft_source_urls=[],
    )

    result = await commit_draft(
        conn, qdrant_client, vault_root, job_id,
        dry_run=False, target_path=None, committed_by="test",
        git_auto_commit_enabled=False,
    )

    assert result.note_id is not None
    note = await notes_repo.get_by_id(conn, result.note_id)
    assert note is not None
    assert note.path == "research/no-commit-topic.md"
    log = await get_log(vault_root)
    assert log == []
