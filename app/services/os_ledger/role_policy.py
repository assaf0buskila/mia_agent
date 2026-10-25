"""Exact V2-14 PostgreSQL grant map and its fail-closed catalog validator.

Nothing connects on import. validate_ledger_role is the V2_PG_CONTRACTS.md §5.5
preflight (first connection, every 10 minutes and after a reconnect; never per
request). validate_ledger_catalog is the operator transaction hook used by
provisioning. V2-14b extends the map for its seven tables and makes retention a
LOGIN NOINHERIT role with exactly its §18.2 purge rights. No default privileges are
granted for future tables.

Besides ACLs and ownership it pins what makes the exact grants safe and durable: no rule,
trigger or policy beyond the reviewed one, a closed dependency island, the owned functions,
every foreign key NO ACTION with its RI triggers enabled, permanent (logged) relations, and
the constraint and index inventory (CONSTRAINT_PINS, INDEX_PINS). V2-14b's pins cover its
seven tables and the constraints and index its core-v2 migration adds to the V2-14 tables.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.os_ledger_migrate import OWNER_ROLE, RUNTIME_ROLE, TABLES, _validate_main_role_catalog

RETENTION_ROLE = "assaf_os_retention"
DENIED = "ledger_role_denied"
# pg_temp last: a session's temporary relation can never shadow a catalog name
# (left out, pg_temp is searched FIRST for relations). Functions never resolve there.
CATALOG_SEARCH_PATH = "SET LOCAL search_path = pg_catalog, pg_temp"

# §18.2: SELECT + INSERT everywhere except two read-only tables. os_authority is written
# by the privileged AuthorityAdmin only, and so is the os_workspaces row itself
# (`ensure_workspace`, §5.4: the runtime never creates a workspace).
_READ_ONLY_TABLES = frozenset({"os_authority", "os_workspaces"})
TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType({
    name: frozenset({"SELECT"} if name in _READ_ONLY_TABLES else {"SELECT", "INSERT"})
    for name in sorted(TABLES)
})
# §18.2: every record table has these UPDATE columns plus its own typed columns.
# Never updatable: workspace_id, record_id, created_at and the generated os_tasks.status.
RECORD_UPDATE_COLUMNS = (
    "revision", "inputs", "business_date", "source", "updated_at", "deleted_at",
    "deleted_by_request_id",
)
_R = RECORD_UPDATE_COLUMNS
COLUMN_UPDATES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "os_workspaces": ("version", "summaries", "updated_at"),
    "os_leads": (*_R, "person_id"),
    "os_clients": _R,
    "os_works": (*_R, "person_id", "agreed_amount_minor", "currency"),
    "os_payments": (*_R, "person_id", "work_id", "amount_minor", "currency", "original_payment_id"),
    "os_time": (*_R, "person_id", "work_id", "minutes"),
    "os_expenses": (*_R, "amount_minor", "currency", "source_type", "source_key", "source_digest"),
    "os_tasks": (*_R, "person_id", "work_id", "due_date"),
    "os_proposals": ("status",),
    "os_outbox": (
        "status", "attempts", "next_attempt_at", "lease_owner", "lease_generation",
        "leased_at", "last_error_code", "updated_at",
    ),
    "os_mirror_state": (
        "status", "projected_version", "verified_version", "last_attempt_at", "last_verified_at",
        "last_error_code", "watermark", "scan_coverage", "updated_at",
    ),
    "os_summary_monthly": ("version", "summary", "calculated_at"),
    "os_summary_cycle": ("version", "starts_on", "ends_on", "summary", "calculated_at"),
    "os_import_batches": ("status", "quarantine", "completed_at"),
    "os_import_rows": ("record_id", "status", "canonical_record", "quarantine"),
})
FUNCTION_PRIVILEGES = frozenset({"expense_source_key_sha256(text)"})
# The complete assaf_os inventory, owned by assaf_os_owner (provisioning re-owns it).
OWNED_TABLES = tuple(sorted(TABLES | {"schema_migrations"}))


class FunctionPin(NamedTuple):
    """The reviewed pg_proc shape of an assaf_os function, as the V2-14 SQL wrote it."""

    language: str
    volatility: str  # pg_proc.provolatile: i immutable, v volatile
    strict: bool
    returns: str
    config: tuple[str, ...] | None  # pg_proc.proconfig: the function's own SET clauses
    source: str  # pg_proc.prosrc: the body between the dollar quotes, verbatim


# Both functions are pinned by body and settings, not just by name and owner: CREATE OR
# REPLACE keeps the owner and the ACL, and ALTER FUNCTION ... SET search_path changes how
# the body resolves names. A test ties each pin to migrations_assaf_os/20261015_*.sql.
FUNCTION_PINS: Mapping[str, FunctionPin] = MappingProxyType({
    "expense_source_key_sha256(text)": FunctionPin(
        "sql", "i", True, "bytea", ("search_path=pg_catalog",),
        "\n    SELECT sha256(convert_to(value, 'UTF8'));\n",
    ),
    "require_pending_assistant_proposal_insert()": FunctionPin(
        "plpgsql", "v", False, "trigger", None,
        r"""
BEGIN
    IF NEW.source LIKE 'ASSISTANT\_%' ESCAPE '\' AND NEW.status <> 'pending' THEN
        RAISE EXCEPTION 'assistant proposals must start pending'
            USING ERRCODE = '23514', CONSTRAINT = 'os_proposals_assistant_insert_pending';
    END IF;
    RETURN NEW;
END;
""",
    ),
})
OWNED_FUNCTIONS = tuple(FUNCTION_PINS)
# The only non-internal trigger on any assaf_os relation:
# (table, trigger, function, tgenabled 'O' = fires normally, tgtype 7 = ROW|BEFORE|INSERT).
# _closure also requires it to have no WHEN clause, no arguments and no column list.
TRIGGERS = frozenset({(
    "os_proposals", "os_proposals_assistant_insert_pending",
    "assaf_os.require_pending_assistant_proposal_insert()", "O", 7,
)})


class ConstraintPin(NamedTuple):
    """One reviewed pg_constraint row of an assaf_os table, as the V2-14 SQL created it."""

    relation: str
    name: str
    # pg_get_constraintdef under CATALOG_SEARCH_PATH, verbatim; None pins the name only. A
    # definition is None exactly when re-creating the constraint from its own deparsed text,
    # which every pg_dump and pg_restore does, deparses it differently: the 17 CHECKs over a
    # varchar column's enumerated values (`= ANY ((ARRAY['a'::character varying, ...])::text[])`
    # becomes `= ANY (ARRAY[('a'::character varying)::text, ...])`). A test enforces both
    # directions, so a restored database is not refused and no other pin escapes its text.
    definition: str | None


# The complete constraint inventory of the owned tables: every PRIMARY KEY, UNIQUE, FOREIGN
# KEY and CHECK, by table, name and server-deparsed definition (the deparse is that of
# PostgreSQL 16, the version the ledger runs on). The database is the exactly-once and
# append-only backstop, so a dropped, renamed, re-targeted or added constraint, and a
# changed definition other than the 17 name-only CHECKs, is a change a migration chunk must
# make on purpose, here. A test derives the keys and foreign keys from
# migrations_assaf_os/20261015_*.sql and compares the rest with a freshly migrated catalog.
# What a foreign key may DO (its actions) is _referential_integrity's generic rule, which
# also covers constraints added later.
CONSTRAINT_PINS: tuple[ConstraintPin, ...] = (
    # os_workspaces
    ConstraintPin("os_workspaces", "os_workspaces_pkey", "PRIMARY KEY (workspace_id)"),
    ConstraintPin("os_workspaces", "os_workspaces_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_workspaces", "os_workspaces_epoch_check", "CHECK ((epoch > 0))"),
    ConstraintPin("os_workspaces", "os_workspaces_owner_id_check",
                  "CHECK (((owner_id)::text ~ '^[0-9]+$'::text))"),
    ConstraintPin("os_workspaces", "os_workspaces_pending_seq_check", "CHECK ((pending_seq >= 0))"),
    ConstraintPin("os_workspaces", "os_workspaces_statement_pepper_fp_check",
                  "CHECK ((statement_pepper_fp ~ '^[0-9a-f]{64}$'::text))"),
    ConstraintPin("os_workspaces", "os_workspaces_summaries_check",
                  "CHECK ((jsonb_typeof(summaries) = 'object'::text))"),
    ConstraintPin("os_workspaces", "os_workspaces_version_check", "CHECK ((version >= 0))"),
    # os_authority
    ConstraintPin("os_authority", "os_authority_pkey", "PRIMARY KEY (workspace_id, domain)"),
    ConstraintPin("os_authority", "os_authority_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_authority", "os_authority_activated_by_check",
                  "CHECK (((activated_by IS NULL) OR ((activated_by)::text ~ "
                  "'^[0-9]+$'::text)))"),
    ConstraintPin("os_authority", "os_authority_check",
                  "CHECK (((activated_at IS NULL) = (activated_by IS NULL)))"),
    ConstraintPin("os_authority", "os_authority_check1",
                  "CHECK ((((state)::text <> 'active'::text) OR (activated_at IS NOT NULL)))"),
    ConstraintPin("os_authority", "os_authority_check2", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_authority", "os_authority_domain_check", None),
    ConstraintPin("os_authority", "os_authority_epoch_check", "CHECK ((epoch > 0))"),
    ConstraintPin("os_authority", "os_authority_state_check", None),
    # os_leads
    ConstraintPin("os_leads", "os_leads_pkey", "PRIMARY KEY (workspace_id, record_id)"),
    ConstraintPin("os_leads", "os_leads_workspace_id_person_id_key",
                  "UNIQUE (workspace_id, person_id)"),
    ConstraintPin("os_leads", "os_leads_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_leads", "os_leads_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_leads", "os_leads_check1",
                  "CHECK (((deleted_at IS NULL) = (deleted_by_request_id IS NULL)))"),
    ConstraintPin("os_leads", "os_leads_inputs_check",
                  "CHECK ((jsonb_typeof(inputs) = 'object'::text))"),
    ConstraintPin("os_leads", "os_leads_inputs_check1",
                  "CHECK (((NOT (inputs ? 'status'::text)) OR COALESCE(((inputs ->> "
                  "'status'::text) = ANY (ARRAY['חדש'::text, 'רלוונטי'::text, 'לא "
                  "רלוונטי'::text, 'לא ענה'::text, 'הפך ללקוח'::text])), false)))"),
    ConstraintPin("os_leads", "os_leads_revision_check", "CHECK ((revision > 0))"),
    # os_clients
    ConstraintPin("os_clients", "os_clients_pkey", "PRIMARY KEY (workspace_id, record_id)"),
    ConstraintPin("os_clients", "os_clients_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_clients", "os_clients_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_clients", "os_clients_check1",
                  "CHECK (((deleted_at IS NULL) = (deleted_by_request_id IS NULL)))"),
    ConstraintPin("os_clients", "os_clients_inputs_check",
                  "CHECK ((jsonb_typeof(inputs) = 'object'::text))"),
    ConstraintPin("os_clients", "os_clients_inputs_check1",
                  "CHECK (((NOT (inputs ? 'status'::text)) OR COALESCE(((inputs ->> "
                  "'status'::text) = ANY (ARRAY['פעיל'::text, 'לא פעיל'::text])), false)))"),
    ConstraintPin("os_clients", "os_clients_revision_check", "CHECK ((revision > 0))"),
    # os_works
    ConstraintPin("os_works", "os_works_pkey", "PRIMARY KEY (workspace_id, record_id)"),
    ConstraintPin("os_works", "os_works_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_works", "os_works_workspace_id_person_id_fkey",
                  "FOREIGN KEY (workspace_id, person_id) REFERENCES "
                  "assaf_os.os_clients(workspace_id, record_id)"),
    ConstraintPin("os_works", "os_works_agreed_amount_minor_check",
                  "CHECK (((agreed_amount_minor IS NULL) OR ((agreed_amount_minor = "
                  "trunc(agreed_amount_minor)) AND (agreed_amount_minor >= (0)::numeric) AND "
                  "(agreed_amount_minor <= ('9007199254740991'::bigint)::numeric))))"),
    ConstraintPin("os_works", "os_works_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_works", "os_works_check1",
                  "CHECK (((deleted_at IS NULL) = (deleted_by_request_id IS NULL)))"),
    ConstraintPin("os_works", "os_works_currency_check", None),
    ConstraintPin("os_works", "os_works_inputs_check",
                  "CHECK ((jsonb_typeof(inputs) = 'object'::text))"),
    ConstraintPin("os_works", "os_works_inputs_check1",
                  "CHECK (((NOT (inputs ? 'status'::text)) OR COALESCE(((inputs ->> "
                  "'status'::text) = ANY (ARRAY['מתוכננת'::text, 'פעילה'::text, 'הושלמה'::text, "
                  "'בוטלה'::text])), false)))"),
    ConstraintPin("os_works", "os_works_revision_check", "CHECK ((revision > 0))"),
    # os_payments
    ConstraintPin("os_payments", "os_payments_pkey", "PRIMARY KEY (workspace_id, record_id)"),
    ConstraintPin("os_payments", "os_payments_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_payments", "os_payments_workspace_id_original_payment_id_fkey",
                  "FOREIGN KEY (workspace_id, original_payment_id) REFERENCES "
                  "assaf_os.os_payments(workspace_id, record_id)"),
    ConstraintPin("os_payments", "os_payments_workspace_id_person_id_fkey",
                  "FOREIGN KEY (workspace_id, person_id) REFERENCES "
                  "assaf_os.os_clients(workspace_id, record_id)"),
    ConstraintPin("os_payments", "os_payments_workspace_id_work_id_fkey",
                  "FOREIGN KEY (workspace_id, work_id) REFERENCES "
                  "assaf_os.os_works(workspace_id, record_id)"),
    ConstraintPin("os_payments", "os_payments_amount_minor_check",
                  "CHECK (((amount_minor = trunc(amount_minor)) AND (amount_minor > "
                  "(0)::numeric) AND (amount_minor <= ('9007199254740991'::bigint)::numeric)))"),
    ConstraintPin("os_payments", "os_payments_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_payments", "os_payments_check1",
                  "CHECK (((deleted_at IS NULL) = (deleted_by_request_id IS NULL)))"),
    ConstraintPin("os_payments", "os_payments_currency_check", None),
    ConstraintPin("os_payments", "os_payments_inputs_check",
                  "CHECK ((jsonb_typeof(inputs) = 'object'::text))"),
    ConstraintPin("os_payments", "os_payments_inputs_check1",
                  "CHECK (((NOT (inputs ? 'kind'::text)) OR COALESCE(((inputs ->> 'kind'::text) "
                  "= ANY (ARRAY['התקבל'::text, 'החזר'::text, 'בוטל'::text])), false)))"),
    ConstraintPin("os_payments", "os_payments_revision_check", "CHECK ((revision > 0))"),
    # os_time
    ConstraintPin("os_time", "os_time_pkey", "PRIMARY KEY (workspace_id, record_id)"),
    ConstraintPin("os_time", "os_time_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_time", "os_time_workspace_id_person_id_fkey",
                  "FOREIGN KEY (workspace_id, person_id) REFERENCES "
                  "assaf_os.os_clients(workspace_id, record_id)"),
    ConstraintPin("os_time", "os_time_workspace_id_work_id_fkey",
                  "FOREIGN KEY (workspace_id, work_id) REFERENCES "
                  "assaf_os.os_works(workspace_id, record_id)"),
    ConstraintPin("os_time", "os_time_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_time", "os_time_check1",
                  "CHECK (((deleted_at IS NULL) = (deleted_by_request_id IS NULL)))"),
    ConstraintPin("os_time", "os_time_inputs_check",
                  "CHECK ((jsonb_typeof(inputs) = 'object'::text))"),
    ConstraintPin("os_time", "os_time_inputs_check1",
                  "CHECK (((NOT (inputs ? 'status'::text)) OR COALESCE(((inputs ->> "
                  "'status'::text) = ANY (ARRAY['פעיל'::text, 'בוטל'::text])), false)))"),
    ConstraintPin("os_time", "os_time_minutes_check",
                  "CHECK (((minutes >= 0) AND (minutes <= '9007199254740991'::bigint)))"),
    ConstraintPin("os_time", "os_time_revision_check", "CHECK ((revision > 0))"),
    # os_expenses
    ConstraintPin("os_expenses", "os_expenses_pkey", "PRIMARY KEY (workspace_id, record_id)"),
    ConstraintPin("os_expenses", "os_expenses_workspace_id_source_digest_key",
                  "UNIQUE (workspace_id, source_digest)"),
    ConstraintPin("os_expenses", "os_expenses_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_expenses", "os_expenses_amount_minor_check",
                  "CHECK (((amount_minor = trunc(amount_minor)) AND (amount_minor > "
                  "(0)::numeric) AND (amount_minor <= ('9007199254740991'::bigint)::numeric)))"),
    ConstraintPin("os_expenses", "os_expenses_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_expenses", "os_expenses_check1",
                  "CHECK (((deleted_at IS NULL) = (deleted_by_request_id IS NULL)))"),
    ConstraintPin("os_expenses", "os_expenses_currency_check", None),
    ConstraintPin("os_expenses", "os_expenses_inputs_check",
                  "CHECK ((jsonb_typeof(inputs) = 'object'::text))"),
    ConstraintPin("os_expenses", "os_expenses_inputs_check1",
                  "CHECK (((NOT (inputs ? 'payment_status'::text)) OR COALESCE(((inputs ->> "
                  "'payment_status'::text) = ANY (ARRAY['שולם'::text, 'לא שולם'::text, "
                  "'בוטל'::text])), false)))"),
    ConstraintPin("os_expenses", "os_expenses_revision_check", "CHECK ((revision > 0))"),
    # os_tasks
    ConstraintPin("os_tasks", "os_tasks_pkey", "PRIMARY KEY (workspace_id, record_id)"),
    ConstraintPin("os_tasks", "os_tasks_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_tasks", "os_tasks_workspace_id_person_id_fkey",
                  "FOREIGN KEY (workspace_id, person_id) REFERENCES "
                  "assaf_os.os_clients(workspace_id, record_id)"),
    ConstraintPin("os_tasks", "os_tasks_workspace_id_work_id_fkey",
                  "FOREIGN KEY (workspace_id, work_id) REFERENCES "
                  "assaf_os.os_works(workspace_id, record_id)"),
    ConstraintPin("os_tasks", "os_tasks_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_tasks", "os_tasks_check1",
                  "CHECK (((deleted_at IS NULL) = (deleted_by_request_id IS NULL)))"),
    ConstraintPin("os_tasks", "os_tasks_inputs_check",
                  "CHECK ((jsonb_typeof(inputs) = 'object'::text))"),
    ConstraintPin("os_tasks", "os_tasks_inputs_check1",
                  "CHECK (((NOT (inputs ? 'status'::text)) OR COALESCE(((inputs ->> "
                  "'status'::text) = ANY (ARRAY['לביצוע'::text, 'בתהליך'::text, 'ממתין'::text, "
                  "'בוצע'::text, 'בוטל'::text])), false)))"),
    ConstraintPin("os_tasks", "os_tasks_revision_check", "CHECK ((revision > 0))"),
    # os_proposals
    ConstraintPin("os_proposals", "os_proposals_pkey", "PRIMARY KEY (workspace_id, proposal_id)"),
    ConstraintPin("os_proposals", "os_proposals_workspace_id_approval_id_key",
                  "UNIQUE (workspace_id, approval_id)"),
    ConstraintPin("os_proposals", "os_proposals_workspace_id_rejection_id_key",
                  "UNIQUE (workspace_id, rejection_id)"),
    ConstraintPin("os_proposals", "os_proposals_workspace_id_request_id_key",
                  "UNIQUE (workspace_id, request_id)"),
    ConstraintPin("os_proposals", "os_proposals_credential_fkey",
                  "FOREIGN KEY (workspace_id, credential_id) REFERENCES "
                  "assaf_os.os_assistant_credentials(workspace_id, credential_id)"),
    ConstraintPin("os_proposals", "os_proposals_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_proposals", "os_proposals_abandoned_kind_check",
                  "CHECK ((((status)::text <> 'abandoned'::text) OR ((kind)::text = "
                  "'owner_direct'::text)))"),
    ConstraintPin("os_proposals", "os_proposals_check", "CHECK ((approval_id <> rejection_id))"),
    ConstraintPin("os_proposals", "os_proposals_check1", "CHECK ((expires_at > created_at))"),
    ConstraintPin("os_proposals", "os_proposals_check2",
                  "CHECK ((((kind)::text <> 'proposal'::text) OR (expires_at <= (created_at + "
                  "'00:10:00'::interval))))"),
    ConstraintPin("os_proposals", "os_proposals_check3",
                  "CHECK ((((kind)::text <> 'statement_batch'::text) OR (expires_at <= "
                  "(created_at + '7 days'::interval))))"),
    ConstraintPin("os_proposals", "os_proposals_check4", None),
    ConstraintPin("os_proposals", "os_proposals_check5",
                  r"CHECK ((((source)::text !~~ like_escape('ASSISTANT\_%'::text, '\'::text)) OR "
                  "((kind)::text = 'statement_batch'::text)))"),
    ConstraintPin("os_proposals", "os_proposals_epoch_check", "CHECK ((epoch > 0))"),
    ConstraintPin("os_proposals", "os_proposals_expected_revision_check",
                  "CHECK ((expected_revision >= 0))"),
    ConstraintPin("os_proposals", "os_proposals_kind_check", None),
    ConstraintPin("os_proposals", "os_proposals_mutations_check",
                  "CHECK ((jsonb_typeof(mutations) = 'array'::text))"),
    ConstraintPin("os_proposals", "os_proposals_owner_id_check",
                  "CHECK (((owner_id)::text ~ '^[0-9]+$'::text))"),
    ConstraintPin("os_proposals", "os_proposals_source_check",
                  "CHECK (((source)::text ~ '^[A-Z][A-Z0-9_]{1,31}$'::text))"),
    ConstraintPin("os_proposals", "os_proposals_status_check", None),
    # os_receipts
    ConstraintPin("os_receipts", "os_receipts_pkey", "PRIMARY KEY (workspace_id, request_id)"),
    ConstraintPin("os_receipts", "os_receipts_workspace_id_proposal_id_key",
                  "UNIQUE (workspace_id, proposal_id)"),
    ConstraintPin("os_receipts", "os_receipts_workspace_id_undo_of_key",
                  "UNIQUE (workspace_id, undo_of)"),
    ConstraintPin("os_receipts", "os_receipts_workspace_id_proposal_id_fkey",
                  "FOREIGN KEY (workspace_id, proposal_id) REFERENCES "
                  "assaf_os.os_proposals(workspace_id, proposal_id)"),
    ConstraintPin("os_receipts", "os_receipts_workspace_id_undo_of_fkey",
                  "FOREIGN KEY (workspace_id, undo_of) REFERENCES "
                  "assaf_os.os_receipts(workspace_id, request_id)"),
    ConstraintPin("os_receipts", "os_receipts_check",
                  "CHECK (((undo_until IS NULL) OR (undo_until > completed_at)))"),
    ConstraintPin("os_receipts", "os_receipts_check1",
                  "CHECK (((undo_of IS NULL) OR (undo_of <> request_id)))"),
    ConstraintPin("os_receipts", "os_receipts_commit_version_check",
                  "CHECK ((commit_version > 0))"),
    ConstraintPin("os_receipts", "os_receipts_epoch_check", "CHECK ((epoch > 0))"),
    ConstraintPin("os_receipts", "os_receipts_kind_check", None),
    ConstraintPin("os_receipts", "os_receipts_reason_codes_check",
                  "CHECK (((jsonb_typeof(reason_codes) = 'array'::text) AND "
                  "(jsonb_array_length(reason_codes) <= 20)))"),
    ConstraintPin("os_receipts", "os_receipts_records_check",
                  "CHECK ((jsonb_typeof(records) = 'array'::text))"),
    ConstraintPin("os_receipts", "os_receipts_status_check", None),
    ConstraintPin("os_receipts", "os_receipts_undo_of_kind_check",
                  "CHECK (((undo_of IS NOT NULL) = ((kind)::text = 'owner_undo'::text)))"),
    ConstraintPin("os_receipts", "os_receipts_undo_state_check", None),
    ConstraintPin("os_receipts", "os_receipts_undo_until_kind_check",
                  "CHECK (((undo_until IS NULL) OR (((kind)::text = 'owner_direct'::text) AND "
                  "((status)::text = 'committed'::text))))"),
    # os_events
    ConstraintPin("os_events", "os_events_pkey", "PRIMARY KEY (workspace_id, event_id)"),
    ConstraintPin("os_events", "os_events_workspace_id_commit_version_key",
                  "UNIQUE (workspace_id, commit_version)"),
    ConstraintPin("os_events", "os_events_workspace_id_decision_id_key",
                  "UNIQUE (workspace_id, decision_id)"),
    ConstraintPin("os_events", "os_events_workspace_id_decision_id_fkey",
                  "FOREIGN KEY (workspace_id, decision_id) REFERENCES "
                  "assaf_os.os_receipts(workspace_id, request_id)"),
    ConstraintPin("os_events", "os_events_body_check",
                  "CHECK ((jsonb_typeof(body) = 'object'::text))"),
    ConstraintPin("os_events", "os_events_commit_version_check", "CHECK ((commit_version > 0))"),
    # os_outbox
    ConstraintPin("os_outbox", "os_outbox_pkey", "PRIMARY KEY (workspace_id, event_id)"),
    ConstraintPin("os_outbox", "os_outbox_workspace_id_commit_version_key",
                  "UNIQUE (workspace_id, commit_version)"),
    ConstraintPin("os_outbox", "os_outbox_workspace_id_event_id_fkey",
                  "FOREIGN KEY (workspace_id, event_id) REFERENCES "
                  "assaf_os.os_events(workspace_id, event_id)"),
    ConstraintPin("os_outbox", "os_outbox_attempts_check", "CHECK ((attempts >= 0))"),
    ConstraintPin("os_outbox", "os_outbox_body_check",
                  "CHECK ((jsonb_typeof(body) = 'object'::text))"),
    ConstraintPin("os_outbox", "os_outbox_check", "CHECK ((updated_at >= created_at))"),
    ConstraintPin("os_outbox", "os_outbox_check1",
                  "CHECK ((((status)::text = 'leased'::text) = (lease_owner IS NOT NULL)))"),
    ConstraintPin("os_outbox", "os_outbox_check2",
                  "CHECK (((lease_owner IS NULL) = (leased_at IS NULL)))"),
    ConstraintPin("os_outbox", "os_outbox_commit_version_check", "CHECK ((commit_version > 0))"),
    ConstraintPin("os_outbox", "os_outbox_lease_generation_check",
                  "CHECK ((lease_generation >= 0))"),
    ConstraintPin("os_outbox", "os_outbox_status_check", None),
    # os_mirror_state
    ConstraintPin("os_mirror_state", "os_mirror_state_pkey", "PRIMARY KEY (workspace_id)"),
    ConstraintPin("os_mirror_state", "os_mirror_state_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_mirror_state", "os_mirror_state_check",
                  "CHECK ((verified_version <= projected_version))"),
    ConstraintPin("os_mirror_state", "os_mirror_state_projected_version_check",
                  "CHECK ((projected_version >= 0))"),
    ConstraintPin("os_mirror_state", "os_mirror_state_scan_coverage_check",
                  "CHECK ((jsonb_typeof(scan_coverage) = 'object'::text))"),
    ConstraintPin("os_mirror_state", "os_mirror_state_status_check", None),
    ConstraintPin("os_mirror_state", "os_mirror_state_verified_version_check",
                  "CHECK ((verified_version >= 0))"),
    ConstraintPin("os_mirror_state", "os_mirror_state_watermark_check",
                  "CHECK ((jsonb_typeof(watermark) = 'object'::text))"),
    # os_summary_monthly
    ConstraintPin("os_summary_monthly", "os_summary_monthly_pkey",
                  "PRIMARY KEY (workspace_id, month_start)"),
    ConstraintPin("os_summary_monthly", "os_summary_monthly_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_summary_monthly", "os_summary_monthly_month_start_check",
                  "CHECK ((month_start = (date_trunc('month'::text, (month_start)::timestamp "
                  "with time zone))::date))"),
    ConstraintPin("os_summary_monthly", "os_summary_monthly_summary_check",
                  "CHECK ((jsonb_typeof(summary) = 'object'::text))"),
    ConstraintPin("os_summary_monthly", "os_summary_monthly_version_check",
                  "CHECK ((version >= 0))"),
    # os_summary_cycle
    ConstraintPin("os_summary_cycle", "os_summary_cycle_pkey",
                  "PRIMARY KEY (workspace_id, cycle_id)"),
    ConstraintPin("os_summary_cycle", "os_summary_cycle_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_summary_cycle", "os_summary_cycle_check", "CHECK ((ends_on >= starts_on))"),
    ConstraintPin("os_summary_cycle", "os_summary_cycle_summary_check",
                  "CHECK ((jsonb_typeof(summary) = 'object'::text))"),
    ConstraintPin("os_summary_cycle", "os_summary_cycle_version_check", "CHECK ((version >= 0))"),
    # os_import_batches
    ConstraintPin("os_import_batches", "os_import_batches_pkey",
                  "PRIMARY KEY (workspace_id, batch_id)"),
    ConstraintPin("os_import_batches", "os_import_batches_workspace_epoch_digest_key",
                  "UNIQUE (workspace_id, authority_epoch, source_digest)"),
    ConstraintPin("os_import_batches", "os_import_batches_workspace_id_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_import_batches", "os_import_batches_authority_epoch_check",
                  "CHECK ((authority_epoch > 0))"),
    ConstraintPin("os_import_batches", "os_import_batches_check", None),
    ConstraintPin("os_import_batches", "os_import_batches_check1",
                  "CHECK (((completed_at IS NULL) OR (completed_at >= created_at)))"),
    ConstraintPin("os_import_batches", "os_import_batches_check2",
                  "CHECK ((((status)::text <> 'quarantined'::text) OR (quarantine IS NOT "
                  "NULL)))"),
    ConstraintPin("os_import_batches", "os_import_batches_provenance_check",
                  "CHECK ((jsonb_typeof(provenance) = 'object'::text))"),
    ConstraintPin("os_import_batches", "os_import_batches_quarantine_check",
                  "CHECK (((quarantine IS NULL) OR (jsonb_typeof(quarantine) = "
                  "'object'::text)))"),
    ConstraintPin("os_import_batches", "os_import_batches_status_check", None),
    # os_import_rows
    ConstraintPin("os_import_rows", "os_import_rows_pkey",
                  "PRIMARY KEY (workspace_id, batch_id, row_number)"),
    ConstraintPin("os_import_rows", "os_import_rows_workspace_id_batch_id_source_digest_key",
                  "UNIQUE (workspace_id, batch_id, source_digest)"),
    ConstraintPin("os_import_rows", "os_import_rows_workspace_id_batch_id_fkey",
                  "FOREIGN KEY (workspace_id, batch_id) REFERENCES "
                  "assaf_os.os_import_batches(workspace_id, batch_id)"),
    ConstraintPin("os_import_rows", "os_import_rows_canonical_record_check",
                  "CHECK (((canonical_record IS NULL) OR (jsonb_typeof(canonical_record) = "
                  "'object'::text)))"),
    ConstraintPin("os_import_rows", "os_import_rows_check",
                  "CHECK ((((status)::text <> 'quarantined'::text) OR (quarantine IS NOT "
                  "NULL)))"),
    ConstraintPin("os_import_rows", "os_import_rows_entity_check", None),
    ConstraintPin("os_import_rows", "os_import_rows_provenance_check",
                  "CHECK ((jsonb_typeof(provenance) = 'object'::text))"),
    ConstraintPin("os_import_rows", "os_import_rows_quarantine_check",
                  "CHECK (((quarantine IS NULL) OR (jsonb_typeof(quarantine) = "
                  "'object'::text)))"),
    ConstraintPin("os_import_rows", "os_import_rows_raw_record_check",
                  "CHECK ((jsonb_typeof(raw_record) = 'object'::text))"),
    ConstraintPin("os_import_rows", "os_import_rows_row_number_check", "CHECK ((row_number > 0))"),
    ConstraintPin("os_import_rows", "os_import_rows_status_check", None),
    # os_statements
    ConstraintPin("os_statements", "os_statements_pkey",
                  "PRIMARY KEY (workspace_id, statement_id)"),
    ConstraintPin("os_statements", "os_statements_label_charge_date_key",
                  "UNIQUE (workspace_id, account_label, charge_date)"),
    ConstraintPin("os_statements", "os_statements_approved_proposal_fkey",
                  "FOREIGN KEY (workspace_id, approved_proposal_id) REFERENCES "
                  "assaf_os.os_proposals(workspace_id, proposal_id)"),
    ConstraintPin("os_statements", "os_statements_current_proposal_fkey",
                  "FOREIGN KEY (workspace_id, current_proposal_id) REFERENCES "
                  "assaf_os.os_proposals(workspace_id, proposal_id)"),
    ConstraintPin("os_statements", "os_statements_workspace_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_statements", "os_statements_account_label_check",
                  "CHECK (((account_label)::text !~ '[0-9]{8,}'::text))"),
    ConstraintPin("os_statements", "os_statements_cycle_check",
                  "CHECK ((cycle ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'::text))"),
    ConstraintPin("os_statements", "os_statements_cycle_range_check",
                  "CHECK ((cycle_to >= cycle_from))"),
    ConstraintPin("os_statements", "os_statements_imported_version_check",
                  "CHECK ((imported_version > 0))"),
    ConstraintPin("os_statements", "os_statements_line_count_check", "CHECK ((line_count >= 0))"),
    ConstraintPin("os_statements", "os_statements_lines_digest_check",
                  "CHECK ((lines_digest ~ '^[0-9a-f]{64}$'::text))"),
    ConstraintPin("os_statements", "os_statements_statement_id_check",
                  "CHECK (((statement_id)::text ~ '^STM_[0-9a-f]{32}$'::text))"),
    ConstraintPin("os_statements", "os_statements_status_check", None),
    ConstraintPin("os_statements", "os_statements_updated_check",
                  "CHECK ((updated_at >= created_at))"),
    # os_statement_lines
    ConstraintPin("os_statement_lines", "os_statement_lines_pkey",
                  "PRIMARY KEY (workspace_id, statement_id, line_key)"),
    ConstraintPin("os_statement_lines", "os_statement_lines_expense_fkey",
                  "FOREIGN KEY (workspace_id, expense_id) REFERENCES "
                  "assaf_os.os_expenses(workspace_id, record_id)"),
    ConstraintPin("os_statement_lines", "os_statement_lines_matched_expense_fkey",
                  "FOREIGN KEY (workspace_id, matched_expense_id) REFERENCES "
                  "assaf_os.os_expenses(workspace_id, record_id)"),
    ConstraintPin("os_statement_lines", "os_statement_lines_statement_fkey",
                  "FOREIGN KEY (workspace_id, statement_id) REFERENCES "
                  "assaf_os.os_statements(workspace_id, statement_id)"),
    ConstraintPin("os_statement_lines", "os_statement_lines_workspace_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_statement_lines", "os_statement_lines_amount_minor_check",
                  "CHECK ((amount_minor > 0))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_assistant_tag_check", None),
    ConstraintPin("os_statement_lines", "os_statement_lines_business_check",
                  "CHECK ((((tag)::text <> 'business'::text) OR ((txn_date IS NOT NULL) AND "
                  "(merchant_display IS NOT NULL) AND (merchant_key IS NOT NULL) AND "
                  "(amount_minor IS NOT NULL) AND (kind IS NOT NULL) AND "
                  "(currency IS NOT NULL))))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_currency_check",
                  "CHECK ((currency ~ '^[A-Z]{3}$'::text))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_expense_revision_at_import_check",
                  "CHECK ((expense_revision_at_import > 0))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_installment_check",
                  "CHECK ((((installment_n IS NULL) = (installment_of IS NULL)) AND "
                  "((installment_n IS NULL) OR ((installment_n >= 1) AND (installment_n <= "
                  "installment_of)))))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_kind_check", None),
    ConstraintPin("os_statement_lines", "os_statement_lines_line_key_check",
                  "CHECK ((line_key ~ '^[0-9a-f]{64}$'::text))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_original_amount_minor_check",
                  "CHECK ((original_amount_minor > 0))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_original_currency_check",
                  "CHECK ((original_currency ~ '^[A-Z]{3}$'::text))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_original_pair_check",
                  "CHECK (((original_amount_minor IS NULL) = (original_currency IS NULL)))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_owner_override_check",
                  "CHECK ((jsonb_typeof(owner_override) = 'object'::text))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_personal_check",
                  "CHECK ((((tag)::text <> 'personal'::text) OR ((txn_date IS NULL) AND "
                  "(merchant_display IS NULL) AND (merchant_key IS NULL) AND (amount_minor IS "
                  "NULL) AND (kind IS NULL) AND (currency IS NULL) AND (original_amount_minor IS "
                  "NULL) AND (original_currency IS NULL) AND (installment_n IS NULL) AND "
                  "(installment_of IS NULL) AND (category IS NULL) AND (path IS NULL) AND "
                  "(platform IS NULL) AND (is_subscription IS NULL) AND (rule_id IS NULL) AND "
                  "(assistant_tag IS NULL) AND (owner_override IS NULL) AND (expense_id IS NULL) "
                  "AND (matched_expense_id IS NULL) AND (expense_revision_at_import IS NULL))))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_revision_check",
                  "CHECK ((revision > 0))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_rule_id_check",
                  "CHECK (((rule_id)::text ~ '^MR_[0-9a-f]{24}$'::text))"),
    ConstraintPin("os_statement_lines", "os_statement_lines_state_check", None),
    ConstraintPin("os_statement_lines", "os_statement_lines_tag_check", None),
    ConstraintPin("os_statement_lines", "os_statement_lines_updated_check",
                  "CHECK ((updated_at >= created_at))"),
    # os_statement_batch_lines
    ConstraintPin("os_statement_batch_lines", "os_statement_batch_lines_pkey",
                  "PRIMARY KEY (workspace_id, proposal_id, line_key)"),
    ConstraintPin("os_statement_batch_lines", "os_statement_batch_lines_proposal_fkey",
                  "FOREIGN KEY (workspace_id, proposal_id) REFERENCES "
                  "assaf_os.os_proposals(workspace_id, proposal_id)"),
    ConstraintPin("os_statement_batch_lines", "os_statement_batch_lines_workspace_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_statement_batch_lines", "os_statement_batch_lines_detail_check",
                  "CHECK ((jsonb_typeof(detail) = 'object'::text))"),
    ConstraintPin("os_statement_batch_lines", "os_statement_batch_lines_line_key_check",
                  "CHECK ((line_key ~ '^[0-9a-f]{64}$'::text))"),
    ConstraintPin("os_statement_batch_lines", "os_statement_batch_lines_occurrence_index_check",
                  "CHECK ((occurrence_index >= 0))"),
    ConstraintPin("os_statement_batch_lines", "os_statement_batch_lines_purge_check",
                  "CHECK (((detail IS NULL) = (purged_at IS NOT NULL)))"),
    ConstraintPin("os_statement_batch_lines", "os_statement_batch_lines_updated_check",
                  "CHECK ((updated_at >= created_at))"),
    # os_merchant_rules
    ConstraintPin("os_merchant_rules", "os_merchant_rules_pkey",
                  "PRIMARY KEY (workspace_id, rule_id)"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_match_hmac_key",
                  "UNIQUE (workspace_id, match, merchant_key_hmac)"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_source_proposal_fkey",
                  "FOREIGN KEY (workspace_id, source_proposal_id) REFERENCES "
                  "assaf_os.os_proposals(workspace_id, proposal_id)"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_workspace_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_aliases_check",
                  "CHECK (((jsonb_typeof(aliases) = 'array'::text) AND "
                  "(jsonb_array_length(aliases) <= 10)))"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_business_check",
                  "CHECK ((((tag)::text <> 'business'::text) OR (merchant_key IS NOT NULL)))"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_match_check", None),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_merchant_key_hmac_check",
                  "CHECK ((merchant_key_hmac ~ '^[0-9a-f]{64}$'::text))"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_personal_check",
                  "CHECK ((((tag)::text <> 'personal'::text) OR ((merchant_key IS NULL) AND "
                  "((match)::text = 'exact'::text) AND (aliases = '[]'::jsonb) AND (label IS "
                  "NULL) AND (category IS NULL) AND (path IS NULL) AND (platform IS NULL) AND "
                  "(is_subscription IS NULL))))"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_revision_check",
                  "CHECK ((revision > 0))"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_rule_id_check",
                  "CHECK (((rule_id)::text ~ '^MR_[0-9a-f]{24}$'::text))"),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_tag_check", None),
    ConstraintPin("os_merchant_rules", "os_merchant_rules_updated_check",
                  "CHECK ((updated_at >= created_at))"),
    # os_statement_review_drafts
    ConstraintPin("os_statement_review_drafts", "os_statement_review_drafts_pkey",
                  "PRIMARY KEY (workspace_id, proposal_id)"),
    ConstraintPin("os_statement_review_drafts", "os_statement_review_drafts_proposal_fkey",
                  "FOREIGN KEY (workspace_id, proposal_id) REFERENCES "
                  "assaf_os.os_proposals(workspace_id, proposal_id)"),
    ConstraintPin("os_statement_review_drafts", "os_statement_review_drafts_workspace_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_statement_review_drafts", "os_statement_review_drafts_choices_check",
                  "CHECK ((jsonb_typeof(choices) = 'object'::text))"),
    ConstraintPin("os_statement_review_drafts", "os_statement_review_drafts_draft_revision_check",
                  "CHECK ((draft_revision >= 0))"),
    ConstraintPin("os_statement_review_drafts", "os_statement_review_drafts_overrides_check",
                  "CHECK ((jsonb_typeof(overrides) = 'object'::text))"),
    ConstraintPin("os_statement_review_drafts", "os_statement_review_drafts_updated_check",
                  "CHECK ((updated_at >= created_at))"),
    # os_assistant_credentials
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_pkey",
                  "PRIMARY KEY (workspace_id, credential_id)"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_secret_hash_key",
                  "UNIQUE (secret_hash)"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_workspace_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_credential_id_check",
                  "CHECK (((credential_id)::text ~ '^AC_[0-9a-f]{32}$'::text))"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_epoch_check",
                  "CHECK ((epoch > 0))"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_expiry_check",
                  "CHECK ((expires_at > created_at))"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_expiry_max_check",
                  "CHECK ((expires_at <= (created_at + '90 days'::interval)))"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_kind_check", None),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_owner_id_check",
                  "CHECK (((owner_id)::text ~ '^[0-9]+$'::text))"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_revoked_check",
                  "CHECK (((revoked_at IS NULL) OR (revoked_at >= created_at)))"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_scopes_check",
                  "CHECK (((scopes)::text = 'merchant_rules:read own_proposals:read "
                  "statement:propose'::text))"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_secret_hash_check",
                  "CHECK ((secret_hash ~ '^[0-9a-f]{64}$'::text))"),
    ConstraintPin("os_assistant_credentials", "os_assistant_credentials_use_count_check",
                  "CHECK ((use_count >= 0))"),
    # os_assistant_audit
    ConstraintPin("os_assistant_audit", "os_assistant_audit_pkey",
                  "PRIMARY KEY (workspace_id, audit_id)"),
    ConstraintPin("os_assistant_audit", "os_assistant_audit_workspace_fkey",
                  "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"),
    ConstraintPin("os_assistant_audit", "os_assistant_audit_bytes_check", "CHECK ((bytes >= 0))"),
    ConstraintPin("os_assistant_audit", "os_assistant_audit_client_ip_hmac_check",
                  "CHECK ((client_ip_hmac ~ '^[0-9a-f]{64}$'::text))"),
    ConstraintPin("os_assistant_audit", "os_assistant_audit_credential_id_check",
                  "CHECK (((credential_id)::text ~ '^AC_[0-9a-f]{32}$'::text))"),
    ConstraintPin("os_assistant_audit", "os_assistant_audit_line_count_check",
                  "CHECK ((line_count >= 0))"),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_approval_check',
        "CHECK ((approval_deadline = (created_at + '00:10:00'::interval)))"
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_approved_check',
        "CHECK (((status <> ALL (ARRAY['approved'::text, 'collected'::text]))"
        ' OR (approved_at IS NOT NULL)))'
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_binding_check',
        "CHECK ((session_binding ~ '^[0-9a-f]{64}$'::text))"
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_collected_check',
        "CHECK (((status = 'collected'::text) = ((collected_at IS NOT NULL) A"
        'ND (credential_id IS NOT NULL))))'
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_days_check',
        'CHECK (((expires_in_days >= 1) AND (expires_in_days <= 90)))'
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_epoch_check',
        'CHECK ((epoch > 0))'
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_id_check',
        "CHECK ((issue_request_id ~ '^IR_[0-9a-f]{32}$'::text))"
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_key_fkey',
        'FOREIGN KEY (workspace_id, credential_id) REFERENCES assaf_os.os_ass'
        'istant_credentials(workspace_id, credential_id)'
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_kind_check',
        "CHECK ((kind = ANY (ARRAY['claude'::text, 'gpt'::text])))"
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_pkey',
        'PRIMARY KEY (workspace_id, issue_request_id)'
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_status_check',
        "CHECK ((status = ANY (ARRAY['pending_approval'::text, 'approved'::te"
        "xt, 'rejected'::text, 'expired'::text, 'collected'::text, 'cancelled'::text])))"
    ),
    ConstraintPin(
        "os_assistant_issue_requests", 'os_issue_requests_workspace_fkey',
        'FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspa'
        'ce_id)'
    ),
    # schema_migrations
    ConstraintPin("schema_migrations", "schema_migrations_pkey", "PRIMARY KEY (filename)"),
)

# The indexes that back no PRIMARY KEY or UNIQUE constraint (those are the pins above), by
# pg_get_indexdef. The expression index is the exactly-once key for expenses (V2-14). The
# audit and proposals indexes and the partial unique index of statement lines are V2-14b's.
INDEX_PINS = frozenset({
    "CREATE INDEX os_assistant_audit_workspace_ip_occurred_idx ON assaf_os.os_assistant_audit "
    "USING btree (workspace_id, client_ip_hmac, occurred_at)",
    "CREATE INDEX os_assistant_audit_workspace_operation_occurred_idx "
    "ON assaf_os.os_assistant_audit "
    "USING btree (workspace_id, operation, occurred_at)",
    "CREATE INDEX os_assistant_audit_credential_occurred_idx ON assaf_os.os_assistant_audit "
    "USING btree (credential_id, occurred_at)",
    "CREATE INDEX os_clients_workspace_business_date_idx ON assaf_os.os_clients USING btree "
    "(workspace_id, business_date)",
    "CREATE UNIQUE INDEX os_expenses_source_key_idx ON assaf_os.os_expenses USING btree "
    "(workspace_id, source_type, assaf_os.expense_source_key_sha256((source_key)::text))",
    "CREATE INDEX os_expenses_workspace_business_date_idx ON assaf_os.os_expenses USING btree "
    "(workspace_id, business_date)",
    "CREATE INDEX os_leads_workspace_business_date_idx ON assaf_os.os_leads USING btree "
    "(workspace_id, business_date)",
    "CREATE INDEX os_outbox_delivery_idx ON assaf_os.os_outbox USING btree (status, "
    "next_attempt_at, workspace_id, commit_version)",
    "CREATE INDEX os_payments_workspace_business_date_idx ON assaf_os.os_payments USING btree "
    "(workspace_id, business_date)",
    "CREATE INDEX os_proposals_workspace_status_kind_created_idx ON assaf_os.os_proposals "
    "USING btree (workspace_id, status, kind, created_at)",
    "CREATE UNIQUE INDEX os_statement_lines_matched_expense_key ON assaf_os.os_statement_lines "
    "USING btree (workspace_id, matched_expense_id) WHERE (((state)::text = 'active'::text) "
    "AND (matched_expense_id IS NOT NULL))",
    "CREATE INDEX os_tasks_workspace_business_date_idx ON assaf_os.os_tasks USING btree "
    "(workspace_id, business_date)",
    "CREATE INDEX os_tasks_workspace_status_due_date_idx ON assaf_os.os_tasks USING btree "
    "(workspace_id, status, due_date)",
    "CREATE INDEX os_time_workspace_business_date_idx ON assaf_os.os_time USING btree "
    "(workspace_id, business_date)",
    "CREATE INDEX os_works_workspace_business_date_idx ON assaf_os.os_works USING btree "
    "(workspace_id, business_date)",
})

# Predefined roles whose members reach assaf_os without any ACL, ownership or pg_depend
# edge: row access (read and write all data, maintain from PostgreSQL 17) and the file and
# program roles that read or overwrite the data files or run as the server's OS user. A
# role that is absent on this server version (pg_maintain on 16) matches nothing.
_PRIVILEGED_PREDEFINED = (
    "pg_read_all_data", "pg_write_all_data", "pg_maintain",
    "pg_read_server_files", "pg_write_server_files", "pg_execute_server_program",
)
_USER_SCHEMA = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'"
)
# Every catalog object that belongs to assaf_os, as (catalog oid, object oid): the schema,
# its relations with their toast tables and toast indexes, functions, types,
# constraints, triggers, rules, column defaults, policies and extended statistics.
_INSIDE = """
WITH ns AS (SELECT oid FROM pg_namespace WHERE nspname = 'assaf_os'),
rel AS (SELECT c.oid, c.reltoastrelid FROM pg_class c WHERE c.relnamespace IN (SELECT oid FROM ns)),
allrel AS (
    SELECT oid FROM rel
    UNION SELECT reltoastrelid FROM rel WHERE reltoastrelid <> 0
    UNION SELECT i.indexrelid FROM pg_index i
          WHERE i.indrelid IN (SELECT reltoastrelid FROM rel WHERE reltoastrelid <> 0)),
inside(classid, objid) AS (
    SELECT 'pg_namespace'::regclass::oid, oid FROM ns
    UNION ALL SELECT 'pg_class'::regclass::oid, oid FROM allrel
    UNION ALL SELECT 'pg_proc'::regclass::oid, p.oid FROM pg_proc p
              WHERE p.pronamespace IN (SELECT oid FROM ns)
    UNION ALL SELECT 'pg_type'::regclass::oid, t.oid FROM pg_type t
              WHERE t.typnamespace IN (SELECT oid FROM ns)
    UNION ALL SELECT 'pg_constraint'::regclass::oid, k.oid FROM pg_constraint k
              WHERE k.connamespace IN (SELECT oid FROM ns)
    UNION ALL SELECT 'pg_trigger'::regclass::oid, g.oid FROM pg_trigger g
              WHERE g.tgrelid IN (SELECT oid FROM allrel)
    UNION ALL SELECT 'pg_rewrite'::regclass::oid, w.oid FROM pg_rewrite w
              WHERE w.ev_class IN (SELECT oid FROM allrel)
    UNION ALL SELECT 'pg_attrdef'::regclass::oid, a.oid FROM pg_attrdef a
              WHERE a.adrelid IN (SELECT oid FROM allrel)
    UNION ALL SELECT 'pg_policy'::regclass::oid, y.oid FROM pg_policy y
              WHERE y.polrelid IN (SELECT oid FROM allrel)
    UNION ALL SELECT 'pg_statistic_ext'::regclass::oid, s.oid FROM pg_statistic_ext s
              WHERE s.stxnamespace IN (SELECT oid FROM ns))
"""
# initdb-created objects (OID < FirstNormalObjectId, e.g. the plpgsql language) are
# the only things outside assaf_os that an assaf_os object may depend on.
_FIRST_NORMAL_OID = 16384

# --- V2-14b extension (V2_PG_CONTRACTS.md section 18.2) --------------------------------
# Kept apart from the V2-15 map above. Literal tuples only; the policy tests pin each one
# to the migration SQL's columns. TABLE_PRIVILEGES already gives every V2-14b table
# SELECT and INSERT, because os_ledger_migrate.TABLES lists them. Never updatable:
# os_workspaces.epoch and statement_pepper_fp, os_receipts.reason_codes, and every
# os_assistant_audit column.
ADDENDUM_COLUMN_UPDATES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "os_workspaces": ("pending_seq",),
    "os_statements": (
        "status", "line_count", "lines_digest", "current_proposal_id", "approved_proposal_id",
        "imported_version", "updated_at",
    ),
    # Every column except workspace_id, statement_id, line_key and created_at.
    "os_statement_lines": (
        "tag", "state", "txn_date", "merchant_display", "merchant_key", "amount_minor", "kind",
        "currency", "original_amount_minor", "original_currency", "installment_n",
        "installment_of", "category", "path", "platform", "is_subscription", "rule_id",
        "assistant_tag", "owner_override", "expense_id", "matched_expense_id",
        "expense_revision_at_import", "revision", "updated_at",
    ),
    # Every column except workspace_id, rule_id and created_at.
    "os_merchant_rules": (
        "match", "tag", "merchant_key", "merchant_key_hmac", "category", "path", "platform",
        "is_subscription", "label", "aliases", "active", "revision", "source_proposal_id",
        "updated_at",
    ),
    "os_statement_review_drafts": ("draft_revision", "overrides", "choices", "updated_at"),
    "os_statement_batch_lines": ("detail", "purged_at"),
    "os_assistant_credentials": ("revoked_at", "last_used_at", "use_count"),
    "os_assistant_issue_requests": ("status", "approved_at", "collected_at", "credential_id"),
})
# Retention (LOGIN NOINHERIT): schema USAGE plus exactly these. The os-retention job uses
# DELETE only for purged batch lines, batch lines whose proposal expired more than a day
# ago (the I11 backstop, whatever purged_at says), and audit rows older than 180 days.
RETENTION_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType({
    "os_assistant_audit": frozenset({"SELECT", "DELETE"}),
    "os_statement_batch_lines": frozenset({"SELECT", "DELETE"}),
})
RETENTION_COLUMN_SELECTS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "os_proposals": ("workspace_id", "proposal_id", "kind", "status", "expires_at"),
})
# §22 E6: the database cannot enforce retention's row predicates (no RLS), so the
# os-retention job runs exactly these fixed statements and no other SQL as the retention
# role. The only bound parameter is :workspace_id; the windows are literals, so no caller
# can widen them. V2-26's app/workers/os_retention.py must execute these constants.
RETENTION_DELETE_SQL: Mapping[str, str] = MappingProxyType({
    # Batch lines whose detail the runtime path already purged.
    "purge": (
        "DELETE FROM assaf_os.os_statement_batch_lines "
        "WHERE workspace_id = :workspace_id AND purged_at IS NOT NULL"
    ),
    # The I11 backstop: lines of a proposal that expired more than a day ago, whatever
    # purged_at says.
    "backstop": (
        "DELETE FROM assaf_os.os_statement_batch_lines AS b USING assaf_os.os_proposals AS p "
        "WHERE b.workspace_id = :workspace_id AND p.workspace_id = b.workspace_id "
        "AND p.proposal_id = b.proposal_id AND p.expires_at < now() - interval '1 day'"
    ),
    # Assistant audit rows are kept for 180 days (§17.2).
    "audit": (
        "DELETE FROM assaf_os.os_assistant_audit "
        "WHERE workspace_id = :workspace_id AND occurred_at < now() - interval '180 days'"
    ),
})


def addendum_grant_statements() -> tuple[str, ...]:
    """V2-14b's GRANTs, appended to ledger_grant_statements() after V2-15's."""
    statements = [
        f"GRANT UPDATE ({', '.join(columns)}) ON TABLE assaf_os.{name} TO assaf_os_ledger;"
        for name, columns in ADDENDUM_COLUMN_UPDATES.items()
    ]
    statements.append("GRANT USAGE ON SCHEMA assaf_os TO assaf_os_retention;")
    for name, privileges in RETENTION_TABLE_PRIVILEGES.items():
        statements.append(
            f"GRANT {', '.join(sorted(privileges))} ON TABLE assaf_os.{name} "
            "TO assaf_os_retention;"
        )
    for name, columns in RETENTION_COLUMN_SELECTS.items():
        statements.append(
            f"GRANT SELECT ({', '.join(columns)}) ON TABLE assaf_os.{name} TO assaf_os_retention;"
        )
    return tuple(statements)


def _addendum_acl() -> tuple[set[tuple], set[tuple], set[tuple]]:
    """Expected V2-14b schema, table and column ACL entries (no grant options)."""
    schema = {(RETENTION_ROLE, "USAGE", False)}
    tables = {
        (name, RETENTION_ROLE, privilege, False)
        for name, privileges in RETENTION_TABLE_PRIVILEGES.items() for privilege in privileges
    }
    columns = {
        (name, column, RUNTIME_ROLE, "UPDATE", False)
        for name, names in ADDENDUM_COLUMN_UPDATES.items() for column in names
    } | {
        (name, column, RETENTION_ROLE, "SELECT", False)
        for name, names in RETENTION_COLUMN_SELECTS.items() for column in names
    }
    return schema, tables, columns


def _retention_effective(conn: Connection) -> None:
    # Effective, not only ACL-level: PUBLIC or any other path into assaf_os also fails.
    tables = conn.execute(text("""
        SELECT c.relname, p.privilege FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',
                                'REFERENCES','TRIGGER']) AS p(privilege)
        WHERE n.nspname='assaf_os' AND c.relkind IN ('r','p','v','m','f')
        AND has_table_privilege(:role, c.oid, p.privilege)
    """), {"role": RETENTION_ROLE}).all()
    _require({tuple(row) for row in tables} == {
        (name, privilege)
        for name, privileges in RETENTION_TABLE_PRIVILEGES.items() for privilege in privileges
    })
    columns = conn.execute(text("""
        SELECT c.relname, a.attname, p.privilege FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum > 0 AND NOT a.attisdropped
        CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) AS p(privilege)
        WHERE n.nspname='assaf_os' AND c.relkind IN ('r','p','v','m','f')
        AND has_column_privilege(:role, c.oid, a.attnum, p.privilege)
    """), {"role": RETENTION_ROLE}).all()
    _require({tuple(row) for row in columns if row[0] not in RETENTION_TABLE_PRIVILEGES} == {
        (name, column, "SELECT")
        for name, names in RETENTION_COLUMN_SELECTS.items() for column in names
    })
    _require({row[2] for row in columns if row[0] in RETENTION_TABLE_PRIVILEGES} <= {"SELECT"})
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='assaf_os' AND CASE WHEN c.relkind='S'
          THEN has_sequence_privilege(:role, c.oid, 'USAGE,SELECT,UPDATE') ELSE false END)
        OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='assaf_os' AND has_function_privilege(:role, p.oid, 'EXECUTE'))
        OR NOT has_schema_privilege(:role, 'assaf_os', 'USAGE')
    """), {"role": RETENTION_ROLE}))


def ledger_grant_statements() -> tuple[str, ...]:
    """Return the complete, deterministic runtime GRANT set (no dynamic SQL names)."""
    statements = ["GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger;"]
    for name, privileges in TABLE_PRIVILEGES.items():
        statements.append(
            f"GRANT {', '.join(sorted(privileges))} ON TABLE assaf_os.{name} TO assaf_os_ledger;"
        )
    for name, columns in COLUMN_UPDATES.items():
        statements.append(
            f"GRANT UPDATE ({', '.join(columns)}) ON TABLE assaf_os.{name} TO assaf_os_ledger;"
        )
    statements.append(
        "GRANT EXECUTE ON FUNCTION assaf_os.expense_source_key_sha256(text) TO assaf_os_ledger;"
    )
    statements.extend(addendum_grant_statements())  # V2-14b
    return tuple(statements)


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError(DENIED)


def _migration_role(conn: Connection, explicit: str | None) -> str:
    # §5.5: the migration identity is the single member holding an ADMIN-only edge
    # (ADMIN TRUE, SET FALSE, INHERIT FALSE) on assaf_os_owner. Zero or several such
    # members, or an explicit pin that differs from it, are refused.
    members = set(conn.execute(text(
        "SELECT DISTINCT pg_get_userbyid(member) FROM pg_auth_members "
        "WHERE roleid = to_regrole(:owner) AND admin_option AND NOT set_option "
        "AND NOT inherit_option"
    ), {"owner": OWNER_ROLE}).scalars())
    _require(len(members) == 1 and (explicit is None or members == {explicit}))
    return members.pop()


def _roles(conn: Connection, main_role: str, migration_role: str | None) -> None:
    migration_role = _migration_role(conn, migration_role)
    _require(bool(main_role) and bool(migration_role) and len({
        RUNTIME_ROLE, OWNER_ROLE, RETENTION_ROLE, main_role, migration_role,
    }) == 5)
    # V2-14b: retention is LOGIN NOINHERIT (section 18.2).
    for role, login in ((RUNTIME_ROLE, True), (OWNER_ROLE, False), (RETENTION_ROLE, True)):
        row = conn.execute(text(
            "SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=:role"
        ), {"role": role}).first()
        _require(row is not None and row[0] == login and not any(row[1:]))
    # Match V2-14 review r1: the owner may ONLY have the ADMIN-only edge to the
    # migration identity. NOINHERIT alone never prevents SET ROLE.
    edges = conn.execute(text(
        "SELECT pg_get_userbyid(roleid), pg_get_userbyid(member), admin_option, "
        "set_option, inherit_option FROM pg_auth_members WHERE "
        "roleid = ANY(SELECT oid FROM pg_roles WHERE rolname = ANY(:roles)) OR "
        "member = ANY(SELECT oid FROM pg_roles WHERE rolname = ANY(:roles))"
    ), {"roles": [RUNTIME_ROLE, OWNER_ROLE, RETENTION_ROLE, main_role]}).all()
    _require(all(tuple(edge) == (OWNER_ROLE, migration_role, True, False, False) for edge in edges))
    _validate_main_role_catalog(conn, main_role)
    # The main role has no TEMP: no temporary relation of its own on any connection.
    _require(not conn.scalar(text(
        "SELECT has_database_privilege(:role, current_database(), 'TEMP')"
    ), {"role": main_role}))
    for role in (RUNTIME_ROLE, RETENTION_ROLE):
        _require(not conn.scalar(text(
            "SELECT has_database_privilege(:role,current_database(),'CREATE,TEMP') OR EXISTS ("
            "SELECT 1 FROM pg_class WHERE relowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_namespace WHERE nspowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_proc WHERE proowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_database WHERE datdba=to_regrole(:role)) OR EXISTS ("
            "SELECT 1 FROM pg_namespace WHERE has_schema_privilege(:role,oid,'CREATE'))"
        ), {"role": role}))
        # Exactly one database ACL entry is allowed: the runtime role's own CONNECT
        # on this database (needed when PUBLIC CONNECT is revoked).
        _require(not conn.scalar(text("""
            SELECT EXISTS(SELECT 1 FROM pg_namespace n
            CROSS JOIN LATERAL aclexplode(n.nspacl) a
            WHERE a.grantee=to_regrole(:role) AND n.nspname<>'assaf_os')
            OR EXISTS(SELECT 1 FROM pg_database d
            CROSS JOIN LATERAL aclexplode(d.datacl) a
            WHERE d.datname=current_database() AND a.grantee=to_regrole(:role)
            AND NOT (CAST(:connect AS boolean) AND a.privilege_type = 'CONNECT'
                     AND NOT a.is_grantable))
        """), {"role": role, "connect": role == RUNTIME_ROLE}))
        _require(not conn.scalar(text(f"""
            SELECT EXISTS(SELECT 1 FROM pg_namespace n WHERE {_USER_SCHEMA}
            AND n.nspname NOT IN ('assaf_os','public')
            AND has_schema_privilege(:role,n.oid,'USAGE'))
        """), {"role": role}))


def _acl(conn: Connection, sql: str, expected: set[tuple]) -> None:
    # A non-owner ACL entry must match exactly, including its grant-option bit.
    # PUBLIC (OID zero) and arbitrary third-party grants are therefore rejected.
    rows = conn.execute(text(sql)).all()
    _require({tuple(row) for row in rows} == expected)


def _closure(conn: Connection) -> None:
    # r1 must-fix 1: a rule or trigger turns one exact-grant INSERT into anything its
    # definer may do. No rule; exactly the reviewed trigger, enabled; no RLS policies.
    _require(not conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class "
        "WHERE c.relnamespace=to_regnamespace('assaf_os'))"
    )))
    # The last column is false for a WHEN clause (tgqual), trigger arguments or a column
    # list: re-creating the trigger under the same name with WHEN (false) keeps every
    # other column here unchanged and silently drops the guard.
    triggers = conn.execute(text(
        "SELECT c.relname, t.tgname, t.tgfoid::regprocedure::text, t.tgenabled::text, "
        "t.tgtype::int, (t.tgqual IS NULL AND t.tgnargs = 0 AND t.tgattr = ''::int2vector) "
        "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "WHERE c.relnamespace=to_regnamespace('assaf_os') AND NOT t.tgisinternal"
    )).all()
    _require(len(triggers) == len(TRIGGERS) and all(row[5] is True for row in triggers))
    _require({tuple(row[:5]) for row in triggers} == TRIGGERS)
    _require(not conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_class c WHERE c.relnamespace=to_regnamespace('assaf_os') "
        "AND (c.relrowsecurity OR c.relforcerowsecurity)) OR EXISTS(SELECT 1 FROM pg_policy p "
        "JOIN pg_class c ON c.oid=p.polrelid WHERE c.relnamespace=to_regnamespace('assaf_os'))"
    )))
    # r1 must-fix 2: assaf_os is a closed dependency island. Nothing outside depends on
    # it (views, matviews, sql-body functions, foreign keys, row types, statistics,
    # publications), and it depends on nothing outside except initdb objects
    # (triggers, defaults, checks or indexes calling another schema's functions).
    _require(not conn.scalar(text(_INSIDE + f"""
        SELECT EXISTS(SELECT 1 FROM pg_depend d
        WHERE ((d.classid, d.objid) IN (SELECT classid, objid FROM inside))
              <> ((d.refclassid, d.refobjid) IN (SELECT classid, objid FROM inside))
          AND NOT ((d.classid, d.objid) IN (SELECT classid, objid FROM inside)
                   AND d.refobjid < {_FIRST_NORMAL_OID}::oid))
    """)))


def _referential_integrity(conn: Connection) -> None:
    # Review of #170 r3 (round 2), major. A foreign key is enforced by internal (RI)
    # triggers, and a referential action (ON UPDATE or ON DELETE CASCADE, SET NULL, SET
    # DEFAULT) runs as the OWNER of the referencing table, not as the session that fired it.
    # One owner-level `ADD CONSTRAINT ... ON UPDATE CASCADE` therefore turns the
    # §18.2-permitted UPDATE of os_leads.person_id into a rewrite of os_receipts.proposal_id,
    # which the runtime role may not UPDATE, and an ON DELETE CASCADE edge would let a
    # retention DELETE erase history. Every foreign key must be NO ACTION, MATCH SIMPLE,
    # immediate and validated, whatever CONSTRAINT_PINS says, so that a later migration's new
    # foreign key meets the same rule.
    _require(not conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
        "WHERE c.relnamespace = to_regnamespace('assaf_os') AND k.contype = 'f' AND NOT ("
        "k.confupdtype = 'a' AND k.confdeltype = 'a' AND k.confmatchtype = 's' "
        "AND NOT k.condeferrable AND NOT k.condeferred AND k.convalidated))"
    )))
    # Disabling or re-modeling the internal triggers (ALTER TABLE ... DISABLE TRIGGER, which
    # needs a superuser) is the same bypass r1 refused through session_replication_role:
    # the runtime role could then insert an event with no receipt. They must all fire in
    # every session (origin), which is tgenabled = 'O'.
    _require(not conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE c.relnamespace = to_regnamespace('assaf_os') AND t.tgisinternal "
        "AND t.tgenabled <> 'O')"
    )))


def _integrity(conn: Connection) -> None:
    _referential_integrity(conn)
    # An UNLOGGED table is truncated by crash recovery, which silently loses the append-only
    # events and receipts and the outbox that exactly-once depends on, whatever fsync and
    # synchronous_commit say. Every relation, indexes included, is permanent.
    _require(not conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_class c WHERE c.relnamespace = to_regnamespace('assaf_os') "
        "AND c.relpersistence <> 'p')"
    )))
    # The constraint inventory is exact, and every constraint is validated (a NOT VALID one
    # is not checked against the rows already there, including a name-only CHECK). The LEFT
    # JOIN keeps a constraint with no table, such as a domain's CHECK in assaf_os, in the
    # comparison, where it matches no pin.
    constraints = conn.execute(text(
        "SELECT COALESCE(c.relname, ''), k.conname, pg_get_constraintdef(k.oid), k.convalidated "
        "FROM pg_constraint k LEFT JOIN pg_class c ON c.oid = k.conrelid "
        "WHERE k.connamespace = to_regnamespace('assaf_os') "
        "OR c.relnamespace = to_regnamespace('assaf_os')"
    )).all()
    live = {(row[0], row[1]): row[2] for row in constraints}
    pinned = {(pin.relation, pin.name): pin.definition for pin in CONSTRAINT_PINS}
    _require(all(row[3] is True for row in constraints)
             and len(constraints) == len(live) == len(CONSTRAINT_PINS) == len(pinned)
             and live.keys() == pinned.keys()
             and all(want is None or live[key] == want for key, want in pinned.items()))
    # Every index is usable (a failed concurrent build leaves an invalid one), and the ones
    # that back no PRIMARY KEY or UNIQUE constraint are exactly the pinned set.
    indexes = conn.execute(text(
        "SELECT pg_get_indexdef(i.indexrelid), EXISTS(SELECT 1 FROM pg_constraint k "
        "WHERE k.conindid = i.indexrelid AND k.conrelid = i.indrelid "
        "AND k.contype IN ('p', 'u', 'x')), i.indisvalid AND i.indisready AND i.indislive "
        "FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
        "WHERE x.relnamespace = to_regnamespace('assaf_os')"
    )).all()
    _require(all(row[2] is True for row in indexes))
    unbacked = [row[0] for row in indexes if row[1] is False]
    _require(len(unbacked) == len(INDEX_PINS) and set(unbacked) == INDEX_PINS)


def _footprints(conn: Connection, main_role: str) -> None:
    params = {
        "owner": OWNER_ROLE, "runtime": RUNTIME_ROLE, "retention": RETENTION_ROLE,
        "main": main_role,
    }
    # r1 must-fix 2: the owner owns nothing in this database outside assaf_os (other
    # than its own default ACL, checked in _defaults) and no shared object.
    _require(not conn.scalar(text(_INSIDE + """
        SELECT EXISTS(SELECT 1 FROM pg_shdepend s
        WHERE s.refclassid = 'pg_authid'::regclass AND s.refobjid = to_regrole(:owner)
          AND s.deptype = 'o'
          AND (s.dbid = 0 OR (
               s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
               AND (s.classid, s.objid) NOT IN (SELECT classid, objid FROM inside)
               AND NOT (s.classid = 'pg_default_acl'::regclass AND s.objid IN (
                   SELECT oid FROM pg_default_acl WHERE defaclrole = to_regrole(:owner))))))
    """), params))
    # The runtime role appears in no ACL and owns nothing in ANY database or shared
    # object (tablespaces, other databases, parameters, large objects, types, ...),
    # except assaf_os objects (pinned exactly by _catalog) and CONNECT on this database.
    # V2-14b: retention appears only as the grantee of its exact §18.2 ACL entries in
    # this database (the assaf_os schema, its two tables, the five os_proposals
    # columns); never as an owner, on a shared object or in another database.
    retention = {
        "retention_tables": sorted(RETENTION_TABLE_PRIVILEGES),
        "column_tables": [t for t, cs in RETENTION_COLUMN_SELECTS.items() for _ in cs],
        "column_names": [c for cs in RETENTION_COLUMN_SELECTS.values() for c in cs],
    }
    _require(not conn.scalar(text(_INSIDE + """,
        retention_acl(classid, objid, objsubid) AS (
            SELECT 'pg_namespace'::regclass::oid, oid, 0 FROM ns
            UNION ALL SELECT 'pg_class'::regclass::oid, c.oid, 0 FROM pg_class c
                WHERE c.relnamespace IN (SELECT oid FROM ns)
                  AND c.relname = ANY(CAST(:retention_tables AS text[]))
            UNION ALL SELECT 'pg_class'::regclass::oid, c.oid, a.attnum::int
                FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
                JOIN unnest(CAST(:column_tables AS text[]), CAST(:column_names AS text[]))
                     AS g(relname, attname)
                  ON g.relname = c.relname AND g.attname = a.attname
                WHERE c.relnamespace IN (SELECT oid FROM ns))
        SELECT EXISTS(SELECT 1 FROM pg_shdepend s
        WHERE s.refclassid = 'pg_authid'::regclass
          AND ((s.refobjid = to_regrole(:retention) AND NOT (s.deptype = 'a'
                 AND s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                 AND (s.classid, s.objid, s.objsubid)
                     IN (SELECT classid, objid, objsubid FROM retention_acl)))
           OR (s.refobjid = to_regrole(:runtime) AND NOT (s.deptype = 'a' AND (
                 (s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                  AND s.classid IN ('pg_namespace'::regclass, 'pg_class'::regclass,
                                    'pg_proc'::regclass)
                  AND (s.classid, s.objid) IN (SELECT classid, objid FROM inside))
                 OR (s.dbid = 0 AND s.classid = 'pg_database'::regclass
                     AND s.objid = (SELECT oid FROM pg_database
                                    WHERE datname = current_database())))))))
    """), params | retention))
    # r1 must-fix 3: SET / ALTER SYSTEM on a parameter (session_replication_role turns
    # off foreign keys and the assistant trigger) is granted to none of these or PUBLIC.
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_parameter_acl p CROSS JOIN LATERAL aclexplode(p.paracl) a
        WHERE a.grantee = 0 OR a.grantee IN (to_regrole(:runtime), to_regrole(:retention),
                                             to_regrole(:owner), to_regrole(:main)))
    """), params))
    # No per-role settings for the ledger roles, and no database- or cluster-wide
    # session_replication_role that would apply to the runtime role's sessions.
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_db_role_setting s
        WHERE s.setrole IN (to_regrole(:runtime), to_regrole(:retention), to_regrole(:owner))
           OR (s.setrole = 0
               AND s.setdatabase IN (0, (SELECT oid FROM pg_database
                                         WHERE datname = current_database()))
               AND EXISTS(SELECT 1 FROM unnest(s.setconfig) c
                          WHERE lower(split_part(c, '=', 1)) = 'session_replication_role')))
    """), params))
    # A user-created SECURITY DEFINER routine owned by a superuser or the owner is a
    # write path into assaf_os whatever its body. EXECUTE is not the boundary: a
    # trigger on a main-owned table or an event trigger invokes it without that check.
    _require(not conn.scalar(text(f"""
        SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
        WHERE p.prosecdef AND p.oid >= {_FIRST_NORMAL_OID}::oid
          AND (r.rolsuper OR r.rolname = :owner))
    """), params))
    # The same holds for ANY other owner that can reach assaf_os, and the list of such
    # owners is not just superusers and the schema owner. The predefined data roles
    # (pg_write_all_data, pg_read_all_data, ...) grant row access with no ACL entry, no
    # ownership and no pg_depend edge, so none of the checks above sees them. Two rules:
    #  (a) no ordinary role anywhere in the cluster is a member of one of them, with or
    #      without INHERIT (SET ROLE is enough); a LOGIN member would be a second identity
    #      with read or write access to every ledger row;
    #  (b) a SECURITY DEFINER routine's non-superuser owner holds no privilege on any
    #      assaf_os relation, however it got it. (b) also covers a predefined role added
    #      to a later PostgreSQL version that (a) does not yet list.
    _require(not conn.scalar(text(f"""
        SELECT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_roles g ON g.rolname = ANY(:privileged)
        WHERE r.oid >= {_FIRST_NORMAL_OID}::oid AND NOT r.rolsuper
          AND pg_has_role(r.oid, g.oid, 'MEMBER'))
    """), {**params, "privileged": list(_PRIVILEGED_PREDEFINED)}))
    _require(not conn.scalar(text(f"""
        SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
        WHERE p.prosecdef AND p.oid >= {_FIRST_NORMAL_OID}::oid
          AND NOT r.rolsuper AND r.rolname <> :owner
          AND EXISTS(SELECT 1 FROM pg_class c
                     WHERE c.relnamespace = to_regnamespace('assaf_os')
                       AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                       AND (has_table_privilege(r.oid, c.oid,
                              'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                            OR has_any_column_privilege(r.oid, c.oid,
                              'SELECT,INSERT,UPDATE,REFERENCES'))))
    """), params))


def _catalog(conn: Connection, main_role: str, migration_role: str | None) -> None:
    _require(conn.dialect.name == "postgresql")
    conn.execute(text("SET LOCAL statement_timeout = '5s'"))
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(text(CATALOG_SEARCH_PATH))
    _roles(conn, main_role, migration_role)
    _require(conn.scalar(text(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='assaf_os'"
    )) == OWNER_ROLE)
    relations = conn.execute(text(
        "SELECT c.relname,c.relkind,pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='assaf_os' "
        "AND c.relkind NOT IN ('i','I','t')"
    )).all()
    _require({tuple(row) for row in relations} == {
        (name, "r", OWNER_ROLE) for name in OWNED_TABLES
    })
    functions = conn.execute(text(
        "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', "
        "pg_get_userbyid(p.proowner), p.prosecdef, l.lanname, p.provolatile::text, "
        "p.proisstrict, p.prorettype::regtype::text, p.proconfig, p.prosrc "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='assaf_os'"
    )).all()
    # identity_arguments includes the declared argument name in this SQL helper.
    _require({
        (name.replace('value text', 'text'), owner, security,
         FunctionPin(language, volatility, strict, returns,
                     None if config is None else tuple(config), source))
        for name, owner, security, language, volatility, strict, returns, config, source
        in functions
    } == {(name, OWNER_ROLE, False, pin) for name, pin in FUNCTION_PINS.items()})
    addendum_schema, addendum_tables, addendum_columns = _addendum_acl()  # V2-14b
    _acl(conn, """
        SELECT pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_namespace n CROSS JOIN LATERAL
        aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>n.nspowner
    """, {(RUNTIME_ROLE, "USAGE", False)} | addendum_schema)
    _acl(conn, """
        SELECT c.relname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault(
            CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,c.relowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>c.relowner
    """, {(name, RUNTIME_ROLE, privilege, False)
          for name, privileges in TABLE_PRIVILEGES.items() for privilege in privileges}
        | addendum_tables)
    _acl(conn, """
        SELECT c.relname,col.attname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute col ON col.attrelid=c.oid
        CROSS JOIN LATERAL aclexplode(col.attacl) a
        WHERE n.nspname='assaf_os' AND a.grantee<>c.relowner
    """, {(name, col, RUNTIME_ROLE, "UPDATE", False)
          for name, columns in COLUMN_UPDATES.items() for col in columns}
        | addendum_columns)
    _acl(conn, """
        SELECT p.proname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>p.proowner
    """, {("expense_source_key_sha256", RUNTIME_ROLE, "EXECUTE", False)})
    # Reject ambient PUBLIC privileges and explicit grants in ANY user schema,
    # even without schema USAGE. V2-14b: inside assaf_os, retention's effective
    # privileges are pinned exactly by _retention_effective below.
    for role in (RUNTIME_ROLE, RETENTION_ROLE):
        _require(not conn.scalar(text(f"""
            SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {_USER_SCHEMA} AND n.nspname<>'assaf_os' AND CASE
              WHEN c.relkind='S' THEN has_sequence_privilege(:role,c.oid,'USAGE,SELECT,UPDATE')
              WHEN c.relkind IN ('r','p','v','m','f') THEN
                has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
              ELSE false END)
            OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE {_USER_SCHEMA} AND n.nspname<>'assaf_os'
            AND has_function_privilege(:role,p.oid,'EXECUTE'))
        """), {"role": role}))
    _retention_effective(conn)  # V2-14b
    _closure(conn)
    _integrity(conn)
    _footprints(conn, main_role)
    _defaults(conn, main_role)


def _defaults(conn: Connection, main_role: str) -> None:
    # Absence of a global owner function ACL restores PostgreSQL's implicit
    # PUBLIC EXECUTE. A schema-local REVOKE cannot cancel that default.
    _require(conn.scalar(text("""
        SELECT NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(
          (SELECT defaclacl FROM pg_default_acl WHERE defaclrole='assaf_os_owner'::regrole
           AND defaclnamespace=0 AND defaclobjtype='f'),
          acldefault('f','assaf_os_owner'::regrole))) a
          WHERE a.grantee<>'assaf_os_owner'::regrole)
    """)))
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_default_acl d
        CROSS JOIN LATERAL aclexplode(d.defaclacl) a WHERE
          (d.defaclnamespace=(SELECT oid FROM pg_namespace WHERE nspname='assaf_os')
           AND a.grantee<>d.defaclrole)
          OR (d.defaclrole='assaf_os_owner'::regrole AND a.grantee<>d.defaclrole)
          OR a.grantee IN (0,'assaf_os_ledger'::regrole,'assaf_os_retention'::regrole)
          OR (d.defaclnamespace=0 AND a.grantee=to_regrole(:main)
              AND d.defaclrole<>a.grantee))
    """), {"main": main_role}))


def validate_ledger_catalog(
    conn: Connection, *, main_role: str, migration_role: str | None = None,
) -> None:
    """Operator-only transactional audit; raises ValueError('ledger_role_denied').

    main_role must come from validate_main_role on the actual main DSN, never a
    caller-supplied replacement. migration_role=None derives it (§5.5); an explicit
    value must equal the derived one. Runtime consumers use validate_ledger_role.
    This function does not commit and cannot prove a supplied role is a real DSN.
    It sets transaction-local timeouts and a catalog-first search_path.
    """
    try:
        _catalog(conn, main_role, migration_role)
    except (ValueError, SQLAlchemyError, TypeError, AttributeError, KeyError):
        raise ValueError(DENIED) from None


def validate_ledger_role(
    db: Session, main_engine: Engine, *, migration_role: str | None = None,
) -> None:
    """§5.5 runtime/authority preflight; audit both actual DSNs, without caching.

    A preflight, not a per-request check (0.1-0.4 s of catalog queries): run it on
    the first connection, every 10 minutes and after a reconnect, and cache the result.
    Requires separate, unimpersonated identities on the same PostgreSQL server and
    database. migration_role=None derives the migration identity as the single
    ADMIN-only member of assaf_os_owner. The ledger session must also run in
    session_replication_role = 'origin' (a cluster-wide replica setting is invisible to
    the catalog checks). Every refusal, including an unavailable catalog, is the single
    sanitized ValueError('ledger_role_denied'). It does not activate the ledger.
    """
    try:
        _require(db.get_bind().dialect.name == main_engine.dialect.name == "postgresql")
        db.execute(text(CATALOG_SEARCH_PATH))
        identity = text(
            "SELECT current_database(),inet_server_addr()::text,inet_server_port(),"
            "session_user,current_user"
        )
        ledger = db.execute(identity).one()
        _require(ledger[3] == ledger[4] == RUNTIME_ROLE and None not in ledger[:3])
        # session_replication_role = replica (ALTER SYSTEM or postgresql.conf, which neither
        # the role- nor the database-setting check sees) turns off foreign keys and the
        # assistant trigger for this very session.
        _require(db.scalar(text("SELECT current_setting('session_replication_role')")) == "origin")
        with main_engine.connect() as main:
            main.execute(text(CATALOG_SEARCH_PATH))
            app = main.execute(identity).one()
            _require(app[:3] == ledger[:3] and app[3] == app[4] and app[3] != ledger[3])
            main.execute(text("SET LOCAL statement_timeout = '5s'"))
            _validate_main_role_catalog(main, app[3])
            validate_ledger_catalog(
                db.connection(), main_role=app[3], migration_role=migration_role,
            )
    except (ValueError, SQLAlchemyError, TypeError, AttributeError, KeyError):
        raise ValueError(DENIED) from None
