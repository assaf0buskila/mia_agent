import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import get_settings
from app.db.models import ChannelIdentityRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.conversation_kill import apply_conversation_kill_policy
from app.domain.events import Channel
from app.domain.followups import (
    REASON_MEETING_OFFERED,
    STATUS_PENDING,
    evaluate_follow_up_send,
    follow_up_due_on,
)
from app.domain.sales import FitLevel, NextAction, SalesState
from app.integrations.base import RecordingMessagePort
from app.integrations.sheets import DisabledSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

OWNER_CONV_KILL_PHONE = "972509994101"


def _good_sales(lead_id: str) -> SalesState:
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


def test_website_not_interested_does_not_kill_or_stop() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        stop = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "not interested"},
        )
        assert stop.status_code == 200
        assert stop.json()["next_action"] == "ask_contact"
        assert stop.json()["lead_id"] == ""
        again = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        assert again.json()["next_action"] == "ask_contact"
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.get_website_lead_id(session_id) is None
        assert store.is_conversation_killed("") is False
    finally:
        db.close()


def test_website_identify_then_sell_never_sets_conversation_killed() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        for text in (
            "We run a clinic and miss calls all day.",
            "ok that's right",
            "I decide this quarter",
            "let's book a meeting",
        ):
            response = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": text},
            )
            assert response.status_code == 200
            assert response.json()["next_action"] in {
                "ask_need",
                "ask_contact",
                "handoff",
                "no_price",
            }
            assert response.json()["lead_id"] == ""
        student = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "I'm a student with a school project"},
        )
        assert student.json()["next_action"] == "ask_contact"
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.get_website_lead_id(session_id) is None
        assert store.is_conversation_killed("") is False
    finally:
        db.close()


def test_evaluate_follow_up_send_conversation_killed() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509994102"
        )
        store.set_conversation_killed(lead_id, True)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.WHATSAPP.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_PENDING,
            due_at=due_at,
        )
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert decision.allowed is False
        assert decision.reason == "conversation_killed"
    finally:
        db.close()


def test_apply_conversation_kill_policy_never_calls_message_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509994103"
        )
        port = RecordingMessagePort()
        apply_conversation_kill_policy(
            store,
            lead_id=lead_id,
            action=NextAction.STOP.value,
        )
        db.commit()
        assert port.sent == []
        assert store.is_conversation_killed(lead_id) is True
        source = inspect.getsource(apply_conversation_kill_policy)
        assert "MessagePort" not in source
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_does_not_set_conversation_killed() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509994104"
        )
        store.set_conversation_killed(lead_id, True)
        db.commit()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.conv_kill.1",
                    "from": OWNER_CONV_KILL_PHONE,
                    "text": "Schedule a follow-up tomorrow.",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_CONV_KILL_PHONE},
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert store.is_conversation_killed(lead_id) is True
        identity = db.scalars(
            select(ChannelIdentityRow).where(
                ChannelIdentityRow.external_id == OWNER_CONV_KILL_PHONE
            )
        ).first()
        assert identity is None
    finally:
        db.close()
