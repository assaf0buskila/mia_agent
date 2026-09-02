import json

import pytest
from app.api.deps import get_sheets_port
from app.api.inbound import process_inbound_texts
from app.core.config import MiaEnv, Settings
from app.core.demo import SCRIPTED_MESSAGES, demo_mode_active
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.integrations.base import RecordingMessagePort
from app.integrations.sheets import FakeSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select


def test_health_demo_false_by_default() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["demo"] is False


def test_demo_mode_active_false_in_prod() -> None:
    settings = Settings(env=MiaEnv.PROD, demo_mode=True)
    assert demo_mode_active(settings) is False


def test_demo_mode_active_true_when_flag_on_non_prod() -> None:
    settings = Settings(env=MiaEnv.TEST, demo_mode=True)
    assert demo_mode_active(settings) is True
    settings_dev = Settings(env=MiaEnv.DEV, demo_mode=True)
    assert demo_mode_active(settings_dev) is True


def test_demo_mode_active_false_when_flag_off() -> None:
    settings = Settings(env=MiaEnv.TEST, demo_mode=False)
    assert demo_mode_active(settings) is False


def test_demo_endpoints_404_when_inactive() -> None:
    with TestClient(app) as client:
        assert client.get("/v1/demo/status").status_code == 404
        assert client.post("/v1/demo/scripted").status_code == 404


def test_demo_status_when_active(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    with TestClient(app) as client:
        response = client.get("/v1/demo/status")
        assert response.status_code == 200
        assert response.json() == {"active": True, "env": "test", "label": "synthetic"}


def test_demo_scripted_identify_then_sell_when_active(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    init_db()
    with TestClient(app) as client:
        response = client.post("/v1/demo/scripted")
        assert response.status_code == 200
        body = response.json()
        assert body["label"] == "synthetic"
        assert "session_id" in body
        assert body["lead_id"] == ""
        assert len(body["steps"]) == len(SCRIPTED_MESSAGES)
        for step, (text, _old_action) in zip(body["steps"], SCRIPTED_MESSAGES, strict=True):
            assert step["user"] == text
            allowed = {
                "ask_need",
                "ask_contact",
                "handoff",
                "no_price",
                "product_answer",
            }
            assert step["next_action"] in allowed
            assert isinstance(step["message"], str)
            assert step["message"]
        dumped = json.dumps(body)
        assert "email" not in body
        assert "phone" not in body
        assert "@" not in dumped
        session_id = body["session_id"]
    db = get_session_factory()()
    try:
        attr_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "attribution",
                )
            )
        )
        assert attr_rows == []
        tool_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "tool_result",
                )
            )
        )
        sheets_tools = [
            row for row in tool_rows
            if json.loads(row.payload_json).get("tool") == "sheets_mirror"
        ]
        assert len(sheets_tools) == 0
    finally:
        db.close()


def test_website_config_demo_true_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    with TestClient(app) as client:
        body = client.get("/v1/website/config").json()
    assert body["demo"] is True


def test_demo_session_does_not_stamp_attribution(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    init_db()
    with TestClient(app) as client:
        created = client.post(
            "/v1/website/sessions",
            params={"utm_source": "meta", "utm_campaign": "yuma"},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert created.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        attr_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "attribution",
                )
            )
        )
        assert attr_rows == []
    finally:
        db.close()


def test_demo_session_skips_source_mirror(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    init_db()
    fake = FakeSheetsPort()
    app.dependency_overrides[get_sheets_port] = lambda: fake
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/website/sessions",
                params={"utm_source": "meta", "utm_campaign": "yuma"},
            )
            assert response.status_code == 200
        assert fake.source_rows == {}
        assert fake.kpi_rows == {}
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)


def test_demo_message_skips_sheets_mirror(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    init_db()
    fake = FakeSheetsPort()
    app.dependency_overrides[get_sheets_port] = lambda: fake
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "hi"},
            )
        assert fake.rows == {}
        assert fake.source_rows == {}
        assert fake.activity_rows == {}
        assert fake.kpi_rows == {}
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)
    db = get_session_factory()()
    try:
        tool_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "tool_result",
                )
            )
        )
        sheets_tools = [
            row for row in tool_rows
            if json.loads(row.payload_json).get("tool") == "sheets_mirror"
        ]
        assert len(sheets_tools) == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_skips_sheets_mirror_when_demo_active(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        result = await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[
                {
                    "id": "evt.demo.sheet.skip.1",
                    "from": "demo.lead.skip.1@example.invalid",
                    "text": "hello",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            sheets=sheets,
        )
        db.commit()
        assert result["processed"] == 1
        assert sheets.rows == {}
        assert sheets.activity_rows == {}
        tool_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type == "tool_result",
                    CanonicalEventRow.provider_event_id
                    == "evt.demo.sheet.skip.1:tool:sheets_mirror",
                )
            )
        )
        assert tool_rows == []
    finally:
        db.close()
