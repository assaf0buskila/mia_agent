"""Google Calendar event create, exact read, and narrow reschedule port.

Separate from read-only ``CalendarPort``. Composio toolkit ``GOOGLECALENDAR``
version ``20260812_00``; pins ``GOOGLECALENDAR_EVENTS_LIST`` and
``GOOGLECALENDAR_CREATE_EVENT`` for booking plus ``GOOGLECALENDAR_EVENTS_GET``
and ``GOOGLECALENDAR_PATCH_EVENT`` for ADR-013 rescheduling. Full event PUT and
delete tools are intentionally unavailable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, field_validator, model_validator

from app.core.config import Settings
from app.domain.meetings.slots import sanitize_event_id, sanitize_meet_link, to_utc_aware
from app.domain.tools import AdapterHttpError
from app.integrations.calendar import COMPOSIO_GOOGLECALENDAR_VERSION

COMPOSIO_EVENTS_LIST_TOOL = "GOOGLECALENDAR_EVENTS_LIST"
COMPOSIO_CREATE_EVENT_TOOL = "GOOGLECALENDAR_CREATE_EVENT"
COMPOSIO_EVENTS_GET_TOOL = "GOOGLECALENDAR_EVENTS_GET"
COMPOSIO_PATCH_EVENT_TOOL = "GOOGLECALENDAR_PATCH_EVENT"
_EVENTS_LIST_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_EVENTS_LIST_TOOL}"
)
_CREATE_EVENT_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_CREATE_EVENT_TOOL}"
)
_EVENTS_GET_URL = f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_EVENTS_GET_TOOL}"
_PATCH_EVENT_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_PATCH_EVENT_TOOL}"
)

MEETING_DURATION_MINUTES = 30
_BOOKING_KEY_MAX = 128
_LOOKUP_MAX_PAGES = 3
_LOOKUP_PAGE_SIZE = 5


class BookingLookupStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"


class CalendarBookingEvent(BaseModel):
    event_id: str
    meet_link: str = ""
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("event_id")
    @classmethod
    def event_id_must_be_valid(cls, value: str) -> str:
        cleaned = sanitize_event_id(value)
        if cleaned is None:
            raise ValueError("invalid calendar event id")
        return cleaned

    @field_validator("meet_link")
    @classmethod
    def meet_link_must_be_valid(cls, value: str) -> str:
        if not value:
            return ""
        cleaned = sanitize_meet_link(value)
        if not cleaned:
            raise ValueError("invalid meet link")
        return cleaned

    @field_validator("start", "end")
    @classmethod
    def event_times_must_be_utc_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        normalized = to_utc_aware(value)
        if normalized is None:
            raise ValueError("calendar event time must be timezone-aware")
        return normalized

    @model_validator(mode="after")
    def ordered_event_times(self) -> CalendarBookingEvent:
        if (self.start is None) != (self.end is None):
            raise ValueError("calendar event start and end must appear together")
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("calendar event start must precede end")
        return self


class BookingLookupResult(BaseModel):
    status: BookingLookupStatus
    event: CalendarBookingEvent | None = None

    @field_validator("event")
    @classmethod
    def event_only_when_found(
        cls, value: CalendarBookingEvent | None, info: Any
    ) -> CalendarBookingEvent | None:
        status = info.data.get("status")
        if status == BookingLookupStatus.FOUND and value is None:
            raise ValueError("found lookup requires event")
        if status != BookingLookupStatus.FOUND and value is not None:
            raise ValueError("event only allowed for found lookup")
        return value


class EventLookupResult(BaseModel):
    status: BookingLookupStatus
    event: CalendarBookingEvent | None = None

    @model_validator(mode="after")
    def found_requires_complete_event(self) -> EventLookupResult:
        if self.status == BookingLookupStatus.FOUND:
            if self.event is None or self.event.start is None or self.event.end is None:
                raise ValueError("found event lookup requires exact start and end")
        elif self.event is not None:
            raise ValueError("event only allowed for found lookup")
        return self


class CalendarBookingPort(Protocol):
    def find_by_booking_key(
        self,
        *,
        booking_key: str,
        calendar_id: str = "primary",
    ) -> BookingLookupResult: ...

    def create_event(
        self,
        *,
        booking_key: str,
        start: datetime,
        end: datetime,
        timezone: str,
        calendar_id: str = "primary",
        summary: str = "AssafWeb intro call",
        create_meeting_room: bool = True,
        allow_nonstandard_duration: bool = False,
    ) -> CalendarBookingEvent | None: ...

    def get_event(
        self,
        *,
        event_id: str,
        calendar_id: str = "primary",
        timezone: str = "Asia/Jerusalem",
    ) -> EventLookupResult: ...

    def patch_event(
        self,
        *,
        event_id: str,
        start: datetime,
        end: datetime,
        timezone: str,
        calendar_id: str = "primary",
        allow_nonstandard_duration: bool = False,
    ) -> CalendarBookingEvent | None: ...


class DisabledCalendarBookingPort:
    def find_by_booking_key(
        self,
        *,
        booking_key: str,
        calendar_id: str = "primary",
    ) -> BookingLookupResult:
        del booking_key, calendar_id
        return BookingLookupResult(status=BookingLookupStatus.ERROR)

    def create_event(
        self,
        *,
        booking_key: str,
        start: datetime,
        end: datetime,
        timezone: str,
        calendar_id: str = "primary",
        summary: str = "AssafWeb intro call",
        create_meeting_room: bool = True,
        allow_nonstandard_duration: bool = False,
    ) -> CalendarBookingEvent | None:
        del (
            booking_key,
            start,
            end,
            timezone,
            calendar_id,
            summary,
            create_meeting_room,
            allow_nonstandard_duration,
        )
        return None

    def get_event(
        self,
        *,
        event_id: str,
        calendar_id: str = "primary",
        timezone: str = "Asia/Jerusalem",
    ) -> EventLookupResult:
        del event_id, calendar_id, timezone
        return EventLookupResult(status=BookingLookupStatus.ERROR)

    def patch_event(
        self,
        *,
        event_id: str,
        start: datetime,
        end: datetime,
        timezone: str,
        calendar_id: str = "primary",
        allow_nonstandard_duration: bool = False,
    ) -> CalendarBookingEvent | None:
        del event_id, start, end, timezone, calendar_id, allow_nonstandard_duration
        return None


class FakeCalendarBookingPort:
    """Test double recording calls and configurable lookup/create results."""

    def __init__(
        self,
        *,
        existing: dict[str, CalendarBookingEvent] | None = None,
        create_result: CalendarBookingEvent | None = None,
        lookup_errors: set[str] | None = None,
        default_lookup: BookingLookupStatus = BookingLookupStatus.NOT_FOUND,
        create_returns_none: bool = False,
        events_by_id: dict[str, CalendarBookingEvent] | None = None,
        get_errors: set[str] | None = None,
        get_not_found: set[str] | None = None,
        patch_returns_none: bool = False,
        patch_updates_event: bool = True,
    ) -> None:
        self.existing = dict(existing or {})
        self.create_result = create_result
        self.lookup_errors = set(lookup_errors or [])
        self.default_lookup = default_lookup
        self.create_returns_none = create_returns_none
        self.events_by_id = dict(events_by_id or {})
        self.get_errors = set(get_errors or [])
        self.get_not_found = set(get_not_found or [])
        self.patch_returns_none = patch_returns_none
        self.patch_updates_event = patch_updates_event
        self.lookup_calls: list[dict[str, str]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, str]] = []
        self.patch_calls: list[dict[str, Any]] = []

    def find_by_booking_key(
        self,
        *,
        booking_key: str,
        calendar_id: str = "primary",
    ) -> BookingLookupResult:
        self.lookup_calls.append({"booking_key": booking_key, "calendar_id": calendar_id})
        if booking_key in self.lookup_errors:
            return BookingLookupResult(status=BookingLookupStatus.ERROR)
        event = self.existing.get(booking_key)
        if event is not None:
            return BookingLookupResult(status=BookingLookupStatus.FOUND, event=event)
        if self.default_lookup == BookingLookupStatus.ERROR:
            return BookingLookupResult(status=BookingLookupStatus.ERROR)
        return BookingLookupResult(status=BookingLookupStatus.NOT_FOUND)

    def create_event(
        self,
        *,
        booking_key: str,
        start: datetime,
        end: datetime,
        timezone: str,
        calendar_id: str = "primary",
        summary: str = "AssafWeb intro call",
        create_meeting_room: bool = True,
        allow_nonstandard_duration: bool = False,
    ) -> CalendarBookingEvent | None:
        self.create_calls.append(
            {
                "booking_key": booking_key,
                "start": start,
                "end": end,
                "timezone": timezone,
                "calendar_id": calendar_id,
                "summary": summary,
                "create_meeting_room": create_meeting_room,
                "allow_nonstandard_duration": allow_nonstandard_duration,
            }
        )
        if booking_key in self.existing:
            return self.existing[booking_key]
        if self.create_result is not None:
            self.existing[booking_key] = self.create_result
            if self.create_returns_none:
                return None
            return self.create_result
        event = CalendarBookingEvent(
            event_id=f"evt_fake_{booking_key[-12:]}",
            meet_link="https://meet.google.com/abc-defg-hij",
        )
        self.existing[booking_key] = event
        if self.create_returns_none:
            return None
        return event

    def get_event(
        self,
        *,
        event_id: str,
        calendar_id: str = "primary",
        timezone: str = "Asia/Jerusalem",
    ) -> EventLookupResult:
        self.get_calls.append(
            {
                "event_id": event_id,
                "calendar_id": calendar_id,
                "timezone": timezone,
            }
        )
        if event_id in self.get_errors:
            return EventLookupResult(status=BookingLookupStatus.ERROR)
        if event_id in self.get_not_found:
            return EventLookupResult(status=BookingLookupStatus.NOT_FOUND)
        event = self.events_by_id.get(event_id)
        if event is None:
            return EventLookupResult(status=BookingLookupStatus.NOT_FOUND)
        if event.start is None or event.end is None:
            return EventLookupResult(status=BookingLookupStatus.ERROR)
        return EventLookupResult(status=BookingLookupStatus.FOUND, event=event)

    def patch_event(
        self,
        *,
        event_id: str,
        start: datetime,
        end: datetime,
        timezone: str,
        calendar_id: str = "primary",
        allow_nonstandard_duration: bool = False,
    ) -> CalendarBookingEvent | None:
        self.patch_calls.append(
            {
                "event_id": event_id,
                "start": start,
                "end": end,
                "timezone": timezone,
                "calendar_id": calendar_id,
                "allow_nonstandard_duration": allow_nonstandard_duration,
            }
        )
        prior = self.events_by_id.get(event_id)
        meet_link = prior.meet_link if prior is not None else ""
        patched = CalendarBookingEvent(
            event_id=event_id,
            meet_link=meet_link,
            start=start,
            end=end,
        )
        if self.patch_updates_event:
            self.events_by_id[event_id] = patched
        if self.patch_returns_none:
            return None
        return patched


class ComposioCalendarBookingPort:
    """Live Composio execute adapter. Raises AdapterHttpError on HTTP/transport."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._client = client

    def find_by_booking_key(
        self,
        *,
        booking_key: str,
        calendar_id: str = "primary",
    ) -> BookingLookupResult:
        if not _valid_booking_key(booking_key):
            return BookingLookupResult(status=BookingLookupStatus.ERROR)
        page_token: str | None = None
        for page_index in range(_LOOKUP_MAX_PAGES):
            arguments: dict[str, Any] = {
                "calendarId": calendar_id,
                "privateExtendedProperty": f"mia_booking_key={booking_key}",
                "maxResults": _LOOKUP_PAGE_SIZE,
                "showDeleted": False,
            }
            if page_token:
                arguments["pageToken"] = page_token
            body = self._execute(_EVENTS_LIST_URL, arguments)
            if body is None:
                return BookingLookupResult(status=BookingLookupStatus.ERROR)
            if not _valid_lookup_page(body):
                return BookingLookupResult(status=BookingLookupStatus.ERROR)
            event = _parse_lookup_match(body, booking_key=booking_key)
            if event is not None:
                return BookingLookupResult(status=BookingLookupStatus.FOUND, event=event)
            page_token = _next_page_token(body)
            if not page_token:
                return BookingLookupResult(status=BookingLookupStatus.NOT_FOUND)
        if page_token:
            return BookingLookupResult(status=BookingLookupStatus.ERROR)
        return BookingLookupResult(status=BookingLookupStatus.NOT_FOUND)

    def create_event(
        self,
        *,
        booking_key: str,
        start: datetime,
        end: datetime,
        timezone: str,
        calendar_id: str = "primary",
        summary: str = "AssafWeb intro call",
        create_meeting_room: bool = True,
        allow_nonstandard_duration: bool = False,
    ) -> CalendarBookingEvent | None:
        if not _valid_booking_key(booking_key):
            return None
        summary = summary.strip()
        if not summary or len(summary) > 120:
            return None
        if not _valid_timezone(timezone):
            return None
        start_utc = to_utc_aware(start)
        end_utc = to_utc_aware(end)
        if start_utc is None or end_utc is None:
            return None
        if start_utc >= end_utc:
            return None
        duration = end_utc - start_utc
        if (
            not allow_nonstandard_duration
            and duration != timedelta(minutes=MEETING_DURATION_MINUTES)
        ) or duration < timedelta(minutes=5) or duration > timedelta(hours=12):
            return None
        try:
            local_start = start_utc.astimezone(ZoneInfo(timezone))
            local_end = end_utc.astimezone(ZoneInfo(timezone))
        except ZoneInfoNotFoundError:
            return None
        arguments = {
            "calendar_id": calendar_id,
            "summary": summary,
            "start_datetime": local_start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_datetime": local_end.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": timezone,
            "send_updates": "none",
            "create_meeting_room": create_meeting_room,
            "transparency": "opaque",
            "visibility": "private",
            "guestsCanModify": False,
            "guestsCanInviteOthers": False,
            "guestsCanSeeOtherGuests": False,
            "exclude_organizer": False,
            "extended_properties": {"private": {"mia_booking_key": booking_key}},
        }
        body = self._execute(_CREATE_EVENT_URL, arguments)
        if body is None:
            return None
        return _parse_create_response(body)

    def get_event(
        self,
        *,
        event_id: str,
        calendar_id: str = "primary",
        timezone: str = "Asia/Jerusalem",
    ) -> EventLookupResult:
        clean_event_id = sanitize_event_id(event_id)
        if clean_event_id is None or not _valid_timezone(timezone):
            return EventLookupResult(status=BookingLookupStatus.ERROR)
        body = self._execute(
            _EVENTS_GET_URL,
            {
                "calendar_id": calendar_id,
                "event_id": clean_event_id,
                "time_zone": timezone,
            },
        )
        if body is None:
            return EventLookupResult(status=BookingLookupStatus.ERROR)
        return _parse_get_response(body, expected_event_id=clean_event_id)

    def patch_event(
        self,
        *,
        event_id: str,
        start: datetime,
        end: datetime,
        timezone: str,
        calendar_id: str = "primary",
        allow_nonstandard_duration: bool = False,
    ) -> CalendarBookingEvent | None:
        clean_event_id = sanitize_event_id(event_id)
        start_utc = to_utc_aware(start)
        end_utc = to_utc_aware(end)
        if clean_event_id is None or not _valid_timezone(timezone):
            return None
        if start_utc is None or end_utc is None or start_utc >= end_utc:
            return None
        duration = end_utc - start_utc
        if (
            not allow_nonstandard_duration
            and duration != timedelta(minutes=MEETING_DURATION_MINUTES)
        ) or duration < timedelta(minutes=5) or duration > timedelta(hours=12):
            return None
        try:
            zone = ZoneInfo(timezone)
            local_start = start_utc.astimezone(zone)
            local_end = end_utc.astimezone(zone)
        except ZoneInfoNotFoundError:
            return None
        body = self._execute(
            _PATCH_EVENT_URL,
            {
                "calendar_id": calendar_id,
                "event_id": clean_event_id,
                "start_time": local_start.isoformat(),
                "end_time": local_end.isoformat(),
                "timezone": timezone,
                "send_updates": "none",
            },
        )
        if body is None:
            return None
        return _parse_partial_patch_response(body, expected_event_id=clean_event_id)

    def _execute(self, url: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_GOOGLECALENDAR_VERSION,
            "arguments": arguments,
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if isinstance(body, str):
                body = json.loads(body)
            if not isinstance(body, dict) or body.get("successful") is not True:
                return None
            return body
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            json.JSONDecodeError,
        ):
            return None


def build_calendar_booking_port(settings: Settings) -> CalendarBookingPort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioCalendarBookingPort(api_key=api_key, user_id=user_id)
    return DisabledCalendarBookingPort()


def lookup_tool_outcome(result: BookingLookupResult) -> tuple[str, int]:
    if result.status == BookingLookupStatus.FOUND:
        return "ok", 1
    if result.status == BookingLookupStatus.NOT_FOUND:
        return "empty", 0
    return "error", 0


def verify_tool_outcome(
    result: BookingLookupResult,
    *,
    create_event_id: str | None,
) -> tuple[str, int]:
    """Post-create verification outcome for calendar_booking_verify audit tool."""
    if result.status == BookingLookupStatus.ERROR:
        return "error", 0
    if result.status == BookingLookupStatus.NOT_FOUND:
        return "empty", 0
    if result.event is None:
        return "error", 0
    if create_event_id is not None and result.event.event_id != create_event_id:
        return "error", 0
    return "ok", 1


def event_lookup_tool_outcome(result: EventLookupResult) -> tuple[str, int]:
    if result.status == BookingLookupStatus.FOUND:
        return "ok", 1
    if result.status == BookingLookupStatus.NOT_FOUND:
        return "empty", 0
    return "error", 0


def _valid_booking_key(key: str) -> bool:
    if not key or len(key) > _BOOKING_KEY_MAX:
        return False
    return bool(re.fullmatch(r"mia_[0-9a-f]{64}", key))


def _valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def _unwrap_response_data(body: dict[str, Any]) -> Any:
    data = body.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict):
        nested = data.get("response_data")
        if nested is not None:
            if isinstance(nested, str):
                try:
                    return json.loads(nested)
                except json.JSONDecodeError:
                    return None
            return nested
    return data


def _next_page_token(body: dict[str, Any]) -> str | None:
    data = _unwrap_response_data(body)
    if not isinstance(data, dict):
        return None
    token = data.get("nextPageToken")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _valid_lookup_page(body: dict[str, Any]) -> bool:
    data = _unwrap_response_data(body)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return False
    token = data.get("nextPageToken")
    return token is None or isinstance(token, str)


def _event_booking_key(event: dict[str, Any]) -> str | None:
    props = event.get("extendedProperties") or event.get("extended_properties")
    if not isinstance(props, dict):
        return None
    private = props.get("private")
    if not isinstance(private, dict):
        return None
    raw = private.get("mia_booking_key")
    if not isinstance(raw, str):
        return None
    return raw if _valid_booking_key(raw) else None


def _parse_lookup_match(
    body: dict[str, Any],
    *,
    booking_key: str,
) -> CalendarBookingEvent | None:
    data = _unwrap_response_data(body)
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status == "cancelled":
            continue
        if _event_booking_key(item) != booking_key:
            continue
        event_id = item.get("id")
        if not isinstance(event_id, str):
            continue
        clean_id = sanitize_event_id(event_id)
        if clean_id is None:
            continue
        meet = _extract_meet_link(item)
        try:
            return CalendarBookingEvent(event_id=clean_id, meet_link=meet)
        except ValueError:
            continue
    return None


def _extract_meet_link(payload: dict[str, Any]) -> str:
    for key in ("hangoutLink", "hangout_link"):
        raw = payload.get(key)
        if isinstance(raw, str):
            link = sanitize_meet_link(raw)
            if link:
                return link
    conference = payload.get("conferenceData")
    if isinstance(conference, dict):
        entry_points = conference.get("entryPoints")
        if isinstance(entry_points, list):
            for entry in entry_points:
                if not isinstance(entry, dict):
                    continue
                uri = entry.get("uri")
                if isinstance(uri, str):
                    link = sanitize_meet_link(uri)
                    if link:
                        return link
    return ""


def _parse_create_response(body: dict[str, Any]) -> CalendarBookingEvent | None:
    data = _unwrap_response_data(body)
    if not isinstance(data, dict):
        return None
    event_id = data.get("id") or data.get("event_id")
    if not isinstance(event_id, str):
        return None
    clean_id = sanitize_event_id(event_id)
    if clean_id is None:
        return None
    meet = _extract_meet_link(data)
    try:
        return CalendarBookingEvent(event_id=clean_id, meet_link=meet)
    except ValueError:
        return None


def _event_datetime(payload: dict[str, Any], key: str) -> datetime | None:
    raw = payload.get(key)
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date_time")
    if not isinstance(raw, str):
        fallback = "start_time" if key == "start" else "end_time"
        raw = payload.get(fallback)
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return to_utc_aware(parsed)


def _parse_get_response(
    body: dict[str, Any],
    *,
    expected_event_id: str,
) -> EventLookupResult:
    data = _unwrap_response_data(body)
    if not isinstance(data, dict):
        return EventLookupResult(status=BookingLookupStatus.ERROR)
    if data.get("status") == "cancelled":
        return EventLookupResult(status=BookingLookupStatus.NOT_FOUND)
    raw_event_id = data.get("id") or data.get("event_id")
    if not isinstance(raw_event_id, str):
        return EventLookupResult(status=BookingLookupStatus.ERROR)
    clean_event_id = sanitize_event_id(raw_event_id)
    if clean_event_id != expected_event_id:
        return EventLookupResult(status=BookingLookupStatus.ERROR)
    start = _event_datetime(data, "start")
    end = _event_datetime(data, "end")
    if start is None or end is None or start >= end:
        return EventLookupResult(status=BookingLookupStatus.ERROR)
    try:
        event = CalendarBookingEvent(
            event_id=clean_event_id,
            meet_link=_extract_meet_link(data),
            start=start,
            end=end,
        )
        return EventLookupResult(status=BookingLookupStatus.FOUND, event=event)
    except ValueError:
        return EventLookupResult(status=BookingLookupStatus.ERROR)


def _parse_partial_patch_response(
    body: dict[str, Any],
    *,
    expected_event_id: str,
) -> CalendarBookingEvent | None:
    data = _unwrap_response_data(body)
    if not isinstance(data, dict):
        return None
    raw_event_id = data.get("id") or data.get("event_id")
    if raw_event_id is None:
        return None
    if not isinstance(raw_event_id, str):
        return None
    clean_event_id = sanitize_event_id(raw_event_id)
    if clean_event_id != expected_event_id:
        return None
    start = _event_datetime(data, "start")
    end = _event_datetime(data, "end")
    try:
        return CalendarBookingEvent(
            event_id=clean_event_id,
            meet_link=_extract_meet_link(data),
            start=start,
            end=end,
        )
    except ValueError:
        return None
