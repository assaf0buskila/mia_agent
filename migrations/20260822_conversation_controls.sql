ALTER TABLE leads ADD COLUMN takeover_state VARCHAR(32) DEFAULT 'mia_active' NOT NULL;

CREATE TABLE IF NOT EXISTS conversation_controls (
    id INTEGER PRIMARY KEY,
    channel VARCHAR(32) NOT NULL,
    external_id VARCHAR(255) NOT NULL,
    automation_scope VARCHAR(32) DEFAULT 'unknown' NOT NULL,
    source VARCHAR(64) DEFAULT '' NOT NULL,
    mia_introduced BOOLEAN DEFAULT 0 NOT NULL,
    lead_id VARCHAR(40) DEFAULT '' NOT NULL,
    UNIQUE (channel, external_id)
);
