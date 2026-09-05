"""Meeting booking orchestration after explicit numbered slot confirmation (ADR-011)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import TYPE_CHECKING

from app.core.config import get_settings
from app.core.errors import PolicyDenied
from app.core.risk import PolicyDecision, RiskAction, RiskLevel, decide
from app.core.write_flags import named_write_may_auto, write_flag_enabled
from app.domain.ai_runs import elapsed_ms
from app.domain.events import Channel, build_meeting_booked_event
from app.domain.followups import cancel_follow_up_for_booked
from app.domain.meetings.availability import slot_is_bookable
from app.domain.meetings.briefs import persist_booked_meeting_brief
from app.domain.meetings.changes import (
    MeetingChangeKind,
    resolve_booked_meeting_change,
)
from app.domain.meetings.copy import (
    BOOKING_CONFIRMED,
    BOOKING_DENIED,
    BOOKING_RETRY,
    CONFLICT_SLOT_TAKEN,
)
from app.domain.meetings.slots import (
    OfferedSlot,
    compute_booking_key,
    is_explicit_slot_selection,
    normalize_scheduled_at_utc,
    offered_slots_from_json,
    parse_slot_selection,
    sanitize_meet_link,
    slot_at_index,
    slot_interval_exactly_available,
    to_utc_aware,
)
from app.domain.meetings.state import (
    MEETING_TYPE_INTRO_CALL,
    STATUS_BOOKED,
    STATUS_CANCELLATION_REQUESTED,
)
from app.domain.owner.notifications import persist_meeting_booked_owner_notify
from app.domain.tools import AdapterHttpError, ToolOutcome
from app.domain.value import ValueKind, persist_business_value
from app.integrations.calendar import CalendarPort, format_slot_time
from app.integrations.calendar_booking import (
    BookingLookupResult,
    BookingLookupStatus,
    CalendarBookingEvent,
    CalendarBookingPort,
    lookup_tool_outcome,
    verify_tool_outcome,
)

if TYPE_CHECKING:
    from app.db.store import LeadStore


class BookingResultKind(StrEnum):
    NOT_HANDLED = "not_handled"
    BOOKED = "booked"
    ALREADY_BOOKED = "already_booked"
    CONFLICT = "conflict"
    RETRY = "retry"
    DENIED = "denied"


@dataclass
class BookingAttemptResult:
    kind: BookingResultKind
    reply: str = ""
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)


def _booked_confirmation_reply(
    *,
    scheduled_at: str,
    timezone: str,
    meet_link: str,
) -> str:
    when = ""
    try:
        normalized = normalize_scheduled_at_utc(scheduled_at)
        if normalized:
            start = datetime.fromisoformat(normalized)
            when = format_slot_time(start, timezone)
    except (ValueError, TypeError):
        when = ""
    lines = [BOOKING_CONFIRMED]
    if when:
        lines.append(f"מועד: {when}.")
    link = sanitize_meet_link(meet_link)
    if link:
        lines.append(f"קישור Meet: {link}")
    return "\n".join(lines)


def _lookup_outcome(result: BookingLookupResult, *, latency_ms: int = 0) -> ToolOutcome:
    status, count = lookup_tool_outcome(result)
    return ToolOutcome(
        tool="calendar_booking_lookup",
        status=status,
        result_count=count,
        latency_ms=latency_ms,
    )


def _verify_outcome(
    result: BookingLookupResult,
    *,
    create_event_id: str | None,
    latency_ms: int = 0,
) -> ToolOutcome:
    status, count = verify_tool_outcome(result, create_event_id=create_event_id)
    return ToolOutcome(
        tool="calendar_booking_verify",
        status=status,
        result_count=count,
        latency_ms=latency_ms,
    )


def _retry_result(
    outcomes: list[ToolOutcome],
    *,
    reply: str = BOOKING_RETRY,
) -> BookingAttemptResult:
    return BookingAttemptResult(
        kind=BookingResultKind.RETRY,
        reply=reply,
        tool_outcomes=outcomes,
    )


def _conflict_result(
    outcomes: list[ToolOutcome],
    *,
    reply: str = "",
) -> BookingAttemptResult:
    return BookingAttemptResult(
        kind=BookingResultKind.CONFLICT,
        reply=reply,
        tool_outcomes=outcomes,
    )


def _persist_booked_from_provider(
    store: LeadStore,
    *,
    lead_id: str,
    channel: Channel,
    provider: str,
    conversation_id: str,
    selected: OfferedSlot,
    event: CalendarBookingEvent,
    timezone: str,
    outcomes: list[ToolOutcome],
    booked_at: datetime,
    kill_switch: bool,
    demo_active: bool,
) -> BookingAttemptResult:
    start_utc = to_utc_aware(selected.start)
    if start_utc is None:
        return _retry_result(outcomes)
    scheduled_at = normalize_scheduled_at_utc(start_utc.isoformat())
    if scheduled_at is None:
        return _retry_result(outcomes)
    booked_at_utc = to_utc_aware(booked_at)
    if booked_at_utc is None:
        return _retry_result(outcomes)
    booked_at_iso = normalize_scheduled_at_utc(booked_at_utc.isoformat())
    if booked_at_iso is None:
        return _retry_result(outcomes)
    meet_link = sanitize_meet_link(event.meet_link)
    if not store.mark_meeting_booked(
        lead_id=lead_id,
        scheduled_at=scheduled_at,
        calendar_event_id=event.event_id,
        meet_link=meet_link,
        booked_at=booked_at_iso,
        meeting_type=MEETING_TYPE_INTRO_CALL,
    ):
        return _retry_result(outcomes)
    cancel_follow_up_for_booked(
        store,
        lead_id=lead_id,
        channel=channel,
        occurred_at=booked_at_utc,
    )
    _persist_meeting_booked_event(
        store,
        provider=provider,
        channel=channel,
        lead_id=lead_id,
        conversation_id=conversation_id,
        scheduled_at=scheduled_at,
    )
    persist_meeting_booked_owner_notify(
        store,
        lead_id=lead_id,
        scheduled_at=scheduled_at,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )
    persist_booked_meeting_brief(
        store,
        lead_id=lead_id,
        scheduled_at=scheduled_at,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )
    reply = _booked_confirmation_reply(
        scheduled_at=scheduled_at,
        timezone=timezone,
        meet_link=meet_link,
    )
    return BookingAttemptResult(
        kind=BookingResultKind.BOOKED, reply=reply, tool_outcomes=outcomes
    )


def attempt_meeting_booking(
    store: LeadStore,
    *,
    lead_id: str,
    channel: Channel,
    provider: str,
    conversation_id: str,
    inbound_provider_event_id: str,
    message: str,
    calendar: CalendarPort,
    booking_port: CalendarBookingPort,
    kill_switch: bool,
    timezone: str,
    next_action: str = "",
    now: datetime | None = None,
    demo_active: bool = False,
) -> BookingAttemptResult:
    """Try explicit slot booking before fetching fresh offers."""
    del next_action, inbound_provider_event_id
    clock = to_utc_aware(now or datetime.now(UTC)) or datetime.now(UTC)

    meeting = store.lock_meeting_for_update(lead_id)
    if meeting is None:
        return BookingAttemptResult(kind=BookingResultKind.NOT_HANDLED)

    if meeting.status == STATUS_BOOKED:
        if is_explicit_slot_selection(message):
            reply = _booked_confirmation_reply(
                scheduled_at=meeting.scheduled_at,
                timezone=timezone,
                meet_link=meeting.meet_link,
            )
            return BookingAttemptResult(kind=BookingResultKind.ALREADY_BOOKED, reply=reply)
        return BookingAttemptResult(kind=BookingResultKind.NOT_HANDLED)

    offered = offered_slots_from_json(meeting.offered_slots_json or "[]")
    selected_index = parse_slot_selection(
        message,
        offered_slots=offered,
        meeting_status=meeting.status,
    )
    if selected_index is None:
        return BookingAttemptResult(kind=BookingResultKind.NOT_HANDLED)

    selected = slot_at_index(offered, selected_index)
    if selected is None:
        return BookingAttemptResult(kind=BookingResultKind.NOT_HANDLED)

    outcomes: list[ToolOutcome] = []

    if kill_switch:
        return BookingAttemptResult(
            kind=BookingResultKind.DENIED,
            reply=BOOKING_DENIED,
            tool_outcomes=[
                ToolOutcome(tool="calendar_create", status="denied", result_count=0)
            ],
        )

    settings = get_settings()
    if not write_flag_enabled(
        settings, "calendar_write"
    ) or not named_write_may_auto(
        enabled=settings.calendar_write,
        risk=RiskLevel.R2_CUSTOMER_MESSAGE,
    ):
        return BookingAttemptResult(
            kind=BookingResultKind.DENIED,
            reply=BOOKING_DENIED,
            tool_outcomes=[
                ToolOutcome(tool="calendar_create", status="denied", result_count=0)
            ],
        )

    try:
        decision = decide(
            RiskAction(
                name="calendar_create",
                risk=RiskLevel.R2_CUSTOMER_MESSAGE,
                in_approved_scope=True,
            ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return BookingAttemptResult(
            kind=BookingResultKind.DENIED,
            reply=BOOKING_DENIED,
            tool_outcomes=[
                ToolOutcome(tool="calendar_create", status="denied", result_count=0)
            ],
        )

    if decision != PolicyDecision.AUTO:
        return BookingAttemptResult(
            kind=BookingResultKind.DENIED,
            reply=BOOKING_DENIED,
            tool_outcomes=[
                ToolOutcome(tool="calendar_create", status="denied", result_count=0)
            ],
        )

    try:
        booking_key = compute_booking_key(
            lead_id=lead_id, start=selected.start, end=selected.end
        )
    except ValueError:
        return _retry_result(outcomes)

    started = perf_counter()
    try:
        lookup = booking_port.find_by_booking_key(booking_key=booking_key)
    except AdapterHttpError as exc:
        lookup_latency = elapsed_ms(started)
        outcomes.append(
            ToolOutcome(
                tool="calendar_booking_lookup",
                status=exc.tool_status(),
                result_count=0,
                latency_ms=lookup_latency,
            )
        )
        return _retry_result(outcomes)
    lookup_latency = elapsed_ms(started)
    outcomes.append(_lookup_outcome(lookup, latency_ms=lookup_latency))
    if lookup.status == BookingLookupStatus.ERROR:
        return _retry_result(outcomes)

    if lookup.status == BookingLookupStatus.FOUND and lookup.event is not None:
        return _persist_booked_from_provider(
            store,
            lead_id=lead_id,
            channel=channel,
            provider=provider,
            conversation_id=conversation_id,
            selected=selected,
            event=lookup.event,
            timezone=timezone,
            outcomes=outcomes,
            booked_at=clock,
            kill_switch=kill_switch,
            demo_active=demo_active,
        )

    if not slot_is_bookable(
        selected.start, selected.end, now=clock, timezone=timezone
    ):
        store.clear_offered_slots(lead_id)
        return _conflict_result(outcomes)

    started = perf_counter()
    try:
        free_slots = calendar.find_free_slots(
            time_min=selected.start,
            time_max=selected.end,
            duration_minutes=30,
            timezone=timezone,
        )
    except AdapterHttpError as exc:
        slots_latency = elapsed_ms(started)
        outcomes.append(
            ToolOutcome(
                tool="calendar_find_free_slots",
                status=exc.tool_status(),
                result_count=0,
                latency_ms=slots_latency,
            )
        )
        return _retry_result(outcomes)
    slots_latency = elapsed_ms(started)
    if not slot_interval_exactly_available(free_slots, selected=selected):
        outcomes.append(
            ToolOutcome(
                tool="calendar_find_free_slots",
                status="empty",
                result_count=0,
                latency_ms=slots_latency,
            )
        )
        store.clear_offered_slots(lead_id)
        return _conflict_result(outcomes)

    outcomes.append(
        ToolOutcome(
            tool="calendar_find_free_slots",
            status="ok",
            result_count=1,
            latency_ms=slots_latency,
        )
    )

    started = perf_counter()
    create_event_id: str | None = None
    try:
        created = booking_port.create_event(
            booking_key=booking_key,
            start=selected.start,
            end=selected.end,
            timezone=timezone,
        )
        create_latency = elapsed_ms(started)
        create_event_id = created.event_id if created is not None else None
        if created is not None:
            outcomes.append(
                ToolOutcome(
                    tool="calendar_create",
                    status="ok",
                    result_count=1,
                    latency_ms=create_latency,
                )
            )
        else:
            outcomes.append(
                ToolOutcome(
                    tool="calendar_create",
                    status="error",
                    result_count=0,
                    latency_ms=create_latency,
                )
            )
    except AdapterHttpError as exc:
        create_latency = elapsed_ms(started)
        outcomes.append(
            ToolOutcome(
                tool="calendar_create",
                status=exc.tool_status(),
                result_count=0,
                latency_ms=create_latency,
            )
        )
    started = perf_counter()
    try:
        verify = booking_port.find_by_booking_key(booking_key=booking_key)
    except AdapterHttpError as exc:
        verify_latency = elapsed_ms(started)
        outcomes.append(
            ToolOutcome(
                tool="calendar_booking_verify",
                status=exc.tool_status(),
                result_count=0,
                latency_ms=verify_latency,
            )
        )
        return _retry_result(outcomes)
    verify_latency = elapsed_ms(started)
    verify_outcome = _verify_outcome(
        verify, create_event_id=create_event_id, latency_ms=verify_latency
    )
    outcomes.append(verify_outcome)
    if verify_outcome.status != "ok" or verify.event is None:
        return _retry_result(outcomes)

    return _persist_booked_from_provider(
        store,
        lead_id=lead_id,
        channel=channel,
        provider=provider,
        conversation_id=conversation_id,
        selected=selected,
        event=verify.event,
        timezone=timezone,
        outcomes=outcomes,
        booked_at=clock,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )


def _persist_meeting_booked_event(
    store: LeadStore,
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    conversation_id: str,
    scheduled_at: str,
) -> None:
    if not store.claim_operation(scope="calendar_create", key=f"{lead_id}:booked"):
        return
    event = build_meeting_booked_event(
        provider=provider,
        channel=channel,
        lead_id=lead_id,
        conversation_id=conversation_id,
        scheduled_at=scheduled_at,
    )
    store.save_canonical_event(provider=provider, event=event)
    persist_business_value(
        store,
        provider=provider,
        channel=channel,
        lead_id=lead_id,
        kind=ValueKind.BOOKED,
        conversation_id=conversation_id,
    )
    store.complete_operation(
        scope="calendar_create",
        key=f"{lead_id}:booked",
        result_json='{"ok": true}',
    )


def resolve_meeting_reply(
    store: LeadStore,
    *,
    lead_id: str,
    channel: Channel,
    provider: str,
    conversation_id: str,
    inbound_provider_event_id: str,
    message: str,
    base_reply: str,
    next_action: str,
    calendar: CalendarPort,
    booking_port: CalendarBookingPort,
    kill_switch: bool,
    timezone: str,
    now: datetime | None = None,
    demo_active: bool = False,
) -> tuple[str, list[ToolOutcome], bool]:
    """Booking attempt then offer fetch. Returns reply, outcomes, meeting_row_changed."""
    from app.integrations.calendar import prepare_meeting_offer

    meeting = store.get_meeting(lead_id)
    if meeting is not None and meeting.status in {
        STATUS_BOOKED,
        STATUS_CANCELLATION_REQUESTED,
    }:
        change = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider=provider,
            channel=channel,
            conversation_id=conversation_id,
            message=message,
            calendar=calendar,
            booking_port=booking_port,
            kill_switch=kill_switch,
            demo_active=demo_active,
            timezone=timezone,
            now=now,
            inbound_id=inbound_provider_event_id,
        )
        if change.kind != MeetingChangeKind.NOT_HANDLED:
            return change.reply, list(change.tool_outcomes), change.changed
        if meeting.status == STATUS_CANCELLATION_REQUESTED:
            return base_reply, [], False
        booking = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=channel,
            provider=provider,
            conversation_id=conversation_id,
            inbound_provider_event_id=inbound_provider_event_id,
            message=message,
            calendar=calendar,
            booking_port=booking_port,
            kill_switch=kill_switch,
            timezone=timezone,
            next_action=next_action,
            now=now,
            demo_active=demo_active,
        )
        if booking.kind == BookingResultKind.ALREADY_BOOKED:
            return booking.reply, list(booking.tool_outcomes), False
        return base_reply, [], False

    booking = attempt_meeting_booking(
        store,
        lead_id=lead_id,
        channel=channel,
        provider=provider,
        conversation_id=conversation_id,
        inbound_provider_event_id=inbound_provider_event_id,
        message=message,
        calendar=calendar,
        booking_port=booking_port,
        kill_switch=kill_switch,
        timezone=timezone,
        next_action=next_action,
        now=now,
        demo_active=demo_active,
    )
    outcomes = list(booking.tool_outcomes)
    if booking.kind in {BookingResultKind.BOOKED, BookingResultKind.ALREADY_BOOKED}:
        return booking.reply, outcomes, True
    if booking.kind == BookingResultKind.RETRY:
        return booking.reply, outcomes, False
    if booking.kind == BookingResultKind.DENIED:
        return booking.reply, outcomes, False
    if booking.kind == BookingResultKind.CONFLICT:
        offer = prepare_meeting_offer(
            reply=base_reply,
            next_action="offer_meeting",
            calendar=calendar,
            kill_switch=kill_switch,
            timezone=timezone,
            now=now,
        )
        if offer.outcome is not None:
            outcomes.append(offer.outcome)
        if offer.slots:
            store.save_offered_slots(
                lead_id=lead_id,
                slots=offer.slots,
                now=now,
                timezone=timezone,
            )
        conflict_prefix = f"{CONFLICT_SLOT_TAKEN}\n\n"
        return conflict_prefix + offer.reply, outcomes, True

    if next_action != "offer_meeting":
        return base_reply, outcomes, False

    offer = prepare_meeting_offer(
        reply=base_reply,
        next_action=next_action,
        calendar=calendar,
        kill_switch=kill_switch,
        timezone=timezone,
        now=now,
    )
    if offer.outcome is not None:
        outcomes.append(offer.outcome)
    if offer.slots:
        store.save_offered_slots(
            lead_id=lead_id,
            slots=offer.slots,
            now=now,
            timezone=timezone,
        )
    return offer.reply, outcomes, bool(offer.slots)
