"""Final Mile Gate 2: explicit reschedule, cancellation request, and follow-up stop."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.api.deps import get_calendar_booking_port, get_calendar_port, get_sheets_port
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.models import CanonicalEventRow, IdempotencyRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.calendar_booking import resolve_meeting_reply
from app.domain.events import Channel, EventType
from app.domain.followups import (
    REASON_MEETING_BOOKED,
    REASON_MEETING_OFFERED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    evaluate_follow_up_send,
)
from app.domain.meeting_changes import (
    CANCELLATION_DENIED_REPLY,
    CANCELLATION_REQUESTED_REPLY,
    RESCHEDULE_CONFIRMED,
    RESCHEDULE_CONFLICT,
    RESCHEDULE_DENIED,
    RESCHEDULE_OFFER_INTRO,
    RESCHEDULE_RETRY,
    MeetingChangeKind,
    cancellation_claim_key,
    claim_cancellation_persist,
    complete_cancellation_persist,
    is_explicit_cancellation_request,
    is_explicit_reschedule_request,
    resolve_booked_meeting_change,
)
from app.domain.meeting_slots import compute_booking_key
from app.domain.meetings import (
    STATUS_BOOKED,
    STATUS_CANCELLATION_REQUESTED,
    apply_meeting_policy,
)
from app.domain.sales import FitLevel, SalesState
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import FakeCalendarPort, TimeSlot
from app.integrations.calendar_booking import (
    COMPOSIO_EVENTS_GET_TOOL,
    COMPOSIO_GOOGLECALENDAR_VERSION,
    COMPOSIO_PATCH_EVENT_TOOL,
    BookingLookupStatus,
    CalendarBookingEvent,
    ComposioCalendarBookingPort,
    EventLookupResult,
    FakeCalendarBookingPort,
)
from app.integrations.sheets import FakeSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

IL = ZoneInfo("Asia/Jerusalem")
FIXED_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)


def _slot(days_ahead: int, hour: int) -> TimeSlot:
    local_now = FIXED_NOW.astimezone(IL)
    start = (local_now + timedelta(days=days_ahead)).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    start_utc = start.astimezone(UTC)
    return TimeSlot(start=start_utc, end=start_utc + timedelta(minutes=30))


def _seed_booked(
    store: LeadStore,
    *,
    external_id: str,
    channel: Channel = Channel.GMAIL,
    with_follow_up: bool = False,
) -> str:
    _, lead_id = store.open_channel_lead(channel=channel, external_id=external_id)
    apply_meeting_policy(
        store,
        lead_id=lead_id,
        channel=channel,
        action="offer_meeting",
        kill_switch=False,
    )
    original = _slot(4, 9)
    assert store.mark_meeting_booked(
        lead_id=lead_id,
        scheduled_at=original.start.isoformat(),
        calendar_event_id=f"evt_{external_id.replace('@', '_')}",
        meet_link="https://meet.google.com/abc-defg-hij",
        booked_at=FIXED_NOW.isoformat(),
    )
    if with_follow_up:
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=channel.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_PENDING,
            due_at="2026-08-20",
        )
    return lead_id


def _provider_event(event_id: str, slot: TimeSlot) -> CalendarBookingEvent:
    return CalendarBookingEvent(
        event_id=event_id,
        meet_link="https://meet.google.com/abc-defg-hij",
        start=slot.start,
        end=slot.end,
    )


@pytest.mark.parametrize(
    "message",
    [
        "reschedule",
        "Reschedule.",
        "reschedule the meeting!",
        "change the meeting time",
        "change time?",
        "לשנות את המועד",
        "להזיז את הפגישה.",
        "אפשר מועד אחר?",
        "צריך מועד אחר",
    ],
)
def test_exact_reschedule_parser_accepts(message: str) -> None:
    assert is_explicit_reschedule_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "please reschedule",
        "can we reschedule the meeting",
        "reschedule tomorrow",
        "change",
        "אולי אפשר מועד אחר מחר",
        "אני צריך מועד אחר",
        "",
    ],
)
def test_exact_reschedule_parser_rejects_embedded_or_vague(message: str) -> None:
    assert not is_explicit_reschedule_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "cancel the meeting",
        "Cancel my meeting.",
        "cancel meeting!",
        "לבטל את הפגישה",
        "תבטל את הפגישה.",
        "אני רוצה לבטל את הפגישה",
    ],
)
def test_exact_cancellation_parser_accepts(message: str) -> None:
    assert is_explicit_cancellation_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "please cancel my meeting",
        "cancel",
        "maybe cancel the meeting tomorrow",
        "אני אולי רוצה לבטל את הפגישה",
        "",
    ],
)
def test_exact_cancellation_parser_rejects_embedded_or_vague(message: str) -> None:
    assert not is_explicit_cancellation_request(message)


def test_booked_reschedule_command_offers_and_stores_separate_slots() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.offer@example.com")
        target = _slot(4, 11)
        port = FakeCalendarBookingPort()
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-gate2-offer",
            message="reschedule",
            calendar=FakeCalendarPort([target]),
            booking_port=port,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result.kind == MeetingChangeKind.RESCHEDULE_OFFERED
        assert "1." in result.reply
        assert row.offered_slots_json == "[]"
        assert row.reschedule_slots_json != "[]"
        assert port.get_calls == []
        assert port.patch_calls == []
    finally:
        db.close()


def test_booked_unrelated_text_makes_no_calendar_calls() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.unrelated@example.com")
        calendar = FakeCalendarPort([_slot(4, 11)])
        booking = FakeCalendarBookingPort()
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-gate2-unrelated",
            message="thanks",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == MeetingChangeKind.NOT_HANDLED
        assert booking.get_calls == []
        assert booking.patch_calls == []
    finally:
        db.close()


def test_reschedule_kill_switch_blocks_all_provider_calls() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.reschedule.kill@example.com")
        booking = FakeCalendarBookingPort()
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-reschedule-kill",
            message="reschedule",
            calendar=FakeCalendarPort([_slot(4, 11)]),
            booking_port=booking,
            kill_switch=True,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result.kind == MeetingChangeKind.DENIED
        assert row.reschedule_slots_json == "[]"
        assert booking.get_calls == []
        assert booking.patch_calls == []
    finally:
        db.close()


def _prepare_reschedule(
    store: LeadStore,
    *,
    external_id: str,
    target: TimeSlot,
) -> tuple[str, str]:
    lead_id = _seed_booked(store, external_id=external_id)
    assert store.save_reschedule_slots(
        lead_id=lead_id,
        slots=[target],
        now=FIXED_NOW,
        timezone="Asia/Jerusalem",
    )
    meeting = store.get_meeting(lead_id)
    assert meeting is not None
    return lead_id, meeting.calendar_event_id


@pytest.mark.parametrize("state", ["error", "not_found"])
def test_reschedule_get_uncertainty_blocks_patch(state: str) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id=f"gate2.get.{state}@example.com",
            target=target,
        )
        kwargs = (
            {"get_errors": {event_id}}
            if state == "error"
            else {"get_not_found": {event_id}}
        )
        booking = FakeCalendarBookingPort(**kwargs)
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id=f"thread-get-{state}",
            message="1",
            calendar=FakeCalendarPort([target]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == MeetingChangeKind.RETRY
        assert booking.patch_calls == []
        assert result.tool_outcomes[0].tool == "calendar_reschedule_get"
        expected_status = "error" if state == "error" else "empty"
        assert result.tool_outcomes[0].status == expected_status
    finally:
        db.close()


def test_provider_already_at_target_recovers_locally_without_patch() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.recover@example.com",
            target=target,
        )
        before = store.get_meeting(lead_id)
        assert before is not None
        booked_at = before.booked_at
        meeting_type = before.meeting_type
        meet_link = before.meet_link
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, target)}
        )
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-recover",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result.kind == MeetingChangeKind.RESCHEDULED
        assert booking.patch_calls == []
        assert row.status == STATUS_BOOKED
        assert row.calendar_event_id == event_id
        assert row.meet_link == meet_link
        assert row.booked_at == booked_at
        assert row.meeting_type == meeting_type
        assert row.rescheduled_at == FIXED_NOW.isoformat()
        assert row.reschedule_slots_json == "[]"
    finally:
        db.close()


def test_reschedule_conflict_blocks_patch_and_clears_slots() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        old = _slot(4, 9)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.conflict@example.com",
            target=target,
        )
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, old)}
        )
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-conflict",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result.kind == MeetingChangeKind.CONFLICT
        assert booking.patch_calls == []
        assert row.reschedule_slots_json == "[]"
    finally:
        db.close()


def test_patch_timeout_then_verified_target_succeeds() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        old = _slot(4, 9)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.timeout@example.com",
            target=target,
        )
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, old)},
            patch_returns_none=True,
        )
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-timeout",
            message="1",
            calendar=FakeCalendarPort([target]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == MeetingChangeKind.RESCHEDULED
        patch = [item for item in result.tool_outcomes if item.tool == "calendar_patch_event"]
        verify = [
            item
            for item in result.tool_outcomes
            if item.tool == "calendar_reschedule_verify"
        ]
        assert patch[0].status == "error"
        assert verify[0].status == "ok"
        assert len(booking.get_calls) == 2
    finally:
        db.close()


def test_verify_mismatch_fails_without_local_update() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        old = _slot(4, 9)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.mismatch@example.com",
            target=target,
        )
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, old)},
            patch_updates_event=False,
        )
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-mismatch",
            message="1",
            calendar=FakeCalendarPort([target]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result.kind == MeetingChangeKind.RETRY
        assert row.scheduled_at == old.start.isoformat()
        assert row.rescheduled_at == ""
        verify = [
            item
            for item in result.tool_outcomes
            if item.tool == "calendar_reschedule_verify"
        ]
        assert verify[0].status == "error"
    finally:
        db.close()


def test_reschedule_get_adapter_http_error_returns_retry() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.get401@example.com",
            target=target,
        )

        class Get401Port(FakeCalendarBookingPort):
            def get_event(
                self,
                *,
                event_id: str,
                calendar_id: str = "primary",
                timezone: str = "Asia/Jerusalem",
            ) -> EventLookupResult:
                del event_id, calendar_id, timezone
                raise AdapterHttpError(401)

        booking = Get401Port()
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-get401",
            message="1",
            calendar=FakeCalendarPort([target]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == MeetingChangeKind.RETRY
        assert result.reply == RESCHEDULE_RETRY
        get_outcomes = [
            o for o in result.tool_outcomes if o.tool == "calendar_reschedule_get"
        ]
        assert get_outcomes[0].status == "unauthorized"
        assert booking.patch_calls == []
    finally:
        db.close()


def test_patch_adapter_http_error_verify_recovery_reschedules() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        old = _slot(4, 9)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.patch503@example.com",
            target=target,
        )

        class Patch503VerifyRecoverPort(FakeCalendarBookingPort):
            def __init__(self) -> None:
                super().__init__(events_by_id={event_id: _provider_event(event_id, old)})

            def patch_event(
                self,
                *,
                event_id: str,
                start: datetime,
                end: datetime,
                timezone: str,
                calendar_id: str = "primary",
            ) -> CalendarBookingEvent | None:
                del event_id, start, end, timezone, calendar_id
                raise AdapterHttpError(503)

            def get_event(
                self,
                *,
                event_id: str,
                calendar_id: str = "primary",
                timezone: str = "Asia/Jerusalem",
            ) -> EventLookupResult:
                self.get_calls.append(
                    {
                        "event_id": event_id,
                        "calendar_id": calendar_id,
                        "timezone": timezone,
                    }
                )
                if len(self.get_calls) == 1:
                    return super().get_event(
                        event_id=event_id,
                        calendar_id=calendar_id,
                        timezone=timezone,
                    )
                return EventLookupResult(
                    status=BookingLookupStatus.FOUND,
                    event=_provider_event(event_id, target),
                )

        booking = Patch503VerifyRecoverPort()
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-patch503",
            message="1",
            calendar=FakeCalendarPort([target]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == MeetingChangeKind.RESCHEDULED
        patch = [o for o in result.tool_outcomes if o.tool == "calendar_patch_event"]
        verify = [
            o for o in result.tool_outcomes if o.tool == "calendar_reschedule_verify"
        ]
        assert patch[0].status == "retryable"
        assert verify[0].status == "ok"
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.scheduled_at == target.start.isoformat()
    finally:
        db.close()


def test_repeat_reschedule_selection_is_idempotent() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.repeat@example.com",
            target=target,
        )
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, target)}
        )
        first = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-repeat",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        second = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-repeat",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert first.kind == MeetingChangeKind.RESCHEDULED
        assert second.kind == MeetingChangeKind.NOT_HANDLED
        assert booking.patch_calls == []
        rows = list(
            store.session.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type
                    == EventType.MEETING_RESCHEDULED.value,
                )
            )
        )
        assert len(rows) == 1
    finally:
        db.close()


def test_reschedule_claim_first_persist_completes_idempotency() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.claim.first@example.com",
            target=target,
        )
        target_key = compute_booking_key(
            lead_id=lead_id, start=target.start, end=target.end
        )
        claim_key = f"{lead_id}:rescheduled:{target_key}"
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, target)}
        )
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-claim-first",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == MeetingChangeKind.RESCHEDULED
        assert result.changed is True
        idem_row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == "calendar_reschedule",
                IdempotencyRow.key == claim_key,
            )
        ).one()
        assert idem_row.status == "completed"
    finally:
        db.close()


def test_duplicate_reschedule_same_target_one_canonical() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.claim.dup@example.com",
            target=target,
        )
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, target)}
        )
        kwargs = dict(
            store=store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-claim-dup",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        first = resolve_booked_meeting_change(**kwargs)
        assert store.save_reschedule_slots(
            lead_id=lead_id,
            slots=[target],
            now=FIXED_NOW,
            timezone="Asia/Jerusalem",
        )
        second = resolve_booked_meeting_change(**kwargs)
        assert first.kind == MeetingChangeKind.RESCHEDULED
        assert first.changed is True
        assert second.kind == MeetingChangeKind.RESCHEDULED
        assert second.changed is False
        assert RESCHEDULE_CONFIRMED in second.reply
        rows = list(
            store.session.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type
                    == EventType.MEETING_RESCHEDULED.value,
                )
            )
        )
        assert len(rows) == 1
    finally:
        db.close()


def test_second_reschedule_different_target_claims_again() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        first_target = _slot(4, 11)
        second_target = _slot(5, 14)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="gate2.claim.second@example.com",
            target=first_target,
        )
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, first_target)}
        )
        first = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-claim-second",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert first.changed is True
        booking.events_by_id[event_id] = _provider_event(event_id, second_target)
        assert store.save_reschedule_slots(
            lead_id=lead_id,
            slots=[second_target],
            now=FIXED_NOW,
            timezone="Asia/Jerusalem",
        )
        second = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-claim-second",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert second.kind == MeetingChangeKind.RESCHEDULED
        assert second.changed is True
        rows = list(
            store.session.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type
                    == EventType.MEETING_RESCHEDULED.value,
                )
            )
        )
        assert len(rows) == 2
    finally:
        db.close()


def test_composio_get_patch_exact_tools_version_and_args() -> None:
    captured: list[tuple[str, dict[str, object]]] = []
    old = _slot(4, 9)
    target = _slot(4, 11)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append((request.url.path.rsplit("/", 1)[-1], body))
        tool = request.url.path.rsplit("/", 1)[-1]
        if tool == COMPOSIO_EVENTS_GET_TOOL:
            return httpx.Response(
                200,
                json={
                    "successful": True,
                    "data": {
                        "response_data": json.dumps(
                            {
                                "id": "evt_gate2_exact",
                                "status": "confirmed",
                                "start": {"dateTime": old.start.isoformat()},
                                "end": {"dateTime": old.end.isoformat()},
                                "hangoutLink": "https://meet.google.com/abc-defg-hij",
                            }
                        )
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "successful": True,
                "data": {"response_data": {"id": "evt_gate2_exact"}},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = ComposioCalendarBookingPort(api_key="cmp", user_id="user-1", client=client)
    found = port.get_event(
        event_id="evt_gate2_exact",
        calendar_id="primary",
        timezone="Asia/Jerusalem",
    )
    patched = port.patch_event(
        event_id="evt_gate2_exact",
        start=target.start,
        end=target.end,
        timezone="Asia/Jerusalem",
        calendar_id="primary",
    )
    assert found.status == BookingLookupStatus.FOUND
    assert found.event is not None and found.event.start == old.start
    assert patched is not None and patched.event_id == "evt_gate2_exact"
    assert [item[0] for item in captured] == [
        COMPOSIO_EVENTS_GET_TOOL,
        COMPOSIO_PATCH_EVENT_TOOL,
    ]
    get_body = captured[0][1]
    patch_body = captured[1][1]
    assert get_body["version"] == COMPOSIO_GOOGLECALENDAR_VERSION
    assert patch_body["version"] == COMPOSIO_GOOGLECALENDAR_VERSION
    assert get_body["arguments"] == {
        "calendar_id": "primary",
        "event_id": "evt_gate2_exact",
        "time_zone": "Asia/Jerusalem",
    }
    assert patch_body["arguments"] == {
        "calendar_id": "primary",
        "event_id": "evt_gate2_exact",
        "start_time": target.start.astimezone(IL).isoformat(),
        "end_time": target.end.astimezone(IL).isoformat(),
        "timezone": "Asia/Jerusalem",
        "send_updates": "none",
    }
    serialized = json.dumps(patch_body)
    for forbidden in (
        "attendees",
        "summary",
        "description",
        "conference",
        "extended_properties",
        "lead",
    ):
        assert forbidden not in serialized.lower()
    assert "GOOGLECALENDAR_UPDATE_EVENT" not in serialized


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "id": "evt_response",
                "status": "cancelled",
                "start": {"dateTime": _slot(4, 9).start.isoformat()},
                "end": {"dateTime": _slot(4, 9).end.isoformat()},
            },
            BookingLookupStatus.NOT_FOUND,
        ),
        (
            {"id": "evt_response", "status": "confirmed"},
            BookingLookupStatus.ERROR,
        ),
        (
            {
                "id": "evt_other",
                "status": "confirmed",
                "start": {"dateTime": _slot(4, 9).start.isoformat()},
                "end": {"dateTime": _slot(4, 9).end.isoformat()},
            },
            BookingLookupStatus.ERROR,
        ),
    ],
)
def test_composio_get_response_validation(
    payload: dict[str, object],
    expected: BookingLookupStatus,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "successful": True,
                "data": json.dumps({"response_data": payload}),
            },
        )

    port = ComposioCalendarBookingPort(
        api_key="cmp",
        user_id="user-1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = port.get_event(event_id="evt_response", timezone="Asia/Jerusalem")
    assert result.status == expected


def test_composio_get_accepts_top_level_json_string() -> None:
    slot = _slot(4, 9)
    encoded = json.dumps(
        {
            "successful": True,
            "data": {
                "response_data": {
                    "id": "evt_top_string",
                    "status": "confirmed",
                    "start": {"dateTime": slot.start.isoformat()},
                    "end": {"dateTime": slot.end.isoformat()},
                }
            },
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=encoded)

    port = ComposioCalendarBookingPort(
        api_key="cmp",
        user_id="user-1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = port.get_event(event_id="evt_top_string", timezone="Asia/Jerusalem")
    assert result.status == BookingLookupStatus.FOUND


def test_cancellation_claim_key_format() -> None:
    assert cancellation_claim_key("evt.cancel.1") == "evt.cancel.1:cancellation"


def test_claim_cancellation_persist_empty_inbound_id_returns_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert claim_cancellation_persist(store=store, inbound_id="") is False
    finally:
        db.close()


def test_claim_cancellation_persist_first_true_complete_second_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        inbound_id = "evt.cancel.claim.1"
        assert claim_cancellation_persist(store=store, inbound_id=inbound_id) is True
        complete_cancellation_persist(store=store, inbound_id=inbound_id)
        assert claim_cancellation_persist(store=store, inbound_id=inbound_id) is False
    finally:
        db.close()


def test_cancellation_same_inbound_writes_once() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.cancel.once@example.com")
        booking = FakeCalendarBookingPort()
        inbound_id = "evt.cancel.once.1"
        kwargs = dict(
            store=store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-cancel-once",
            message="cancel my meeting",
            calendar=FakeCalendarPort([_slot(4, 11)]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            inbound_id=inbound_id,
        )
        first = resolve_booked_meeting_change(**kwargs, now=FIXED_NOW)
        second = resolve_booked_meeting_change(
            **kwargs, now=FIXED_NOW + timedelta(minutes=1)
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert first.reply == CANCELLATION_REQUESTED_REPLY
        assert second.reply == CANCELLATION_REQUESTED_REPLY
        assert row.status == STATUS_CANCELLATION_REQUESTED
        assert row.cancellation_requested_at == FIXED_NOW.isoformat()
        assert booking.get_calls == []
        event = store.get_canonical_event(
            provider="gmail",
            provider_event_id=f"{lead_id}:cancellation_requested",
        )
        assert event is not None
    finally:
        db.close()


def test_cancellation_failed_persist_is_reclaimable_once(monkeypatch) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.cancel.reclaim@example.com")
        inbound_id = "evt.cancel.reclaim.1"
        original = store.mark_meeting_cancellation_requested
        calls = 0

        def fail_once(*, lead_id: str, requested_at: str) -> bool:
            nonlocal calls
            calls += 1
            return False if calls == 1 else original(lead_id=lead_id, requested_at=requested_at)

        monkeypatch.setattr(store, "mark_meeting_cancellation_requested", fail_once)
        kwargs = dict(
            store=store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-cancel-reclaim",
            message="cancel my meeting",
            calendar=FakeCalendarPort([]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=False,
            timezone="Asia/Jerusalem",
            inbound_id=inbound_id,
        )
        first = resolve_booked_meeting_change(**kwargs, now=FIXED_NOW)
        second = resolve_booked_meeting_change(**kwargs, now=FIXED_NOW + timedelta(minutes=1))
        assert first.kind == MeetingChangeKind.RETRY
        assert second.kind == MeetingChangeKind.CANCELLATION_REQUESTED
        assert calls == 2
    finally:
        db.close()


def test_cancellation_duplicate_inbound_claim_skips_write_while_still_booked() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.cancel.skip@example.com")
        inbound_id = "evt.cancel.skip.1"
        assert claim_cancellation_persist(store=store, inbound_id=inbound_id) is True
        complete_cancellation_persist(store=store, inbound_id=inbound_id)
        booking = FakeCalendarBookingPort()
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-cancel-skip",
            message="cancel my meeting",
            calendar=FakeCalendarPort([_slot(4, 11)]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
            inbound_id=inbound_id,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result.kind == MeetingChangeKind.RETRY
        assert result.reply == CANCELLATION_DENIED_REPLY
        assert row.status == STATUS_BOOKED
        assert row.cancellation_requested_at == ""
        event = store.get_canonical_event(
            provider="gmail",
            provider_event_id=f"{lead_id}:cancellation_requested",
        )
        assert event is None
        assert booking.get_calls == []
    finally:
        db.close()


def test_cancellation_empty_inbound_id_still_persists() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.cancel.empty@example.com")
        booking = FakeCalendarBookingPort()
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-cancel-empty",
            message="cancel my meeting",
            calendar=FakeCalendarPort([_slot(4, 11)]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result.kind == MeetingChangeKind.CANCELLATION_REQUESTED
        assert result.reply == CANCELLATION_REQUESTED_REPLY
        assert row.status == STATUS_CANCELLATION_REQUESTED
        assert row.cancellation_requested_at == FIXED_NOW.isoformat()
        event = store.get_canonical_event(
            provider="gmail",
            provider_event_id=f"{lead_id}:cancellation_requested",
        )
        assert event is not None
        assert booking.get_calls == []
    finally:
        db.close()


def test_cancellation_request_local_only_idempotent_and_redacted() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.cancel@example.com")
        booking = FakeCalendarBookingPort()
        first = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-cancel",
            message="cancel my meeting",
            calendar=FakeCalendarPort([_slot(4, 11)]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        second = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-cancel",
            message="cancel my meeting",
            calendar=FakeCalendarPort([_slot(4, 11)]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW + timedelta(minutes=1),
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert first.reply == CANCELLATION_REQUESTED_REPLY
        assert second.reply == CANCELLATION_REQUESTED_REPLY
        assert row.status == STATUS_CANCELLATION_REQUESTED
        assert row.calendar_event_id
        assert row.scheduled_at
        assert row.meet_link
        assert row.cancellation_requested_at == FIXED_NOW.isoformat()
        assert booking.get_calls == []
        assert booking.patch_calls == []
        event = store.get_canonical_event(
            provider="gmail",
            provider_event_id=f"{lead_id}:cancellation_requested",
        )
        assert event is not None
        assert json.loads(event.payload_json) == {"status": "cancellation_requested"}
        assert "meet" not in event.payload_json
        assert "event" not in event.payload_json
    finally:
        db.close()


def test_cancellation_kill_switch_skips_local_write() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id="gate2.cancel.kill@example.com")
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-cancel-kill",
            message="cancel meeting",
            calendar=FakeCalendarPort([]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=True,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result.kind == MeetingChangeKind.DENIED
        assert row.status == STATUS_BOOKED
        assert row.cancellation_requested_at == ""
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_reschedule_audits_redacted_and_sends_one_reply(monkeypatch) -> None:
    from tests.conftest import freeze_mia_clock

    freeze_mia_clock(monkeypatch, FIXED_NOW)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        external_id = "gate2.inbound@example.com"
        lead_id = _seed_booked(store, external_id=external_id)
        target = _slot(4, 11)
        meeting = store.get_meeting(lead_id)
        assert meeting is not None
        event_id = meeting.calendar_event_id
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, _slot(4, 9))}
        )
        calendar = FakeCalendarPort([target])
        message_port = RecordingMessagePort()
        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[
                {
                    "id": "evt.gate2.offer",
                    "from": external_id,
                    "text": "reschedule",
                }
            ],
            store=store,
            port=message_port,
            kill_switch=False,
            calendar=calendar,
            calendar_booking=booking,
            sheets=FakeSheetsPort(),
        )
        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[
                {
                    "id": "evt.gate2.select",
                    "from": external_id,
                    "text": "1",
                }
            ],
            store=store,
            port=message_port,
            kill_switch=False,
            calendar=calendar,
            calendar_booking=booking,
            sheets=FakeSheetsPort(),
        )
        assert len(message_port.sent) == 2
        assert RESCHEDULE_CONFIRMED in message_port.sent[-1].text
        for tool in (
            "calendar_reschedule_get",
            "calendar_find_free_slots",
            "calendar_patch_event",
            "calendar_reschedule_verify",
        ):
            audit = store.get_canonical_event(
                provider="gmail",
                provider_event_id=f"evt.gate2.select:tool:{tool}",
            )
            assert audit is not None
            payload = json.loads(audit.payload_json)
            assert set(payload) == {"tool", "status", "result_count"}
            serialized = audit.payload_json.lower()
            assert "meet.google" not in serialized
            assert event_id.lower() not in serialized
            assert external_id.lower() not in serialized
        rescheduled_rows = [
            row
            for row in store.session.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type
                    == EventType.MEETING_RESCHEDULED.value,
                )
            )
        ]
        assert len(rescheduled_rows) == 1
        payload = json.loads(rescheduled_rows[0].payload_json)
        assert payload == {"status": "booked", "scheduled_at": payload["scheduled_at"]}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_cancellation_is_local_only_and_sends_one_honest_reply() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        external_id = "gate2.inbound.cancel@example.com"
        lead_id = _seed_booked(store, external_id=external_id)
        booking = FakeCalendarBookingPort()
        message_port = RecordingMessagePort()
        sheets = FakeSheetsPort()
        result = await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[
                {
                    "id": "evt.gate2.cancel.inbound",
                    "from": external_id,
                    "text": "cancel the meeting",
                }
            ],
            store=store,
            port=message_port,
            kill_switch=False,
            calendar=FakeCalendarPort([_slot(4, 11)]),
            calendar_booking=booking,
            sheets=sheets,
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert result["processed"] == 1
        assert len(message_port.sent) == 1
        assert message_port.sent[0].text == CANCELLATION_REQUESTED_REPLY
        assert row.status == STATUS_CANCELLATION_REQUESTED
        assert booking.get_calls == []
        assert booking.patch_calls == []
        assert sheets.meeting_rows[lead_id].status == STATUS_CANCELLATION_REQUESTED
    finally:
        db.close()


def test_website_e2e_reschedule_then_cancellation_request(monkeypatch) -> None:
    from tests.conftest import freeze_mia_clock

    freeze_mia_clock(monkeypatch, FIXED_NOW)
    init_db()
    db = get_session_factory()()
    session_id = "web_gate2_reschedule_cancel"
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(
            store,
            external_id=session_id,
            channel=Channel.WEBSITE,
        )
        target = _slot(4, 11)
        meeting = store.get_meeting(lead_id)
        assert meeting is not None
        event_id = meeting.calendar_event_id
        calendar = FakeCalendarPort([target])
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, _slot(4, 9))}
        )
        sheets = FakeSheetsPort()
        app.dependency_overrides[get_calendar_port] = lambda: calendar
        app.dependency_overrides[get_calendar_booking_port] = lambda: booking
        app.dependency_overrides[get_sheets_port] = lambda: sheets
        try:
            with TestClient(app) as client:
                offer = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "לשנות את המועד"},
                )
                selected = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "1"},
                )
                cancelled = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "לבטל את הפגישה"},
                )
            assert offer.status_code == 200
            assert offer.json()["next_action"] in {
                "ask_need",
                "ask_contact",
                "handoff",
                "answer",
                "confirm_contact",
            }
            assert selected.status_code == 200
            assert cancelled.status_code == 200
            assert cancelled.json()["next_action"] in {
                "ask_need",
                "ask_contact",
                "handoff",
                "answer",
                "confirm_contact",
            }
        finally:
            app.dependency_overrides.pop(get_calendar_port, None)
            app.dependency_overrides.pop(get_calendar_booking_port, None)
            app.dependency_overrides.pop(get_sheets_port, None)
        db.expire_all()
        assert booking.patch_calls == []
        assert lead_id not in sheets.meeting_rows
    finally:
        db.close()


def test_cancellation_request_mirrors_status_and_lead_review_allows_it() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(
            store,
            external_id="gate2.sheet@example.com",
            channel=Channel.WEBSITE,
        )
        store.save_sales(SalesState(lead_id=lead_id, fit=FitLevel.GOOD))
        booking = FakeCalendarBookingPort()
        sheets = FakeSheetsPort()
        reply, outcomes, changed = resolve_meeting_reply(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            provider="website",
            conversation_id="web-gate2-sheet",
            inbound_provider_event_id="web-gate2-sheet:cancel",
            message="לבטל את הפגישה",
            base_reply="base",
            next_action="understand_workflow",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        meeting = store.get_meeting(lead_id)
        assert meeting is not None
        from app.domain.lead_reviews import build_lead_review_snapshot
        from app.integrations.sheets import MeetingMirrorRow, mirror_meeting

        snapshot = build_lead_review_snapshot(store, lead_id=lead_id)
        assert reply == CANCELLATION_REQUESTED_REPLY
        assert outcomes == []
        assert changed is True
        assert snapshot is not None
        assert snapshot.meeting_status == STATUS_CANCELLATION_REQUESTED
        assert mirror_meeting(
            sheets=sheets,
            row=MeetingMirrorRow(
                lead_id=lead_id,
                status=meeting.status,
                source=meeting.source,
                scheduled_at=meeting.scheduled_at,
                calendar_event_id=meeting.calendar_event_id,
                summary=meeting.summary,
            ),
            kill_switch=False,
        )
        mirrored = sheets.meeting_rows[lead_id]
        assert mirrored.status == STATUS_CANCELLATION_REQUESTED
        assert mirrored.scheduled_at
        assert mirrored.calendar_event_id
        assert booking.get_calls == []
        assert booking.patch_calls == []
    finally:
        db.close()


def test_successful_booking_cancels_pending_follow_up_and_send_stays_denied() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP,
            external_id="972509998812",
        )
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WHATSAPP,
            action="offer_meeting",
            kill_switch=False,
        )
        target = _slot(4, 11)
        assert store.save_offered_slots(
            lead_id=lead_id,
            slots=[target],
            now=FIXED_NOW,
            timezone="Asia/Jerusalem",
        )
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.WHATSAPP.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_PENDING,
            due_at="2026-08-20",
        )
        booking = FakeCalendarBookingPort()
        reply, _outcomes, changed = resolve_meeting_reply(
            store,
            lead_id=lead_id,
            channel=Channel.WHATSAPP,
            provider="whatsapp",
            conversation_id="972509998812",
            inbound_provider_event_id="wamid.gate2.book",
            message="1",
            base_reply="base",
            next_action="offer_meeting",
            calendar=FakeCalendarPort([target]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        follow_up = store.get_follow_up(lead_id)
        assert changed is True
        assert "נקבעה פגישה" in reply
        assert follow_up is not None
        assert follow_up.status == STATUS_CANCELLED
        assert follow_up.reason == REASON_MEETING_BOOKED
        assert follow_up.send_ready is False
        assert follow_up.block_reason == ""
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=SalesState(lead_id=lead_id, fit=FitLevel.GOOD),
            timezone="Asia/Jerusalem",
            kill_switch=False,
            now=FIXED_NOW,
        )
        assert decision.allowed is False
        assert decision.reason == REASON_MEETING_BOOKED
        event = store.get_canonical_event(
            provider=Channel.WHATSAPP.value,
            provider_event_id=f"{lead_id}:followup:{REASON_MEETING_BOOKED}:cancelled",
        )
        assert event is not None
        assert json.loads(event.payload_json) == {
            "status": STATUS_CANCELLED,
            "reason": REASON_MEETING_BOOKED,
        }
    finally:
        db.close()


def test_stale_pending_follow_up_cannot_send_after_booked_or_cancellation_request() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(
            store,
            external_id="972509998813",
            channel=Channel.WHATSAPP,
        )
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.WHATSAPP.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_PENDING,
            due_at="2026-08-20",
        )
        sales = SalesState(lead_id=lead_id, fit=FitLevel.GOOD)
        booked = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=sales,
            timezone="Asia/Jerusalem",
            kill_switch=False,
            now=FIXED_NOW,
        )
        assert booked.allowed is False
        assert booked.reason == REASON_MEETING_BOOKED
        assert store.mark_meeting_cancellation_requested(
            lead_id=lead_id,
            requested_at=FIXED_NOW.isoformat(),
        )
        cancellation_requested = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=sales,
            timezone="Asia/Jerusalem",
            kill_switch=False,
            now=FIXED_NOW,
        )
        assert cancellation_requested.allowed is False
        assert cancellation_requested.reason == REASON_MEETING_BOOKED
    finally:
        db.close()


def test_new_customer_copy_obeys_human_voice_anti_patterns() -> None:
    messages = (
        RESCHEDULE_OFFER_INTRO,
        RESCHEDULE_RETRY,
        RESCHEDULE_CONFLICT,
        RESCHEDULE_DENIED,
        RESCHEDULE_CONFIRMED,
        CANCELLATION_REQUESTED_REPLY,
    )
    forbidden = (
        "—",
        "–",
        "\\",
        "/",
        "Absolutely",
        "Let's dive in",
        "It's important to note",
        "game-changing",
        "seamless",
        "leverage",
        "בהחלט",
        "כמובן",
    )
    for message in messages:
        assert all(token not in message for token in forbidden)
        assert message.count("?") <= 1


def test_event_lookup_result_requires_complete_times() -> None:
    with pytest.raises(ValueError):
        EventLookupResult(
            status=BookingLookupStatus.FOUND,
            event=CalendarBookingEvent(event_id="evt_missing_times"),
        )


def test_settings_pin_is_unchanged() -> None:
    settings = Settings()
    assert settings.calendar_timezone == "Asia/Jerusalem"
    assert COMPOSIO_GOOGLECALENDAR_VERSION == "20260812_00"
