CREATE TABLE IF NOT EXISTS crm_form_intake_receipts (
    source_id VARCHAR(36) PRIMARY KEY,
    payload_sha256 VARCHAR(64) NOT NULL,
    contact_id VARCHAR(64) NOT NULL REFERENCES crm_contacts(id),
    activity_id VARCHAR(64) NOT NULL REFERENCES crm_activities(id),
    created_at VARCHAR(64) NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_crm_form_intake_receipt_contact
ON crm_form_intake_receipts(contact_id);
