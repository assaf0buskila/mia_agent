"""``PgLedgerTx``: the ``api.LedgerTx`` of the PostgreSQL ledger (V2_PG_CONTRACTS.md §5.1, §5.3).

One instance lives inside one ``PgLedger.transaction()`` (or one read), on that transaction's
connection, while it holds the ``os_workspaces`` row lock. It mirrors the FakeLedger statement for
statement; the database enforces every V2-14/14b key and CHECK as the backstop.

* ``apply`` runs inside a SAVEPOINT, so it is atomic on its own and leaves the transaction usable.
* A database error outside ``apply`` poisons the transaction: every later call, and the exit,
  re-raise the same ``LedgerError`` and nothing commits (SQLSTATE class 23 is
  ``ledger_integrity_failed``; anything else is ``ledger_unavailable``).
* A caller bug that the FakeLedger reports as ``fake_invariant:<name>`` is
  ``ledger_integrity_failed`` (reason ``<name>``) here, before any write.
* ``purpose="expire"`` admits only ``proposal()``, ``finish(status="expired")`` and
  ``connection``; anything else raises ``AssertionError("ledger_invariant:expire_scope")``.
Nothing here logs a record field, an amount or DB error text.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from app.services.os_ledger import api, role_policy
from app.services.os_ledger.domain import DomainError, canonical_input, validate_transition
from app.services.os_ledger.ids import (
    DATE_KEYS,
    FUSED_KINDS,
    TERMINAL,
    envelope_digest,
    fused_rejection_id,
    hex_id,
    is_uuid,
    mutation_json,
    refuse_live_dependents,
    same_record,
)
from app.services.os_ledger.validate_full import domain_error

_LOG = logging.getLogger("mia.os_ledger")

# Each record table's typed columns: its §18.2 UPDATE grant minus the common record columns.
TYPED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    entity: tuple(
        column for column in role_policy.COLUMN_UPDATES["os_" + entity]
        if column not in role_policy.RECORD_UPDATE_COLUMNS
    )
    for entity in api.ENTITIES
}
# The wire fields copied verbatim onto a typed column of the same name (the FakeLedger's set).
_CLAIM_FIELDS = ("person_id", "work_id", "original_payment_id", "currency", "source_type",
                 "source_key", "minutes")
_PROPOSAL_COLUMNS = (
    "workspace_id, proposal_id, request_id::text AS request_id, request_digest, envelope_digest, "
    "owner_id, epoch, approval_id::text AS approval_id, rejection_id::text AS rejection_id, "
    "entity, record_id, expected_revision, mutations, dependency_digest, source, "
    "credential_id, kind, status, created_at, expires_at"
)
_RECEIPT_COLUMNS = (
    "workspace_id, request_id::text AS request_id, proposal_id, epoch, status, kind, "
    "commit_version, records, completed_at, undo_until, undo_of::text AS undo_of, reason_codes"
)


_HELD = threading.local()


@contextmanager
def held(workspace: str):
    """One open ledger transaction per workspace per thread (the FakeLedger's nested-lock rule).

    On PostgreSQL a nested transaction would wait forever on the outer one's FOR UPDATE, so it
    fails loudly instead. A service that needs ledger effects inside an open transaction takes the
    ``LedgerTx`` as a parameter.
    """
    workspaces = getattr(_HELD, "workspaces", frozenset())
    if workspace in workspaces:
        raise AssertionError("ledger_invariant:nested_transaction")
    _HELD.workspaces = workspaces | {workspace}
    try:
        yield
    finally:
        _HELD.workspaces = workspaces


def integrity(reason: str) -> api.LedgerError:
    _LOG.warning("os_ledger op=tx outcome=refused reason=%s", reason)
    return api.LedgerError("ledger_integrity_failed", reason=reason)


def sqlstate(exc: BaseException) -> str:
    code = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return code if isinstance(code, str) and len(code) == 5 else "00000"


def database_error(exc: BaseException) -> api.LedgerError:
    """§5.3: SQLSTATE class 23 is ledger_integrity_failed; any other DB error ledger_unavailable."""
    state = sqlstate(exc)
    if state.startswith("23"):
        return integrity("integrity_" + state.lower())
    _LOG.warning("os_ledger op=tx outcome=refused reason=db_error_%s", state.lower())
    return api.LedgerError("ledger_unavailable", reason="db_error_" + state.lower())


def jsonb(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def wire(entity: str, row: Any) -> dict[str, Any]:
    """The v1 wire record of a stored row: its inputs plus the id key and the revision."""
    result = dict(row.inputs)
    result.update({api.ID_KEYS[entity]: row.record_id, "revision": int(row.revision)})
    if entity == "leads":
        result["person_id"] = row.person_id
    return result


def record_columns(entity: str) -> str:
    return "record_id, revision, inputs, deleted_at" + (", person_id" if entity == "leads" else "")


def proposal_row(row: Any) -> api.ProposalRow:
    return api.ProposalRow(
        row.workspace_id, row.proposal_id, row.request_id, row.request_digest,
        row.envelope_digest, row.owner_id, int(row.epoch), row.approval_id, row.rejection_id,
        row.entity, row.record_id, int(row.expected_revision),
        tuple(api.Mutation(**item) for item in row.mutations), row.dependency_digest,
        row.source, row.credential_id, row.kind, row.status, row.created_at, row.expires_at)


def receipt_row(row: Any) -> api.ReceiptRow:
    return api.ReceiptRow(
        row.workspace_id, row.request_id, row.proposal_id, int(row.epoch), row.status, row.kind,
        int(row.commit_version), tuple(dict(item) for item in row.records), row.completed_at,
        row.undo_until, row.undo_of, tuple(row.reason_codes))


class PgLedgerTx:
    """One admitted ledger transaction (or read) on one connection."""

    def __init__(self, conn: Connection, ctx: api.TrustedOwnerContext, *, kind: str,
                 purpose: str, now: datetime, version: int,
                 uuid4: Callable[[], str], undo_window_s: Callable[[], int],
                 validator: Any, summaries: Any) -> None:
        self.ctx = ctx
        self.now = now
        self.version = version
        self.connection = conn
        self.scratch: dict[str, Any] = {}
        self._conn = conn
        self._ws = ctx.workspace
        self._kind = kind
        self._purpose = purpose
        self._uuid4 = uuid4
        self._undo_window_s = undo_window_s
        self._validator = validator
        self._summaries = summaries
        self.current_version = version  # moves with finish(); reads report it as observed
        self.pending_seq = 0  # reads: the snapshot's pending_seq and states (head())
        self.states: dict[str, str] = {}
        self._records: dict[str, dict[str, dict[str, Any]]] | None = None
        self.poisoned: api.LedgerError | None = None
        self._in_apply = False

    # ---- plumbing ------------------------------------------------------------------------

    def _scope(self) -> None:
        if self._purpose == "expire":
            raise AssertionError("ledger_invariant:expire_scope")

    def _exec(self, sql: str, params: Mapping[str, Any] | None = None):
        if self.poisoned is not None:
            raise self.poisoned
        try:
            return self._conn.execute(text(sql), {"ws": self._ws, **(params or {})})
        except SQLAlchemyError as exc:
            error = database_error(exc)
            if not self._in_apply:  # apply's SAVEPOINT handler keeps the transaction usable
                self.poisoned = error
            raise error from None

    def _one(self, sql: str, params: Mapping[str, Any] | None = None):
        return self._exec(sql, params).first()

    # ---- record state ----------------------------------------------------------------------

    def _load_records(self) -> dict[str, dict[str, dict[str, Any]]]:
        if self._records is None:
            records: dict[str, dict[str, dict[str, Any]]] = {}
            for entity in api.ENTITIES:
                rows = self._exec(
                    f"SELECT {record_columns(entity)} FROM assaf_os.os_{entity} "
                    "WHERE workspace_id = :ws AND deleted_at IS NULL"
                ).all()
                records[entity] = {row.record_id: wire(entity, row) for row in rows}
            self._records = records
        return self._records

    def snapshot_records(self) -> dict[str, list[dict[str, Any]]]:
        """Copies of every non-deleted wire record, per entity, sorted by record id.

        The exact shape the FakeLedger hands to ``validate_state``, ``build_summaries`` and the
        dependency digest. Canonical inputs are flat scalars, so a dict copy is a deep copy.
        """
        records = self._load_records()
        return {entity: [dict(records[entity][key]) for key in sorted(records[entity])]
                for entity in api.ENTITIES}

    def _entity(self, entity: Any) -> str:
        if entity not in api.ENTITIES:
            raise api.LedgerError("invalid_entity")
        return entity

    # ---- reads -----------------------------------------------------------------------------

    def get(self, entity: api.Entity, record_id: str, *,
            include_deleted: bool = False) -> api.RecordDict | None:
        self._scope()
        entity = self._entity(entity)
        if not isinstance(record_id, str):
            return None
        row = self._one(
            f"SELECT {record_columns(entity)} FROM assaf_os.os_{entity} "
            "WHERE workspace_id = :ws AND record_id = :id", {"id": record_id})
        if row is None or (row.deleted_at is not None and not include_deleted):
            return None
        return wire(entity, row)

    def get_many(self, refs: Sequence[api.RecordRef], *,
                 include_deleted: bool = False) -> dict[api.RecordRef, api.RecordDict]:
        self._scope()
        refs = list(refs)
        found: dict[tuple[str, str], api.RecordDict] = {}
        wanted: dict[str, set[str]] = {}
        for ref in refs:
            if isinstance(ref.record_id, str):
                wanted.setdefault(self._entity(ref.entity), set()).add(ref.record_id)
        for entity, ids in wanted.items():
            rows = self._exec(
                f"SELECT {record_columns(entity)} FROM assaf_os.os_{entity} "
                "WHERE workspace_id = :ws AND record_id = ANY(:ids)", {"ids": sorted(ids)}).all()
            for row in rows:
                if row.deleted_at is None or include_deleted:
                    found[(entity, row.record_id)] = wire(entity, row)
        return {ref: dict(found[(ref.entity, ref.record_id)]) for ref in refs
                if (ref.entity, ref.record_id) in found}

    def find_expenses(self, *, ids: Sequence[str] = (), source_key_prefix: str | None = None,
                      manual_window: tuple[date, date] | None = None) -> list[api.RecordDict]:
        """Non-deleted expenses matching ANY given filter (a UNION; no filter returns []).

        §22 E7 (c): the ids, OR a source_key starting with ``source_key_prefix``, OR a
        business_date inside ``manual_window`` (both bounds inclusive). Tombstones never match.
        """
        self._scope()
        clauses: list[str] = []
        params: dict[str, Any] = {}
        ids = [value for value in ids if isinstance(value, str)]
        if ids:
            clauses.append("record_id = ANY(:ids)")
            params["ids"] = ids
        if source_key_prefix is not None:
            clauses.append("starts_with(source_key, CAST(:prefix AS text))")
            params["prefix"] = source_key_prefix
        if manual_window is not None:
            clauses.append("business_date BETWEEN :lo AND :hi")
            params.update(lo=manual_window[0], hi=manual_window[1])
        if not clauses:
            return []
        rows = self._exec(
            f"SELECT {record_columns('expenses')} FROM assaf_os.os_expenses "
            f"WHERE workspace_id = :ws AND deleted_at IS NULL AND ({' OR '.join(clauses)}) "
            'ORDER BY record_id COLLATE "C"', params).all()
        return [wire("expenses", row) for row in rows]

    def dependents(self, entity: api.Entity, record_id: str) -> list[api.RecordRef]:
        """Live referrers over the one ``api.DEPENDENTS`` table, judged by ``api.is_live``."""
        self._scope()
        result: list[api.RecordRef] = []
        for referrer, field, target in api.DEPENDENTS:
            if target != entity:
                continue
            if field not in TYPED_COLUMNS[referrer]:
                raise integrity("dependents_column")
            rows = self._exec(
                f"SELECT {record_columns(referrer)} FROM assaf_os.os_{referrer} "
                f'WHERE workspace_id = :ws AND {field} = :id ORDER BY record_id COLLATE "C"',
                {"id": record_id}).all()
            for row in rows:
                record = wire(referrer, row)
                if record.get(field) == record_id and api.is_live(
                    referrer, record, deleted=row.deleted_at is not None
                ):
                    result.append(api.RecordRef(referrer, row.record_id))
        return result

    def proposal(self, proposal_id: str) -> api.ProposalRow | None:
        if not isinstance(proposal_id, str):
            return None
        row = self._one(f"SELECT {_PROPOSAL_COLUMNS} FROM assaf_os.os_proposals "
                        "WHERE workspace_id = :ws AND proposal_id = :id", {"id": proposal_id})
        return None if row is None else proposal_row(row)

    def proposal_by_request(self, request_id: str) -> api.ProposalRow | None:
        self._scope()
        if not is_uuid(request_id):
            return None
        row = self._one(f"SELECT {_PROPOSAL_COLUMNS} FROM assaf_os.os_proposals "
                        "WHERE workspace_id = :ws AND request_id = CAST(:id AS uuid)",
                        {"id": request_id})
        return None if row is None else proposal_row(row)

    def receipt(self, request_id: str) -> api.ReceiptRow | None:
        self._scope()
        if not is_uuid(request_id):
            return None
        row = self._one(f"SELECT {_RECEIPT_COLUMNS} FROM assaf_os.os_receipts "
                        "WHERE workspace_id = :ws AND request_id = CAST(:id AS uuid)",
                        {"id": request_id})
        return None if row is None else receipt_row(row)

    def receipt_for_proposal(self, proposal_id: str) -> api.ReceiptRow | None:
        self._scope()
        if not isinstance(proposal_id, str):
            return None
        row = self._one(f"SELECT {_RECEIPT_COLUMNS} FROM assaf_os.os_receipts "
                        "WHERE workspace_id = :ws AND proposal_id = :id", {"id": proposal_id})
        return None if row is None else receipt_row(row)

    def undo_receipt_of(self, request_id: str) -> api.ReceiptRow | None:
        self._scope()
        if not is_uuid(request_id):
            return None
        row = self._one(f"SELECT {_RECEIPT_COLUMNS} FROM assaf_os.os_receipts "
                        "WHERE workspace_id = :ws AND undo_of = CAST(:id AS uuid)",
                        {"id": request_id})
        return None if row is None else receipt_row(row)

    def lead_person_taken(self, person_id: Any, record_id: str) -> bool:
        """UNIQUE (workspace_id, person_id) on os_leads, tombstones included (a business code)."""
        self._scope()
        return bool(self._exec(
            "SELECT EXISTS (SELECT 1 FROM assaf_os.os_leads WHERE workspace_id = :ws "
            "AND person_id = :person AND record_id <> :id)",
            {"person": person_id, "id": record_id}).scalar())

    def pending_proposals(self, epoch: int, fetch: int, offset: int) -> list[api.ProposalRow]:
        """§8/§22 E7 (d): kind proposal, status pending, unexpired at ``now``, of ``epoch``."""
        rows = self._exec(
            f"SELECT {_PROPOSAL_COLUMNS} FROM assaf_os.os_proposals WHERE workspace_id = :ws "
            "AND kind = 'proposal' AND status = 'pending' AND expires_at > :now "
            'AND epoch = :epoch ORDER BY created_at, proposal_id COLLATE "C" '
            "LIMIT :fetch OFFSET :offset",
            {"now": self.now, "epoch": epoch, "fetch": fetch, "offset": offset}).all()
        return [proposal_row(row) for row in rows]

    # ---- writes ----------------------------------------------------------------------------

    def _keyspace_taken(self, request_id: str) -> bool:
        """§5.3 keyspace: any proposal's approval/rejection id, or any receipt key."""
        return bool(self._exec(
            "SELECT EXISTS (SELECT 1 FROM assaf_os.os_receipts WHERE workspace_id = :ws "
            "AND request_id = CAST(:id AS uuid)) OR EXISTS (SELECT 1 FROM assaf_os.os_proposals "
            "WHERE workspace_id = :ws AND (approval_id = CAST(:id AS uuid) "
            "OR rejection_id = CAST(:id AS uuid)))", {"id": request_id}).scalar())

    def insert_proposal(self, draft: api.ProposalDraft) -> api.ProposalRow:
        """Insert ``draft`` as ``pending`` and bump ``pending_seq`` by one (§5.3)."""
        self._scope()
        if self._purpose != "write":
            raise integrity("proposal_read_only")
        if draft.kind != self._kind:  # §22 E7: the draft kind is the transaction kind
            raise integrity("proposal_kind_mismatch")
        if not is_uuid(draft.request_id):
            raise integrity("proposal_request_id")
        if not isinstance(draft.expires_at, datetime) or draft.expires_at.utcoffset() is None:
            raise integrity("proposal_expiry")
        fused = draft.kind in FUSED_KINDS
        if fused and self._keyspace_taken(draft.request_id):
            raise api.LedgerError("idempotency_mismatch")
        approval = draft.request_id if fused else self._uuid4()
        rejection = fused_rejection_id(draft.request_id) if fused else self._uuid4()
        stored = json.loads(jsonb([mutation_json(m) for m in draft.mutations]))
        row = api.ProposalRow(
            self._ws, hex_id("PRO", self._uuid4), draft.request_id, draft.request_digest, "",
            str(self.ctx.numeric_owner), self.ctx.authority_epoch, approval, rejection,
            draft.entity, draft.record_id, draft.expected_revision,
            tuple(api.Mutation(**item) for item in stored), draft.dependency_digest,
            draft.source, draft.credential_id, draft.kind, "pending", self.now, draft.expires_at)
        row = replace(row, envelope_digest=envelope_digest(row))
        self._exec(
            "INSERT INTO assaf_os.os_proposals (workspace_id, proposal_id, request_id, "
            "request_digest, envelope_digest, owner_id, epoch, approval_id, rejection_id, entity, "
            "record_id, expected_revision, mutations, dependency_digest, source, credential_id, "
            "kind, status, created_at, expires_at) VALUES (:ws, :proposal_id, "
            "CAST(:request_id AS uuid), :request_digest, :envelope_digest, :owner_id, :epoch, "
            "CAST(:approval_id AS uuid), CAST(:rejection_id AS uuid), :entity, :record_id, "
            ":expected_revision, CAST(:mutations AS jsonb), :dependency_digest, :source, "
            ":credential_id, :kind, 'pending', :created_at, :expires_at)",
            {"proposal_id": row.proposal_id, "request_id": row.request_id,
             "request_digest": row.request_digest, "envelope_digest": row.envelope_digest,
             "owner_id": row.owner_id, "epoch": row.epoch, "approval_id": row.approval_id,
             "rejection_id": row.rejection_id, "entity": row.entity, "record_id": row.record_id,
             "expected_revision": row.expected_revision, "mutations": jsonb(stored),
             "dependency_digest": row.dependency_digest, "source": row.source,
             "credential_id": row.credential_id, "kind": row.kind, "created_at": row.created_at,
             "expires_at": row.expires_at})
        self._exec("UPDATE assaf_os.os_workspaces SET pending_seq = pending_seq + 1 "
                   "WHERE workspace_id = :ws")
        return row

    def apply(self, mutations: Sequence[api.Mutation], *, source: api.Source,
              request_id: str) -> tuple[api.RecordDict, ...]:
        """Compare-and-set every mutation, then validate and maintain summaries (§5.3).

        The compare-and-set is strict (§22 E22): ``before`` must equal the stored record, not only
        its revision. A record that lands in its FORK-DELETE end state must have no live
        dependents (§22 E20, ``has_dependents``, any kind), judged on the batch's result.

        Atomic on its own: it runs inside a SAVEPOINT, so on any failure none of its writes
        remain and the transaction stays usable (a service can still record a terminal
        ``target_changed`` receipt).
        """
        self._scope()
        if self._purpose != "write":
            raise integrity("apply_read_only")
        if self.poisoned is not None:
            raise self.poisoned
        mutations = tuple(mutations)
        try:
            savepoint = self._conn.begin_nested()
        except SQLAlchemyError as exc:
            self.poisoned = database_error(exc)
            raise self.poisoned from None
        self._in_apply = True
        try:
            result = tuple(self._apply_one(m, source, request_id, mutations) for m in mutations)
            self._validator.validate(self, mutations)
            self._summaries.on_apply(self, mutations)
            savepoint.commit()
            return result
        except BaseException:
            self._records = None
            try:
                if savepoint.is_active:
                    savepoint.rollback()
            except SQLAlchemyError as exc:
                self.poisoned = database_error(exc)
            raise
        finally:
            self._in_apply = False

    def _apply_one(self, mutation: api.Mutation, source: str, request_id: str,
                   batch: Sequence[api.Mutation]) -> api.RecordDict:
        entity, key = self._entity(mutation.entity), mutation.record_id
        if not isinstance(key, str) or not isinstance(mutation.after, Mapping):
            raise integrity("apply_mutation_shape")
        current = self._one(
            f"SELECT {record_columns(entity)} FROM assaf_os.os_{entity} "
            "WHERE workspace_id = :ws AND record_id = :id", {"id": key})
        before = mutation.before
        revision = before["revision"] if before is not None else 0
        after_revision = mutation.after.get("revision")
        # §5.3: create = 1 and update = before + 1; an import writes the manifest revision,
        # >= 1 on a create and greater than the current revision on an update.
        valid_revision = type(after_revision) is int and (
            after_revision > revision if self._kind == "import" else after_revision == revision + 1)
        matches = (current is None if before is None else
                   current is not None and current.revision == revision)
        if current is not None:
            tombstoned = current.deleted_at is not None
            matches = matches and tombstoned == (mutation.op == "restore")
        if mutation.op == "restore" and before is None:
            matches = False
        if not matches or not valid_revision:
            raise api.LedgerError("target_changed")
        # §22 E22: a strict compare-and-set. The revision matches, so a different record means the
        # before-image was not built from the stored row; the summaries read it, so refuse.
        if before is not None and not same_record(before, wire(entity, current)):
            raise integrity("apply_before_image")
        refuse_live_dependents(self, mutation, batch)  # §22 E20
        system = {api.ID_KEYS[entity], "revision"} | ({"person_id"} if entity == "leads" else set())
        try:
            inputs = canonical_input(entity, {k: v for k, v in mutation.after.items()
                                               if k not in system})
            # A restore has no target in the non-deleted projection; its identity is the
            # compare-and-set above.
            validate_transition(entity, None if mutation.op == "restore" else before,
                                dict(mutation.after), self.snapshot_records())
        except DomainError as exc:
            raise domain_error(exc) from None
        raw_date = inputs.get(DATE_KEYS.get(entity))
        business_date = date.fromisoformat(raw_date) if raw_date else None
        typed: dict[str, Any] = {
            column: mutation.after.get(column)
            for column in TYPED_COLUMNS[entity] if column in _CLAIM_FIELDS
        }
        if entity in {"payments", "expenses"}:
            typed["amount_minor"] = inputs[
                "actual_amount_ils" if entity == "payments" else "amount_ils"]
        if entity == "works":
            typed["agreed_amount_minor"] = inputs.get("agreed_amount_ils")
        if entity == "tasks":
            typed["due_date"] = business_date
        if entity == "expenses":
            typed["source_digest"] = api.digest({
                "source_type": inputs["source_type"], "source_key": inputs["source_key"]})
            # The unique index covers tombstoned rows too, which the domain check does not see.
            if self._exec(
                "SELECT EXISTS (SELECT 1 FROM assaf_os.os_expenses WHERE workspace_id = :ws "
                "AND record_id <> :id AND source_type = :type AND "
                "assaf_os.expense_source_key_sha256(source_key) = "
                "assaf_os.expense_source_key_sha256(CAST(:key AS text)) AND source_key = :key)",
                {"id": key, "type": typed["source_type"], "key": typed["source_key"]},
            ).scalar():
                raise api.LedgerError("target_changed",
                                      details={"reason": "duplicate_expense_source"})
        if entity == "leads" and self.lead_person_taken(typed["person_id"], key):
            raise api.LedgerError("lead_already_exists")
        if set(typed) != set(TYPED_COLUMNS[entity]):
            raise integrity("apply_typed_columns")
        tombstone = mutation.op == "tombstone"
        params = {
            "id": key, "revision": after_revision, "inputs": jsonb(inputs),
            "business_date": business_date, "source": source, "now": self.now,
            "deleted_at": self.now if tombstone else None,
            "deleted_by": request_id if tombstone else None,
            **{"t_" + column: value for column, value in typed.items()},
        }
        if current is None:
            columns = ", ".join(TYPED_COLUMNS[entity])
            values = ", ".join(":t_" + column for column in TYPED_COLUMNS[entity])
            self._exec(
                f"INSERT INTO assaf_os.os_{entity} (workspace_id, record_id, revision, inputs, "
                "business_date, source, created_at, updated_at, deleted_at, deleted_by_request_id"
                f"{', ' + columns if columns else ''}) VALUES (:ws, :id, :revision, "
                "CAST(:inputs AS jsonb), :business_date, :source, :now, :now, :deleted_at, "
                f"CAST(:deleted_by AS uuid){', ' + values if values else ''})", params)
        else:
            assignments = "".join(f", {column} = :t_{column}" for column in TYPED_COLUMNS[entity])
            self._exec(
                f"UPDATE assaf_os.os_{entity} SET revision = :revision, "
                "inputs = CAST(:inputs AS jsonb), business_date = :business_date, "
                "source = :source, updated_at = :now, deleted_at = :deleted_at, "
                f"deleted_by_request_id = CAST(:deleted_by AS uuid){assignments} "
                "WHERE workspace_id = :ws AND record_id = :id", params)
        stored = dict(inputs)
        stored.update({api.ID_KEYS[entity]: key, "revision": after_revision})
        if entity == "leads":
            stored["person_id"] = typed["person_id"]
        records = self._load_records()[entity]
        if tombstone:
            records.pop(key, None)
        else:
            records[key] = stored
        return dict(stored)

    def _batch_line_count(self, proposal_id: str) -> int:
        return int(self._exec(
            "SELECT count(*) FROM assaf_os.os_statement_batch_lines "
            "WHERE workspace_id = :ws AND proposal_id = :id", {"id": proposal_id}).scalar())

    def finish(self, proposal: api.ProposalRow, *, status: api.ReceiptStatus,
               receipt_request_id: str, applied: Sequence[api.Mutation] = (),
               undo_until: datetime | None = None, undo_of: str | None = None,
               reason_codes: Sequence[str] = ()) -> api.ReceiptRow:
        """pending -> terminal; version and pending_seq +1; one receipt, one event, and one
        outbox row only when committed (§5.3)."""
        reason_codes = tuple(reason_codes)
        applied = tuple(applied)
        if self._purpose == "expire":
            # §5.3/§12.2 and the §22 E7 restored-batch caveat: a pending statement_batch that is
            # overdue, of an older epoch, or ``restored_without_detail`` with zero batch lines.
            overdue = proposal.expires_at < self.now
            older = proposal.epoch != self.ctx.authority_epoch
            if not (proposal.kind == "statement_batch" and proposal.status == "pending"
                    and status == "expired"
                    and (overdue or older or (
                        reason_codes == ("restored_without_detail",)
                        and self._batch_line_count(proposal.proposal_id) == 0))):
                raise AssertionError("ledger_invariant:expire_scope")
        elif self._purpose != "write":
            raise integrity("finish_read_only")
        current = self._one(
            "SELECT status, approval_id::text AS approval_id, rejection_id::text AS rejection_id "
            "FROM assaf_os.os_proposals WHERE workspace_id = :ws AND proposal_id = :id",
            {"id": proposal.proposal_id})
        if current is None or current.status != "pending":
            raise integrity("finish_pending")
        if status not in TERMINAL:
            raise integrity("receipt_status")
        if status == "abandoned" and proposal.kind != "owner_direct":
            raise integrity("abandoned_kind")
        # A fused kind finishes only committed or target_changed (owner_direct also abandoned).
        if proposal.kind in FUSED_KINDS and status not in {
                "committed", "target_changed", "abandoned"}:
            raise integrity("receipt_fused_status")
        if len(reason_codes) > 20 or not all(isinstance(code, str) for code in reason_codes):
            raise integrity("receipt_reason_codes")
        if (undo_of is not None) != (proposal.kind == "owner_undo"):
            raise integrity("receipt_undo_kind")
        if undo_until is not None and not (
                proposal.kind == "owner_direct" and status == "committed"):
            raise integrity("receipt_undo_until_kind")
        if proposal.kind == "owner_direct" and status == "committed":
            undo_until = self.now + timedelta(seconds=int(self._undo_window_s()))
        # §5.3 receipt-key table. An expiry from expire_due (purpose="expire") or from an epoch
        # change is keyed by the rejection_id; a late approve or reject by the id it supplied.
        allowed = {
            "committed": {current.approval_id}, "target_changed": {current.approval_id},
            "abandoned": {current.approval_id}, "rejected": {current.rejection_id},
            "superseded": {current.rejection_id},
            "expired": {current.approval_id, current.rejection_id}}[status]
        if status == "expired" and (self._purpose == "expire" or "epoch_changed" in reason_codes):
            allowed = {current.rejection_id}
        if receipt_request_id not in allowed:
            raise integrity("receipt_key")
        version = int(self._exec(
            "UPDATE assaf_os.os_workspaces SET version = version + 1, "
            "pending_seq = pending_seq + 1, updated_at = GREATEST(updated_at, :now) "
            "WHERE workspace_id = :ws RETURNING version", {"now": self.now}).scalar_one())
        updated = self._exec(
            "UPDATE assaf_os.os_proposals SET status = :status WHERE workspace_id = :ws "
            "AND proposal_id = :id AND status = 'pending'",
            {"status": status, "id": proposal.proposal_id})
        if updated.rowcount != 1:
            raise integrity("finish_pending")
        refs = tuple({"entity": m.entity, "record_id": m.record_id,
                      "revision": m.after["revision"]} for m in applied)
        records = tuple(ref | {"digest": api.digest(dict(m.after))}
                        for ref, m in zip(refs, applied))
        if status != "committed" or proposal.kind == "import":
            records = ()
        self._exec(
            "INSERT INTO assaf_os.os_receipts (workspace_id, request_id, proposal_id, epoch, "
            "status, kind, commit_version, records, completed_at, undo_until, undo_of, "
            "reason_codes) VALUES (:ws, CAST(:key AS uuid), :proposal_id, :epoch, :status, :kind, "
            ":version, CAST(:records AS jsonb), :now, :undo_until, CAST(:undo_of AS uuid), "
            "CAST(:reasons AS jsonb))",
            {"key": receipt_request_id, "proposal_id": proposal.proposal_id,
             "epoch": proposal.epoch, "status": status, "kind": proposal.kind,
             "version": version, "records": jsonb(list(records)), "now": self.now,
             "undo_until": undo_until, "undo_of": undo_of, "reasons": jsonb(list(reason_codes))})
        event_id = self._uuid4()
        effects = list(refs) if status == "committed" else []
        self._exec(
            "INSERT INTO assaf_os.os_events (workspace_id, event_id, decision_id, commit_version, "
            "body, created_at) VALUES (:ws, CAST(:event AS uuid), CAST(:key AS uuid), :version, "
            "CAST(:body AS jsonb), :now)",
            {"event": event_id, "key": receipt_request_id, "version": version, "now": self.now,
             "body": jsonb({"status": status, "kind": proposal.kind, "effects": effects})})
        if status == "committed":
            body: dict[str, Any] = {"authority_epoch": proposal.epoch, "refs": list(refs)}
            if proposal.kind == "import":
                body.update(refs=[], full=True)
            self._exec(
                "INSERT INTO assaf_os.os_outbox (workspace_id, event_id, commit_version, body, "
                "status, attempts, next_attempt_at, lease_generation, created_at, updated_at) "
                "VALUES (:ws, CAST(:event AS uuid), :version, CAST(:body AS jsonb), 'pending', 0, "
                ":now, 0, :now, :now)",
                {"event": event_id, "version": version, "body": jsonb(body), "now": self.now})
        self.current_version = version
        return api.ReceiptRow(
            self._ws, receipt_request_id, proposal.proposal_id, proposal.epoch, status,
            proposal.kind, version, records, self.now, undo_until, undo_of, reason_codes)

    # ---- erratum E1-rev: import staging (§22) --------------------------------------------------

    def _import_guard(self) -> None:
        self._scope()
        if self._kind != "import" or self._purpose != "write":
            raise api.LedgerError("invalid_transaction_kind")

    def insert_import_batch(self, *, batch_id: str, authority_epoch: int, source_digest: str,
                            source_binding: str, provenance: Mapping[str, Any]) -> None:
        self._import_guard()
        if not is_uuid(batch_id):
            raise integrity("import_batch_id")
        if type(authority_epoch) is not int or authority_epoch <= 0:
            raise integrity("import_batch_epoch")
        if authority_epoch != self.ctx.authority_epoch:  # I12: never stage across epochs
            raise api.LedgerError("authority_changed", reason="import_epoch_mismatch")
        if not (isinstance(source_digest, str) and 0 < len(source_digest) <= 64):
            raise integrity("import_batch_source_digest")
        if not (isinstance(source_binding, str) and 0 < len(source_binding) <= 256):
            raise integrity("import_batch_source_binding")
        if not isinstance(provenance, Mapping):
            raise integrity("import_batch_provenance")
        self._exec(
            "INSERT INTO assaf_os.os_import_batches (workspace_id, batch_id, source_binding, "
            "source_digest, authority_epoch, status, provenance, quarantine, created_at, "
            "completed_at) VALUES (:ws, CAST(:batch AS uuid), :binding, :digest, :epoch, "
            "'staged', CAST(:provenance AS jsonb), NULL, :now, NULL)",
            {"batch": batch_id, "binding": source_binding, "digest": source_digest,
             "epoch": authority_epoch, "provenance": jsonb(dict(provenance)), "now": self.now})

    def _batch_status(self, batch_id: str, missing: str) -> str:
        row = self._one(
            "SELECT status FROM assaf_os.os_import_batches WHERE workspace_id = :ws "
            "AND batch_id = CAST(:batch AS uuid)", {"batch": batch_id}
        ) if is_uuid(batch_id) else None
        if row is None:
            raise integrity(missing)
        return row.status

    def insert_import_rows(self, batch_id: str, rows: Sequence[api.ImportRow]) -> int:
        """Insert-only rows of a ``staged`` batch; a settled batch is immutable."""
        self._import_guard()
        if self._batch_status(batch_id, "import_row_batch_fk") != "staged":
            raise api.LedgerError("import_status_invalid")
        prepared = []
        for row in rows:
            if not isinstance(row, api.ImportRow):
                raise integrity("import_row_type")
            checks = (
                ("import_row_number", type(row.row_number) is int and row.row_number > 0),
                ("import_row_entity", row.entity in api.ENTITIES),
                ("import_row_status", row.status in {"staged", "quarantined"}),
                ("import_row_record", row.record_id is None or (
                    isinstance(row.record_id, str) and 0 < len(row.record_id) <= 64)),
                ("import_row_source_digest", isinstance(row.source_digest, str)
                 and 0 < len(row.source_digest) <= 64),
                ("import_row_raw", isinstance(row.raw_record, Mapping)),
                ("import_row_canonical", row.canonical_record is None
                 or isinstance(row.canonical_record, Mapping)),
                ("import_row_provenance", isinstance(row.provenance, Mapping)),
                ("import_row_quarantine_type", row.quarantine is None
                 or isinstance(row.quarantine, Mapping)),
                ("import_row_quarantine", row.status != "quarantined"
                 or row.quarantine is not None),
                ("import_row_idless", row.record_id is not None or row.status == "quarantined"),
            )
            for name, ok in checks:
                if not ok:
                    raise integrity(name)
            prepared.append({
                "batch": batch_id, "number": row.row_number, "entity": row.entity,
                "record": row.record_id, "digest": row.source_digest, "status": row.status,
                "raw": jsonb(dict(row.raw_record)),
                "canonical": None if row.canonical_record is None
                else jsonb(dict(row.canonical_record)),
                "provenance": jsonb(dict(row.provenance)),
                "quarantine": None if row.quarantine is None else jsonb(dict(row.quarantine)),
            })
        for item in prepared:  # the primary and unique keys reject a duplicate (class 23)
            self._exec(
                "INSERT INTO assaf_os.os_import_rows (workspace_id, batch_id, row_number, entity, "
                "record_id, source_digest, status, raw_record, canonical_record, provenance, "
                "quarantine) VALUES (:ws, CAST(:batch AS uuid), :number, :entity, :record, "
                ":digest, :status, CAST(:raw AS jsonb), CAST(:canonical AS jsonb), "
                "CAST(:provenance AS jsonb), CAST(:quarantine AS jsonb))", item)
        return len(prepared)

    def set_import_status(self, batch_id: str, status: api.ImportStatus, *,
                          quarantine: Mapping[str, Any] | None = None) -> None:
        """Move along ``api.IMPORT_STATUS_EDGES`` only; completed_at on the terminal states."""
        self._import_guard()
        current = self._batch_status(batch_id, "import_status_batch_fk")
        if status not in {state for edge in api.IMPORT_STATUS_EDGES for state in edge}:
            raise integrity("import_status_value")
        if quarantine is not None and status != "quarantined":
            raise integrity("import_batch_quarantine")
        if (current, status) not in api.IMPORT_STATUS_EDGES:
            raise api.LedgerError("import_status_invalid")
        flagged = bool(self._exec(
            "SELECT EXISTS (SELECT 1 FROM assaf_os.os_import_rows WHERE workspace_id = :ws "
            "AND batch_id = CAST(:batch AS uuid) AND status = 'quarantined')",
            {"batch": batch_id}).scalar())
        if status == "validated" and flagged:  # validated: the manifest, no quarantined row
            raise api.LedgerError("import_status_invalid")
        if status == "quarantined" and not (flagged and isinstance(quarantine, Mapping)):
            raise api.LedgerError("import_status_invalid")
        self._exec(
            "UPDATE assaf_os.os_import_batches SET status = :status, "
            "quarantine = COALESCE(CAST(:quarantine AS jsonb), quarantine), "
            "completed_at = CASE WHEN :terminal THEN GREATEST(:now, created_at) "
            "ELSE completed_at END WHERE workspace_id = :ws AND batch_id = CAST(:batch AS uuid)",
            {"status": status, "batch": batch_id, "now": self.now,
             "terminal": status in {"imported", "quarantined", "failed"},
             "quarantine": jsonb(dict(quarantine)) if status == "quarantined" else None})

    def live_ids(self, entity: api.Entity) -> frozenset[str]:
        """§22 E7 (a): ids that are not tombstoned and not in their FORK-DELETE end state.

        ``api.FORK_DELETE_END_STATE`` and ``api.LIVE_IDS_LIMIT`` are read at call time; above the
        limit this is ``local_record_limit``.
        """
        self._scope()
        if self._kind != "import" and self._purpose != "read":
            raise api.LedgerError("invalid_transaction_kind")
        if entity not in api.ENTITIES:
            raise api.LedgerError("invalid_entity")
        end = api.FORK_DELETE_END_STATE[entity]
        limit = api.LIVE_IDS_LIMIT
        sql = (f"SELECT record_id FROM assaf_os.os_{entity} "
               "WHERE workspace_id = :ws AND deleted_at IS NULL")
        params: dict[str, Any] = {"cap": limit + 1}
        if end is not None:
            sql += " AND (inputs ->> CAST(:column AS text)) IS DISTINCT FROM CAST(:value AS text)"
            params.update(column=end[0], value=end[1])
        ids = frozenset(self._exec(sql + " LIMIT :cap", params).scalars())
        if len(ids) > limit:
            raise api.LedgerError("local_record_limit")
        return ids
