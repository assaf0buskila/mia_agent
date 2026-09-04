"""Owner Calendar tools: availability, agenda reads and the approval-gated change request."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.domain.calendar_write_gate import ASK_ASSAF
from app.domain.events import Channel
from app.domain.owner_calendar import (
    apply_owner_calendar,
    format_calendar_agenda,
    resolve_agenda_window,
)
from app.domain.owner_calendar_writes import apply_owner_calendar_change_request
from app.integrations.calendar import build_calendar_agenda_port, build_calendar_port
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
    title = str(args.get("title") or "").strip()
    start = str(args.get("start") or "").strip()
    minutes = str(args.get("minutes") or "30").strip()
    location = str(args.get("location") or "").strip()
    if not title or not start:
        return ToolResult(ok=False, error="title and start are required")
    line = f"צור אירוע: {title} {location} | {start} | {minutes} | {ctx.timezone()}"
    reply = apply_owner_calendar_change_request(
        ctx.store,
        text=line,
        channel=Channel.TELEGRAM,
        kill_switch=ctx.kill_switch,
        demo_active=ctx.demo_active,
        default_timezone=ctx.timezone(),
    )
    return ToolResult(ok=True, text=reply or ASK_ASSAF)


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
