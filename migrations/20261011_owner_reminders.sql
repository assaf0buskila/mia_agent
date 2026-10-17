CREATE TABLE IF NOT EXISTS owner_reminders (
    id VARCHAR(32) PRIMARY KEY,
    owner_id VARCHAR(32) NOT NULL,
    source_event_id VARCHAR(255) NOT NULL,
    text TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Jerusalem',
    contact_id VARCHAR(64) NULL REFERENCES crm_contacts(id),
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1,
    delivery_state VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_owner_reminder_source UNIQUE (owner_id, source_event_id),
    CONSTRAINT ck_owner_reminders_status
        CHECK (status IN ('active', 'completed', 'cancelled')),
    CONSTRAINT ck_owner_reminders_delivery_state
        CHECK (delivery_state IN ('pending', 'dispatching', 'accepted', 'unknown', 'rejected')),
    CONSTRAINT ck_owner_reminders_revision CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS ix_owner_reminders_due
ON owner_reminders(owner_id, status, scheduled_at);
