"""Google Calendar free/busy read port.

Production adapter: Composio `GOOGLECALENDAR` toolkit version `20260812_00`,
pin `GOOGLECALENDAR_FIND_FREE_SLOTS` only when `MIA_COMPOSIO_API_KEY` and
`MIA_COMPOSIO_USER_ID` are set. Never create/update/delete events this slice.
"""

from __future__ import annotations

import json
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
from app.domain.meeting_availability import carve_policy_slots
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.sales import NextAction
from app.domain.tools import AdapterHttpError, ToolOutcome

COMPOSIO_GOOGLECALENDAR_VERSION = "20260812_00"
COMPOSIO_FIND_FREE_SLOTS_TOOL = "GOOGLECALENDAR_FIND_FREE_SLOTS"
_COMPOSIO_EXECUTE_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_FIND_FREE_SLOTS_TOOL}"
)

DEFAULT_MEETING_MINUTES = 30
DEFAULT_SEARCH_DAYS = 7
_FREE_LIST_KEYS = ("free_slots", "freeSlots", "free")


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
