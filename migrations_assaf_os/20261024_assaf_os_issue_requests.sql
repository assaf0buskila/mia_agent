-- E37: immutable request, Telegram approval, same-session one-time collection.
-- Grants live exclusively in role_policy.py. The deadline is derived, never runtime-writable.
CREATE TABLE assaf_os.os_assistant_issue_requests (
    workspace_id text NOT NULL,
    issue_request_id text NOT NULL,
    owner_id text NOT NULL,
    epoch bigint NOT NULL,
    label text NOT NULL,
    kind text NOT NULL,
    expires_in_days integer NOT NULL,
    session_binding text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL,
    approval_deadline timestamptz NOT NULL,
    approved_at timestamptz,
    collect_deadline timestamptz GENERATED ALWAYS AS
        (((approved_at AT TIME ZONE 'UTC') + interval '10 minutes') AT TIME ZONE 'UTC') STORED,
    collected_at timestamptz,
    credential_id varchar(35),
    CONSTRAINT os_issue_requests_pkey PRIMARY KEY (workspace_id, issue_request_id),
    CONSTRAINT os_issue_requests_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id),
    CONSTRAINT os_issue_requests_key_fkey FOREIGN KEY (workspace_id, credential_id)
        REFERENCES assaf_os.os_assistant_credentials(workspace_id, credential_id),
    CONSTRAINT os_issue_requests_kind_check CHECK (kind IN ('claude', 'gpt')),
    CONSTRAINT os_issue_requests_status_check CHECK
        (status IN ('pending_approval', 'approved', 'rejected', 'expired', 'collected')),
    CONSTRAINT os_issue_requests_days_check CHECK (expires_in_days BETWEEN 1 AND 90),
    CONSTRAINT os_issue_requests_epoch_check CHECK (epoch > 0),
    CONSTRAINT os_issue_requests_binding_check CHECK (session_binding ~ '^[0-9a-f]{64}$'),
    CONSTRAINT os_issue_requests_id_check CHECK (issue_request_id ~ '^IR_[0-9a-f]{32}$'),
    CONSTRAINT os_issue_requests_approval_check CHECK
        (approval_deadline = created_at + interval '10 minutes'),
    CONSTRAINT os_issue_requests_collected_check CHECK
        ((status = 'collected') = (collected_at IS NOT NULL AND credential_id IS NOT NULL)),
    CONSTRAINT os_issue_requests_approved_check CHECK
        (status NOT IN ('approved', 'collected') OR approved_at IS NOT NULL)
);
