"""Bounded synthetic-only staging and reconciliation for the local OS prototype.

This module deliberately has its own metadata and tables.  It never writes to the
business ledger, creates approval receipts, or loads files/provider exports.  The
``synthetic:`` marker is only a fixture boundary; it is not proof about real data.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.prototypes.assaf_os_domain import (
    ENTITIES,
    ID_KEYS,
    SCHEMA,
    canonical_input,
    validate_state,
)
from app.prototypes.assaf_os_primary import (
    LocalBinding,
    LocalPrimaryError,
    LocalPrincipal,
    LocalTaskPrimary,
    _digest,
)
from app.prototypes.assaf_os_summaries import SummaryInputError, build_summaries

MAX_RECORDS = 2_000
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 100
MAX_JSON_NODES = 100_000
MAX_SAFE_INTEGER = 9_007_199_254_740_991
DEFAULT_CHUNK_SIZE = 200
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_BATCH_KEYS = {
    "schema_version",
    "source_id",
    "source_epoch",
    "workspace_id",
    "authority_epoch",
    "batch_id",
    "observed_at",
    "coverage",
    "records",
    "provenance",
}
_PROVENANCE_KEYS = {
    "source_input_digest",
    "accepted_input_digest",
    "accepted_revision",
    "acceptance_kind",
    "source_snapshot_version",
}


class ShadowBase(DeclarativeBase):
    """Metadata isolated from both the application and business-ledger bases."""


class ShadowWorkspace(ShadowBase):
    __tablename__ = "os_local_shadow_workspaces"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(20))
    authority_epoch: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint("authority_epoch > 0"),)


class ShadowBatch(ShadowBase):
    __tablename__ = "os_local_shadow_batches"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload_fingerprint: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(128))
    source_id: Mapped[str] = mapped_column(String(256))
    source_epoch: Mapped[int] = mapped_column(BigInteger)
    authority_epoch: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage: Mapped[dict] = mapped_column(JSONB)
    expected_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    total_records: Mapped[int] = mapped_column(BigInteger)
    stage_status: Mapped[str] = mapped_column(String(24), default="staging")
    reconcile_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id"], ["os_local_shadow_workspaces.workspace_id"]),
        CheckConstraint("source_epoch > 0 AND authority_epoch > 0 AND total_records >= 0"),
        CheckConstraint("stage_status IN ('staging','complete')"),
        CheckConstraint(
            "reconcile_status IS NULL OR reconcile_status IN ('accepted','quarantined')"
        ),
    )


class ShadowRecord(ShadowBase):
    __tablename__ = "os_local_shadow_records"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity: Mapped[str] = mapped_column(String(24))
    record_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_record: Mapped[Any] = mapped_column(JSONB)
    raw_record_digest: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["os_local_shadow_batches.workspace_id", "os_local_shadow_batches.batch_id"],
        ),
        CheckConstraint("ordinal >= 0"),
    )


class ShadowProvenance(ShadowBase):
    __tablename__ = "os_local_shadow_provenance"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accepted_input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accepted_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    acceptance_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_snapshot_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_provenance: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "batch_id", "ordinal"],
            [
                "os_local_shadow_records.workspace_id",
                "os_local_shadow_records.batch_id",
                "os_local_shadow_records.ordinal",
            ],
        ),
    )


class ShadowIssue(ShadowBase):
    __tablename__ = "os_local_shadow_issues"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    issue_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    ordinal: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    entity: Mapped[str | None] = mapped_column(String(24), nullable=True)
    record_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["os_local_shadow_batches.workspace_id", "os_local_shadow_batches.batch_id"],
        ),
    )


class ShadowCheckpoint(ShadowBase):
    __tablename__ = "os_local_shadow_checkpoints"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    next_ordinal: Mapped[int] = mapped_column(BigInteger, default=0)
    total_records: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="staging")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "batch_id"],
            ["os_local_shadow_batches.workspace_id", "os_local_shadow_batches.batch_id"],
        ),
        CheckConstraint(
            "next_ordinal >= 0 AND total_records >= 0 AND next_ordinal <= total_records"
        ),
        CheckConstraint("status IN ('staging','complete')"),
    )


@dataclass(frozen=True)
class _Issue:
    reason_code: str
    ordinal: int | None = None
    entity: str | None = None
    record_id: str | None = None
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class _PreparedRecord:
    ordinal: int
    entity: str
    record_id: str | None
    revision: int | None
    raw_record: Any
    provenance: Any
    issues: tuple[_Issue, ...]


@dataclass(frozen=True)
class _PreparedBatch:
    batch: dict[str, Any]
    fingerprint: str
    observed_at: datetime
    records: tuple[_PreparedRecord, ...]
    initial_issues: tuple[_Issue, ...]


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _validate_json_payload(value: Any) -> None:
    """Validate JSON types iteratively so hostile depth cannot hit recursion limits."""

    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise LocalPrimaryError("batch_json_too_complex")
        if item is None or type(item) in {bool, int}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise LocalPrimaryError("batch_not_json")
            continue
        if type(item) is str:
            if "\x00" in item:
                raise LocalPrimaryError("batch_not_json")
            continue
        if type(item) is list:
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or "\x00" in key:
                    raise LocalPrimaryError("batch_not_json")
                pending.append((child, depth + 1))
            continue
        raise LocalPrimaryError("batch_not_json")


def _canonical_uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise LocalPrimaryError("invalid_observed_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise LocalPrimaryError("invalid_observed_at") from None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset().total_seconds() != 0
    ):
        raise LocalPrimaryError("observed_at_must_be_utc")
    return parsed.astimezone(UTC)


def _record_input(entity: str, raw: dict[str, Any]) -> dict[str, Any]:
    excluded = {ID_KEYS[entity], "revision"}
    if entity == "leads":
        excluded.add("person_id")
    return canonical_input(
        entity, {key: value for key, value in raw.items() if key not in excluded}
    )


def _issue_key(issue: _Issue) -> str:
    return _digest(
        {
            "reason_code": issue.reason_code,
            "ordinal": issue.ordinal,
            "entity": issue.entity,
            "record_id": issue.record_id,
            "detail": issue.detail or {},
        }
    )


class LocalShadowImporter:
    """Default-off staging facade for bounded, explicitly synthetic fixtures."""

    def __init__(
        self,
        engine: Engine,
        binding: LocalBinding,
        *,
        enabled: bool = False,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.engine = engine
        self.binding = binding
        self.enabled = enabled
        self._local_guard = LocalTaskPrimary(engine, binding, enabled=enabled)
        if type(chunk_size) is not int or not 1 <= chunk_size <= MAX_RECORDS:
            raise LocalPrimaryError("invalid_chunk_size")
        self.chunk_size = chunk_size

    def _guard(self) -> None:
        self._local_guard._guard()
        if self.binding.authority_epoch > MAX_SAFE_INTEGER:
            raise LocalPrimaryError("invalid_local_binding")

    def _require_shadow_owner(self, principal: LocalPrincipal) -> None:
        self._local_guard._require_owner(principal)
        if principal.source not in {"OS", "MIA", "TELEGRAM_APPROVAL"}:
            raise LocalPrimaryError("owner_required")

    def _workspace(self, session: Session, *, lock: bool = False) -> ShadowWorkspace:
        query = select(ShadowWorkspace).where(
            ShadowWorkspace.workspace_id == self.binding.workspace_id
        )
        row = session.scalar(query.with_for_update() if lock else query)
        if (
            row is None
            or row.owner_id != self.binding.owner_id
            or row.authority_epoch != self.binding.authority_epoch
        ):
            raise LocalPrimaryError("authority_changed")
        return row

    def initialize_local_schema(self) -> None:
        self._guard()
        with self.engine.begin() as connection:
            database, marker = connection.execute(
                text("SELECT current_database(), current_setting('assaf_os.local_prototype', true)")
            ).one()
            if database != self.engine.url.database or marker != "disposable_only":
                raise LocalPrimaryError("disposable_loopback_database_required")
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('assaf_os_shadow_schema'))")
            )
            ShadowBase.metadata.create_all(connection)
            connection.execute(
                insert(ShadowWorkspace)
                .values(
                    workspace_id=self.binding.workspace_id,
                    owner_id=self.binding.owner_id,
                    authority_epoch=self.binding.authority_epoch,
                )
                .on_conflict_do_nothing(index_elements=["workspace_id"])
            )
            row = connection.execute(
                select(
                    ShadowWorkspace.owner_id,
                    ShadowWorkspace.authority_epoch,
                ).where(ShadowWorkspace.workspace_id == self.binding.workspace_id)
            ).one()
            if row != (self.binding.owner_id, self.binding.authority_epoch):
                raise LocalPrimaryError("authority_changed")

    def _prepare(self, batch: Any) -> _PreparedBatch:
        if type(batch) is not dict:
            raise LocalPrimaryError("invalid_batch_shape")
        allowed = _BATCH_KEYS | {"expected_summary"}
        keys = set(batch)
        if keys != _BATCH_KEYS and keys != allowed:
            raise LocalPrimaryError("invalid_batch_shape")
        _validate_json_payload(batch)
        try:
            encoded = json.dumps(
                batch,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError):
            raise LocalPrimaryError("batch_not_json") from None
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise LocalPrimaryError("batch_payload_too_large")
        if batch["schema_version"] != SCHEMA["version"]:
            raise LocalPrimaryError("schema_version_mismatch")
        if (
            not isinstance(batch["source_id"], str)
            or len(batch["source_id"]) > 256
            or not batch["source_id"].startswith(("synthetic:", "owner_export:"))
        ):
            raise LocalPrimaryError("synthetic_source_required")
        if (
            type(batch["source_epoch"]) is not int
            or not 0 < batch["source_epoch"] <= MAX_SAFE_INTEGER
        ):
            raise LocalPrimaryError("invalid_source_epoch")
        if batch["workspace_id"] != self.binding.workspace_id:
            raise LocalPrimaryError("workspace_mismatch")
        if (
            type(batch["authority_epoch"]) is not int
            or not 0 < batch["authority_epoch"] <= MAX_SAFE_INTEGER
            or batch["authority_epoch"] != self.binding.authority_epoch
        ):
            raise LocalPrimaryError("authority_changed")
        if not _canonical_uuid(batch["batch_id"]):
            raise LocalPrimaryError("invalid_batch_id")
        observed_at = _parse_utc(batch["observed_at"])
        coverage = batch["coverage"]
        records = batch["records"]
        provenance = batch["provenance"]
        if (
            type(coverage) is not dict
            or set(coverage) != set(ENTITIES)
            or any(
                type(value) is not str or value not in {"complete", "partial", "unknown"}
                for value in coverage.values()
            )
            or type(records) is not dict
            or set(records) != set(ENTITIES)
            or any(type(records[entity]) is not list for entity in ENTITIES)
            or type(provenance) is not dict
            or set(provenance) != set(ENTITIES)
            or any(type(provenance[entity]) is not dict for entity in ENTITIES)
            or ("expected_summary" in batch and type(batch["expected_summary"]) is not dict)
        ):
            raise LocalPrimaryError("invalid_batch_shape")
        total = sum(len(records[entity]) for entity in ENTITIES)
        if total > MAX_RECORDS:
            raise LocalPrimaryError("shadow_record_limit")
        fingerprint = _digest(batch)

        prepared: list[_PreparedRecord] = []
        initial: list[_Issue] = []
        seen: dict[str, set[str]] = {entity: set() for entity in ENTITIES}
        ordinal = 0
        for entity in ENTITIES:
            id_key = ID_KEYS[entity]
            used_provenance: set[str] = set()
            for raw in records[entity]:
                issues: list[_Issue] = []
                record_id = raw.get(id_key) if type(raw) is dict else None
                revision = raw.get("revision") if type(raw) is dict else None
                if type(raw) is not dict:
                    issues.append(_Issue("invalid_record", ordinal, entity))
                if not isinstance(record_id, str) or not record_id or len(record_id) > 256:
                    issues.append(_Issue("missing_or_invalid_record_id", ordinal, entity))
                    record_id = None
                elif record_id in seen[entity]:
                    issues.append(_Issue("duplicate_record_id", ordinal, entity, record_id))
                else:
                    seen[entity].add(record_id)
                if type(revision) is not int or not 0 < revision <= 9_007_199_254_740_991:
                    issues.append(_Issue("missing_or_invalid_revision", ordinal, entity, record_id))
                    revision = None
                meta = provenance[entity].get(record_id) if record_id is not None else None
                if record_id is not None and record_id in provenance[entity]:
                    used_provenance.add(record_id)
                if type(meta) is not dict or set(meta) != _PROVENANCE_KEYS:
                    issues.append(
                        _Issue("provenance_missing_or_invalid", ordinal, entity, record_id)
                    )
                else:
                    if _DIGEST_RE.fullmatch(str(meta["source_input_digest"])) is None:
                        issues.append(
                            _Issue("invalid_source_input_digest", ordinal, entity, record_id)
                        )
                    if _DIGEST_RE.fullmatch(str(meta["accepted_input_digest"])) is None:
                        issues.append(
                            _Issue("invalid_accepted_input_digest", ordinal, entity, record_id)
                        )
                    if (
                        type(meta["accepted_revision"]) is not int
                        or not 0 < meta["accepted_revision"] <= 9_007_199_254_740_991
                    ):
                        issues.append(
                            _Issue("invalid_accepted_revision", ordinal, entity, record_id)
                        )
                    elif revision is not None and meta["accepted_revision"] != revision:
                        issues.append(
                            _Issue("accepted_revision_mismatch", ordinal, entity, record_id)
                        )
                    if meta["acceptance_kind"] != "synthetic_native_acceptance":
                        issues.append(_Issue("unaccepted_record", ordinal, entity, record_id))
                    if (
                        type(meta["source_snapshot_version"]) is not int
                        or not 0 < meta["source_snapshot_version"] <= 9_007_199_254_740_991
                    ):
                        issues.append(
                            _Issue("invalid_source_snapshot_version", ordinal, entity, record_id)
                        )
                if type(raw) is dict:
                    try:
                        accepted = _record_input(entity, raw)
                        accepted_digest = _digest(accepted)
                    except LocalPrimaryError as exc:
                        issues.append(
                            _Issue(
                                "record_validation_failed",
                                ordinal,
                                entity,
                                record_id,
                                {"domain_code": exc.code},
                            )
                        )
                    else:
                        if (
                            type(meta) is dict
                            and meta.get("source_input_digest") != accepted_digest
                        ):
                            issues.append(
                                _Issue("source_input_changed", ordinal, entity, record_id)
                            )
                        if (
                            type(meta) is dict
                            and meta.get("accepted_input_digest") != accepted_digest
                        ):
                            issues.append(
                                _Issue("accepted_input_changed", ordinal, entity, record_id)
                            )
                prepared.append(
                    _PreparedRecord(
                        ordinal,
                        entity,
                        record_id,
                        revision,
                        _copy(raw),
                        _copy(meta),
                        tuple(issues),
                    )
                )
                ordinal += 1
            for orphan in sorted(set(provenance[entity]) - used_provenance):
                initial.append(_Issue("orphan_provenance", None, entity, orphan))
            if coverage[entity] != "complete":
                initial.append(
                    _Issue(
                        "coverage_" + coverage[entity],
                        None,
                        entity,
                        None,
                        {"coverage": coverage[entity]},
                    )
                )
        return _PreparedBatch(
            _copy(batch), fingerprint, observed_at, tuple(prepared), tuple(initial)
        )

    def _lock_batch(self, session: Session, batch_id: str) -> None:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": self.binding.workspace_id + ":" + batch_id},
        )

    def _insert_issue(self, session: Session, batch_id: str, issue: _Issue) -> None:
        stored_record_id = (
            issue.record_id
            if isinstance(issue.record_id, str) and len(issue.record_id) <= 256
            else None
        )
        detail = dict(issue.detail or {})
        if issue.record_id is not None and stored_record_id is None:
            detail["raw_record_id"] = issue.record_id
        session.execute(
            insert(ShadowIssue)
            .values(
                workspace_id=self.binding.workspace_id,
                batch_id=batch_id,
                issue_key=_issue_key(issue),
                ordinal=issue.ordinal,
                entity=issue.entity,
                record_id=stored_record_id,
                reason_code=issue.reason_code,
                detail=detail,
            )
            .on_conflict_do_nothing()
        )

    def _before_checkpoint(self, batch_id: str, start: int, end: int) -> None:
        """Test seam called inside a chunk transaction; raising proves rollback."""

    def _after_chunk(self, batch_id: str, next_ordinal: int) -> None:
        """Test seam called after a committed chunk; raising proves resumability."""

    def stage(self, principal: LocalPrincipal, batch: dict[str, Any]) -> dict[str, Any]:
        self._require_shadow_owner(principal)
        prepared = self._prepare(batch)  # Header/size rejection happens before DB checkout.
        batch_id = prepared.batch["batch_id"]
        now = datetime.now(UTC)
        duplicate = False
        with Session(self.engine) as session, session.begin():
            self._workspace(session, lock=True)
            self._lock_batch(session, batch_id)
            row = session.get(ShadowBatch, (self.binding.workspace_id, batch_id))
            if row is None:
                row = ShadowBatch(
                    workspace_id=self.binding.workspace_id,
                    batch_id=batch_id,
                    payload_fingerprint=prepared.fingerprint,
                    schema_version=prepared.batch["schema_version"],
                    source_id=prepared.batch["source_id"],
                    source_epoch=prepared.batch["source_epoch"],
                    authority_epoch=prepared.batch["authority_epoch"],
                    observed_at=prepared.observed_at,
                    coverage=prepared.batch["coverage"],
                    expected_summary=prepared.batch.get("expected_summary"),
                    total_records=len(prepared.records),
                    stage_status="complete" if not prepared.records else "staging",
                    manifest={"raw_payload": prepared.batch},
                    created_at=now,
                )
                session.add(row)
                session.add(
                    ShadowCheckpoint(
                        workspace_id=self.binding.workspace_id,
                        batch_id=batch_id,
                        next_ordinal=0,
                        total_records=len(prepared.records),
                        status="complete" if not prepared.records else "staging",
                        updated_at=now,
                    )
                )
                session.flush()
                for issue in prepared.initial_issues:
                    self._insert_issue(session, batch_id, issue)
            elif row.payload_fingerprint != prepared.fingerprint:
                raise LocalPrimaryError("batch_payload_mismatch")
            else:
                checkpoint = session.get(ShadowCheckpoint, (self.binding.workspace_id, batch_id))
                self._verify_persisted_batch(session, row, checkpoint)
                duplicate = True

        while True:
            with Session(self.engine) as session, session.begin():
                self._workspace(session, lock=True)
                self._lock_batch(session, batch_id)
                row = session.get(ShadowBatch, (self.binding.workspace_id, batch_id))
                checkpoint = session.get(
                    ShadowCheckpoint,
                    (self.binding.workspace_id, batch_id),
                    with_for_update=True,
                )
                if (
                    row is None
                    or checkpoint is None
                    or row.payload_fingerprint != prepared.fingerprint
                ):
                    raise LocalPrimaryError("shadow_batch_integrity_failed")
                self._verify_persisted_batch(session, row, checkpoint)
                if checkpoint.next_ordinal == checkpoint.total_records:
                    if checkpoint.status != "complete" or row.stage_status != "complete":
                        checkpoint.status = row.stage_status = "complete"
                        checkpoint.updated_at = datetime.now(UTC)
                    break
                start = checkpoint.next_ordinal
                end = min(start + self.chunk_size, checkpoint.total_records)
                for item in prepared.records[start:end]:
                    session.add(
                        ShadowRecord(
                            workspace_id=self.binding.workspace_id,
                            batch_id=batch_id,
                            ordinal=item.ordinal,
                            entity=item.entity,
                            record_id=item.record_id,
                            revision=item.revision,
                            raw_record=item.raw_record,
                            raw_record_digest=_digest(item.raw_record),
                        )
                    )
                # The tables intentionally have no ORM relationships.  Flush the
                # parents explicitly so PostgreSQL never observes provenance first.
                session.flush()
                for item in prepared.records[start:end]:
                    meta = self._stored_provenance(item.provenance)
                    session.add(
                        ShadowProvenance(
                            workspace_id=self.binding.workspace_id,
                            batch_id=batch_id,
                            ordinal=item.ordinal,
                            source_input_digest=meta["source_input_digest"],
                            accepted_input_digest=meta["accepted_input_digest"],
                            accepted_revision=meta["accepted_revision"],
                            acceptance_kind=meta["acceptance_kind"],
                            source_snapshot_version=meta["source_snapshot_version"],
                            raw_provenance=item.provenance,
                        )
                    )
                    for issue in item.issues:
                        self._insert_issue(session, batch_id, issue)
                self._before_checkpoint(batch_id, start, end)
                checkpoint.next_ordinal = end
                checkpoint.updated_at = datetime.now(UTC)
                if end == checkpoint.total_records:
                    checkpoint.status = row.stage_status = "complete"
            self._after_chunk(batch_id, end)

        return self._stage_result(batch_id, duplicate=duplicate)

    def _stage_result(self, batch_id: str, *, duplicate: bool) -> dict[str, Any]:
        with Session(self.engine) as session:
            batch = session.get(ShadowBatch, (self.binding.workspace_id, batch_id))
            checkpoint = session.get(ShadowCheckpoint, (self.binding.workspace_id, batch_id))
            self._verify_persisted_batch(session, batch, checkpoint)
            issue_count = len(
                session.scalars(
                    select(ShadowIssue).where(
                        ShadowIssue.workspace_id == self.binding.workspace_id,
                        ShadowIssue.batch_id == batch_id,
                    )
                ).all()
            )
            if batch is None or checkpoint is None:
                raise LocalPrimaryError("batch_not_found")
            return {
                "workspace_id": self.binding.workspace_id,
                "batch_id": batch_id,
                "status": batch.stage_status,
                "payload_fingerprint": batch.payload_fingerprint,
                "total_records": batch.total_records,
                "staged_records": checkpoint.next_ordinal,
                "issue_count": issue_count,
                "duplicate": duplicate,
                "checkpoint": {
                    "next_ordinal": checkpoint.next_ordinal,
                    "total_records": checkpoint.total_records,
                    "status": checkpoint.status,
                },
            }

    def _rows(self, session: Session, batch_id: str) -> list[ShadowRecord]:
        return list(
            session.scalars(
                select(ShadowRecord)
                .where(
                    ShadowRecord.workspace_id == self.binding.workspace_id,
                    ShadowRecord.batch_id == batch_id,
                )
                .order_by(ShadowRecord.ordinal)
            )
        )

    def _provenance_rows(self, session: Session, batch_id: str) -> list[ShadowProvenance]:
        return list(
            session.scalars(
                select(ShadowProvenance)
                .where(
                    ShadowProvenance.workspace_id == self.binding.workspace_id,
                    ShadowProvenance.batch_id == batch_id,
                )
                .order_by(ShadowProvenance.ordinal)
            )
        )

    @staticmethod
    def _stored_provenance(meta: Any) -> dict[str, Any]:
        value = meta if type(meta) is dict else {}
        source_digest = value.get("source_input_digest")
        accepted_digest = value.get("accepted_input_digest")
        accepted_revision = value.get("accepted_revision")
        acceptance_kind = value.get("acceptance_kind")
        snapshot_version = value.get("source_snapshot_version")
        return {
            "source_input_digest": source_digest
            if isinstance(source_digest, str) and _DIGEST_RE.fullmatch(source_digest)
            else None,
            "accepted_input_digest": accepted_digest
            if isinstance(accepted_digest, str) and _DIGEST_RE.fullmatch(accepted_digest)
            else None,
            "accepted_revision": accepted_revision
            if type(accepted_revision) is int and 0 < accepted_revision <= 9_007_199_254_740_991
            else None,
            "acceptance_kind": acceptance_kind
            if isinstance(acceptance_kind, str) and len(acceptance_kind) <= 64
            else None,
            "source_snapshot_version": snapshot_version
            if type(snapshot_version) is int and 0 < snapshot_version <= 9_007_199_254_740_991
            else None,
        }

    def _verify_persisted_batch(
        self,
        session: Session,
        batch: ShadowBatch | None,
        checkpoint: ShadowCheckpoint | None,
    ) -> tuple[list[ShadowRecord], list[ShadowProvenance]]:
        if batch is None or checkpoint is None:
            raise LocalPrimaryError("shadow_batch_integrity_failed")
        if batch.authority_epoch != self.binding.authority_epoch:
            raise LocalPrimaryError("authority_changed")
        if (
            checkpoint.total_records != batch.total_records
            or not 0 <= checkpoint.next_ordinal <= checkpoint.total_records
            or (checkpoint.status == "complete")
            != (checkpoint.next_ordinal == checkpoint.total_records)
            or (batch.stage_status == "complete") != (checkpoint.status == "complete")
        ):
            raise LocalPrimaryError("shadow_batch_integrity_failed")
        envelope = batch.manifest
        if type(envelope) is not dict or type(envelope.get("raw_payload")) is not dict:
            raise LocalPrimaryError("shadow_batch_integrity_failed")
        raw_payload = envelope["raw_payload"]
        try:
            prepared = self._prepare(raw_payload)
        except LocalPrimaryError:
            raise LocalPrimaryError("shadow_batch_integrity_failed") from None
        if (
            prepared.fingerprint != batch.payload_fingerprint
            or _digest(raw_payload) != batch.payload_fingerprint
            or batch.schema_version != raw_payload["schema_version"]
            or batch.source_id != raw_payload["source_id"]
            or batch.source_epoch != raw_payload["source_epoch"]
            or batch.authority_epoch != raw_payload["authority_epoch"]
            or batch.workspace_id != raw_payload["workspace_id"]
            or batch.observed_at.astimezone(UTC) != prepared.observed_at
            or batch.coverage != raw_payload["coverage"]
            or batch.expected_summary != raw_payload.get("expected_summary")
            or batch.total_records != len(prepared.records)
        ):
            raise LocalPrimaryError("shadow_batch_integrity_failed")
        rows = self._rows(session, batch.batch_id)
        provenance_rows = self._provenance_rows(session, batch.batch_id)
        expected_count = checkpoint.next_ordinal
        if (
            len(rows) != expected_count
            or len(provenance_rows) != expected_count
            or [row.ordinal for row in rows] != list(range(expected_count))
            or [row.ordinal for row in provenance_rows] != list(range(expected_count))
        ):
            raise LocalPrimaryError("shadow_batch_integrity_failed")
        for row, provenance, expected in zip(
            rows, provenance_rows, prepared.records[:expected_count], strict=True
        ):
            normalized = self._stored_provenance(expected.provenance)
            if (
                row.entity != expected.entity
                or row.record_id != expected.record_id
                or row.revision != expected.revision
                or row.raw_record != expected.raw_record
                or row.raw_record_digest != _digest(expected.raw_record)
                or provenance.raw_provenance != expected.provenance
                or provenance.source_input_digest != normalized["source_input_digest"]
                or provenance.accepted_input_digest != normalized["accepted_input_digest"]
                or provenance.accepted_revision != normalized["accepted_revision"]
                or provenance.acceptance_kind != normalized["acceptance_kind"]
                or provenance.source_snapshot_version != normalized["source_snapshot_version"]
            ):
                raise LocalPrimaryError("shadow_batch_integrity_failed")
        expected_issues = {
            _issue_key(issue)
            for issue in prepared.initial_issues
            if issue.ordinal is None or issue.ordinal < expected_count
        }
        for item in prepared.records[:expected_count]:
            expected_issues.update(_issue_key(issue) for issue in item.issues)
        stored_issues = set(
            session.scalars(
                select(ShadowIssue.issue_key).where(
                    ShadowIssue.workspace_id == self.binding.workspace_id,
                    ShadowIssue.batch_id == batch.batch_id,
                )
            )
        )
        if not expected_issues <= stored_issues:
            raise LocalPrimaryError("shadow_batch_integrity_failed")
        return rows, provenance_rows

    def read_shadow(self, principal: LocalPrincipal, batch_id: str) -> dict[str, list[Any]]:
        self._require_shadow_owner(principal)
        if not _canonical_uuid(batch_id):
            raise LocalPrimaryError("invalid_batch_id")
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            with connection.begin(), Session(bind=connection) as session:
                session.execute(text("SET TRANSACTION READ ONLY"))
                self._workspace(session)
                batch = session.get(ShadowBatch, (self.binding.workspace_id, batch_id))
                if batch is None:
                    raise LocalPrimaryError("batch_not_found")
                checkpoint = session.get(ShadowCheckpoint, (self.binding.workspace_id, batch_id))
                rows, _ = self._verify_persisted_batch(session, batch, checkpoint)
                result = {entity: [] for entity in ENTITIES}
                for row in rows:
                    result[row.entity].append(_copy(row.raw_record))
                return result

    @staticmethod
    def _references(records: dict[str, list[dict]]) -> dict[str, Any]:
        indexes = {
            entity: {
                row.get(ID_KEYS[entity])
                for row in rows
                if isinstance(row.get(ID_KEYS[entity]), str)
            }
            for entity, rows in records.items()
        }
        items: list[dict[str, Any]] = []

        def add(
            from_entity: str,
            row: dict,
            field: str,
            to_entity: str,
            *,
            required: bool = True,
        ) -> None:
            target = row.get(field)
            if target:
                items.append(
                    {
                        "from_entity": from_entity,
                        "from_id": row.get(ID_KEYS[from_entity])
                        if isinstance(row.get(ID_KEYS[from_entity]), str)
                        else None,
                        "field": field,
                        "to_entity": to_entity,
                        "to_id": target if isinstance(target, str) else None,
                        "required": required,
                        "resolved": isinstance(target, str) and target in indexes[to_entity],
                    }
                )

        for row in records["leads"]:
            promoted = row.get("status") == SCHEMA["enums"]["lead_status"][-1]
            add("leads", row, "person_id", "clients", required=promoted)
        for entity in ("works", "payments", "time", "tasks"):
            for row in records[entity]:
                add(entity, row, "person_id", "clients")
                if entity != "works":
                    add(entity, row, "work_id", "works")
        for row in records["payments"]:
            add("payments", row, "original_payment_id", "payments")
        items.sort(key=lambda item: (item["from_entity"], str(item["from_id"]), item["field"]))
        return {
            "total": len(items),
            "unresolved": sum(item["required"] and not item["resolved"] for item in items),
            "items": items,
        }

    @staticmethod
    def _money(records: dict[str, list[dict]]) -> tuple[dict[str, dict[str, int]], list[dict]]:
        payment_kinds = SCHEMA["enums"]["payment_kind"]
        paid_status = SCHEMA["enums"]["payment_status"][0]
        actual_source = SCHEMA["enums"]["expense_source"][0]
        result: dict[str, dict[str, int]] = {}
        refunds: list[dict] = []

        def bucket(currency: Any) -> dict[str, int]:
            return result.setdefault(
                str(currency),
                {
                    "received_minor": 0,
                    "refund_minor": 0,
                    "net_payments_minor": 0,
                    "paid_expenses_minor": 0,
                    "net_cash_minor": 0,
                },
            )

        for row in records["payments"]:
            target = bucket(row.get("currency"))
            amount = row.get("actual_amount_ils", 0)
            if row.get("kind") == payment_kinds[0]:
                target["received_minor"] += amount
                target["net_payments_minor"] += amount
            elif row.get("kind") == payment_kinds[1]:
                target["refund_minor"] += amount
                target["net_payments_minor"] -= amount
                refunds.append(
                    {
                        "payment_id": row.get("payment_id"),
                        "original_payment_id": row.get("original_payment_id"),
                        "amount_minor": amount,
                        "currency": row.get("currency"),
                    }
                )
        for row in records["expenses"]:
            if row.get("payment_status") == paid_status and row.get("source_type") == actual_source:
                bucket(row.get("currency"))["paid_expenses_minor"] += row.get("amount_ils", 0)
        for target in result.values():
            target["net_cash_minor"] = target["net_payments_minor"] - target["paid_expenses_minor"]
        refunds.sort(key=lambda row: str(row["payment_id"]))
        return dict(sorted(result.items())), refunds

    def reconcile(self, principal: LocalPrincipal, batch_id: str) -> dict[str, Any]:
        self._require_shadow_owner(principal)
        if not _canonical_uuid(batch_id):
            raise LocalPrimaryError("invalid_batch_id")
        with Session(self.engine) as session, session.begin():
            self._workspace(session, lock=True)
            self._lock_batch(session, batch_id)
            batch = session.get(
                ShadowBatch, (self.binding.workspace_id, batch_id), with_for_update=True
            )
            checkpoint = session.get(ShadowCheckpoint, (self.binding.workspace_id, batch_id))
            if batch is None or checkpoint is None:
                raise LocalPrimaryError("batch_not_found")
            if (
                checkpoint.status != "complete"
                or checkpoint.next_ordinal != checkpoint.total_records
            ):
                raise LocalPrimaryError("batch_incomplete")
            rows, provenance_rows = self._verify_persisted_batch(session, batch, checkpoint)
            records: dict[str, list[Any]] = {entity: [] for entity in ENTITIES}
            for row in rows:
                records[row.entity].append(_copy(row.raw_record))

            summaries = None
            if all(type(row.raw_record) is dict for row in rows):
                try:
                    validate_state(records)
                    coverage_values = set(batch.coverage.values())
                    aggregate_coverage = (
                        "complete"
                        if coverage_values == {"complete"}
                        else "unknown"
                        if coverage_values == {"unknown"}
                        else "partial"
                    )
                    summaries = build_summaries(records, batch.coverage, aggregate_coverage)
                except (LocalPrimaryError, SummaryInputError) as exc:
                    self._insert_issue(
                        session,
                        batch_id,
                        _Issue("state_validation_failed", detail={"domain_code": exc.code}),
                    )
            if summaries is not None and batch.expected_summary is not None:
                if _digest(summaries) != _digest(batch.expected_summary):
                    self._insert_issue(session, batch_id, _Issue("expected_summary_mismatch"))
            valid_records = all(type(row.raw_record) is dict for row in rows)
            references = (
                self._references(records)
                if valid_records
                else {"total": 0, "unresolved": 0, "items": []}
            )
            for reference in references["items"]:
                if reference["required"] and not reference["resolved"]:
                    self._insert_issue(
                        session,
                        batch_id,
                        _Issue(
                            "unresolved_reference",
                            entity=reference["from_entity"],
                            record_id=reference["from_id"],
                            detail={
                                "field": reference["field"],
                                "to_entity": reference["to_entity"],
                                "to_id": reference["to_id"],
                            },
                        ),
                    )
            session.flush()
            issues = list(
                session.scalars(
                    select(ShadowIssue)
                    .where(
                        ShadowIssue.workspace_id == self.binding.workspace_id,
                        ShadowIssue.batch_id == batch_id,
                    )
                    .order_by(ShadowIssue.reason_code, ShadowIssue.issue_key)
                )
            )
            ids = {
                entity: sorted(
                    str(row.get(ID_KEYS[entity]))
                    for row in records[entity]
                    if valid_records and isinstance(row.get(ID_KEYS[entity]), str)
                )
                for entity in ENTITIES
            }
            revisions = {
                entity: {
                    str(row.get(ID_KEYS[entity])): row.get("revision")
                    for row in records[entity]
                    if valid_records and isinstance(row.get(ID_KEYS[entity]), str)
                }
                for entity in ENTITIES
            }
            hashes = {
                entity: {
                    str(row.get(ID_KEYS[entity])): _digest(row)
                    for row in records[entity]
                    if valid_records and isinstance(row.get(ID_KEYS[entity]), str)
                }
                for entity in ENTITIES
            }
            input_hashes: dict[str, dict[str, dict[str, str | None]]] = {
                entity: {} for entity in ENTITIES
            }
            for row, provenance in zip(rows, provenance_rows, strict=True):
                if isinstance(row.record_id, str):
                    input_hashes[row.entity][row.record_id] = {
                        "source_input_digest": provenance.source_input_digest,
                        "accepted_input_digest": provenance.accepted_input_digest,
                    }
            money, refunds = (
                self._money(records) if valid_records and summaries is not None else ({}, [])
            )
            status = "quarantined" if issues else "accepted"
            manifest = {
                "workspace_id": self.binding.workspace_id,
                "authority_epoch": batch.authority_epoch,
                "batch_id": batch_id,
                "schema_version": batch.schema_version,
                "source_id": batch.source_id,
                "source_epoch": batch.source_epoch,
                "observed_at": batch.observed_at.astimezone(UTC).isoformat(),
                "payload_fingerprint": batch.payload_fingerprint,
                "status": status,
                "counts": {entity: len(records[entity]) for entity in ENTITIES},
                "ids": ids,
                "revisions": revisions,
                "record_hashes": hashes,
                "input_hashes": input_hashes,
                "records_hash": _digest(records),
                "coverage": _copy(batch.coverage),
                "coverage_hash": _digest(batch.coverage),
                "references": references,
                "money_by_currency": money,
                "refunds": refunds,
                "summaries": summaries,
                "summaries_hash": _digest(summaries) if summaries is not None else None,
                "expected_summary_match": None
                if batch.expected_summary is None or summaries is None
                else _digest(summaries) == _digest(batch.expected_summary),
                "issue_count": len(issues),
                "issue_codes": sorted({issue.reason_code for issue in issues}),
                "issues": [
                    {
                        "reason_code": issue.reason_code,
                        "entity": issue.entity,
                        "record_id": issue.record_id,
                        "ordinal": issue.ordinal,
                        "detail": _copy(issue.detail),
                    }
                    for issue in issues
                ],
            }
            manifest["manifest_digest"] = _digest(manifest)
            batch.reconcile_status = status
            batch.manifest = {
                "raw_payload": batch.manifest["raw_payload"],
                "reconciliation": manifest,
            }
            return _copy(manifest)


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MAX_RECORDS",
    "LocalShadowImporter",
    "ShadowBase",
    "ShadowBatch",
    "ShadowCheckpoint",
    "ShadowIssue",
    "ShadowProvenance",
    "ShadowRecord",
    "ShadowWorkspace",
]
