from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import (
    ACTION_FOLLOW_UP,
    ACTION_LOG,
    ACTION_NONE,
    CONDITION_IF_NOT_REPLIED,
    CONDITION_NONE,
    TRIGGER_DUE_DATE,
    TRIGGER_NONE,
    parse_condition,
    parse_due_at,
    plan_owner_commitment,
    scan_due_owner_tasks,
)
from app.domain.followups import follow_up_due_on
from app.domain.owner_tasks import classify_owner_task

_JERUSALEM = ZoneInfo("Asia/Jerusalem")
_FIXED_NOW = datetime(2026, 8, 21, 10, 0, tzinfo=_JERUSALEM)


def test_parse_due_at_tomorrow_english() -> None:
    due = parse_due_at(
        "Schedule a follow-up with Daniel tomorrow.",
        now=_FIXED_NOW,
        timezone="Asia/Jerusalem",
    )
    assert due == "2026-08-22"
    assert "Daniel" not in (due or "")


def test_parse_due_at_tomorrow_hebrew() -> None:
    due = parse_due_at("תזכירי לי מחר על הליד", now=_FIXED_NOW, timezone="Asia/Jerusalem")
    assert due == "2026-08-22"


def test_parse_due_at_today_english() -> None:
    due = parse_due_at("check the lead today", now=_FIXED_NOW, timezone="Asia/Jerusalem")
    assert due == "2026-08-21"


def test_parse_due_at_today_hebrew() -> None:
    due = parse_due_at("תבדקי היום את הקמפיין", now=_FIXED_NOW, timezone="Asia/Jerusalem")
    assert due == "2026-08-21"


def test_parse_due_at_no_token() -> None:
    assert parse_due_at("remind me about the thing later", now=_FIXED_NOW) is None


def test_parse_due_at_soonest_when_multiple_tokens() -> None:
    due = parse_due_at("do it tomorrow or next week", now=_FIXED_NOW, timezone="Asia/Jerusalem")
    assert due == "2026-08-22"


def test_parse_due_at_naive_now_assumes_utc() -> None:
    naive = datetime(2026, 8, 21, 10, 0)
    due = parse_due_at("tomorrow", now=naive, timezone="Asia/Jerusalem")
    assert due == "2026-08-22"


def test_parse_due_at_ignores_today_inside_word() -> None:
    assert parse_due_at("notoday follow-up", now=_FIXED_NOW) is None


def test_parse_due_at_ignores_machar_inside_hebrew_word() -> None:
    assert parse_due_at("תזכירי לי מחרתיים על הליד", now=_FIXED_NOW) is None


def test_parse_condition_if_not_replied_english() -> None:
    text = "Schedule a follow-up with Daniel tomorrow if he has not replied."
    assert parse_condition(text) == CONDITION_IF_NOT_REPLIED


def test_parse_condition_if_not_replied_hebrew() -> None:
    assert parse_condition("תעקבי מחר אם לא יענה") == CONDITION_IF_NOT_REPLIED


def test_parse_condition_none_without_token() -> None:
    assert parse_condition("how's the campaign spend") == CONDITION_NONE


def test_plan_sales_follow_up_with_due_and_condition() -> None:
    text = "Schedule a follow-up with Daniel tomorrow if he has not replied."
    decision = classify_owner_task(text)
    due_at = parse_due_at(text, now=_FIXED_NOW, timezone="Asia/Jerusalem")
    plan = plan_owner_commitment(decision=decision, text=text, due_at=due_at)
    assert plan.trigger == TRIGGER_DUE_DATE
    assert plan.condition == CONDITION_IF_NOT_REPLIED
    assert plan.action == ACTION_FOLLOW_UP


def test_plan_hebrew_sales_follow_up_with_condition() -> None:
    text = "תעקבי מחר אם לא יענה"
    decision = classify_owner_task(text)
    due_at = parse_due_at(text, now=_FIXED_NOW, timezone="Asia/Jerusalem")
    plan = plan_owner_commitment(decision=decision, text=text, due_at=due_at)
    assert plan.trigger == TRIGGER_DUE_DATE
    assert plan.condition == CONDITION_IF_NOT_REPLIED
    assert plan.action == ACTION_FOLLOW_UP


def test_plan_preference_all_none() -> None:
    text = "from now on never say tomorrow"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(decision=decision, text=text, due_at=None)
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_NONE


def test_plan_understanding_check_all_none() -> None:
    text = "remind me tomorrow about the thing"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(decision=decision, text=text, due_at=None)
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_NONE


def test_plan_calendar_tomorrow_no_due_trigger() -> None:
    from app.domain.owner_tasks import OwnerTaskType

    text = "check my calendar tomorrow"
    decision = classify_owner_task(text)
    due_at = parse_due_at(text, now=_FIXED_NOW, timezone="Asia/Jerusalem")
    plan = plan_owner_commitment(decision=decision, text=text, due_at=due_at)
    assert decision.task_type == OwnerTaskType.CALENDAR
    assert plan.trigger == TRIGGER_NONE
    assert plan.action == ACTION_LOG


def _seed_owner_task(
    store: LeadStore,
    *,
    provider_event_id: str,
    due_at: str,
    status: str = "logged",
    trigger: str = TRIGGER_DUE_DATE,
    condition: str = CONDITION_NONE,
) -> None:
    store.save_owner_task(
        provider="whatsapp",
        provider_event_id=provider_event_id,
        channel="whatsapp",
        external_id="972509990099",
        task_type="sales",
        status=status,
        due_at=due_at,
        trigger=trigger,
        condition=condition,
        action=ACTION_FOLLOW_UP,
    )


def test_scan_due_owner_tasks_due_pending() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        event_id = "evt.owner.scan.due_pending"
        _seed_owner_task(store, provider_event_id=event_id, due_at=due_at)
        db.commit()
        results = scan_due_owner_tasks(
            store,
            timezone=settings.calendar_timezone,
            now=now,
        )
        matching = [item for item in results if item.provider_event_id == event_id]
        assert len(matching) == 1
        assert matching[0].due_ready is True
        assert matching[0].reason == "due_pending"
        row = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert row is not None
        assert row.due_ready is True
        assert row.block_reason == "due_pending"
    finally:
        db.close()


def test_scan_due_owner_tasks_if_not_replied() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        event_id = "evt.owner.scan.if_not_replied"
        _seed_owner_task(
            store,
            provider_event_id=event_id,
            due_at=due_at,
            condition=CONDITION_IF_NOT_REPLIED,
        )
        db.commit()
        results = scan_due_owner_tasks(
            store,
            timezone=settings.calendar_timezone,
            now=now,
        )
        matching = [item for item in results if item.provider_event_id == event_id]
        assert len(matching) == 1
        assert matching[0].due_ready is False
        assert matching[0].reason == "if_not_replied"
        row = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert row is not None
        assert row.due_ready is False
        assert row.block_reason == "if_not_replied"
    finally:
        db.close()


def test_scan_due_owner_tasks_not_due_trigger() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        event_id = "evt.owner.scan.not_due_trigger"
        _seed_owner_task(
            store,
            provider_event_id=event_id,
            due_at=due_at,
            trigger=TRIGGER_NONE,
        )
        db.commit()
        results = scan_due_owner_tasks(
            store,
            timezone=settings.calendar_timezone,
            now=now,
        )
        matching = [item for item in results if item.provider_event_id == event_id]
        assert len(matching) == 1
        assert matching[0].due_ready is False
        assert matching[0].reason == "not_due_trigger"
        row = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert row is not None
        assert row.due_ready is False
        assert row.block_reason == "not_due_trigger"
    finally:
        db.close()


def test_scan_due_owner_tasks_tomorrow_not_scanned() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=1)
        event_id = "evt.owner.scan.tomorrow"
        _seed_owner_task(store, provider_event_id=event_id, due_at=due_at)
        db.commit()
        results = scan_due_owner_tasks(
            store,
            timezone=settings.calendar_timezone,
            now=now,
        )
        matching = [item for item in results if item.provider_event_id == event_id]
        assert matching == []
        row = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert row is not None
        assert row.due_ready is False
        assert row.block_reason == ""
    finally:
        db.close()


def test_scan_due_owner_tasks_invalid_timezone_returns_empty() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        due_at = follow_up_due_on(now=now, timezone="Asia/Jerusalem", offset_days=0)
        event_id = "evt.owner.scan.bad_tz_only"
        _seed_owner_task(store, provider_event_id=event_id, due_at=due_at)
        db.commit()
        results = scan_due_owner_tasks(store, timezone="Not/A/Timezone", now=now)
        matching = [item for item in results if item.provider_event_id == event_id]
        assert matching == []
        row = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert row is not None
        assert row.due_ready is False
        assert row.block_reason == ""
    finally:
        db.close()


def test_commitments_module_no_message_port() -> None:
    import inspect

    import app.domain.commitments as commitments_module

    source = inspect.getsource(commitments_module)
    assert "MessagePort" not in source


def test_scan_due_owner_tasks_needs_clarification_not_in_due_list() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        event_id = "evt.owner.scan.needs_clarification"
        _seed_owner_task(
            store,
            provider_event_id=event_id,
            due_at=due_at,
            status="needs_clarification",
        )
        db.commit()
        results = scan_due_owner_tasks(
            store,
            timezone=settings.calendar_timezone,
            now=now,
        )
        matching = [item for item in results if item.provider_event_id == event_id]
        assert matching == []
        row = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert row is not None
        assert row.due_ready is False
        assert row.block_reason == ""
    finally:
        db.close()
