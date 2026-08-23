ALTER TABLE voice_transcripts ADD COLUMN confidence VARCHAR(16) DEFAULT '' NOT NULL;
ALTER TABLE voice_transcripts ADD COLUMN cost_usd INTEGER DEFAULT 0 NOT NULL;
ALTER TABLE voice_transcripts ADD COLUMN retention_status VARCHAR(16) DEFAULT '' NOT NULL;
