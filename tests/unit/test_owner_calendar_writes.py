import json
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_CALENDAR_CREATE,
    DECISION_APPROVED,
    DECISION_PENDING,
    RESOURCE_CALENDAR,
)
from app.domain.events import Channel
from app.domain.owner_calendar_writes import (
    apply_owner_calendar_change_request,
    decide_calendar_change,
    execute_approved_calendar_change,
)
from app.integrations.calendar import FakeCalendarPort, TimeSlot
from app.integrations.calendar_booking import FakeCalendarBookingPort


def _store():
    init_db()
    db = get_session_factory()()
    return db, LeadStore(db)


def _request() -> str:
    return "צור אירוע: פגישת תכנון בתל אביב | 2026-09-02T10:00 | 60 | Asia/Jerusalem"


def test_owner_calendar_create_is_payload_bound_and_waits_for_approval() -> None:
    db, store = _store()
    try:
        reply = apply_owner_calendar_change_request(
            store,
            text=_request(),
            channel=Channel.TELEGRAM,
            kill_switch=False,
            demo_active=False,
            default_timezone="Asia/Jerusalem",
        )
        row = next(
            item
            for item in store.list_all_pending_approvals()
            if item.action == ACTION_CALENDAR_CREATE
        )
        assert "לא שיניתי" in reply
        assert row.decision == DECISION_PENDING
        assert row.resource_type == RESOURCE_CALENDAR
        assert json.loads(row.proposed_parameters)["title"] == "פגישת תכנון בתל אביב"
        decision, resource_id = decide_calendar_change(
            store,
            text=f"אשר אירוע {row.approval_id}",
            kill_switch=False,
        )
        assert decision == DECISION_APPROVED
        assert resource_id == row.resource_id
        assert row.decision == DECISION_APPROVED
    finally:
        db.close()


def test_approved_owner_calendar_create_preflights_then_executes_once() -> None:
    db, store = _store()
    try:
        apply_owner_calendar_change_request(
            store,
            text=_request(),
            channel=Channel.TELEGRAM,
            kill_switch=False,
            demo_active=False,
            default_timezone="Asia/Jerusalem",
        )
        row = next(
            item
            for item in store.list_all_pending_approvals()
            if item.action == ACTION_CALENDAR_CREATE
        )
        decision, resource_id = decide_calendar_change(
            store,
            text=f"אשר אירוע {row.approval_id}",
            kill_switch=False,
        )
        assert decision == DECISION_APPROVED
        assert resource_id
        start = datetime(2026, 9, 2, 7, 0, tzinfo=UTC)
        calendar = FakeCalendarPort([TimeSlot(start=start, end=start + timedelta(hours=2))])
        booking = FakeCalendarBookingPort()
        result = execute_approved_calendar_change(
            store=store,
            settings=Settings(calendar_write=True),
            calendar=calendar,
            booking=booking,
            resource_id=resource_id,
            kill_switch=False,
            demo_active=False,
        )
        assert result == "יצרתי את האירוע ביומן."
        assert booking.create_calls[0]["summary"] == "פגישת תכנון בתל אביב"
        assert booking.create_calls[0]["create_meeting_room"] is False
        replay = execute_approved_calendar_change(
            store=store,
            settings=Settings(calendar_write=True),
            calendar=calendar,
            booking=booking,
            resource_id=resource_id,
            kill_switch=False,
            demo_active=False,
        )
        assert "כבר טופל" in replay
        assert len(booking.create_calls) == 1
    finally:
        db.close()
