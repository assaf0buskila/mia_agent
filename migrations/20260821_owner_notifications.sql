CREATE TABLE IF NOT EXISTS owner_notifications (
    id INTEGER PRIMARY KEY,
    kind VARCHAR(32) NOT NULL,
    lead_id VARCHAR(32) NOT NULL,
    scheduled_at VARCHAR(32) NOT NULL,
    seen_at VARCHAR(32) DEFAULT '' NOT NULL,
    UNIQUE (kind, lead_id)
);
