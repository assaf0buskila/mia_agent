-- C12: does the live site outrun the knowledge files Mia reads? Additive only,
-- portable across sqlite/postgres. Written only by the scheduled ingest run
-- (app/brain/site_freshness.py). Blank/"unknown" until the first check has run.

ALTER TABLE brain_knowledge_sources ADD COLUMN site_last_modified VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE brain_knowledge_sources ADD COLUMN source_last_modified VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE brain_knowledge_sources ADD COLUMN site_checked_at VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE brain_knowledge_sources ADD COLUMN site_stale VARCHAR(16) NOT NULL DEFAULT 'unknown';
