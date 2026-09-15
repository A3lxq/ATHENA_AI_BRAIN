"""Consolidated adversarial regression suite (docs/design/production-
hardening.md §2.3).

Every attack pattern below was previously only claimed as defended-against
in a design doc / `docs/SECURITY_MODEL.md`, and covered (if at all) by
narrow unit tests scattered across the suite. This module actually
*executes* each attack pattern against the real, production tool
functions/modules -- never mocking the defense under test -- so a future
regression in any of these five defenses is caught by one obviously-named
file, matching this project's "real filesystem/subprocess/scanner, never
mocked" testing philosophy (see e.g. `tests/safety/test_paths.py`,
`tests/research/test_fetch.py`, `tests/security/test_secrets.py`).

Five attack classes, one test class each:

1. `TestPathTraversalAndSymlinkEscape`   -- ../.. and symlink escapes
                                             against note_read/note_create/
                                             note_update/note_move/
                                             note_delete.
2. `TestFts5QuerySyntaxInjection`         -- FTS5 grammar abuse through the
                                             real `vault_search` tool.
3. `TestSsrfCrossReference`               -- one integration-level check
                                             that `athena.research.fetch`'s
                                             SSRF defense (exhaustively unit-
                                             and-real-tested in
                                             tests/research/test_fetch.py,
                                             not duplicated here) is actually
                                             wired into the higher-level
                                             `athena.research.workflow.
                                             run_research`.
4. `TestSecretShapedContentThroughCommitDraft` -- a real, high-confidence
                                             secret literal through the one
                                             write path
                                             (`athena.research.workflow.
                                             commit_draft`) that secret-scans,
                                             confirming end-to-end redaction.
5. `TestGitleaksAllowlistConfigDrift`     -- a lightweight, gitleaks-binary-
                                             free regression guard on
                                             `.gitleaks.toml`'s allowlist
                                             configuration.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import aiosqlite
import pytest
from mcp.server.mcpserver import AcceptedElicitation, CancelledElicitation
from qdrant_client import QdrantClient

from athena.config import AthenaConfig
from athena.db.repository import chunks as chunks_repo
from athena.db.repository import notes as notes_repo
from athena.db.repository import research_jobs as research_jobs_repo
from athena.indexing.chunking import Chunk
from athena.indexing.embedding import SparseVector
from athena.indexing.qdrant_store import upsert_chunks
from athena.mcp_server import _runtime, mutation_tools, read_tools, write_tools
from athena.research.workflow import commit_draft, run_research
from athena.safety.paths import VaultRoot

_REPO_ROOT = Path(__file__).resolve().parents[2]

# AWS's own published example access key (from AWS's official docs) -- the
# same literal every other secret-fixture test in this repo already uses
# (tests/vault/test_ingest.py, tests/research/test_workflow.py,
# tests/security/test_secrets.py), and the same one `.gitleaks.toml`
# allowlists those three files for. Not a real credential.
_AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"


class _FakeElicitContext:
    """A minimal stand-in for `mcp.server.mcpserver.Context` -- copied from
    `tests/mcp_server/test_mutation_tools.py`'s own fixture of the same
    name/shape (the tool functions here only ever call `ctx.elicit(...)`,
    so a duck-typed object with just that one async method is sufficient).
    """

    def __init__(self, result: object) -> None:
        self._result = result

    async def elicit(self, *, message: str, schema: type[object]) -> object:
        return self._result


@pytest.fixture
def patched_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    vault_dir: Path,
    vault_root: VaultRoot,
    qdrant_client: QdrantClient,
) -> Iterator[AthenaConfig]:
    """Point `_runtime`'s module-level state at this test's fixtures rather
    than the real environment-loaded config -- copied from
    `tests/mcp_server/test_read_tools.py`'s own fixture of the same name.
    """
    config = AthenaConfig(
        vault_root=vault_dir,
        data_dir=tmp_path,
        db_path=tmp_path / "athena.db",
        huey_db_path=tmp_path / "huey.db",
        huey_serializer_secret="test-secret",  # noqa: S106 -- test fixture
        secret_scanner_block_on_high_confidence=False,
        qdrant_url="http://127.0.0.1:6333",
        log_level="INFO",
        git_auto_commit_enabled=True,
        git_auto_push_enabled=False,
        git_push_interval_minutes=60,
        git_command_timeout_s=5.0,
        llm_enabled=False,
        llm_default_provider=None,
        llm_default_model=None,
        openai_api_key=None,
        anthropic_api_key=None,
        google_api_key=None,
        ollama_base_url="http://localhost:11434",
        llm_call_timeout_s=5.0,
        llm_max_calls_per_day=50,
        research_max_dispatches_per_day=50,
        reindex_max_dispatches_per_day=20,
    )
    monkeypatch.setattr(_runtime, "config", config)
    monkeypatch.setattr(_runtime, "get_qdrant_client", lambda: qdrant_client)
    monkeypatch.setattr(_runtime, "require_vault_root", lambda: vault_root)
    yield config


# ===========================================================================
# 1. Path traversal / symlink escape
# ===========================================================================


class TestPathTraversalAndSymlinkEscape:
    """docs/SECURITY_MODEL.md's vault-escape findings, exercised through the
    actual MCP tool functions (not only `athena.safety.paths`' own unit
    tests in `tests/safety/test_paths.py`). Every attempt below must be
    refused with a clear message -- either a plain "cannot ..."/"invalid
    path" return string, or a deliberate `ValueError` the tool raises on
    purpose for `note_create`/`note_move` (per those modules' own
    docstrings) -- and must never touch anything outside the vault
    directory.
    """

    @pytest.mark.parametrize(
        "malicious_path",
        ["../../../etc/passwd", "../../../../../../../root/.ssh/id_rsa", "/etc/passwd"],
    )
    async def test_note_read_refuses_traversal_and_absolute_paths(
        self, patched_runtime: AthenaConfig, malicious_path: str
    ) -> None:
        result = await read_tools.note_read(malicious_path)

        assert "cannot read vault note" in result

    async def test_note_read_refusal_never_leaks_the_outside_file_content(
        self, patched_runtime: AthenaConfig
    ) -> None:
        real_passwd_content = Path("/etc/passwd").read_text(encoding="utf-8")
        real_first_line = real_passwd_content.splitlines()[0]

        result = await read_tools.note_read("/etc/passwd")

        assert "cannot read vault note" in result
        # The refusal message is a path-safety error string, never the
        # target file's actual content.
        assert real_first_line not in result

    @pytest.mark.parametrize(
        "malicious_path",
        ["../evil-dotdot.md", "../../../../evil-deep.md"],
    )
    async def test_note_create_refuses_dotdot_traversal_and_writes_nothing_outside_vault(
        self, patched_runtime: AthenaConfig, tmp_path: Path, malicious_path: str
    ) -> None:
        with pytest.raises(ValueError, match="cannot create vault note"):
            await write_tools.note_create(malicious_path, "malicious content")

        # No file was created anywhere outside the vault directory.
        assert list(tmp_path.glob("**/evil-*.md")) == []

    async def test_note_create_refuses_absolute_path_outside_vault(
        self, patched_runtime: AthenaConfig, tmp_path: Path
    ) -> None:
        outside_target = tmp_path / "outside-abs-target.md"

        with pytest.raises(ValueError, match="cannot create vault note"):
            await write_tools.note_create(str(outside_target), "malicious content")

        assert not outside_target.exists()

    async def test_note_move_refuses_traversal_destination(
        self,
        conn: aiosqlite.Connection,
        patched_runtime: AthenaConfig,
        vault_dir: Path,
        tmp_path: Path,
    ) -> None:
        await write_tools.note_create("real.md", "legitimate content")
        outside_target = tmp_path / "escaped.md"

        with pytest.raises(ValueError, match="cannot move vault note to"):
            await write_tools.note_move("real.md", "../escaped.md")

        assert not outside_target.exists()
        assert (vault_dir / "real.md").read_text(encoding="utf-8") == "legitimate content"

    async def test_note_move_refuses_when_source_path_is_an_unrecorded_traversal_string(
        self, conn: aiosqlite.Connection, patched_runtime: AthenaConfig
    ) -> None:
        # No notes row is ever recorded under a literal "../../etc/passwd"
        # path -- the lookup-by-path guard refuses before resolve_vault_path
        # is even reached, a safe (if early) refusal in its own right.
        result = await write_tools.note_move("../../etc/passwd", "dest.md")

        assert "no such note" in result

    async def test_note_update_patch_mode_refuses_a_tampered_traversal_path(
        self, conn: aiosqlite.Connection, patched_runtime: AthenaConfig, tmp_path: Path
    ) -> None:
        """Simulates a tampered/legacy `notes` row whose `path` column
        itself contains a traversal string -- proving `resolve_vault_path`
        is a real, independent second layer of defense inside
        `note_update`, not merely upstream input validation that a
        corrupted or migrated DB row could bypass."""
        outside_target = tmp_path / "outside-update-target.md"
        outside_target.write_text("original outside content", encoding="utf-8")
        malicious_path = "../outside-update-target.md"
        await notes_repo.insert(
            conn,
            path=malicious_path,
            title="tampered",
            origin="human",
            provider=None,
            folder=None,
            content_hash="h1",
            created_at="2026-09-15T00:00:00+00:00",
        )

        ctx = _FakeElicitContext(CancelledElicitation())  # never consulted in patch mode
        result = await mutation_tools.note_update(
            malicious_path, "malicious appended text", ctx, mode="patch"  # type: ignore[arg-type]
        )

        assert "invalid path" in result
        assert outside_target.read_text(encoding="utf-8") == "original outside content"

    async def test_note_delete_refuses_a_tampered_traversal_path(
        self, conn: aiosqlite.Connection, patched_runtime: AthenaConfig, tmp_path: Path
    ) -> None:
        outside_target = tmp_path / "outside-delete-target.md"
        outside_target.write_text("do not delete me", encoding="utf-8")
        malicious_path = "../outside-delete-target.md"
        await notes_repo.insert(
            conn,
            path=malicious_path,
            title="tampered",
            origin="human",
            provider=None,
            folder=None,
            content_hash="h2",
            created_at="2026-09-15T00:00:00+00:00",
        )

        ctx = _FakeElicitContext(
            AcceptedElicitation(data=mutation_tools.ConfirmDeleteNote(confirm_path=malicious_path))
        )
        result = await mutation_tools.note_delete(malicious_path, ctx)  # type: ignore[arg-type]

        assert "invalid path" in result
        assert outside_target.exists()
        assert outside_target.read_text(encoding="utf-8") == "do not delete me"

    async def test_symlink_escaping_the_vault_is_refused_by_note_read(
        self, vault_dir: Path, patched_runtime: AthenaConfig, tmp_path: Path
    ) -> None:
        secret_target = tmp_path / "outside-secret.md"
        secret_target.write_text("TOP SECRET outside-vault content", encoding="utf-8")
        link_path = vault_dir / "escape-link.md"
        link_path.symlink_to(secret_target)

        result = await read_tools.note_read("escape-link.md")

        assert "cannot read vault note" in result
        assert "TOP SECRET" not in result

    async def test_symlink_escaping_the_vault_is_refused_by_note_update(
        self,
        conn: aiosqlite.Connection,
        vault_dir: Path,
        patched_runtime: AthenaConfig,
        tmp_path: Path,
    ) -> None:
        secret_target = tmp_path / "outside-secret-2.md"
        secret_target.write_text("TOP SECRET outside-vault content 2", encoding="utf-8")
        link_path = vault_dir / "escape-link-2.md"
        link_path.symlink_to(secret_target)
        # A real symlink planted inside the vault will already have a
        # legitimately-recorded notes row in production (e.g. if it were
        # somehow created before this safety boundary existed) -- record
        # one here so note_update reaches resolve_vault_path rather than
        # being refused earlier by the "no such note" lookup.
        await notes_repo.insert(
            conn,
            path="escape-link-2.md",
            title="escape-link-2.md",
            origin="human",
            provider=None,
            folder=None,
            content_hash="h3",
            created_at="2026-09-15T00:00:00+00:00",
        )

        ctx = _FakeElicitContext(CancelledElicitation())
        result = await mutation_tools.note_update(
            "escape-link-2.md", "malicious content", ctx, mode="patch"  # type: ignore[arg-type]
        )

        assert "invalid path" in result
        assert secret_target.read_text(encoding="utf-8") == "TOP SECRET outside-vault content 2"


# ===========================================================================
# 2. FTS5 query-syntax injection
# ===========================================================================


async def _index_one_note(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, *, path: str, text: str
) -> None:
    """Insert one indexed note (a `notes` row, a `chunks` row, and a
    matching Qdrant point) so `vault_search` has a real, small fixture
    vault to search over -- pattern copied from
    `tests/mcp_server/test_read_tools.py`'s own `_index_note` helper."""
    note_id = await notes_repo.insert(
        conn,
        path=path,
        title=path,
        origin="human",
        provider=None,
        folder=None,
        content_hash=f"hash-{path}",
        created_at="2026-09-15T00:00:00+00:00",
    )
    (point_id,) = upsert_chunks(
        qdrant_client,
        note_id=note_id,
        chunks=[Chunk(text=text, chunk_index=0, token_count=len(text.split()))],
        dense_vectors=[[0.1] * 1024],
        sparse_vectors=[SparseVector(indices=[1, 2], values=[0.5, 0.5])],
        payload_fields={
            "note_path": path,
            "tags": [],
            "folder": None,
            "status": "active",
            "origin": "human",
            "provider": None,
            "embedding_model_version": "test@1",
        },
    )
    await chunks_repo.insert(
        conn,
        note_id=note_id,
        chunk_index=0,
        chunk_text=text,
        content_hash=f"chunk-{path}",
        qdrant_point_id=point_id,
        embedding_model_version="test@1",
        token_count=len(text.split()),
        created_at="2026-09-15T00:00:00+00:00",
    )


class TestFts5QuerySyntaxInjection:
    """docs/SECURITY_MODEL.md TB-7. `athena.retrieval.keyword_search.
    sanitize_fts5_query` already has its own unit tests
    (tests/retrieval/test_keyword_search.py) -- this is the higher-level,
    tool-facing regression check that `vault_search` (the function MCP
    clients actually call) never lets raw FTS5 grammar reach SQLite,
    end to end, against a real fixture vault and a real connection."""

    @pytest.mark.parametrize(
        "malicious_query",
        [
            '"unbalanced quote with no closing mark',
            "NOT gardening",
            "-gardening",
            "NEAR(gardening tomatoes, 3)",
            "column:gardening",
        ],
    )
    async def test_vault_search_never_raises_on_fts5_syntax_injection(
        self,
        conn: aiosqlite.Connection,
        qdrant_client: QdrantClient,
        patched_runtime: AthenaConfig,
        malicious_query: str,
    ) -> None:
        await _index_one_note(
            conn, qdrant_client, path="ordinary.md", text="an ordinary note about gardening tips"
        )

        # Must complete without raising -- an unhandled FTS5 syntax error
        # here would be a real regression in sanitize_fts5_query's wiring.
        result_text = await read_tools.vault_search(malicious_query)

        assert isinstance(result_text, str)


# ===========================================================================
# 3. SSRF (cross-reference only -- see tests/research/test_fetch.py for the
#    exhaustive, real, non-mocked SSRF test suite this deliberately does not
#    duplicate: private IPs, loopback, link-local/cloud-metadata addresses,
#    DNS-rebinding-shaped redirect chains, encoded-IP tricks, and more).
# ===========================================================================


class TestSsrfCrossReference:
    """One integration-level check that `athena.research.fetch`'s SSRF
    defense is actually wired into the higher-level
    `athena.research.workflow.run_research` batch function -- not only
    exercised directly against `fetch_url` as `tests/research/test_fetch.py`
    already does exhaustively. Makes a real network call (this project's
    other tests already do the same -- see
    `tests/research/test_fetch.py::test_fetch_url_succeeds_against_a_real_public_url`)."""

    def test_run_research_refuses_cloud_metadata_url_but_completes_batch_with_legitimate_url(
        self,
    ) -> None:
        draft = run_research(
            ["http://169.254.169.254/latest/meta-data/", "https://example.com/"],
            topic="SSRF cross-reference probe",
        )

        assert draft.succeeded_urls == ["https://example.com/"]
        assert draft.failed_urls == ["http://169.254.169.254/latest/meta-data/"]
        assert "Example Domain" in draft.body
        assert "meta-data" not in draft.body


# ===========================================================================
# 4. Secret-shaped content through the write path
# ===========================================================================


class TestSecretShapedContentThroughCommitDraft:
    """`athena.research.workflow.commit_draft` is the one write path in this
    codebase that secret-scans (per its own docstring/implementation) --
    this confirms the already-unit-tested `athena.security.secrets` module
    is actually wired into this real call path end-to-end: redaction in the
    resulting vault file, and `notes.secret_scan_status` recorded as
    'flagged', queried directly against a real DB."""

    async def test_commit_draft_redacts_a_planted_aws_key_and_flags_the_note(
        self,
        conn: aiosqlite.Connection,
        qdrant_client: QdrantClient,
        vault_root: VaultRoot,
        vault_dir: Path,
    ) -> None:
        job_id = await research_jobs_repo.insert(
            conn,
            huey_task_id="adversarial-1",
            job_type="research_start",
            created_at="2026-09-15T00:00:00+00:00",
        )
        await research_jobs_repo.record_draft(
            conn,
            job_id,
            draft_title="Adversarial Secret Probe",
            draft_body=f"leaked credential: aws_access_key_id = {_AWS_EXAMPLE_KEY}",
            draft_source_urls=["https://leaky.example/"],
        )

        result = await commit_draft(
            conn,
            qdrant_client,
            vault_root,
            job_id,
            dry_run=False,
            target_path=None,
            committed_by="test:adversarial",
        )

        assert result.note_id is not None
        assert _AWS_EXAMPLE_KEY not in result.preview_body
        on_disk = (vault_dir / "research" / "adversarial-secret-probe.md").read_text(
            encoding="utf-8"
        )
        assert _AWS_EXAMPLE_KEY not in on_disk

        cursor = await conn.execute(
            "SELECT secret_scan_status FROM notes WHERE id = ?", (result.note_id,)
        )
        row = await cursor.fetchone()
        assert row == ("flagged",)


# ===========================================================================
# 5. gitleaks / .gitleaks.toml allowlist config-drift guard
# ===========================================================================


class TestGitleaksAllowlistConfigDrift:
    """A lightweight config-drift guard for Phase 8's `.gitleaks.toml`
    allowlist, requiring no `gitleaks` binary in the test-running
    environment (there is none here -- confirmed via `which gitleaks`).
    Parses the real repo-root TOML file and asserts it still extends the
    default ruleset and still allowlists the three files with intentionally
    secret-shaped fixtures, per Phase 8's session notes
    (docs/sessions/2026-09-12_phase8-git-automation.md)."""

    @staticmethod
    def _load_config() -> dict[str, object]:
        with (_REPO_ROOT / ".gitleaks.toml").open("rb") as handle:
            return tomllib.load(handle)

    def test_still_extends_the_default_gitleaks_ruleset(self) -> None:
        config = self._load_config()

        extend = config["extend"]
        assert isinstance(extend, dict)
        assert extend["useDefault"] is True

    @pytest.mark.parametrize(
        "fixture_file",
        [
            "tests/vault/test_ingest.py",
            "tests/research/test_workflow.py",
            "tests/security/test_secrets.py",
        ],
    )
    def test_allowlist_still_covers_the_known_secret_fixture_files(
        self, fixture_file: str
    ) -> None:
        config = self._load_config()

        allowlist = config["allowlist"]
        assert isinstance(allowlist, dict)
        paths = allowlist["paths"]
        assert isinstance(paths, list)

        assert any(re.fullmatch(pattern, fixture_file) for pattern in paths), (
            f"{fixture_file!r} is no longer covered by .gitleaks.toml's allowlist paths: {paths!r}"
        )
