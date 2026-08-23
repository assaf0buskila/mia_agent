import inspect
from datetime import UTC, datetime

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.db.models import CanonicalEventRow, OwnerWeeklyRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import (
    ACTION_LOG,
    CONDITION_NONE,
    TRIGGER_NONE,
    plan_owner_commitment,
)
from app.domain.events import (
    Channel,
    build_meeting_booked_event,
    build_meeting_cancellation_requested_event,
)
from app.domain.kpis import week_start_on
from app.domain.owner_tasks import OwnerTaskType, classify_owner_task
from app.domain.owner_weeklies import (
    apply_owner_weekly,
    apply_owner_weekly_policy,
    compute_weekly_brief,
    format_weekly_brief,
)
from app.integrations.base import RecordingMessagePort
from sqlalchemy import delete

FROZEN_NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
TIMEZONE = "Asia/Jerusalem"


def _week_start() -> str:
    value = week_start_on(now=FROZEN_NOW, timezone=TIMEZONE)
    assert value is not None
    return value


def test_classify_weekly_brief_english() -> None:
    decision = classify_owner_task("send me the weekly brief")
    assert decision.task_type == OwnerTaskType.WEEKLY_BRIEF
    assert decision.needs_clarification is False


def test_classify_weekly_brief_hebrew() -> None:
    decision = classify_owner_task("תני לי סיכום שבועי")
    assert decision.task_type == OwnerTaskType.WEEKLY_BRIEF
    assert decision.needs_clarification is False


def test_classify_daily_brief_still_daily_not_weekly() -> None:
    decision = classify_owner_task("daily brief")
    assert decision.task_type == OwnerTaskType.DAILY_BRIEF
    assert decision.needs_clarification is False
    weekly = classify_owner_task("weekly brief")
    assert weekly.task_type == OwnerTaskType.WEEKLY_BRIEF
    assert weekly.task_type != OwnerTaskType.DAILY_BRIEF


def test_classify_weekly_brief_plus_campaign_clarification() -> None:
    decision = classify_owner_task("weekly brief and campaign spend")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert decision.matched_types == ["analytics", "weekly_brief"]


def test_classify_bare_week_or_hashavua_not_weekly() -> None:
    assert classify_owner_task("week").task_type == OwnerTaskType.NOTE
    assert classify_owner_task("השבוע").task_type == OwnerTaskType.NOTE


def test_plan_weekly_brief_trigger_none_even_with_due_at() -> None:
    text = "weekly brief today"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(
        decision=decision,
        text=text,
        due_at="2026-08-21",
    )
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_LOG


def test_format_weekly_brief_header_and_no_execute_line() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        snapshot = compute_weekly_brief(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert snapshot is not None
        text = format_weekly_brief(snapshot)
        assert text.startswith("סיכום שבועי 17.08.2026")
        assert "פגישות נקבעו:" in text
        assert "בקשות ביטול:" in text
        assert "מעקבים פתוחים:" in text
        assert "לא ביצעתי משימות ולא שלחתי מעקבים." in text
        assert "spend" not in text.lower()
        assert "$" not in text
        assert "lead_" not in text
    finally:
        db.close()


def test_apply_owner_weekly_kill_switch_formats_no_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        week_start = _week_start()
        before = store.get_owner_weekly(week_start)
        before_leads = None if before is None else before.leads
        ack = apply_owner_weekly(
            store,
            timezone=TIMEZONE,
            kill_switch=True,
            demo_active=False,
            now=FROZEN_NOW,
        )
        assert ack is not None
        assert "סיכום שבועי" in ack
        db.commit()
        after = store.get_owner_weekly(week_start)
        if before is None:
            assert after is None
        else:
            assert after is not None
            assert after.leads == before_leads
    finally:
        db.close()


def test_apply_owner_weekly_demo_returns_none_no_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        week_start = _week_start()
        before = store.get_owner_weekly(week_start)
        before_leads = None if before is None else before.leads
        ack = apply_owner_weekly(
            store,
            timezone=TIMEZONE,
            kill_switch=False,
            demo_active=True,
            now=FROZEN_NOW,
        )
        assert ack is None
        db.commit()
        after = store.get_owner_weekly(week_start)
        if before is None:
            assert after is None
        else:
            assert after is not None
            assert after.leads == before_leads
    finally:
        db.close()


def test_apply_owner_weekly_policy_persists_and_upserts() -> None:
    init_db()
    db = get_session_factory()()
    week_start = _week_start()
    try:
        store = LeadStore(db)
        snapshot = compute_weekly_brief(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert snapshot is not None
        apply_owner_weekly_policy(
            store,
            snapshot=snapshot,
            kill_switch=False,
            demo_active=False,
        )
        db.commit()
        row = store.get_owner_weekly(week_start)
        assert row is not None
        assert row.leads == snapshot.leads
        assert row.follow_ups_pending == snapshot.follow_ups_pending
        assert row.meetings_booked == snapshot.meetings_booked
        assert row.cancellation_requests == snapshot.cancellation_requests
        updated = snapshot.model_copy(update={"leads": snapshot.leads + 1})
        apply_owner_weekly_policy(
            store,
            snapshot=updated,
            kill_switch=False,
            demo_active=False,
        )
        db.commit()
        row2 = store.get_owner_weekly(week_start)
        assert row2 is not None
        assert row2.leads == updated.leads
    finally:
        db.execute(
            delete(OwnerWeeklyRow).where(OwnerWeeklyRow.week_start == week_start)
        )
        db.commit()
        db.close()


def test_compute_weekly_meetings_booked_increment_in_iso_week() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = "lead_w1e2e3k4b5o6"
        before = compute_weekly_brief(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert before is not None
        before_booked = before.meetings_booked
        event = build_meeting_booked_event(
            provider="website",
            channel=Channel.WEBSITE,
            lead_id=lead_id,
            conversation_id="sess_weekly_brf001",
            scheduled_at="2026-08-22T07:00:00+00:00",
            occurred_at=FROZEN_NOW,
        )
        store.save_canonical_event(provider="website", event=event)
        db.commit()
        after = compute_weekly_brief(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert after is not None
        assert after.meetings_booked >= before_booked + 1
        outside = datetime(2020, 1, 6, 12, 0, tzinfo=UTC)
        outside_snapshot = compute_weekly_brief(
            store,
            timezone=TIMEZONE,
            now=outside,
        )
        assert outside_snapshot is not None
        assert outside_snapshot.meetings_booked == 0
    finally:
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == f"{lead_id}:booked"
            )
        )
        db.commit()
        db.close()


def test_compute_weekly_cancellation_requests_increment_in_iso_week() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = "lead_w1e2e3k4c5a6"
        before = compute_weekly_brief(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert before is not None
        before_requests = before.cancellation_requests
        event = build_meeting_cancellation_requested_event(
            provider="website",
            channel=Channel.WEBSITE,
            lead_id=lead_id,
            conversation_id="sess_weekly_canc001",
            occurred_at=FROZEN_NOW,
        )
        store.save_canonical_event(provider="website", event=event)
        db.commit()
        after = compute_weekly_brief(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert after is not None
        assert after.cancellation_requests >= before_requests + 1
    finally:
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id
                == f"{lead_id}:cancellation_requested"
            )
        )
        db.commit()
        db.close()


def test_require_alive_owner_weekly() -> None:
    require_alive(CapabilityId.OWNER_WEEKLY)


def test_owner_weeklies_module_no_message_or_openai() -> None:
    import app.domain.owner_weeklies as module

    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "openai" not in source.lower()


@pytest.mark.asyncio
async def test_owner_inbound_weekly_brief_scorecard_and_persist() -> None:
    init_db()
    db = get_session_factory()()
    week_start = week_start_on(now=datetime.now(UTC), timezone=TIMEZONE)
    assert week_start is not None
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        owner_phone = "972509994701"
        event_id = "evt.weekly.owner.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": event_id,
                    "from": owner_phone,
                    "text": "weekly brief",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={owner_phone},
        )
        db.commit()
        task = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert task is not None
        assert task.task_type == "weekly_brief"
        assert task.due_at is None
        assert len(port.sent) == 1
        ack = port.sent[0].text
        assert "סיכום שבועי" in ack
        assert "לא שלחתי מעקבים" in ack
        assert "lead_" not in ack
        row = store.get_owner_weekly(week_start)
        assert row is not None
    finally:
        db.execute(
            delete(OwnerWeeklyRow).where(OwnerWeeklyRow.week_start == week_start)
        )
        db.commit()
        db.close()
