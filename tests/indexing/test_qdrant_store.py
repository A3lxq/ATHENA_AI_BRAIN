from __future__ import annotations

from pathlib import Path

from huey import SqliteHuey
from qdrant_client import QdrantClient, models

from athena.indexing.chunking import Chunk
from athena.indexing.embedding import SparseVector
from athena.indexing.qdrant_store import (
    COLLECTION_ALIAS,
    delete_points_for_note,
    ensure_collection,
    upsert_chunks,
)


def _huey(tmp_path: Path) -> SqliteHuey:
    # SqliteHuey's storage opens a fresh connection per call -- ":memory:"
    # gives each call an empty, table-less database (same finding
    # tests/vault/conftest.py already documents). A real temp file is needed.
    return SqliteHuey(name="athena-test", filename=str(tmp_path / "huey.db"))


def test_ensure_collection_creates_expected_vector_and_sparse_config(tmp_path: Path) -> None:
    client = QdrantClient(":memory:")
    huey = _huey(tmp_path)

    ensure_collection(client, huey)

    info = client.get_collection(COLLECTION_ALIAS)
    dense = info.config.params.vectors
    assert isinstance(dense, dict)
    assert dense["dense"].size == 1024
    assert dense["dense"].distance == models.Distance.COSINE

    sparse = info.config.params.sparse_vectors
    assert sparse is not None
    assert sparse["minicoil"].modifier == models.Modifier.IDF


def test_ensure_collection_is_idempotent(tmp_path: Path) -> None:
    client = QdrantClient(":memory:")
    huey = _huey(tmp_path)

    ensure_collection(client, huey)
    ensure_collection(client, huey)  # must not raise, must not duplicate the alias

    aliases = [a for a in client.get_aliases().aliases if a.alias_name == COLLECTION_ALIAS]
    assert len(aliases) == 1


def test_ensure_collection_alias_resolves_to_a_real_collection(tmp_path: Path) -> None:
    client = QdrantClient(":memory:")
    huey = _huey(tmp_path)

    ensure_collection(client, huey)

    assert client.collection_exists(COLLECTION_ALIAS)


# --- Integration tests: require a real Qdrant server. -----------------------
#
# Docker access was blocked in this development environment through Phase 6
# (design doc §0/§8); resolved 2026-09-10 -- these now run against a real,
# local, pinned-version Qdrant server (docker run qdrant/qdrant:v1.19.1,
# 127.0.0.1-only per ADR-0006) instead of being skipped.
#
# Unlike the ":memory:" tests above (a fresh, isolated client per test),
# these all share one persistent real server/collection across the whole
# suite run -- confirmed empirically the first time these actually ran
# against a live server: leftover points from one test were still present
# when a later test queried the same collection, producing a real,
# reproducible false failure (3 hits instead of 1) that had nothing to do
# with the code under test. `_clear_real_collection` wipes all points (not
# the collection/alias itself) before each such test, giving per-test
# isolation without re-paying `ensure_collection`'s alias-creation cost.


def _clear_real_collection(client: QdrantClient) -> None:
    client.delete(
        collection_name=COLLECTION_ALIAS,
        points_selector=models.FilterSelector(filter=models.Filter()),
    )


def test_ensure_collection_alias_resolves_against_real_server(tmp_path: Path) -> None:
    client = QdrantClient(url="http://127.0.0.1:6333")
    huey = _huey(tmp_path)

    ensure_collection(client, huey)

    # get_collection_aliases() takes a real collection name, not an alias --
    # confirmed empirically: passing the alias name silently returns an
    # empty list rather than erroring. get_aliases() (no args, list
    # everything) + filter is the correct pattern, already used by
    # test_ensure_collection_is_idempotent above.
    aliases = client.get_aliases()
    assert any(a.alias_name == COLLECTION_ALIAS for a in aliases.aliases)


def test_upsert_and_delete_round_trip_against_real_server(tmp_path: Path) -> None:
    client = QdrantClient(url="http://127.0.0.1:6333")
    huey = _huey(tmp_path)
    ensure_collection(client, huey)
    _clear_real_collection(client)

    chunks = [
        Chunk(text="first chunk of note 1", chunk_index=0, token_count=5),
        Chunk(text="second chunk of note 1", chunk_index=1, token_count=5),
    ]
    dense_vectors = [[0.1] * 1024, [0.2] * 1024]
    sparse_vectors = [
        SparseVector(indices=[1, 2], values=[0.5, 0.5]),
        SparseVector(indices=[3, 4], values=[0.5, 0.5]),
    ]
    payload_fields = {
        "note_path": "notes/a.md",
        "tags": ["rag"],
        "folder": "notes",
        "status": "active",
        "origin": "imported",
        "provider": None,
        "embedding_model_version": "bge-m3@abc123",
    }

    point_ids = upsert_chunks(
        client,
        note_id=1,
        chunks=chunks,
        dense_vectors=dense_vectors,
        sparse_vectors=sparse_vectors,
        payload_fields=payload_fields,
    )
    assert len(point_ids) == 2

    fetched = client.retrieve(collection_name=COLLECTION_ALIAS, ids=point_ids, with_payload=True)
    assert {p.payload["chunk_index"] for p in fetched if p.payload is not None} == {0, 1}  # type: ignore[index]

    delete_points_for_note(client, note_id=1)

    remaining = client.retrieve(collection_name=COLLECTION_ALIAS, ids=point_ids)
    assert remaining == []


def test_delete_points_for_note_only_removes_target_note(tmp_path: Path) -> None:
    client = QdrantClient(url="http://127.0.0.1:6333")
    huey = _huey(tmp_path)
    ensure_collection(client, huey)
    _clear_real_collection(client)

    chunk_a = [Chunk(text="note a chunk", chunk_index=0, token_count=3)]
    chunk_b = [Chunk(text="note b chunk", chunk_index=0, token_count=3)]
    dense = [[0.1] * 1024]
    sparse = [SparseVector(indices=[1], values=[1.0])]
    payload_a = {
        "note_path": "a.md",
        "tags": [],
        "folder": "",
        "status": "active",
        "origin": "imported",
        "provider": None,
        "embedding_model_version": "bge-m3@abc123",
    }
    payload_b = {**payload_a, "note_path": "b.md"}

    ids_a = upsert_chunks(
        client, note_id=1, chunks=chunk_a, dense_vectors=dense,
        sparse_vectors=sparse, payload_fields=payload_a,
    )
    ids_b = upsert_chunks(
        client, note_id=2, chunks=chunk_b, dense_vectors=dense,
        sparse_vectors=sparse, payload_fields=payload_b,
    )

    delete_points_for_note(client, note_id=1)

    assert client.retrieve(collection_name=COLLECTION_ALIAS, ids=ids_a) == []
    remaining_b = client.retrieve(collection_name=COLLECTION_ALIAS, ids=ids_b)
    assert len(remaining_b) == 1


def test_sparse_vector_upsert_and_query_round_trip_with_idf_modifier(tmp_path: Path) -> None:
    client = QdrantClient(url="http://127.0.0.1:6333")
    huey = _huey(tmp_path)
    ensure_collection(client, huey)
    _clear_real_collection(client)

    chunks = [Chunk(text="qdrant hybrid search with sparse vectors", chunk_index=0, token_count=6)]
    dense = [[0.1] * 1024]
    sparse = [SparseVector(indices=[10, 20, 30], values=[1.5, 2.0, 0.5])]
    payload_fields = {
        "note_path": "a.md",
        "tags": [],
        "folder": "",
        "status": "active",
        "origin": "imported",
        "provider": None,
        "embedding_model_version": "bge-m3@abc123",
    }

    upsert_chunks(
        client, note_id=1, chunks=chunks, dense_vectors=dense,
        sparse_vectors=sparse, payload_fields=payload_fields,
    )

    results = client.query_points(
        collection_name=COLLECTION_ALIAS,
        using="minicoil",
        query=models.SparseVector(indices=[10, 20], values=[1.0, 1.0]),
        limit=5,
    )
    assert len(results.points) == 1
    assert results.points[0].score > 0
