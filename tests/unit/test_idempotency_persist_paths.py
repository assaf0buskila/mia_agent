"""Adjustment E: duplicate persist paths write one SoR row each."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.db.models import (
    ApprovalRow,
    CanonicalEventRow,
    FollowUpRow,
    IdempotencyRow,
    OwnerTaskRow,
    WebhookEventRow,
)
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import apply_approval_policy
from app.domain.calendar_booking import (
    BookingResultKind,
    _persist_meeting_booked_event,
    attempt_meeting_booking,
)
from app.domain.events import (
    Channel,
    EventType,
    build_message_in_event,
    build_message_out_event,
)
from app.domain.followups import apply_follow_up_policy
from app.domain.idempotency import ALLOWLISTED_OPERATION_SCOPES
from app.domain.meeting_changes import (
    CANCELLATION_REQUESTED_REPLY,
    MeetingChangeKind,
    resolve_booked_meeting_change,
)
from app.domain.meeting_slots import compute_booking_key
from app.domain.meetings import STATUS_BOOKED, STATUS_CANCELLATION_REQUESTED, apply_meeting_policy
from app.domain.sales import FitLevel, NextAction, SalesState
from app.integrations.calendar import FakeCalendarPort, TimeSlot
from app.integrations.calendar_booking import CalendarBookingEvent, FakeCalendarBookingPort
from app.integrations.sheets import (
    ContentMirrorRow,
    FakeSheetsPort,
    LeadMirrorRow,
    claim_sheets_mirror,
    complete_sheets_mirror,
    mirror_content,
    mirror_lead,
)
from sqlalchemy import func, select

IL = ZoneInfo("Asia/Jerusalem")
FIXED_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)

LIVE_CLAIM_SCOPES = frozenset({
    "calendar_create",
    "calendar_reschedule",
    "approval",
    "owner_task",
    "sheets_mirror",
    "follow_up",
    "calendar_cancellation",
})


def _slot(days_ahead: int, hour: int, minute: int = 0) -> TimeSlot:
    local_now = FIXED_NOW.astimezone(IL)
    local_start = (local_now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    start = local_start.astimezone(UTC)
    return TimeSlot(start=start, end=start + timedelta(minutes=30))


def _seed_offered(store: LeadStore, lead_id: str, slots: list[TimeSlot]) -> None:
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


def _seed_booked(store: LeadStore, *, external_id: str) -> str:
    _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id=external_id)
    apply_meeting_policy(
        store,
        lead_id=lead_id,
        channel=Channel.GMAIL,
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


def _provider_event(event_id: str, slot: TimeSlot) -> CalendarBookingEvent:
    return CalendarBookingEvent(
        event_id=event_id,
        meet_link="https://meet.google.com/abc-defg-hij",
        start=slot.start,
        end=slot.end,
    )


def _sample_mirror_row(*, lead_id: str) -> LeadMirrorRow:
    return LeadMirrorRow(
        lead_id=lead_id,
        channel="gmail",
        stage="open",
        fit="unknown",
        pain_level=0,
        next_action="understand_workflow",
    )


class _CountingSheetsPort(FakeSheetsPort):
    def __init__(self) -> None:
        super().__init__()
        self.lead_upserts = 0

    def upsert_lead(self, row: LeadMirrorRow) -> None:
        self.lead_upserts += 1
        super().upsert_lead(row)


class _CountingContentSheetsPort(FakeSheetsPort):
    def __init__(self) -> None:
        super().__init__()
        self.content_upserts = 0

    def upsert_content(self, row: ContentMirrorRow) -> None:
        self.content_upserts += 1
        super().upsert_content(row)


def _good_willing_sales(lead_id: str) -> SalesState:
    return SalesState(
        lead_id=lead_id,
        fit=FitLevel.GOOD,
        workflow_known=True,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        buying_reality_known=True,
        willingness_to_meet=True,
    )


def test_live_claim_scopes_are_allowlisted() -> None:
    assert LIVE_CLAIM_SCOPES <= ALLOWLISTED_OPERATION_SCOPES
    assert "canonical" in ALLOWLISTED_OPERATION_SCOPES


def test_webhook_duplicate_claim_writes_one_row() -> None:
    init_db()
    db = get_session_factory()()
    provider = "whatsapp"
    provider_event_id = "xcut.wh.1"
    try:
        store = LeadStore(db)
        assert store.claim_webhook(provider=provider, provider_event_id=provider_event_id) is True
        assert store.claim_webhook(provider=provider, provider_event_id=provider_event_id) is False
        db.commit()
        row = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == provider,
                WebhookEventRow.provider_event_id == provider_event_id,
            )
        ).one()
        assert row.status == "received"
    finally:
        db.close()


def test_canonical_duplicate_message_in_one_row() -> None:
    init_db()
    db = get_session_factory()()
    provider_event_id = "xcut.msg.in.1"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="xcut.msg.web.1"
        )
        event = build_message_in_event(
            provider="website",
            channel=Channel.WEBSITE,
            provider_event_id=provider_event_id,
            conversation_id="xcut.msg.web.1",
            text="hello",
            actor_role="prospect",
            lead_id=lead_id,
        )
        for _ in range(2):
            store.save_canonical_event(provider="website", event=event)
        db.commit()
        row = store.get_canonical_event(
            provider="website", provider_event_id=provider_event_id
        )
        assert row is not None
        assert row.lead_id == lead_id
        assert row.event_type == EventType.MESSAGE_IN.value
    finally:
        db.close()


def test_outbound_duplicate_message_out_one_row() -> None:
    init_db()
    db = get_session_factory()()
    inbound_id = "xcut.out.1"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509990802"
        )
        event = build_message_out_event(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            inbound_provider_event_id=inbound_id,
            conversation_id="972509990802",
            text="reply text",
            lead_id=lead_id,
        )
        assert event.idempotency_key == f"{inbound_id}:out"
        for _ in range(2):
            store.save_canonical_event(provider="whatsapp", event=event)
        db.commit()
        row = store.get_canonical_event(
            provider="whatsapp", provider_event_id=f"{inbound_id}:out"
        )
        assert row is not None
        assert row.event_type == EventType.MESSAGE_OUT.value
    finally:
        db.close()


def test_calendar_booked_persist_duplicate_one_canonical() -> None:
    init_db()
    db = get_session_factory()()
    scheduled_at = "2026-09-01T10:00:00+00:00"
    external_id = "xcut.cal.book@example.com"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=external_id
        )
        db.commit()
        for _ in range(2):
            _persist_meeting_booked_event(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                lead_id=lead_id,
                conversation_id=external_id,
                scheduled_at=scheduled_at,
            )
        db.commit()
        booked_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.MEETING_BOOKED.value,
                )
            ).all()
        )
        assert len(booked_rows) == 1
        idem_row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == "calendar_create",
                IdempotencyRow.key == f"{lead_id}:booked",
            )
        ).one()
        assert idem_row.status == "completed"
    finally:
        db.close()


def test_calendar_existing_booking_key_skips_create() -> None:
    init_db()
    db = get_session_factory()()
    external_id = "xcut.cal.key@example.com"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id=external_id)
        slot = _slot(4, 10)
        _seed_offered(store, lead_id, [slot])
        key = compute_booking_key(lead_id=lead_id, start=slot.start, end=slot.end)
        existing = CalendarBookingEvent(
            event_id="xcut.evt.recovered",
            meet_link="https://meet.google.com/abc-defg-hij",
        )
        booking = FakeCalendarBookingPort(existing={key: existing})
        calendar = FakeCalendarPort([])
        kwargs = dict(
            store=store,
            lead_id=lead_id,
            channel=Channel.GMAIL,
            provider="gmail",
            conversation_id=external_id,
            inbound_provider_event_id="xcut.cal.key.in.1",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        first = attempt_meeting_booking(**kwargs)
        second = attempt_meeting_booking(**kwargs)
        db.commit()
        assert first.kind == BookingResultKind.BOOKED
        assert second.kind == BookingResultKind.ALREADY_BOOKED
        assert booking.create_calls == []
        row = store.get_meeting(lead_id)
        assert row is not None
        assert row.status == STATUS_BOOKED
    finally:
        db.close()


def test_reschedule_duplicate_target_one_canonical() -> None:
    init_db()
    db = get_session_factory()()
    external_id = "xcut.rsvp@example.com"
    try:
        store = LeadStore(db)
        target = _slot(4, 11)
        lead_id, event_id = _prepare_reschedule(
            store, external_id=external_id, target=target
        )
        target_key = compute_booking_key(
            lead_id=lead_id, start=target.start, end=target.end
        )
        claim_key = f"{lead_id}:rescheduled:{target_key}"
        booking = FakeCalendarBookingPort(
            events_by_id={event_id: _provider_event(event_id, target)}
        )
        kwargs = dict(
            store=store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="xcut.rsvp.thread",
            message="1",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        first = resolve_booked_meeting_change(**kwargs)
        second = resolve_booked_meeting_change(**kwargs)
        db.commit()
        assert first.kind == MeetingChangeKind.RESCHEDULED
        assert first.changed is True
        assert second.kind in {
            MeetingChangeKind.RESCHEDULED,
            MeetingChangeKind.NOT_HANDLED,
        }
        if second.kind == MeetingChangeKind.RESCHEDULED:
            assert second.changed is False
        assert booking.patch_calls == []
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.MEETING_RESCHEDULED.value,
                )
            ).all()
        )
        assert len(rows) == 1
        idem_row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == "calendar_reschedule",
                IdempotencyRow.key == claim_key,
            )
        ).one()
        assert idem_row.status == "completed"
    finally:
        db.close()


def test_approval_duplicate_handoff_one_canonical() -> None:
    init_db()
    db = get_session_factory()()
    external_id = "xcut.appr.web.1"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=external_id
        )
        sales = SalesState(
            lead_id=lead_id,
            workflow_known=True,
            owner_required=True,
        )
        store.save_sales(sales)
        for _ in range(2):
            apply_approval_policy(
                store,
                lead_id=lead_id,
                channel=Channel.WEBSITE,
                action=NextAction.HANDOFF.value,
                sales=sales,
                kill_switch=False,
            )
        db.commit()
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.APPROVAL_REQUIRED.value,
                )
            ).all()
        )
        assert len(events) == 1
        approval = db.scalars(
            select(ApprovalRow).where(ApprovalRow.lead_id == lead_id)
        ).one()
        assert approval.decision == "pending"
    finally:
        db.close()


def test_owner_task_duplicate_save_one_row() -> None:
    init_db()
    db = get_session_factory()()
    provider_event_id = "xcut.owner.1"
    claim_key = f"whatsapp:{provider_event_id}"
    try:
        store = LeadStore(db)
        assert store.claim_operation(scope="owner_task", key=claim_key) is True
        kwargs = {
            "provider": "whatsapp",
            "provider_event_id": provider_event_id,
            "channel": "whatsapp",
            "external_id": "972509990801",
            "task_type": "sales",
            "status": "logged",
        }
        store.save_owner_task(**kwargs)
        store.complete_operation(
            scope="owner_task",
            key=claim_key,
            result_json='{"ok": true}',
        )
        assert store.claim_operation(scope="owner_task", key=claim_key) is False
        store.save_owner_task(**kwargs)
        db.commit()
        count = db.scalar(
            select(func.count())
            .select_from(OwnerTaskRow)
            .where(
                OwnerTaskRow.provider == "whatsapp",
                OwnerTaskRow.provider_event_id == provider_event_id,
            )
        )
        assert count == 1
    finally:
        db.close()


def test_cancellation_same_inbound_one_canonical() -> None:
    init_db()
    db = get_session_factory()()
    external_id = "xcut.cancel@example.com"
    inbound_id = "xcut.cancel.1"
    try:
        store = LeadStore(db)
        lead_id = _seed_booked(store, external_id=external_id)
        booking = FakeCalendarBookingPort()
        base = dict(
            store=store,
            lead_id=lead_id,
            provider="gmail",
            channel=Channel.GMAIL,
            conversation_id="xcut.cancel.thread",
            message="cancel my meeting",
            calendar=FakeCalendarPort([]),
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            inbound_id=inbound_id,
        )
        first = resolve_booked_meeting_change(**base, now=FIXED_NOW)
        second = resolve_booked_meeting_change(
            **base, now=FIXED_NOW + timedelta(minutes=1)
        )
        db.commit()
        row = store.get_meeting(lead_id)
        assert row is not None
        assert first.reply == CANCELLATION_REQUESTED_REPLY
        assert second.reply == CANCELLATION_REQUESTED_REPLY
        assert row.status == STATUS_CANCELLATION_REQUESTED
        assert booking.get_calls == []
        assert booking.patch_calls == []
        event = store.get_canonical_event(
            provider="gmail",
            provider_event_id=f"{lead_id}:cancellation_requested",
        )
        assert event is not None
    finally:
        db.close()


def test_sheets_same_inbound_upserts_once() -> None:
    init_db()
    db = get_session_factory()()
    inbound_id = "xcut.sheets.1"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="xcut.sheets.web.1"
        )
        port = _CountingSheetsPort()
        row = _sample_mirror_row(lead_id=lead_id)
        for _ in range(2):
            if claim_sheets_mirror(store=store, inbound_id=inbound_id, tab="sales"):
                mirror_lead(sheets=port, row=row, kill_switch=False)
                complete_sheets_mirror(store=store, inbound_id=inbound_id, tab="sales")
        db.commit()
        assert port.lead_upserts == 1
        assert claim_sheets_mirror(store=store, inbound_id=inbound_id, tab="sales") is False
    finally:
        db.close()


def test_follow_up_same_inbound_writes_one_row() -> None:
    init_db()
    db = get_session_factory()()
    inbound_id = "xcut.fu.1"
    external_id = "xcut.fu.web.1"
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=external_id
        )
        sales = _good_willing_sales(lead_id)
        store.save_sales(sales)
        settings = get_settings()
        for _ in range(2):
            apply_follow_up_policy(
                store,
                lead_id=lead_id,
                channel=Channel.WEBSITE,
                action=NextAction.OFFER_MEETING.value,
                sales=sales,
                timezone=settings.calendar_timezone,
                kill_switch=False,
                inbound_id=inbound_id,
            )
        db.commit()
        follow_up = db.scalars(
            select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)
        ).one()
        assert follow_up.status == "pending"
        follow_events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.FOLLOW_UP.value,
                )
            ).all()
        )
        assert len(follow_events) == 1
        payload = json.loads(follow_events[0].payload_json)
        assert payload["status"] == "pending"
    finally:
        db.close()


def test_content_sheets_same_inbound_upserts_once() -> None:
    init_db()
    db = get_session_factory()()
    inbound_id = "xcut.sheets.content.1"
    try:
        store = LeadStore(db)
        port = _CountingContentSheetsPort()
        row = ContentMirrorRow(
            media_id="17841400112233445566",
            media_type="IMAGE",
            views="1200",
            reach="900",
            likes="45",
            comments="3",
            saved="12",
            lead_signals=0,
        )
        for _ in range(2):
            if claim_sheets_mirror(store=store, inbound_id=inbound_id, tab="content"):
                mirror_content(sheets=port, row=row, kill_switch=False)
                complete_sheets_mirror(store=store, inbound_id=inbound_id, tab="content")
        db.commit()
        assert port.content_upserts == 1
        assert claim_sheets_mirror(store=store, inbound_id=inbound_id, tab="content") is False
    finally:
        db.close()
