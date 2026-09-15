"""Durable CRM service.

The database is authoritative.  Sheets rows and provider payloads enter this module as
plain bounded data and can never select tools or grant authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.orm import Session

from app.db.models import (
    CrmActivityRow,
    CrmContactConversationRow,
    CrmContactRow,
    CrmIdentityRow,
    CrmIssueContactRow,
    CrmIssueRow,
    CrmOutboxRow,
    CrmSyncSnapshotRow,
)

CONTACT_FIELDS = (
    "name",
    "phone",
    "email",
    "date",
    "business",
    "source",
    "language",
    "want",
    "status",
    "summary",
    "next_step",
    "created",
    "updated",
    "pinged",
)
MAX_FIELD_CHARS = 2_000
OUTBOX_STATUSES = frozenset(
    {"pending", "in_flight", "confirmed", "failed", "unknown", "conflict"}
)
DESTINATIONS = frozenset({"contacts", "activity", "telegram"})
CRM_PROJECTION_LOCK_KEY = 1_872_440_921
_PHONE_DIGITS = re.compile(r"\D+")


class CrmError(ValueError):
    pass


class CrmRevisionConflict(CrmError):
    pass


class CrmNotFound(CrmError):
    pass


@dataclass(frozen=True)
class ActivityInput:
    who: str = "Mia"
    channel: str = ""
    action: str = "contact_updated"
    result: str = ""
    occurred_at: str | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class DestinationIntent:
    destination: Literal["contacts", "activity", "telegram"]
    payload: Mapping[str, Any]
    dedupe_key: str


@dataclass(frozen=True)
class ContactView:
    id: str
    revision: int
    fields: dict[str, str]
    source_ref: str
    conversation_id: str


@dataclass(frozen=True)
class ContactConversationView:
    contact_id: str
    conversation_id: str
    captured_fields: dict[str, str]
    source_ref: str


@dataclass(frozen=True)
class ActivityView:
    id: str
    contact_id: str
    occurred_at: str
    who: str
    channel: str
    action: str
    result: str


@dataclass(frozen=True)
class CaptureResult:
    contact: ContactView | None
    activity: ActivityView | None = None
    outbox_ids: tuple[str, ...] = ()
    issue_ids: tuple[str, ...] = ()
    status: Literal["created", "updated", "unchanged", "conflict"] = "unchanged"


@dataclass(frozen=True)
class TargetSnapshot:
    contact_id: str
    revision: int
    snapshot_hash: str
    fields: dict[str, str]


@dataclass(frozen=True)
class ConflictView:
    id: str
    contact_id: str
    issue_type: str
    field_name: str
    base_value: str
    database_value: str
    sheet_value: str
    status: str


@dataclass(frozen=True)
class SheetImportResult:
    rows_seen: int = 0
    contacts_created: int = 0
    contacts_updated: int = 0
    conflicts: int = 0
    issues: int = 0
    outbox_ids: tuple[str, ...] = field(default_factory=tuple)


def normalize_phone(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    digits = _PHONE_DIGITS.sub("", raw)
    if len(digits) < 7 or len(digits) > 15:
        return ""
    return f"+{digits}" if raw.startswith("+") else digits


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized or len(normalized) > 320 or normalized.count("@") != 1:
        return ""
    local, domain = normalized.split("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return ""
    return normalized


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_fields(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: str(value.get(key) or "") for key in CONTACT_FIELDS}


def _bounded_fields(fields: Mapping[str, Any]) -> dict[str, str]:
    bounded: dict[str, str] = {}
    for key in CONTACT_FIELDS:
        raw = fields.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if len(value) > MAX_FIELD_CHARS:
            raise CrmError(f"field {key} exceeds {MAX_FIELD_CHARS} characters")
        bounded[key] = value
    if "phone" in bounded:
        raw_phone = bounded["phone"]
        bounded["phone"] = normalize_phone(raw_phone)
        if raw_phone and not bounded["phone"]:
            raise CrmError("invalid phone")
    if "email" in bounded:
        raw_email = bounded["email"]
        bounded["email"] = normalize_email(raw_email)
        if raw_email and not bounded["email"]:
            raise CrmError("invalid email")
    return bounded


def _autoflushing(method: Callable[..., Any]) -> Callable[..., Any]:
    """Run one CRM operation with autoflush on, restoring the caller's setting.

    The application session factory disables autoflush, but CRM operations read rows
    they have just added (issue links, pending projections) and are only correct when
    those reads see them. Without this, production raised duplicate-link and
    foreign-key errors that tests running with autoflush on could not reproduce.
    """

    @wraps(method)
    def wrapper(self: CrmService, *args: Any, **kwargs: Any) -> Any:
        previous = self.session.autoflush
        self.session.autoflush = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self.session.autoflush = previous

    return wrapper


class CrmService:
    """Transaction-scoped CRM API. The caller controls commit and rollback."""

    def __init__(
        self,
        session: Session,
        *,
        now: Callable[[], datetime] | datetime | None = None,
    ) -> None:
        self.session = session
        self._clock = now

    def _now(self) -> str:
        moment = self._clock() if callable(self._clock) else self._clock
        moment = moment or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()

    def _lock_projection_effects(self) -> None:
        """Serialize issue state changes with the final Contacts Sheet write."""
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": CRM_PROJECTION_LOCK_KEY},
            )

    @_autoflushing
    def capture(
        self,
        fields: Mapping[str, Any],
        *,
        source_ref: str,
        conversation_id: str | None = None,
        contact_id: str | None = None,
        activity: ActivityInput | None = None,
        destination_intents: Sequence[DestinationIntent] = (),
        expected_revision: int | None = None,
    ) -> CaptureResult:
        self._lock_projection_effects()
        source_ref = source_ref.strip()
        if not source_ref or len(source_ref) > 255:
            raise CrmError("source_ref is required and must be at most 255 characters")
        incoming = _bounded_fields(fields)
        identities = self._identities(incoming)
        self._lock_identities(identities)
        matched_ids = self._matched_contact_ids(identities)
        if contact_id:
            matched_ids.add(contact_id)
        if len(matched_ids) > 1:
            issue_id = self._identity_issue(matched_ids, identities, source_ref)
            self.session.flush()
            return CaptureResult(contact=None, issue_ids=(issue_id,), status="conflict")

        row = self.session.get(CrmContactRow, next(iter(matched_ids))) if matched_ids else None
        prior_fields: dict[str, str] | None = None
        created = row is None
        if row is not None and expected_revision == 0:
            raise CrmRevisionConflict("contact was created after the proposal")
        if row is None:
            if not identities:
                raise CrmError("a new contact requires a valid phone or email")
            stamp = self._now()
            row = CrmContactRow(
                id=contact_id or _new_id("crm"),
                revision=1,
                fields_json=_json({key: incoming.get(key, "") for key in CONTACT_FIELDS}),
                source_ref=source_ref,
                conversation_id=(conversation_id or "")[:255],
                created_at=stamp,
                updated_at=stamp,
            )
            self.session.add(row)
            self.session.flush()
        else:
            if expected_revision is not None and row.revision != expected_revision:
                raise CrmRevisionConflict(
                    f"contact revision changed from {expected_revision} to {row.revision}"
                )
            current = _load_fields(row.fields_json)
            prior_fields = current
            merged = dict(current)
            merged.update({key: value for key, value in incoming.items() if value})
            changed = merged != current
            if changed:
                old_revision = row.revision
                values: dict[str, Any] = {
                    "fields_json": _json(merged),
                    "revision": old_revision + 1,
                    "updated_at": self._now(),
                    "source_ref": source_ref,
                }
                if conversation_id:
                    values["conversation_id"] = conversation_id[:255]
                result = self.session.execute(
                    update(CrmContactRow)
                    .where(CrmContactRow.id == row.id, CrmContactRow.revision == old_revision)
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise CrmRevisionConflict("contact changed concurrently")
                self.session.expire(row)

        collision_ids = (
            self._attach_identities(row.id, identities, source_ref)
            if prior_fields is None
            else self._reconcile_identities(
                row.id, prior_fields, _load_fields(row.fields_json), source_ref
            )
        )
        if collision_ids:
            issue_id = self._identity_issue(collision_ids | {row.id}, identities, source_ref)
            self.session.flush()
            return CaptureResult(
                contact=self._contact_view(row), issue_ids=(issue_id,), status="conflict"
            )

        if conversation_id:
            issue_id = self._link_conversation(
                row.id, conversation_id, incoming, source_ref=source_ref
            )
            if issue_id:
                self.session.flush()
                return CaptureResult(
                    contact=self._contact_view(row), issue_ids=(issue_id,), status="conflict"
                )

        self.session.flush()
        contact_view = self._contact_view(row)
        outbox_ids = [self._enqueue_contact(contact_view)]
        activity_view = None
        if activity is not None:
            activity_view, activity_outbox = self._record_activity(row.id, activity, source_ref)
            outbox_ids.append(activity_outbox)
        for intent in destination_intents:
            outbox_ids.append(
                self._enqueue(
                    aggregate_type="contact",
                    aggregate_id=row.id,
                    intent=intent,
                )
            )
        self.session.flush()
        return CaptureResult(
            contact=contact_view,
            activity=activity_view,
            outbox_ids=tuple(dict.fromkeys(outbox_ids)),
            status="created" if created else ("updated" if incoming else "unchanged"),
        )

    upsert_contact = capture

    @_autoflushing
    def capture_site_lead(
        self,
        fields: Mapping[str, Any],
        *,
        conversation_id: str,
        source_ref: str,
        summary: str = "",
        recipient_ids: Sequence[str] = (),
    ) -> CaptureResult:
        safe_summary = summary.strip()[:MAX_FIELD_CHARS]
        intents = tuple(
            DestinationIntent(
                destination="telegram",
                payload={
                    "recipient_id": str(recipient_id),
                    "text": safe_summary,
                    "conversation_id": conversation_id,
                    "receipt_key": f"crm:{conversation_id}:{recipient_id}",
                },
                dedupe_key=f"telegram:crm:{conversation_id}:{recipient_id}",
            )
            for recipient_id in recipient_ids
            if str(recipient_id).strip()
        )
        return self.capture(
            fields,
            source_ref=source_ref,
            conversation_id=conversation_id,
            activity=ActivityInput(
                channel="website",
                action="contact_captured",
                result=safe_summary,
                source_ref=f"{source_ref}:activity",
            ),
            destination_intents=intents,
        )

    @_autoflushing
    def refresh_pending_site_brief(
        self,
        *,
        contact_id: str,
        job_ids: Sequence[str],
        summary: str,
        source_ref: str,
        next_step: str = "",
        name: str = "",
    ) -> None:
        """Rewrite this turn's still-pending website brief after the model call.

        ``capture_site_lead`` builds the Telegram outbox payload and the capture
        Activity before the model turn runs, so a ``submit_lead`` tool call that
        arrives in the *same* turn (setting ``next_step`` or a verbatim visitor name)
        never reached that already-built text. Only ``job_ids`` produced by this
        capture are touched, and only while still ``status == "pending"``; a job
        already claimed, sent, failed or otherwise no longer pending is left exactly
        as it is. This never enqueues a new outbox row: it rewrites the payload of an
        existing one, so it can never create a second job or a second ping.
        """
        contact_id = contact_id.strip()
        safe_summary = summary.strip()[:MAX_FIELD_CHARS]
        if not contact_id or not safe_summary:
            return
        for job_id in job_ids:
            job = self.session.get(CrmOutboxRow, job_id)
            if (
                job is None
                or job.status != "pending"
                or job.aggregate_id != contact_id
                or job.destination != "telegram"
            ):
                continue
            try:
                payload = json.loads(job.payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            payload["text"] = safe_summary
            job.payload_json = _json(payload)
        activity = self.session.scalars(
            select(CrmActivityRow).where(CrmActivityRow.source_ref == f"{source_ref}:activity")
        ).one_or_none()
        if activity is not None:
            activity.result = safe_summary
        row = self.session.get(CrmContactRow, contact_id)
        if row is None:
            return
        fields = _load_fields(row.fields_json)
        updated = dict(fields)
        changed = False
        safe_next_step = next_step.strip()[:MAX_FIELD_CHARS]
        if safe_next_step and updated.get("next_step") != safe_next_step:
            updated["next_step"] = safe_next_step
            changed = True
        safe_name = name.strip()[:MAX_FIELD_CHARS]
        if safe_name and not updated.get("name"):
            updated["name"] = safe_name
            changed = True
        if not changed:
            return
        old_revision = row.revision
        result = self.session.execute(
            update(CrmContactRow)
            .where(CrmContactRow.id == row.id, CrmContactRow.revision == old_revision)
            .values(
                fields_json=_json(updated),
                revision=old_revision + 1,
                updated_at=self._now(),
            )
        )
        if result.rowcount == 1:
            self.session.expire(row)

    @_autoflushing
    def lookup(
        self,
        *,
        contact_id: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[ContactView]:
        limit = max(1, min(int(limit), 100))
        if contact_id:
            row = self.session.get(CrmContactRow, contact_id)
            return [self._contact_view(row)] if row is not None else []
        if query and (email := normalize_email(query)):
            return self._lookup_identity("email", email, limit)
        if query and (phone := normalize_phone(query)):
            return self._lookup_identity("phone", phone, limit)
        statement = select(CrmContactRow).order_by(CrmContactRow.updated_at.desc()).limit(limit)
        if query:
            escaped = query.strip().replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(
                CrmContactRow.fields_json.ilike(f"%{escaped}%", escape="\\")
            )
        return [self._contact_view(row) for row in self.session.scalars(statement)]

    @_autoflushing
    def lookup_for_conversation(self, conversation_id: str) -> ContactView | None:
        """Return only the contact explicitly linked to this public conversation."""
        conversation_id = conversation_id.strip()
        if not conversation_id:
            return None
        row = self.session.scalars(
            select(CrmContactRow)
            .join(
                CrmContactConversationRow,
                CrmContactConversationRow.contact_id == CrmContactRow.id,
            )
            .where(CrmContactConversationRow.conversation_id == conversation_id)
        ).one_or_none()
        return self._contact_view(row) if row is not None else None

    @_autoflushing
    def list_contact_conversations(self, contact_id: str) -> list[ContactConversationView]:
        rows = self.session.scalars(
            select(CrmContactConversationRow)
            .where(CrmContactConversationRow.contact_id == contact_id)
            .order_by(CrmContactConversationRow.created_at, CrmContactConversationRow.id)
        ).all()
        return [
            ContactConversationView(
                contact_id=row.contact_id,
                conversation_id=row.conversation_id,
                captured_fields=_load_fields(row.captured_fields_json),
                source_ref=row.source_ref,
            )
            for row in rows
        ]

    @_autoflushing
    def snapshot_target(self, contact_id: str) -> TargetSnapshot:
        row = self.session.get(CrmContactRow, contact_id)
        if row is None:
            raise CrmNotFound(contact_id)
        fields = _load_fields(row.fields_json)
        digest = sha256(f"{row.id}:{row.revision}:{_json(fields)}".encode()).hexdigest()
        return TargetSnapshot(row.id, row.revision, digest, fields)

    @_autoflushing
    def snapshot_identity(self, fields: Mapping[str, Any]) -> TargetSnapshot:
        """Bind a create proposal either to an existing contact or to observed absence."""
        bounded = _bounded_fields(fields)
        matched = self._matched_contact_ids(self._identities(bounded))
        if len(matched) > 1:
            raise CrmError("identities belong to different contacts")
        if matched:
            return self.snapshot_target(next(iter(matched)))
        identity_fields = {
            key: bounded.get(key, "") for key in ("phone", "email") if bounded.get(key)
        }
        if not identity_fields:
            raise CrmError("a create proposal requires a valid phone or email")
        digest = sha256(f"new:{_json(identity_fields)}".encode()).hexdigest()
        return TargetSnapshot("", 0, digest, identity_fields)

    @_autoflushing
    def record_activity(
        self,
        contact_id: str,
        *,
        source_ref: str,
        activity: ActivityInput | None = None,
        kind: str = "activity",
        summary: str = "",
        destination_intents: Sequence[DestinationIntent] = (),
    ) -> ActivityView:
        if self.session.get(CrmContactRow, contact_id) is None:
            raise CrmNotFound(contact_id)
        activity = activity or ActivityInput(action=kind, result=summary)
        view, _outbox_id = self._record_activity(contact_id, activity, source_ref)
        for intent in destination_intents:
            self._enqueue(aggregate_type="activity", aggregate_id=view.id, intent=intent)
        self.session.flush()
        return view

    @_autoflushing
    def list_conflicts(
        self, *, contact_id: str | None = None, unresolved_only: bool = True
    ) -> list[ConflictView]:
        statement = select(CrmIssueRow).order_by(CrmIssueRow.created_at, CrmIssueRow.id)
        if contact_id:
            statement = statement.where(self._issue_contact_predicate(contact_id))
        if unresolved_only:
            statement = statement.where(CrmIssueRow.status.in_(("open", "resolving")))
        return [self._conflict_view(row) for row in self.session.scalars(statement).all()]

    @_autoflushing
    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        resolution: Literal["database", "sheet", "value"],
        value: str | None = None,
        expected_contact_revision: int | None = None,
    ) -> ContactView:
        self._lock_projection_effects()
        issue = self.session.get(CrmIssueRow, conflict_id)
        if issue is None or issue.status != "open" or not issue.contact_id:
            raise CrmNotFound(conflict_id)
        contact_stmt = select(CrmContactRow).where(CrmContactRow.id == issue.contact_id)
        if self.session.get_bind().dialect.name == "postgresql":
            contact_stmt = contact_stmt.with_for_update()
        contact = self.session.scalars(contact_stmt).one_or_none()
        if contact is None:
            raise CrmNotFound(issue.contact_id)
        if expected_contact_revision is not None and contact.revision != expected_contact_revision:
            raise CrmRevisionConflict("contact changed before conflict resolution")
        if issue.field_name:
            selected = {
                "database": issue.database_value,
                "sheet": issue.sheet_value,
                "value": value,
            }[resolution]
            if selected is None:
                raise CrmError("value resolution requires value")
            fields = _load_fields(contact.fields_json)
            selected_value = _bounded_fields({issue.field_name: selected}).get(
                issue.field_name, ""
            )
            if issue.field_name in {"phone", "email"}:
                old_value = fields.get(issue.field_name, "")
                lock_values = {
                    issue.field_name: value
                    for value in (old_value, selected_value)
                    if value
                }
                self._lock_identity_values(issue.field_name, lock_values.values())
                owner = self.session.scalars(
                    select(CrmIdentityRow.contact_id).where(
                        CrmIdentityRow.kind == issue.field_name,
                        CrmIdentityRow.normalized_value == selected_value,
                    )
                ).first() if selected_value else None
                if owner is not None and owner != contact.id:
                    raise CrmError("resolved identity belongs to another contact")
                self.session.execute(
                    delete(CrmIdentityRow).where(
                        CrmIdentityRow.contact_id == contact.id,
                        CrmIdentityRow.kind == issue.field_name,
                    )
                )
                if selected_value:
                    self.session.add(
                        CrmIdentityRow(
                            id=_new_id("identity"),
                            contact_id=contact.id,
                            kind=issue.field_name,
                            normalized_value=selected_value,
                            source_ref=f"conflict:{issue.id}",
                            created_at=self._now(),
                        )
                    )
            fields[issue.field_name] = selected_value
            if resolution in {"database", "value"}:
                issue.database_value = selected_value
            old_revision = contact.revision
            result = self.session.execute(
                update(CrmContactRow)
                .where(CrmContactRow.id == contact.id, CrmContactRow.revision == old_revision)
                .values(
                    fields_json=_json(fields),
                    revision=old_revision + 1,
                    updated_at=self._now(),
                )
            )
            if result.rowcount != 1:
                raise CrmRevisionConflict("contact changed concurrently")
            self.session.expire(contact)
        details = self._issue_details(issue)
        requires_sheet_write = bool(issue.field_name and resolution in {"database", "value"})
        issue.status = "resolving" if requires_sheet_write else "resolved"
        issue.resolution = resolution
        issue.resolved_at = "" if requires_sheet_write else self._now()
        self.session.flush()
        view = self._contact_view(contact)
        if requires_sheet_write:
            observed_cells = details.get("observed_cells")
            if (
                not isinstance(observed_cells, list)
                or len(observed_cells) != len(CONTACT_FIELDS)
                or not all(isinstance(cell, str) for cell in observed_cells)
            ):
                raise CrmRevisionConflict("conflict has no exact observed Sheet snapshot")
        remaining_open = self.session.scalars(
            select(CrmIssueRow.id).where(
                self._issue_contact_predicate(contact.id),
                CrmIssueRow.status == "open",
            )
        ).first()
        resolving = self.session.scalars(
            select(CrmIssueRow).where(
                self._issue_contact_predicate(contact.id),
                CrmIssueRow.status == "resolving",
            )
        ).all()
        if remaining_open is not None:
            self._pause_contact_projections(contact.id)
        elif resolving:
            observed_cells = self._issue_details(resolving[0]).get("observed_cells")
            if (
                not isinstance(observed_cells, list)
                or len(observed_cells) != len(CONTACT_FIELDS)
                or not all(isinstance(cell, str) for cell in observed_cells)
            ):
                raise CrmRevisionConflict("conflict has no exact observed Sheet snapshot")
            for pending_issue in resolving[1:]:
                if self._issue_details(pending_issue).get("observed_cells") != observed_cells:
                    raise CrmRevisionConflict(
                        "approved conflicts do not share one observed Sheet snapshot"
                    )
            self._enqueue_contact(
                view,
                base_cells=observed_cells,
                resolution_issue_ids=[pending_issue.id for pending_issue in resolving],
            )
        else:
            self._enqueue_contact(view, ignored_issue_id=issue.id)
        self.session.flush()
        return view

    @_autoflushing
    def import_sheet_contact(self, cells: Sequence[Any], *, row_number: int) -> CaptureResult:
        self._lock_projection_effects()
        raw_values = [str(value or "") for value in cells[:15]]
        raw_values.extend([""] * (15 - len(raw_values)))
        values = [value.strip() for value in raw_values]
        values.extend([""] * (15 - len(values)))
        sheet_fields = dict(zip(CONTACT_FIELDS, values[:14], strict=True))
        sheet_id = values[14]
        source_ref = f"sheets:Contacts:{row_number}"
        if not sheet_id:
            try:
                result = self.capture(sheet_fields, source_ref=source_ref)
                if result.contact is None or result.issue_ids:
                    return result
                binding = {
                    "row_number": row_number,
                    "cells": raw_values[:14],
                    "snapshot_hash": sha256(_json({"cells": raw_values[:14]}).encode()).hexdigest(),
                }
                job = self.session.get(CrmOutboxRow, result.outbox_ids[0])
                if job is None:
                    raise CrmError("legacy bootstrap projection is missing")
                existing_bindings = []
                for other in self.session.scalars(
                    select(CrmOutboxRow).where(
                        CrmOutboxRow.aggregate_id == result.contact.id,
                        CrmOutboxRow.destination == "contacts",
                    )
                ):
                    try:
                        other_payload = json.loads(other.payload_json)
                    except json.JSONDecodeError:
                        continue
                    if other_payload.get("legacy_binding"):
                        existing_bindings.append(other_payload["legacy_binding"])
                if any(
                    item.get("snapshot_hash") != binding["snapshot_hash"]
                    for item in existing_bindings
                ):
                    issue_id = self._issue(
                        contact_id=result.contact.id,
                        issue_type="legacy_row_collision",
                        details={"row": row_number},
                    )
                    self._pause_contact_projections(result.contact.id)
                    self.session.flush()
                    return CaptureResult(
                        contact=result.contact,
                        outbox_ids=result.outbox_ids,
                        issue_ids=(issue_id,),
                        status="conflict",
                    )
                payload = json.loads(job.payload_json)
                payload["legacy_binding"] = binding
                job.payload_json = _json(payload)
                self.mark_contact_snapshot(
                    result.contact.id,
                    fields=sheet_fields,
                    revision=result.contact.revision,
                    row_number=row_number,
                )
                return result
            except CrmError as exc:
                issue_id = self._issue(
                    contact_id="",
                    issue_type="invalid_legacy_row",
                    details={"row": row_number, "reason": str(exc)},
                )
                self.session.flush()
                return CaptureResult(contact=None, issue_ids=(issue_id,), status="conflict")
        contact = self.session.get(CrmContactRow, sheet_id)
        if contact is None:
            issue_id = self._issue(
                contact_id=sheet_id,
                issue_type="missing_contact",
                details={"row": row_number},
            )
            self.session.flush()
            return CaptureResult(contact=None, issue_ids=(issue_id,), status="conflict")
        return self._merge_sheet_row(contact, sheet_fields, row_number=row_number)

    @_autoflushing
    def mark_contact_synced(self, contact_id: str, *, row_number: int = 0) -> None:
        contact = self.session.get(CrmContactRow, contact_id)
        if contact is None:
            raise CrmNotFound(contact_id)
        stamp = self._now()
        for name, value in _load_fields(contact.fields_json).items():
            snapshot = self.session.get(CrmSyncSnapshotRow, (contact_id, name))
            if snapshot is None:
                snapshot = CrmSyncSnapshotRow(contact_id=contact_id, field_name=name)
                self.session.add(snapshot)
            snapshot.value = value
            snapshot.contact_revision = contact.revision
            snapshot.sheet_row = row_number
            snapshot.synced_at = stamp
        self.session.flush()

    @_autoflushing
    def mark_contact_snapshot(
        self,
        contact_id: str,
        *,
        fields: Mapping[str, Any],
        revision: int,
        row_number: int = 0,
    ) -> None:
        """Record exactly the revision the destination acknowledged."""
        if self.session.get(CrmContactRow, contact_id) is None:
            raise CrmNotFound(contact_id)
        delivered = _bounded_fields(fields)
        stamp = self._now()
        for name in CONTACT_FIELDS:
            snapshot = self.session.get(CrmSyncSnapshotRow, (contact_id, name))
            if snapshot is None:
                snapshot = CrmSyncSnapshotRow(contact_id=contact_id, field_name=name)
                self.session.add(snapshot)
            snapshot.value = delivered.get(name, "")
            snapshot.contact_revision = revision
            snapshot.sheet_row = row_number
            snapshot.synced_at = stamp
        self.session.flush()

    @_autoflushing
    def note_missing_sheet_contacts(self, seen_contact_ids: set[str]) -> tuple[str, ...]:
        self._lock_projection_effects()
        issue_ids: list[str] = []
        contacts = self.session.scalars(select(CrmContactRow)).all()
        for contact in contacts:
            if contact.id in seen_contact_ids:
                continue
            was_projected = self.session.scalars(
                select(CrmSyncSnapshotRow.contact_id).where(
                    CrmSyncSnapshotRow.contact_id == contact.id
                )
            ).first()
            if was_projected is None:
                continue
            existing = self.session.scalars(
                select(CrmIssueRow.id).where(
                    CrmIssueRow.contact_id == contact.id,
                    CrmIssueRow.issue_type == "missing_sheet_row",
                    CrmIssueRow.status == "open",
                )
            ).first()
            if existing is None:
                issue_ids.append(
                    self._issue(contact_id=contact.id, issue_type="missing_sheet_row")
                )
            self._enqueue_contact(self._contact_view(contact))
        self.session.flush()
        return tuple(issue_ids)

    @_autoflushing
    def record_projection_issue(
        self,
        contact_id: str,
        *,
        issue_type: str,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        self._lock_projection_effects()
        existing = self.session.scalars(
            select(CrmIssueRow.id).where(
                CrmIssueRow.contact_id == contact_id,
                CrmIssueRow.issue_type == issue_type,
                CrmIssueRow.status == "open",
            )
        ).first()
        issue_id = existing or self._issue(
            contact_id=contact_id, issue_type=issue_type, details=details
        )
        self._pause_contact_projections(contact_id)
        self.session.flush()
        return issue_id

    def _merge_sheet_row(
        self, contact: CrmContactRow, sheet_fields: Mapping[str, str], *, row_number: int
    ) -> CaptureResult:
        db_fields = _load_fields(contact.fields_json)
        merged = dict(db_fields)
        issue_ids: list[str] = []
        accepted_from_sheet: list[str] = []
        for name in CONTACT_FIELDS:
            sheet_value = _bounded_fields({name: sheet_fields.get(name, "")}).get(name, "")
            snapshot = self.session.get(CrmSyncSnapshotRow, (contact.id, name))
            base = snapshot.value if snapshot is not None else db_fields[name]
            db_value = db_fields[name]
            if sheet_value == base:
                continue
            if db_value == base or db_value == sheet_value:
                merged[name] = sheet_value
                accepted_from_sheet.append(name)
                continue
            issue_ids.append(
                self._upsert_field_conflict(
                    contact_id=contact.id,
                    field_name=name,
                    base_value=base,
                    database_value=db_value,
                    sheet_value=sheet_value,
                    details={"row": row_number, "observed_cells": list(sheet_fields.values())},
                )
            )
        proposed_identities = self._identities(merged)
        for kind in ("phone", "email"):
            self._lock_identity_values(
                kind,
                [value for value in (db_fields.get(kind, ""), merged.get(kind, "")) if value],
            )
        identity_owners = self._matched_contact_ids(proposed_identities) - {contact.id}
        if identity_owners:
            for name in ("phone", "email"):
                if merged.get(name) != db_fields.get(name):
                    merged[name] = db_fields.get(name, "")
            issue_ids.append(
                self._identity_issue(
                    identity_owners | {contact.id},
                    proposed_identities,
                    f"sheets:Contacts:{row_number}",
                )
            )
        if merged != db_fields:
            old_revision = contact.revision
            result = self.session.execute(
                update(CrmContactRow)
                .where(CrmContactRow.id == contact.id, CrmContactRow.revision == old_revision)
                .values(
                    fields_json=_json(merged),
                    revision=old_revision + 1,
                    updated_at=self._now(),
                    source_ref=f"sheets:Contacts:{row_number}",
                )
            )
            if result.rowcount != 1:
                raise CrmRevisionConflict("contact changed during sheet import")
            self.session.expire(contact)
        self.session.flush()
        view = self._contact_view(contact)
        self._reconcile_identities(
            contact.id,
            db_fields,
            self._identities(view.fields),
            f"sheets:Contacts:{row_number}",
        )
        for name in accepted_from_sheet:
            snapshot = self.session.get(CrmSyncSnapshotRow, (contact.id, name))
            if snapshot is None:
                snapshot = CrmSyncSnapshotRow(contact_id=contact.id, field_name=name)
                self.session.add(snapshot)
            snapshot.value = view.fields[name]
            snapshot.contact_revision = view.revision
            snapshot.sheet_row = row_number
            snapshot.synced_at = self._now()
        outbox_id = self._enqueue_contact(view)
        if issue_ids:
            self._pause_contact_projections(contact.id)
        self.session.flush()
        return CaptureResult(
            contact=view,
            outbox_ids=(outbox_id,),
            issue_ids=tuple(issue_ids),
            status="conflict" if issue_ids else ("updated" if merged != db_fields else "unchanged"),
        )

    def _identities(self, fields: Mapping[str, str]) -> dict[str, str]:
        return {
            kind: value
            for kind, value in (
                ("phone", fields.get("phone", "")),
                ("email", fields.get("email", "")),
            )
            if value
        }

    def _matched_contact_ids(self, identities: Mapping[str, str]) -> set[str]:
        if not identities:
            return set()
        predicates = [
            (CrmIdentityRow.kind == kind) & (CrmIdentityRow.normalized_value == value)
            for kind, value in identities.items()
        ]
        return set(
            self.session.scalars(select(CrmIdentityRow.contact_id).where(or_(*predicates))).all()
        )

    def _lock_identities(self, identities: Mapping[str, str]) -> None:
        """Serialize identity ownership, including the currently-absent case on PostgreSQL."""
        bind = self.session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        for kind, value in sorted(identities.items()):
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:identity_key))"),
                {"identity_key": f"crm:{kind}:{value}"},
            )

    def _lock_identity_values(self, kind: str, values: Sequence[str]) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        for value in sorted(set(values)):
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:identity_key))"),
                {"identity_key": f"crm:{kind}:{value}"},
            )

    def _link_conversation(
        self,
        contact_id: str,
        conversation_id: str,
        captured_fields: Mapping[str, str],
        *,
        source_ref: str,
    ) -> str:
        conversation_id = conversation_id.strip()
        if not conversation_id or len(conversation_id) > 255:
            raise CrmError("conversation_id must be at most 255 characters")
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"crm:conversation:{conversation_id}"},
            )
        existing = self.session.scalars(
            select(CrmContactConversationRow).where(
                CrmContactConversationRow.conversation_id == conversation_id
            )
        ).one_or_none()
        if existing is not None:
            if existing.contact_id != contact_id:
                return self._issue(
                    contact_id=contact_id,
                    issue_type="conversation_collision",
                    details={"conversation_id": conversation_id},
                )
            existing.captured_fields_json = _json(captured_fields)
            existing.source_ref = source_ref
            existing.updated_at = self._now()
            return ""
        stamp = self._now()
        self.session.add(
            CrmContactConversationRow(
                id=_new_id("conversation"),
                contact_id=contact_id,
                conversation_id=conversation_id,
                captured_fields_json=_json(captured_fields),
                source_ref=source_ref,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        return ""

    def _pause_contact_projections(self, contact_id: str) -> None:
        jobs = self.session.scalars(
            select(CrmOutboxRow).where(
                CrmOutboxRow.aggregate_id == contact_id,
                CrmOutboxRow.destination == "contacts",
                CrmOutboxRow.status.in_(("pending", "failed")),
            )
        ).all()
        for job in jobs:
            try:
                payload = json.loads(job.payload_json)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            resolution_issue_ids = {
                str(issue_id)
                for issue_id in payload.get("resolution_issue_ids", [])
                if issue_id
            } if isinstance(payload.get("resolution_issue_ids"), list) else set()
            legacy_resolution_issue_id = str(payload.get("resolution_issue_id") or "")
            if legacy_resolution_issue_id:
                resolution_issue_ids.add(legacy_resolution_issue_id)
            resolution_issues = [
                self.session.get(CrmIssueRow, issue_id)
                for issue_id in resolution_issue_ids
            ]
            if resolution_issues and all(
                issue is not None and issue.status == "resolving"
                for issue in resolution_issues
            ):
                continue
            job.status = "conflict"
            job.last_error = "contact projection paused for owner conflict resolution"

    def _attach_identities(
        self, contact_id: str, identities: Mapping[str, str], source_ref: str
    ) -> set[str]:
        collisions: set[str] = set()
        for kind, value in identities.items():
            existing = self.session.scalars(
                select(CrmIdentityRow).where(
                    CrmIdentityRow.kind == kind,
                    CrmIdentityRow.normalized_value == value,
                )
            ).one_or_none()
            if existing is not None:
                if existing.contact_id != contact_id:
                    collisions.add(existing.contact_id)
                continue
            self.session.add(
                CrmIdentityRow(
                    id=_new_id("identity"),
                    contact_id=contact_id,
                    kind=kind,
                    normalized_value=value,
                    source_ref=source_ref,
                    created_at=self._now(),
                )
            )
        return collisions

    def _reconcile_identities(
        self,
        contact_id: str,
        old_fields: Mapping[str, str],
        new_fields: Mapping[str, str],
        source_ref: str,
    ) -> set[str]:
        old_identities = self._identities(old_fields)
        new_identities = self._identities(new_fields)
        collisions = self._matched_contact_ids(new_identities) - {contact_id}
        if collisions:
            return collisions
        for kind in ("phone", "email"):
            old_value = old_identities.get(kind, "")
            new_value = new_identities.get(kind, "")
            if old_value == new_value:
                continue
            self.session.execute(
                delete(CrmIdentityRow).where(
                    CrmIdentityRow.contact_id == contact_id,
                    CrmIdentityRow.kind == kind,
                )
            )
        return self._attach_identities(contact_id, new_identities, source_ref)

    def _identity_issue(
        self, contact_ids: set[str], identities: Mapping[str, str], source_ref: str
    ) -> str:
        issue_id = self._issue(
            contact_id=sorted(contact_ids)[0] if contact_ids else "",
            issue_type="identity_collision",
            details={
                "contact_ids": sorted(contact_ids),
                "identity_kinds": sorted(identities),
                "source_ref": source_ref,
            },
        )
        for contact_id in contact_ids:
            self._link_issue_contact(issue_id, contact_id)
            self._pause_contact_projections(contact_id)
        return issue_id

    def _upsert_field_conflict(
        self,
        *,
        contact_id: str,
        field_name: str,
        base_value: str,
        database_value: str,
        sheet_value: str,
        details: Mapping[str, Any],
    ) -> str:
        issue = self.session.scalars(
            select(CrmIssueRow).where(
                CrmIssueRow.contact_id == contact_id,
                CrmIssueRow.issue_type == "field_conflict",
                CrmIssueRow.field_name == field_name,
                CrmIssueRow.status.in_(("open", "resolving")),
            )
        ).first()
        if issue is None:
            return self._issue(
                contact_id=contact_id,
                issue_type="field_conflict",
                field_name=field_name,
                base_value=base_value,
                database_value=database_value,
                sheet_value=sheet_value,
                details=details,
            )
        if (
            issue.status == "resolving"
            and issue.base_value == base_value
            and issue.database_value == database_value
            and issue.sheet_value == sheet_value
            and self._issue_details(issue) == dict(details)
        ):
            return issue.id
        issue.base_value = base_value
        issue.database_value = database_value
        issue.sheet_value = sheet_value
        issue.details_json = _json(details)
        issue.status = "open"
        issue.resolution = ""
        issue.resolved_at = ""
        return issue.id

    def _issue(
        self,
        *,
        contact_id: str,
        issue_type: str,
        field_name: str = "",
        base_value: str = "",
        database_value: str = "",
        sheet_value: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> str:
        issue_id = _new_id("issue")
        self.session.add(
            CrmIssueRow(
                id=issue_id,
                contact_id=contact_id,
                issue_type=issue_type,
                field_name=field_name,
                base_value=base_value,
                database_value=database_value,
                sheet_value=sheet_value,
                details_json=_json(details or {}),
                status="open",
                created_at=self._now(),
            )
        )
        # No ORM relationship links CrmIssueContactRow to its issue, so the unit of
        # work cannot order the inserts. Flush the parent first or PostgreSQL rejects
        # the link with a foreign-key violation (this stopped every delivery cycle).
        self.session.flush()
        self._link_issue_contact(issue_id, contact_id)
        return issue_id

    def _link_issue_contact(self, issue_id: str, contact_id: str) -> None:
        if not contact_id or self.session.get(CrmContactRow, contact_id) is None:
            return
        if self.session.get(CrmIssueContactRow, (issue_id, contact_id)) is None:
            self.session.add(CrmIssueContactRow(issue_id=issue_id, contact_id=contact_id))

    @staticmethod
    def _issue_details(issue: CrmIssueRow) -> dict[str, Any]:
        try:
            details = json.loads(issue.details_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return details if isinstance(details, dict) else {}

    @staticmethod
    def _issue_contact_predicate(contact_id: str) -> Any:
        return or_(
            CrmIssueRow.contact_id == contact_id,
            CrmIssueRow.id.in_(
                select(CrmIssueContactRow.issue_id).where(
                    CrmIssueContactRow.contact_id == contact_id
                )
            ),
        )

    def _record_activity(
        self, contact_id: str, activity: ActivityInput, fallback_source_ref: str
    ) -> tuple[ActivityView, str]:
        source_ref = (activity.source_ref or fallback_source_ref).strip()
        existing = self.session.scalars(
            select(CrmActivityRow).where(CrmActivityRow.source_ref == source_ref)
        ).one_or_none()
        if existing is None:
            row = CrmActivityRow(
                id=_new_id("activity"),
                contact_id=contact_id,
                occurred_at=(activity.occurred_at or self._now())[:64],
                who=activity.who.strip()[:160],
                channel=activity.channel.strip()[:32],
                action=activity.action.strip()[:MAX_FIELD_CHARS],
                result=activity.result.strip()[:MAX_FIELD_CHARS],
                source_ref=source_ref[:255],
                created_at=self._now(),
            )
            self.session.add(row)
            self.session.flush()
        else:
            row = existing
        view = self._activity_view(row)
        outbox_id = self._enqueue(
            aggregate_type="activity",
            aggregate_id=row.id,
            intent=DestinationIntent(
                destination="activity",
                payload={
                    "activity_id": row.id,
                    "contact_id": contact_id,
                    "cells": [
                        row.occurred_at,
                        row.who,
                        row.channel,
                        row.action,
                        row.result,
                        row.id,
                    ],
                },
                dedupe_key=f"activity:{row.id}",
            ),
        )
        return view, outbox_id

    def _enqueue_contact(
        self,
        contact: ContactView,
        *,
        base_cells: Sequence[str] | None = None,
        resolution_issue_id: str = "",
        resolution_issue_ids: Sequence[str] = (),
        ignored_issue_id: str = "",
    ) -> str:
        payload: dict[str, Any] = {
            "contact_id": contact.id,
            "revision": contact.revision,
            "cells": [contact.fields.get(name, "") for name in CONTACT_FIELDS]
            + [contact.id],
            "base_cells": list(base_cells) if base_cells is not None else [
                (
                    self.session.get(CrmSyncSnapshotRow, (contact.id, name)).value
                    if self.session.get(CrmSyncSnapshotRow, (contact.id, name)) is not None
                    else ""
                )
                for name in CONTACT_FIELDS
            ],
        }
        approved_resolution_ids = tuple(
            dict.fromkeys(
                issue_id
                for issue_id in (resolution_issue_id, *resolution_issue_ids)
                if issue_id
            )
        )
        if approved_resolution_ids:
            payload["resolution_issue_ids"] = list(approved_resolution_ids)
        outbox_id = self._enqueue(
            aggregate_type="contact",
            aggregate_id=contact.id,
            intent=DestinationIntent(
                destination="contacts",
                payload=payload,
                dedupe_key=f"contacts:{contact.id}:{contact.revision}",
            ),
        )
        existing_job = self.session.get(CrmOutboxRow, outbox_id)
        existing_resolution_issue_ids: set[str] = set()
        if existing_job is not None:
            try:
                existing_payload = json.loads(existing_job.payload_json)
            except (TypeError, json.JSONDecodeError):
                existing_payload = {}
            legacy_resolution_id = str(existing_payload.get("resolution_issue_id") or "")
            if legacy_resolution_id:
                existing_resolution_issue_ids.add(legacy_resolution_id)
            raw_resolution_ids = existing_payload.get("resolution_issue_ids")
            if isinstance(raw_resolution_ids, list):
                existing_resolution_issue_ids.update(
                    str(issue_id) for issue_id in raw_resolution_ids if issue_id
                )
        issue_statement = select(CrmIssueRow.id).where(
            self._issue_contact_predicate(contact.id),
            CrmIssueRow.status.in_(("open", "resolving")),
        )
        if ignored_issue_id:
            issue_statement = issue_statement.where(CrmIssueRow.id != ignored_issue_id)
        ignored_resolution_ids = set(approved_resolution_ids) | existing_resolution_issue_ids
        if ignored_resolution_ids:
            issue_statement = issue_statement.where(
                CrmIssueRow.id.notin_(ignored_resolution_ids)
            )
        has_open_issue = self.session.scalars(
            issue_statement
        ).first()
        if has_open_issue is not None:
            job = self.session.get(CrmOutboxRow, outbox_id)
            if job is not None and job.status in {"pending", "failed"}:
                job.status = "conflict"
                job.last_error = "contact projection paused for owner conflict resolution"
        return outbox_id

    def _enqueue(
        self, *, aggregate_type: str, aggregate_id: str, intent: DestinationIntent
    ) -> str:
        if intent.destination not in DESTINATIONS:
            raise CrmError("unsupported CRM destination")
        dedupe_key = intent.dedupe_key.strip()
        if not dedupe_key or len(dedupe_key) > 255:
            raise CrmError("invalid destination dedupe key")
        existing = self.session.scalars(
            select(CrmOutboxRow).where(CrmOutboxRow.dedupe_key == dedupe_key)
        ).one_or_none()
        if existing is not None:
            return existing.id
        outbox_id = _new_id("job")
        self.session.add(
            CrmOutboxRow(
                id=outbox_id,
                dedupe_key=dedupe_key,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                destination=intent.destination,
                payload_json=_json(intent.payload),
                status="pending",
                next_attempt_at=self._now(),
                created_at=self._now(),
            )
        )
        return outbox_id

    def _lookup_identity(self, kind: str, value: str, limit: int) -> list[ContactView]:
        rows = self.session.scalars(
            select(CrmContactRow)
            .join(CrmIdentityRow, CrmIdentityRow.contact_id == CrmContactRow.id)
            .where(CrmIdentityRow.kind == kind, CrmIdentityRow.normalized_value == value)
            .limit(limit)
        ).all()
        return [self._contact_view(row) for row in rows]

    @staticmethod
    def _contact_view(row: CrmContactRow) -> ContactView:
        return ContactView(
            id=row.id,
            revision=row.revision,
            fields=_load_fields(row.fields_json),
            source_ref=row.source_ref,
            conversation_id=row.conversation_id,
        )

    @staticmethod
    def _activity_view(row: CrmActivityRow) -> ActivityView:
        return ActivityView(
            id=row.id,
            contact_id=row.contact_id,
            occurred_at=row.occurred_at,
            who=row.who,
            channel=row.channel,
            action=row.action,
            result=row.result,
        )

    @staticmethod
    def _conflict_view(row: CrmIssueRow) -> ConflictView:
        return ConflictView(
            id=row.id,
            contact_id=row.contact_id,
            issue_type=row.issue_type,
            field_name=row.field_name,
            base_value=row.base_value,
            database_value=row.database_value,
            sheet_value=row.sheet_value,
            status=row.status,
        )
