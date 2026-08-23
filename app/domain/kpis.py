"""Weekly KPI scorecard from canonical events (§19.1 tab 09)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from app.db.store import LeadStore

KPI_EVENT_TYPES = ("lead_created", "meeting_offered", "handoff", "message_in")
OWNER_BRIEF_EVENT_TYPES = (
    "meeting_booked",
    "meeting_cancellation_requested",
)
COUNTABLE_EVENT_TYPES = frozenset((*KPI_EVENT_TYPES, *OWNER_BRIEF_EVENT_TYPES))


class WeeklyKpiSnapshot(BaseModel):
    week_start: str
    leads: int = Field(ge=0)
    meetings_offered: int = Field(ge=0)
    handoffs: int = Field(ge=0)
    messages_in: int = Field(ge=0)
    follow_ups_pending: int = Field(ge=0)


def week_start_on(*, now: datetime, timezone: str) -> str | None:
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError):
        return None
    if now.tzinfo is None:
        local = now.replace(tzinfo=UTC).astimezone(tz)
    else:
        local = now.astimezone(tz)
    monday = (local - timedelta(days=local.isoweekday() - 1)).date()
    return monday.isoformat()


def week_bounds_utc_iso(*, week_start: str, timezone: str) -> tuple[str, str] | None:
    try:
        tz = ZoneInfo(timezone)
        parts = week_start.split("-")
        if len(parts) != 3:
            return None
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        start_local = datetime(year, month, day, 0, 0, 0, tzinfo=tz)
        end_local = start_local + timedelta(days=7)
        return (
            start_local.astimezone(UTC).isoformat(),
            end_local.astimezone(UTC).isoformat(),
        )
    except (ValueError, OSError, KeyError):
        return None


def compute_weekly_kpi(
    store: LeadStore,
    *,
    timezone: str,
    now: datetime | None = None,
) -> WeeklyKpiSnapshot | None:
    instant = now if now is not None else datetime.now(UTC)
    week_start = week_start_on(now=instant, timezone=timezone)
    if week_start is None:
        return None
    bounds = week_bounds_utc_iso(week_start=week_start, timezone=timezone)
    if bounds is None:
        return None
    occurred_from, occurred_to = bounds
    return WeeklyKpiSnapshot(
        week_start=week_start,
        leads=store.count_canonical_events(
            event_type="lead_created",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        ),
        meetings_offered=store.count_canonical_events(
            event_type="meeting_offered",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        ),
        handoffs=store.count_canonical_events(
            event_type="handoff",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        ),
        messages_in=store.count_canonical_events(
            event_type="message_in",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        ),
        follow_ups_pending=store.count_follow_ups(status="pending"),
    )
