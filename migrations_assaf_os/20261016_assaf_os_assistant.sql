-- V2-14b addendum 3 of 3 (V2_PG_CONTRACTS.md section 4): assistant credentials and audit.
-- Run only by app/db/os_ledger_migrate.py as assaf_os_owner, after
-- 20261016_assaf_os_statements.sql. Access is defined only by role_policy.py
-- (section 18.2); this file defines none.
-- A credential stores only credential_hash(value, purpose="assistant"), never the secret.
-- Audit rows carry codes and counts only (I7) and are kept for 180 days.

CREATE TABLE assaf_os.os_assistant_credentials (
    workspace_id VARCHAR(128) NOT NULL,
    credential_id VARCHAR(35) NOT NULL
        CONSTRAINT os_assistant_credentials_credential_id_check
        CHECK (credential_id ~ '^AC_[0-9a-f]{32}$'),
    owner_id VARCHAR(20) NOT NULL
        CONSTRAINT os_assistant_credentials_owner_id_check CHECK (owner_id ~ '^[0-9]+$'),
    epoch BIGINT NOT NULL
        CONSTRAINT os_assistant_credentials_epoch_check CHECK (epoch > 0),
    label VARCHAR(40) NOT NULL,
    kind VARCHAR(8) NOT NULL
        CONSTRAINT os_assistant_credentials_kind_check CHECK (kind IN ('claude', 'gpt')),
    scopes VARCHAR(64) NOT NULL
        CONSTRAINT os_assistant_credentials_scopes_check
        CHECK (scopes = 'merchant_rules:read own_proposals:read statement:propose'),
    secret_hash CHAR(64) NOT NULL
        CONSTRAINT os_assistant_credentials_secret_hash_check
        CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    use_count BIGINT NOT NULL DEFAULT 0
        CONSTRAINT os_assistant_credentials_use_count_check CHECK (use_count >= 0),
    CONSTRAINT os_assistant_credentials_pkey PRIMARY KEY (workspace_id, credential_id),
    CONSTRAINT os_assistant_credentials_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id),
    CONSTRAINT os_assistant_credentials_secret_hash_key UNIQUE (secret_hash),
    CONSTRAINT os_assistant_credentials_expiry_check CHECK (expires_at > created_at),
    CONSTRAINT os_assistant_credentials_expiry_max_check
        CHECK (expires_at <= created_at + interval '90 days'),
    CONSTRAINT os_assistant_credentials_revoked_check
        CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

-- credential_id is NULL when no credential matched; neither id is a foreign key, so an
-- audit row is never refused for naming an unknown credential or proposal.
CREATE TABLE assaf_os.os_assistant_audit (
    workspace_id VARCHAR(128) NOT NULL,
    audit_id UUID NOT NULL,
    credential_id VARCHAR(35)
        CONSTRAINT os_assistant_audit_credential_id_check
        CHECK (credential_id ~ '^AC_[0-9a-f]{32}$'),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation VARCHAR(32) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    ok BOOLEAN NOT NULL,
    line_count INTEGER NOT NULL DEFAULT 0
        CONSTRAINT os_assistant_audit_line_count_check CHECK (line_count >= 0),
    bytes INTEGER NOT NULL DEFAULT 0
        CONSTRAINT os_assistant_audit_bytes_check CHECK (bytes >= 0),
    proposal_id VARCHAR(64),
    client_ip_hmac CHAR(64) NOT NULL
        CONSTRAINT os_assistant_audit_client_ip_hmac_check
        CHECK (client_ip_hmac ~ '^[0-9a-f]{64}$'),
    CONSTRAINT os_assistant_audit_pkey PRIMARY KEY (workspace_id, audit_id),
    CONSTRAINT os_assistant_audit_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id)
);

CREATE INDEX os_assistant_audit_credential_occurred_idx
    ON assaf_os.os_assistant_audit(credential_id, occurred_at);

-- A proposal that names a credential must name an existing credential of its own workspace.
ALTER TABLE assaf_os.os_proposals
    ADD CONSTRAINT os_proposals_credential_fkey FOREIGN KEY (workspace_id, credential_id)
        REFERENCES assaf_os.os_assistant_credentials(workspace_id, credential_id);
