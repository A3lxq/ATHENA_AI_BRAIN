"""Token-budgeted, cited context assembly (docs/design/retrieval-pipeline.md
§2.5) from the reranker's final chunk ranking.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from athena.db.repository import chunks as chunks_repo
from athena.db.repository import notes as notes_repo
from athena.db.repository.notes import NoteRow
from athena.retrieval.reranking import RerankedResult


@dataclass(frozen=True)
class ContextResult:
    text: str
    note_ids: list[int]


async def build_context(
    conn: aiosqlite.Connection, reranked: list[RerankedResult], *, max_tokens: int = 4096
) -> ContextResult:
    chunk_rows = await chunks_repo.get_by_ids(conn, [r.chunk_id for r in reranked])
    chunks_by_id = {row.id: row for row in chunk_rows}

    notes_by_id: dict[int, NoteRow | None] = {}
    text_parts: list[str] = []
    note_ids: list[int] = []
    seen_note_ids: set[int] = set()
    total_tokens = 0

    for result in reranked:
        chunk = chunks_by_id.get(result.chunk_id)
        if chunk is None:
            # Deleted between fusion/reranking and context assembly (§5) --
            # skip rather than crash, matching reranking's own tolerance.
            continue

        # token_count is optional (chunks.token_count, DATA_MODEL.md §2.8);
        # a missing value is conservatively estimated at ~4 chars/token
        # rather than dropping the chunk outright.
        token_count = (
            chunk.token_count if chunk.token_count is not None else len(chunk.chunk_text) // 4
        )
        if total_tokens + token_count > max_tokens:
            # Never truncate a chunk mid-text -- an omitted chunk is better
            # than a corrupted one. Try the next candidate instead.
            continue

        if chunk.note_id not in notes_by_id:
            notes_by_id[chunk.note_id] = await notes_repo.get_by_id(conn, chunk.note_id)
        note = notes_by_id[chunk.note_id]
        if note is None:
            continue

        # The citation label is trusted metadata this codebase produced;
        # the chunk text itself is untrusted retrieved vault content and is
        # wrapped in a structurally distinct delimiter so a calling LLM has
        # a per-call, adjacent-to-the-data signal (not just the one-time
        # `vault_search` tool description) that this span is data to read
        # or quote, never an instruction to follow (docs/SECURITY_MODEL.md
        # TB-2 remediation #4).
        text_parts.append(
            f"[Source: {note.path}]\n"
            f"<untrusted_vault_content>\n{chunk.chunk_text}\n</untrusted_vault_content>\n\n"
        )
        total_tokens += token_count
        if chunk.note_id not in seen_note_ids:
            seen_note_ids.add(chunk.note_id)
            note_ids.append(chunk.note_id)

    return ContextResult(text="".join(text_parts), note_ids=note_ids)
