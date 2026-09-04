-- When an AI run happened, so "engine health today" can mean today.
-- ai_runs carried no timestamp at all, so aggregate_ai_runs accepted a day window
-- and ignored it, and the owner brief could only ever report an all time total. It
-- said so honestly, but the number stopped being useful the moment the table had
-- more than a few days in it.
-- Rows written before this migration keep an empty string and are simply outside
-- every day window, which is correct: nobody knows when they ran.
-- Additive, portable, idempotent-safe via the runner's duplicate-column skip.
-- A comment line must never contain a statement separator, because the runner
-- splits this file on it.

ALTER TABLE ai_runs ADD COLUMN occurred_at VARCHAR(32) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS ix_ai_runs_occurred_at ON ai_runs (occurred_at);
