-- Draft content storage for research_start/research_commit (Phase 7).
-- Per docs/design/research-ingestion.md §3: EVENT_MODEL.md already specifies
-- a `draft_handle` carried on `research.job_completed`, but no column ever
-- existed to hold the draft itself between job completion and commit.
-- `draft_handle` is simply `str(research_jobs.id)` -- no new identifier.

ALTER TABLE research_jobs ADD COLUMN draft_title TEXT;
ALTER TABLE research_jobs ADD COLUMN draft_body TEXT;
ALTER TABLE research_jobs ADD COLUMN draft_source_urls TEXT;  -- JSON array of strings
