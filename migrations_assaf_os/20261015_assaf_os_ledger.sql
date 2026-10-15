-- Assaf OS PostgreSQL ledger blueprint.
-- The dedicated ledger runner creates/owns the schema and executes this file as
-- assaf_os_owner in one native PostgreSQL statement batch.

CREATE TABLE assaf_os.os_workspaces (
    workspace_id VARCHAR(128) PRIMARY KEY,
    owner_id VARCHAR(20) NOT NULL CHECK (owner_id ~ '^[0-9]+$'),
    epoch BIGINT NOT NULL CHECK (epoch > 0),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    summaries JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(summaries) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (updated_at >= created_at)
);

CREATE TABLE assaf_os.os_authority (
    workspace_id VARCHAR(128) NOT NULL,
    domain VARCHAR(16) NOT NULL CHECK (domain IN ('core', 'statements')),
    state VARCHAR(16) NOT NULL CHECK (state IN ('shadow', 'active', 'fenced', 'restoring')),
    epoch BIGINT NOT NULL CHECK (epoch > 0),
    activated_at TIMESTAMPTZ,
    activated_by VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, domain),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    CHECK ((activated_at IS NULL) = (activated_by IS NULL)),
    CHECK (activated_by IS NULL OR activated_by ~ '^[0-9]+$'),
    CHECK (state <> 'active' OR activated_at IS NOT NULL),
    CHECK (updated_at >= created_at)
);

CREATE TABLE assaf_os.os_leads (
    workspace_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(inputs) = 'object'),
    business_date DATE,
    source VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    deleted_by_request_id UUID,
    person_id VARCHAR(64) NOT NULL,
    PRIMARY KEY (workspace_id, record_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    UNIQUE (workspace_id, person_id),
    CHECK (updated_at >= created_at),
    CHECK ((deleted_at IS NULL) = (deleted_by_request_id IS NULL)),
    CHECK (NOT (inputs ? 'status') OR COALESCE(inputs->>'status' IN
           ('חדש', 'רלוונטי', 'לא רלוונטי', 'לא ענה', 'הפך ללקוח'), false))
);

CREATE TABLE assaf_os.os_clients (
    workspace_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(inputs) = 'object'),
    business_date DATE,
    source VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    deleted_by_request_id UUID,
    PRIMARY KEY (workspace_id, record_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    CHECK (updated_at >= created_at),
    CHECK ((deleted_at IS NULL) = (deleted_by_request_id IS NULL)),
    CHECK (NOT (inputs ? 'status') OR
           COALESCE(inputs->>'status' IN ('פעיל', 'לא פעיל'), false))
);

CREATE TABLE assaf_os.os_works (
    workspace_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(inputs) = 'object'),
    business_date DATE,
    source VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    deleted_by_request_id UUID,
    person_id VARCHAR(64) NOT NULL,
    agreed_amount_minor NUMERIC,
    currency VARCHAR(3),
    PRIMARY KEY (workspace_id, record_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    FOREIGN KEY (workspace_id, person_id)
        REFERENCES assaf_os.os_clients(workspace_id, record_id),
    CHECK (updated_at >= created_at),
    CHECK ((deleted_at IS NULL) = (deleted_by_request_id IS NULL)),
    CHECK (agreed_amount_minor IS NULL OR
           (agreed_amount_minor = trunc(agreed_amount_minor)
            AND agreed_amount_minor >= 0
            AND agreed_amount_minor <= 9007199254740991)),
    CHECK (currency IS NULL OR currency IN ('ILS', 'USD', 'EUR', 'GBP')),
    CHECK (NOT (inputs ? 'status') OR COALESCE(inputs->>'status' IN
           ('מתוכננת', 'פעילה', 'הושלמה', 'בוטלה'), false))
);

CREATE TABLE assaf_os.os_payments (
    workspace_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(inputs) = 'object'),
    business_date DATE,
    source VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    deleted_by_request_id UUID,
    person_id VARCHAR(64) NOT NULL,
    work_id VARCHAR(64) NOT NULL,
    amount_minor NUMERIC NOT NULL,
    currency VARCHAR(3),
    original_payment_id VARCHAR(64),
    PRIMARY KEY (workspace_id, record_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    FOREIGN KEY (workspace_id, person_id)
        REFERENCES assaf_os.os_clients(workspace_id, record_id),
    FOREIGN KEY (workspace_id, work_id)
        REFERENCES assaf_os.os_works(workspace_id, record_id),
    FOREIGN KEY (workspace_id, original_payment_id)
        REFERENCES assaf_os.os_payments(workspace_id, record_id),
    CHECK (updated_at >= created_at),
    CHECK ((deleted_at IS NULL) = (deleted_by_request_id IS NULL)),
    CHECK (amount_minor = trunc(amount_minor)
           AND amount_minor > 0
           AND amount_minor <= 9007199254740991),
    CHECK (currency IS NULL OR currency IN ('ILS', 'USD', 'EUR', 'GBP')),
    CHECK (NOT (inputs ? 'kind') OR
           COALESCE(inputs->>'kind' IN ('התקבל', 'החזר', 'בוטל'), false))
);

CREATE TABLE assaf_os.os_time (
    workspace_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(inputs) = 'object'),
    business_date DATE,
    source VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    deleted_by_request_id UUID,
    person_id VARCHAR(64) NOT NULL,
    work_id VARCHAR(64) NOT NULL,
    minutes BIGINT NOT NULL CHECK (minutes >= 0 AND minutes <= 9007199254740991),
    PRIMARY KEY (workspace_id, record_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    FOREIGN KEY (workspace_id, person_id)
        REFERENCES assaf_os.os_clients(workspace_id, record_id),
    FOREIGN KEY (workspace_id, work_id)
        REFERENCES assaf_os.os_works(workspace_id, record_id),
    CHECK (updated_at >= created_at),
    CHECK ((deleted_at IS NULL) = (deleted_by_request_id IS NULL)),
    CHECK (NOT (inputs ? 'status') OR
           COALESCE(inputs->>'status' IN ('פעיל', 'בוטל'), false))
);

-- A valid 2000-character Unicode source key can exceed a B-tree entry's size.
-- Fixed UTF-8 conversion is independent of client encoding, so this wrapper is
-- immutable for a given text value. Hash collisions fail closed as duplicates.
-- V2-15 must grant its NOINHERIT runtime role EXECUTE on this index helper.
CREATE FUNCTION assaf_os.expense_source_key_sha256(value text)
RETURNS bytea
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog
AS $$
    SELECT sha256(convert_to(value, 'UTF8'));
$$;

REVOKE ALL ON FUNCTION assaf_os.expense_source_key_sha256(text) FROM PUBLIC;

CREATE TABLE assaf_os.os_expenses (
    workspace_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(inputs) = 'object'),
    business_date DATE,
    source VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    deleted_by_request_id UUID,
    amount_minor NUMERIC NOT NULL,
    currency VARCHAR(3),
    source_type VARCHAR(64) NOT NULL,
    source_key VARCHAR(2000) NOT NULL,
    source_digest VARCHAR(64) NOT NULL,
    PRIMARY KEY (workspace_id, record_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    UNIQUE (workspace_id, source_digest),
    CHECK (updated_at >= created_at),
    CHECK ((deleted_at IS NULL) = (deleted_by_request_id IS NULL)),
    CHECK (amount_minor = trunc(amount_minor)
           AND amount_minor > 0
           AND amount_minor <= 9007199254740991),
    CHECK (currency IS NULL OR currency IN ('ILS', 'USD', 'EUR', 'GBP')),
    CHECK (NOT (inputs ? 'payment_status') OR COALESCE(inputs->>'payment_status' IN
           ('שולם', 'לא שולם', 'בוטל'), false))
);

-- Enforce the logical (workspace, source_type, source_key) key from stored text,
-- independently of the caller-supplied source_digest retained from the prototype.
CREATE UNIQUE INDEX os_expenses_source_key_idx
    ON assaf_os.os_expenses(workspace_id, source_type,
                           assaf_os.expense_source_key_sha256(source_key));

CREATE TABLE assaf_os.os_tasks (
    workspace_id VARCHAR(128) NOT NULL,
    record_id VARCHAR(64) NOT NULL,
    revision BIGINT NOT NULL CHECK (revision > 0),
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(inputs) = 'object'),
    business_date DATE,
    source VARCHAR(24) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    deleted_by_request_id UUID,
    person_id VARCHAR(64),
    work_id VARCHAR(64),
    status TEXT GENERATED ALWAYS AS (inputs->>'status') STORED,
    due_date DATE,
    PRIMARY KEY (workspace_id, record_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    FOREIGN KEY (workspace_id, person_id)
        REFERENCES assaf_os.os_clients(workspace_id, record_id),
    FOREIGN KEY (workspace_id, work_id)
        REFERENCES assaf_os.os_works(workspace_id, record_id),
    CHECK (updated_at >= created_at),
    CHECK ((deleted_at IS NULL) = (deleted_by_request_id IS NULL)),
    CHECK (NOT (inputs ? 'status') OR COALESCE(inputs->>'status' IN
           ('לביצוע', 'בתהליך', 'ממתין', 'בוצע', 'בוטל'), false))
);

CREATE INDEX os_leads_workspace_business_date_idx
    ON assaf_os.os_leads(workspace_id, business_date);
CREATE INDEX os_clients_workspace_business_date_idx
    ON assaf_os.os_clients(workspace_id, business_date);
CREATE INDEX os_works_workspace_business_date_idx
    ON assaf_os.os_works(workspace_id, business_date);
CREATE INDEX os_payments_workspace_business_date_idx
    ON assaf_os.os_payments(workspace_id, business_date);
CREATE INDEX os_time_workspace_business_date_idx
    ON assaf_os.os_time(workspace_id, business_date);
CREATE INDEX os_expenses_workspace_business_date_idx
    ON assaf_os.os_expenses(workspace_id, business_date);
CREATE INDEX os_tasks_workspace_business_date_idx
    ON assaf_os.os_tasks(workspace_id, business_date);
CREATE INDEX os_tasks_workspace_status_due_date_idx
    ON assaf_os.os_tasks(workspace_id, status, due_date);

CREATE TABLE assaf_os.os_proposals (
    workspace_id VARCHAR(128) NOT NULL,
    proposal_id VARCHAR(64) NOT NULL,
    request_id UUID NOT NULL,
    request_digest VARCHAR(64) NOT NULL,
    envelope_digest VARCHAR(64) NOT NULL,
    owner_id VARCHAR(20) NOT NULL CHECK (owner_id ~ '^[0-9]+$'),
    epoch BIGINT NOT NULL CHECK (epoch > 0),
    approval_id UUID NOT NULL,
    rejection_id UUID NOT NULL,
    entity VARCHAR(24) NOT NULL,
    record_id VARCHAR(64) NOT NULL,
    expected_revision BIGINT NOT NULL CHECK (expected_revision >= 0),
    mutations JSONB NOT NULL CHECK (jsonb_typeof(mutations) = 'array'),
    dependency_digest VARCHAR(64) NOT NULL,
    source VARCHAR(32) NOT NULL,
    credential_id VARCHAR(128),
    kind VARCHAR(24) NOT NULL CHECK
        (kind IN ('proposal', 'owner_direct', 'owner_undo', 'statement_batch', 'import')),
    status VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK
        (status IN ('pending', 'committed', 'rejected', 'expired', 'target_changed', 'superseded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (workspace_id, proposal_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    UNIQUE (workspace_id, request_id),
    UNIQUE (workspace_id, approval_id),
    UNIQUE (workspace_id, rejection_id),
    CHECK (approval_id <> rejection_id),
    CHECK (expires_at > created_at),
    CHECK (kind <> 'proposal' OR expires_at <= created_at + interval '10 minutes'),
    CHECK (kind <> 'statement_batch' OR expires_at <= created_at + interval '7 days'),
    CHECK (kind NOT IN ('owner_direct', 'owner_undo') OR source = 'OS_DIRECT'),
    CHECK (source NOT LIKE 'ASSISTANT\_%' ESCAPE '\' OR kind = 'statement_batch')
);

CREATE FUNCTION assaf_os.require_pending_assistant_proposal_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.source LIKE 'ASSISTANT\_%' ESCAPE '\' AND NEW.status <> 'pending' THEN
        RAISE EXCEPTION 'assistant proposals must start pending'
            USING ERRCODE = '23514', CONSTRAINT = 'os_proposals_assistant_insert_pending';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION assaf_os.require_pending_assistant_proposal_insert() FROM PUBLIC;

CREATE TRIGGER os_proposals_assistant_insert_pending
BEFORE INSERT ON assaf_os.os_proposals
FOR EACH ROW EXECUTE FUNCTION assaf_os.require_pending_assistant_proposal_insert();

CREATE TABLE assaf_os.os_receipts (
    workspace_id VARCHAR(128) NOT NULL,
    request_id UUID NOT NULL,
    proposal_id VARCHAR(64) NOT NULL,
    epoch BIGINT NOT NULL CHECK (epoch > 0),
    status VARCHAR(24) NOT NULL CHECK
        (status IN ('committed', 'rejected', 'expired', 'target_changed')),
    kind VARCHAR(24) NOT NULL DEFAULT 'proposal' CHECK
        (kind IN ('proposal', 'owner_direct', 'owner_undo', 'statement_batch', 'import')),
    commit_version BIGINT NOT NULL CHECK (commit_version > 0),
    records JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(records) = 'array'),
    completed_at TIMESTAMPTZ NOT NULL,
    undo_until TIMESTAMPTZ,
    undo_state VARCHAR(16) NOT NULL DEFAULT 'committed' CHECK
        (undo_state IN ('committed', 'undone', 'undo_expired')),
    undo_of UUID,
    PRIMARY KEY (workspace_id, request_id),
    FOREIGN KEY (workspace_id, proposal_id)
        REFERENCES assaf_os.os_proposals(workspace_id, proposal_id),
    FOREIGN KEY (workspace_id, undo_of)
        REFERENCES assaf_os.os_receipts(workspace_id, request_id),
    UNIQUE (workspace_id, proposal_id),
    UNIQUE (workspace_id, undo_of),
    CHECK (undo_until IS NULL OR undo_until > completed_at),
    CHECK (undo_of IS NULL OR undo_of <> request_id)
);

CREATE TABLE assaf_os.os_events (
    workspace_id VARCHAR(128) NOT NULL,
    event_id UUID NOT NULL,
    decision_id UUID NOT NULL,
    commit_version BIGINT NOT NULL CHECK (commit_version > 0),
    body JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, event_id),
    FOREIGN KEY (workspace_id, decision_id)
        REFERENCES assaf_os.os_receipts(workspace_id, request_id),
    UNIQUE (workspace_id, decision_id),
    UNIQUE (workspace_id, commit_version)
);

CREATE TABLE assaf_os.os_outbox (
    workspace_id VARCHAR(128) NOT NULL,
    event_id UUID NOT NULL,
    commit_version BIGINT NOT NULL CHECK (commit_version > 0),
    body JSONB NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK
        (status IN ('pending', 'leased', 'sent', 'unknown', 'conflict')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner VARCHAR(128),
    lease_generation BIGINT NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    leased_at TIMESTAMPTZ,
    last_error_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, event_id),
    FOREIGN KEY (workspace_id, event_id)
        REFERENCES assaf_os.os_events(workspace_id, event_id),
    UNIQUE (workspace_id, commit_version),
    CHECK (updated_at >= created_at),
    CHECK ((status = 'leased') = (lease_owner IS NOT NULL)),
    CHECK ((lease_owner IS NULL) = (leased_at IS NULL))
);

CREATE INDEX os_outbox_delivery_idx
    ON assaf_os.os_outbox(status, next_attempt_at, workspace_id, commit_version);

CREATE TABLE assaf_os.os_mirror_state (
    workspace_id VARCHAR(128) PRIMARY KEY,
    destination_binding VARCHAR(256) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK
        (status IN ('pending', 'synced', 'delayed', 'unknown', 'conflict', 'fenced')),
    projected_version BIGINT NOT NULL DEFAULT 0 CHECK (projected_version >= 0),
    verified_version BIGINT NOT NULL DEFAULT 0 CHECK (verified_version >= 0),
    last_attempt_at TIMESTAMPTZ,
    last_verified_at TIMESTAMPTZ,
    last_error_code VARCHAR(64),
    watermark JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(watermark) = 'object'),
    scan_coverage JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(scan_coverage) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    CHECK (verified_version <= projected_version)
);

CREATE TABLE assaf_os.os_summary_monthly (
    workspace_id VARCHAR(128) NOT NULL,
    month_start DATE NOT NULL CHECK (month_start = date_trunc('month', month_start)::date),
    version BIGINT NOT NULL CHECK (version >= 0),
    summary JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(summary) = 'object'),
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, month_start),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)
);

CREATE TABLE assaf_os.os_summary_cycle (
    workspace_id VARCHAR(128) NOT NULL,
    cycle_id VARCHAR(64) NOT NULL,
    version BIGINT NOT NULL CHECK (version >= 0),
    starts_on DATE NOT NULL,
    ends_on DATE NOT NULL,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(summary) = 'object'),
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, cycle_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    CHECK (ends_on >= starts_on)
);

CREATE TABLE assaf_os.os_import_batches (
    workspace_id VARCHAR(128) NOT NULL,
    batch_id UUID NOT NULL,
    source_binding VARCHAR(256) NOT NULL,
    source_digest VARCHAR(64) NOT NULL,
    authority_epoch BIGINT NOT NULL CHECK (authority_epoch > 0),
    status VARCHAR(16) NOT NULL DEFAULT 'staged' CHECK
        (status IN ('staged', 'validated', 'imported', 'quarantined', 'failed')),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(provenance) = 'object'),
    quarantine JSONB CHECK (quarantine IS NULL OR jsonb_typeof(quarantine) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, batch_id),
    FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id),
    UNIQUE (workspace_id, source_digest),
    CHECK ((status IN ('imported', 'quarantined', 'failed')) = (completed_at IS NOT NULL)),
    CHECK (completed_at IS NULL OR completed_at >= created_at),
    CHECK (status <> 'quarantined' OR quarantine IS NOT NULL)
);

CREATE TABLE assaf_os.os_import_rows (
    workspace_id VARCHAR(128) NOT NULL,
    batch_id UUID NOT NULL,
    row_number BIGINT NOT NULL CHECK (row_number > 0),
    entity VARCHAR(24) NOT NULL CHECK
        (entity IN ('leads', 'clients', 'works', 'payments', 'time', 'expenses', 'tasks')),
    record_id VARCHAR(64),
    source_digest VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'staged' CHECK
        (status IN ('staged', 'validated', 'imported', 'quarantined', 'skipped')),
    raw_record JSONB NOT NULL CHECK (jsonb_typeof(raw_record) = 'object'),
    canonical_record JSONB CHECK
        (canonical_record IS NULL OR jsonb_typeof(canonical_record) = 'object'),
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(provenance) = 'object'),
    quarantine JSONB CHECK (quarantine IS NULL OR jsonb_typeof(quarantine) = 'object'),
    PRIMARY KEY (workspace_id, batch_id, row_number),
    FOREIGN KEY (workspace_id, batch_id)
        REFERENCES assaf_os.os_import_batches(workspace_id, batch_id),
    UNIQUE (workspace_id, batch_id, source_digest),
    CHECK (status <> 'quarantined' OR quarantine IS NOT NULL)
);
