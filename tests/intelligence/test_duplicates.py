from __future__ import annotations

from pathlib import Path

import aiosqlite
from qdrant_client import QdrantClient

from athena.db.repository import chunks as chunks_repo
from athena.db.repository import duplicates as duplicates_repo
from athena.db.repository import notes as notes_repo
from athena.indexing.chunking import Chunk
from athena.indexing.embedding import SparseVector
from athena.indexing.qdrant_store import upsert_chunks
from athena.intelligence.duplicates import scan_for_duplicates
from athena.safety.paths import VaultRoot


def _write(vault_dir: Path, relative: str, text: str) -> Path:
    path = vault_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def _make_note(conn: aiosqlite.Connection, path: str, content_hash: str) -> int:
    return await notes_repo.insert(
        conn, path=path, title=path, origin="human", provider=None,
        folder=None, content_hash=content_hash, created_at="2026-09-04T00:00:00+00:00",
    )


async def _give_note_a_chunk(
    conn: aiosqlite.Connection, qdrant_client: QdrantClient, note_id: int, text: str
) -> None:
    """Attach exactly one indexed chunk (real SQLite `chunks` row + a real
    Qdrant point) to a note, matching the shape `find_similar_by_point_id`
    and `get_first_chunk_id_for_note` expect."""
    (point_id,) = upsert_chunks(
        qdrant_client,
        note_id=note_id,
        chunks=[Chunk(text=text, chunk_index=0, token_count=len(text.split()))],
        dense_vectors=[[0.1] * 1024],
        sparse_vectors=[SparseVector(indices=[1, 2], values=[0.5, 0.5])],
        payload_fields={
            "note_path": f"note-{note_id}.md",
            "tags": [],
            "folder": "",
            "status": "active",
            "origin": "human",
            "provider": None,
            "embedding_model_version": "bge-m3@test",
        },
    )
    await chunks_repo.insert(
        conn,
        note_id=note_id,
        chunk_index=0,
        chunk_text=text,
        content_hash="chunk-hash",
        qdrant_point_id=point_id,
        embedding_model_version="bge-m3@test",
        token_count=len(text.split()),
        created_at="2026-09-04T00:00:00+00:00",
    )


async def test_exact_content_hash_match_is_flagged_with_combined_score_one(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot, qdrant_client: QdrantClient
) -> None:
    _write(vault_dir, "a.md", "identical body text")
    _write(vault_dir, "b.md", "identical body text")
    note_a = await _make_note(conn, "a.md", content_hash="same-hash")
    note_b = await _make_note(conn, "b.md", content_hash="same-hash")

    candidates = await scan_for_duplicates(conn, qdrant_client, vault_root)

    matches = [c for c in candidates if {c.note_a_id, c.note_b_id} == {note_a, note_b}]
    assert len(matches) == 1
    # Byte-identical content also has identical shingles, so the lexical
    # signal legitimately co-fires here too -- 'combined' is the correct
    # label, not a bug. What matters for the exact-hash case is the score.
    assert matches[0].detection_method in ("content_hash", "combined")
    assert matches[0].combined_score == 1.0


async def test_lexical_near_duplicate_is_flagged_unrelated_notes_are_not(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot, qdrant_client: QdrantClient
) -> None:
    shared_text = " ".join(f"word{i}" for i in range(200))
    near_dup_text = shared_text.replace("word5", "wordFIVE")
    unrelated_text = " ".join(f"other{i}" for i in range(200))

    _write(vault_dir, "a.md", shared_text)
    _write(vault_dir, "b.md", near_dup_text)
    _write(vault_dir, "c.md", unrelated_text)
    note_a = await _make_note(conn, "a.md", content_hash="hash-a")
    note_b = await _make_note(conn, "b.md", content_hash="hash-b")
    note_c = await _make_note(conn, "c.md", content_hash="hash-c")

    candidates = await scan_for_duplicates(conn, qdrant_client, vault_root, threshold=0.3)

    pairs = {frozenset({c.note_a_id, c.note_b_id}) for c in candidates}
    assert frozenset({note_a, note_b}) in pairs
    assert frozenset({note_a, note_c}) not in pairs
    assert frozenset({note_b, note_c}) not in pairs


async def test_minhash_signature_persists_and_reloads_across_scans(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot, qdrant_client: QdrantClient
) -> None:
    text = " ".join(f"token{i}" for i in range(50))
    _write(vault_dir, "a.md", text)
    note_a = await _make_note(conn, "a.md", content_hash="hash-a")

    await scan_for_duplicates(conn, qdrant_client, vault_root)

    signatures = await duplicates_repo.list_all_signatures(conn)
    assert len(signatures) == 1
    assert signatures[0].note_id == note_a
    assert signatures[0].num_perm == 128


async def test_note_with_no_chunks_only_skips_the_semantic_signal(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot, qdrant_client: QdrantClient
) -> None:
    shared_text = " ".join(f"alpha{i}" for i in range(200))
    _write(vault_dir, "a.md", shared_text)
    _write(vault_dir, "b.md", shared_text)
    note_a = await _make_note(conn, "a.md", content_hash="hash-a")
    note_b = await _make_note(conn, "b.md", content_hash="hash-b")
    # Neither note has any chunks -- the semantic signal has nothing to work
    # with, but the lexical/exact/metadata signals still run.

    candidates = await scan_for_duplicates(conn, qdrant_client, vault_root)

    matches = [c for c in candidates if {c.note_a_id, c.note_b_id} == {note_a, note_b}]
    assert len(matches) == 1
    assert matches[0].semantic_score is None
    assert matches[0].lexical_score is not None


async def test_semantic_signal_fires_when_notes_have_chunks(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot, qdrant_client: QdrantClient
) -> None:
    _write(vault_dir, "a.md", "note a body")
    _write(vault_dir, "b.md", "note b body, unrelated text entirely")
    note_a = await _make_note(conn, "a.md", content_hash="hash-a")
    note_b = await _make_note(conn, "b.md", content_hash="hash-b")
    # Identical dense vectors -> cosine similarity 1.0, well above the
    # semantic threshold, even though the lexical text differs.
    await _give_note_a_chunk(conn, qdrant_client, note_a, "note a body")
    await _give_note_a_chunk(conn, qdrant_client, note_b, "note b body unrelated text entirely")

    candidates = await scan_for_duplicates(conn, qdrant_client, vault_root)

    matches = [c for c in candidates if {c.note_a_id, c.note_b_id} == {note_a, note_b}]
    assert len(matches) == 1
    assert matches[0].semantic_score is not None
    assert matches[0].semantic_score > 0.9


async def test_qdrant_unreachable_degrades_to_three_signals(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot
) -> None:
    _write(vault_dir, "a.md", "identical body text")
    _write(vault_dir, "b.md", "identical body text")
    note_a = await _make_note(conn, "a.md", content_hash="same-hash")
    note_b = await _make_note(conn, "b.md", content_hash="same-hash")
    unreachable_qdrant = QdrantClient(url="http://127.0.0.1:1")

    candidates = await scan_for_duplicates(conn, unreachable_qdrant, vault_root)

    matches = [c for c in candidates if {c.note_a_id, c.note_b_id} == {note_a, note_b}]
    assert len(matches) == 1
    assert matches[0].semantic_score is None
    assert matches[0].combined_score == 1.0  # exact hash match still fires


async def test_pair_below_threshold_is_not_upserted(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot, qdrant_client: QdrantClient
) -> None:
    _write(vault_dir, "a.md", "completely different text about apples")
    _write(vault_dir, "b.md", "totally unrelated text about oranges and cars")
    await _make_note(conn, "a.md", content_hash="hash-a")
    await _make_note(conn, "b.md", content_hash="hash-b")

    candidates = await scan_for_duplicates(conn, qdrant_client, vault_root, threshold=0.5)

    assert candidates == []


async def test_note_ids_narrows_scan_source_but_not_comparison_pool(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot, qdrant_client: QdrantClient
) -> None:
    _write(vault_dir, "a.md", "identical body text")
    _write(vault_dir, "b.md", "identical body text")
    note_a = await _make_note(conn, "a.md", content_hash="same-hash")
    note_b = await _make_note(conn, "b.md", content_hash="same-hash")

    candidates = await scan_for_duplicates(conn, qdrant_client, vault_root, note_ids=[note_a])

    matches = [c for c in candidates if {c.note_a_id, c.note_b_id} == {note_a, note_b}]
    assert len(matches) == 1


async def test_rescan_does_not_overwrite_an_already_resolved_candidate(
    conn: aiosqlite.Connection, vault_dir: Path, vault_root: VaultRoot, qdrant_client: QdrantClient
) -> None:
    _write(vault_dir, "a.md", "identical body text")
    _write(vault_dir, "b.md", "identical body text")
    note_a = await _make_note(conn, "a.md", content_hash="same-hash")
    note_b = await _make_note(conn, "b.md", content_hash="same-hash")

    first_pass = await scan_for_duplicates(conn, qdrant_client, vault_root)
    match = next(c for c in first_pass if {c.note_a_id, c.note_b_id} == {note_a, note_b})
    await duplicates_repo.update_resolution(
        conn, match.id, status="rejected", resolved_at="t1", resolved_by="user"
    )

    await scan_for_duplicates(conn, qdrant_client, vault_root)

    row = await duplicates_repo.get_by_id(conn, match.id)
    assert row is not None
    assert row.status == "rejected"
