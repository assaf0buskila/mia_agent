-- V2-42 cache of the last validated snapshot. Never a source of truth or a write input.
-- One row per owner and workspace, overwritten in place. Emptying the table is always safe.
-- Owned by the role the migrations and the application already use. No other role gets access.
CREATE TABLE IF NOT EXISTS owner_os_snapshot_cache (
    owner_id VARCHAR(32) NOT NULL,
    workspace_id VARCHAR(128) NOT NULL,
    workbook_id VARCHAR(128) NOT NULL,
    mode VARCHAR(8) NOT NULL,
    api_version VARCHAR(32) NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    binding_hash VARCHAR(64) NOT NULL,
    snapshot_json TEXT NOT NULL,
    snapshot_sha256 VARCHAR(64) NOT NULL,
    byte_length INTEGER NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    version BIGINT NOT NULL,
    PRIMARY KEY (owner_id, workspace_id),
    CONSTRAINT ck_owner_os_snapshot_cache_mode CHECK (mode IN ('TEST', 'LIVE')),
    CONSTRAINT ck_owner_os_snapshot_cache_bytes CHECK (byte_length BETWEEN 2 AND 261120),
    CONSTRAINT ck_owner_os_snapshot_cache_version CHECK (version >= 1)
);
