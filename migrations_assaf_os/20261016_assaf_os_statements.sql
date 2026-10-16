-- V2-14b addendum 2 of 3 (V2_PG_CONTRACTS.md section 4): the statement tables.
-- Run only by app/db/os_ledger_migrate.py as assaf_os_owner, after
-- 20261016_assaf_os_ledger_core_v2.sql. Access is defined only by role_policy.py
-- (section 18.2); this file defines none.
-- I11: a raw statement, a card-like digit run or a bank credential is never stored.
-- Personal line detail lives only in os_statement_batch_lines.detail while its batch is
-- pending; personal lines and personal rules keep only their HMAC-peppered keys.
-- Every column the contract does not mark NULL is NOT NULL. Every CHECK is named.

CREATE TABLE assaf_os.os_statements (
    workspace_id VARCHAR(128) NOT NULL,
    statement_id VARCHAR(36) NOT NULL
        CONSTRAINT os_statements_statement_id_check CHECK (statement_id ~ '^STM_[0-9a-f]{32}$'),
    account_label VARCHAR(40) NOT NULL
        CONSTRAINT os_statements_account_label_check CHECK (account_label !~ '[0-9]{8,}'),
    charge_date DATE NOT NULL,
    cycle CHAR(7) NOT NULL
        CONSTRAINT os_statements_cycle_check CHECK (cycle ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    cycle_from DATE NOT NULL,
    cycle_to DATE NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CONSTRAINT os_statements_status_check CHECK (status IN ('active', 'replaced')),
    line_count INTEGER NOT NULL
        CONSTRAINT os_statements_line_count_check CHECK (line_count >= 0),
    lines_digest CHAR(64) NOT NULL
        CONSTRAINT os_statements_lines_digest_check CHECK (lines_digest ~ '^[0-9a-f]{64}$'),
    current_proposal_id VARCHAR(64),
    approved_proposal_id VARCHAR(64),
    imported_version BIGINT
        CONSTRAINT os_statements_imported_version_check CHECK (imported_version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT os_statements_pkey PRIMARY KEY (workspace_id, statement_id),
    CONSTRAINT os_statements_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id),
    CONSTRAINT os_statements_current_proposal_fkey FOREIGN KEY (workspace_id, current_proposal_id)
        REFERENCES assaf_os.os_proposals(workspace_id, proposal_id),
    CONSTRAINT os_statements_approved_proposal_fkey
        FOREIGN KEY (workspace_id, approved_proposal_id)
        REFERENCES assaf_os.os_proposals(workspace_id, proposal_id),
    CONSTRAINT os_statements_label_charge_date_key
        UNIQUE (workspace_id, account_label, charge_date),
    CONSTRAINT os_statements_cycle_range_check CHECK (cycle_to >= cycle_from),
    CONSTRAINT os_statements_updated_check CHECK (updated_at >= created_at)
);

-- Required facts: the six a business line always has. The optional ones come in pairs.
CREATE TABLE assaf_os.os_statement_lines (
    workspace_id VARCHAR(128) NOT NULL,
    statement_id VARCHAR(36) NOT NULL,
    line_key CHAR(64) NOT NULL
        CONSTRAINT os_statement_lines_line_key_check CHECK (line_key ~ '^[0-9a-f]{64}$'),
    tag VARCHAR(16) NOT NULL
        CONSTRAINT os_statement_lines_tag_check CHECK (tag IN ('business', 'personal')),
    state VARCHAR(16) NOT NULL DEFAULT 'active'
        CONSTRAINT os_statement_lines_state_check
        CHECK (state IN ('active', 'replaced', 'voided')),
    txn_date DATE,
    merchant_display VARCHAR(80),
    merchant_key VARCHAR(160),
    amount_minor BIGINT
        CONSTRAINT os_statement_lines_amount_minor_check CHECK (amount_minor > 0),
    kind VARCHAR(16)
        CONSTRAINT os_statement_lines_kind_check CHECK (kind IN ('charge', 'credit')),
    currency CHAR(3)
        CONSTRAINT os_statement_lines_currency_check CHECK (currency ~ '^[A-Z]{3}$'),
    original_amount_minor BIGINT
        CONSTRAINT os_statement_lines_original_amount_minor_check
        CHECK (original_amount_minor > 0),
    original_currency CHAR(3)
        CONSTRAINT os_statement_lines_original_currency_check
        CHECK (original_currency ~ '^[A-Z]{3}$'),
    installment_n INTEGER,
    installment_of INTEGER,
    category VARCHAR(40),
    path VARCHAR(40),
    platform VARCHAR(40),
    is_subscription BOOLEAN,
    rule_id VARCHAR(27)
        CONSTRAINT os_statement_lines_rule_id_check CHECK (rule_id ~ '^MR_[0-9a-f]{24}$'),
    assistant_tag VARCHAR(16)
        CONSTRAINT os_statement_lines_assistant_tag_check
        CHECK (assistant_tag IN ('business', 'personal')),
    owner_override JSONB
        CONSTRAINT os_statement_lines_owner_override_check
        CHECK (jsonb_typeof(owner_override) = 'object'),
    expense_id VARCHAR(64),
    matched_expense_id VARCHAR(64),
    expense_revision_at_import BIGINT
        CONSTRAINT os_statement_lines_expense_revision_at_import_check
        CHECK (expense_revision_at_import > 0),
    revision BIGINT NOT NULL
        CONSTRAINT os_statement_lines_revision_check CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT os_statement_lines_pkey PRIMARY KEY (workspace_id, statement_id, line_key),
    CONSTRAINT os_statement_lines_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id),
    CONSTRAINT os_statement_lines_statement_fkey FOREIGN KEY (workspace_id, statement_id)
        REFERENCES assaf_os.os_statements(workspace_id, statement_id),
    CONSTRAINT os_statement_lines_expense_fkey FOREIGN KEY (workspace_id, expense_id)
        REFERENCES assaf_os.os_expenses(workspace_id, record_id),
    CONSTRAINT os_statement_lines_matched_expense_fkey
        FOREIGN KEY (workspace_id, matched_expense_id)
        REFERENCES assaf_os.os_expenses(workspace_id, record_id),
    CONSTRAINT os_statement_lines_personal_check CHECK (tag <> 'personal' OR (
        txn_date IS NULL AND merchant_display IS NULL AND merchant_key IS NULL
        AND amount_minor IS NULL AND kind IS NULL AND currency IS NULL
        AND original_amount_minor IS NULL AND original_currency IS NULL
        AND installment_n IS NULL AND installment_of IS NULL
        AND category IS NULL AND path IS NULL AND platform IS NULL
        AND is_subscription IS NULL AND rule_id IS NULL AND assistant_tag IS NULL
        AND owner_override IS NULL
        AND expense_id IS NULL AND matched_expense_id IS NULL
        AND expense_revision_at_import IS NULL)),
    CONSTRAINT os_statement_lines_business_check CHECK (tag <> 'business' OR (
        txn_date IS NOT NULL AND merchant_display IS NOT NULL AND merchant_key IS NOT NULL
        AND amount_minor IS NOT NULL AND kind IS NOT NULL AND currency IS NOT NULL)),
    CONSTRAINT os_statement_lines_original_pair_check
        CHECK ((original_amount_minor IS NULL) = (original_currency IS NULL)),
    CONSTRAINT os_statement_lines_installment_check CHECK (
        (installment_n IS NULL) = (installment_of IS NULL)
        AND (installment_n IS NULL OR (installment_n >= 1 AND installment_n <= installment_of))),
    CONSTRAINT os_statement_lines_updated_check CHECK (updated_at >= created_at)
);

-- One manual expense is matched by at most one active line.
CREATE UNIQUE INDEX os_statement_lines_matched_expense_key
    ON assaf_os.os_statement_lines(workspace_id, matched_expense_id)
    WHERE state = 'active' AND matched_expense_id IS NOT NULL;

-- The only place personal detail may exist, and only until the purge sets purged_at.
-- FORK-BACKUP dumps exclude this table's data.
CREATE TABLE assaf_os.os_statement_batch_lines (
    workspace_id VARCHAR(128) NOT NULL,
    proposal_id VARCHAR(64) NOT NULL,
    line_key CHAR(64) NOT NULL
        CONSTRAINT os_statement_batch_lines_line_key_check CHECK (line_key ~ '^[0-9a-f]{64}$'),
    occurrence_index INTEGER NOT NULL
        CONSTRAINT os_statement_batch_lines_occurrence_index_check
        CHECK (occurrence_index >= 0),
    detail JSONB
        CONSTRAINT os_statement_batch_lines_detail_check CHECK (jsonb_typeof(detail) = 'object'),
    purged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT os_statement_batch_lines_pkey PRIMARY KEY (workspace_id, proposal_id, line_key),
    CONSTRAINT os_statement_batch_lines_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id),
    CONSTRAINT os_statement_batch_lines_proposal_fkey FOREIGN KEY (workspace_id, proposal_id)
        REFERENCES assaf_os.os_proposals(workspace_id, proposal_id),
    CONSTRAINT os_statement_batch_lines_purge_check
        CHECK ((detail IS NULL) = (purged_at IS NOT NULL)),
    CONSTRAINT os_statement_batch_lines_updated_check CHECK (updated_at >= created_at)
);

-- A personal rule's rule_id is HMAC-derived (section 12.2), never the domain's sha256 id.
CREATE TABLE assaf_os.os_merchant_rules (
    workspace_id VARCHAR(128) NOT NULL,
    rule_id VARCHAR(27) NOT NULL
        CONSTRAINT os_merchant_rules_rule_id_check CHECK (rule_id ~ '^MR_[0-9a-f]{24}$'),
    match VARCHAR(8) NOT NULL
        CONSTRAINT os_merchant_rules_match_check CHECK (match IN ('exact', 'prefix')),
    tag VARCHAR(16) NOT NULL
        CONSTRAINT os_merchant_rules_tag_check CHECK (tag IN ('business', 'personal')),
    merchant_key VARCHAR(160),
    merchant_key_hmac CHAR(64) NOT NULL
        CONSTRAINT os_merchant_rules_merchant_key_hmac_check
        CHECK (merchant_key_hmac ~ '^[0-9a-f]{64}$'),
    category VARCHAR(40),
    path VARCHAR(40),
    platform VARCHAR(40),
    is_subscription BOOLEAN,
    label VARCHAR(80),
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb
        CONSTRAINT os_merchant_rules_aliases_check
        CHECK (jsonb_typeof(aliases) = 'array' AND jsonb_array_length(aliases) <= 10),
    active BOOLEAN NOT NULL DEFAULT true,
    revision BIGINT NOT NULL
        CONSTRAINT os_merchant_rules_revision_check CHECK (revision > 0),
    source_proposal_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT os_merchant_rules_pkey PRIMARY KEY (workspace_id, rule_id),
    CONSTRAINT os_merchant_rules_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id),
    CONSTRAINT os_merchant_rules_source_proposal_fkey FOREIGN KEY (workspace_id, source_proposal_id)
        REFERENCES assaf_os.os_proposals(workspace_id, proposal_id),
    CONSTRAINT os_merchant_rules_match_hmac_key UNIQUE (workspace_id, match, merchant_key_hmac),
    CONSTRAINT os_merchant_rules_personal_check CHECK (tag <> 'personal' OR (
        merchant_key IS NULL AND match = 'exact' AND aliases = '[]'::jsonb AND label IS NULL
        AND category IS NULL AND path IS NULL AND platform IS NULL
        AND is_subscription IS NULL)),
    CONSTRAINT os_merchant_rules_business_check
        CHECK (tag <> 'business' OR merchant_key IS NOT NULL),
    CONSTRAINT os_merchant_rules_updated_check CHECK (updated_at >= created_at)
);

CREATE TABLE assaf_os.os_statement_review_drafts (
    workspace_id VARCHAR(128) NOT NULL,
    proposal_id VARCHAR(64) NOT NULL,
    draft_revision BIGINT NOT NULL DEFAULT 0
        CONSTRAINT os_statement_review_drafts_draft_revision_check CHECK (draft_revision >= 0),
    overrides JSONB NOT NULL DEFAULT '{}'::jsonb
        CONSTRAINT os_statement_review_drafts_overrides_check
        CHECK (jsonb_typeof(overrides) = 'object'),
    choices JSONB NOT NULL DEFAULT '{}'::jsonb
        CONSTRAINT os_statement_review_drafts_choices_check
        CHECK (jsonb_typeof(choices) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT os_statement_review_drafts_pkey PRIMARY KEY (workspace_id, proposal_id),
    CONSTRAINT os_statement_review_drafts_workspace_fkey FOREIGN KEY (workspace_id)
        REFERENCES assaf_os.os_workspaces(workspace_id),
    CONSTRAINT os_statement_review_drafts_proposal_fkey FOREIGN KEY (workspace_id, proposal_id)
        REFERENCES assaf_os.os_proposals(workspace_id, proposal_id),
    CONSTRAINT os_statement_review_drafts_updated_check CHECK (updated_at >= created_at)
);
