ALTER TABLE idempotency_records ADD COLUMN status VARCHAR(16) DEFAULT 'completed' NOT NULL;
ALTER TABLE idempotency_records ADD COLUMN expires_at VARCHAR(64) DEFAULT '' NOT NULL;
ALTER TABLE idempotency_records ADD COLUMN result_json TEXT DEFAULT '{}' NOT NULL;
