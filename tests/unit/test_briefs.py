import importlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.api.inbound import process_inbound_texts
from app.db.models import CanonicalEventRow, MeetingBriefRow, OwnerTaskRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.briefs import (
    apply_meeting_brief_policy,
    apply_owner_meeting_brief,
    persist_booked_meeting_brief,
)
from app.domain.calendar_booking import BookingResultKind, attempt_meeting_booking
from app.domain.events import Channel, EventType
from app.domain.meeting_changes import MeetingChangeKind, resolve_booked_meeting_change
from app.domain.meetings import apply_meeting_policy
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort, FakeCalendarPort, TimeSlot
from app.integrations.calendar_booking import CalendarBookingEvent, FakeCalendarBookingPort
from app.integrations.sheets import DisabledSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

PROSPECT_PHONE = "972509994011"
PROSPECT_PHONE_2 = "972509994012"
OWNER_PHONE = "972509998421"
IL = ZoneInfo("Asia/Jerusalem")
FIXED_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)

_MEETING_BRIEF_PAYLOAD_KEYS = frozenset({
    "channel",
    "fit",
    "pain_level",
    "workflow_known",
    "impact_confirmed",
    "reflected",
    "hypothesis_offered",
    "buying_reality_known",
    "authority_known",
    "timeline_known",
    "metric_known",
    "willingness_to_meet",
    "owner_required",
    "active_objection",
    "missing_fields",
    "owner_questions",
    "next_action",
})


def test_website_identify_then_sell_does_not_create_meeting_brief() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        clinic = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        assert clinic.status_code == 200
        assert clinic.json()["next_action"] == "ask_contact"
        meeting = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "let's book a meeting", "phone": "0501234567"},
        )
        assert meeting.json()["next_action"] == "handoff"
        again = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "let's book a meeting"},
        )
        assert again.json()["next_action"] == "handoff"
        assert again.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        assert db.scalars(select(MeetingBriefRow)).all() == []
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type == EventType.MEETING_BRIEF.value
                )
            )
        )
        assert events == []
    finally:
        db.close()


def test_student_disqualify_no_brief() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "I'm a student with a school project"},
        )
        assert response.status_code == 200
        assert response.json()["next_action"] == "ask_contact"
        assert response.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        assert db.scalars(select(MeetingBriefRow)).all() == []
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type == EventType.MEETING_BRIEF.value,
                )
            )
        )
        assert events == []
    finally:
        db.close()


def test_kill_switch_skips_brief_create(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_kill_brief_1"
        )
        sales = SalesState(
            lead_id=lead_id,
            fit=FitLevel.GOOD,
            workflow_known=True,
            impact_confirmed=True,
            reflected=True,
            hypothesis_offered=True,
            buying_reality_known=True,
            willingness_to_meet=True,
        )
        store.save_sales(sales)
        db.commit()
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=True,
        )
        db.commit()
        assert store.get_meeting_brief(lead_id) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_prospect_offer_meeting_creates_brief() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        messages = [
            "We run a clinic and miss calls all day.",
            "ok that's right",
            "I decide this quarter",
            "let's book a meeting",
        ]
        lead_id = ""
        for index, text in enumerate(messages):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{"id": f"wamid.brief.{index}", "from": PROSPECT_PHONE_2, "text": text}],
                store=store,
                port=port,
                kill_switch=False,
                calendar=DisabledCalendarPort(),
                sheets=DisabledSheetsPort(),
            )
            db.commit()
            _, lead_id = store.open_channel_lead(
                channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE_2
            )
        row = store.get_meeting_brief(lead_id)
        assert row is not None
        assert row.channel == Channel.WHATSAPP.value
        payload = json.loads(row.payload_json)
        assert payload["channel"] == Channel.WHATSAPP.value
    finally:
        db.close()


def test_apply_meeting_brief_policy_never_calls_message_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
        )
        sales = SalesState(
            lead_id=lead_id,
            fit=FitLevel.GOOD,
            workflow_known=True,
            impact_confirmed=True,
            reflected=True,
            hypothesis_offered=True,
            buying_reality_known=True,
            willingness_to_meet=True,
        )
        store.save_sales(sales)
        port = RecordingMessagePort()
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WHATSAPP,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
        )
        db.commit()
        assert port.sent == []
        assert store.get_meeting_brief(lead_id) is not None
    finally:
        db.close()


def _local_dt(*, days_ahead: int, hour: int, minute: int = 0) -> datetime:
    local_now = FIXED_NOW.astimezone(IL)
    local_start = (local_now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return local_start.astimezone(UTC)


def _slot(days_ahead: int, hour: int, minute: int = 0) -> TimeSlot:
    start = _local_dt(days_ahead=days_ahead, hour=hour, minute=minute)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


def _ready_sales(lead_id: str) -> SalesState:
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
        missing_fields=[],
    )


def _seed_offered_with_brief(store: LeadStore, lead_id: str, slots: list[TimeSlot]) -> None:
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
    apply_meeting_brief_policy(
        store,
        lead_id=lead_id,
        channel=Channel.GMAIL,
        action=NextAction.OFFER_MEETING.value,
        sales=_ready_sales(lead_id),
        kill_switch=False,
    )


def _delete_owner_tasks(db, *, event_ids: tuple[str, ...]) -> None:
    for event_id in event_ids:
        db.execute(
            delete(OwnerTaskRow).where(
                OwnerTaskRow.provider == "whatsapp",
                OwnerTaskRow.provider_event_id == event_id,
            )
        )
    db.commit()


def test_booking_stamps_brief_without_new_canonical_event() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="brief.booked@ex.com"
        )
        slot = _slot(4, 10)
        _seed_offered_with_brief(store, lead_id, [slot])
        db.commit()
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="brief.booked@ex.com",
            inbound_provider_event_id="evt.brief.booked.1",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=FakeCalendarBookingPort(
                create_result=CalendarBookingEvent(
                    event_id="evt_brief_booked_1",
                    meet_link="https://meet.google.com/secret-link",
                )
            ),
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert result.kind == BookingResultKind.BOOKED
        row = store.get_meeting_brief(lead_id)
        assert row is not None
        payload = json.loads(row.payload_json)
        assert payload["meeting_status"] == "booked"
        assert payload["scheduled_at"]
        assert "meet.google.com" not in row.payload_json.lower()
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.MEETING_BRIEF.value,
                )
            )
        )
        assert len(events) == 1
        assert events[0].provider_event_id == f"{lead_id}:brief:offer_meeting"
        event_payload = json.loads(events[0].payload_json)
        assert "scheduled_at" not in event_payload
        assert "meet_link" not in event_payload
        assert "meeting_status" not in event_payload
    finally:
        db.close()


def test_stamp_skips_kill_switch_and_demo() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="brief.skip@ex.com"
        )
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            action=NextAction.OFFER_MEETING.value,
            sales=_ready_sales(lead_id),
            kill_switch=False,
        )
        scheduled = _slot(4, 11).start.isoformat()
        persist_booked_meeting_brief(
            store,
            lead_id=lead_id,
            scheduled_at=scheduled,
            kill_switch=True,
            demo_active=False,
        )
        persist_booked_meeting_brief(
            store,
            lead_id=f"{lead_id}_demo",
            scheduled_at=scheduled,
            kill_switch=False,
            demo_active=True,
        )
        db.commit()
        payload = json.loads(store.get_meeting_brief(lead_id).payload_json)
        assert "meeting_status" not in payload
        assert "scheduled_at" not in payload
    finally:
        db.close()


def test_offer_meeting_upsert_preserves_booked_stamp() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="brief.preserve@ex.com"
        )
        sales = _ready_sales(lead_id)
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
        )
        scheduled = _slot(4, 11).start.isoformat()
        persist_booked_meeting_brief(
            store,
            lead_id=lead_id,
            scheduled_at=scheduled,
            kill_switch=False,
            demo_active=False,
        )
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
        )
        db.commit()
        payload = json.loads(store.get_meeting_brief(lead_id).payload_json)
        assert payload["meeting_status"] == "booked"
        assert payload["scheduled_at"]
        assert "meet_link" not in payload
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.MEETING_BRIEF.value,
                )
            )
        )
        assert len(events) == 1
        assert "scheduled_at" not in json.loads(events[0].payload_json)
    finally:
        db.close()


@pytest.mark.parametrize(
    "text",
    [
        "meeting brief",
        "pre-meeting brief",
        "pre meeting brief",
        "תקציר פגישה",
        "בריף פגישה",
    ],
)
def test_classify_meeting_brief_phrases(text: str) -> None:
    decision = classify_owner_task(text)
    assert decision.task_type == OwnerTaskType.MEETING_BRIEF
    assert decision.needs_clarification is True
    assert decision.matched_types == ["meeting_brief"]


def test_classify_meeting_debrief_not_meeting_brief() -> None:
    decision = classify_owner_task("סיכום פגישה lead_abc123456789")
    assert decision.task_type == OwnerTaskType.MEETING_DEBRIEF


def test_classify_owner_notify_not_meeting_brief() -> None:
    decision = classify_owner_task("booked meetings")
    assert decision.task_type == OwnerTaskType.OWNER_NOTIFY


def test_classify_lead_review_not_meeting_brief() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509998422"
        )
        db.commit()
        decision = classify_owner_task(f"review {lead_id}")
        assert decision.task_type == OwnerTaskType.LEAD_REVIEW
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_meeting_brief_after_booking() -> None:
    init_db()
    db = get_session_factory()()
    owner_event = "evt.brief.booked.owner.1"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="brief.owner@ex.com"
        )
        slot = _slot(4, 12)
        _seed_offered_with_brief(store, lead_id, [slot])
        db.commit()
        attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="brief.owner@ex.com",
            inbound_provider_event_id="evt.brief.booked.2",
            message="1",
            calendar=FakeCalendarPort([slot]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": owner_event,
                "from": OWNER_PHONE,
                "text": f"meeting brief {lead_id}",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
        )
        db.commit()
        assert len(port.sent) == 1
        reply = port.sent[0].text
        assert lead_id in reply
        assert "מועד:" in reply
        assert "meet.google.com" not in reply
        task = db.scalars(
            select(OwnerTaskRow).where(
                OwnerTaskRow.provider_event_id == owner_event,
            )
        ).one()
        assert task.task_type == "meeting_brief"
        assert task.due_at is None
    finally:
        _delete_owner_tasks(db, event_ids=(owner_event,))
        db.close()


def test_owner_inbound_meeting_brief_without_lead_id_needs_clarification() -> None:
    decision = classify_owner_task("תקציר פגישה")
    assert decision.task_type == OwnerTaskType.MEETING_BRIEF
    assert decision.needs_clarification is True
    ack = ack_for_owner_task(decision)
    assert "מה מזהה הליד" in ack


def test_reschedule_updates_brief_scheduled_at() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id="brief.resched@ex.com"
        )
        original = _slot(4, 9)
        target = _slot(4, 11)
        _seed_offered_with_brief(store, lead_id, [original])
        db.commit()
        attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id="brief.resched@ex.com",
            inbound_provider_event_id="evt.brief.booked.3",
            message="1",
            calendar=FakeCalendarPort([original]),
            booking_port=FakeCalendarBookingPort(),
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        before = json.loads(store.get_meeting_brief(lead_id).payload_json)
        first_at = before["scheduled_at"]
        assert store.save_reschedule_slots(
            lead_id=lead_id,
            slots=[target],
            now=FIXED_NOW,
            timezone="Asia/Jerusalem",
        )
        meeting = store.get_meeting(lead_id)
        assert meeting is not None
        event_id = meeting.calendar_event_id
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: CalendarBookingEvent(
                event_id=event_id,
                meet_link="https://meet.google.com/abc-defg-hij",
                start=target.start,
                end=target.end,
            )}
        )
        result = resolve_booked_meeting_change(
            store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="thread-brief-resched",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        db.commit()
        assert result.kind == MeetingChangeKind.RESCHEDULED
        after = json.loads(store.get_meeting_brief(lead_id).payload_json)
        assert after["meeting_status"] == "booked"
        assert after["scheduled_at"] != first_at
        assert after["scheduled_at"]
    finally:
        db.close()


def test_apply_owner_meeting_brief_unknown_lead() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        text = apply_owner_meeting_brief(
            store,
            text="meeting brief lead_deadbeefdead",
            timezone="Asia/Jerusalem",
            kill_switch=False,
            demo_active=False,
        )
        assert text is not None
        assert "לא מצאתי תקציר" in text
    finally:
        db.close()


def test_briefs_module_no_forbidden_imports() -> None:
    module = importlib.import_module("app.domain.briefs")
    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "MetaAdsPort" not in source
