import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.api.inbound import process_inbound_texts
from app.capabilities.types import Principal
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.meeting_availability import is_workday_local
from app.domain.owner_calendar import apply_owner_calendar
from app.domain.owner_tasks import ack_for_owner_task, classify_owner_task
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState
from app.graph.replies import WEBSITE_REPLIES
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import (
    ComposioCalendarPort,
    DisabledCalendarPort,
    FakeCalendarPort,
    TimeSlot,
    prepare_meeting_offer,
)

IL_TZ = ZoneInfo("Asia/Jerusalem")
FIXED_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
OWNER_NOW = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)
OFFER_COPY = WEBSITE_REPLIES[NextAction.OFFER_MEETING]
OFFER_LEAD_EMAIL = "cal.fresh.offer.1@example.com"
OWNER_FRESH_PHONE = "972509998601"
OWNER_FRESH_EMPTY_PHONE = "972509998602"


def _ready_to_meet_state(lead_id: str) -> SalesState:
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


def _policy_gap(*, now: datetime, days_ahead: int = 4) -> TimeSlot:
    local = now.astimezone(IL_TZ) + timedelta(days=max(days_ahead, 2))
    for _ in range(8):
        if is_workday_local(local):
            break
        local = local + timedelta(days=1)
    gap_start = datetime(
        local.year, local.month, local.day, 10, 0, tzinfo=IL_TZ
    ).astimezone(UTC)
    gap_end = datetime(
        local.year, local.month, local.day, 12, 0, tzinfo=IL_TZ
    ).astimezone(UTC)
    return TimeSlot(start=gap_start, end=gap_end)


def test_prepare_meeting_offer_fake_freshness_live() -> None:
    slot = _policy_gap(now=FIXED_NOW)
    result = prepare_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=FakeCalendarPort([slot]),
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert "זמין:" in result.reply
    assert "1." in result.reply
    assert "השיבו 1, 2, 3 כדי" in result.reply
    assert result.outcome is not None
    assert result.outcome.freshness == "live"
    assert result.outcome.status == "ok"


def test_prepare_meeting_offer_empty_freshness_unverified() -> None:
    for port in (DisabledCalendarPort(), FakeCalendarPort([])):
        result = prepare_meeting_offer(
            reply=OFFER_COPY,
            next_action=NextAction.OFFER_MEETING.value,
            calendar=port,
            kill_switch=False,
            now=FIXED_NOW,
        )
        assert result.reply == OFFER_COPY
        assert "1." not in result.reply
        assert result.outcome is not None
        assert result.outcome.freshness == "unverified"
        assert result.outcome.status == "empty"


def test_prepare_meeting_offer_kill_switch_freshness_empty() -> None:
    class RaisingCalendarPort:
        def find_free_slots(self, **_kwargs: object) -> list[TimeSlot]:
            raise RuntimeError("must not call port when kill switch is on")

    result = prepare_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=RaisingCalendarPort(),
        kill_switch=True,
        now=FIXED_NOW,
    )
    assert result.reply == OFFER_COPY
    assert result.outcome is not None
    assert result.outcome.freshness == ""
    assert result.outcome.status == "denied"


def test_prepare_meeting_offer_http_401_freshness_unverified() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    client = httpx.Client(transport=transport)
    calendar = ComposioCalendarPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    result = prepare_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=calendar,
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert result.reply == OFFER_COPY
    assert "זמין" not in result.reply
    assert result.outcome is not None
    assert result.outcome.status == "unauthorized"
    assert result.outcome.freshness == "unverified"


def test_apply_owner_calendar_fake_freshness_live() -> None:
    ack = ack_for_owner_task(classify_owner_task("check my calendar"))
    enriched, outcome = apply_owner_calendar(
        ack,
        FakeCalendarPort([_policy_gap(now=OWNER_NOW)]),
        principal=Principal.owner(source="test"),
        kill_switch=False,
        timezone="Asia/Jerusalem",
        now=OWNER_NOW,
    )
    assert "מועדים פנויים:" in enriched
    assert "1." in enriched
    assert outcome is not None
    assert outcome.freshness == "live"
    assert outcome.status == "ok"


def test_apply_owner_calendar_empty_freshness_unverified() -> None:
    ack = ack_for_owner_task(classify_owner_task("check my calendar"))
    for port in (DisabledCalendarPort(), FakeCalendarPort([])):
        enriched, outcome = apply_owner_calendar(
            ack,
            port,
            principal=Principal.owner(source="test"),
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=OWNER_NOW,
        )
        assert "אין מועדים פנויים" in enriched
        assert outcome is not None
        assert outcome.freshness == "unverified"
        assert outcome.status == "empty"


def test_apply_owner_calendar_kill_switch_freshness_empty() -> None:
    class RaisingCalendarPort:
        def find_free_slots(self, **_kwargs: object) -> list[TimeSlot]:
            raise RuntimeError("port must not be called")

    ack = ack_for_owner_task(classify_owner_task("check my calendar"))
    enriched, outcome = apply_owner_calendar(
        ack,
        RaisingCalendarPort(),
        principal=Principal.owner(source="test"),
        kill_switch=True,
        timezone="Asia/Jerusalem",
        now=OWNER_NOW,
    )
    assert enriched == ack
    assert outcome is not None
    assert outcome.freshness == ""
    assert outcome.status == "denied"


@pytest.mark.asyncio
async def test_inbound_offer_meeting_freshness_persisted(monkeypatch) -> None:
    from tests.conftest import freeze_mia_clock

    freeze_mia_clock(monkeypatch, FIXED_NOW)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL, external_id=OFFER_LEAD_EMAIL
        )
        store.save_sales(_ready_to_meet_state(lead_id))
        db.commit()

        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[
                {
                    "id": "cal.fresh.offer.1",
                    "from": OFFER_LEAD_EMAIL,
                    "text": "ok",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=FakeCalendarPort([_policy_gap(now=FIXED_NOW)]),
        )
        db.commit()
        row = store.get_tool_run("cal.fresh.offer.1:tool:calendar_find_free_slots")
        assert row is not None
        assert row.freshness == "live"
        assert row.status == "ok"
        event = store.get_canonical_event(
            provider="gmail",
            provider_event_id="cal.fresh.offer.1:tool:calendar_find_free_slots",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert "freshness" not in payload
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_owner_calendar_freshness_persisted() -> None:
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
                    "id": "cal.fresh.owner.1",
                    "from": OWNER_FRESH_PHONE,
                    "text": "check my calendar",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_PHONE},
            calendar=FakeCalendarPort([_policy_gap(now=datetime.now(UTC))]),
        )
        db.commit()
        row = store.get_tool_run("cal.fresh.owner.1:tool:calendar_find_free_slots")
        assert row is not None
        assert row.freshness == "live"
        assert row.status == "ok"
        event = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="cal.fresh.owner.1:tool:calendar_find_free_slots",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload == {
            "tool": "calendar_find_free_slots",
            "status": "ok",
            "result_count": row.result_count,
        }
        assert "freshness" not in payload
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_owner_calendar_empty_freshness_unverified() -> None:
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
                    "id": "cal.fresh.owner.empty.1",
                    "from": OWNER_FRESH_EMPTY_PHONE,
                    "text": "check my calendar",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_EMPTY_PHONE},
            calendar=DisabledCalendarPort(),
        )
        db.commit()
        row = store.get_tool_run(
            "cal.fresh.owner.empty.1:tool:calendar_find_free_slots"
        )
        assert row is not None
        assert row.freshness == "unverified"
        assert row.status == "empty"
    finally:
        db.close()
