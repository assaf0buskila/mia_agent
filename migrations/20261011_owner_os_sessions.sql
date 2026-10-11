-- Opaque credentials only. Raw Telegram proofs, browser tokens, names, and payloads
-- must never be added to these tables.
CREATE TABLE IF NOT EXISTS owner_os_login_challenges (
    challenge_hash VARCHAR(64) PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_owner_os_login_challenges_expiry
ON owner_os_login_challenges(expires_at);

CREATE TABLE IF NOT EXISTS owner_os_login_proofs (
    proof_hash VARCHAR(64) PRIMARY KEY,
    owner_id VARCHAR(32) NOT NULL,
    consumed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_owner_os_login_proofs_owner_numeric
        CHECK (owner_id ~ '^[0-9]+$')
);

CREATE TABLE IF NOT EXISTS owner_os_sessions (
    session_hash VARCHAR(64) PRIMARY KEY,
    owner_id VARCHAR(32) NOT NULL,
    workspace_id VARCHAR(128) NOT NULL,
    login_proof_hash VARCHAR(64) NOT NULL,
    scopes TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_owner_os_sessions_owner_numeric
        CHECK (owner_id ~ '^[0-9]+$')
);

CREATE INDEX IF NOT EXISTS ix_owner_os_sessions_owner_active
ON owner_os_sessions(owner_id, revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS owner_os_revocations (
    owner_id VARCHAR(32) PRIMARY KEY,
    revoked_before TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_owner_os_revocations_owner_numeric
        CHECK (owner_id ~ '^[0-9]+$')
);

CREATE TABLE IF NOT EXISTS owner_os_auth_attempts (
    id BIGSERIAL PRIMARY KEY,
    kind VARCHAR(16) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_owner_os_auth_attempts_kind
        CHECK (kind IN ('challenge', 'login'))
);

CREATE INDEX IF NOT EXISTS ix_owner_os_auth_attempts_window
ON owner_os_auth_attempts(kind, occurred_at);

-- The migration role owns schema changes. The runtime DSN must use a separate,
-- NOINHERIT login role with only the grants listed below. The API verifies that
-- posture on every checked-out connection and fails closed on privilege drift.
REVOKE ALL ON TABLE owner_os_login_challenges FROM PUBLIC;
REVOKE ALL ON TABLE owner_os_login_proofs FROM PUBLIC;
REVOKE ALL ON TABLE owner_os_sessions FROM PUBLIC;
REVOKE ALL ON TABLE owner_os_revocations FROM PUBLIC;
REVOKE ALL ON TABLE owner_os_auth_attempts FROM PUBLIC;
REVOKE ALL ON SEQUENCE owner_os_auth_attempts_id_seq FROM PUBLIC;

-- Operator-applied grants to the dedicated runtime role:
--   challenges: SELECT, INSERT, UPDATE
--   proofs: INSERT
--   sessions: SELECT, INSERT, UPDATE
--   revocations: SELECT, INSERT, UPDATE
--   attempts: SELECT, INSERT and attempts_id_seq: USAGE, SELECT
