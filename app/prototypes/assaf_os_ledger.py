"""Default-off seven-entity ledger for synthetic disposable PostgreSQL only.

No runtime registration, settings, provider access, importer, or authority activation.
Injected principals are local test context, not authentication proof.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.prototypes.assaf_os_domain import (
    ENTITIES,
    ID_KEYS,
    canonical_input,
    lead_contact_dependencies,
    validate_state,
    validate_transition,
)
from app.prototypes.assaf_os_primary import (
    LocalPrimaryError,
    LocalPrincipal,
    LocalTaskPrimary,
    _digest,
    _uuid,
)
from app.prototypes.assaf_os_summaries import SummaryInputError
from app.prototypes.assaf_os_summaries import build_summaries as _build_summaries


def build_summaries(records):
    try:
        return _build_summaries(records)
    except SummaryInputError as exc:
        raise LocalPrimaryError(exc.code) from None


MAX_RECORDS = 2000
PREFIXES = dict(zip(ENTITIES, ("LEA", "PER", "WOR", "PAY", "TIM", "EXP", "TAS")))
DATE_KEYS = {
    "works": "start_date",
    "payments": "date",
    "time": "date",
    "expenses": "date",
    "tasks": "due_date",
}


class LedgerBase(DeclarativeBase):
    pass


class LedgerWorkspace(LedgerBase):
    __tablename__ = "os_local_ledger_workspaces"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(20))
    epoch: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(BigInteger, default=0)
    summaries: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (CheckConstraint("epoch > 0 AND version >= 0"),)


class Record:
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    inputs: Mapped[dict] = mapped_column(JSONB)
    business_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def record_constraints(*extra):
    return (
        ForeignKeyConstraint(["workspace_id"], ["os_local_ledger_workspaces.workspace_id"]),
        CheckConstraint("revision > 0"),
        *extra,
    )


class Lead(Record, LedgerBase):
    __tablename__ = "os_local_ledger_leads"
    person_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = record_constraints(UniqueConstraint("workspace_id", "person_id"))


class Client(Record, LedgerBase):
    __tablename__ = "os_local_ledger_clients"
    __table_args__ = record_constraints()


class Work(Record, LedgerBase):
    __tablename__ = "os_local_ledger_works"
    person_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = record_constraints(
        ForeignKeyConstraint(
            ["workspace_id", "person_id"],
            ["os_local_ledger_clients.workspace_id", "os_local_ledger_clients.record_id"],
        )
    )


class Payment(Record, LedgerBase):
    __tablename__ = "os_local_ledger_payments"
    person_id: Mapped[str] = mapped_column(String(64))
    work_id: Mapped[str] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    original_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = record_constraints(
        ForeignKeyConstraint(
            ["workspace_id", "person_id"],
            ["os_local_ledger_clients.workspace_id", "os_local_ledger_clients.record_id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "work_id"],
            ["os_local_ledger_works.workspace_id", "os_local_ledger_works.record_id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "original_payment_id"],
            ["os_local_ledger_payments.workspace_id", "os_local_ledger_payments.record_id"],
        ),
        CheckConstraint("amount_minor > 0 AND amount_minor <= 9007199254740991"),
    )


class TimeEntry(Record, LedgerBase):
    __tablename__ = "os_local_ledger_time"
    person_id: Mapped[str] = mapped_column(String(64))
    work_id: Mapped[str] = mapped_column(String(64))
    minutes: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = record_constraints(
        ForeignKeyConstraint(
            ["workspace_id", "person_id"],
            ["os_local_ledger_clients.workspace_id", "os_local_ledger_clients.record_id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "work_id"],
            ["os_local_ledger_works.workspace_id", "os_local_ledger_works.record_id"],
        ),
        CheckConstraint("minutes >= 0 AND minutes <= 9007199254740991"),
    )


class Expense(Record, LedgerBase):
    __tablename__ = "os_local_ledger_expenses"
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    source_type: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(2000))
    source_digest: Mapped[str] = mapped_column(String(64))
    __table_args__ = record_constraints(
        UniqueConstraint("workspace_id", "source_digest"),
        CheckConstraint("amount_minor > 0 AND amount_minor <= 9007199254740991"),
    )


class LedgerTask(Record, LedgerBase):
    __tablename__ = "os_local_ledger_tasks"
    person_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    work_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = record_constraints(
        ForeignKeyConstraint(
            ["workspace_id", "person_id"],
            ["os_local_ledger_clients.workspace_id", "os_local_ledger_clients.record_id"],
        ),
        ForeignKeyConstraint(
            ["workspace_id", "work_id"],
            ["os_local_ledger_works.workspace_id", "os_local_ledger_works.record_id"],
        ),
    )


MODELS = dict(zip(ENTITIES, (Lead, Client, Work, Payment, TimeEntry, Expense, LedgerTask)))


class LedgerProposal(LedgerBase):
    __tablename__ = "os_local_ledger_proposals"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36))
    request_digest: Mapped[str] = mapped_column(String(64))
    envelope_digest: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[str] = mapped_column(String(20))
    epoch: Mapped[int] = mapped_column(BigInteger)
    approval_id: Mapped[str] = mapped_column(String(36))
    rejection_id: Mapped[str] = mapped_column(String(36))
    entity: Mapped[str] = mapped_column(String(24))
    record_id: Mapped[str] = mapped_column(String(64))
    expected_revision: Mapped[int] = mapped_column(BigInteger)
    mutations: Mapped[list] = mapped_column(JSONB)
    dependency_digest: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(24))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["os_local_ledger_workspaces.workspace_id"]),
        UniqueConstraint("workspace_id", "request_id"),
        UniqueConstraint("workspace_id", "approval_id"),
        UniqueConstraint("workspace_id", "rejection_id"),
        CheckConstraint("expected_revision >= 0 AND epoch > 0"),
        CheckConstraint("approval_id <> rejection_id"),
        CheckConstraint("status IN ('pending','committed','rejected','expired','target_changed')"),
    )


class LedgerReceipt(LedgerBase):
    __tablename__ = "os_local_ledger_receipts"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64))
    epoch: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(BigInteger)
    records: Mapped[list] = mapped_column(JSONB)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["os_local_ledger_proposals.workspace_id", "os_local_ledger_proposals.proposal_id"],
        ),
        UniqueConstraint("workspace_id", "proposal_id"),
        CheckConstraint("version > 0 AND epoch > 0"),
        CheckConstraint("status IN ('committed','rejected','expired','target_changed')"),
    )


class LedgerEvent(LedgerBase):
    __tablename__ = "os_local_ledger_events"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(36))
    body: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["os_local_ledger_receipts.workspace_id", "os_local_ledger_receipts.request_id"],
        ),
        UniqueConstraint("workspace_id", "decision_id"),
    )


class LedgerOutbox(LedgerBase):
    __tablename__ = "os_local_ledger_outbox"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "event_id"],
            ["os_local_ledger_events.workspace_id", "os_local_ledger_events.event_id"],
        ),
        UniqueConstraint("workspace_id", "version"),
        CheckConstraint("status = 'pending'"),
    )


def _copy(value):
    return json.loads(json.dumps(value))


def _record(entity, row):
    if row is None:
        return None
    date_key = DATE_KEYS.get(entity)
    if date_key and (
        row.business_date.isoformat() if row.business_date else None
    ) != row.inputs.get(date_key):
        raise LocalPrimaryError("primary_verification_failed")
    for field in ("person_id", "work_id", "original_payment_id", "source_type", "source_key"):
        if (
            entity != "leads"
            and hasattr(type(row), field)
            and getattr(row, field) != row.inputs.get(field)
        ):
            raise LocalPrimaryError("primary_verification_failed")
    amount_key = "actual_amount_ils" if entity == "payments" else "amount_ils"
    if entity in {"payments", "expenses"} and row.amount_minor != row.inputs.get(amount_key):
        raise LocalPrimaryError("primary_verification_failed")
    if entity == "time" and row.minutes != row.inputs.get("minutes"):
        raise LocalPrimaryError("primary_verification_failed")
    if entity == "expenses" and row.source_digest != _digest(
        {
            "source_type": row.source_type,
            "source_key": row.source_key,
        }
    ):
        raise LocalPrimaryError("primary_verification_failed")
    identity = {ID_KEYS[entity]: row.record_id, "revision": row.revision}
    if entity == "leads":
        identity["person_id"] = row.person_id
    return _copy(row.inputs | identity)


def _envelope(row):
    return {
        key: getattr(row, key)
        for key in (
            "workspace_id",
            "proposal_id",
            "request_id",
            "request_digest",
            "owner_id",
            "epoch",
            "approval_id",
            "rejection_id",
            "entity",
            "record_id",
            "expected_revision",
            "mutations",
            "dependency_digest",
            "source",
        )
    } | {"expires_at": row.expires_at.isoformat()}


def _lookup(records, entity, record_id):
    return next((r for r in records[entity] if r[ID_KEYS[entity]] == record_id), None)


def _prospective(records, mutations):
    result = _copy(records)
    for change in mutations:
        entity, record_id = change["entity"], change["record_id"]
        result[entity] = [r for r in result[entity] if r[ID_KEYS[entity]] != record_id]
        result[entity].append(_copy(change["after"]))
        result[entity].sort(key=lambda r: r[ID_KEYS[entity]])
    return result


def _dependencies(records, mutations):
    """Capture target and exact source/dependency records, including absent matches."""
    result = []
    for change in mutations:
        entity, key, candidate = change["entity"], change["record_id"], change["after"]
        part = {"entity": entity, "target": _lookup(records, entity, key)}
        if candidate.get("person_id"):
            part["client"] = _lookup(records, "clients", candidate["person_id"])
        if candidate.get("work_id") and entity != "works":
            part["work"] = _lookup(records, "works", candidate["work_id"])
        if entity == "leads":
            part["contacts"] = lead_contact_dependencies(candidate, records["leads"], key)
        if entity == "payments":
            original = candidate.get("original_payment_id")
            part["payments"] = [
                r
                for r in records["payments"]
                if r["payment_id"] in {key, original}
                or r.get("original_payment_id") in {key, original} - {None}
            ]
        if entity == "expenses":
            part["sources"] = [
                r
                for r in records["expenses"]
                if (r["source_type"], r["source_key"])
                == (candidate["source_type"], candidate["source_key"])
            ]
        result.append(part)
    return result


class LocalLedgerPrimary(LocalTaskPrimary):
    """Bounded shadow prototype; every entry point retains disposable-only guards."""

    def initialize_local_schema(self):
        self._guard()
        with self.engine.connect() as c:
            db, marker = c.execute(
                text("SELECT current_database(), current_setting('assaf_os.local_prototype', true)")
            ).one()
            if db != self.engine.url.database or marker != "disposable_only":
                raise LocalPrimaryError("disposable_loopback_database_required")
            if c.scalar(text("SHOW fsync")) != "on":
                raise LocalPrimaryError("durable_database_required")
        LedgerBase.metadata.create_all(self.engine)
        with Session(self.engine) as s, s.begin():
            row = s.get(LedgerWorkspace, self.binding.workspace_id)
            if row is None:
                s.add(
                    LedgerWorkspace(
                        workspace_id=self.binding.workspace_id,
                        owner_id=self.binding.owner_id,
                        epoch=self.binding.authority_epoch,
                        version=0,
                        summaries=build_summaries({e: [] for e in ENTITIES}),
                    )
                )
            else:
                self._binding(row)

    def _workspace(self, s):
        row = s.scalar(
            select(LedgerWorkspace)
            .where(LedgerWorkspace.workspace_id == self.binding.workspace_id)
            .with_for_update()
        )
        self._binding(row)
        if s.scalar(text("SHOW synchronous_commit")) != "on":
            raise LocalPrimaryError("durable_database_required")
        return row

    def _records(self, s):
        records, count = {}, 0
        for entity, model in MODELS.items():
            rows = s.scalars(
                select(model)
                .where(model.workspace_id == self.binding.workspace_id)
                .order_by(model.record_id)
                .limit(MAX_RECORDS + 1)
            ).all()
            count += len(rows)
            if count > MAX_RECORDS:
                raise LocalPrimaryError("local_record_limit")
            records[entity] = [_record(entity, r) for r in rows]
        return records

    def propose(
        self,
        principal: LocalPrincipal,
        request_id: str,
        entity: str,
        inputs: dict,
        *,
        record_id: str | None = None,
        expected_revision: int = 0,
        approval_expires_at: datetime | None = None,
    ) -> dict:
        self._require_owner(principal)
        if principal.source not in {"OS", "MIA"}:
            raise LocalPrimaryError("proposal_source_required")
        _uuid(request_id)
        if type(entity) is not str or entity not in (*ENTITIES, "promote_lead"):
            raise LocalPrimaryError("invalid_entity")
        if type(expected_revision) is not int or expected_revision < 0:
            raise LocalPrimaryError("invalid_revision")
        if record_id is not None:
            # The canonical ID validator has the same no-prefix-assumption contract.
            canonical_input("tasks", {"person_id": record_id}, creating=False)
        now = self._now()
        expiry = now + timedelta(minutes=10)
        if approval_expires_at is not None:
            if (
                not isinstance(approval_expires_at, datetime)
                or approval_expires_at.utcoffset() is None
            ):
                raise LocalPrimaryError("invalid_approval_expiry")
            expiry = min(expiry, approval_expires_at.astimezone(now.tzinfo))
        if entity == "promote_lead":
            if type(inputs) is not dict or inputs or record_id is None:
                raise LocalPrimaryError("invalid_promotion_input")
            values = {}
        else:
            values = canonical_input(entity, inputs, creating=record_id is None)
        request_digest = _digest(
            {
                "owner": principal.owner_id,
                "workspace": self.binding.workspace_id,
                "epoch": self.binding.authority_epoch,
                "source": principal.source,
                "entity": entity,
                "record_id": record_id,
                "revision": expected_revision,
                "input": values,
                "outer_expiry": approval_expires_at.isoformat() if approval_expires_at else None,
            }
        )
        with Session(self.engine) as s, s.begin():
            self._workspace(s)
            existing = s.scalar(
                select(LedgerProposal).where(
                    LedgerProposal.workspace_id == self.binding.workspace_id,
                    LedgerProposal.request_id == request_id,
                )
            )
            if existing:
                if existing.request_digest != request_digest:
                    raise LocalPrimaryError("idempotency_mismatch")
                return _copy(_envelope(existing) | {"status": existing.status})
            if expiry <= now:
                raise LocalPrimaryError("invalid_approval_expiry")
            records = self._records(s)
            target_entity = "leads" if entity == "promote_lead" else entity
            before = _lookup(records, target_entity, record_id) if record_id else None
            if (record_id is None and expected_revision != 0) or (
                record_id is not None
                and (before is None or before["revision"] != expected_revision)
            ):
                raise LocalPrimaryError("target_changed")
            target_id = record_id or PREFIXES[entity] + "_" + uuid4().hex
            if entity == "promote_lead":
                # Enums are frozen from the existing schema; promotion preserves person identity.
                from app.prototypes.assaf_os_domain import SCHEMA

                lead_after = before | {
                    "status": SCHEMA["enums"]["lead_status"][-1],
                    "revision": expected_revision + 1,
                }
                mutations = [
                    {
                        "entity": "leads",
                        "record_id": target_id,
                        "before": before,
                        "after": lead_after,
                    }
                ]
                client = _lookup(records, "clients", before["person_id"])
                if client is None:
                    client_input = canonical_input(
                        "clients",
                        {
                            "name": before["name"],
                            "phone": before.get("phone") or None,
                            "email": before.get("email") or None,
                            "status": SCHEMA["enums"]["client_status"][0],
                            "notes": None,
                        },
                    )
                    mutations.append(
                        {
                            "entity": "clients",
                            "record_id": before["person_id"],
                            "before": None,
                            "after": client_input
                            | {"person_id": before["person_id"], "revision": 1},
                        }
                    )
                else:
                    mutations.append(
                        {
                            "entity": "clients",
                            "record_id": client["person_id"],
                            "before": client,
                            "after": client | {"revision": client["revision"] + 1},
                        }
                    )
            else:
                identity = {ID_KEYS[entity]: target_id, "revision": expected_revision + 1}
                if entity == "leads":
                    identity["person_id"] = before["person_id"] if before else "PER_" + uuid4().hex
                after = (before or {}) | values | identity
                mutations = [
                    {
                        "entity": entity,
                        "record_id": target_id,
                        "before": before,
                        "after": after,
                    }
                ]
                validate_transition(entity, before, after, records)
            prospective = _prospective(records, mutations)
            if sum(map(len, prospective.values())) > MAX_RECORDS:
                raise LocalPrimaryError("local_record_limit")
            validate_state(prospective)
            build_summaries(prospective)  # Reject unsafe aggregate sums before proposing.
            row = LedgerProposal(
                workspace_id=self.binding.workspace_id,
                proposal_id="PRO_" + uuid4().hex,
                request_id=request_id,
                request_digest=request_digest,
                owner_id=principal.owner_id,
                epoch=self.binding.authority_epoch,
                approval_id=str(uuid4()),
                rejection_id=str(uuid4()),
                entity=entity,
                record_id=target_id,
                expected_revision=expected_revision,
                mutations=mutations,
                dependency_digest=_digest(_dependencies(records, mutations)),
                source=principal.source,
                expires_at=expiry,
                status="pending",
            )
            row.envelope_digest = _digest(_envelope(row))
            s.add(row)
            return _copy(_envelope(row) | {"status": row.status})

    def _apply_mutations(self, s, row, now):
        # Order is significant for newly promoted clients and future reference-bearing batches.
        for change in sorted(row.mutations, key=lambda c: c["entity"] != "clients"):
            entity, key, after = change["entity"], change["record_id"], change["after"]
            model = MODELS[entity]
            current = s.get(model, (self.binding.workspace_id, key))
            if current is None:
                current = model(
                    workspace_id=self.binding.workspace_id, record_id=key, created_at=now
                )
                s.add(current)
            current.inputs = canonical_input(
                entity,
                {
                    k: v
                    for k, v in after.items()
                    if k not in {ID_KEYS[entity], "revision"}
                    and not (entity == "leads" and k == "person_id")
                },
            )
            current.revision, current.source, current.updated_at = (
                after["revision"],
                row.source,
                now,
            )
            date_value = after.get(DATE_KEYS.get(entity))
            current.business_date = date.fromisoformat(date_value) if date_value else None
            for field in (
                "person_id",
                "work_id",
                "original_payment_id",
                "source_type",
                "source_key",
            ):
                if hasattr(model, field):
                    setattr(current, field, after.get(field))
            if entity in {"payments", "expenses"}:
                current.amount_minor = after[
                    "actual_amount_ils" if entity == "payments" else "amount_ils"
                ]
            if entity == "expenses":
                current.source_digest = _digest(
                    {
                        "source_type": after["source_type"],
                        "source_key": after["source_key"],
                    }
                )
            if entity == "time":
                current.minutes = after["minutes"]
            s.flush()

    def _insert_outbox(self, s, event, receipt, mutations):
        s.add(
            LedgerOutbox(
                workspace_id=self.binding.workspace_id,
                event_id=event.event_id,
                version=receipt.version,
                body={
                    "authority_epoch": receipt.epoch,
                    "records": [{"entity": c["entity"], "record": c["after"]} for c in mutations],
                },
                status="pending",
            )
        )

    def decide(
        self,
        principal: LocalPrincipal,
        proposal_id: str,
        request_id: str,
        decision: Literal["approve", "reject"] = "approve",
    ) -> dict:
        self._require_owner(principal, deciding=True)
        _uuid(request_id)
        if type(decision) is not str or decision not in {"approve", "reject"}:
            raise LocalPrimaryError("invalid_decision")
        with Session(self.engine) as s, s.begin():
            workspace = self._workspace(s)
            row = s.get(LedgerProposal, (self.binding.workspace_id, proposal_id))
            if row is None:
                raise LocalPrimaryError("proposal_not_found")
            if row.owner_id != principal.owner_id or row.epoch != self.binding.authority_epoch:
                raise LocalPrimaryError("authority_changed")
            if _digest(_envelope(row)) != row.envelope_digest:
                raise LocalPrimaryError("proposal_integrity_failed")
            expected_id = row.approval_id if decision == "approve" else row.rejection_id
            if request_id != expected_id:
                raise LocalPrimaryError("decision_request_mismatch")
            previous = s.scalar(
                select(LedgerReceipt).where(
                    LedgerReceipt.workspace_id == self.binding.workspace_id,
                    LedgerReceipt.proposal_id == proposal_id,
                )
            )
            if previous:
                if previous.request_id != request_id:
                    raise LocalPrimaryError("already_decided")
            else:
                if row.status != "pending":
                    raise LocalPrimaryError("proposal_integrity_failed")
                now, records = self._now(), self._records(s)
                status = "committed" if decision == "approve" else "rejected"
                if now >= row.expires_at:
                    status = "expired"
                elif decision == "approve":
                    if _digest(_dependencies(records, row.mutations)) != row.dependency_digest:
                        status = "target_changed"
                    else:
                        prospective = _prospective(records, row.mutations)
                        if sum(map(len, prospective.values())) > MAX_RECORDS:
                            status = "target_changed"
                        try:
                            validate_state(prospective)
                            summaries = build_summaries(prospective)
                        except LocalPrimaryError:
                            status = "target_changed"
                        if status == "committed":
                            self._apply_mutations(s, row, now)
                            workspace.summaries = summaries
                workspace.version += 1
                receipt = LedgerReceipt(
                    workspace_id=self.binding.workspace_id,
                    request_id=request_id,
                    proposal_id=row.proposal_id,
                    epoch=row.epoch,
                    status=status,
                    version=workspace.version,
                    completed_at=now,
                    records=[
                        {
                            "entity": c["entity"],
                            "record_id": c["record_id"],
                            "revision": c["after"]["revision"],
                            "digest": _digest(c["after"]),
                        }
                        for c in row.mutations
                    ]
                    if status == "committed"
                    else [],
                )
                row.status = status
                s.add(receipt)
                s.flush()
                event = LedgerEvent(
                    workspace_id=self.binding.workspace_id,
                    event_id=str(uuid4()),
                    decision_id=request_id,
                    body={"status": status, "mutations": row.mutations},
                )
                s.add(event)
                s.flush()
                if status == "committed":
                    self._insert_outbox(s, event, receipt, row.mutations)
        return self.receipt(principal, request_id)

    def _read_workspace(self, s):
        workspace = s.get(LedgerWorkspace, self.binding.workspace_id)
        self._binding(workspace)
        records = self._records(s)
        if workspace.summaries != build_summaries(records):
            raise LocalPrimaryError("primary_verification_failed")
        return workspace, records

    def receipt(self, principal: LocalPrincipal, request_id: str) -> dict:
        self._require_owner(principal)
        _uuid(request_id)
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            with c.begin(), Session(bind=c) as s:
                s.execute(text("SET TRANSACTION READ ONLY"))
                workspace, records = self._read_workspace(s)
                row = s.get(LedgerReceipt, (self.binding.workspace_id, request_id))
                if row is None:
                    return {
                        "status": "unknown",
                        "request_id": request_id,
                        "verified_primary": False,
                    }
                current_records = []
                if workspace.version < row.version or workspace.epoch != row.epoch:
                    raise LocalPrimaryError("primary_verification_failed")
                for item in row.records:
                    current = _lookup(records, item["entity"], item["record_id"])
                    if (
                        current is None
                        or current["revision"] < item["revision"]
                        or (
                            current["revision"] == item["revision"]
                            and _digest(current) != item["digest"]
                        )
                    ):
                        raise LocalPrimaryError("primary_verification_failed")
                    current_records.append({"entity": item["entity"], "record": current})
                return _copy(
                    {
                        "primary_source": "postgresql",
                        "workspace_id": self.binding.workspace_id,
                        "authority_epoch": row.epoch,
                        "verified_primary": True,
                        "observed_version": workspace.version,
                        "records": current_records,
                        "summaries": workspace.summaries,
                        "receipt": {
                            "request_id": row.request_id,
                            "proposal_id": row.proposal_id,
                            "status": row.status,
                            "commit_version": row.version,
                            "records": row.records,
                            "committed_at": row.completed_at.isoformat()
                            if row.status == "committed"
                            else None,
                        },
                        "mirror": {
                            "status": "pending" if row.status == "committed" else "not_required"
                        },
                    }
                )

    def creation_request(self, principal: LocalPrincipal, request_id: str) -> dict:
        """Read-only recovery of a proposal by its creation request ID.

        Never expires, consumes or replays anything. A missing row is ``unknown``, not
        proof the creation never committed. Decision truth stays with ``receipt``.
        """
        self._require_owner(principal)
        _uuid(request_id)
        now = self._now()
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            with c.begin(), Session(bind=c) as s:
                s.execute(text("SET TRANSACTION READ ONLY"))
                workspace, _ = self._read_workspace(s)
                row = s.scalar(
                    select(LedgerProposal).where(
                        LedgerProposal.workspace_id == self.binding.workspace_id,
                        LedgerProposal.request_id == request_id,
                    )
                )
                if row is None:
                    return {
                        "status": "unknown",
                        "request_id": request_id,
                        "verified_primary": False,
                    }
                if (
                    row.owner_id != principal.owner_id
                    or row.epoch != workspace.epoch
                    or _digest(_envelope(row)) != row.envelope_digest
                ):
                    raise LocalPrimaryError("primary_verification_failed")
                receipt = s.scalar(
                    select(LedgerReceipt).where(
                        LedgerReceipt.workspace_id == self.binding.workspace_id,
                        LedgerReceipt.proposal_id == row.proposal_id,
                    )
                )
                if receipt is None:
                    if row.status != "pending":
                        raise LocalPrimaryError("primary_verification_failed")
                    decision = None
                else:
                    allowed = {
                        "committed": {row.approval_id},
                        "target_changed": {row.approval_id},
                        "rejected": {row.rejection_id},
                        "expired": {row.approval_id, row.rejection_id},
                    }
                    if (
                        receipt.status != row.status
                        or receipt.request_id not in allowed.get(receipt.status, ())
                        or receipt.epoch != row.epoch
                        or receipt.version > workspace.version
                    ):
                        raise LocalPrimaryError("primary_verification_failed")
                    decision = {"request_id": receipt.request_id, "status": receipt.status}
                return _copy(
                    {
                        "primary_source": "postgresql",
                        "workspace_id": self.binding.workspace_id,
                        "authority_epoch": workspace.epoch,
                        "verified_primary": True,
                        "observed_version": workspace.version,
                        "proposal": _envelope(row) | {"status": row.status},
                        # Only meaningful while undecided; terminal outcomes come from receipts.
                        "past_expiry": now >= row.expires_at if row.status == "pending" else None,
                        "decision": decision,
                    }
                )

    def snapshot(self, principal: LocalPrincipal, *, limit: int = 100, after: dict | None = None):
        self._require_owner(principal)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise LocalPrimaryError("invalid_page_limit")
        after = {} if after is None else after
        if type(after) is not dict or any(
            e not in ENTITIES or type(key) is not str for e, key in after.items()
        ):
            raise LocalPrimaryError("invalid_page_cursor")
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            with c.begin(), Session(bind=c) as s:
                s.execute(text("SET TRANSACTION READ ONLY"))
                workspace, records = self._read_workspace(s)
                page, cursors = {}, {}
                for entity in ENTITIES:
                    remaining = [
                        r for r in records[entity] if r[ID_KEYS[entity]] > after.get(entity, "")
                    ]
                    page[entity] = remaining[:limit]
                    cursors[entity] = (
                        page[entity][-1][ID_KEYS[entity]] if len(remaining) > limit else None
                    )
                return _copy(
                    {
                        "primary_source": "postgresql",
                        "authority_epoch": workspace.epoch,
                        "workspace_id": workspace.workspace_id,
                        "observed_version": workspace.version,
                        "coverage": "complete",
                        "records": page,
                        "next_cursor": cursors,
                        "record_counts": {e: len(records[e]) for e in ENTITIES},
                        "summaries": workspace.summaries,
                    }
                )
