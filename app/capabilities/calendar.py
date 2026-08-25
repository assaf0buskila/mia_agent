"""Owner calendar.get_schedule — free/busy behind capability/policy, never a Composio slug."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.errors import InvalidArguments
from app.integrations.calendar import (
    DEFAULT_MEETING_MINUTES,
    DEFAULT_SEARCH_DAYS,
    CalendarPort,
    TimeSlot,
)


def _parse_dt(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidArguments("time_min and time_max must be ISO-8601") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _duration_minutes(args: dict[str, Any]) -> int:
    raw = args.get("duration_minutes", DEFAULT_MEETING_MINUTES)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidArguments("duration_minutes must be an integer") from exc
    if value < 15 or value > 180:
        raise InvalidArguments("duration_minutes must be between 15 and 180")
    return value


def calendar_get_schedule(port: CalendarPort, args: dict[str, Any]) -> dict[str, Any]:
    timezone = str(args.get("timezone") or "Asia/Jerusalem").strip() or "Asia/Jerusalem"
    duration = _duration_minutes(args)
    clock = datetime.now(UTC)
    time_min = _parse_dt(args.get("time_min")) or clock
    time_max = _parse_dt(args.get("time_max")) or (clock + timedelta(days=DEFAULT_SEARCH_DAYS))
    if time_max <= time_min:
        raise InvalidArguments("time_max must be after time_min")
    slots: list[TimeSlot] = port.find_free_slots(
        time_min=time_min,
        time_max=time_max,
        duration_minutes=duration,
        timezone=timezone,
    )
    return {
        "timezone": timezone,
        "duration_minutes": duration,
        "count": len(slots),
        "slots": [
            {"start": slot.start.isoformat(), "end": slot.end.isoformat()} for slot in slots
        ],
    }


def calendar_handlers(port: CalendarPort) -> dict[str, Any]:
    return {"calendar.get_schedule": lambda args: calendar_get_schedule(port, args)}
