-- Owner-notification send-once ledger, keyed on the CONVERSATION.
--
-- Why: the website finalization claim was keyed on (kind, lead_id) via the
-- owner_notifications UNIQUE constraint. A returning lead's SECOND website
-- conversation therefore claimed as a duplicate and the owner was never notified.
-- owner_notifications stays exactly as it is -- it is the owner's inbox, and one
-- unseen row per lead per kind is the correct key there. The claim moves here.
--
-- Portable on SQLite AND PostgreSQL, deliberately:
--   * no SERIAL / IDENTITY -- the natural key is the primary key, so there is no
--     surrogate id and no sequence to create, and the same DDL text is valid on both.
--   * no JSONB, no NOW(), no gen_random_uuid(), no vector, no partial index.
--   * VARCHAR + explicit NOT NULL DEFAULT only.
--   * CREATE TABLE IF NOT EXISTS, and a backfill guarded by NOT EXISTS, so this is
--     safe to apply to a live database that already has rows and safe to re-apply.
-- Additive only. Nothing is dropped and no existing row is modified.
-- Rollback is DROP TABLE owner_notification_claims -- the old code paths still read
-- and write owner_notifications, which this migration leaves untouched.
--
-- A comment line must never contain a statement separator, because the runner
-- splits this file on it.

CREATE TABLE IF NOT EXISTS owner_notification_claims (
    kind VARCHAR(32) NOT NULL,
    lead_id VARCHAR(40) NOT NULL,
    conversation_id VARCHAR(255) DEFAULT '' NOT NULL,
    claimed_at VARCHAR(32) DEFAULT '' NOT NULL,
    PRIMARY KEY (kind, lead_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS ix_owner_notification_claims_lead_id
    ON owner_notification_claims (lead_id);

CREATE INDEX IF NOT EXISTS ix_owner_notification_claims_conversation_id
    ON owner_notification_claims (conversation_id);

INSERT INTO owner_notification_claims (kind, lead_id, conversation_id, claimed_at)
SELECT owner_notifications.kind,
       owner_notifications.lead_id,
       '',
       owner_notifications.scheduled_at
FROM owner_notifications
WHERE NOT EXISTS (
    SELECT 1
    FROM owner_notification_claims
    WHERE owner_notification_claims.kind = owner_notifications.kind
      AND owner_notification_claims.lead_id = owner_notifications.lead_id
      AND owner_notification_claims.conversation_id = ''
);
