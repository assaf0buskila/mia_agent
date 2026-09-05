"""Deterministic meeting availability policy (ADR-012).

Sunday-Thursday, 09:00-17:00 local, 30-minute slots on :00/:30 boundaries,
minimum 24 hours notice. No env bypass this slice.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from app.integrations.calendar import TimeSlot

MEETING_MINUTES = 30
WORKDAY_WEEKDAYS = frozenset({6, 0, 1, 2, 3})
BUSINESS_OPEN = time(9, 0)
BUSINESS_CLOSE = time(17, 0)
MIN_NOTICE = timedelta(hours=24)
SLOT_DURATION = timedelta(minutes=MEETING_MINUTES)
MAX_POLICY_SLOTS = 3


def _local_zone(timezone: str) -> ZoneInfo:
    return ZoneInfo(timezone)


def _ensure_utc(value: datetime) -> datetime | None:
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def is_workday_local(local_dt: datetime) -> bool:
    return local_dt.weekday() in WORKDAY_WEEKDAYS


def is_within_business_hours_local(local_start: datetime, local_end: datetime) -> bool:
    if local_start.date() != local_end.date():
        return False
    start_t = local_start.time()
    end_t = local_end.time()
    return start_t >= BUSINESS_OPEN and end_t <= BUSINESS_CLOSE


def meets_notice_requirement(*, start_utc: datetime, now_utc: datetime) -> bool:
    return start_utc >= now_utc + MIN_NOTICE


def _next_half_hour_boundary(local_dt: datetime) -> datetime:
    dt = local_dt.replace(second=0, microsecond=0)
    minute = dt.minute
    if minute in {0, 30}:
        return dt
    if minute < 30:
        return dt.replace(minute=30)
    return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def slot_is_bookable(
    start: datetime,
    end: datetime,
    *,
    now: datetime,
    timezone: str,
) -> bool:
    """True when slot satisfies workday, hours, duration, and 24h notice."""
    start_utc = _ensure_utc(start)
    end_utc = _ensure_utc(end)
    now_utc = _ensure_utc(now)
    if start_utc is None or end_utc is None or now_utc is None:
        return False
    if end_utc - start_utc != SLOT_DURATION:
        return False
    if not meets_notice_requirement(start_utc=start_utc, now_utc=now_utc):
        return False
    try:
        local_start = start_utc.astimezone(_local_zone(timezone))
        local_end = end_utc.astimezone(_local_zone(timezone))
    except ZoneInfoNotFoundError:
        return False
    if not is_workday_local(local_start):
        return False
    return is_within_business_hours_local(local_start, local_end)


def carve_policy_slots(
    gaps: list[TimeSlot],
    *,
    timezone: str,
    now: datetime,
    max_slots: int = MAX_POLICY_SLOTS,
) -> list[TimeSlot]:
    """Carve provider free gaps into exact 30m policy-valid slots aligned to :00/:30 local."""
    from app.integrations.calendar import TimeSlot

    now_utc = _ensure_utc(now) or datetime.now(UTC)
    try:
        tz = _local_zone(timezone)
    except ZoneInfoNotFoundError:
        return []

    slots: list[TimeSlot] = []
    for gap in gaps:
        gap_start = _ensure_utc(gap.start)
        gap_end = _ensure_utc(gap.end)
        if gap_start is None or gap_end is None:
            continue
        local_start = gap_start.astimezone(tz)
        local_end = gap_end.astimezone(tz)
        cursor = _next_half_hour_boundary(local_start)
        while cursor + SLOT_DURATION <= local_end and len(slots) < max_slots:
            local_slot_end = cursor + SLOT_DURATION
            if (
                is_workday_local(cursor)
                and is_within_business_hours_local(cursor, local_slot_end)
            ):
                utc_start = cursor.astimezone(UTC)
                utc_end = local_slot_end.astimezone(UTC)
                if slot_is_bookable(
                    utc_start, utc_end, now=now_utc, timezone=timezone
                ):
                    slots.append(TimeSlot(start=utc_start, end=utc_end))
            cursor += SLOT_DURATION
        if len(slots) >= max_slots:
            break
    return slots
