-- Adjustment M: persist-only approval object fields (no execute wiring).
-- SQLite tests use init_db() from models — this file is for existing Postgres DBs.

ALTER TABLE approvals ADD COLUMN approval_id VARCHAR(32) DEFAULT '' NOT NULL;
ALTER TABLE approvals ADD COLUMN business_id VARCHAR(32) DEFAULT '' NOT NULL;
ALTER TABLE approvals ADD COLUMN actor_id VARCHAR(32) DEFAULT '' NOT NULL;
ALTER TABLE approvals ADD COLUMN proposed_parameters VARCHAR(255) DEFAULT '' NOT NULL;
ALTER TABLE approvals ADD COLUMN approved_at VARCHAR(40) DEFAULT '' NOT NULL;
ALTER TABLE approvals ADD COLUMN executed_at VARCHAR(40) DEFAULT '' NOT NULL;
ALTER TABLE approvals ADD COLUMN execution_operation_id VARCHAR(64) DEFAULT '' NOT NULL;
ALTER TABLE approvals ADD COLUMN result VARCHAR(255) DEFAULT '' NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_approval_id
    ON approvals (approval_id)
    WHERE approval_id != '';
