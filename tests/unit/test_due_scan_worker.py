import inspect
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from app.core.capabilities import CapabilityId, require_alive
from app.core.config import get_settings
from app.db.models import FollowUpRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import (
    ACTION_ANALYZE,
    ACTION_FOLLOW_UP,
    CONDITION_NONE,
    TRIGGER_DUE_DATE,
    TRIGGER_SPEND_THRESHOLD,
)
from app.domain.events import Channel
from app.domain.followup_voice import MEETING_OFFERED_FOLLOW_UP
from app.domain.followups import (
    REASON_MEETING_OFFERED,
    STATUS_PENDING,
    follow_up_due_on,
)
from app.domain.sales import FitLevel, SalesState
from app.workers import due_scan as due_scan_module
from app.workers.due_scan import main, run_due_scan
from sqlalchemy import select

SCAN_PHONE_WA = "972509994401"
MAIN_PHONE = "972509994403"
OWNER_EVENT_ID = "evt.owner.scan.worker.due"
OWNER_SPEND_EVENT_ID = "evt.owner.scan.worker.spend"
OWNER_EXTERNAL_ID = "972509994404"
OWNER_SPEND_EXTERNAL_ID = "972509994405"
_FIXED_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))


def _seed_due_follow_up(
    store: LeadStore,
    *,
    channel: Channel,
    external_id: str,
    due_at: str,
    fit: FitLevel = FitLevel.POSSIBLE,
) -> str:
    _, lead_id = store.open_channel_lead(channel=channel, external_id=external_id)
    store.save_sales(SalesState(lead_id=lead_id, fit=fit))
    store.upsert_follow_up(
        lead_id=lead_id,
        channel=channel.value,
        reason=REASON_MEETING_OFFERED,
        status=STATUS_PENDING,
        due_at=due_at,
    )
    return lead_id


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
        external_id=OWNER_EXTERNAL_ID,
        task_type="sales",
        status=status,
        due_at=due_at,
        trigger=trigger,
        condition=condition,
        action=ACTION_FOLLOW_UP,
    )


def _seed_spend_threshold_owner_task(
    store: LeadStore,
    *,
    provider_event_id: str,
    external_id: str = OWNER_SPEND_EXTERNAL_ID,
) -> None:
    store.save_owner_task(
        provider="whatsapp",
        provider_event_id=provider_event_id,
        channel="whatsapp",
        external_id=external_id,
        task_type="analytics",
        status="logged",
        due_at=None,
        trigger=TRIGGER_SPEND_THRESHOLD,
        condition=CONDITION_NONE,
        action=ACTION_ANALYZE,
    )


def _follow_up_for_lead(db, lead_id: str) -> FollowUpRow:
    return db.scalars(
        select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)
    ).one()


def test_run_due_scan_persists_follow_up_and_owner_task() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        due_at = follow_up_due_on(
            now=_FIXED_NOW, timezone=settings.calendar_timezone, offset_days=0
        )
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=SCAN_PHONE_WA,
            due_at=due_at,
            fit=FitLevel.POSSIBLE,
        )
        _seed_owner_task(store, provider_event_id=OWNER_EVENT_ID, due_at=due_at)
        db.commit()
        summary = run_due_scan(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=_FIXED_NOW,
        )
        assert summary.follow_ups_scanned >= 1
        assert summary.follow_ups_send_ready >= 1
        assert summary.owner_tasks_scanned >= 1
        assert summary.owner_tasks_due_ready >= 1
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is True
        assert row.block_reason == "due_pending"
        assert row.draft == MEETING_OFFERED_FOLLOW_UP
        owner_row = store.get_owner_task(provider="whatsapp", provider_event_id=OWNER_EVENT_ID)
        assert owner_row is not None
        assert owner_row.due_ready is True
        assert owner_row.block_reason == "due_pending"
    finally:
        db.close()


def test_run_due_scan_invalid_timezone_skips_date_follow_ups() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        summary = run_due_scan(
            store,
            timezone="Not/A/Timezone",
            kill_switch=False,
            now=_FIXED_NOW,
        )
        assert summary.follow_ups_scanned == 0
        assert summary.follow_ups_send_ready == 0
    finally:
        db.close()


def test_run_due_scan_calls_both_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    db = get_session_factory()()
    calls: dict[str, object] = {}

    def fake_follow_ups(store, *, timezone, kill_switch, now=None):
        calls["follow_ups"] = {
            "timezone": timezone,
            "kill_switch": kill_switch,
            "now": now,
        }
        return []

    def fake_owner_tasks(store, *, timezone, now=None, monthly_budget=None, spend_mtd=None):
        calls["owner_tasks"] = {
            "timezone": timezone,
            "now": now,
            "monthly_budget": monthly_budget,
            "spend_mtd": spend_mtd,
        }
        return []

    monkeypatch.setattr(due_scan_module, "scan_due_follow_ups", fake_follow_ups)
    monkeypatch.setattr(due_scan_module, "scan_due_owner_tasks", fake_owner_tasks)
    try:
        store = LeadStore(db)
        summary = run_due_scan(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=True,
            now=_FIXED_NOW,
        )
        assert summary.model_dump() == {
            "follow_ups_scanned": 0,
            "follow_ups_send_ready": 0,
            "owner_tasks_scanned": 0,
            "owner_tasks_due_ready": 0,
            "website_conversations_finalized": 0,
        }
        assert calls["follow_ups"] == {
            "timezone": "Asia/Jerusalem",
            "kill_switch": True,
            "now": _FIXED_NOW,
        }
        assert calls["owner_tasks"] == {
            "timezone": "Asia/Jerusalem",
            "now": _FIXED_NOW,
            "monthly_budget": None,
            "spend_mtd": None,
        }
    finally:
        db.close()


def test_run_due_scan_spend_threshold_with_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import Settings

    monkeypatch.setattr(
        due_scan_module,
        "get_settings",
        lambda: Settings(campaign_monthly_budget="5000"),
    )
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        _seed_spend_threshold_owner_task(
            store, provider_event_id=OWNER_SPEND_EVENT_ID
        )
        store.upsert_campaign_pacing(
            scope="account",
            campaign="account",
            monthly_budget="5000",
            spend="5000.00",
            expected_spend="2500.00",
            remaining="2500.00",
            projected="5000.00",
            over_under="0.00",
            status="on_track",
        )
        db.commit()
        summary = run_due_scan(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=_FIXED_NOW,
        )
        assert summary.owner_tasks_scanned >= 1
        assert summary.owner_tasks_due_ready >= 1
        owner_row = store.get_owner_task(
            provider="whatsapp", provider_event_id=OWNER_SPEND_EVENT_ID
        )
        assert owner_row is not None
        assert owner_row.due_ready is True
        assert owner_row.block_reason == "spend_reached"
    finally:
        pacing = store.get_campaign_pacing()
        if pacing is not None:
            db.delete(pacing)
            db.commit()
        db.close()


def test_run_due_scan_never_imports_message_port() -> None:
    source = inspect.getsource(due_scan_module)
    assert "MessagePort" not in source
    assert "app.integrations.base" not in source


def test_require_alive_due_scan() -> None:
    require_alive(CapabilityId.DUE_SCAN)


def test_main_stdout_counts_only(capsys: pytest.CaptureFixture[str]) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        due_at = follow_up_due_on(
            now=datetime.now(UTC),
            timezone=settings.calendar_timezone,
            offset_days=0,
        )
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=MAIN_PHONE,
            due_at=due_at,
            fit=FitLevel.POSSIBLE,
        )
        db.commit()
    finally:
        db.close()
    main()
    captured = capsys.readouterr()
    body = json.loads(captured.out.strip())
    assert set(body.keys()) == {
        "follow_ups_scanned",
        "follow_ups_send_ready",
        "owner_tasks_scanned",
        "owner_tasks_due_ready",
        "website_conversations_finalized",
    }
    for value in body.values():
        assert isinstance(value, int)
    assert MAIN_PHONE not in captured.out
    assert "lead_" not in captured.out
    assert MEETING_OFFERED_FOLLOW_UP not in captured.out
    assert "draft" not in captured.out
    db = get_session_factory()()
    try:
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is True
        assert row.block_reason == "due_pending"
        assert row.draft == MEETING_OFFERED_FOLLOW_UP
    finally:
        db.close()
