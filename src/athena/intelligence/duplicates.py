"""Duplicate detection (docs/design/knowledge-intelligence.md §2.1).

Computes four independent signals over active notes -- exact content-hash
matches, lexical near-duplicates (MinHash-LSH), semantic near-duplicates
(cosine similarity over the first-chunk Qdrant point), and a metadata
(filename/title) similarity that only ever annotates a pair the other three
signals already surfaced -- and upserts `duplicate_candidates` rows for any
pair whose combined score clears a configurable threshold.

Per §0's `datasketch` 2.0.0 research finding, every `MinHash(...)`
construction in this module pins `scheme="affine32"` explicitly rather than
relying on the installed version's default, since a persisted signature
built under a different scheme is not comparable to one built under this
one.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from datasketch import LeanMinHash, MinHash, MinHashLSH
from qdrant_client import QdrantClient

from athena.db.repository import chunks as chunks_repo
from athena.db.repository import duplicates as duplicates_repo
from athena.db.repository import notes as notes_repo
from athena.db.repository.duplicates import DuplicateCandidateRow
from athena.db.repository.notes import NoteRow
from athena.retrieval.vector_search import find_similar_by_point_id
from athena.safety.paths import PathMode, VaultPathError, VaultRoot, resolve_vault_path

__all__ = ["DuplicateCandidate", "scan_for_duplicates"]

logger = logging.getLogger(__name__)

# `DuplicateCandidateRow` already carries every field the design doc's own
# `DuplicateCandidate` interface (§3) asks for, plus the resolution/audit
# columns callers of a scan result don't need but other callers (the merge
# engine, review CLI) do -- aliasing rather than duplicating the dataclass
# keeps one row shape for the whole `duplicate_candidates` lifecycle.
DuplicateCandidate = DuplicateCandidateRow

_NUM_PERM = 128
# The MinHashLSH index's own similarity threshold (docs/design/
# knowledge-intelligence.md §2.1) -- distinct from `scan_for_duplicates`'s
# `threshold` parameter, which gates whether a *combined* score is strong
# enough to upsert a candidate at all. They default to the same value today
# but are conceptually independent knobs.
_LSH_THRESHOLD = 0.5
_SEMANTIC_SCORE_THRESHOLD = 0.85
_SHINGLE_SIZE = 3


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _pair_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _shingles(text: str) -> list[str]:
    """Word 3-gram shingles of `text` -- the representation fed to
    `MinHash.update()` (design doc's "shingled (word-3-gram) representation
    of each note's plain text" language, §2.1)."""
    words = text.split()
    if len(words) < _SHINGLE_SIZE:
        return [" ".join(words)] if words else []
    return [
        " ".join(words[i : i + _SHINGLE_SIZE]) for i in range(len(words) - _SHINGLE_SIZE + 1)
    ]


def _build_minhash(text: str) -> MinHash:
    minhash = MinHash(num_perm=_NUM_PERM, scheme="affine32")
    for shingle in _shingles(text):
        minhash.update(shingle.encode("utf-8"))
    return minhash


def _serialize_minhash(minhash: MinHash) -> bytes:
    lean = LeanMinHash(minhash)
    buf = bytearray(lean.bytesize())
    lean.serialize(buf)
    return bytes(buf)


def _normalized_metadata_key(note: NoteRow) -> str:
    """Lowercased, extension-stripped filename plus lowercased title, per
    §2.1's "normalized (lowercased, extension-stripped) filename/title
    pairs" -- combined into one string per note so the two notes' combined
    keys can be compared with a single `SequenceMatcher` ratio."""
    stem = Path(note.path).stem.lower()
    return f"{stem} {note.title.lower()}"


@dataclass
class _PairSignals:
    content_hash: bool = False
    lexical_score: float | None = None
    semantic_score: float | None = None
    metadata_match_score: float | None = None
    methods: set[str] = field(default_factory=set)


def _record_exact_matches(
    all_notes: list[NoteRow],
    scanned_ids: set[int],
    pairs: dict[tuple[int, int], _PairSignals],
) -> None:
    """Signal 1: `GROUP BY content_hash HAVING COUNT(*) > 1` over the full
    active-note comparison pool -- cheapest signal, checked first. A pair is
    only recorded if at least one of its two notes is actually being
    scanned this run (`note_ids` narrows which notes are scanned *from*,
    not the comparison pool itself, per this module's task brief)."""
    groups: dict[str, list[int]] = {}
    for note in all_notes:
        groups.setdefault(note.content_hash, []).append(note.id)

    for group in groups.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a not in scanned_ids and b not in scanned_ids:
                    continue
                key = _pair_key(a, b)
                signals = pairs.setdefault(key, _PairSignals())
                signals.content_hash = True
                signals.methods.add("content_hash")


async def _record_lexical_matches(
    conn: aiosqlite.Connection,
    scanned_notes: list[NoteRow],
    notes_by_id: dict[int, NoteRow],
    vault_root: VaultRoot,
    pairs: dict[tuple[int, int], _PairSignals],
) -> None:
    """Signal 2: build + persist a MinHash signature for each scanned note,
    rebuild `MinHashLSH` from *every* persisted signature (not just this
    run's), and query each scanned note's signature against the full index.

    `note.path` is DB-sourced, not caller-controlled, but must still be
    re-resolved through `resolve_vault_path(..., PathMode.EXISTING)` before
    being read -- the same second-layer defense every sibling call site
    that touches a `notes.path` value uses (`note_update`/`note_delete`/
    `note_move` in `mutation_tools.py`/`write_tools.py`,
    `athena.intelligence.merge.merge_notes`), so a tampered or legacy DB row
    containing a traversal path can never be read straight off disk here.
    """
    now = _now()
    for note in scanned_notes:
        try:
            safe_path = resolve_vault_path(note.path, vault_root, PathMode.EXISTING)
            text = safe_path.path.read_text(encoding="utf-8")
        except (VaultPathError, OSError):
            logger.warning(
                "could not read vault file for note_id=%s path=%s -- skipping "
                "lexical duplicate signal for this note",
                note.id,
                note.path,
                exc_info=True,
            )
            continue
        minhash = _build_minhash(text)
        await duplicates_repo.upsert_signature(
            conn,
            note_id=note.id,
            num_perm=_NUM_PERM,
            signature=_serialize_minhash(minhash),
            computed_at=now,
        )

    # Reload from persisted state rather than reusing in-memory MinHash
    # objects -- this is deliberately the persist/reload round trip (design
    # doc §0/§7), and also picks up signatures from notes scanned in a prior
    # run that this run's `note_ids` didn't include.
    all_signatures = await duplicates_repo.list_all_signatures(conn)
    id_to_minhash: dict[int, LeanMinHash] = {}
    for row in all_signatures:
        if row.note_id not in notes_by_id:
            continue  # signature belongs to a note no longer active
        if row.num_perm != _NUM_PERM:
            logger.warning(
                "note_id=%s has a minhash signature with num_perm=%s (expected %s) "
                "-- skipping it for this scan",
                row.note_id,
                row.num_perm,
                _NUM_PERM,
            )
            continue
        id_to_minhash[row.note_id] = LeanMinHash.deserialize(row.signature)

    lsh = MinHashLSH(threshold=_LSH_THRESHOLD, num_perm=_NUM_PERM)
    for note_id, minhash in id_to_minhash.items():
        lsh.insert(note_id, minhash)

    for note in scanned_notes:
        minhash = id_to_minhash.get(note.id)
        if minhash is None:
            continue
        for other_id in lsh.query(minhash):
            if not isinstance(other_id, int) or other_id == note.id:
                continue
            other_minhash = id_to_minhash.get(other_id)
            if other_minhash is None:
                continue
            jaccard = minhash.jaccard(other_minhash)
            key = _pair_key(note.id, other_id)
            signals = pairs.setdefault(key, _PairSignals())
            if signals.lexical_score is None or jaccard > signals.lexical_score:
                signals.lexical_score = jaccard
            signals.methods.add("minhash_lsh")


async def _record_semantic_matches(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    scanned_notes: list[NoteRow],
    notes_by_id: dict[int, NoteRow],
    pairs: dict[tuple[int, int], _PairSignals],
) -> None:
    """Signal 3: cosine similarity over each scanned note's first chunk's
    Qdrant point (§2.1's "first chunk as representative proxy", mirroring
    `fusion.py`'s existing pattern). A note with no chunks yet only skips
    this one signal (logged INFO). The whole leg degrades gracefully (logged
    WARNING, matching `athena.retrieval.search._vector_search_or_degrade`)
    if Qdrant itself is unreachable -- never crashes the scan.
    """
    try:
        for note in scanned_notes:
            chunk_id = await chunks_repo.get_first_chunk_id_for_note(conn, note.id)
            if chunk_id is None:
                logger.info(
                    "note_id=%s has no chunks -- skipping semantic duplicate signal",
                    note.id,
                )
                continue
            chunk_rows = await chunks_repo.get_by_ids(conn, [chunk_id])
            if not chunk_rows:
                continue
            point_id = chunk_rows[0].qdrant_point_id

            hits = find_similar_by_point_id(
                qdrant_client, point_id, score_threshold=_SEMANTIC_SCORE_THRESHOLD
            )
            for hit in hits:
                if hit.note_id == note.id or hit.note_id not in notes_by_id:
                    continue
                if hit.score is None:
                    continue
                key = _pair_key(note.id, hit.note_id)
                signals = pairs.setdefault(key, _PairSignals())
                if signals.semantic_score is None or hit.score > signals.semantic_score:
                    signals.semantic_score = hit.score
                signals.methods.add("cosine_similarity")
    except Exception:
        logger.warning(
            "Qdrant unreachable during duplicate scan -- degrading to "
            "lexical+exact+metadata signals",
            exc_info=True,
        )
        # Discard any pairs/scores this leg already recorded before failing
        # partway through -- the scan either has a complete semantic signal
        # or none at all, never a partial one (design doc §5).
        for signals in pairs.values():
            if "cosine_similarity" in signals.methods:
                signals.methods.discard("cosine_similarity")
                signals.semantic_score = None


def _annotate_metadata_matches(
    notes_by_id: dict[int, NoteRow],
    pairs: dict[tuple[int, int], _PairSignals],
) -> None:
    """Signal 4: annotates pairs the other three signals already found --
    per §2.1, metadata match alone never promotes a pair into the results."""
    for (a, b), signals in pairs.items():
        note_a = notes_by_id.get(a)
        note_b = notes_by_id.get(b)
        if note_a is None or note_b is None:
            continue
        ratio = difflib.SequenceMatcher(
            None, _normalized_metadata_key(note_a), _normalized_metadata_key(note_b)
        ).ratio()
        signals.metadata_match_score = ratio


async def scan_for_duplicates(
    conn: aiosqlite.Connection,
    qdrant_client: QdrantClient,
    vault_root: VaultRoot,
    *,
    note_ids: list[int] | None = None,
    threshold: float = 0.5,
) -> list[DuplicateCandidate]:
    """Scan for duplicate notes and upsert `duplicate_candidates` rows.

    Computes all four signals from docs/design/knowledge-intelligence.md
    §2.1 for the given notes (or every active note if `note_ids` is
    omitted). `note_ids` narrows which notes are scanned *from*; the
    comparison pool is always every active note, since a duplicate of a
    scanned note might not itself be in `note_ids`.

    `vault_root` is a `VaultRoot`, not a plain `Path` -- design §3.3's
    type-level friction control -- so every filesystem read this function
    (transitively, via `_record_lexical_matches`) performs is forced through
    `resolve_vault_path` rather than a raw path join.
    """
    all_notes = await notes_repo.list_active(conn)
    notes_by_id = {note.id: note for note in all_notes}

    if note_ids is None:
        scanned_notes = all_notes
    else:
        wanted = set(note_ids)
        scanned_notes = [note for note in all_notes if note.id in wanted]
    scanned_ids = {note.id for note in scanned_notes}

    pairs: dict[tuple[int, int], _PairSignals] = {}

    _record_exact_matches(all_notes, scanned_ids, pairs)
    await _record_lexical_matches(conn, scanned_notes, notes_by_id, vault_root, pairs)
    await _record_semantic_matches(conn, qdrant_client, scanned_notes, notes_by_id, pairs)
    _annotate_metadata_matches(notes_by_id, pairs)

    detected_at = _now()
    results: list[DuplicateCandidate] = []
    for (note_a_id, note_b_id), signals in pairs.items():
        combined_score = max(signals.lexical_score or 0.0, signals.semantic_score or 0.0)
        if signals.content_hash:
            combined_score = 1.0
        if combined_score < threshold:
            continue

        detection_method = "combined" if len(signals.methods) > 1 else next(iter(signals.methods))

        candidate_id = await duplicates_repo.upsert_candidate(
            conn,
            note_a_id=note_a_id,
            note_b_id=note_b_id,
            detection_method=detection_method,
            lexical_score=signals.lexical_score,
            semantic_score=signals.semantic_score,
            metadata_match_score=signals.metadata_match_score,
            combined_score=combined_score,
            detected_at=detected_at,
        )
        row = await duplicates_repo.get_by_id(conn, candidate_id)
        if row is not None:
            results.append(row)

    return results
