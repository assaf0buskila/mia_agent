ALTER TABLE meetings ADD COLUMN reschedule_slots_json TEXT DEFAULT '[]' NOT NULL;
ALTER TABLE meetings ADD COLUMN rescheduled_at VARCHAR(32) DEFAULT '' NOT NULL;
ALTER TABLE meetings ADD COLUMN cancellation_requested_at VARCHAR(32) DEFAULT '' NOT NULL;
