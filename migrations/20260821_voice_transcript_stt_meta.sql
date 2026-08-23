ALTER TABLE voice_transcripts ADD COLUMN stt_provider VARCHAR(32) DEFAULT '' NOT NULL;
ALTER TABLE voice_transcripts ADD COLUMN stt_model VARCHAR(64) DEFAULT '' NOT NULL;
ALTER TABLE voice_transcripts ADD COLUMN language VARCHAR(16) DEFAULT '' NOT NULL;
ALTER TABLE voice_transcripts ADD COLUMN duration_ms INTEGER DEFAULT 0 NOT NULL;
