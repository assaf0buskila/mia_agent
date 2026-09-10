"""Tests for website conversion tracking, hot lead owner notifications, and Composio guidance."""

from __future__ import annotations

import json

from app.api.deps import get_telegram_port
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory
from app.graph.owner_agent import PROMPT_VERSION, SYSTEM_PROMPT
from app.integrations.base import RecordingMessagePort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select


def test_website_contact_captured_emits_conversion_event_and_pings_owner(monkeypatch) -> None:
    """When a visitor provides phone/email, emit website_conversion event and ping owner."""
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "111")
    port = RecordingMessagePort()
    app.dependency_overrides[get_telegram_port] = lambda: port
    try:
        with TestClient(app) as client:
            session_resp = client.post("/v1/website/sessions")
            assert session_resp.status_code == 200
            session_id = session_resp.json()["session_id"]

            # Initial greeting / inquiry
            first = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "שלום, אני צריך סוכן AI לניהול תורים"},
            )
            assert first.status_code == 200
            assert first.json()["lead_id"] == ""

            # Visitor provides contact details (Conversion moment)
            conv = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={
                    "text": "מעוניין בהקמה, קוראים לי ישראל",
                    "name": "ישראל ישראלי",
                    "phone": "050-1234567",
                    "email": "israel@example.com",
                },
            )
            assert conv.status_code == 200
            assert conv.json()["lead_id"] == ""

            # Follow-up message in same session
            follow = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "מתי אסף יכול לחזור אליי?"},
            )
            assert follow.status_code == 200

        # Verify behavior events in database
        db = get_session_factory()()
        try:
            events = db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "behavior",
                )
            ).all()
            kinds = [json.loads(row.payload_json).get("kind") for row in events]
            assert "conversation_started" in kinds
            assert "website_conversion" in kinds
            # Exactly one conversion event emitted per session
            assert kinds.count("website_conversion") == 1

            # Verify owner notification contains the hot lead indicator
            assert port.sent
            assert any("ליד חדש מהאתר — ליד חם" in msg.text for msg in port.sent)
            assert any("0501234567" in msg.text for msg in port.sent)
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_website_message_without_contact_does_not_emit_conversion() -> None:
    """Exploratory messages without contact info do NOT emit website_conversion."""
    with TestClient(app) as client:
        session_resp = client.post("/v1/website/sessions")
        assert session_resp.status_code == 200
        session_id = session_resp.json()["session_id"]

        msg_resp = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "מה המחיר של אתר תדמית?"},
        )
        assert msg_resp.status_code == 200

    db = get_session_factory()()
    try:
        events = db.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.conversation_id == session_id,
                CanonicalEventRow.event_type == "behavior",
            )
        ).all()
        kinds = [json.loads(row.payload_json).get("kind") for row in events]
        assert "website_conversion" not in kinds
    finally:
        db.close()


def test_composio_prompt_instructions() -> None:
    """Verify owner agent prompt instructions for Composio free discovery & approval."""
    assert PROMPT_VERSION == "owner_agent_v8"
    assert "composio_search_tools" in SYSTEM_PROMPT
    assert "composio_get_tool_schema" in SYSTEM_PROMPT
    assert "composio_execute_tool" in SYSTEM_PROMPT
    assert "composio_propose_action" in SYSTEM_PROMPT
    assert "Telegram approval" in SYSTEM_PROMPT
    assert "cannot send a message, book, approve, pay, publish" in SYSTEM_PROMPT
    assert (
        "LinkedIn side effects and Composio writes require Assaf's explicit Telegram approval"
        in SYSTEM_PROMPT
    )
