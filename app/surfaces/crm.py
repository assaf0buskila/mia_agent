"""Locked Contacts + Activity CRM. No 01 Leads. No lead IDs. No row without phone or email."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

LOCKED_SPREADSHEET_ID = "1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI"
CONTACTS_TAB = "Contacts"
ACTIVITY_TAB = "Activity"
FORBIDDEN_TABS = frozenset({"01 Leads", "10 Mia Activity"})
LEAD_ID_PREFIX = "lead_"

CONTACTS_HEADERS: tuple[str, ...] = (
    "שם",
    "טלפון",
    "אימייל",
    "תאריך",
    "עסק",
    "מקור",
    "שפה",
    "מה רוצים",
    "סטטוס",
    "סיכום שיחה",
    "הבא",
    "נוצר",
    "עודכן",
    "פינג לאסף",
)

ACTIVITY_HEADERS: tuple[str, ...] = (
    "מתי",
    "מי",
    "ערוץ",
    "מה עשתה",
    "תוצאה",
)

_IL_TZ = ZoneInfo("Asia/Jerusalem")


class CrmDenied(ValueError):
    """Refused a Contacts write that would invent identity or use a forbidden tab."""


@dataclass(frozen=True)
class ContactRecord:
    name: str = ""
    phone: str = ""
    email: str = ""
    date: str = ""
    business: str = ""
    source: str = ""
    language: str = ""
    want: str = ""
    status: str = ""
    summary: str = ""
    next_step: str = ""
    created: str = ""
    updated: str = ""
    pinged: str = ""

    def has_contact_key(self) -> bool:
        return bool(self.phone.strip() or self.email.strip())

    def contact_key(self) -> str:
        phone = normalize_phone(self.phone)
        email = normalize_email(self.email)
        if phone:
            return f"phone:{phone}"
        if email:
            return f"email:{email}"
        raise CrmDenied("no row without phone or email")

    def cells(self) -> list[str]:
        return [
            self.name.strip(),
            normalize_phone(self.phone),
            normalize_email(self.email),
            self.date.strip(),
            self.business.strip(),
            self.source.strip(),
            self.language.strip(),
            self.want.strip(),
            self.status.strip(),
            self.summary.strip(),
            self.next_step.strip(),
            self.created.strip(),
            self.updated.strip(),
            self.pinged.strip(),
        ]


@dataclass(frozen=True)
class ActivityRecord:
    when: str
    who: str
    channel: str
    action: str
    result: str

    def cells(self) -> list[str]:
        return [
            self.when.strip(),
            self.who.strip(),
            self.channel.strip(),
            self.action.strip(),
            self.result.strip(),
        ]


def now_israel(clock: datetime | None = None) -> str:
    moment = clock or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_IL_TZ).replace(microsecond=0).isoformat()


def normalize_phone(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+") and digits:
        return f"+{digits}"
    return digits


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def assert_allowed_contact(record: ContactRecord) -> None:
    if not record.has_contact_key():
        raise CrmDenied("no row without phone or email")
    blob = " ".join(record.cells())
    if LEAD_ID_PREFIX in blob.lower():
        raise CrmDenied("lead ids are not used")


def merge_contact(existing: ContactRecord | None, incoming: ContactRecord) -> ContactRecord:
    if existing is None:
        return incoming
    return ContactRecord(
        name=incoming.name or existing.name,
        phone=incoming.phone or existing.phone,
        email=incoming.email or existing.email,
        date=incoming.date or existing.date,
        business=incoming.business or existing.business,
        source=incoming.source or existing.source,
        language=incoming.language or existing.language,
        want=incoming.want or existing.want,
        status=incoming.status or existing.status,
        summary=incoming.summary or existing.summary,
        next_step=incoming.next_step or existing.next_step,
        created=existing.created or incoming.created,
        updated=incoming.updated or existing.updated,
        pinged=incoming.pinged or existing.pinged,
    )


class ContactsCrm(Protocol):
    spreadsheet_id: str

    def upsert_contact(self, record: ContactRecord) -> ContactRecord: ...

    def append_activity(self, record: ActivityRecord) -> None: ...

    def written_tabs(self) -> tuple[str, ...]: ...


@dataclass
class FakeContactsCrm:
    """In-memory Contacts/Activity. Tests prove the lock without a live Sheet."""

    spreadsheet_id: str = LOCKED_SPREADSHEET_ID
    contacts: dict[str, ContactRecord] = field(default_factory=dict)
    activity: list[ActivityRecord] = field(default_factory=list)
    tabs: list[str] = field(default_factory=list)
    cells_written: list[list[str]] = field(default_factory=list)

    def upsert_contact(self, record: ContactRecord) -> ContactRecord:
        assert_allowed_contact(record)
        if CONTACTS_TAB in FORBIDDEN_TABS:
            raise CrmDenied("forbidden tab")
        key = record.contact_key()
        merged = merge_contact(self.contacts.get(key), record)
        if merged.phone and merged.email:
            phone_key = f"phone:{normalize_phone(merged.phone)}"
            email_key = f"email:{normalize_email(merged.email)}"
            prior = self.contacts.get(phone_key) or self.contacts.get(email_key)
            merged = merge_contact(prior, merged)
            self.contacts.pop(phone_key, None)
            self.contacts.pop(email_key, None)
            self.contacts[phone_key] = merged
            self.contacts[email_key] = merged
        else:
            self.contacts[key] = merged
        self.tabs.append(CONTACTS_TAB)
        self.cells_written.append(merged.cells())
        return merged

    def append_activity(self, record: ActivityRecord) -> None:
        cells = record.cells()
        if any(LEAD_ID_PREFIX in cell.lower() for cell in cells):
            raise CrmDenied("lead ids are not used")
        self.activity.append(record)
        self.tabs.append(ACTIVITY_TAB)
        self.cells_written.append(cells)

    def written_tabs(self) -> tuple[str, ...]:
        return tuple(self.tabs)


@dataclass
class DisabledContactsCrm:
    spreadsheet_id: str = LOCKED_SPREADSHEET_ID

    def upsert_contact(self, record: ContactRecord) -> ContactRecord:
        assert_allowed_contact(record)
        return record

    def append_activity(self, record: ActivityRecord) -> None:
        del record

    def written_tabs(self) -> tuple[str, ...]:
        return ()


class SheetsContactsCrm:
    """Live Contacts/Activity writer. Never targets 01 Leads or mints a lead_ id."""

    def __init__(self, port: object, *, spreadsheet_id: str = LOCKED_SPREADSHEET_ID) -> None:
        self._port = port
        self.spreadsheet_id = spreadsheet_id
        self._tabs: list[str] = []

    def upsert_contact(self, record: ContactRecord) -> ContactRecord:
        assert_allowed_contact(record)
        key_column = "טלפון" if normalize_phone(record.phone) else "אימייל"
        writer = getattr(self._port, "write_locked_contact", None)
        if callable(writer):
            writer(record.cells(), key_column=key_column)
        self._tabs.append(CONTACTS_TAB)
        return record

    def append_activity(self, record: ActivityRecord) -> None:
        cells = record.cells()
        if any(LEAD_ID_PREFIX in cell.lower() for cell in cells):
            raise CrmDenied("lead ids are not used")
        writer = getattr(self._port, "append_locked_activity", None)
        if callable(writer):
            writer(cells)
        self._tabs.append(ACTIVITY_TAB)

    def written_tabs(self) -> tuple[str, ...]:
        return tuple(self._tabs)


def resolved_spreadsheet_id(settings: object | None = None) -> str:
    """Env override if set; otherwise the locked Contacts workbook."""
    if settings is not None:
        resolver = getattr(settings, "resolved_sheets_spreadsheet_id", None)
        if callable(resolver):
            return str(resolver())
        raw = str(getattr(settings, "sheets_spreadsheet_id", "") or "").strip()
        if raw:
            return raw
    return LOCKED_SPREADSHEET_ID


def build_contacts_crm(settings: object | None = None, port: object | None = None) -> ContactsCrm:
    spreadsheet_id = resolved_spreadsheet_id(settings)
    if port is not None and hasattr(port, "write_locked_contact"):
        return SheetsContactsCrm(port, spreadsheet_id=spreadsheet_id)
    return DisabledContactsCrm(spreadsheet_id=spreadsheet_id)


def log_contact(
    crm: ContactsCrm,
    record: ContactRecord,
    *,
    who: str,
    channel: str,
    action: str,
    result: str,
    clock: datetime | None = None,
) -> ContactRecord:
    """Upsert Contacts and append Activity. Refuses a row with no phone or email."""
    stamp = now_israel(clock)
    stamped = ContactRecord(
        name=record.name,
        phone=record.phone,
        email=record.email,
        date=record.date,
        business=record.business,
        source=record.source,
        language=record.language,
        want=record.want,
        status=record.status or "פתוח",
        summary=record.summary,
        next_step=record.next_step,
        created=record.created or stamp,
        updated=stamp,
        pinged=record.pinged,
    )
    written = crm.upsert_contact(stamped)
    crm.append_activity(
        ActivityRecord(
            when=stamp,
            who=who,
            channel=channel,
            action=action,
            result=result,
        )
    )
    return written
