ALTER TABLE owner_briefs ADD COLUMN meetings_booked INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE owner_briefs ADD COLUMN cancellation_requests INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE owner_weeklies ADD COLUMN meetings_booked INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE owner_weeklies ADD COLUMN cancellation_requests INTEGER DEFAULT 0 NOT NULL;
