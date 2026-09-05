from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.models import ToolRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.followups import (
    REASON_MEETING_OFFERED,
    STATUS_PENDING,
    follow_up_due_on,
    lead_recent_messages_outcome,
    scan_due_follow_ups,
    website_session_events_outcome,
)
from app.domain.ownership_freshness import owner_permissions_outcome
from app.domain.sales import FitLevel, SalesState
from app.integrations.base import RecordingMessagePort
from sqlalchemy import select

OWNER_PERM_PHONE = "972509997001"
OWNER_PERM_PHONE_2 = "972509997002"
PROSPECT_WA_PHONE = "972509997003"
SCAN_FRESH_PHONE = "972509997901"
SCAN_FRESH_TOMORROW = "972509997902"


def test_owner_permissions_outcome_live_when_present() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = owner_permissions_outcome(present=True, now=now)
    assert outcome.freshness == "live"
    assert outcome.status == "ok"
    assert outcome.tool == "owner_permissions"


def test_lead_recent_messages_outcome_cached_when_present() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = lead_recent_messages_outcome(present=True, now=now)
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert outcome.tool == "lead_recent_messages"


def test_website_session_events_outcome_cached_when_present() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = website_session_events_outcome(present=True, now=now)
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert outcome.tool == "website_session_events"


def _owner_perm_rows(db, owner_from: str) -> list[ToolRunRow]:
    return list(
        db.scalars(
            select(ToolRunRow).where(
                ToolRunRow.provider_event_id
                == f"owner:{owner_from}:tool:owner_permissions"
            )
        )
    )


@pytest.mark.asyncio
async def test_owner_whatsapp_stamps_owner_permissions_once() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        for event_id, text in (
            ("wa.owner.perm.1", "סיכום יומי"),
            ("wa.owner.perm.2", "סיכום יומי"),
        ):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{"id": event_id, "from": OWNER_PERM_PHONE, "text": text}],
                store=store,
                port=port,
                kill_switch=False,
                owner_ids={OWNER_PERM_PHONE},
            )
        db.commit()
        rows = _owner_perm_rows(db, OWNER_PERM_PHONE)
        assert len(rows) == 1
        assert rows[0].freshness == "live"
        assert rows[0].lead_id is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_whatsapp_does_not_stamp_owner_permissions() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wa.prospect.no.perm.1",
                    "from": PROSPECT_WA_PHONE,
                    "text": "hello clinic",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PERM_PHONE_2},
        )
        db.commit()
        rows = list(
            db.scalars(
                select(ToolRunRow).where(
                    ToolRunRow.provider_event_id
                    == f"owner:{PROSPECT_WA_PHONE}:tool:owner_permissions"
                )
            )
        )
        assert rows == []
    finally:
        db.close()


def _seed_due_follow_up(store: LeadStore, *, external_id: str, due_at: str) -> str:
    _, lead_id = store.open_channel_lead(
        channel=Channel.WHATSAPP, external_id=external_id
    )
    store.save_sales(SalesState(lead_id=lead_id, fit=FitLevel.POSSIBLE))
    store.upsert_follow_up(
        lead_id=lead_id,
        channel=Channel.WHATSAPP.value,
        reason=REASON_MEETING_OFFERED,
        status=STATUS_PENDING,
        due_at=due_at,
    )
    return lead_id


def test_scan_due_follow_up_stamps_lead_recent_messages_cached() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(
            now=now, timezone=settings.calendar_timezone, offset_days=0
        )
        lead_id = _seed_due_follow_up(
            store, external_id=SCAN_FRESH_PHONE, due_at=due_at
        )
        db.commit()
        scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        db.commit()
        row = store.get_tool_run(
            f"{lead_id}:followup-scan:{due_at}:tool:lead_recent_messages"
        )
        assert row is not None
        assert row.freshness == "cached"
        assert row.provider == "followup_scan"
    finally:
        db.close()


def test_scan_not_due_does_not_stamp_lead_recent_messages() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(calendar_timezone="Asia/Jerusalem")
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(
            now=now, timezone=settings.calendar_timezone, offset_days=1
        )
        lead_id = _seed_due_follow_up(
            store, external_id=SCAN_FRESH_TOMORROW, due_at=due_at
        )
        db.commit()
        scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        db.commit()
        rows = list(
            db.scalars(
                select(ToolRunRow).where(
                    ToolRunRow.lead_id == lead_id,
                    ToolRunRow.tool == "lead_recent_messages",
                )
            )
        )
        assert rows == []
    finally:
        db.close()
