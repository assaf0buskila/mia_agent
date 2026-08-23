CREATE TABLE IF NOT EXISTS owner_corrections (
    id INTEGER PRIMARY KEY,
    provider VARCHAR(32) NOT NULL,
    provider_event_id VARCHAR(255) NOT NULL,
    scope VARCHAR(32) NOT NULL,
    body TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'logged',
    UNIQUE (provider, provider_event_id)
);
