"""Owner-only, approval-bound personal calendar create and reschedule actions.

This deliberately accepts a compact, unambiguous request grammar.  A model never
selects an event or fills in a date: the exact title, time, duration, timezone and
for moves the provider event id are persisted, hashed, approved, and revalidated
before the Composio write.  Calendar deletion/cancellation is not implemented.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.core.write_flags import write_flag_enabled
from app.domain.approvals import (
    ACTION_CALENDAR_CREATE,
    ACTION_CALENDAR_RESCHEDULE,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    RESOURCE_CALENDAR,
    RISK_R3,
    approval_expires_at,
    extract_approval_id,
    is_approval_expired,
)
from app.domain.calendar_write_gate import ASK_ASSAF, assess_calendar_write
from app.domain.events import Channel, build_approval_required_event
from app.domain.meeting_slots import sanitize_event_id
from app.integrations.calendar import CalendarPort
from app.integrations.calendar_booking import (
    BookingLookupStatus,
    CalendarBookingPort,
    DisabledCalendarBookingPort,
)

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.db.store import LeadStore

_CREATE_RE = re.compile(
    r"^(?:create (?:calendar )?event|add (?:calendar )?event|צור אירוע|תוסיף אירוע)\s*:\s*"
    r"(?P<title>[^|]{1,120})\|\s*(?P<start>[^|]+?)\s*\|\s*(?P<minutes>\d{1,3})"
    r"(?:\s*\|\s*(?P<timezone>[A-Za-z_/-]+))?\s*$",
    re.IGNORECASE,
)
_MOVE_RE = re.compile(
    r"^(?:move (?:calendar )?event|reschedule (?:calendar )?event|העבר אירוע|דחה אירוע)\s*:\s*"
    r"(?P<event_id>[^|]+?)\|\s*(?P<start>[^|]+?)\s*\|\s*(?P<minutes>\d{1,3})"
    r"(?:\s*\|\s*(?P<timezone>[A-Za-z_/-]+))?\s*$",
    re.IGNORECASE,
)
_APPROVE = ("approve calendar", "approve event", "אשר אירוע", "אשר את האירוע")
_REJECT = ("reject calendar", "reject event", "דחה אירוע", "בטל אישור אירוע")


@dataclass(frozen=True)
class CalendarChange:
    action: Literal["calendar_create", "calendar_reschedule"]
    resource_id: str
    title: str
    start: datetime
    end: datetime
    timezone: str
    event_id: str = ""


def _parse_datetime(raw: str, timezone: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        return value.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _proposal_id(
    *, action: str, title: str, start: datetime, end: datetime, timezone: str, event_id: str
) -> str:
    payload = json.dumps(
        {
            "a": action,
            "e": event_id,
            "end": end.isoformat(),
            "s": start.isoformat(),
            "t": title,
            "z": timezone,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "cal_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _parameters(change: CalendarChange) -> str:
    return json.dumps(
        {
            "end": change.end.isoformat(),
            "event_id": change.event_id,
            "start": change.start.isoformat(),
            "title": change.title,
            "timezone": change.timezone,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_hash(*, action: str, channel: str, resource_id: str, parameters: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "action": action,
                "channel": channel,
                "parameters": parameters,
                "resource_id": resource_id,
                "resource_type": RESOURCE_CALENDAR,
                "risk": RISK_R3,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def parse_calendar_change_request(text: str, *, default_timezone: str) -> CalendarChange | None:
    match = _CREATE_RE.match(text.strip()) or _MOVE_RE.match(text.strip())
    if match is None:
        return None
    title = match.groupdict().get("title", "").strip() or "אירוע ביומן"
    timezone = (match.group("timezone") or default_timezone).strip()
    try:
        ZoneInfo(timezone)
        minutes = int(match.group("minutes"))
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if minutes < 5 or minutes > 720:
        return None
    start = _parse_datetime(match.group("start"), timezone)
    if start is None:
        return None
    end = start + timedelta(minutes=minutes)
    event_id = ""
    action = ACTION_CALENDAR_CREATE
    if _MOVE_RE.match(text.strip()):
        event_id = sanitize_event_id(match.group("event_id").strip()) or ""
        if not event_id:
            return None
        action = ACTION_CALENDAR_RESCHEDULE
    resource_id = _proposal_id(
        action=action, title=title, start=start, end=end, timezone=timezone, event_id=event_id
    )
    return CalendarChange(
        action=action,
        resource_id=resource_id,
        title=title,
        start=start,
        end=end,
        timezone=timezone,
        event_id=event_id,
    )


def calendar_request_help() -> str:
    return (
        "כדי שאכין בקשה לאישור, כתוב למשל: \n"
        "צור אירוע: שם האירוע | 2026-09-02T10:00 | 60 | Asia/Jerusalem\n"
        "או: העבר אירוע: event_id | 2026-09-02T10:00 | 60 | Asia/Jerusalem.\n"
        "לא יוצרת, מזיזה, מבטלת או שולחת הזמנות בלי אישור שלך."
    )


def parse_calendar_change_decision(text: str) -> str | None:
    lowered = text.lower()
    wants_approve = any(phrase in lowered or phrase in text for phrase in _APPROVE)
    wants_reject = any(phrase in lowered or phrase in text for phrase in _REJECT)
    if wants_approve == wants_reject:
        return None
    return DECISION_APPROVED if wants_approve else DECISION_REJECTED


def apply_owner_calendar_change_request(
    store: LeadStore,
    *,
    text: str,
    channel: Channel,
    kill_switch: bool,
    demo_active: bool,
    default_timezone: str,
) -> str | None:
    if not (_CREATE_RE.match(text.strip()) or _MOVE_RE.match(text.strip())):
        return None
    if demo_active or kill_switch:
        return "לא הכנתי שינוי ביומן במצב הזה. לא שיניתי כלום."
    change = parse_calendar_change_request(text, default_timezone=default_timezone)
    if change is None:
        return calendar_request_help()
    gate = assess_calendar_write(
        title=change.title,
        start=change.start,
        end=change.end,
        location=change.title,
    )
    if not gate.allowed:
        return gate.ask_assaf or ASK_ASSAF
    try:
        assert_allowed(
            RiskAction(name="calendar_change_proposal", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return "לא הכנתי שינוי ביומן. לא שיניתי כלום."
    parameters = _parameters(change)
    digest = _payload_hash(
        action=change.action,
        channel=channel.value,
        resource_id=change.resource_id,
        parameters=parameters,
    )
    store.upsert_calendar_approval(
        channel=channel.value,
        action=change.action,
        risk=RISK_R3,
        payload_hash=digest,
        decision=DECISION_PENDING,
        resource_id=change.resource_id,
        expires_at=approval_expires_at(now=datetime.now(UTC)),
        proposed_parameters=parameters,
    )
    row = store.get_approval_by_resource(RESOURCE_CALENDAR, change.resource_id, change.action)
    if row is None:
        return "לא הצלחתי לרשום את בקשת האישור ליומן. לא שיניתי כלום."
    claim_key = f"{change.resource_id}:approval:{change.action}"
    if store.claim_operation(scope="approval", key=claim_key):
        store.save_canonical_event(
            provider=channel.value,
            event=build_approval_required_event(
                provider=channel.value,
                channel=channel,
                lead_id=None,
                action=change.action,
                risk=RISK_R3,
                resource_id=change.resource_id,
            ),
        )
        store.complete_operation(scope="approval", key=claim_key, result_json='{"ok": true}')
    verb = "יצירה" if change.action == ACTION_CALENDAR_CREATE else "העברה"
    local_start = change.start.astimezone(ZoneInfo(change.timezone)).strftime("%d.%m %H:%M")
    return (
        f"בקשת {verb} מוכנה: {change.title}, {local_start}, {change.timezone}. "
        "לא שיניתי ביומן. אשר בלחצן למטה."
    )


def _row_change(row) -> CalendarChange | None:
    try:
        value = json.loads(row.proposed_parameters)
        if not isinstance(value, dict):
            return None
        action = row.action
        if action not in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE):
            return None
        timezone = str(value["timezone"])
        title = str(value["title"])
        start = datetime.fromisoformat(str(value["start"]))
        end = datetime.fromisoformat(str(value["end"]))
        event_id = str(value.get("event_id") or "")
        if (
            not title
            or len(title) > 120
            or start.tzinfo is None
            or end.tzinfo is None
            or start >= end
        ):
            return None
        if action == ACTION_CALENDAR_RESCHEDULE and not sanitize_event_id(event_id):
            return None
        expected = _payload_hash(
            action=action,
            channel=row.channel,
            resource_id=row.resource_id,
            parameters=_parameters(
                CalendarChange(
                    action=action,
                    resource_id=row.resource_id,
                    title=title,
                    start=start,
                    end=end,
                    timezone=timezone,
                    event_id=event_id,
                )
            ),
        )
        if row.payload_hash != expected:
            return None
        return CalendarChange(
            action=action,
            resource_id=row.resource_id,
            title=title,
            start=start,
            end=end,
            timezone=timezone,
            event_id=event_id,
        )
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None


def decide_calendar_change(
    store: LeadStore, *, text: str, kill_switch: bool
) -> tuple[str | None, str | None]:
    requested = parse_calendar_change_decision(text)
    if requested is None:
        return None, None
    decision = requested
    row = store.get_approval_by_approval_id(extract_approval_id(text) or "")
    if row is None:
        pending = [
            item
            for item in store.list_all_pending_approvals()
            if item.action in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE)
            and item.decision == DECISION_PENDING
        ]
        if len(pending) != 1:
            return ("ambiguous" if pending else "none"), None
        row = pending[0]
    if row.action not in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE):
        return "unbound", None
    if row.decision != DECISION_PENDING:
        return "already_decided", row.resource_id
    if kill_switch:
        return "skipped", row.resource_id
    change = _row_change(row)
    if change is None or is_approval_expired(row, now=datetime.now(UTC)):
        return "unbound", row.resource_id
    if not store.decide_calendar_approval(
        resource_id=row.resource_id, action=row.action, decision=decision
    ):
        return "none", row.resource_id
    return decision, row.resource_id


def execute_approved_calendar_change(
    *,
    store: LeadStore,
    settings: Settings,
    calendar: CalendarPort,
    booking: CalendarBookingPort,
    resource_id: str,
    kill_switch: bool,
    demo_active: bool,
) -> str:
    if demo_active or kill_switch:
        return "לא שיניתי ביומן במצב הזה."
    row = next(
        (
            item
            for item in store.list_all_pending_approvals()
            if item.resource_type == RESOURCE_CALENDAR and item.resource_id == resource_id
        ),
        None,
    )
    # Approved rows are intentionally not "pending", so find direct by each possible action.
    row = (
        row
        or store.get_approval_by_resource(RESOURCE_CALENDAR, resource_id, ACTION_CALENDAR_CREATE)
        or store.get_approval_by_resource(
            RESOURCE_CALENDAR, resource_id, ACTION_CALENDAR_RESCHEDULE
        )
    )
    if row is None or row.decision != DECISION_APPROVED:
        return "לא שיניתי ביומן. הבקשה אינה מאושרת."
    change = _row_change(row)
    if change is None or is_approval_expired(row, now=datetime.now(UTC)):
        return "לא שיניתי ביומן. האישור אינו תקף."
    if not write_flag_enabled(settings, "calendar_write"):
        return "הכתיבה ליומן כבויה. לא שיניתי כלום."
    try:
        assert_allowed(
            RiskAction(name=change.action, risk=RiskLevel.R3_COMMERCIAL), kill_switch=kill_switch
        )
    except PolicyDenied:
        return "לא שיניתי ביומן."
    operation_key = f"{resource_id}:execute:{change.action}"
    prior_write_status = store.get_provider_write_status(scope="approval", key=operation_key)
    if prior_write_status == "completed":
        return "השינוי ביומן כבר טופל. לא ביצעתי אותו שוב."
    if prior_write_status in {"pending_review", "provider_claimed"}:
        # Reconcile before availability preflight: a successfully-created event can
        # make its own slot unavailable after a crash between provider success and
        # our result commit.
        try:
            if change.action == ACTION_CALENDAR_CREATE:
                booking_key = "mia_" + hashlib.sha256(resource_id.encode()).hexdigest()
                lookup = booking.find_by_booking_key(booking_key=booking_key)
                reconciled = lookup.status == BookingLookupStatus.FOUND
            else:
                lookup = booking.get_event(event_id=change.event_id, timezone=change.timezone)
                reconciled = (
                    lookup.status == BookingLookupStatus.FOUND
                    and lookup.event is not None
                    and lookup.event.start == change.start
                    and lookup.event.end == change.end
                )
        except Exception:
            reconciled = False
        if reconciled and store.complete_provider_write(
            scope="approval", key=operation_key, result_json='{"ok": true}'
        ):
            return (
                "יצרתי את האירוע ביומן."
                if change.action == ACTION_CALENDAR_CREATE
                else "העברתי את האירוע ביומן."
            )
        return "תוצאת השינוי ביומן ממתינה לבדיקה. לא ביצעתי אותו שוב."
    try:
        duration_minutes = max(5, int((change.end - change.start).total_seconds() // 60))
        slots = calendar.find_free_slots(
            time_min=change.start,
            # The port returns only intervals contained in its requested window.
            # Extend it by one duration so a free interval covering the requested
            # end is not discarded before this exact containment check.
            time_max=change.end + timedelta(minutes=duration_minutes),
            duration_minutes=duration_minutes,
            timezone=change.timezone,
        )
        gate = assess_calendar_write(
            title=change.title,
            start=change.start,
            end=change.end,
            location=change.title,
            slots=slots,
        )
        if not gate.allowed:
            return gate.ask_assaf or ASK_ASSAF
        if isinstance(booking, DisabledCalendarBookingPort):
            return "Calendar לא מחובר לכתיבה. לא שיניתי כלום."
    except Exception:
        return "לא הצלחתי לאמת את המועד ביומן. לא שיניתי כלום."

    if not store.claim_provider_write(scope="approval", key=operation_key):
        # A durable claim can survive a crash after the provider accepted the write.
        # Reconcile only against an exact provider identifier; never reissue a write.
        try:
            if change.action == ACTION_CALENDAR_CREATE:
                booking_key = "mia_" + hashlib.sha256(resource_id.encode()).hexdigest()
                lookup = booking.find_by_booking_key(booking_key=booking_key)
                reconciled = lookup.status == BookingLookupStatus.FOUND
            else:
                lookup = booking.get_event(event_id=change.event_id, timezone=change.timezone)
                reconciled = (
                    lookup.status == BookingLookupStatus.FOUND
                    and lookup.event is not None
                    and lookup.event.start == change.start
                    and lookup.event.end == change.end
                )
        except Exception:
            reconciled = False
        if reconciled:
            if store.complete_provider_write(
                scope="approval", key=operation_key, result_json='{"ok": true}'
            ):
                return (
                    "יצרתי את האירוע ביומן."
                    if change.action == ACTION_CALENDAR_CREATE
                    else "העברתי את האירוע ביומן."
                )
        return "תוצאת השינוי ביומן ממתינה לבדיקה. לא ביצעתי אותו שוב."

    try:
        if change.action == ACTION_CALENDAR_CREATE:
            result = booking.create_event(
                booking_key="mia_" + hashlib.sha256(resource_id.encode()).hexdigest(),
                start=change.start,
                end=change.end,
                timezone=change.timezone,
                summary=change.title,
                create_meeting_room=False,
                allow_nonstandard_duration=True,
            )
        else:
            existing = booking.get_event(event_id=change.event_id, timezone=change.timezone)
            if existing.status != BookingLookupStatus.FOUND:
                store.mark_provider_write_pending_review(scope="approval", key=operation_key)
                return "לא מצאתי את האירוע להעברה. לא שיניתי כלום."
            result = booking.patch_event(
                event_id=change.event_id,
                start=change.start,
                end=change.end,
                timezone=change.timezone,
                allow_nonstandard_duration=True,
            )
    except Exception:
        store.mark_provider_write_pending_review(scope="approval", key=operation_key)
        return "תוצאת השינוי ביומן אינה ודאית וממתינה לבדיקה. לא ביצעתי אותו שוב."
    if result is None:
        store.mark_provider_write_pending_review(scope="approval", key=operation_key)
        return "תוצאת השינוי ביומן אינה ודאית וממתינה לבדיקה. לא ביצעתי אותו שוב."
    if not store.complete_provider_write(
        scope="approval", key=operation_key, result_json='{"ok": true}'
    ):
        return "השינוי ביומן התקבל אך תוצאתו ממתינה לבדיקה. לא ביצעתי אותו שוב."
    return (
        "יצרתי את האירוע ביומן."
        if change.action == ACTION_CALENDAR_CREATE
        else "העברתי את האירוע ביומן."
    )
