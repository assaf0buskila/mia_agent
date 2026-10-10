"""Owner-scoped durable reminder CRUD."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.models import CrmContactRow, CrmIdentityRow
from app.db.reminders import OwnerReminderRow
from app.services.crm_v2 import normalize_email, normalize_phone

ReminderAction = Literal["complete", "cancel", "reschedule", "snooze"]
MAX_REMINDER_TEXT = 2_800


class ReminderError(RuntimeError):
    pass


class ReminderValidationError(ReminderError):
    pass


class ReminderNotFound(ReminderError):
    pass


class ReminderConflict(ReminderError):
    pass


class ReminderDispatching(ReminderConflict):
    pass


@dataclass(frozen=True)
class ContactCandidate:
    id: str
    name: str


class ReminderContactAmbiguous(ReminderError):
    def __init__(self, candidates: tuple[ContactCandidate, ...]) -> None:
        self.candidates = candidates
        super().__init__("contact_ambiguous")


@dataclass(frozen=True)
class ReminderView:
    id: str
    owner_id: str
    source_event_id: str
    text: str
    scheduled_at: datetime
    timezone: str
    contact_id: str | None
    status: str
    revision: int
    delivery_state: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ReminderCreateResult:
    reminder: ReminderView
    created: bool
    contact_unlinked: bool


@dataclass(frozen=True)
class ReminderChangeResult:
    reminder: ReminderView


@dataclass(frozen=True)
class ReminderSnapshot:
    counts: dict[str, int]
    upcoming: tuple[ReminderView, ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _view(row: OwnerReminderRow) -> ReminderView:
    return ReminderView(
        id=row.id,
        owner_id=row.owner_id,
        source_event_id=row.source_event_id,
        text=row.text,
        scheduled_at=_utc(row.scheduled_at),
        timezone=row.timezone,
        contact_id=row.contact_id,
        status=row.status,
        revision=row.revision,
        delivery_state=row.delivery_state,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.split()).casefold()


def _contact_fields(row: CrmContactRow) -> dict[str, str]:
    try:
        value = json.loads(row.fields_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {key: str(value.get(key) or "") for key in ("name", "email", "phone")}


class ReminderService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _find_contact(self, query: str) -> str | None:
        needle = query.strip()
        if not needle:
            return None
        rows = self.session.scalars(select(CrmContactRow)).all()
        exact_id = next((row for row in rows if row.id == needle), None)
        if exact_id is not None:
            return exact_id.id
        email = normalize_email(needle)
        phone = normalize_phone(needle)
        name = _normalize_name(needle)
        matches: dict[str, ContactCandidate] = {}
        identity_values = {value for value in (email, phone) if value}
        if identity_values:
            identity_rows = self.session.scalars(
                select(CrmIdentityRow).where(CrmIdentityRow.normalized_value.in_(identity_values))
            ).all()
            by_id = {row.id: row for row in rows}
            for identity in identity_rows:
                contact = by_id.get(identity.contact_id)
                if contact is not None:
                    fields = _contact_fields(contact)
                    matches[contact.id] = ContactCandidate(id=contact.id, name=fields["name"])
        for row in rows:
            fields = _contact_fields(row)
            is_match = bool(email and normalize_email(fields["email"]) == email)
            is_match = is_match or bool(phone and normalize_phone(fields["phone"]) == phone)
            is_match = is_match or bool(name and _normalize_name(fields["name"]) == name)
            if is_match:
                matches[row.id] = ContactCandidate(id=row.id, name=fields["name"])
        candidates = tuple(sorted(matches.values(), key=lambda item: item.id))
        if len(candidates) > 1:
            raise ReminderContactAmbiguous(candidates)
        return candidates[0].id if candidates else None

    def create(
        self,
        *,
        owner_id: str,
        source_event_id: str,
        text: str,
        scheduled_at: datetime,
        contact_query: str = "",
        explicit_unlinked: bool = False,
        now: datetime | None = None,
    ) -> ReminderCreateResult:
        owner_id = owner_id.strip()
        source_event_id = source_event_id.strip()
        if not owner_id or len(owner_id) > 32 or not owner_id.isdecimal():
            raise ReminderValidationError("invalid_owner_id")
        if not source_event_id or len(source_event_id) > 255:
            raise ReminderValidationError("invalid_source_event_id")

        existing = self.get_by_source_event(owner_id=owner_id, source_event_id=source_event_id)
        if existing is not None:
            self.session.commit()
            return ReminderCreateResult(
                reminder=existing,
                created=False,
                contact_unlinked=existing.contact_id is None,
            )

        effective_now = _utc(now or datetime.now(UTC))
        due = _utc(scheduled_at)
        reminder_text = text.strip()
        if not reminder_text or len(reminder_text) > MAX_REMINDER_TEXT:
            raise ReminderValidationError("invalid_text")
        if due <= effective_now:
            raise ReminderValidationError("past")

        contact_id = None if explicit_unlinked else self._find_contact(contact_query)
        if contact_query.strip() and contact_id is None and not explicit_unlinked:
            raise ReminderValidationError("contact_not_found")
        values = {
            "id": uuid4().hex,
            "owner_id": owner_id,
            "source_event_id": source_event_id,
            "text": reminder_text,
            "scheduled_at": due,
            "timezone": "Asia/Jerusalem",
            "contact_id": contact_id,
            "status": "active",
            "revision": 1,
            "delivery_state": "pending",
            "created_at": effective_now,
            "updated_at": effective_now,
        }
        dialect = self.session.get_bind().dialect.name
        builder = postgres_insert if dialect == "postgresql" else sqlite_insert
        statement = (
            builder(OwnerReminderRow.__table__)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["owner_id", "source_event_id"])
            .returning(OwnerReminderRow.id)
        )
        try:
            inserted_id = self.session.execute(statement).scalar_one_or_none()
            reminder_id = inserted_id or self.session.scalar(
                select(OwnerReminderRow.id).where(
                    OwnerReminderRow.owner_id == owner_id,
                    OwnerReminderRow.source_event_id == source_event_id,
                )
            )
            if reminder_id is None:
                raise ReminderConflict("duplicate_without_row")
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        row = self.session.scalar(
            select(OwnerReminderRow).where(
                OwnerReminderRow.owner_id == owner_id,
                OwnerReminderRow.id == reminder_id,
            )
        )
        if row is None:
            raise ReminderNotFound("reminder_not_found")
        return ReminderCreateResult(
            reminder=_view(row),
            created=inserted_id is not None,
            contact_unlinked=row.contact_id is None,
        )

    def get(self, *, owner_id: str, reminder_id: str) -> ReminderView | None:
        row = self.session.scalar(
            select(OwnerReminderRow).where(
                OwnerReminderRow.owner_id == owner_id,
                OwnerReminderRow.id == reminder_id,
            )
        )
        return _view(row) if row is not None else None

    def get_by_source_event(self, *, owner_id: str, source_event_id: str) -> ReminderView | None:
        row = self.session.scalar(
            select(OwnerReminderRow).where(
                OwnerReminderRow.owner_id == owner_id,
                OwnerReminderRow.source_event_id == source_event_id,
            )
        )
        return _view(row) if row is not None else None

    def list(self, *, owner_id: str, limit: int = 20, offset: int = 0) -> list[ReminderView]:
        bounded_limit = max(1, min(limit, 100))
        bounded_offset = max(0, min(offset, 10_000))
        rows = self.session.scalars(
            select(OwnerReminderRow)
            .where(
                OwnerReminderRow.owner_id == owner_id,
                OwnerReminderRow.status == "active",
            )
            .order_by(OwnerReminderRow.scheduled_at.asc(), OwnerReminderRow.id.asc())
            .offset(bounded_offset)
            .limit(bounded_limit)
        ).all()
        return [_view(row) for row in rows]

    def change(
        self,
        *,
        owner_id: str,
        reminder_id: str,
        action: ReminderAction,
        expected_revision: int,
        scheduled_at: datetime | None = None,
        now: datetime | None = None,
    ) -> ReminderChangeResult:
        if action not in {"complete", "cancel", "reschedule", "snooze"}:
            raise ReminderValidationError("invalid_action")
        effective_now = _utc(now or datetime.now(UTC))
        changes: dict[str, object] = {
            "revision": OwnerReminderRow.revision + 1,
            "updated_at": effective_now,
        }
        if action in {"complete", "cancel"}:
            if scheduled_at is not None:
                raise ReminderValidationError("unexpected_scheduled_at")
            changes["status"] = "completed" if action == "complete" else "cancelled"
        else:
            if scheduled_at is None:
                raise ReminderValidationError("missing_scheduled_at")
            due = _utc(scheduled_at)
            if due <= effective_now:
                raise ReminderValidationError("past")
            changes["scheduled_at"] = due
            changes["delivery_state"] = "pending"

        statement = (
            update(OwnerReminderRow)
            .where(
                OwnerReminderRow.owner_id == owner_id,
                OwnerReminderRow.id == reminder_id,
                OwnerReminderRow.status == "active",
                OwnerReminderRow.revision == expected_revision,
                OwnerReminderRow.delivery_state != "dispatching",
            )
            .values(**changes)
            .returning(OwnerReminderRow.id)
        )
        try:
            changed_id = self.session.execute(statement).scalar_one_or_none()
            if changed_id is None:
                current = self.session.scalar(
                    select(OwnerReminderRow).where(
                        OwnerReminderRow.owner_id == owner_id,
                        OwnerReminderRow.id == reminder_id,
                    )
                )
                self.session.rollback()
                if current is None:
                    raise ReminderNotFound("reminder_not_found")
                if current.delivery_state == "dispatching":
                    raise ReminderDispatching("dispatching")
                raise ReminderConflict("revision_or_status_conflict")
            self.session.commit()
        except ReminderError:
            raise
        except Exception:
            self.session.rollback()
            raise
        row = self.session.scalar(
            select(OwnerReminderRow).where(
                OwnerReminderRow.owner_id == owner_id,
                OwnerReminderRow.id == changed_id,
            )
        )
        if row is None:
            raise ReminderNotFound("reminder_not_found")
        return ReminderChangeResult(reminder=_view(row))

    def snapshot(self, *, owner_id: str, now: datetime | None = None) -> ReminderSnapshot:
        effective_now = _utc(now or datetime.now(UTC))
        counts = {"active": 0, "completed": 0, "cancelled": 0}
        for status, count in self.session.execute(
            select(OwnerReminderRow.status, func.count())
            .where(OwnerReminderRow.owner_id == owner_id)
            .group_by(OwnerReminderRow.status)
        ):
            counts[str(status)] = int(count)
        counts["due_pending"] = int(
            self.session.scalar(
                select(func.count()).select_from(OwnerReminderRow).where(
                    OwnerReminderRow.owner_id == owner_id,
                    OwnerReminderRow.status == "active",
                    OwnerReminderRow.scheduled_at <= effective_now,
                    OwnerReminderRow.delivery_state.in_(("pending", "rejected")),
                )
            )
            or 0
        )
        for delivery_state in ("unknown", "dispatching"):
            counts[delivery_state] = int(
                self.session.scalar(
                    select(func.count()).select_from(OwnerReminderRow).where(
                        OwnerReminderRow.owner_id == owner_id,
                        OwnerReminderRow.status == "active",
                        OwnerReminderRow.delivery_state == delivery_state,
                    )
                )
                or 0
            )
        rows = self.session.scalars(
            select(OwnerReminderRow)
            .where(
                OwnerReminderRow.owner_id == owner_id,
                OwnerReminderRow.status == "active",
                OwnerReminderRow.scheduled_at >= effective_now,
            )
            .order_by(OwnerReminderRow.scheduled_at.asc(), OwnerReminderRow.id.asc())
            .limit(5)
        ).all()
        return ReminderSnapshot(counts=counts, upcoming=tuple(_view(row) for row in rows))
