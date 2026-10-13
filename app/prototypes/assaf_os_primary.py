"""Default-off, disposable-PostgreSQL-only task transaction experiment.

No routes, settings, provider clients, migration registration or background worker.
Principals are injected by local tests; this is not a new authentication mechanism.
The separate metadata below is deliberately excluded from application readiness.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

TASK_STATUSES = ("לביצוע", "בתהליך", "ממתין", "בוצע", "בוטל")
PATHS = ("לימוד", "אוטומציה")


class LocalPrimaryError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    title: str = Field(min_length=1, max_length=2000)
    status: str
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    path: str | None = None
    person_id: None = None
    work_id: None = None

    @field_validator("title")
    @classmethod
    def title_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title_required")
        return value.strip()

    @field_validator("status")
    @classmethod
    def status_known(cls, value: str) -> str:
        if value not in TASK_STATUSES:
            raise ValueError("task_status_invalid")
        return value

    @field_validator("path")
    @classmethod
    def path_known(cls, value: str | None) -> str | None:
        if value is not None and value not in PATHS:
            raise ValueError("task_path_invalid")
        return value


@dataclass(frozen=True)
class LocalBinding:
    workspace_id: str
    owner_id: str
    authority_epoch: int = 1


@dataclass(frozen=True)
class LocalPrincipal:
    owner_id: str
    source: Literal["OS", "MIA", "TELEGRAM_APPROVAL"]


class LocalBase(DeclarativeBase):
    pass


class Workspace(LocalBase):
    __tablename__ = "os_proto_workspaces"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(20))
    epoch: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (CheckConstraint("epoch > 0 AND version >= 0"),)


class Task(LocalBase):
    __tablename__ = "os_proto_tasks"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    inputs: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["os_proto_workspaces.workspace_id"]),
        CheckConstraint("revision > 0"),
    )


class Proposal(LocalBase):
    __tablename__ = "os_proto_proposals"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(20))
    epoch: Mapped[int] = mapped_column(BigInteger)
    request_id: Mapped[str] = mapped_column(String(36))
    request_digest: Mapped[str] = mapped_column(String(64))
    envelope_digest: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[str] = mapped_column(String(36))
    rejection_id: Mapped[str] = mapped_column(String(36))
    task_id: Mapped[str] = mapped_column(String(64))
    expected_revision: Mapped[int] = mapped_column(BigInteger)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(24))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["os_proto_workspaces.workspace_id"]),
        UniqueConstraint("workspace_id", "request_id"),
        UniqueConstraint("workspace_id", "approval_id"),
        UniqueConstraint("workspace_id", "rejection_id"),
        CheckConstraint("expected_revision >= 0"),
        CheckConstraint("approval_id <> rejection_id"),
        CheckConstraint("status IN ('pending','committed','rejected','expired','target_changed')"),
    )


class Receipt(LocalBase):
    __tablename__ = "os_proto_receipts"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64))
    epoch: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24))
    task_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(BigInteger)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "proposal_id"],
            ["os_proto_proposals.workspace_id", "os_proto_proposals.proposal_id"],
        ),
        UniqueConstraint("workspace_id", "proposal_id"),
        CheckConstraint("status IN ('committed','rejected','expired','target_changed')"),
        CheckConstraint("revision >= 0 AND version > 0"),
    )


class AuditEvent(LocalBase):
    __tablename__ = "os_proto_events"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(32))
    body: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["os_proto_receipts.workspace_id", "os_proto_receipts.request_id"],
        ),
        UniqueConstraint("workspace_id", "decision_id"),
    )


class Outbox(LocalBase):
    __tablename__ = "os_proto_outbox"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "event_id"],
            ["os_proto_events.workspace_id", "os_proto_events.event_id"],
        ),
        UniqueConstraint("workspace_id", "task_id", "revision"),
        UniqueConstraint("workspace_id", "version"),
        CheckConstraint("status = 'pending'"),
    )


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise LocalPrimaryError("invalid_request_id") from None
    return value


def _task(row: Task | None) -> dict | None:
    if row is None:
        return None
    return {"task_id": row.task_id, "revision": row.revision, "input": dict(row.inputs)}


def _envelope(row: Proposal) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "workspace_id",
            "proposal_id",
            "owner_id",
            "epoch",
            "request_id",
            "request_digest",
            "approval_id",
            "rejection_id",
            "task_id",
            "expected_revision",
            "before",
            "after",
            "source",
        )
    } | {"expires_at": row.expires_at.isoformat()}


def _proposal(row: Proposal) -> dict:
    # Serialization ensures callers cannot mutate a stored JSON object through a returned reference.
    return json.loads(json.dumps(_envelope(row) | {"status": row.status}))


class LocalTaskPrimary:
    """Explicit local invocation only. All methods refuse the default disabled mode."""

    def __init__(
        self,
        engine: Engine,
        binding: LocalBinding,
        *,
        enabled: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.engine, self.binding, self.enabled, self.clock = engine, binding, enabled, clock

    def _guard(self):
        if self.enabled is not True:
            raise LocalPrimaryError("prototype_disabled")
        b, url = self.binding, self.engine.url
        if (
            url.get_backend_name() != "postgresql"
            or url.host not in {"127.0.0.1", "localhost", "::1"}
            or not (url.database or "").startswith("assaf_os_local_")
            or url.query
        ):
            raise LocalPrimaryError("disposable_loopback_database_required")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", b.workspace_id)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", b.owner_id)
            or type(b.authority_epoch) is not int
            or b.authority_epoch <= 0
        ):
            raise LocalPrimaryError("invalid_local_binding")

    def _require_owner(self, principal: LocalPrincipal, *, deciding: bool = False):
        self._guard()
        allowed = {"OS", "TELEGRAM_APPROVAL"} if deciding else {"OS", "MIA", "TELEGRAM_APPROVAL"}
        if (
            not isinstance(principal, LocalPrincipal)
            or type(principal.owner_id) is not str
            or type(principal.source) is not str
            or principal.owner_id != self.binding.owner_id
            or principal.source not in allowed
        ):
            raise LocalPrimaryError("owner_approval_required" if deciding else "owner_required")

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise LocalPrimaryError("aware_clock_required")
        return now.astimezone(UTC)

    def initialize_local_schema(self):
        self._guard()
        with self.engine.connect() as c:
            db, marker = c.execute(
                text("SELECT current_database(), current_setting('assaf_os.local_prototype', true)")
            ).one()
            if db != self.engine.url.database or marker != "disposable_only":
                raise LocalPrimaryError("disposable_loopback_database_required")
        LocalBase.metadata.create_all(self.engine)
        with Session(self.engine) as s, s.begin():
            row = s.get(Workspace, self.binding.workspace_id)
            if row is None:
                s.add(
                    Workspace(
                        workspace_id=self.binding.workspace_id,
                        owner_id=self.binding.owner_id,
                        epoch=self.binding.authority_epoch,
                        version=0,
                    )
                )
            else:
                self._binding(row)

    def _binding(self, row: Workspace | None):
        if (
            row is None
            or row.owner_id != self.binding.owner_id
            or row.epoch != self.binding.authority_epoch
        ):
            raise LocalPrimaryError("authority_changed")

    def _workspace(self, s: Session) -> Workspace:
        row = s.scalar(
            select(Workspace)
            .where(
                Workspace.workspace_id == self.binding.workspace_id,
            )
            .with_for_update()
        )
        self._binding(row)
        return row

    def propose(
        self,
        principal: LocalPrincipal,
        request_id: str,
        inputs: TaskInput,
        *,
        task_id: str | None = None,
        expected_revision: int = 0,
    ) -> dict:
        self._require_owner(principal)
        if principal.source not in {"OS", "MIA"}:
            raise LocalPrimaryError("proposal_source_required")
        _uuid(request_id)
        if not isinstance(inputs, TaskInput):
            raise LocalPrimaryError("validated_task_input_required")
        if type(expected_revision) is not int or expected_revision < 0:
            raise LocalPrimaryError("invalid_revision")
        if task_id is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,63}", task_id):
            raise LocalPrimaryError("invalid_task_id")
        # Revalidate even a Pydantic model built with model_construct/model_copy bypasses.
        values = TaskInput.model_validate_json(inputs.model_dump_json()).model_dump(mode="json")
        digest = _digest(
            {
                "workspace": self.binding.workspace_id,
                "owner": principal.owner_id,
                "epoch": self.binding.authority_epoch,
                "source": principal.source,
                "task_id": task_id,
                "expected_revision": expected_revision,
                "input": values,
            }
        )
        with Session(self.engine) as s, s.begin():
            self._workspace(s)
            existing = s.scalar(
                select(Proposal).where(
                    Proposal.workspace_id == self.binding.workspace_id,
                    Proposal.request_id == request_id,
                )
            )
            if existing:
                if existing.request_digest != digest:
                    raise LocalPrimaryError("idempotency_mismatch")
                return _proposal(existing)
            current = s.get(Task, (self.binding.workspace_id, task_id)) if task_id else None
            if (task_id is None and expected_revision != 0) or (
                task_id is not None and (current is None or current.revision != expected_revision)
            ):
                raise LocalPrimaryError("target_changed")
            target = task_id or "TAS_" + uuid4().hex
            row = Proposal(
                workspace_id=self.binding.workspace_id,
                proposal_id="PRO_" + uuid4().hex,
                owner_id=principal.owner_id,
                epoch=self.binding.authority_epoch,
                request_id=request_id,
                request_digest=digest,
                approval_id=str(uuid4()),
                rejection_id=str(uuid4()),
                task_id=target,
                expected_revision=expected_revision,
                before=_task(current),
                after={"task_id": target, "revision": expected_revision + 1, "input": values},
                source=principal.source,
                expires_at=self._now() + timedelta(minutes=10),
                status="pending",
            )
            row.envelope_digest = _digest(_envelope(row))
            s.add(row)
            return _proposal(row)

    def _insert_outbox(self, s: Session, event: AuditEvent, receipt: Receipt, after: dict):
        s.add(
            Outbox(
                workspace_id=event.workspace_id,
                event_id=event.event_id,
                task_id=receipt.task_id,
                revision=receipt.revision,
                version=receipt.version,
                body=after,
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
        if decision not in {"approve", "reject"}:
            raise LocalPrimaryError("invalid_decision")
        with Session(self.engine) as s, s.begin():
            workspace = self._workspace(s)
            row = s.get(Proposal, (self.binding.workspace_id, proposal_id))
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
                select(Receipt).where(
                    Receipt.workspace_id == self.binding.workspace_id,
                    Receipt.proposal_id == proposal_id,
                )
            )
            if previous is not None:
                if previous.request_id != request_id:
                    raise LocalPrimaryError("already_decided")
            else:
                if row.status != "pending":
                    raise LocalPrimaryError("proposal_integrity_failed")
                now, current = self._now(), s.get(Task, (self.binding.workspace_id, row.task_id))
                status = "rejected" if decision == "reject" else "committed"
                if now >= row.expires_at:
                    status = "expired"
                elif decision == "approve" and _task(current) != row.before:
                    status = "target_changed"
                if status == "committed":
                    if current is None:
                        current = Task(
                            workspace_id=self.binding.workspace_id,
                            task_id=row.task_id,
                            created_at=now,
                        )
                        s.add(current)
                    current.revision, current.inputs = row.after["revision"], row.after["input"]
                    current.source, current.updated_at = row.source, now
                workspace.version += 1
                receipt = Receipt(
                    workspace_id=self.binding.workspace_id,
                    request_id=request_id,
                    proposal_id=proposal_id,
                    epoch=row.epoch,
                    status=status,
                    task_id=row.task_id,
                    revision=row.after["revision"]
                    if status == "committed"
                    else row.expected_revision,
                    version=workspace.version,
                    completed_at=now,
                    input_digest=_digest(row.after["input"]) if status == "committed" else None,
                )
                row.status = status
                s.add(receipt)
                s.flush()
                event = AuditEvent(
                    workspace_id=self.binding.workspace_id,
                    event_id=str(uuid4()),
                    decision_id=request_id,
                    kind="task_" + status,
                    body={"before": row.before, "after": row.after, "status": status},
                )
                s.add(event)
                s.flush()
                if status == "committed":
                    self._insert_outbox(s, event, receipt, row.after)
        # This method runs after successful COMMIT and uses a separate consistent primary read.
        return self.receipt(principal, request_id)

    def receipt(self, principal: LocalPrincipal, request_id: str) -> dict:
        self._require_owner(principal)
        _uuid(request_id)
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
            with c.begin(), Session(bind=c) as s:
                s.execute(text("SET TRANSACTION READ ONLY"))
                workspace = s.get(Workspace, self.binding.workspace_id)
                self._binding(workspace)
                row = s.get(Receipt, (self.binding.workspace_id, request_id))
                if row is None:
                    return {
                        "status": "unknown",
                        "request_id": request_id,
                        "verified_primary": False,
                    }
                current = s.get(Task, (self.binding.workspace_id, row.task_id))
                if row.status == "committed" and (
                    current is None
                    or current.revision < row.revision
                    or workspace.version < row.version
                    or (
                        current.revision == row.revision
                        and _digest(current.inputs) != row.input_digest
                    )
                ):
                    raise LocalPrimaryError("primary_verification_failed")
                return {
                    "primary_source": "postgresql",
                    "workspace_id": self.binding.workspace_id,
                    "authority_epoch": row.epoch,
                    "verified_primary": True,
                    "observed_version": workspace.version,
                    "task": _task(current),
                    "receipt": {
                        "request_id": row.request_id,
                        "proposal_id": row.proposal_id,
                        "status": row.status,
                        "task_id": row.task_id,
                        "revision": row.revision,
                        "commit_version": row.version,
                        "committed_at": row.completed_at.isoformat()
                        if row.status == "committed"
                        else None,
                    },
                    "mirror": {
                        "status": "pending" if row.status == "committed" else "not_required"
                    },
                }
