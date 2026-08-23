CREATE TABLE IF NOT EXISTS shadow_decisions (
    id INTEGER PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL UNIQUE,
    lead_id VARCHAR(40),
    channel VARCHAR(32) NOT NULL,
    next_action VARCHAR(32) NOT NULL,
    proposed_reply TEXT NOT NULL DEFAULT '',
    policy_version VARCHAR(32) NOT NULL DEFAULT ''
);
