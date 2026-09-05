import importlib
import inspect
import json

import pytest
from app.api.inbound import process_inbound_texts
from app.db.models import AiRunRow, CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import (
    Channel,
    EventType,
    sanitize_correlation_id,
    sanitize_payload_version,
)
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort
from app.integrations.sheets import DisabledSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

PROSPECT_PHONE = "972509996201"
OWNER_PHONE = "972509996202"
PAYLOAD_PHONE = "972509996211"
PAYLOAD_EVENT = "wamid.payload.ver.1"
VISITOR_TEXT = "hi"


def test_sanitize_correlation_id() -> None:
    assert sanitize_correlation_id("run_abc123") == "run_abc123"
    assert sanitize_correlation_id("x y") == ""
    assert sanitize_correlation_id("") == ""
    assert sanitize_correlation_id("   ") == ""


def test_sanitize_payload_version() -> None:
    assert sanitize_payload_version("1") == "1"
    assert sanitize_payload_version("") == ""
    assert sanitize_payload_version("  ") == ""
    assert sanitize_payload_version("2") == ""
    assert sanitize_payload_version("1\n") == ""
    assert sanitize_payload_version("v1") == ""


@pytest.mark.asyncio
async def test_prospect_inbound_canonical_payload_version_is_one() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": PAYLOAD_EVENT, "from": PAYLOAD_PHONE, "text": VISITOR_TEXT}],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id=PAYLOAD_EVENT
        )
        assert in_row is not None
        assert in_row.event_type == EventType.MESSAGE_IN.value
        assert in_row.payload_version == "1"
        payload = json.loads(in_row.payload_json)
        assert "payload_version" not in payload
        store.mark_webhook(
            provider="whatsapp",
            provider_event_id=PAYLOAD_EVENT,
            status="failed",
        )
        db.commit()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": PAYLOAD_EVENT, "from": PAYLOAD_PHONE, "text": VISITOR_TEXT}],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        in_row_after = store.get_canonical_event(
            provider="whatsapp", provider_event_id=PAYLOAD_EVENT
        )
        assert in_row_after is not None
        assert in_row_after.payload_version == "1"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_whatsapp_inbound_shares_correlation_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        event_id = "wamid.corr.prospect.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": event_id, "from": PROSPECT_PHONE, "text": VISITOR_TEXT}],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP,
            external_id=PROSPECT_PHONE,
        )
        ai_row = db.scalars(select(AiRunRow).where(AiRunRow.lead_id == lead_id)).one()
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type.in_(
                        [
                            EventType.MESSAGE_IN.value,
                            EventType.MESSAGE_OUT.value,
                            EventType.TOOL_RESULT.value,
                            EventType.QUALIFICATION_UPDATED.value,
                        ]
                    ),
                )
            ).all()
        )
        assert rows
        assert all(row.correlation_id == ai_row.run_id for row in rows)
        assert ai_row.run_id.startswith("run_")
        in_row = store.get_canonical_event(provider="whatsapp", provider_event_id=event_id)
        out_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id=f"{event_id}:out"
        )
        assert in_row is not None and out_row is not None
        assert in_row.correlation_id == out_row.correlation_id == ai_row.run_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_message_in_out_share_correlation_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        event_id = "wamid.corr.owner.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": event_id, "from": OWNER_PHONE, "text": "daily brief"}],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(provider="whatsapp", provider_event_id=event_id)
        out_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id=f"{event_id}:out"
        )
        assert in_row is not None and out_row is not None
        assert in_row.correlation_id
        assert in_row.correlation_id == out_row.correlation_id
        assert in_row.correlation_id.startswith("cor_")
        owner_perm = store.get_tool_run(
            f"owner:{OWNER_PHONE}:tool:owner_permissions"
        )
        assert owner_perm is not None
        assert owner_perm.correlation_id == in_row.correlation_id
    finally:
        db.close()


def test_website_message_in_and_out_share_correlation_without_ai_run() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": VISITOR_TEXT},
        )
        assert response.status_code == 200
        assert response.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        assert (
            db.scalars(select(AiRunRow).where(AiRunRow.lead_id == session_id)).all() == []
        )
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type.in_(
                        [EventType.MESSAGE_IN.value, EventType.MESSAGE_OUT.value]
                    ),
                )
            ).all()
        )
        visitor_in = [
            row
            for row in rows
            if row.event_type == EventType.MESSAGE_IN.value
            and json.loads(row.payload_json).get("text") == VISITOR_TEXT
        ]
        visitor_out = [row for row in rows if row.event_type == EventType.MESSAGE_OUT.value]
        assert len(visitor_in) == 1
        assert len(visitor_out) == 1
        assert visitor_in[0].correlation_id.startswith("run_")
        assert visitor_in[0].correlation_id == visitor_out[0].correlation_id
    finally:
        db.close()


def test_events_module_has_no_message_port_import() -> None:
    events_module = importlib.import_module("app.domain.events")
    source = inspect.getsource(events_module)
    assert "MessagePort" not in source
