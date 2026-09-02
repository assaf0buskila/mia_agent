"""Phase 4: the verified website-handoff send exemption must stay narrow.

`MIA_WHATSAPP_HANDOFF_SEND` is the one switch that lets Mia answer on WhatsApp
while the rest of production stays in shadow. These tests exist to prove the
switch cannot become a general WhatsApp opener: it only applies to a WhatsApp
conversation whose scope was set by consuming a valid website handoff token, and
every other guard still runs. The switch itself stays off by default.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.conversation_scope import MIA_INTRO_HE, AutomationScope
from app.domain.events import Channel
from app.domain.shadow import should_skip_prospect_send
from app.integrations.base import OutboundMessage, RecordingMessagePort
from app.integrations.sheets import FakeSheetsPort
from app.main import app
from fastapi.testclient import TestClient

_SEND_PHONE = "972508170030"
_UNKNOWN_PHONE = "972508170031"
_FAIL_PHONE = "972508170032"
_TWICE_PHONE = "972508170033"


class _FailingPort(RecordingMessagePort):
    async def send(self, message: OutboundMessage) -> None:
        raise RuntimeError("provider rejected the message")


def _handoff_env(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE", "true")
    monkeypatch.setenv("MIA_AUTOMATION_MODE", "shadow")
    monkeypatch.setenv("MIA_WHATSAPP_HANDOFF_SEND", "true")


def _issue_token() -> str:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        identify = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "אני מוכר נעליים ומזין הכל ידנית לשיטס",
                "phone": "0501234567",
            },
        )
        assert identify.status_code == 200
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert handoff.status_code == 200
        return handoff.json()["token"]


def test_handoff_send_is_off_by_default() -> None:
    """Production stays in shadow until Assaf flips this one switch."""
    assert Settings().whatsapp_handoff_send is False


def test_shadow_exemption_requires_whatsapp_and_business_scope() -> None:
    from app.core.config import AutomationMode

    business = AutomationScope.MIA_BUSINESS.value
    assert (
        should_skip_prospect_send(
            AutomationMode.SHADOW,
            "prospect",
            channel=Channel.WHATSAPP.value,
            automation_scope=business,
            whatsapp_handoff_send=True,
        )
        is False
    )
    # Same flag, wrong channel: still staged.
    assert (
        should_skip_prospect_send(
            AutomationMode.SHADOW,
            "prospect",
            channel=Channel.WEBSITE.value,
            automation_scope=business,
            whatsapp_handoff_send=True,
        )
        is True
    )
    # Same flag, WhatsApp, but no verified business scope: still staged.
    for scope in ("", AutomationScope.PERSONAL.value, AutomationScope.DO_NOT_AUTOMATE.value):
        assert (
            should_skip_prospect_send(
                AutomationMode.SHADOW,
                "prospect",
                channel=Channel.WHATSAPP.value,
                automation_scope=scope,
                whatsapp_handoff_send=True,
            )
            is True
        ), scope


@pytest.mark.asyncio
async def test_verified_handoff_sends_and_introduces_mia_once(monkeypatch) -> None:
    _handoff_env(monkeypatch)
    init_db()
    token = _issue_token()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        first = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "wamid.send.1", "from": _SEND_PHONE, "text": token}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        assert first["sent"] is True
        assert len(port.sent) == 1
        assert MIA_INTRO_HE in port.sent[0].text
        second = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.send.2",
                    "from": _SEND_PHONE,
                    "text": "כל יום שעה בערך",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        assert second["sent"] is True
        assert len(port.sent) == 2
        # Introduce once. A second hello is how a continuation stops feeling like one.
        assert MIA_INTRO_HE not in port.sent[1].text
        assert port.sent[1].text != port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unknown_contact_stays_silent_even_with_the_flag_on(monkeypatch) -> None:
    _handoff_env(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        result = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.unknown.1",
                    "from": _UNKNOWN_PHONE,
                    "text": "היי מה אתם מציעים",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        assert result["sent"] is False
        assert port.sent == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unknown_contact_stays_silent_in_auto_approved_mode(monkeypatch) -> None:
    """The mode production actually runs (ADR-022), not just shadow.

    Outside shadow `should_skip_prospect_send` stops skipping, so silence for a
    stranger has to come from the business-scope gate rather than from the mode.
    """
    monkeypatch.setenv("MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE", "true")
    monkeypatch.setenv("MIA_AUTOMATION_MODE", "auto_approved")
    monkeypatch.setenv("MIA_WHATSAPP_HANDOFF_SEND", "false")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        result = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.auto.unknown.1",
                    "from": "972508170034",
                    "text": "היי מה המחיר שלכם",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        assert result["sent"] is False
        assert port.sent == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_do_not_automate_scope_stays_silent_in_auto_approved_mode(
    monkeypatch,
) -> None:
    """A contact Assaf marked personal is never answered, whatever the mode."""
    monkeypatch.setenv("MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE", "true")
    monkeypatch.setenv("MIA_AUTOMATION_MODE", "auto_approved")
    init_db()
    token = _issue_token()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        phone = "972508170035"
        # Consume a real handoff so the conversation is genuinely business scoped,
        # then have Assaf override it. The override must win.
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "wamid.scope.1", "from": phone, "text": token}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        store.upsert_conversation_control(
            channel=Channel.WHATSAPP.value,
            external_id=phone,
            automation_scope=AutomationScope.DO_NOT_AUTOMATE.value,
            source="owner",
        )
        db.commit()
        before = len(port.sent)
        result = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "wamid.scope.2", "from": phone, "text": "כל יום שעה"}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        assert result["sent"] is False
        assert len(port.sent) == before
    finally:
        db.close()


@pytest.mark.asyncio
async def test_kill_switch_still_blocks_a_verified_handoff(monkeypatch) -> None:
    """The handoff flag does not buy an exemption from the kill switch.

    The webhook route returns before it reaches here when the switch is on, so this
    is the backstop: processing aborts loudly and nothing reaches the provider.
    """
    _handoff_env(monkeypatch)
    init_db()
    token = _issue_token()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        with pytest.raises(PolicyDenied):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{"id": "wamid.kill.1", "from": _TWICE_PHONE, "text": token}],
                store=store,
                port=port,
                kill_switch=True,
                sheets=FakeSheetsPort(),
            )
        assert port.sent == []
    finally:
        db.close()


def test_whatsapp_route_returns_before_processing_when_killed(monkeypatch) -> None:
    """The live route never reaches the backstop, so a kill switch is not a 500."""
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.route.kill.1",
                                    "from": _SEND_PHONE,
                                    "type": "text",
                                    "text": {"body": "היי"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        response = client.post(
            "/v1/whatsapp/webhook",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={digest}",
            },
        )
    assert response.status_code == 200
    assert response.json()["sent"] is False


@pytest.mark.asyncio
async def test_provider_failure_is_not_reported_as_sent(monkeypatch) -> None:
    _handoff_env(monkeypatch)
    init_db()
    token = _issue_token()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = _FailingPort()
        result = await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "wamid.fail.1", "from": _FAIL_PHONE, "text": token}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        assert result["sent"] is False
        webhook = store.get_webhook(
            provider="whatsapp", provider_event_id="wamid.fail.1"
        )
        assert webhook is not None
        assert webhook.status == "processed"
    finally:
        db.close()
