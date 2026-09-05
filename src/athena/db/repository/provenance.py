"""Repository functions for `provenance`/`provenance_sources`/`provenance_derivations`
(docs/DATA_MODEL.md §2.4) plus the lineage read-side query (docs/design/
knowledge-intelligence.md §2.6).
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite


async def insert_activity(
    conn: aiosqlite.Connection,
    *,
    note_id: int,
    activity_type: str,
    provider: str | None,
    model: str | None,
    human_edited: bool,
    occurred_at: str,
    recorded_at: str,
    transformation_notes: str | None = None,
    research_job_id: int | None = None,
    supersedes_note_id: int | None = None,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO provenance (note_id, activity_type, provider, model, "
        "human_edited, research_job_id, supersedes_note_id, transformation_notes, "
        "occurred_at, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            note_id,
            activity_type,
            provider,
            model,
            1 if human_edited else 0,
            research_job_id,
            supersedes_note_id,
            transformation_notes,
            occurred_at,
            recorded_at,
        ),
    )
    await conn.commit()
    provenance_id = cursor.lastrowid
    if provenance_id is None:
        raise RuntimeError("INSERT INTO provenance did not yield a rowid")
    return provenance_id


async def insert_source(
    conn: aiosqlite.Connection,
    *,
    provenance_id: int,
    url: str,
    title: str | None = None,
    accessed_at: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO provenance_sources (provenance_id, url, title, accessed_at) "
        "VALUES (?, ?, ?, ?)",
        (provenance_id, url, title, accessed_at),
    )
    await conn.commit()


async def insert_derivation(
    conn: aiosqlite.Connection, *, provenance_id: int, source_note_id: int
) -> None:
    """Record a PROV `wasDerivedFrom` relation for a multi-source activity
    (a merge with more than one absorbed note) -- docs/DATA_MODEL.md §2.4."""
    await conn.execute(
        "INSERT INTO provenance_derivations (provenance_id, source_note_id) VALUES (?, ?)",
        (provenance_id, source_note_id),
    )
    await conn.commit()


@dataclass(frozen=True)
class LineageEdge:
    note_id: int
    provenance_id: int
    activity_type: str
    occurred_at: str


@dataclass(frozen=True)
class LineageGraph:
    note_id: int
    ancestors: list[LineageEdge]
    descendants: list[LineageEdge]


async def get_lineage(conn: aiosqlite.Connection, note_id: int) -> LineageGraph:
    """Walks `provenance`/`provenance_derivations` to answer "what did this
    note come from" (ancestors) and "what did this note become"
    (descendants) -- docs/design/knowledge-intelligence.md §2.6.

    Both directions are answered from the same `provenance` table, just
    filtered differently: an ancestor is `supersedes_note_id` on one of this
    note's own activity rows; a descendant is the `note_id` of some other
    activity row that names *this* note as what it supersedes. No write-side
    change is needed to support this -- `superseded_by_note_id` is left
    unpopulated by this project's merge logic on purpose (see the design
    doc), since this query answers the same question without it.
    """
    ancestor_cursor = await conn.execute(
        "SELECT supersedes_note_id, id, activity_type, occurred_at FROM provenance "
        "WHERE note_id = ? AND supersedes_note_id IS NOT NULL",
        (note_id,),
    )
    ancestors = [
        LineageEdge(note_id=row[0], provenance_id=row[1], activity_type=row[2], occurred_at=row[3])
        for row in await ancestor_cursor.fetchall()
    ]

    derivation_cursor = await conn.execute(
        "SELECT pd.source_note_id, p.id, p.activity_type, p.occurred_at "
        "FROM provenance_derivations pd JOIN provenance p ON p.id = pd.provenance_id "
        "WHERE p.note_id = ?",
        (note_id,),
    )
    ancestors.extend(
        LineageEdge(note_id=row[0], provenance_id=row[1], activity_type=row[2], occurred_at=row[3])
        for row in await derivation_cursor.fetchall()
    )

    descendant_cursor = await conn.execute(
        "SELECT note_id, id, activity_type, occurred_at FROM provenance "
        "WHERE supersedes_note_id = ?",
        (note_id,),
    )
    descendants = [
        LineageEdge(note_id=row[0], provenance_id=row[1], activity_type=row[2], occurred_at=row[3])
        for row in await descendant_cursor.fetchall()
    ]

    return LineageGraph(note_id=note_id, ancestors=ancestors, descendants=descendants)


@dataclass(frozen=True)
class ProvenanceRow:
    id: int
    note_id: int
    activity_type: str
    provider: str | None
    model: str | None
    human_edited: bool
    supersedes_note_id: int | None
    occurred_at: str


async def get_activities_for_note(
    conn: aiosqlite.Connection, note_id: int
) -> list[ProvenanceRow]:
    """A note's own PROV activity history (docs/design/mcp-server.md §2.1's
    `note_provenance` tool) -- what produced it, human-edited, and any
    supersession this specific activity recorded. Distinct from
    `get_lineage`, which answers the broader supersession-graph question
    across potentially many activities/notes.
    """
    cursor = await conn.execute(
        "SELECT id, note_id, activity_type, provider, model, human_edited, "
        "supersedes_note_id, occurred_at FROM provenance WHERE note_id = ? "
        "ORDER BY occurred_at",
        (note_id,),
    )
    rows = await cursor.fetchall()
    return [
        ProvenanceRow(
            id=row[0],
            note_id=row[1],
            activity_type=row[2],
            provider=row[3],
            model=row[4],
            human_edited=bool(row[5]),
            supersedes_note_id=row[6],
            occurred_at=row[7],
        )
        for row in rows
    ]
