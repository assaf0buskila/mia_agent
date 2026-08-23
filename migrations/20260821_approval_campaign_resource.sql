-- Campaign-bound R4 approval rows (lead_id nullable, unique on resource+action).
-- SQLite tests use init_db() from models — this file is for existing Postgres DBs.
-- SQLite recreate note: ALTER COLUMN DROP NOT NULL is PG-only. sqlite fresh init_db() applies models.

ALTER TABLE approvals ALTER COLUMN lead_id DROP NOT NULL;

ALTER TABLE approvals DROP CONSTRAINT IF EXISTS uq_lead_approval_action;

CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_resource_action
    ON approvals (resource_type, resource_id, action);
