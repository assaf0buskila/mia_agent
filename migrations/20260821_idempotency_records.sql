CREATE TABLE IF NOT EXISTS idempotency_records (
    id INTEGER PRIMARY KEY,
    scope VARCHAR(32) NOT NULL,
    key VARCHAR(255) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    UNIQUE (scope, key)
);
