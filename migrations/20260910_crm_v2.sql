CREATE TABLE IF NOT EXISTS crm_contacts (
    id VARCHAR(64) PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    fields_json TEXT NOT NULL DEFAULT '{}',
    source_ref VARCHAR(255) NOT NULL DEFAULT '',
    conversation_id VARCHAR(255) NOT NULL DEFAULT '',
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    updated_at VARCHAR(64) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS crm_identities (
    id VARCHAR(64) PRIMARY KEY,
    contact_id VARCHAR(64) NOT NULL REFERENCES crm_contacts(id),
    kind VARCHAR(16) NOT NULL,
    normalized_value VARCHAR(320) NOT NULL,
    source_ref VARCHAR(255) NOT NULL DEFAULT '',
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    CONSTRAINT uq_crm_identity_value UNIQUE (kind, normalized_value)
);

CREATE INDEX IF NOT EXISTS ix_crm_identity_contact ON crm_identities(contact_id);

CREATE TABLE IF NOT EXISTS crm_contact_conversations (
    id VARCHAR(64) PRIMARY KEY,
    contact_id VARCHAR(64) NOT NULL REFERENCES crm_contacts(id),
    conversation_id VARCHAR(255) NOT NULL,
    captured_fields_json TEXT NOT NULL DEFAULT '{}',
    source_ref VARCHAR(255) NOT NULL DEFAULT '',
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    updated_at VARCHAR(64) NOT NULL DEFAULT '',
    CONSTRAINT uq_crm_contact_conversation UNIQUE (conversation_id)
);

CREATE INDEX IF NOT EXISTS ix_crm_contact_conversation_contact
ON crm_contact_conversations(contact_id, created_at);

CREATE TABLE IF NOT EXISTS crm_activities (
    id VARCHAR(64) PRIMARY KEY,
    contact_id VARCHAR(64) NOT NULL REFERENCES crm_contacts(id),
    occurred_at VARCHAR(64) NOT NULL,
    who VARCHAR(160) NOT NULL DEFAULT '',
    channel VARCHAR(32) NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    source_ref VARCHAR(255) NOT NULL,
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    CONSTRAINT uq_crm_activity_source_ref UNIQUE (source_ref)
);

CREATE INDEX IF NOT EXISTS ix_crm_activity_contact ON crm_activities(contact_id, occurred_at);

CREATE TABLE IF NOT EXISTS crm_sync_snapshots (
    contact_id VARCHAR(64) NOT NULL REFERENCES crm_contacts(id),
    field_name VARCHAR(32) NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    contact_revision INTEGER NOT NULL DEFAULT 0,
    sheet_row INTEGER NOT NULL DEFAULT 0,
    synced_at VARCHAR(64) NOT NULL DEFAULT '',
    PRIMARY KEY (contact_id, field_name)
);

CREATE TABLE IF NOT EXISTS crm_issues (
    id VARCHAR(64) PRIMARY KEY,
    contact_id VARCHAR(64) NOT NULL DEFAULT '',
    issue_type VARCHAR(32) NOT NULL,
    field_name VARCHAR(32) NOT NULL DEFAULT '',
    base_value TEXT NOT NULL DEFAULT '',
    database_value TEXT NOT NULL DEFAULT '',
    sheet_value TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    resolution VARCHAR(16) NOT NULL DEFAULT '',
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    resolved_at VARCHAR(64) NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_crm_issue_open ON crm_issues(status, contact_id);

CREATE TABLE IF NOT EXISTS crm_issue_contacts (
    issue_id VARCHAR(64) NOT NULL REFERENCES crm_issues(id) ON DELETE CASCADE,
    contact_id VARCHAR(64) NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
    PRIMARY KEY (issue_id, contact_id)
);

CREATE INDEX IF NOT EXISTS ix_crm_issue_contacts_contact_id
    ON crm_issue_contacts(contact_id);

INSERT INTO crm_issue_contacts (issue_id, contact_id)
SELECT issue.id, issue.contact_id
FROM crm_issues AS issue
JOIN crm_contacts AS contact ON contact.id = issue.contact_id
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS crm_outbox (
    id VARCHAR(64) PRIMARY KEY,
    dedupe_key VARCHAR(255) NOT NULL,
    aggregate_type VARCHAR(24) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    destination VARCHAR(16) NOT NULL,
    payload_json TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at VARCHAR(64) NOT NULL DEFAULT '',
    lease_owner VARCHAR(80) NOT NULL DEFAULT '',
    lease_expires_at VARCHAR(64) NOT NULL DEFAULT '',
    last_attempt_at VARCHAR(64) NOT NULL DEFAULT '',
    confirmed_at VARCHAR(64) NOT NULL DEFAULT '',
    last_error VARCHAR(255) NOT NULL DEFAULT '',
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    CONSTRAINT uq_crm_outbox_dedupe UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS ix_crm_outbox_claim
ON crm_outbox(status, next_attempt_at, lease_expires_at);

CREATE INDEX IF NOT EXISTS ix_crm_outbox_aggregate ON crm_outbox(aggregate_id);

CREATE TABLE IF NOT EXISTS crm_worker_state (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at VARCHAR(64) NOT NULL DEFAULT ''
);
