-- Record confirmed Telegram acceptance separately from a retained ambiguous claim.
-- Existing rows are intentionally marked legacy: they still prevent resends but
-- cannot prove an owner received a past notification.

ALTER TABLE owner_notification_recipient_claims
    ADD COLUMN delivery_status VARCHAR(16) NOT NULL DEFAULT 'legacy';
