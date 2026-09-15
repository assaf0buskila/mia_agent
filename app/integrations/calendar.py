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
from app.domain.tools import AdapterHttpError, AdapterResponseError, AdapterSchemaError, ToolOutcome

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
        connected_account_id: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._connected_account_id = connected_account_id.strip()
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
        if self._connected_account_id:
            payload["connected_account_id"] = self._connected_account_id
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
            raw_slots = _extract_time_slots(data, time_min=window_min, time_max=window_max)
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

    def approval_connected_account_id(self) -> str:
        return "fake-calendar-account"

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
            return _gaps_from_busy(time_min=time_min, time_max=time_max, busy=busy_intervals)
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
        suffix = f"\n\nזמין:\n{numbered}\nהשיבו {choices} כדי לאשר."
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

    def list_events_strict(
        self, *, start: datetime, end: datetime, limit: int = 20
    ) -> list[CalendarEvent]:
        """Same read as `list_events`, but for safety checks only: fails closed
        (raises `AdapterSchemaError`) when the response was paginated (more
        results exist beyond this page) or contained any item this adapter
        could not parse, instead of silently treating the window as if those
        events did not exist. `list_events` (display) is unaffected and keeps
        truncating/skipping silently.
        """
        ...


class ComposioCalendarAgendaPort:
    """Live Composio adapter over GOOGLECALENDAR_EVENTS_LIST. Read-only: lists events
    in [start, end); never creates, patches, or deletes.

    Raises AdapterHttpError on HTTP/transport failure, AdapterResponseError when
    Composio accepted the call but reported `successful: false`, and
    AdapterSchemaError when the payload does not have the shape this adapter
    understands (so a provider failure is never read back as "no events" --
    a genuinely empty day still returns `[]`). One malformed or partial event
    inside an otherwise well-shaped `items` list is skipped, never raised.
    """

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        connected_account_id: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._connected_account_id = connected_account_id.strip()
        self._client = client

    def _fetch_response(self, *, start: datetime, end: datetime, cap: int) -> dict[str, Any]:
        """Shared HTTP call for list_events / list_events_strict: validates HTTP
        transport and the top-level `successful` envelope, and returns the raw
        body for the caller to parse (loosely for display, strictly for the
        reschedule safety check).
        """
        window_start = _ensure_aware(start)
        window_end = _ensure_aware(end)
        payload: dict[str, Any] = {
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
        if self._connected_account_id:
            payload["connected_account_id"] = self._connected_account_id
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
            if not isinstance(body, dict) or not isinstance(body.get("successful"), bool):
                raise AdapterSchemaError()
            if body["successful"] is False:
                raise AdapterResponseError()
            return body
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            raise AdapterSchemaError() from None

    def list_events(
        self, *, start: datetime, end: datetime, limit: int = 20
    ) -> list[CalendarEvent]:
        cap = _cap_agenda_limit(limit)
        body = self._fetch_response(start=start, end=end, cap=cap)
        return _parse_agenda_events(body, limit=cap)

    def list_events_strict(
        self, *, start: datetime, end: datetime, limit: int = 20
    ) -> list[CalendarEvent]:
        cap = _cap_agenda_limit(limit)
        body = self._fetch_response(start=start, end=end, cap=cap)
        events, skipped, paginated = _parse_agenda_events_detailed(body, limit=cap)
        if skipped or paginated:
            raise AdapterSchemaError()
        return events


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
            if _ensure_aware(event.start) < window_end and _ensure_aware(event.end) > window_start
        ]
        matches.sort(key=lambda event: _ensure_aware(event.start))
        return matches[: _cap_agenda_limit(limit)]

    def list_events_strict(
        self, *, start: datetime, end: datetime, limit: int = 20
    ) -> list[CalendarEvent]:
        """The fake does not simulate pagination or unparseable items (there is
        no raw provider payload to malform), so this is the same read as
        list_events -- tests that need the strict path to fail closed inject a
        double that raises, or drive the real ComposioCalendarAgendaPort.
        """
        return self.list_events(start=start, end=end, limit=limit)


def build_calendar_agenda_port(
    settings: Settings, *, connected_account_id: str = ""
) -> CalendarAgendaPort | None:
    """Mirrors build_calendar_port's credential check; returns None (not a Disabled
    port) so callers can tell "not configured" apart from "configured, came back
    empty" without adding a new settings field.

    `connected_account_id` binds the read to one specific provider connection --
    pass the proposal's approved connection at the approval-time re-check so the
    agenda read cannot silently consult a different (e.g. newer/switched)
    calendar account than the one the owner approved.
    """
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioCalendarAgendaPort(
            api_key=api_key, user_id=user_id, connected_account_id=connected_account_id
        )
    return None


def window_free_excluding_self(
    calendar: CalendarPort,
    *,
    window_start: datetime,
    window_end: datetime,
    self_start: datetime | None,
    self_end: datetime | None,
    self_event_id: str | None = None,
    agenda: CalendarAgendaPort | None = None,
    timezone: str = "Asia/Jerusalem",
) -> bool:
    """True when [window_start, window_end) is free, without counting the
    event being moved as a conflict with itself.

    `find_free_slots` reports merged free/busy: another meeting that happens
    to overlap the moved event's own current span is indistinguishable, in
    that merged view, from the event's own busy block. So this only ever
    "forgives" self-conflict when it is actually safe to:

    - The destination does not overlap the event's current span at all: the
      event cannot possibly be the thing making the destination busy, so a
      plain provider free/busy check on the destination alone is exact.
    - The destination DOES overlap the event's current span: free/busy alone
      cannot tell self-busy from another-event-busy, so this reads the actual
      events in the destination window through `agenda` (CalendarAgendaPort,
      the same read used for "what's on my calendar") via `list_events_strict`,
      drops only the exact event being moved (by `self_event_id`), and refuses
      if any remaining event still overlaps the destination. Any other all-day
      item counts as busy outright, without comparing its (UTC-midnight)
      start/end against the destination window -- a bare `YYYY-MM-DD` means
      the provider's local calendar day, not literal UTC midnight, so that
      comparison cannot be trusted either way; provider `transparency` is not
      parsed. Refuses (fails closed) if `agenda` is unavailable, the read
      fails, the response was paginated, or any returned item could not be
      parsed (see `list_events_strict`).

    Pass self_start/self_end as None for a plain free check (a create has no
    self event to exclude).
    """
    window_start = _ensure_aware(window_start)
    window_end = _ensure_aware(window_end)

    def _plain_free_check(period_start: datetime, period_end: datetime) -> bool:
        duration = max(1, int((period_end - period_start).total_seconds() // 60))
        slots = calendar.find_free_slots(
            time_min=period_start,
            time_max=period_end,
            duration_minutes=duration,
            timezone=timezone,
        )
        return any(slot.start <= period_start and slot.end >= period_end for slot in slots)

    if self_start is None or self_end is None:
        return _plain_free_check(window_start, window_end)

    self_start = _ensure_aware(self_start)
    self_end = _ensure_aware(self_end)
    overlaps_self = window_start < self_end and self_start < window_end
    if not overlaps_self:
        return _plain_free_check(window_start, window_end)

    if agenda is None:
        return False
    try:
        events = agenda.list_events_strict(start=window_start, end=window_end)
    except (AdapterHttpError, AdapterResponseError, AdapterSchemaError):
        return False
    for event in events:
        if self_event_id is not None and event.event_id == self_event_id:
            continue
        if event.all_day:
            # Date-only start/end means the provider's local calendar day, not
            # UTC midnight (see CalendarEvent/_parse_agenda_date); comparing it
            # against the destination window is not reliable in either
            # direction, so any other all-day item in the read is treated as
            # busy outright rather than risk missing a real conflict.
            return False
        event_start = _ensure_aware(event.start)
        event_end = _ensure_aware(event.end)
        if window_start < event_end and event_start < window_end:
            return False
    return True


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
    """Raises AdapterSchemaError when the top-level payload shape is not one this
    adapter understands, so callers never read a malformed response back as a
    genuinely empty agenda. An individual malformed event inside a well-shaped
    `items` list is still just skipped (see `_parse_agenda_event`). Used by
    display (`list_events`); the reschedule safety check uses the detailed,
    stricter variant below instead.
    """
    events, _skipped, _paginated = _parse_agenda_events_detailed(body, limit=limit)
    return events


def _parse_agenda_events_detailed(
    body: dict[str, Any], *, limit: int
) -> tuple[list[CalendarEvent], bool, bool]:
    """Same parse as `_parse_agenda_events`, plus the two facts the reschedule
    safety check needs and display does not: whether any item in an
    otherwise well-shaped `items` list was cancelled-and-thus-skipped is NOT
    counted here as a skip (a cancelled event is legitimately absent, not a
    parse failure); only a genuinely unparseable item sets `skipped`. Google
    may return a short (even empty) page with more results still available,
    so `paginated` reflects a `nextPageToken` on the response regardless of
    whether `items` filled the requested page.
    """
    data = _unwrap_agenda_response_data(body)
    if not isinstance(data, dict):
        raise AdapterSchemaError()
    items = data.get("items")
    if not isinstance(items, list):
        raise AdapterSchemaError()
    events: list[CalendarEvent] = []
    skipped = False
    for item in items:
        if _is_cancelled_agenda_item(item):
            continue
        event = _parse_agenda_event(item)
        if event is None:
            skipped = True
            continue
        events.append(event)
        if len(events) >= limit:
            break
    paginated = bool(str(data.get("nextPageToken") or "").strip())
    return events, skipped, paginated


def _is_cancelled_agenda_item(item: Any) -> bool:
    return isinstance(item, dict) and item.get("status") == "cancelled"


def _parse_agenda_event(item: Any) -> CalendarEvent | None:
    if not isinstance(item, dict):
        return None
    if _is_cancelled_agenda_item(item):
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
