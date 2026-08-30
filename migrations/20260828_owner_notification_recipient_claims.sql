-- Per-recipient notification delivery ledger.
--
-- A fan-out can partially succeed.  This additive table keeps accepted and ambiguous
-- recipients claimed while a known rejected recipient is released for a later retry.
-- It is portable SQLite/PostgreSQL natural-key DDL and deliberately has no backfill:
-- legacy lead-wide claims cannot truthfully identify which recipient accepted a send.

CREATE TABLE IF NOT EXISTS owner_notification_recipient_claims (
    kind VARCHAR(32) NOT NULL,
    lead_id VARCHAR(40) NOT NULL,
    notification_key VARCHAR(255) NOT NULL DEFAULT '',
    recipient_id VARCHAR(32) NOT NULL,
    claimed_at VARCHAR(32) DEFAULT '' NOT NULL,
    PRIMARY KEY (kind, lead_id, notification_key, recipient_id)
);

CREATE INDEX IF NOT EXISTS ix_owner_notification_recipient_claims_lead_id
    ON owner_notification_recipient_claims (lead_id);
