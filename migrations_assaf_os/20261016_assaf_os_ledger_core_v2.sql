-- V2-14b addendum 1 of 3 (V2_PG_CONTRACTS.md section 4): core v2 columns and constraints.
-- Run only by app/db/os_ledger_migrate.py as assaf_os_owner, in its digest-journalled
-- transaction, after 20261015_assaf_os_ledger.sql. Access is defined only by
-- app/services/os_ledger/role_policy.py (section 18.2); this file defines none.
-- Each replaced V2-14 constraint is dropped by its exact name, without IF EXISTS:
-- a renamed or missing constraint aborts the whole migration instead of being skipped.

-- The section 10.2 "since" token moves on every insert_proposal and finish.
-- statement_pepper_fp is written once, on the privileged DSN only (section 12.2).
ALTER TABLE assaf_os.os_workspaces
    ADD COLUMN pending_seq BIGINT NOT NULL DEFAULT 0
        CONSTRAINT os_workspaces_pending_seq_check CHECK (pending_seq >= 0),
    ADD COLUMN statement_pepper_fp CHAR(64)
        CONSTRAINT os_workspaces_statement_pepper_fp_check
        CHECK (statement_pepper_fp ~ '^[0-9a-f]{64}$');

ALTER TABLE assaf_os.os_proposals DROP CONSTRAINT os_proposals_status_check;
ALTER TABLE assaf_os.os_proposals
    ADD CONSTRAINT os_proposals_status_check CHECK (status IN (
        'pending', 'committed', 'rejected', 'expired', 'target_changed', 'superseded',
        'abandoned')),
    ADD CONSTRAINT os_proposals_abandoned_kind_check
        CHECK (status <> 'abandoned' OR kind = 'owner_direct');

CREATE INDEX os_proposals_workspace_status_kind_created_idx
    ON assaf_os.os_proposals(workspace_id, status, kind, created_at);

-- Receipts stay append-only; reason_codes is written once, with the receipt.
-- undo_state keeps its V2-14 definition and is inert: undo is derived, never stored.
ALTER TABLE assaf_os.os_receipts DROP CONSTRAINT os_receipts_status_check;
ALTER TABLE assaf_os.os_receipts
    ADD CONSTRAINT os_receipts_status_check CHECK (status IN (
        'committed', 'rejected', 'expired', 'target_changed', 'superseded', 'abandoned')),
    ADD CONSTRAINT os_receipts_undo_of_kind_check
        CHECK ((undo_of IS NOT NULL) = (kind = 'owner_undo')),
    ADD CONSTRAINT os_receipts_undo_until_kind_check
        CHECK (undo_until IS NULL OR (kind = 'owner_direct' AND status = 'committed')),
    ADD COLUMN reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb
        CONSTRAINT os_receipts_reason_codes_check
        CHECK (jsonb_typeof(reason_codes) = 'array' AND jsonb_array_length(reason_codes) <= 20);

-- Section 15: re-staging an unchanged workbook in a later authority epoch is a new batch.
ALTER TABLE assaf_os.os_import_batches
    DROP CONSTRAINT os_import_batches_workspace_id_source_digest_key;
ALTER TABLE assaf_os.os_import_batches
    ADD CONSTRAINT os_import_batches_workspace_epoch_digest_key
        UNIQUE (workspace_id, authority_epoch, source_digest);
