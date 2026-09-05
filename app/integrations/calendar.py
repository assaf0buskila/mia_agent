"""Google Calendar free/busy read port, plus a read-only event agenda listing.

Production adapter: Composio `GOOGLECALENDAR` toolkit version `20260812_00`,
pin `GOOGLECALENDAR_FIND_FREE_SLOTS` only when `MIA_COMPOSIO_API_KEY` and
`MIA_COMPOSIO_USER_ID` are set. Never create/update/delete events this slice.

`CalendarAgendaPort` (added for the owner "what's on my calendar" read) reuses the
same `GOOGLECALENDAR_EVENTS_LIST` pin that `app.integrations.calendar_booking`
already uses for booking lookups — same toolkit version, same tool slug, no new
Composio surface (ADR-007 / ADR-015: pin production tool schemas, no drift). The
slug is re-declared locally rather than imported from `calendar_booking` because
that module imports `COMPOSIO_GOOGLECALENDAR_VERSION` from here; importing back
would be circular.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.ai_runs import elapsed_ms
from app.domain.meetings.availability import carve_policy_slots
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.sales import NextAction
from app.domain.tools import AdapterHttpError, ToolOutcome

COMPOSIO_GOOGLECALENDAR_VERSION = "20260812_00"
COMPOSIO_FIND_FREE_SLOTS_TOOL = "GOOGLECALENDAR_FIND_FREE_SLOTS"
_COMPOSIO_EXECUTE_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_FIND_FREE_SLOTS_TOOL}"
)

# Same Composio slug as calendar_booking.COMPOSIO_EVENTS_LIST_TOOL (see module
# docstring for why it is re-declared here instead of imported).
COMPOSIO_AGENDA_EVENTS_LIST_TOOL = "GOOGLECALENDAR_EVENTS_LIST"
_AGENDA_EVENTS_LIST_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_AGENDA_EVENTS_LIST_TOOL}"
)

DEFAULT_MEETING_MINUTES = 30
DEFAULT_SEARCH_DAYS = 7
_FREE_LIST_KEYS = ("free_slots", "freeSlots", "free")
_MAX_AGENDA_EVENTS = 20


class TimeSlot(BaseModel):
    start: datetime
    end: datetime


class CalendarPort(Protocol):
    def find_free_slots(
        self,
        *,
        time_min: datetime,
        time_max: datetime,
        duration_minutes: int = DEFAULT_MEETING_MINUTES,
        calendar_id: str = "primary",
        timezone: str = "Asia/Jerusalem",
    ) -> list[TimeSlot]: ...


class DisabledCalendarPort:
    def find_free_slots(
        self,
        *,
        time_min: datetime,
        time_max: datetime,
        duration_minutes: int = DEFAULT_MEETING_MINUTES,
        calendar_id: str = "primary",
        timezone: str = "Asia/Jerusalem",
    ) -> list[TimeSlot]:
        return []


class ComposioCalendarPort:
    """Live Composio execute adapter for FIND_FREE_SLOTS. Raises AdapterHttpError on HTTP."""

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

    def find_free_slots(
        self,
        *,
        time_min: datetime,
        time_max: datetime,
        duration_minutes: int = DEFAULT_MEETING_MINUTES,
        calendar_id: str = "primary",
        timezone: str = "Asia/Jerusalem",
    ) -> list[TimeSlot]:
        window_min = _ensure_aware(time_min)
        window_max = _ensure_aware(time_max)
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_GOOGLECALENDAR_VERSION,
            "arguments": {
                "items": [calendar_id],
                "time_min": window_min.isoformat(),
                "time_max": window_max.isoformat(),
                "timezone": timezone,
            },
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _COMPOSIO_EXECUTE_URL,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        _COMPOSIO_EXECUTE_URL,
                        json=payload,
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("successful") is not True:
                return []
            data = body.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    return []
            raw_slots = _extract_time_slots(
                data, time_min=window_min, time_max=window_max
            )
            return [
                slot
                for slot in raw_slots
                if _slot_fits_window(
                    slot,
                    time_min=window_min,
                    time_max=window_max,
                    duration_minutes=duration_minutes,
                )
            ]
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return []


class FakeCalendarPort:
    """Test double. Filters configured slots in our code (Composio gaps are unfiltered)."""

    def __init__(self, slots: list[TimeSlot] | None = None) -> None:
        self._slots = slots or []

    def find_free_slots(
        self,
        *,
        time_min: datetime,
        time_max: datetime,
        duration_minutes: int = DEFAULT_MEETING_MINUTES,
        calendar_id: str = "primary",
        timezone: str = "Asia/Jerusalem",
    ) -> list[TimeSlot]:
        del calendar_id, timezone
        return [
            slot
            for slot in self._slots
            if _slot_fits_window(
                slot,
                time_min=time_min,
                time_max=time_max,
                duration_minutes=duration_minutes,
            )
        ]


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        normalized = value.replace("Z", "+00:00")
        return _ensure_aware(datetime.fromisoformat(normalized))
    except (ValueError, TypeError):
        return None


def _extract_time_slots(
    data: Any,
    *,
    time_min: datetime,
    time_max: datetime,
) -> list[TimeSlot]:
    if not isinstance(data, dict):
        return []

    free_list_slots = _extract_free_list_slots(data)
    if free_list_slots:
        return free_list_slots

    calendars = data.get("calendars")
    if isinstance(calendars, dict) and calendars:
        busy_intervals = _collect_busy_intervals(calendars)
        if busy_intervals is not None:
            return _gaps_from_busy(
                time_min=time_min, time_max=time_max, busy=busy_intervals
            )
    return []


def _extract_free_list_slots(data: dict[str, Any]) -> list[TimeSlot]:
    for key in _FREE_LIST_KEYS:
        value = data.get(key)
        if not isinstance(value, list):
            continue
        slots: list[TimeSlot] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            start_raw = item.get("start")
            end_raw = item.get("end")
            if not isinstance(start_raw, str) or not isinstance(end_raw, str):
                continue
            start = _parse_iso_datetime(start_raw)
            end = _parse_iso_datetime(end_raw)
            if start is None or end is None:
                continue
            slots.append(TimeSlot(start=start, end=end))
        if slots:
            return slots
    return []


def _collect_busy_intervals(
    calendars: dict[str, Any],
) -> list[tuple[datetime, datetime]] | None:
    """Return busy intervals, or None if any calendar reported errors (do not treat as free)."""
    intervals: list[tuple[datetime, datetime]] = []
    for calendar_data in calendars.values():
        if not isinstance(calendar_data, dict):
            return None
        errors = calendar_data.get("errors")
        if isinstance(errors, list) and errors:
            return None
        busy = calendar_data.get("busy")
        if not isinstance(busy, list):
            continue
        for item in busy:
            if not isinstance(item, dict):
                continue
            start_raw = item.get("start")
            end_raw = item.get("end")
            if not isinstance(start_raw, str) or not isinstance(end_raw, str):
                continue
            start = _parse_iso_datetime(start_raw)
            end = _parse_iso_datetime(end_raw)
            if start is None or end is None:
                continue
            intervals.append((start, end))
    return intervals


def _merge_busy_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda item: item[0])
    merged = [sorted_intervals[0]]
    for start, end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _gaps_from_busy(
    *,
    time_min: datetime,
    time_max: datetime,
    busy: list[tuple[datetime, datetime]],
) -> list[TimeSlot]:
    merged = _merge_busy_intervals(busy)
    gaps: list[TimeSlot] = []
    cursor = time_min
    for busy_start, busy_end in merged:
        clipped_start = max(busy_start, time_min)
        clipped_end = min(busy_end, time_max)
        if clipped_end <= time_min or clipped_start >= time_max:
            continue
        if clipped_start > cursor:
            gaps.append(TimeSlot(start=cursor, end=clipped_start))
        cursor = max(cursor, clipped_end)
    if cursor < time_max:
        gaps.append(TimeSlot(start=cursor, end=time_max))
    return gaps


def _slot_fits_window(
    slot: TimeSlot,
    *,
    time_min: datetime,
    time_max: datetime,
    duration_minutes: int,
) -> bool:
    start = _ensure_aware(slot.start)
    end = _ensure_aware(slot.end)
    window_min = _ensure_aware(time_min)
    window_max = _ensure_aware(time_max)
    if start < window_min or end > window_max:
        return False
    return (end - start) >= timedelta(minutes=duration_minutes)


def format_slot_time(start: datetime, timezone: str) -> str:
    local = _ensure_aware(start).astimezone(ZoneInfo(timezone))
    return local.strftime("%a %d %b %H:%M")


class MeetingOfferResult(BaseModel):
    reply: str
    outcome: ToolOutcome | None = None
    slots: list[TimeSlot] = Field(default_factory=list)


def calendar_availability_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "calendar_availability",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="calendar_find_free_slots",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def prepare_meeting_offer(
    *,
    reply: str,
    next_action: str,
    calendar: CalendarPort,
    kill_switch: bool,
    timezone: str = "Asia/Jerusalem",
    now: datetime | None = None,
    duration_minutes: int = DEFAULT_MEETING_MINUTES,
    max_slots: int = 3,
) -> MeetingOfferResult:
    """Fetch slots and format numbered Hebrew options. Never creates events."""
    if next_action != NextAction.OFFER_MEETING.value:
        return MeetingOfferResult(reply=reply, outcome=None, slots=[])

    try:
        assert_allowed(
            RiskAction(name="calendar_read", risk=RiskLevel.R0_READ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return MeetingOfferResult(
            reply=reply,
            outcome=ToolOutcome(
                tool="calendar_find_free_slots",
                status="denied",
                result_count=0,
                freshness="",
            ),
            slots=[],
        )

    clock = _ensure_aware(now or datetime.now(UTC))
    time_min = clock
    time_max = clock + timedelta(days=DEFAULT_SEARCH_DAYS)

    try:
        started = perf_counter()
        slots = calendar.find_free_slots(
            time_min=time_min,
            time_max=time_max,
            duration_minutes=duration_minutes,
            timezone=timezone,
        )
        latency = elapsed_ms(started)
        if not slots:
            return MeetingOfferResult(
                reply=reply,
                outcome=calendar_availability_outcome(
                    base_status="empty",
                    present=False,
                    result_count=0,
                    latency_ms=latency,
                    now=clock,
                ),
                slots=[],
            )
        included = carve_policy_slots(
            slots,
            timezone=timezone,
            now=clock,
            max_slots=max_slots,
        )
        if not included:
            return MeetingOfferResult(
                reply=reply,
                outcome=calendar_availability_outcome(
                    base_status="empty",
                    present=False,
                    result_count=0,
                    latency_ms=latency,
                    now=clock,
                ),
                slots=[],
            )
        lines = [
            f"{index}. {format_slot_time(slot.start, timezone)}"
            for index, slot in enumerate(included, start=1)
        ]
        numbered = "\n".join(lines)
        choices = ", ".join(str(index) for index in range(1, len(included) + 1))
        suffix = (
            f"\n\nזמין:\n{numbered}\n"
            f"השיבו {choices} כדי לאשר."
        )
        return MeetingOfferResult(
            reply=f"{reply}{suffix}",
            outcome=calendar_availability_outcome(
                base_status="ok",
                present=True,
                result_count=len(included),
                latency_ms=latency,
                now=clock,
            ),
            slots=included,
        )
    except AdapterHttpError as exc:
        return MeetingOfferResult(
            reply=reply,
            outcome=calendar_availability_outcome(
                base_status=exc.tool_status(),
                present=False,
                result_count=0,
                latency_ms=elapsed_ms(started),
                now=clock,
            ),
            slots=[],
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError, ZoneInfoNotFoundError):
        return MeetingOfferResult(
            reply=reply,
            outcome=calendar_availability_outcome(
                base_status="error",
                present=False,
                result_count=0,
                latency_ms=elapsed_ms(started),
                now=clock,
            ),
            slots=[],
        )


def enrich_meeting_offer(
    *,
    reply: str,
    next_action: str,
    calendar: CalendarPort,
    kill_switch: bool,
    timezone: str = "Asia/Jerusalem",
    now: datetime | None = None,
    duration_minutes: int = DEFAULT_MEETING_MINUTES,
    max_slots: int = 3,
) -> tuple[str, ToolOutcome | None]:
    """Append calendar slots to OFFER_MEETING copy. Never raises; never creates events."""
    result = prepare_meeting_offer(
        reply=reply,
        next_action=next_action,
        calendar=calendar,
        kill_switch=kill_switch,
        timezone=timezone,
        now=now,
        duration_minutes=duration_minutes,
        max_slots=max_slots,
    )
    return result.reply, result.outcome


def build_calendar_port(settings: Settings) -> CalendarPort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioCalendarPort(api_key=api_key, user_id=user_id)
    return DisabledCalendarPort()


# --- Read-only agenda listing (owner "what's on my calendar" reads) -----------------
#
# Separate from CalendarPort above: FIND_FREE_SLOTS answers "when am I free", this
# answers "what do I have" — the owner cannot ask "מה יש לי מחר?" through a
# free-slots-only port. Additive only; CalendarPort/ComposioCalendarPort/
# FakeCalendarPort/build_calendar_port above are untouched.


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    attendees: tuple[str, ...] = ()


class CalendarAgendaPort(Protocol):
    def list_events(
        self, *, start: datetime, end: datetime, limit: int = 20
    ) -> list[CalendarEvent]: ...


class ComposioCalendarAgendaPort:
    """Live Composio adapter over GOOGLECALENDAR_EVENTS_LIST. Read-only: lists events
    in [start, end); never creates, patches, or deletes. Raises AdapterHttpError on
    HTTP/transport failure; a malformed or partial event in the payload is skipped,
    never raised.
    """

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

    def list_events(
        self, *, start: datetime, end: datetime, limit: int = 20
    ) -> list[CalendarEvent]:
        window_start = _ensure_aware(start)
        window_end = _ensure_aware(end)
        cap = _cap_agenda_limit(limit)
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_GOOGLECALENDAR_VERSION,
            "arguments": {
                "calendarId": "primary",
                "timeMin": window_start.isoformat(),
                "timeMax": window_end.isoformat(),
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": cap,
                "showDeleted": False,
            },
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _AGENDA_EVENTS_LIST_URL,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        _AGENDA_EVENTS_LIST_URL,
                        json=payload,
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("successful") is not True:
                return []
            return _parse_agenda_events(body, limit=cap)
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return []


class FakeCalendarAgendaPort:
    """Test double for read-only agenda listing. Filters/sorts configured events
    in our code (real Composio responses are already ordered by startTime).
    """

    def __init__(self, events: list[CalendarEvent] | None = None) -> None:
        self._events = list(events or [])

    def list_events(
        self, *, start: datetime, end: datetime, limit: int = 20
    ) -> list[CalendarEvent]:
        window_start = _ensure_aware(start)
        window_end = _ensure_aware(end)
        matches = [
            event
            for event in self._events
            if _ensure_aware(event.start) < window_end
            and _ensure_aware(event.end) > window_start
        ]
        matches.sort(key=lambda event: _ensure_aware(event.start))
        return matches[: _cap_agenda_limit(limit)]


def build_calendar_agenda_port(settings: Settings) -> CalendarAgendaPort | None:
    """Mirrors build_calendar_port's credential check; returns None (not a Disabled
    port) so callers can tell "not configured" apart from "configured, came back
    empty" without adding a new settings field.
    """
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioCalendarAgendaPort(api_key=api_key, user_id=user_id)
    return None


def _cap_agenda_limit(limit: int) -> int:
    return max(1, min(int(limit or _MAX_AGENDA_EVENTS), _MAX_AGENDA_EVENTS))


def _unwrap_agenda_response_data(body: dict[str, Any]) -> Any:
    """Same envelope-unwrapping as calendar_booking._unwrap_response_data for this
    same tool (GOOGLECALENDAR_EVENTS_LIST): `data` is sometimes a JSON string, and
    sometimes wraps the real payload one level deeper under `response_data`.
    Duplicated locally to avoid the circular import described in the module
    docstring.
    """
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


def _parse_agenda_events(body: dict[str, Any], *, limit: int) -> list[CalendarEvent]:
    data = _unwrap_agenda_response_data(body)
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    events: list[CalendarEvent] = []
    for item in items:
        event = _parse_agenda_event(item)
        if event is None:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return events


def _parse_agenda_event(item: Any) -> CalendarEvent | None:
    if not isinstance(item, dict):
        return None
    if item.get("status") == "cancelled":
        return None
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None

    start_raw = item.get("start")
    end_raw = item.get("end")
    if not isinstance(start_raw, dict) or not isinstance(end_raw, dict):
        return None

    all_day = "dateTime" not in start_raw and "date" in start_raw
    try:
        if all_day:
            start = _parse_agenda_date(start_raw.get("date"))
            end = _parse_agenda_date(end_raw.get("date"))
        else:
            start = _parse_agenda_datetime(start_raw.get("dateTime"))
            end = _parse_agenda_datetime(end_raw.get("dateTime"))
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    if start is None or end is None:
        return None

    summary_raw = item.get("summary")
    summary = summary_raw.strip() if isinstance(summary_raw, str) else ""
    location_raw = item.get("location")
    location = location_raw.strip() if isinstance(location_raw, str) else ""

    return CalendarEvent(
        event_id=raw_id.strip(),
        summary=summary,
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        attendees=_parse_agenda_attendees(item.get("attendees")),
    )


def _parse_agenda_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _ensure_aware(parsed)


def _parse_agenda_date(raw: object) -> datetime | None:
    """All-day events carry a bare `YYYY-MM-DD`; represent it as UTC midnight."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        day = datetime.strptime(raw.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    return day.replace(tzinfo=UTC)


def _parse_agenda_attendees(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    emails: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if isinstance(email, str) and email.strip():
            emails.append(email.strip())
    return tuple(emails)
