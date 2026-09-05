"""Explicit booked-meeting reschedule and local cancellation-request flows."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import TYPE_CHECKING

from app.core.config import get_settings
from app.core.errors import PolicyDenied
from app.core.risk import PolicyDecision, RiskAction, RiskLevel, assert_allowed, decide
from app.core.write_flags import named_write_may_auto, write_flag_enabled
from app.domain.ai_runs import elapsed_ms
from app.domain.events import (
    Channel,
    build_meeting_cancellation_requested_event,
    build_meeting_rescheduled_event,
)
from app.domain.meetings.availability import slot_is_bookable
from app.domain.meetings.briefs import persist_booked_meeting_brief
from app.domain.meetings.slots import (
    compute_booking_key,
    is_explicit_slot_selection,
    normalize_scheduled_at_utc,
    offered_slots_from_json,
    parse_slot_selection,
    sanitize_event_id,
    sanitize_meet_link,
    slot_at_index,
    slot_interval_exactly_available,
    to_utc_aware,
)
from app.domain.meetings.state import (
    STATUS_BOOKED,
    STATUS_CANCELLATION_REQUESTED,
)
from app.domain.owner.notifications import (
    KIND_MEETING_CANCELLATION_REQUESTED,
    KIND_MEETING_RESCHEDULED,
    persist_owner_notify,
)
from app.domain.tools import AdapterHttpError, ToolOutcome
from app.integrations.calendar import CalendarPort, format_slot_time, prepare_meeting_offer
from app.integrations.calendar_booking import (
    BookingLookupStatus,
    CalendarBookingEvent,
    CalendarBookingPort,
    EventLookupResult,
    event_lookup_tool_outcome,
)

if TYPE_CHECKING:
    from app.db.store import LeadStore

_RESCHEDULE_PHRASES = frozenset(
    {
        "reschedule",
        "reschedule the meeting",
        "change the meeting time",
        "change time",
        "לשנות את המועד",
        "להזיז את הפגישה",
        "אפשר מועד אחר",
        "צריך מועד אחר",
    }
)
_CANCELLATION_PHRASES = frozenset(
    {
        "cancel the meeting",
        "cancel my meeting",
        "cancel meeting",
        "לבטל את הפגישה",
        "תבטל את הפגישה",
        "אני רוצה לבטל את הפגישה",
    }
)

RESCHEDULE_OFFER_INTRO = "אפשר לבחור מועד חדש."
RESCHEDULE_RETRY = "לא הצלחתי לשנות את המועד. נסו שוב בעוד רגע."
RESCHEDULE_CONFLICT = "המועד הזה כבר לא פנוי. בחרו מועד אחר."
RESCHEDULE_DENIED = "לא ניתן לשנות את המועד כרגע. נסו שוב מאוחר יותר."
RESCHEDULE_CONFIRMED = "הפגישה הועברה."
CANCELLATION_REQUESTED_REPLY = "רשמתי בקשת ביטול. אסף יעדכן את היומן."
CANCELLATION_DENIED_REPLY = "לא ניתן לרשום בקשת ביטול כרגע. נסו שוב מאוחר יותר."

CANCELLATION_SCOPE = "calendar_cancellation"


def cancellation_claim_key(inbound_id: str) -> str:
    return f"{inbound_id}:cancellation"


def claim_cancellation_persist(*, store, inbound_id: str) -> bool:
    if not inbound_id:
        return False
    return store.claim_operation(
        scope=CANCELLATION_SCOPE, key=cancellation_claim_key(inbound_id)
    )


def complete_cancellation_persist(*, store, inbound_id: str) -> None:
    if not inbound_id:
        return
    store.complete_operation(
        scope=CANCELLATION_SCOPE,
        key=cancellation_claim_key(inbound_id),
        result_json='{"ok": true}',
    )


class MeetingChangeKind(StrEnum):
    NOT_HANDLED = "not_handled"
    RESCHEDULE_OFFERED = "reschedule_offered"
    RESCHEDULED = "rescheduled"
    CANCELLATION_REQUESTED = "cancellation_requested"
    RETRY = "retry"
    CONFLICT = "conflict"
    DENIED = "denied"


@dataclass
class MeetingChangeResult:
    kind: MeetingChangeKind
    reply: str = ""
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    changed: bool = False


def _strip_boundary_punctuation(value: str) -> str:
    text = value.strip()
    while text and unicodedata.category(text[0]).startswith("P"):
        text = text[1:].lstrip()
    while text and unicodedata.category(text[-1]).startswith("P"):
        text = text[:-1].rstrip()
    return text


def _normalized_command(message: str) -> str:
    return _strip_boundary_punctuation(message).casefold()


def is_explicit_reschedule_request(message: str) -> bool:
    return _normalized_command(message) in _RESCHEDULE_PHRASES


def is_explicit_cancellation_request(message: str) -> bool:
    return _normalized_command(message) in _CANCELLATION_PHRASES


def _get_outcome(tool: str, result: EventLookupResult, *, latency_ms: int = 0) -> ToolOutcome:
    status, count = event_lookup_tool_outcome(result)
    return ToolOutcome(tool=tool, status=status, result_count=count, latency_ms=latency_ms)


def _verified_target(
    result: EventLookupResult,
    *,
    event_id: str,
    start: datetime,
    end: datetime,
) -> CalendarBookingEvent | None:
    if result.status != BookingLookupStatus.FOUND or result.event is None:
        return None
    event = result.event
    target_start = to_utc_aware(start)
    target_end = to_utc_aware(end)
    if target_start is None or target_end is None:
        return None
    if (
        event.event_id != event_id
        or event.start != target_start
        or event.end != target_end
    ):
        return None
    return event


def _rescheduled_reply(
    *,
    scheduled_at: str,
    timezone: str,
    meet_link: str,
) -> str:
    start = datetime.fromisoformat(scheduled_at)
    lines = [RESCHEDULE_CONFIRMED, f"מועד: {format_slot_time(start, timezone)}."]
    link = sanitize_meet_link(meet_link)
    if link:
        lines.append(f"קישור Meet: {link}")
    return "\n".join(lines)


def _persist_rescheduled(
    store: LeadStore,
    *,
    lead_id: str,
    provider: str,
    channel: Channel,
    conversation_id: str,
    event_id: str,
    start: datetime,
    end: datetime,
    timezone: str,
    meet_link: str,
    occurred_at: datetime,
    outcomes: list[ToolOutcome],
    kill_switch: bool,
    demo_active: bool,
) -> MeetingChangeResult:
    start_utc = to_utc_aware(start)
    if start_utc is None:
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
            tool_outcomes=outcomes,
        )
    scheduled_at = normalize_scheduled_at_utc(start_utc.isoformat())
    changed_at = normalize_scheduled_at_utc(occurred_at.isoformat())
    if scheduled_at is None or changed_at is None:
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
            tool_outcomes=outcomes,
        )
    if not store.mark_meeting_rescheduled(
        lead_id=lead_id,
        scheduled_at=scheduled_at,
        calendar_event_id=event_id,
        rescheduled_at=changed_at,
    ):
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
            tool_outcomes=outcomes,
        )
    target_key = compute_booking_key(lead_id=lead_id, start=start, end=end)
    claim_key = f"{lead_id}:rescheduled:{target_key}"
    reply = _rescheduled_reply(
        scheduled_at=scheduled_at,
        timezone=timezone,
        meet_link=meet_link,
    )
    if not store.claim_operation(scope="calendar_reschedule", key=claim_key):
        return MeetingChangeResult(
            kind=MeetingChangeKind.RESCHEDULED,
            reply=reply,
            tool_outcomes=outcomes,
            changed=False,
        )
    store.save_canonical_event(
        provider=provider,
        event=build_meeting_rescheduled_event(
            provider=provider,
            channel=channel,
            lead_id=lead_id,
            conversation_id=conversation_id,
            target_booking_key=target_key,
            scheduled_at=scheduled_at,
            occurred_at=occurred_at,
        ),
    )
    persist_owner_notify(
        store,
        kind=KIND_MEETING_RESCHEDULED,
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
    store.complete_operation(
        scope="calendar_reschedule",
        key=claim_key,
        result_json='{"ok": true}',
    )
    return MeetingChangeResult(
        kind=MeetingChangeKind.RESCHEDULED,
        reply=reply,
        tool_outcomes=outcomes,
        changed=True,
    )


def _request_cancellation(
    store: LeadStore,
    *,
    lead_id: str,
    provider: str,
    channel: Channel,
    conversation_id: str,
    kill_switch: bool,
    demo_active: bool,
    occurred_at: datetime,
    inbound_id: str = "",
) -> MeetingChangeResult:
    meeting = store.lock_meeting_for_update(lead_id)
    if meeting is None:
        return MeetingChangeResult(kind=MeetingChangeKind.NOT_HANDLED)
    if meeting.status == STATUS_CANCELLATION_REQUESTED:
        return MeetingChangeResult(
            kind=MeetingChangeKind.CANCELLATION_REQUESTED,
            reply=CANCELLATION_REQUESTED_REPLY,
        )
    if meeting.status != STATUS_BOOKED:
        return MeetingChangeResult(kind=MeetingChangeKind.NOT_HANDLED)
    persisted = False
    try:
        assert_allowed(
            RiskAction(
                name="calendar_cancellation_request",
                risk=RiskLevel.R1_LOW_WRITE,
            ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return MeetingChangeResult(
            kind=MeetingChangeKind.DENIED,
            reply=CANCELLATION_DENIED_REPLY,
        )
    claimed = False
    if inbound_id:
        if not claim_cancellation_persist(store=store, inbound_id=inbound_id):
            return MeetingChangeResult(
                kind=MeetingChangeKind.RETRY,
                reply=CANCELLATION_DENIED_REPLY,
            )
        claimed = True
    try:
        booked_scheduled_at = meeting.scheduled_at
        requested_at = normalize_scheduled_at_utc(occurred_at.isoformat())
        if requested_at is None or not store.mark_meeting_cancellation_requested(
            lead_id=lead_id,
            requested_at=requested_at,
        ):
            return MeetingChangeResult(
                kind=MeetingChangeKind.RETRY,
                reply=CANCELLATION_DENIED_REPLY,
            )
        store.save_canonical_event(
            provider=provider,
            event=build_meeting_cancellation_requested_event(
                provider=provider,
                channel=channel,
                lead_id=lead_id,
                conversation_id=conversation_id,
                occurred_at=occurred_at,
            ),
        )
        persist_owner_notify(
            store,
            kind=KIND_MEETING_CANCELLATION_REQUESTED,
            lead_id=lead_id,
            scheduled_at=booked_scheduled_at,
            kill_switch=kill_switch,
            demo_active=demo_active,
        )
        persisted = True
        return MeetingChangeResult(
            kind=MeetingChangeKind.CANCELLATION_REQUESTED,
            reply=CANCELLATION_REQUESTED_REPLY,
            changed=True,
        )
    finally:
        if claimed:
            if persisted:
                complete_cancellation_persist(store=store, inbound_id=inbound_id)
            else:
                store.fail_operation(
                    scope=CANCELLATION_SCOPE,
                    key=cancellation_claim_key(inbound_id),
                )


def _offer_reschedule(
    store: LeadStore,
    *,
    lead_id: str,
    calendar: CalendarPort,
    kill_switch: bool,
    timezone: str,
    now: datetime,
) -> MeetingChangeResult:
    if kill_switch:
        return MeetingChangeResult(
            kind=MeetingChangeKind.DENIED,
            reply=RESCHEDULE_DENIED,
            tool_outcomes=[
                ToolOutcome(
                    tool="calendar_find_free_slots",
                    status="denied",
                    result_count=0,
                )
            ],
        )
    offer = prepare_meeting_offer(
        reply=RESCHEDULE_OFFER_INTRO,
        next_action="offer_meeting",
        calendar=calendar,
        kill_switch=kill_switch,
        timezone=timezone,
        now=now,
    )
    outcomes = [offer.outcome] if offer.outcome is not None else []
    changed = False
    if offer.slots:
        changed = store.save_reschedule_slots(
            lead_id=lead_id,
            slots=offer.slots,
            now=now,
            timezone=timezone,
        )
    return MeetingChangeResult(
        kind=MeetingChangeKind.RESCHEDULE_OFFERED,
        reply=offer.reply,
        tool_outcomes=outcomes,
        changed=changed,
    )


def _attempt_reschedule(
    store: LeadStore,
    *,
    lead_id: str,
    provider: str,
    channel: Channel,
    conversation_id: str,
    message: str,
    calendar: CalendarPort,
    booking_port: CalendarBookingPort,
    kill_switch: bool,
    demo_active: bool,
    timezone: str,
    now: datetime,
) -> MeetingChangeResult:
    meeting = store.lock_meeting_for_update(lead_id)
    if meeting is None or meeting.status != STATUS_BOOKED:
        return MeetingChangeResult(kind=MeetingChangeKind.NOT_HANDLED)
    event_id = sanitize_event_id(meeting.calendar_event_id)
    if event_id is None:
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
        )
    offered = offered_slots_from_json(meeting.reschedule_slots_json or "[]")
    selected_index = parse_slot_selection(
        message,
        offered_slots=offered,
        meeting_status="offered",
    )
    if selected_index is None:
        return MeetingChangeResult(kind=MeetingChangeKind.NOT_HANDLED)
    selected = slot_at_index(offered, selected_index)
    if selected is None:
        return MeetingChangeResult(kind=MeetingChangeKind.NOT_HANDLED)
    if kill_switch:
        return MeetingChangeResult(
            kind=MeetingChangeKind.DENIED,
            reply=RESCHEDULE_DENIED,
            tool_outcomes=[
                ToolOutcome(
                    tool="calendar_reschedule_get",
                    status="denied",
                    result_count=0,
                )
            ],
        )
    try:
        decision = decide(
            RiskAction(
                name="calendar_reschedule",
                risk=RiskLevel.R2_CUSTOMER_MESSAGE,
                in_approved_scope=True,
            ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        decision = PolicyDecision.DENY
    if decision != PolicyDecision.AUTO:
        return MeetingChangeResult(
            kind=MeetingChangeKind.DENIED,
            reply=RESCHEDULE_DENIED,
        )

    outcomes: list[ToolOutcome] = []
    started = perf_counter()
    try:
        before = booking_port.get_event(
            event_id=event_id,
            calendar_id="primary",
            timezone=timezone,
        )
    except AdapterHttpError as exc:
        get_latency = elapsed_ms(started)
        outcomes.append(
            ToolOutcome(
                tool="calendar_reschedule_get",
                status=exc.tool_status(),
                result_count=0,
                latency_ms=get_latency,
            )
        )
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
            tool_outcomes=outcomes,
        )
    get_latency = elapsed_ms(started)
    outcomes.append(_get_outcome("calendar_reschedule_get", before, latency_ms=get_latency))
    if before.status != BookingLookupStatus.FOUND or before.event is None:
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
            tool_outcomes=outcomes,
        )
    already_target = _verified_target(
        before,
        event_id=event_id,
        start=selected.start,
        end=selected.end,
    )
    if already_target is not None:
        return _persist_rescheduled(
            store,
            lead_id=lead_id,
            provider=provider,
            channel=channel,
            conversation_id=conversation_id,
            event_id=event_id,
            start=selected.start,
            end=selected.end,
            timezone=timezone,
            meet_link=meeting.meet_link,
            occurred_at=now,
            outcomes=outcomes,
            kill_switch=kill_switch,
            demo_active=demo_active,
        )

    if not slot_is_bookable(
        selected.start,
        selected.end,
        now=now,
        timezone=timezone,
    ):
        store.clear_reschedule_slots(lead_id)
        return MeetingChangeResult(
            kind=MeetingChangeKind.CONFLICT,
            reply=RESCHEDULE_CONFLICT,
            tool_outcomes=outcomes,
            changed=True,
        )
    started = perf_counter()
    try:
        free_slots = calendar.find_free_slots(
            time_min=selected.start,
            time_max=selected.end,
            duration_minutes=30,
            calendar_id="primary",
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
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
            tool_outcomes=outcomes,
        )
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
        store.clear_reschedule_slots(lead_id)
        return MeetingChangeResult(
            kind=MeetingChangeKind.CONFLICT,
            reply=RESCHEDULE_CONFLICT,
            tool_outcomes=outcomes,
            changed=True,
        )
    outcomes.append(
        ToolOutcome(
            tool="calendar_find_free_slots",
            status="ok",
            result_count=1,
            latency_ms=slots_latency,
        )
    )
    settings = get_settings()
    if not write_flag_enabled(
        settings, "calendar_write"
    ) or not named_write_may_auto(
        enabled=settings.calendar_write,
        risk=RiskLevel.R2_CUSTOMER_MESSAGE,
    ):
        return MeetingChangeResult(
            kind=MeetingChangeKind.DENIED,
            reply=RESCHEDULE_DENIED,
            tool_outcomes=[
                *outcomes,
                ToolOutcome(
                    tool="calendar_patch_event",
                    status="denied",
                    result_count=0,
                ),
            ],
        )
    patch_started = perf_counter()
    try:
        patched = booking_port.patch_event(
            event_id=event_id,
            start=selected.start,
            end=selected.end,
            timezone=timezone,
            calendar_id="primary",
        )
        patch_latency = elapsed_ms(patch_started)
        outcomes.append(
            ToolOutcome(
                tool="calendar_patch_event",
                status="ok" if patched is not None else "error",
                result_count=1 if patched is not None else 0,
                latency_ms=patch_latency,
            )
        )
    except AdapterHttpError as exc:
        patch_latency = elapsed_ms(patch_started)
        outcomes.append(
            ToolOutcome(
                tool="calendar_patch_event",
                status=exc.tool_status(),
                result_count=0,
                latency_ms=patch_latency,
            )
        )
    started = perf_counter()
    try:
        verified = booking_port.get_event(
            event_id=event_id,
            calendar_id="primary",
            timezone=timezone,
        )
    except AdapterHttpError as exc:
        verify_latency = elapsed_ms(started)
        outcomes.append(
            ToolOutcome(
                tool="calendar_reschedule_verify",
                status=exc.tool_status(),
                result_count=0,
                latency_ms=verify_latency,
            )
        )
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
            tool_outcomes=outcomes,
        )
    verify_latency = elapsed_ms(started)
    verified_event = _verified_target(
        verified,
        event_id=event_id,
        start=selected.start,
        end=selected.end,
    )
    verify_status = "ok" if verified_event is not None else (
        "empty" if verified.status == BookingLookupStatus.NOT_FOUND else "error"
    )
    outcomes.append(
        ToolOutcome(
            tool="calendar_reschedule_verify",
            status=verify_status,
            result_count=1 if verified_event is not None else 0,
            latency_ms=verify_latency,
        )
    )
    if verified_event is None:
        return MeetingChangeResult(
            kind=MeetingChangeKind.RETRY,
            reply=RESCHEDULE_RETRY,
            tool_outcomes=outcomes,
        )
    return _persist_rescheduled(
        store,
        lead_id=lead_id,
        provider=provider,
        channel=channel,
        conversation_id=conversation_id,
        event_id=event_id,
        start=selected.start,
        end=selected.end,
        timezone=timezone,
        meet_link=meeting.meet_link,
        occurred_at=now,
        outcomes=outcomes,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )


def resolve_booked_meeting_change(
    store: LeadStore,
    *,
    lead_id: str,
    provider: str,
    channel: Channel,
    conversation_id: str,
    message: str,
    calendar: CalendarPort,
    booking_port: CalendarBookingPort,
    kill_switch: bool,
    demo_active: bool = False,
    timezone: str,
    now: datetime | None = None,
    inbound_id: str = "",
) -> MeetingChangeResult:
    """Resolve exact cancellation, reschedule command, or stored-slot selection."""
    clock = to_utc_aware(now or datetime.now(UTC)) or datetime.now(UTC)
    meeting = store.get_meeting(lead_id)
    if meeting is None:
        return MeetingChangeResult(kind=MeetingChangeKind.NOT_HANDLED)
    if is_explicit_cancellation_request(message):
        return _request_cancellation(
            store,
            lead_id=lead_id,
            provider=provider,
            channel=channel,
            conversation_id=conversation_id,
            kill_switch=kill_switch,
            demo_active=demo_active,
            occurred_at=clock,
            inbound_id=inbound_id,
        )
    if meeting.status != STATUS_BOOKED:
        return MeetingChangeResult(kind=MeetingChangeKind.NOT_HANDLED)
    if is_explicit_reschedule_request(message):
        if sanitize_event_id(meeting.calendar_event_id) is None:
            return MeetingChangeResult(
                kind=MeetingChangeKind.RETRY,
                reply=RESCHEDULE_RETRY,
            )
        return _offer_reschedule(
            store,
            lead_id=lead_id,
            calendar=calendar,
            kill_switch=kill_switch,
            timezone=timezone,
            now=clock,
        )
    if (
        meeting.reschedule_slots_json
        and meeting.reschedule_slots_json != "[]"
        and is_explicit_slot_selection(message)
    ):
        return _attempt_reschedule(
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
            now=clock,
        )
    return MeetingChangeResult(kind=MeetingChangeKind.NOT_HANDLED)
