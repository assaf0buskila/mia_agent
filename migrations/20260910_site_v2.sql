CREATE TABLE IF NOT EXISTS site_v2_sessions (
    session_id VARCHAR(64) PRIMARY KEY,
    credential_hash VARCHAR(64) NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    active_message_id VARCHAR(120) NOT NULL DEFAULT '',
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    updated_at VARCHAR(64) NOT NULL DEFAULT '',
    ended_at VARCHAR(64) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS site_v2_messages (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL REFERENCES site_v2_sessions(session_id),
    client_message_id VARCHAR(120) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'processing',
    response_json TEXT NOT NULL DEFAULT '',
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    updated_at VARCHAR(64) NOT NULL DEFAULT '',
    CONSTRAINT uq_site_v2_session_message UNIQUE (session_id, client_message_id)
);

CREATE INDEX IF NOT EXISTS ix_site_v2_messages_session
ON site_v2_messages(session_id);
