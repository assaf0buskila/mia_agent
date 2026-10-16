-- V2-08 session chains. Additive and nullable, so the previous image can still insert.
-- Legacy rows (chain_id NULL) keep authenticating until their old expiry and are never renewed.
-- PostgreSQL ADD CONSTRAINT has no IF NOT EXISTS. A re-run through app.db.migrate is safe
-- because each statement runs in a savepoint and "already exists" is skipped. The live auth
-- database is the separate owner-auth database, which mia-migrate never reaches. It gets this file through
-- ops/assaf-os/provision_auth.py --migrate-session-chains, which guards each constraint.
-- No new table, so no grant change: table-level grants cover the new columns.
ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS chain_id VARCHAR(64) NULL;
ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS chain_started_at TIMESTAMPTZ NULL;
ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS authenticated_at TIMESTAMPTZ NULL;
ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS first_used_at TIMESTAMPTZ NULL;
ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMPTZ NULL;
ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS grace_until TIMESTAMPTZ NULL;
ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS successor_hash VARCHAR(64) NULL;
CREATE INDEX IF NOT EXISTS ix_owner_os_sessions_chain ON owner_os_sessions(chain_id);
ALTER TABLE owner_os_sessions ADD CONSTRAINT ck_owner_os_sessions_chain CHECK (
  (chain_id IS NULL AND chain_started_at IS NULL AND authenticated_at IS NULL)
  OR (chain_id IS NOT NULL AND chain_started_at IS NOT NULL AND authenticated_at IS NOT NULL
      AND chain_started_at <= created_at));
ALTER TABLE owner_os_sessions ADD CONSTRAINT ck_owner_os_sessions_rotation CHECK (
  (rotated_at IS NULL) = (grace_until IS NULL)
  AND (grace_until IS NULL OR grace_until <= rotated_at + INTERVAL '60 seconds')
  AND (successor_hash IS NULL OR rotated_at IS NOT NULL));
