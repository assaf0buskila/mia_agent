import importlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.api.inbound import process_inbound_texts
from app.capabilities.types import Principal
from app.core.capabilities import CapabilityId, require_alive
from app.core.errors import PolicyDenied
from app.db.models import CanonicalEventRow, OwnerTaskRow, ToolRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import ACTION_LOG, TRIGGER_NONE
from app.domain.events import Channel
from app.domain.meeting_availability import is_workday_local
from app.domain.owner_calendar import apply_owner_calendar
from app.domain.owner_tasks import (
    OwnerTaskType,
    ack_for_owner_task,
    classify_owner_task,
)
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import (
    DisabledCalendarPort,
    FakeCalendarPort,
    TimeSlot,
)
from sqlalchemy import delete

IL = ZoneInfo("Asia/Jerusalem")
BASE_NOW = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)
OWNER_PHONE = "972509994901"
OWNER_EVENT = "evt.owner.cal.inbound.1"
OWNER_EVENT_KILL = "evt.owner.cal.inbound.2"


def _next_workday(local_dt: datetime) -> datetime:
    """Advance to the next Sun-Thu day, using the product's own workday rule."""
    for _ in range(7):
        if is_workday_local(local_dt):
            return local_dt
        local_dt = local_dt + timedelta(days=1)
    return local_dt


def _policy_gap(*, now: datetime | None = None) -> TimeSlot:
    clock = now if now is not None else BASE_NOW
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    # Seed the free window on a real working day. A fixed +4 lands on Friday whenever the
    # suite runs on a Monday, and the Sun-Thu policy correctly rejects it — which looked
    # like a broken calendar every Monday instead of a broken fixture.
    local_date = _next_workday(clock.astimezone(IL) + timedelta(days=4)).date()
    gap_start = datetime(
        local_date.year, local_date.month, local_date.day, 8, 0, tzinfo=IL
    ).astimezone(UTC)
    gap_end = datetime(
        local_date.year, local_date.month, local_date.day, 18, 0, tzinfo=IL
    ).astimezone(UTC)
    return TimeSlot(start=gap_start, end=gap_end)


class RaisingCalendarPort:
    def find_free_slots(self, **_kwargs: object) -> list[TimeSlot]:
        raise RuntimeError("port must not be called")


def _delete_test_rows(db, *, event_ids: tuple[str, ...]) -> None:
    for event_id in event_ids:
        db.execute(
            delete(OwnerTaskRow).where(
                OwnerTaskRow.provider == "whatsapp",
                OwnerTaskRow.provider_event_id == event_id,
            )
        )
        db.execute(
            delete(ToolRunRow).where(
                ToolRunRow.provider_event_id == f"{event_id}:tool:calendar_find_free_slots",
            )
        )
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider == "whatsapp",
                CanonicalEventRow.provider_event_id == event_id,
            )
        )
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider == "whatsapp",
                CanonicalEventRow.provider_event_id == f"{event_id}:out",
            )
        )
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider == "whatsapp",
                CanonicalEventRow.provider_event_id
                == f"{event_id}:tool:calendar_find_free_slots",
            )
        )
    db.commit()


@pytest.mark.parametrize(
    "text",
    [
        "check my calendar",
        "calendar availability",
        "what's free on my calendar",
        "whats free on my calendar",
        "free slots",
        "check my calendar tomorrow",
        "זמינות ביומן",
        "מה פנוי ביומן",
        "מועדים פנויים",
        "תבדוק את היומן",
        "בדוק את היומן",
    ],
)
def test_classify_calendar_phrases(text: str) -> None:
    decision = classify_owner_task(text)
    assert decision.task_type == OwnerTaskType.CALENDAR
    assert decision.needs_clarification is False
    assert decision.matched_types == ["calendar"]


def test_classify_calendar_wins_over_analytics() -> None:
    decision = classify_owner_task("calendar availability and instagram content")
    assert decision.task_type == OwnerTaskType.CALENDAR
    assert decision.needs_clarification is False


def test_classify_meeting_debrief_not_calendar() -> None:
    decision = classify_owner_task("סיכום פגישה lead_abc123456789")
    assert decision.task_type == OwnerTaskType.MEETING_DEBRIEF
    assert decision.task_type != OwnerTaskType.CALENDAR


def test_classify_daily_brief_not_calendar() -> None:
    decision = classify_owner_task("daily brief")
    assert decision.task_type == OwnerTaskType.DAILY_BRIEF
    assert decision.task_type != OwnerTaskType.CALENDAR


def test_classify_analytics_not_calendar() -> None:
    decision = classify_owner_task("analyze instagram content")
    assert decision.task_type == OwnerTaskType.ANALYTICS
    assert decision.task_type != OwnerTaskType.CALENDAR


def test_classify_calendar_alone_is_note() -> None:
    decision = classify_owner_task("calendar")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True


def test_classify_calendar_app_is_note() -> None:
    decision = classify_owner_task("the calendar app")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True


def test_classify_preference_with_my_calendar_stays_preference() -> None:
    decision = classify_owner_task("from now on never say my calendar")
    assert decision.task_type == OwnerTaskType.PREFERENCE
    assert decision.task_type != OwnerTaskType.CALENDAR


def test_apply_owner_calendar_numbered_slots() -> None:
    ack = ack_for_owner_task(
        classify_owner_task("check my calendar"),
    )
    enriched, outcome = apply_owner_calendar(
        ack,
        FakeCalendarPort([_policy_gap()]),
        principal=Principal.owner(source="test"),
        kill_switch=False,
        timezone="Asia/Jerusalem",
        now=BASE_NOW,
    )
    assert "מועדים פנויים:" in enriched
    assert "1." in enriched
    assert "לא יוצרת פגישה." in enriched
    assert "השב" not in enriched
    assert outcome is not None
    assert outcome.status == "ok"
    assert 1 <= outcome.result_count <= 3


def test_apply_owner_calendar_empty_disabled() -> None:
    ack = ack_for_owner_task(classify_owner_task("check my calendar"))
    for port in (DisabledCalendarPort(), FakeCalendarPort([])):
        enriched, outcome = apply_owner_calendar(
            ack,
            port,
            principal=Principal.owner(source="test"),
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=BASE_NOW,
        )
        assert "אין מועדים פנויים" in enriched
        assert "לא יוצרת פגישה." in enriched
        assert outcome is not None
        assert outcome.status == "empty"
        assert outcome.result_count == 0


def test_apply_owner_calendar_kill_switch() -> None:
    ack = ack_for_owner_task(classify_owner_task("check my calendar"))
    enriched, outcome = apply_owner_calendar(
        ack,
        RaisingCalendarPort(),
        principal=Principal.owner(source="test"),
        kill_switch=True,
        timezone="Asia/Jerusalem",
        now=BASE_NOW,
    )
    assert enriched == ack
    assert outcome is not None
    assert outcome.status == "denied"
    assert outcome.result_count == 0


def test_apply_owner_calendar_demo_skips_port() -> None:
    ack = ack_for_owner_task(classify_owner_task("check my calendar"))
    enriched, outcome = apply_owner_calendar(
        ack,
        RaisingCalendarPort(),
        principal=Principal.owner(source="test"),
        kill_switch=False,
        timezone="Asia/Jerusalem",
        now=BASE_NOW,
        demo_active=True,
    )
    assert enriched == ack
    assert outcome is None


@pytest.mark.asyncio
async def test_owner_inbound_calendar_persist_and_ack() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": OWNER_EVENT,
                "from": OWNER_PHONE,
                "text": "check my calendar",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
            calendar=FakeCalendarPort([_policy_gap(now=datetime.now(UTC))]),
        )
        db.commit()
        task = store.get_owner_task(provider="whatsapp", provider_event_id=OWNER_EVENT)
        assert task is not None
        assert task.task_type == "calendar"
        assert task.due_at is None
        assert task.trigger == TRIGGER_NONE
        assert task.action == ACTION_LOG
        assert len(port.sent) == 1
        reply = port.sent[0].text
        assert "מועדים פנויים" in reply
        assert "לא יוצרת פגישה." in reply
        assert "השב" not in reply
        tool_run = store.get_tool_run(
            f"{OWNER_EVENT}:tool:calendar_find_free_slots"
        )
        assert tool_run is not None
        assert tool_run.status == "ok"
        cal_tool = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id=f"{OWNER_EVENT}:tool:calendar_find_free_slots",
        )
        assert cal_tool is not None
        payload = json.loads(cal_tool.payload_json)
        assert payload["status"] == "ok"
        assert "start" not in payload and "time" not in payload
    finally:
        _delete_test_rows(db, event_ids=(OWNER_EVENT,))
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_calendar_kill_switch() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        with pytest.raises(PolicyDenied):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{
                    "id": OWNER_EVENT_KILL,
                    "from": OWNER_PHONE,
                    "text": "check my calendar",
                }],
                store=store,
                port=port,
                kill_switch=True,
                owner_ids={OWNER_PHONE},
                calendar=RaisingCalendarPort(),
            )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id=OWNER_EVENT_KILL
        )
        assert task is not None
        assert task.task_type == "calendar"
        ack = ack_for_owner_task(classify_owner_task("check my calendar"))
        assert "משימת יומן" in ack
        assert "לא ביצעתי" in ack
        tool_run = store.get_tool_run(
            f"{OWNER_EVENT_KILL}:tool:calendar_find_free_slots"
        )
        assert tool_run is not None
        assert tool_run.status == "denied"
        assert len(port.sent) == 0
    finally:
        _delete_test_rows(db, event_ids=(OWNER_EVENT_KILL,))
        db.close()


def test_owner_calendar_module_no_forbidden_imports() -> None:
    module = importlib.import_module("app.domain.owner_calendar")
    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "CREATE_EVENT" not in source
    assert "PATCH_EVENT" not in source
    assert "attendees" not in source


def test_require_alive_owner_calendar() -> None:
    require_alive(CapabilityId.OWNER_CALENDAR)
