import importlib
import inspect
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.core.errors import PolicyDenied
from app.db.models import OwnerNotificationRow, OwnerTaskRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.meetings.booking import BookingResultKind, attempt_meeting_booking
from app.domain.meetings.changes import (
    MeetingChangeKind,
    resolve_booked_meeting_change,
)
from app.domain.owner.notifications import (
    KIND_MEETING_BOOKED,
    KIND_MEETING_CANCELLATION_REQUESTED,
    KIND_MEETING_RESCHEDULED,
    OWNER_NOTIFY_KINDS,
    apply_owner_notify,
    persist_meeting_booked_owner_notify,
)
from app.domain.owner.tasks import OwnerTaskType, classify_owner_task
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import FakeCalendarPort, TimeSlot
from app.integrations.calendar_booking import CalendarBookingEvent, FakeCalendarBookingPort
from sqlalchemy import delete, select

IL = ZoneInfo("Asia/Jerusalem")
FIXED_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
OWNER_PHONE = "972509994902"
OWNER_EVENT = "evt.owner.notify.inbound.1"
OWNER_EVENT_KILL = "evt.owner.notify.inbound.2"


def _local_dt(*, days_ahead: int, hour: int, minute: int = 0) -> datetime:
    local_now = FIXED_NOW.astimezone(IL)
    local_start = (local_now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return local_start.astimezone(UTC)


def _slot(days_ahead: int, hour: int, minute: int = 0) -> TimeSlot:
    start = _local_dt(days_ahead=days_ahead, hour=hour, minute=minute)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


def _seed_offered(store: LeadStore, lead_id: str, slots: list[TimeSlot]) -> None:
    from app.domain.meetings.state import apply_meeting_policy
    from app.domain.sales import NextAction

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
        now=FIXED_NOW,
        timezone="Asia/Jerusalem",
    )


def _notify_rows(
    db, *, lead_id: str, kind: str = KIND_MEETING_BOOKED
) -> list[OwnerNotificationRow]:
    return list(
        db.scalars(
            select(OwnerNotificationRow).where(
                OwnerNotificationRow.kind == kind,
                OwnerNotificationRow.lead_id == lead_id,
            )
        ).all()
    )


def _delete_notify_rows(db, *, lead_ids: tuple[str, ...]) -> None:
    for lead_id in lead_ids:
        db.execute(
            delete(OwnerNotificationRow).where(OwnerNotificationRow.lead_id == lead_id)
        )
    db.commit()


def _clear_unseen_notifications(db) -> None:
    unseen = list(
        db.scalars(
            select(OwnerNotificationRow).where(
                OwnerNotificationRow.kind.in_(OWNER_NOTIFY_KINDS),
                OwnerNotificationRow.seen_at == "",
            )
        ).all()
    )
    if unseen:
        LeadStore(db).mark_owner_notifications_seen(
            [row.id for row in unseen],
            seen_at="2020-01-01T00:00:00+00:00",
        )
        db.commit()


def _seed_booked(
    store: LeadStore,
    *,
    external_id: str,
    slot: TimeSlot | None = None,
) -> str:
    from app.domain.meetings.state import apply_meeting_policy

    _, lead_id = store.open_channel_lead(
        channel=Channel.GMAIL, external_id=external_id
    )
    apply_meeting_policy(
        store,
        lead_id=lead_id,
        channel=Channel.GMAIL,
        action="offer_meeting",
        kill_switch=False,
    )
    booked_slot = slot or _slot(4, 9)
    assert store.mark_meeting_booked(
        lead_id=lead_id,
        scheduled_at=booked_slot.start.isoformat(),
        calendar_event_id=f"evt_{external_id.replace('@', '_')}",
        meet_link="https://meet.google.com/abc-defg-hij",
        booked_at=FIXED_NOW.isoformat(),
    )
    return lead_id


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


def _delete_test_rows(db, *, event_ids: tuple[str, ...]) -> None:
    for event_id in event_ids:
        db.execute(
            delete(OwnerTaskRow).where(
                OwnerTaskRow.provider == "whatsapp",
                OwnerTaskRow.provider_event_id == event_id,
            )
        )
    db.commit()


def test_booking_persists_unseen_owner_notify() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.book@ex.com"
        )
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="notify.book@ex.com",
            inbound_provider_event_id="evt.notify.book.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=FakeCalendarBookingPort(
                create_result=CalendarBookingEvent(
                    event_id="evt_notify_book_1",
                    meet_link="https://meet.google.com/secret-link",
                )
            ),
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert result.kind == BookingResultKind.BOOKED
        rows = _notify_rows(db, lead_id=lead_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.scheduled_at
        assert row.seen_at == ""
        assert "meet.google.com" not in row.scheduled_at
        assert "meet.google.com" not in row.lead_id
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


def test_second_book_same_lead_keeps_first_notify_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.repeat@ex.com"
        )
        slot = _slot(4, 11)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        booking = FakeCalendarBookingPort()
        calendar = FakeCalendarPort([slot])
        first = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="notify.repeat@ex.com",
            inbound_provider_event_id="evt.notify.rep.1",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert first.kind == BookingResultKind.BOOKED
        rows_after_first = _notify_rows(db, lead_id=lead_id)
        assert len(rows_after_first) == 1
        first_scheduled = rows_after_first[0].scheduled_at
        second = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="notify.repeat@ex.com",
            inbound_provider_event_id="evt.notify.rep.2",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert second.kind == BookingResultKind.ALREADY_BOOKED
        rows_after_second = _notify_rows(db, lead_id=lead_id)
        assert len(rows_after_second) == 1
        assert rows_after_second[0].scheduled_at == first_scheduled
        assert rows_after_second[0].seen_at == ""
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


@pytest.mark.parametrize(
    "text",
    [
        "booked meetings",
        "what got booked",
        "meeting notifications",
        "מה נקבע",
        "פגישות שנקבעו",
        "התראות פגישות",
    ],
)
def test_classify_owner_notify_phrases(text: str) -> None:
    decision = classify_owner_task(text)
    assert decision.task_type == OwnerTaskType.OWNER_NOTIFY
    assert decision.needs_clarification is False
    assert decision.matched_types == ["owner_notify"]


def test_classify_calendar_not_owner_notify() -> None:
    decision = classify_owner_task("check my calendar")
    assert decision.task_type == OwnerTaskType.CALENDAR


def test_classify_preference_without_notify_phrases() -> None:
    decision = classify_owner_task("from now on remember my style")
    assert decision.task_type == OwnerTaskType.PREFERENCE


def test_apply_owner_notify_marks_seen_then_empty() -> None:
    init_db()
    db = get_session_factory()()
    try:
        _clear_unseen_notifications(db)
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.apply@ex.com"
        )
        slot = _slot(4, 12)
        scheduled = slot.start.isoformat()
        store.upsert_owner_notification(
            kind=KIND_MEETING_BOOKED,
            lead_id=lead_id,
            scheduled_at=scheduled,
        )
        db.commit()
        first = apply_owner_notify(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=False,
            demo_active=False,
            now=FIXED_NOW,
        )
        assert first is not None
        assert lead_id in first
        assert "מועד:" in first
        assert "https://" not in first
        assert "Absolutely!" not in first
        assert "—" not in first
        db.commit()
        rows = _notify_rows(db, lead_id=lead_id)
        assert len(rows) == 1
        assert rows[0].seen_at
        second = apply_owner_notify(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=False,
            demo_active=False,
            now=FIXED_NOW,
        )
        assert second == "אין התראות פגישות חדשות."
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


def test_apply_owner_notify_demo_returns_none() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.demo@ex.com"
        )
        store.upsert_owner_notification(
            kind=KIND_MEETING_BOOKED,
            lead_id=lead_id,
            scheduled_at=_slot(4, 13).start.isoformat(),
        )
        db.commit()
        assert (
            apply_owner_notify(
                store,
                timezone="Asia/Jerusalem",
                kill_switch=False,
                demo_active=True,
            )
            is None
        )
        persist_meeting_booked_owner_notify(
            store,
            lead_id=lead_id,
            scheduled_at=_slot(5, 9).start.isoformat(),
            kill_switch=False,
            demo_active=True,
        )
        db.commit()
        rows = _notify_rows(db, lead_id=lead_id)
        assert len(rows) == 1
        assert rows[0].seen_at == ""
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


def test_apply_owner_notify_kill_switch_format_only() -> None:
    init_db()
    db = get_session_factory()()
    try:
        _clear_unseen_notifications(db)
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.ks@ex.com"
        )
        store.upsert_owner_notification(
            kind=KIND_MEETING_BOOKED,
            lead_id=lead_id,
            scheduled_at=_slot(4, 14).start.isoformat(),
        )
        db.commit()
        text = apply_owner_notify(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=True,
            demo_active=False,
            now=FIXED_NOW,
        )
        assert text is not None
        assert lead_id in text
        db.commit()
        rows = _notify_rows(db, lead_id=lead_id)
        assert rows[0].seen_at == ""
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_notify_after_booking() -> None:
    init_db()
    db = get_session_factory()()
    try:
        _clear_unseen_notifications(db)
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.inbound@ex.com"
        )
        slot = _slot(4, 15)
        _seed_offered(store, lead_id, [slot])
        db.commit()
        booked = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="notify.inbound@ex.com",
            inbound_provider_event_id="evt.notify.inbound.book",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert booked.kind == BookingResultKind.BOOKED
        db.commit()
        port = RecordingMessagePort()
        result = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": OWNER_EVENT,
                "from": OWNER_PHONE,
                "text": "מה נקבע",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
        )
        db.commit()
        assert result["processed"] == 1
        assert len(port.sent) == 1
        reply = port.sent[0].text
        assert lead_id in reply
        assert "מועד:" in reply
        rows = _notify_rows(db, lead_id=lead_id)
        assert rows[0].seen_at
    finally:
        _delete_test_rows(db, event_ids=(OWNER_EVENT,))
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_notify_kill_switch() -> None:
    init_db()
    db = get_session_factory()()
    try:
        _clear_unseen_notifications(db)
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.inbound.ks@ex.com"
        )
        store.upsert_owner_notification(
            kind=KIND_MEETING_BOOKED,
            lead_id=lead_id,
            scheduled_at=_slot(4, 16).start.isoformat(),
        )
        db.commit()
        port = RecordingMessagePort()
        with pytest.raises(PolicyDenied):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{
                    "id": OWNER_EVENT_KILL,
                    "from": OWNER_PHONE,
                    "text": "booked meetings",
                }],
                store=store,
                port=port,
                kill_switch=True,
                owner_ids={OWNER_PHONE},
            )
        db.commit()
        assert len(port.sent) == 0
        rows = _notify_rows(db, lead_id=lead_id)
        assert rows[0].seen_at == ""
    finally:
        _delete_test_rows(db, event_ids=(OWNER_EVENT_KILL,))
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


def test_owner_notify_module_no_forbidden_imports() -> None:
    module = importlib.import_module("app.domain.owner.notifications")
    source = inspect.getsource(module)
    assert "httpx" not in source
    assert "openai" not in source.lower()
    assert "MessagePort" not in source


def test_require_alive_owner_notify() -> None:
    require_alive(CapabilityId.OWNER_NOTIFY)


def test_persist_skips_kill_switch_and_demo() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.skip@ex.com"
        )
        scheduled = _slot(4, 17).start.isoformat()
        persist_meeting_booked_owner_notify(
            store,
            lead_id=lead_id,
            scheduled_at=scheduled,
            kill_switch=True,
            demo_active=False,
        )
        persist_meeting_booked_owner_notify(
            store,
            lead_id=f"{lead_id}_demo",
            scheduled_at=scheduled,
            kill_switch=False,
            demo_active=True,
        )
        db.commit()
        assert _notify_rows(db, lead_id=lead_id) == []
        assert _notify_rows(db, lead_id=f"{lead_id}_demo") == []
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id, f"{lead_id}_demo"))
        db.close()


def test_reschedule_success_persists_unseen_notify() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="notify.reschedule@ex.com",
            target=target,
        )
        booking = FakeCalendarBookingPort(
            events_by_id={
                event_id: CalendarBookingEvent(
                    event_id=event_id,
                    meet_link="https://meet.google.com/secret-link",
                    start=_slot(4, 9).start,
                    end=_slot(4, 9).end,
                )
            }
        )
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.reschedule@ex.com",
            message="1",
            calendar=FakeCalendarPort([target]),
            booking_port=booking,
            kill_switch=False,
            demo_active=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert result.kind == MeetingChangeKind.RESCHEDULED
        rows = _notify_rows(db, lead_id=lead_id, kind=KIND_MEETING_RESCHEDULED)
        assert len(rows) == 1
        assert rows[0].seen_at == ""
        assert rows[0].scheduled_at == target.start.isoformat()
        assert "meet.google.com" not in rows[0].scheduled_at
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


def test_second_reschedule_same_lead_keeps_first_notify_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        first_target = _slot(4, 11)
        second_target = _slot(4, 14)
        lead_id, event_id = _prepare_reschedule(
            store,
            external_id="notify.reschedule.twice@ex.com",
            target=first_target,
        )
        original_slot = _slot(4, 9)
        booking = FakeCalendarBookingPort(
            events_by_id={
                event_id: CalendarBookingEvent(
                    event_id=event_id,
                    meet_link="https://meet.google.com/abc",
                    start=original_slot.start,
                    end=original_slot.end,
                )
            }
        )
        calendar = FakeCalendarPort([first_target, second_target])
        first = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.reschedule.twice@ex.com",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            demo_active=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert first.kind == MeetingChangeKind.RESCHEDULED
        rows_after_first = _notify_rows(
            db, lead_id=lead_id, kind=KIND_MEETING_RESCHEDULED
        )
        assert len(rows_after_first) == 1
        first_scheduled = rows_after_first[0].scheduled_at
        assert store.save_reschedule_slots(
            lead_id=lead_id,
            slots=[second_target],
            now=FIXED_NOW,
            timezone="Asia/Jerusalem",
        )
        booking.events_by_id[event_id] = CalendarBookingEvent(
            event_id=event_id,
            meet_link="https://meet.google.com/abc",
            start=first_target.start,
            end=first_target.end,
        )
        second = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.reschedule.twice@ex.com",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            demo_active=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW + timedelta(hours=1),
        )
        db.commit()
        assert second.kind == MeetingChangeKind.RESCHEDULED
        rows_after_second = _notify_rows(
            db, lead_id=lead_id, kind=KIND_MEETING_RESCHEDULED
        )
        assert len(rows_after_second) == 1
        assert rows_after_second[0].scheduled_at == first_scheduled
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


def test_first_cancellation_request_persists_notify() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        slot = _slot(4, 10)
        lead_id = _seed_booked(
            store, external_id="notify.cancel@ex.com", slot=slot
        )
        first = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.cancel@ex.com",
            message="cancel my meeting",
            calendar=FakeCalendarPort([]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=False,
            demo_active=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        second = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.cancel@ex.com",
            message="cancel my meeting",
            calendar=FakeCalendarPort([]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=False,
            demo_active=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW + timedelta(minutes=1),
        )
        db.commit()
        assert first.kind == MeetingChangeKind.CANCELLATION_REQUESTED
        assert second.kind == MeetingChangeKind.CANCELLATION_REQUESTED
        rows = _notify_rows(
            db, lead_id=lead_id, kind=KIND_MEETING_CANCELLATION_REQUESTED
        )
        assert len(rows) == 1
        assert rows[0].seen_at == ""
        assert rows[0].scheduled_at == slot.start.isoformat()
    finally:
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()


def test_reschedule_and_cancel_skip_persist_on_demo_and_kill_switch() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id_demo, event_id_demo = _prepare_reschedule(
            store,
            external_id="notify.skip.demo@ex.com",
            target=target,
        )
        lead_id_ks, event_id_ks = _prepare_reschedule(
            store,
            external_id="notify.skip.ks@ex.com",
            target=target,
        )
        lead_id_cancel_demo = _seed_booked(
            store, external_id="notify.skip.cancel.demo@ex.com"
        )
        lead_id_cancel_ks = _seed_booked(
            store, external_id="notify.skip.cancel.ks@ex.com"
        )
        booking_demo = FakeCalendarBookingPort(
            events_by_id={
                event_id_demo: CalendarBookingEvent(
                    event_id=event_id_demo,
                    meet_link="https://meet.google.com/x",
                    start=_slot(4, 9).start,
                    end=_slot(4, 9).end,
                )
            }
        )
        booking_ks = FakeCalendarBookingPort(
            events_by_id={
                event_id_ks: CalendarBookingEvent(
                    event_id=event_id_ks,
                    meet_link="https://meet.google.com/y",
                    start=_slot(4, 9).start,
                    end=_slot(4, 9).end,
                )
            }
        )
        calendar = FakeCalendarPort([target])
        resolve_booked_meeting_change(
            store,
            lead_id=lead_id_demo,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.skip.demo@ex.com",
            message="1",
            calendar=calendar,
            booking_port=booking_demo,
            kill_switch=False,
            demo_active=True,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        resolve_booked_meeting_change(
            store,
            lead_id=lead_id_ks,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.skip.ks@ex.com",
            message="1",
            calendar=calendar,
            booking_port=booking_ks,
            kill_switch=True,
            demo_active=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        resolve_booked_meeting_change(
            store,
            lead_id=lead_id_cancel_demo,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.skip.cancel.demo@ex.com",
            message="cancel meeting",
            calendar=FakeCalendarPort([]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=False,
            demo_active=True,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        resolve_booked_meeting_change(
            store,
            lead_id=lead_id_cancel_ks,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.skip.cancel.ks@ex.com",
            message="cancel meeting",
            calendar=FakeCalendarPort([]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=True,
            demo_active=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert _notify_rows(
            db, lead_id=lead_id_demo, kind=KIND_MEETING_RESCHEDULED
        ) == []
        assert _notify_rows(
            db, lead_id=lead_id_ks, kind=KIND_MEETING_RESCHEDULED
        ) == []
        assert _notify_rows(
            db,
            lead_id=lead_id_cancel_demo,
            kind=KIND_MEETING_CANCELLATION_REQUESTED,
        ) == []
        assert _notify_rows(
            db,
            lead_id=lead_id_cancel_ks,
            kind=KIND_MEETING_CANCELLATION_REQUESTED,
        ) == []
    finally:
        _delete_notify_rows(
            db,
            lead_ids=(
                lead_id_demo,
                lead_id_ks,
                lead_id_cancel_demo,
                lead_id_cancel_ks,
            ),
        )
        db.close()


def test_apply_owner_notify_mixed_kinds_and_extra_line() -> None:
    init_db()
    db = get_session_factory()()
    try:
        _clear_unseen_notifications(db)
        store = LeadStore(db)
        lead_ids: list[str] = []
        kinds = (
            KIND_MEETING_BOOKED,
            KIND_MEETING_RESCHEDULED,
            KIND_MEETING_CANCELLATION_REQUESTED,
        )
        for index, kind in enumerate(kinds):
            _, lead_id = store.open_channel_lead(
                channel=Channel.GMAIL,
                external_id=f"notify.mixed.{index}@ex.com",
            )
            lead_ids.append(lead_id)
            store.upsert_owner_notification(
                kind=kind,
                lead_id=lead_id,
                scheduled_at=_slot(4 + index, 10).start.isoformat(),
            )
        _, extra_lead = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="notify.mixed.extra@ex.com"
        )
        lead_ids.append(extra_lead)
        store.upsert_owner_notification(
            kind=KIND_MEETING_BOOKED,
            lead_id=extra_lead,
            scheduled_at=_slot(8, 10).start.isoformat(),
        )
        db.commit()
        text = apply_owner_notify(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=False,
            demo_active=False,
            now=FIXED_NOW,
        )
        assert text is not None
        assert "נקבעה פגישה." in text
        assert "פגישה עודכנה." in text
        assert "בקשת ביטול." in text
        assert "עוד 1 התראות." in text
        db.commit()
        for lead_id in lead_ids[:3]:
            rows = list(
                db.scalars(
                    select(OwnerNotificationRow).where(
                        OwnerNotificationRow.lead_id == lead_id
                    )
                ).all()
            )
            assert len(rows) == 1
            assert rows[0].seen_at
        extra_rows = _notify_rows(db, lead_id=extra_lead)
        assert len(extra_rows) == 1
        assert extra_rows[0].seen_at == ""
    finally:
        _delete_notify_rows(db, lead_ids=tuple(lead_ids))
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_notify_after_cancellation() -> None:
    init_db()
    db = get_session_factory()()
    owner_event = "evt.owner.notify.cancel.inbound"
    try:
        _clear_unseen_notifications(db)
        store = LeadStore(db)
        slot = _slot(4, 15)
        lead_id = _seed_booked(
            store, external_id="notify.inbound.cancel@ex.com", slot=slot
        )
        db.commit()
        cancel = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="notify.inbound.cancel@ex.com",
            message="cancel my meeting",
            calendar=FakeCalendarPort([]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=False,
            demo_active=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert cancel.kind == MeetingChangeKind.CANCELLATION_REQUESTED
        db.commit()
        port = RecordingMessagePort()
        result = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": owner_event,
                "from": OWNER_PHONE,
                "text": "מה נקבע",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
        )
        db.commit()
        assert result["processed"] == 1
        assert len(port.sent) == 1
        reply = port.sent[0].text
        assert "בקשת ביטול." in reply
        assert lead_id in reply
        rows = _notify_rows(
            db, lead_id=lead_id, kind=KIND_MEETING_CANCELLATION_REQUESTED
        )
        assert rows[0].seen_at
    finally:
        _delete_test_rows(db, event_ids=(owner_event,))
        _delete_notify_rows(db, lead_ids=(lead_id,))
        db.close()
