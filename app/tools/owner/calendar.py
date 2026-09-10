"""Owner Calendar tools: availability, agenda reads and the approval-gated change request."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.meetings.slots import sanitize_event_id
from app.domain.owner.calendar import (
    apply_owner_calendar,
    format_calendar_agenda,
    resolve_agenda_window,
)
from app.integrations.calendar import build_calendar_agenda_port, build_calendar_port
from app.integrations.calendar_booking import (
    BookingLookupStatus,
    DisabledCalendarBookingPort,
    build_calendar_booking_port,
)
from app.services.owner_actions import propose_owner_action, typed_composio_binding
from app.tools.owner.types import ToolContext, ToolResult, _empty, _house_unavailable


def _calendar_availability(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    calendar = ctx.calendar
    if calendar is None and ctx.settings.composio_ready():
        calendar = build_calendar_port(ctx.settings)
    if calendar is None:
        return _house_unavailable(ctx, "Calendar")
    text, _outcome = apply_owner_calendar(
        "",
        calendar,
        principal=ctx.principal,
        kill_switch=ctx.kill_switch,
        timezone=ctx.timezone(),
        now=ctx.now,
        demo_active=ctx.demo_active,
    )
    return _empty(text, "No free slots found.")


def _calendar_create_meeting(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if ctx.kill_switch:
        return ToolResult(ok=False, error="calendar write denied")
    title = str(args.get("title") or "").strip()
    start = str(args.get("start") or "").strip()
    minutes = str(args.get("minutes") or "30").strip()
    location = str(args.get("location") or "").strip()
    if not title or not start:
        return ToolResult(ok=False, error="title and start are required")
    try:
        duration = int(minutes)
        if duration < 5 or duration > 720:
            raise ValueError("duration must be between 5 and 720 minutes")
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=ZoneInfo(ctx.timezone()))
        end_at = start_at + timedelta(minutes=duration)
        calendar = ctx.calendar or build_calendar_port(ctx.settings)
        slots = calendar.find_free_slots(
            time_min=start_at,
            time_max=end_at,
            duration_minutes=duration,
            timezone=ctx.timezone(),
        )
        free = any(slot.start <= start_at and slot.end >= end_at for slot in slots)
        if not free:
            return ToolResult(ok=False, error="the requested calendar time is not free")
        binding = typed_composio_binding(ctx.settings, "GOOGLECALENDAR", port=calendar)
        proposal = propose_owner_action(
            ctx.store,
            principal=ctx.principal,
            source_ref=ctx.source_ref,
            kind="calendar.create",
            parameters={
                "title": title,
                "start": start_at.isoformat(),
                "end": end_at.isoformat(),
                "timezone": ctx.timezone(),
                "location": location,
            },
            target={
                "free": True,
                "start": start_at.isoformat(),
                "end": end_at.isoformat(),
                "provider_binding": binding,
            },
        )
    except (PermissionError, ValueError, RuntimeError, ZoneInfoNotFoundError) as exc:
        return ToolResult(ok=False, error=f"calendar proposal could not be bound: {exc}")
    return ToolResult(
        ok=True,
        text="Prepared an exact calendar proposal. Nothing was created.",
        approval_id=proposal.approval_id,
    )


def _calendar_reschedule(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Bind a model-resolved event id and its current provider snapshot."""
    if ctx.kill_switch:
        return ToolResult(ok=False, error="calendar write denied")
    event_id = sanitize_event_id(str(args.get("event_id") or "").strip())
    start_raw = str(args.get("start") or "").strip()
    try:
        minutes = int(args.get("minutes") or 30)
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo(ctx.timezone()))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return ToolResult(ok=False, error="event_id, valid start and duration are required")
    if event_id is None or minutes < 5 or minutes > 720:
        return ToolResult(ok=False, error="event_id, valid start and duration are required")
    booking = build_calendar_booking_port(ctx.settings)
    if isinstance(booking, DisabledCalendarBookingPort):
        return _house_unavailable(ctx, "Calendar")
    current = booking.get_event(event_id=event_id, timezone=ctx.timezone())
    if current.status is not BookingLookupStatus.FOUND or current.event is None:
        return ToolResult(ok=False, error="the selected calendar event is unavailable")
    event = current.event
    end = start + timedelta(minutes=minutes)
    calendar = ctx.calendar or build_calendar_port(ctx.settings)
    slots = calendar.find_free_slots(
        time_min=start,
        time_max=end,
        duration_minutes=minutes,
        timezone=ctx.timezone(),
    )
    destination_free = any(slot.start <= start and slot.end >= end for slot in slots)
    if not destination_free:
        return ToolResult(ok=False, error="the requested new calendar time is not free")
    try:
        binding = typed_composio_binding(ctx.settings, "GOOGLECALENDAR", port=booking)
    except RuntimeError as exc:
        return ToolResult(ok=False, error=f"calendar proposal could not be bound: {exc}")
    target = {
        "event_id": event.event_id,
        "start": event.start.isoformat() if event.start else "",
        "end": event.end.isoformat() if event.end else "",
        "destination_free": True,
        "provider_binding": binding,
    }
    try:
        proposal = propose_owner_action(
            ctx.store,
            principal=ctx.principal,
            source_ref=ctx.source_ref,
            kind="calendar.reschedule",
            parameters={
                "event_id": event.event_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timezone": ctx.timezone(),
            },
            target=target,
        )
    except (PermissionError, ValueError) as exc:
        return ToolResult(ok=False, error=f"calendar proposal could not be bound: {exc}")
    return ToolResult(
        ok=True,
        text="Prepared an exact calendar move proposal. Nothing was changed.",
        approval_id=proposal.approval_id,
    )


def _calendar_agenda(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """What is actually on the calendar for one window. Read only: only ever calls
    CalendarAgendaPort.list_events, never create/patch/delete.
    """
    agenda = ctx.calendar_agenda
    if agenda is None and ctx.settings.composio_ready():
        agenda = build_calendar_agenda_port(ctx.settings)
    if agenda is None:
        return _house_unavailable(ctx, "Calendar")
    range_key = str(args.get("range") or "").strip()
    moment = ctx.now or datetime.now(UTC)
    start, end = resolve_agenda_window(range_key, now=moment, timezone=ctx.timezone())
    events = agenda.list_events(start=start, end=end)
    text = format_calendar_agenda(events, range_key=range_key, timezone=ctx.timezone(), now=moment)
    return ToolResult(ok=True, text=text)
