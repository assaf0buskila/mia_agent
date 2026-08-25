"""Owner WhatsApp calendar availability read (ADR-012). Read-only; never creates events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import perf_counter
from zoneinfo import ZoneInfoNotFoundError

from app.capabilities.calendar import calendar_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import GraphName
from app.core.errors import PermissionDenied, PolicyDenied
from app.domain.ai_runs import elapsed_ms
from app.domain.meeting_availability import carve_policy_slots
from app.domain.tools import AdapterHttpError, ToolOutcome
from app.integrations.calendar import (
    DEFAULT_MEETING_MINUTES,
    DEFAULT_SEARCH_DAYS,
    CalendarPort,
    TimeSlot,
    calendar_availability_outcome,
    format_slot_time,
)

_EMPTY_ACK = "אין מועדים פנויים בחלון הקרוב לפי שעות העבודה.\nלא יוצרת פגישה."


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def apply_owner_calendar(
    ack: str,
    calendar: CalendarPort,
    *,
    kill_switch: bool,
    timezone: str,
    now: datetime | None = None,
    demo_active: bool = False,
) -> tuple[str, ToolOutcome | None]:
    if demo_active:
        return ack, None

    clock = _ensure_aware(now or datetime.now(UTC))
    time_min = clock
    time_max = clock + timedelta(days=DEFAULT_SEARCH_DAYS)

    try:
        started = perf_counter()
        payload = execute_capability(
            "calendar.get_schedule",
            graph=GraphName.OWNER,
            args={
                "time_min": time_min.isoformat(),
                "time_max": time_max.isoformat(),
                "duration_minutes": DEFAULT_MEETING_MINUTES,
                "timezone": timezone,
            },
            handlers=calendar_handlers(calendar),
            kill_switch=kill_switch,
        )
        latency = elapsed_ms(started)
        slots = [
            TimeSlot(
                start=datetime.fromisoformat(str(item["start"])),
                end=datetime.fromisoformat(str(item["end"])),
            )
            for item in payload.get("slots") or []
            if isinstance(item, dict) and item.get("start") and item.get("end")
        ]
        if not slots:
            return _EMPTY_ACK, calendar_availability_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=clock,
            )
        included = carve_policy_slots(
            slots,
            timezone=timezone,
            now=clock,
            max_slots=3,
        )
        if not included:
            return _EMPTY_ACK, calendar_availability_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=clock,
            )
        lines = [
            f"{index}. {format_slot_time(slot.start, timezone)}"
            for index, slot in enumerate(included, start=1)
        ]
        numbered = "\n".join(lines)
        enriched = f"מועדים פנויים:\n{numbered}\nלא יוצרת פגישה."
        return enriched, calendar_availability_outcome(
            base_status="ok",
            present=True,
            result_count=len(included),
            latency_ms=latency,
            now=clock,
        )
    except PermissionDenied:
        return ack, ToolOutcome(
            tool="calendar_find_free_slots",
            status="denied",
            result_count=0,
            freshness="",
        )
    except AdapterHttpError as exc:
        return ack, calendar_availability_outcome(
            base_status=exc.tool_status(),
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=clock,
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError, ZoneInfoNotFoundError):
        return ack, calendar_availability_outcome(
            base_status="error",
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=clock,
        )
