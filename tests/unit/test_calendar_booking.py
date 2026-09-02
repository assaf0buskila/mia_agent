"""Calendar booking: parser, ports, orchestration, E2E."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.api.deps import get_calendar_booking_port, get_calendar_port
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.models import MeetingRow, OwnerNotificationRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.booking_voice import BOOKING_RETRY
from app.domain.calendar_booking import (
    BookingResultKind,
    attempt_meeting_booking,
    resolve_meeting_reply,
)
from app.domain.events import Channel, EventType, build_meeting_booked_event
from app.domain.meeting_availability import is_workday_local
from app.domain.meeting_slots import (
    compute_booking_key,
    is_explicit_slot_selection,
    offered_slots_from_json,
    parse_slot_selection,
    sanitize_event_id,
    sanitize_meet_link,
    slot_interval_exactly_available,
    validate_offered_slots,
)
from app.domain.meetings import (
    MEETING_TYPE_INTRO_CALL,
    STATUS_BOOKED,
    STATUS_OFFERED,
    apply_meeting_policy,
)
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState
from app.domain.tools import AdapterHttpError
from app.graph.replies import WEBSITE_REPLIES
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import (
    FakeCalendarPort,
    TimeSlot,
    enrich_meeting_offer,
    prepare_meeting_offer,
)
from app.integrations.calendar_booking import (
    COMPOSIO_CREATE_EVENT_TOOL,
    COMPOSIO_EVENTS_LIST_TOOL,
    COMPOSIO_GOOGLECALENDAR_VERSION,
    BookingLookupResult,
    BookingLookupStatus,
    CalendarBookingEvent,
    ComposioCalendarBookingPort,
    DisabledCalendarBookingPort,
    FakeCalendarBookingPort,
    build_calendar_booking_port,
    lookup_tool_outcome,
    verify_tool_outcome,
)
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

IL = ZoneInfo("Asia/Jerusalem")
# Thursday 2026-08-20 09:00 Asia/Jerusalem (ADR-012 policy baseline)
FIXED_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
OFFER_COPY = WEBSITE_REPLIES[NextAction.OFFER_MEETING]
LEAD_EMAIL = "book.cal.1@example.com"


def _local_dt(*, days_ahead: int, hour: int, minute: int = 0) -> datetime:
    local_now = FIXED_NOW.astimezone(IL)
    local_start = (local_now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return local_start.astimezone(UTC)


def _slot(days_ahead: int, hour: int, minute: int = 0) -> TimeSlot:
    """Policy-valid 30m slot (Sun-Thu 09:00-17:00 IL, >=24h notice)."""
    start = _local_dt(days_ahead=days_ahead, hour=hour, minute=minute)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


def _bookable_slot_from_now(*, hour: int = 15) -> TimeSlot:
    """A slot that is still bookable against the live clock, not FIXED_NOW."""
    clock = datetime.now(UTC)
    local = clock.astimezone(IL) + timedelta(days=2)
    for _ in range(10):
        candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if is_workday_local(candidate):
            start = candidate.astimezone(UTC)
            return TimeSlot(start=start, end=start + timedelta(minutes=30))
        local = local + timedelta(days=1)
    raise AssertionError("no bookable workday slot in the next 12 days")


def _il_gap(*, days_ahead: int, start_hour: int, end_hour: int) -> TimeSlot:
    local_now = FIXED_NOW.astimezone(IL)
    local_date = (local_now + timedelta(days=days_ahead)).date()
    local_start = datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        start_hour,
        0,
        tzinfo=IL,
    )
    local_end = datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        end_hour,
        0,
        tzinfo=IL,
    )
    return TimeSlot(
        start=local_start.astimezone(UTC),
        end=local_end.astimezone(UTC),
    )


def _next_real_business_slot(*, min_days_ahead: int, hour: int, minute: int = 0) -> TimeSlot:
    """A policy-valid 30m slot computed from the *real* clock, for the one test
    (`test_website_e2e_booking`) that goes through the live HTTP endpoint without
    freezing `now` — so the slot it seeds must actually be valid against whatever
    day the suite happens to run on, not just against FIXED_NOW.

    A fixed `days_ahead` from FIXED_NOW (2026-08-20, a Thursday) rots: the resulting
    calendar date is static, so as real time passes, that date can land on a Friday
    or Saturday, and ADR-012's Sun-Thu policy then correctly refuses the booking —
    which looked like a broken calendar integration rather than a stale fixture.
    Walking forward from the real "today" to the next Sun-Thu day keeps the slot
    valid on every day the suite is run, the same fix already applied to the sibling
    fixture in `tests/unit/test_owner_calendar.py::_next_workday` (commit 094c052).
    """
    local_now = datetime.now(UTC).astimezone(IL)
    candidate = (local_now + timedelta(days=min_days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    while not is_workday_local(candidate):
        candidate += timedelta(days=1)
    return TimeSlot(
        start=candidate.astimezone(UTC),
        end=(candidate + timedelta(minutes=30)).astimezone(UTC),
    )


def _booked_at() -> str:
    return FIXED_NOW.isoformat()


def _ready_state(lead_id: str) -> SalesState:
    return SalesState(
        lead_id=lead_id,
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        authority_known=True,
        timeline_known=True,
        metric_known=True,
        buying_reality_known=True,
        fit=FitLevel.GOOD,
        willingness_to_meet=True,
        company_domain="clinic.co.il",
        missing_fields=[],
    )


def _seed_offered(
    store: LeadStore,
    lead_id: str,
    slots: list[TimeSlot],
    *,
    now: datetime | None = None,
) -> None:
    apply_meeting_policy(
        store,
        lead_id=lead_id,
        channel=Channel.GMAIL,
        action=NextAction.OFFER_MEETING.value,
        kill_switch=False,
    )
    store.save_offered_slots(
        lead_id=lead_id,
        slots=slots,
        now=now or FIXED_NOW,
        timezone="Asia/Jerusalem",
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("1", 1),
        ("2", 2),
        ("3", 3),
        ("slot 1", 1),
        ("option 2", 2),
        ("אפשרות 3", 3),
        ("הראשון", 1),
        ("השני", 2),
        ("השלישי", 3),
        ("yes", None),
        ("21/08", None),
        ("4", None),
        ("", None),
    ],
)
def test_parse_slot_selection(message: str, expected: int | None) -> None:
    from app.domain.meeting_slots import OfferedSlot

    slots = [
        OfferedSlot(start=_slot(4, 10).start, end=_slot(4, 10).end),
        OfferedSlot(start=_slot(4, 14).start, end=_slot(4, 14).end),
        OfferedSlot(start=_slot(5, 9).start, end=_slot(5, 9).end),
    ]
    assert (
        parse_slot_selection(message, offered_slots=slots, meeting_status=STATUS_OFFERED)
        == expected
    )


def test_parse_rejects_without_offers() -> None:
    assert parse_slot_selection("1", offered_slots=[], meeting_status=STATUS_OFFERED) is None
    assert parse_slot_selection("1", offered_slots=[], meeting_status=STATUS_BOOKED) is None


def test_validate_offered_slots_rejects_naive_and_normalizes_utc() -> None:
    from zoneinfo import ZoneInfo

    aware = _slot(4, 10)
    naive = TimeSlot(
        start=datetime(2026, 8, 22, 10, 0),
        end=datetime(2026, 8, 22, 10, 30),
    )
    clean = validate_offered_slots([naive, aware], now=FIXED_NOW)
    assert len(clean) == 1
    assert clean[0].start.tzinfo == UTC
    il = ZoneInfo("Asia/Jerusalem")
    offset_start = clean[0].start.astimezone(il)
    offset_end = offset_start + timedelta(minutes=30)
    key_utc = compute_booking_key(
        lead_id="lead_x", start=aware.start, end=aware.end
    )
    key_offset = compute_booking_key(
        lead_id="lead_x", start=offset_start, end=offset_end
    )
    assert key_utc == key_offset


def test_validate_offered_slots_max_three_future_30m() -> None:
    slots = [_slot(4, h) for h in (9, 10, 11, 12)]
    past = TimeSlot(
        start=FIXED_NOW - timedelta(hours=2),
        end=FIXED_NOW - timedelta(hours=1, minutes=30),
    )
    short = TimeSlot(
        start=FIXED_NOW + timedelta(days=1),
        end=FIXED_NOW + timedelta(days=1, minutes=15),
    )
    clean = validate_offered_slots([past, short, *slots], now=FIXED_NOW)
    assert len(clean) == 3


def test_booked_status_forward_only_no_downgrade() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id="fwd_book_1")
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        store.mark_meeting_booked(
            lead_id=lead_id,
            scheduled_at=_slot(5, 10).start.isoformat(),
            calendar_event_id="evt_existing",
            meet_link="https://meet.google.com/abc-defg-hij",
            booked_at=_booked_at(),
        )
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        db.commit()
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_BOOKED
        assert row.calendar_event_id == "evt_existing"
        assert row.offered_slots_json == "[]"
    finally:
        db.close()


def test_prepare_meeting_offer_numbered_and_wrapper_compat() -> None:
    gaps = [
        _il_gap(days_ahead=4, start_hour=10, end_hour=11),
        _il_gap(days_ahead=4, start_hour=14, end_hour=15),
    ]
    calendar = FakeCalendarPort(gaps)
    prepared = prepare_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=calendar,
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert "1." in prepared.reply
    assert "2." in prepared.reply
    assert "השיבו 1, 2, 3 כדי" in prepared.reply
    assert len(prepared.slots) == 3
    wrapped, outcome = enrich_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=calendar,
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert wrapped == prepared.reply
    assert outcome == prepared.outcome


def test_kill_switch_blocks_booking_no_port_calls() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id=LEAD_EMAIL)
        _seed_offered(store, lead_id, [_slot(4, 10)])
        db.commit()
        booking = FakeCalendarBookingPort()
        calendar = FakeCalendarPort([_slot(4, 10)])
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id=LEAD_EMAIL,
            inbound_provider_event_id="evt.ks.1",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=True,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == BookingResultKind.DENIED
        assert booking.lookup_calls == []
        assert booking.create_calls == []
    finally:
        db.close()


def test_conflict_recheck_blocks_create_and_clears_offers() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="conflict@ex.com")
        offered = _slot(4, 10)
        _seed_offered(store, lead_id, [offered])
        db.commit()
        booking = FakeCalendarBookingPort()
        fresh = [_slot(5, 11), _slot(5, 15)]
        reply, outcomes, _changed = resolve_meeting_reply(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="conflict@ex.com",
            inbound_provider_event_id="evt.conf.1",
            message="1",
            base_reply=OFFER_COPY,
            next_action=NextAction.OFFER_MEETING.value,
            calendar=FakeCalendarPort(fresh),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert booking.create_calls == []
        assert "כבר לא פנוי" in reply
        assert "1." in reply
        tools = {o.tool for o in outcomes}
        assert "calendar_find_free_slots" in tools
        row = store.get_meeting(lead_id)
        assert row is not None
        assert offered_slots_from_json(row.offered_slots_json or "[]")
    finally:
        db.close()


def test_fake_booking_success_updates_meeting_event_reply() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="book.ok@ex.com")
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        booking = FakeCalendarBookingPort(
            create_result=CalendarBookingEvent(
                event_id="evt_live_123",
                meet_link="https://meet.google.com/xyz-abcd-efg",
            )
        )
        calendar = FakeCalendarPort([slot])
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="book.ok@ex.com",
            inbound_provider_event_id="evt.book.1",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert result.kind == BookingResultKind.BOOKED
        assert "נקבעה פגישה" in result.reply
        assert "meet.google.com" in result.reply
        verify_outcomes = [
            o for o in result.tool_outcomes if o.tool == "calendar_booking_verify"
        ]
        assert verify_outcomes[0].status == "ok"
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_BOOKED
        assert row.calendar_event_id == "evt_live_123"
        assert row.meeting_type == MEETING_TYPE_INTRO_CALL
        assert row.booked_at
        assert row.scheduled_at
        assert row.offered_slots_json == "[]"
        booked = store.get_canonical_event(
            provider="gmail", provider_event_id=f"{lead_id}:booked"
        )
        assert booked is not None
        payload = json.loads(booked.payload_json)
        assert payload == {"status": "booked", "scheduled_at": payload["scheduled_at"]}
        assert "event_id" not in payload and "meet" not in payload
        notify_rows = list(
            db.scalars(
                select(OwnerNotificationRow).where(
                    OwnerNotificationRow.kind == "meeting_booked",
                    OwnerNotificationRow.lead_id == lead_id,
                )
            ).all()
        )
        assert len(notify_rows) == 1
        assert notify_rows[0].seen_at == ""
        assert notify_rows[0].scheduled_at
    finally:
        db.execute(
            delete(OwnerNotificationRow).where(
                OwnerNotificationRow.lead_id == lead_id,
            )
        )
        db.commit()
        db.close()


def test_repeat_confirmation_no_create() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="repeat@ex.com")
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        booking = FakeCalendarBookingPort()
        calendar = FakeCalendarPort([slot])
        first = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="repeat@ex.com",
            inbound_provider_event_id="evt.rep.1",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert first.kind == BookingResultKind.BOOKED
        assert len(booking.create_calls) == 1
        second = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="repeat@ex.com",
            inbound_provider_event_id="evt.rep.2",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert second.kind == BookingResultKind.ALREADY_BOOKED
        assert len(booking.create_calls) == 1
        assert len(booking.lookup_calls) == 2
    finally:
        db.close()


def test_crash_recovery_lookup_books_without_create() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="recover@ex.com")
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        key = compute_booking_key(lead_id=lead_id, start=slot.start, end=slot.end)
        existing = CalendarBookingEvent(
            event_id="evt_recovered", meet_link="https://meet.google.com/rec-over-ed"
        )
        booking = FakeCalendarBookingPort(existing={key: existing})
        calendar = FakeCalendarPort([])
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="recover@ex.com",
            inbound_provider_event_id="evt.rec.1",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=slot.start - timedelta(hours=1),
        )
        db.commit()
        assert result.kind == BookingResultKind.BOOKED
        assert booking.create_calls == []
        assert len(booking.lookup_calls) == 1
        assert not any(
            o.tool == "calendar_find_free_slots" for o in result.tool_outcomes
        )
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_BOOKED
        assert row.calendar_event_id == "evt_recovered"
    finally:
        db.close()


def test_composio_lookup_create_request_shape_no_pii() -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((str(request.url), json.loads(request.content)))
        tool = request.url.path.rsplit("/", 1)[-1]
        if tool == COMPOSIO_EVENTS_LIST_TOOL:
            return httpx.Response(200, json={"data": {"items": []}, "successful": True})
        return httpx.Response(
            200,
            json={
                "data": {
                    "response_data": {
                        "id": "evt_composio_1",
                        "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc",
                    }
                },
                "successful": True,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = ComposioCalendarBookingPort(api_key="cmp", user_id="user-1", client=client)
    start = _local_dt(days_ahead=4, hour=10)
    end = start + timedelta(minutes=30)
    key = compute_booking_key(lead_id="lead_test_1", start=start, end=end)
    lookup = port.find_by_booking_key(booking_key=key)
    assert lookup.status == BookingLookupStatus.NOT_FOUND
    assert lookup_tool_outcome(lookup) == ("empty", 0)
    port.create_event(booking_key=key, start=start, end=end, timezone="Asia/Jerusalem")
    assert len(captured) == 2
    lookup_url, lookup_body = captured[0]
    create_url, create_body = captured[1]
    assert lookup_url.endswith(f"/{COMPOSIO_EVENTS_LIST_TOOL}")
    assert create_url.endswith(f"/{COMPOSIO_CREATE_EVENT_TOOL}")
    assert lookup_body["version"] == COMPOSIO_GOOGLECALENDAR_VERSION
    assert create_body["version"] == COMPOSIO_GOOGLECALENDAR_VERSION
    lookup_args = lookup_body["arguments"]
    create_args = create_body["arguments"]
    assert lookup_args["privateExtendedProperty"] == f"mia_booking_key={key}"
    assert create_args["summary"] == "AssafWeb intro call"
    assert create_args["send_updates"] == "none"
    assert "attendees" not in create_args
    assert "description" not in create_args
    serialized = json.dumps(create_body)
    assert "lead" not in serialized.lower()


def test_build_meeting_booked_event_allowlist() -> None:
    event = build_meeting_booked_event(
        provider="website",
        channel=Channel.WEBSITE,
        lead_id="lead_abc",
        conversation_id="sess_1",
        scheduled_at="2026-08-22T07:00:00+00:00",
    )
    assert event.event_type == EventType.MEETING_BOOKED
    assert set(event.payload.keys()) == {"status", "scheduled_at"}
    assert event.payload["status"] == "booked"


def test_build_calendar_booking_port_disabled_lookup_is_error() -> None:
    settings = Settings(composio_api_key="", composio_user_id="")
    port = build_calendar_booking_port(settings)
    assert isinstance(port, DisabledCalendarBookingPort)
    result = port.find_by_booking_key(booking_key="mia_" + "a" * 64)
    assert result.status == BookingLookupStatus.ERROR


def test_lookup_error_blocks_create() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="lkerr@ex.com")
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        key = compute_booking_key(lead_id=lead_id, start=slot.start, end=slot.end)
        booking = FakeCalendarBookingPort(lookup_errors={key})
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="lkerr@ex.com",
            inbound_provider_event_id="evt.lkerr.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == BookingResultKind.RETRY
        assert booking.create_calls == []
        lookup_outcomes = [
            o for o in result.tool_outcomes if o.tool == "calendar_booking_lookup"
        ]
        assert lookup_outcomes[0].status == "error"
    finally:
        db.close()


def test_invalid_provider_event_id_returns_retry_not_booked() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="badid@ex.com")
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        bad = CalendarBookingEvent.model_construct(
            event_id="bad\nid", meet_link="https://meet.google.com/abc-defg-hij"
        )
        booking = FakeCalendarBookingPort(create_result=bad)
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="badid@ex.com",
            inbound_provider_event_id="evt.bad.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert result.kind == BookingResultKind.RETRY
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_OFFERED
        booked_event = store.get_canonical_event(
            provider="gmail", provider_event_id=f"{lead_id}:booked"
        )
        assert booked_event is None
    finally:
        db.close()


def test_booked_unrelated_text_skips_calendar_ports() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="booked@ex.com")
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        store.mark_meeting_booked(
            lead_id=lead_id,
            scheduled_at=_slot(5, 10).start.isoformat(),
            calendar_event_id="evt_booked_keep",
            meet_link="https://meet.google.com/abc-defg-hij",
            booked_at=_booked_at(),
        )
        db.commit()

        class ExplodingCalendarPort:
            def find_free_slots(self, **_kwargs: object) -> list[TimeSlot]:
                raise RuntimeError("calendar must not be called for booked unrelated text")

        class ExplodingBookingPort:
            def find_by_booking_key(self, **_kwargs: object) -> BookingLookupResult:
                raise RuntimeError("booking lookup must not be called")

            def create_event(self, **_kwargs: object) -> CalendarBookingEvent | None:
                raise RuntimeError("booking create must not be called")

        reply, outcomes, changed = resolve_meeting_reply(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="booked@ex.com",
            inbound_provider_event_id="evt.booked.follow",
            message="thanks for the info",
            base_reply="base sales reply",
            next_action=NextAction.OFFER_MEETING.value,
            calendar=ExplodingCalendarPort(),  # type: ignore[arg-type]
            booking_port=ExplodingBookingPort(),  # type: ignore[arg-type]
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert reply == "base sales reply"
        assert outcomes == []
        assert changed is False
        assert "זמין:" not in reply
    finally:
        db.close()


def test_booked_repeat_selection_returns_local_confirmation() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="repsel@ex.com")
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        store.mark_meeting_booked(
            lead_id=lead_id,
            scheduled_at=_slot(5, 10).start.isoformat(),
            calendar_event_id="evt_booked_keep",
            meet_link="https://meet.google.com/abc-defg-hij",
            booked_at=_booked_at(),
        )
        db.commit()
        booking = FakeCalendarBookingPort()
        reply, outcomes, changed = resolve_meeting_reply(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="repsel@ex.com",
            inbound_provider_event_id="evt.repsel.1",
            message="1",
            base_reply=OFFER_COPY,
            next_action=NextAction.OFFER_MEETING.value,
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert "נקבעה פגישה" in reply
        assert booking.lookup_calls == []
        assert booking.create_calls == []
        assert outcomes == []
        assert changed is False
    finally:
        db.close()


def test_sanitize_event_id_and_meet_link() -> None:
    assert sanitize_event_id("evt_123") == "evt_123"
    assert sanitize_event_id("bad\nid") is None
    assert sanitize_meet_link("https://meet.google.com/abc-defg-hij") == (
        "https://meet.google.com/abc-defg-hij"
    )
    assert sanitize_meet_link("http://meet.google.com/abc") == ""
    assert sanitize_meet_link("https://evil.com/x") == ""
    assert sanitize_meet_link("https://meet.google.com:badport/abc-defg-hij") == ""


def test_composio_malformed_lookup_page_is_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": "not-a-list"}, "successful": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = ComposioCalendarBookingPort(api_key="cmp", user_id="user-1", client=client)
    result = port.find_by_booking_key(booking_key="mia_" + "b" * 64)
    assert result.status == BookingLookupStatus.ERROR


def test_composio_create_accepts_event_id_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {"response_data": {"event_id": "evt_fallback_1"}},
                "successful": True,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = ComposioCalendarBookingPort(api_key="cmp", user_id="user-1", client=client)
    start = _local_dt(days_ahead=4, hour=10)
    end = start + timedelta(minutes=30)
    created = port.create_event(
        booking_key="mia_" + "c" * 64,
        start=start,
        end=end,
        timezone="Asia/Jerusalem",
    )
    assert created is not None
    assert created.event_id == "evt_fallback_1"


def test_mark_meeting_booked_rejects_different_event_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="diff@ex.com")
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        store.mark_meeting_booked(
            lead_id=lead_id,
            scheduled_at=_slot(5, 10).start.isoformat(),
            calendar_event_id="evt_first",
            meet_link="https://meet.google.com/abc-defg-hij",
            booked_at=_booked_at(),
        )
        assert (
            store.mark_meeting_booked(
                lead_id=lead_id,
                scheduled_at=_slot(5, 10).start.isoformat(),
                calendar_event_id="evt_other",
                meet_link="",
                booked_at=_booked_at(),
            )
            is False
        )
    finally:
        db.close()


def test_create_timeout_verify_persists_booked() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="timeout@ex.com")
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        booking = FakeCalendarBookingPort(create_returns_none=True)
        calendar = FakeCalendarPort([slot])
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="timeout@ex.com",
            inbound_provider_event_id="evt.timeout.1",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert result.kind == BookingResultKind.BOOKED
        create_outcomes = [o for o in result.tool_outcomes if o.tool == "calendar_create"]
        verify_outcomes = [
            o for o in result.tool_outcomes if o.tool == "calendar_booking_verify"
        ]
        assert create_outcomes[0].status == "error"
        assert verify_outcomes[0].status == "ok"
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_BOOKED
    finally:
        db.close()


def test_verify_mismatch_returns_retry_not_booked() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="vm@ex.com")
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()

        class MismatchVerifyPort:
            def __init__(self) -> None:
                self.lookup_calls = 0
                self.create_calls: list[dict[str, object]] = []

            def find_by_booking_key(
                self, *, booking_key: str, calendar_id: str = "primary"
            ) -> BookingLookupResult:
                del calendar_id
                self.lookup_calls += 1
                if self.lookup_calls == 1:
                    return BookingLookupResult(status=BookingLookupStatus.NOT_FOUND)
                return BookingLookupResult(
                    status=BookingLookupStatus.FOUND,
                    event=CalendarBookingEvent(
                        event_id="evt_verify_other",
                        meet_link="https://meet.google.com/abc-defg-hij",
                    ),
                )

            def create_event(self, **kwargs: object) -> CalendarBookingEvent:
                self.create_calls.append(kwargs)
                return CalendarBookingEvent(
                    event_id="evt_created_one",
                    meet_link="https://meet.google.com/abc-defg-hij",
                )

        booking = MismatchVerifyPort()
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="vm@ex.com",
            inbound_provider_event_id="evt.vm.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == BookingResultKind.RETRY
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_OFFERED
        verify_outcomes = [
            o for o in result.tool_outcomes if o.tool == "calendar_booking_verify"
        ]
        assert verify_outcomes[0].status == "error"
    finally:
        db.close()


def test_stale_slot_inside_24h_at_confirm_is_conflict() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id="stale@ex.com")
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        booking = FakeCalendarBookingPort()
        later = slot.start - timedelta(hours=23)
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="stale@ex.com",
            inbound_provider_event_id="evt.stale.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=later,
        )
        assert result.kind == BookingResultKind.CONFLICT
        assert booking.create_calls == []
    finally:
        db.close()


def test_calendar_booking_verify_allowlisted() -> None:
    from app.domain.tools import ALLOWLISTED_TOOLS, ToolOutcome

    assert "calendar_booking_verify" in ALLOWLISTED_TOOLS
    outcome = ToolOutcome(tool="calendar_booking_verify", status="ok", result_count=1)
    assert outcome.tool == "calendar_booking_verify"


def test_verify_tool_outcome_mismatch() -> None:
    found = BookingLookupResult(
        status=BookingLookupStatus.FOUND,
        event=CalendarBookingEvent(
            event_id="evt_a", meet_link="https://meet.google.com/abc-defg-hij"
        ),
    )
    assert verify_tool_outcome(found, create_event_id="evt_b") == ("error", 0)
    assert verify_tool_outcome(found, create_event_id="evt_a") == ("ok", 1)


def test_slot_interval_exactly_available_requires_full_cover() -> None:
    from app.domain.meeting_slots import OfferedSlot

    selected = OfferedSlot(start=_slot(4, 10).start, end=_slot(4, 10).end)
    partial = [
        TimeSlot(
            start=_slot(4, 10).start,
            end=_slot(4, 10).start + timedelta(minutes=15),
        )
    ]
    full = [_slot(4, 10)]
    assert slot_interval_exactly_available(partial, selected=selected) is False
    assert slot_interval_exactly_available(full, selected=selected) is True


def test_is_explicit_slot_selection() -> None:
    assert is_explicit_slot_selection("1")
    assert is_explicit_slot_selection("הראשון")
    assert not is_explicit_slot_selection("yes")


class _RaisingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.HTTPError("network error")


def test_composio_booking_port_http_500_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = ComposioCalendarBookingPort(api_key="cmp", user_id="user-1", client=client)
    start = _local_dt(days_ahead=4, hour=10)
    end = start + timedelta(minutes=30)
    key = compute_booking_key(lead_id="lead_test_1", start=start, end=end)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.create_event(
            booking_key=key,
            start=start,
            end=end,
            timezone="Asia/Jerusalem",
        )
    assert exc_info.value.status_code == 500


def test_composio_booking_port_network_error_raises_adapter_error() -> None:
    port = ComposioCalendarBookingPort(
        api_key="cmp",
        user_id="user-1",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.find_by_booking_key(booking_key="mia_" + "d" * 64)
    assert exc_info.value.status_code is None


def test_composio_booking_port_http_429_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(429))
    client = httpx.Client(transport=transport)
    port = ComposioCalendarBookingPort(api_key="cmp", user_id="user-1", client=client)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.find_by_booking_key(booking_key="mia_" + "e" * 64)
    assert exc_info.value.status_code == 429


def test_lookup_adapter_http_error_blocks_create() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="lkhttp401@ex.com"
        )
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()

        class Lookup401Port(FakeCalendarBookingPort):
            def find_by_booking_key(
                self, *, booking_key: str, calendar_id: str = "primary"
            ) -> BookingLookupResult:
                del booking_key, calendar_id
                raise AdapterHttpError(401)

        booking = Lookup401Port()
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="lkhttp401@ex.com",
            inbound_provider_event_id="evt.lkhttp401.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == BookingResultKind.RETRY
        assert result.reply == BOOKING_RETRY
        lookup_outcomes = [
            o for o in result.tool_outcomes if o.tool == "calendar_booking_lookup"
        ]
        assert lookup_outcomes[0].status == "unauthorized"
        assert booking.create_calls == []
    finally:
        db.close()


def test_create_adapter_http_error_verify_recovery_books() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="crt500@ex.com"
        )
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()

        class Create500VerifyRecoverPort:
            def __init__(self) -> None:
                self.lookup_calls = 0
                self.create_calls: list[dict[str, object]] = []

            def find_by_booking_key(
                self, *, booking_key: str, calendar_id: str = "primary"
            ) -> BookingLookupResult:
                del calendar_id
                self.lookup_calls += 1
                if self.lookup_calls == 1:
                    return BookingLookupResult(status=BookingLookupStatus.NOT_FOUND)
                return BookingLookupResult(
                    status=BookingLookupStatus.FOUND,
                    event=CalendarBookingEvent(
                        event_id="evt_verify_recovered",
                        meet_link="https://meet.google.com/abc-defg-hij",
                    ),
                )

            def create_event(self, **kwargs: object) -> CalendarBookingEvent | None:
                self.create_calls.append(kwargs)
                raise AdapterHttpError(500)

        booking = Create500VerifyRecoverPort()
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="crt500@ex.com",
            inbound_provider_event_id="evt.crt500.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert result.kind == BookingResultKind.BOOKED
        create_outcomes = [o for o in result.tool_outcomes if o.tool == "calendar_create"]
        verify_outcomes = [
            o for o in result.tool_outcomes if o.tool == "calendar_booking_verify"
        ]
        assert create_outcomes[0].status == "retryable"
        assert verify_outcomes[0].status == "ok"
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_BOOKED
    finally:
        db.close()


def test_create_and_verify_adapter_http_error_returns_retry() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="crt500v429@ex.com"
        )
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()

        class Create500Verify429Port:
            def __init__(self) -> None:
                self.lookup_calls = 0
                self.create_calls: list[dict[str, object]] = []

            def find_by_booking_key(
                self, *, booking_key: str, calendar_id: str = "primary"
            ) -> BookingLookupResult:
                del calendar_id
                self.lookup_calls += 1
                if self.lookup_calls == 1:
                    return BookingLookupResult(status=BookingLookupStatus.NOT_FOUND)
                raise AdapterHttpError(429)

            def create_event(self, **kwargs: object) -> CalendarBookingEvent | None:
                self.create_calls.append(kwargs)
                raise AdapterHttpError(500)

        booking = Create500Verify429Port()
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="crt500v429@ex.com",
            inbound_provider_event_id="evt.crt500v429.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == BookingResultKind.RETRY
        create_outcomes = [o for o in result.tool_outcomes if o.tool == "calendar_create"]
        verify_outcomes = [
            o for o in result.tool_outcomes if o.tool == "calendar_booking_verify"
        ]
        assert create_outcomes[0].status == "retryable"
        assert verify_outcomes[0].status == "rate_limited"
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_OFFERED
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_e2e_booking_one_reply(monkeypatch) -> None:
    from tests.conftest import freeze_mia_clock

    freeze_mia_clock(monkeypatch, FIXED_NOW)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id=LEAD_EMAIL)
        store.save_sales(_ready_state(lead_id))
        slot = _slot(4, 11)
        _seed_offered(store, lead_id, [slot, _slot(4, 14)])
        db.commit()
        calendar = FakeCalendarPort([slot, _slot(4, 14)])
        booking = FakeCalendarBookingPort()
        port = RecordingMessagePort()
        result = await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[{"id": "evt.e2e.book", "from": LEAD_EMAIL, "text": "1"}],
            store=store,
            port=port,
            kill_switch=False,
            calendar=calendar,
            calendar_booking=booking,
        )
        db.commit()
        assert result["processed"] == 1
        assert len(port.sent) == 1
        assert "נקבעה פגישה" in port.sent[0].text
        assert len(booking.create_calls) == 1
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_BOOKED
    finally:
        db.close()


def test_website_e2e_booking() -> None:
    # This is the one test in this file that drives the live HTTP endpoint without
    # freezing the clock, so the seeded slot must be computed from the real clock
    # rather than a fixed offset from FIXED_NOW.
    #
    # Master fixed the same date-rot by freezing the clock to FIXED_NOW instead. Both
    # fixes work in isolation, but not together: the body below seeds its slot from
    # the real clock, so freezing "now" back to 2026-08-20 would leave the app
    # judging a slot six days past a frozen present. Kept the real-clock version,
    # which is self-consistent end to end.
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_book_e2e_1"
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        store.save_sales(_ready_state(lead_id))
        slot = _bookable_slot_from_now()
        _seed_offered(store, lead_id, [slot], now=datetime.now(UTC))
        db.commit()
        fake_cal = FakeCalendarPort([slot])
        fake_book = FakeCalendarBookingPort()
        app.dependency_overrides[get_calendar_port] = lambda: fake_cal
        app.dependency_overrides[get_calendar_booking_port] = lambda: fake_book
        try:
            with TestClient(app) as client:
                response = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "1"},
                )
                assert response.status_code == 200
                body = response.json()
                assert body["next_action"] in {"ask_need", "ask_contact"}
                assert "נקבעה פגישה" not in body["message"]
                assert fake_book.create_calls == []
        finally:
            app.dependency_overrides.pop(get_calendar_port, None)
            app.dependency_overrides.pop(get_calendar_booking_port, None)
    finally:
        db.close()
