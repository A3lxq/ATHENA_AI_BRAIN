from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

from huey import SqliteHuey
from qdrant_client import QdrantClient, models

from athena.indexing.chunking import Chunk
from athena.indexing.embedding import SparseVector
from athena.indexing.qdrant_store import COLLECTION_ALIAS, ensure_collection, upsert_chunks
from athena.retrieval.vector_search import VectorHit, find_similar_by_point_id, search


def _huey(tmp_path: Path) -> SqliteHuey:
    # SqliteHuey's storage opens a fresh connection per call -- ":memory:"
    # gives each call an empty, table-less database. A real temp file is
    # needed (same finding tests/indexing/test_qdrant_store.py documents).
    return SqliteHuey(name="athena-test", filename=str(tmp_path / "huey.db"))


def _client(tmp_path: Path) -> QdrantClient:
    client = QdrantClient(":memory:")
    ensure_collection(client, _huey(tmp_path))
    return client


_PAYLOAD_FIELDS = {
    "note_path": "CLAUDE/note.md",
    "tags": ["rag", "qdrant"],
    "folder": "CLAUDE",
    "status": "active",
    "origin": "ai_generated",
    "provider": "anthropic",
    "embedding_model_version": "bge-m3@abc123",
}


def _upsert_one_chunk(client: QdrantClient, *, note_id: int, text: str) -> str:
    (point_id,) = upsert_chunks(
        client,
        note_id=note_id,
        chunks=[Chunk(text=text, chunk_index=0, token_count=len(text.split()))],
        dense_vectors=[[0.1] * 1024],
        sparse_vectors=[SparseVector(indices=[1, 2], values=[0.5, 0.5])],
        payload_fields=_PAYLOAD_FIELDS,
    )
    return point_id


def test_search_returns_hits_with_note_id_and_rank(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _upsert_one_chunk(client, note_id=1, text="hybrid search over dense and sparse vectors")

    hits = search(client, "hybrid search", limit=10)

    assert len(hits) == 1
    assert isinstance(hits[0], VectorHit)
    assert hits[0].note_id == 1
    assert hits[0].rank == 1
    assert isinstance(hits[0].qdrant_point_id, str)


def test_filter_is_set_on_every_prefetch_not_only_query_filter(tmp_path: Path) -> None:
    """Regression guard for the empirically-found bug (design doc §0): an
    outer-only query_filter was silently ignored in embedded mode. Every
    Prefetch must carry the same filter.
    """
    client = _client(tmp_path)
    _upsert_one_chunk(client, note_id=1, text="a chunk about qdrant filters")

    with patch.object(client, "query_points", wraps=client.query_points) as spy:
        search(client, "qdrant filters", status="active", folder="CLAUDE", limit=5)

    assert spy.call_count == 1
    _, kwargs = spy.call_args
    prefetches = kwargs["prefetch"]
    assert len(prefetches) == 2
    for prefetch in prefetches:
        assert prefetch.filter is not None
        assert isinstance(prefetch.filter, models.Filter)
        assert prefetch.filter.must is not None
        keys = {condition.key for condition in prefetch.filter.must}
        assert keys == {"status", "folder"}
    # query_filter is also set, defense-in-depth, but must not be the only place.
    assert kwargs["query_filter"] is not None
    assert kwargs["query_filter"] == prefetches[0].filter


def test_no_filters_means_no_constraint(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _upsert_one_chunk(client, note_id=1, text="unfiltered chunk text")

    with patch.object(client, "query_points", wraps=client.query_points) as spy:
        search(client, "unfiltered chunk text")

    _, kwargs = spy.call_args
    for prefetch in kwargs["prefetch"]:
        assert prefetch.filter is None
    assert kwargs["query_filter"] is None


def test_single_tag_uses_match_value_multiple_tags_use_match_any(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _upsert_one_chunk(client, note_id=1, text="tag filter chunk")

    with patch.object(client, "query_points", wraps=client.query_points) as spy:
        search(client, "tag filter chunk", tags=["rag"])
    _, kwargs = spy.call_args
    condition = kwargs["prefetch"][0].filter.must[0]
    assert isinstance(condition.match, models.MatchValue)
    assert condition.match.value == "rag"

    with patch.object(client, "query_points", wraps=client.query_points) as spy:
        search(client, "tag filter chunk", tags=["rag", "qdrant"])
    _, kwargs = spy.call_args
    condition = kwargs["prefetch"][0].filter.must[0]
    assert isinstance(condition.match, models.MatchAny)
    assert condition.match.any == ["rag", "qdrant"]


def test_chunk_id_is_none_when_payload_lacks_it(tmp_path: Path) -> None:
    """upsert_chunks deliberately never writes chunk_id into the payload
    (see its docstring) -- simulating exactly that real, current shape.
    """
    client = _client(tmp_path)
    _upsert_one_chunk(client, note_id=7, text="a pre-Phase-4-shaped point")

    hits = search(client, "pre-Phase-4-shaped point")

    assert len(hits) == 1
    assert hits[0].chunk_id is None
    assert hits[0].note_id == 7


def test_chunk_id_is_populated_when_present_in_payload(tmp_path: Path) -> None:
    client = _client(tmp_path)
    point_id = str(uuid.uuid4())
    client.upsert(
        collection_name=COLLECTION_ALIAS,
        points=[
            models.PointStruct(
                id=point_id,
                vector={
                    "dense": [0.1] * 1024,
                    "minicoil": models.SparseVector(indices=[1, 2], values=[0.5, 0.5]),
                },
                payload={**_PAYLOAD_FIELDS, "note_id": 42, "chunk_id": 4821, "chunk_index": 0},
            )
        ],
    )

    hits = search(client, "a chunk with a known chunk_id")

    assert len(hits) == 1
    assert hits[0].chunk_id == 4821
    assert hits[0].note_id == 42
    assert hits[0].qdrant_point_id == point_id


def test_find_similar_by_point_id_excludes_the_queried_point_itself(tmp_path: Path) -> None:
    client = _client(tmp_path)
    queried_point_id = _upsert_one_chunk(client, note_id=1, text="the original note")
    _upsert_one_chunk(client, note_id=2, text="a second, different note")

    hits = find_similar_by_point_id(client, queried_point_id, limit=10)

    assert queried_point_id not in {hit.qdrant_point_id for hit in hits}
    assert {hit.note_id for hit in hits} == {2}


def test_find_similar_by_point_id_populates_score(tmp_path: Path) -> None:
    client = _client(tmp_path)
    queried_point_id = _upsert_one_chunk(client, note_id=1, text="the original note")
    _upsert_one_chunk(client, note_id=2, text="a second, different note")

    hits = find_similar_by_point_id(client, queried_point_id, limit=10)

    assert len(hits) == 1
    assert hits[0].score is not None


def test_find_similar_by_point_id_respects_score_threshold(tmp_path: Path) -> None:
    client = _client(tmp_path)
    queried_point_id = _upsert_one_chunk(client, note_id=1, text="the original note")
    _upsert_one_chunk(client, note_id=2, text="a second, different note")

    hits = find_similar_by_point_id(client, queried_point_id, limit=10, score_threshold=2.0)

    assert hits == []


# --- Integration tests: require a real Qdrant server. -----------------------
#
# Docker access was blocked in this development environment through Phase 6
# (design doc §0/§8); resolved 2026-09-10 (see docs/sessions/ for that
# session's record) -- these now run against a real, local, pinned-version
# Qdrant server (docker run qdrant/qdrant:v1.19.1, 127.0.0.1-only per
# ADR-0006) instead of being skipped.
#
# Unlike the ":memory:" tests above (a fresh, isolated client per test), this
# shares one persistent real server/collection with tests/indexing/
# test_qdrant_store.py's own real-server tests across the whole suite run --
# confirmed empirically the first time this test actually ran against a live
# server: leftover points from an earlier test in the same run were still
# present, producing a real, reproducible false failure (3 hits instead of 1)
# that had nothing to do with the status filter under test. Clearing the
# collection first gives this test the isolation ":memory:" always gave it
# for free.


def test_status_filter_excludes_archived_points_against_real_server(tmp_path: Path) -> None:
    client = QdrantClient(url="http://127.0.0.1:6333")
    ensure_collection(client, _huey(tmp_path))
    client.delete(
        collection_name=COLLECTION_ALIAS,
        points_selector=models.FilterSelector(filter=models.Filter()),
    )

    upsert_chunks(
        client,
        note_id=1,
        chunks=[Chunk(text="an active note about retrieval", chunk_index=0, token_count=5)],
        dense_vectors=[[0.1] * 1024],
        sparse_vectors=[SparseVector(indices=[1, 2], values=[0.5, 0.5])],
        payload_fields={**_PAYLOAD_FIELDS, "status": "active"},
    )
    upsert_chunks(
        client,
        note_id=2,
        chunks=[Chunk(text="an archived note about retrieval", chunk_index=0, token_count=5)],
        dense_vectors=[[0.1] * 1024],
        sparse_vectors=[SparseVector(indices=[1, 2], values=[0.5, 0.5])],
        payload_fields={**_PAYLOAD_FIELDS, "status": "archived"},
    )

    hits = search(client, "note about retrieval", status="active", limit=10)

    assert len(hits) == 1
    assert hits[0].note_id == 1
