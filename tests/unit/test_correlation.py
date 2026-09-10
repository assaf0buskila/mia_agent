import importlib
import inspect
import json

import pytest
from app.api.owner import process_owner_texts as process_inbound_texts
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
async def test_retired_whatsapp_prospect_inbound_is_ignored() -> None:
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
        assert store.get_canonical_event(
            provider="whatsapp", provider_event_id=PAYLOAD_EVENT
        ) is None
        assert port.sent == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_retired_whatsapp_prospect_inbound_creates_no_ai_run() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        before = {
            row.provider_event_id for row in db.scalars(select(CanonicalEventRow)).all()
        }
        before_ai_runs = {row.id for row in db.scalars(select(AiRunRow)).all()}
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
        after_ai_runs = {row.id for row in db.scalars(select(AiRunRow)).all()}
        assert after_ai_runs == before_ai_runs
        after = {
            row.provider_event_id for row in db.scalars(select(CanonicalEventRow)).all()
        }
        assert after == before
        assert port.sent == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_message_in_out_share_correlation_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        event_id = "tg.corr.owner.1"
        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": event_id, "from": OWNER_PHONE, "text": "daily brief"}],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(provider="telegram", provider_event_id=event_id)
        out_row = store.get_canonical_event(
            provider="telegram", provider_event_id=f"{event_id}:out"
        )
        assert in_row is not None and out_row is not None
        assert in_row.correlation_id
        assert in_row.correlation_id == out_row.correlation_id
        assert in_row.correlation_id.startswith("cor_")
    finally:
        db.close()


def test_website_message_in_and_out_share_correlation_without_ai_run() -> None:
    init_db()
    with TestClient(app) as client:
        session = client.post("/v1/website/sessions").json()
        session_id = session["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": VISITOR_TEXT, "client_message_id": "correlation-website-1"},
            headers={"X-Mia-Session-Credential": session["session_credential"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        assert db.scalars(select(AiRunRow).where(AiRunRow.lead_id == session_id)).all() == []
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
        visitor_out = [
            row
            for row in rows
            if row.event_type == EventType.MESSAGE_OUT.value
            and row.provider_event_id == f"{session_id}:v2:correlation-website-1:out"
        ]
        assert len(visitor_in) == 1
        assert len(visitor_out) == 1
        assert visitor_in[0].correlation_id == visitor_out[0].correlation_id == ""
    finally:
        db.close()


def test_events_module_has_no_message_port_import() -> None:
    events_module = importlib.import_module("app.domain.events")
    source = inspect.getsource(events_module)
    assert "MessagePort" not in source
