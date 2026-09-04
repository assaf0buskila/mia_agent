-- Website conversation state, so a deploy mid conversation does not wipe what Mia
-- learned. SiteSession lives in a process-local dict: when the task is replaced the
-- captured phone number, the "Assaf was already told" flag and the "they said no"
-- flag all vanish, so Mia re-asks, double-pings and resumes selling.
-- Additive and portable. Written inside the request transaction, never a second
-- connection.
-- A comment line must never contain a statement separator, because the runner
-- splits this file on it.

CREATE TABLE IF NOT EXISTS website_session_state (
    session_id VARCHAR(64) PRIMARY KEY,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at VARCHAR(32) NOT NULL DEFAULT ''
)
