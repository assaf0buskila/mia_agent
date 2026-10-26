-- V2-38b: OAuth 2.1 authorization requests for the assistant MCP resource.
-- Grants live exclusively in role_policy.py. OAuth state is stored verbatim so the
-- authorization response can return it exactly; codes and credentials are hash-only.
ALTER TABLE assaf_os.os_assistant_credentials
    ADD COLUMN audience text;

CREATE TABLE assaf_os.os_assistant_oauth_requests (
    workspace_id varchar(128) NOT NULL,
    oauth_request_id varchar(35) NOT NULL,
    owner_id varchar(20) NOT NULL,
    client_id text NOT NULL,
    redirect_uri text NOT NULL,
    code_challenge character(43) NOT NULL,
    scope text NOT NULL,
    resource text NOT NULL,
    state text NOT NULL,
    epoch bigint NOT NULL,
    status text NOT NULL,
    chain_binding character(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    deadline timestamptz NOT NULL,
    approved_at timestamptz,
    code_hash character(64),
    code_expires_at timestamptz,
    code_used_at timestamptz,
    credential_id varchar(35),
    client_ip_hmac character(64) NOT NULL,
    CONSTRAINT os_oauth_requests_pkey PRIMARY KEY (workspace_id, oauth_request_id),
    CONSTRAINT os_oauth_requests_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id),
    CONSTRAINT os_oauth_requests_credential_fkey FOREIGN KEY (workspace_id, credential_id)
        REFERENCES assaf_os.os_assistant_credentials(workspace_id, credential_id),
    CONSTRAINT os_oauth_requests_id_check CHECK (oauth_request_id ~ '^OR_[0-9a-f]{32}$'),
    CONSTRAINT os_oauth_requests_owner_id_check CHECK (owner_id ~ '^[0-9]+$'),
    CONSTRAINT os_oauth_requests_client_ip_hmac_check CHECK
        (client_ip_hmac ~ '^[0-9a-f]{64}$'),
    CONSTRAINT os_oauth_requests_client_check CHECK
        (client_id = 'https://chatgpt.com/oauth/client.json'),
    CONSTRAINT os_oauth_requests_redirect_check CHECK
        (redirect_uri = 'https://chatgpt.com/connector_platform_oauth_redirect'),
    CONSTRAINT os_oauth_requests_challenge_check CHECK
        (code_challenge ~ '^[A-Za-z0-9_-]{43}$'),
    CONSTRAINT os_oauth_requests_scope_check CHECK (scope = 'assaf-os:assistant'),
    CONSTRAINT os_oauth_requests_resource_check CHECK (resource <> ''),
    CONSTRAINT os_oauth_requests_state_check CHECK
        (state ~ '^[ -~]+$' AND length(state) BETWEEN 1 AND 512),
    CONSTRAINT os_oauth_requests_epoch_check CHECK (epoch > 0),
    CONSTRAINT os_oauth_requests_status_check CHECK (status IN (
        'pending_owner', 'approval_requested', 'approved', 'code_issued', 'exchanged',
        'rejected', 'expired', 'cancelled'
    )),
    CONSTRAINT os_oauth_requests_chain_binding_check CHECK
        (chain_binding ~ '^[0-9a-f]{64}$'),
    CONSTRAINT os_oauth_requests_code_hash_check CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT os_oauth_requests_deadline_check CHECK
        (deadline = created_at + interval '10 minutes')
);

CREATE UNIQUE INDEX os_assistant_oauth_code_hash_key
    ON assaf_os.os_assistant_oauth_requests (workspace_id, code_hash)
    WHERE code_hash IS NOT NULL;
