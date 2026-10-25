-- E37a / V2-37c: cancel uncollected requests and index bearer admission counts.
-- Runtime and retention grants are unchanged; the migration contains no data writes.
ALTER TABLE assaf_os.os_assistant_issue_requests
    DROP CONSTRAINT os_issue_requests_status_check;
ALTER TABLE assaf_os.os_assistant_issue_requests
    ADD CONSTRAINT os_issue_requests_status_check CHECK
        (status IN ('pending_approval', 'approved', 'rejected', 'expired', 'collected', 'cancelled'));

CREATE INDEX os_assistant_audit_workspace_ip_occurred_idx
    ON assaf_os.os_assistant_audit (workspace_id, client_ip_hmac, occurred_at);
CREATE INDEX os_assistant_audit_workspace_operation_occurred_idx
    ON assaf_os.os_assistant_audit (workspace_id, operation, occurred_at);
