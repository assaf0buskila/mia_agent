import hashlib
import hmac
import json

import httpx
import pytest
from app.api.deps import get_whatsapp_port
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.extract import extract_sales_signals
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState, select_next_action
from app.domain.tools import AdapterHttpError
from app.graph.orchestrator import build_graph
from app.graph.state import empty_state
from app.integrations.base import OutboundMessage, RecordingMessagePort
from app.integrations.whatsapp import WhatsAppCloudPort, WhatsAppSendError
from app.main import app
from fastapi.testclient import TestClient

from tests.unit.sales_copy import assert_discovery_reply


def test_extract_marks_missed_calls_as_workflow_pain() -> None:
    state = SalesState(lead_id="lead_x")
    updated = extract_sales_signals(
        state, "We run a clinic and miss calls all day on WhatsApp."
    )
    assert updated.workflow_known is True
    assert updated.pain_level >= PainLevel.P2
    assert updated.impact_confirmed is True
    assert select_next_action(updated) != NextAction.UNDERSTAND_WORKFLOW


def test_extract_hebrew_inquiries_sets_workflow() -> None:
    state = SalesState(lead_id="lead_pnioyot")
    updated = extract_sales_signals(state, "פניות")
    assert updated.workflow_known is True
    assert select_next_action(updated) == NextAction.DEEPEN_PAIN


def test_extract_shoe_inventory_then_sheets_reaches_whatsapp_on_website() -> None:
    state = SalesState(lead_id="lead_shoes")
    greeting = extract_sales_signals(state, "היי")
    assert greeting.workflow_known is False
    assert select_next_action(greeting, channel="website") == NextAction.UNDERSTAND_WORKFLOW

    inventory = extract_sales_signals(
        greeting, "אני מוכר נעליים יש לי עיסוק רק במלאי"
    )
    assert inventory.workflow_known is True
    assert inventory.pain_level == PainLevel.P1
    assert inventory.fit == FitLevel.POSSIBLE
    assert select_next_action(inventory, channel="website") == NextAction.DEEPEN_PAIN
    assert select_next_action(inventory) == NextAction.DEEPEN_PAIN

    sheets = extract_sales_signals(inventory, "להכניס הכל לשיטס")
    assert sheets.pain_level >= PainLevel.P2
    assert sheets.manual_step_known is True
    # The manual step is known but nothing about volume is. Ask, do not hand off yet.
    assert select_next_action(sheets, channel="website") == NextAction.QUANTIFY
    assert select_next_action(sheets) == NextAction.QUANTIFY

    sizes = extract_sales_signals(sheets, "נעליים מידות דגמים")
    assert select_next_action(sizes, channel="website") == NextAction.OFFER_WHATSAPP
    # WhatsApp is a website affordance only; other channels keep the funnel.
    assert select_next_action(sizes) != NextAction.OFFER_WHATSAPP


def test_extract_prelaunch_website_intent_offers_whatsapp() -> None:
    state = SalesState(lead_id="lead_prelaunch")
    greeting = extract_sales_signals(state, "היי")
    assert select_next_action(greeting, channel="website") == NextAction.UNDERSTAND_WORKFLOW
    opened = extract_sales_signals(
        greeting,
        "אני האמת לא עוסק כרגע אני רוצה לפתוח עסק והייתי רוצה אולי לבנות אתר",
    )
    assert opened.workflow_known is True
    assert opened.pain_level >= PainLevel.P2
    assert opened.fit == FitLevel.POSSIBLE
    assert select_next_action(opened, channel="website") == NextAction.OFFER_WHATSAPP
    clothes = extract_sales_signals(
        opened, "עסק של בגדים ושהאתר יביא לי לידים כמובן"
    )
    assert clothes.workflow_known is True
    long_chat = extract_sales_signals(
        SalesState(lead_id="lead_long"), "וואי זה ארוך השיחה"
    )
    assert long_chat.pain_level >= PainLevel.P2
    assert select_next_action(long_chat, channel="website") == NextAction.OFFER_WHATSAPP
    bye = extract_sales_signals(SalesState(lead_id="lead_bye"), "בי תודה")
    assert bye.willingness_to_meet is False
    assert select_next_action(bye, channel="website") == NextAction.STOP


def test_extract_hebrew_lead_line_sets_workflow_and_pain() -> None:
    state = SalesState(lead_id="lead_he")
    updated = extract_sales_signals(
        state,
        "רוב היום אני עם לקוחות, אז אני לא תמיד עונה לטלפון.",
    )
    assert updated.workflow_known is True
    assert updated.pain_level >= PainLevel.P1
    assert select_next_action(updated) != NextAction.UNDERSTAND_WORKFLOW


def test_extract_hebrew_impact_line_sets_impact_or_pain() -> None:
    state = SalesState(lead_id="lead_he2", workflow_known=True, pain_level=PainLevel.P1)
    updated = extract_sales_signals(
        state,
        "חלק חוזרים אליהם אחר כך, אבל בטוח שחלק נעלמים.",
    )
    assert updated.pain_level >= PainLevel.P2 or updated.impact_confirmed is True


def test_website_second_message_leaves_opening_question() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages", json={"text": "hi"}
        )
        assert first.json()["next_action"] == "ask_contact"
        second = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day on WhatsApp."},
        )
        assert second.status_code == 200
        assert second.json()["next_action"] == "ask_contact"


def test_graph_persists_extracted_sales_state() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_prog")
        db.commit()
        build_graph(store).invoke(
            empty_state(
                run_id="run_2",
                thread_id="web_prog",
                channel="website",
                lead_id=lead_id,
                latest_message="We run a clinic and miss calls all day on WhatsApp.",
            )
        )
        db.commit()
        sales = store.get_sales(lead_id)
        assert sales.workflow_known is True
        assert sales.pain_level >= PainLevel.P2
        # One message of context is not enough to hand off yet.
        assert sales.whatsapp_handoff_offered is False
        assert sales.discovery_turns == 1
        assert sales.reflected is False
    finally:
        db.close()


def test_whatsapp_verify_and_inbound_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "972501234567",
                                    "id": "wamid.1",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        challenge = client.get(
            "/v1/whatsapp/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "abc",
            },
        )
        assert challenge.status_code == 200
        assert challenge.text == "abc"
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={digest}",
        }
        first = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
        assert first.status_code == 200
        assert first.json()["processed"] == 1
        assert first.json()["sent"] is False
        second = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
        assert second.json()["duplicates"] == 1
        assert second.json()["processed"] == 0


def _signed_whatsapp_payload(*, message_id: str = "wamid.1") -> tuple[bytes, dict[str, str]]:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "972501234567",
                                    "id": message_id,
                                    "type": "text",
                                    "text": {"body": "hi"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    return raw, headers


def test_whatsapp_inbound_sends_via_recording_port(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    raw, headers = _signed_whatsapp_payload(message_id="wamid.recording.1")
    try:
        with TestClient(app) as client:
            first = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert first.status_code == 200
            body = first.json()
            assert body["processed"] == 1
            assert body["sent"] is True
            assert body["sent_count"] == 1
            assert len(recorder.sent) == 1
            message = recorder.sent[0]
            assert message.conversation_id == "972501234567"
            assert message.reply_to_id == "wamid.recording.1"
            assert_discovery_reply(message.text)
            second = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert second.json()["duplicates"] == 1
            assert second.json()["sent"] is False
            assert len(recorder.sent) == 1
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)


def test_whatsapp_kill_switch_skips_processing(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    raw, headers = _signed_whatsapp_payload(message_id="wamid.killed.1")
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["killed"] is True
            assert body["processed"] == 0
            assert body["sent"] is False
            assert recorder.sent == []
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        monkeypatch.delenv("MIA_KILL_SWITCH", raising=False)


@pytest.mark.asyncio
async def test_whatsapp_cloud_port_posts_text_message() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppCloudPort(
        access_token="secret-token-value",
        phone_number_id="phone-123",
        graph_version="v25.0",
        client=client,
    )
    await port.send(
        OutboundMessage(
            conversation_id="972501234567",
            text="hello",
            channel="whatsapp",
            idempotency_key="wamid.1",
            reply_to_id="wamid.1",
        )
    )
    await client.aclose()

    assert "/v25.0/phone-123/messages" in str(captured["url"])
    assert captured["authorization"] == "Bearer secret-token-value"
    body = captured["body"]
    assert body["type"] == "text"
    assert body["text"]["body"] == "hello"
    assert body["context"] == {"message_id": "wamid.1"}


@pytest.mark.asyncio
async def test_whatsapp_cloud_port_error_omits_token() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": {"message": "Invalid OAuth"}})
    )
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppCloudPort(
        access_token="secret-token-value",
        phone_number_id="phone-123",
        graph_version="v25.0",
        client=client,
    )
    with pytest.raises(WhatsAppSendError, match="HTTP 401") as exc_info:
        await port.send(
            OutboundMessage(
                conversation_id="972501234567",
                text="hello",
                channel="whatsapp",
                idempotency_key="wamid.1",
            )
        )
    assert "secret-token-value" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 401
    assert cause.tool_status() == "unauthorized"
    await client.aclose()


@pytest.mark.asyncio
async def test_whatsapp_cloud_port_http_429_is_rate_limited() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(429, json={"error": {"message": "Rate limit"}})
    )
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppCloudPort(
        access_token="secret-token-value",
        phone_number_id="phone-123",
        graph_version="v25.0",
        client=client,
    )
    with pytest.raises(WhatsAppSendError, match="HTTP 429") as exc_info:
        await port.send(
            OutboundMessage(
                conversation_id="972501234567",
                text="hello",
                channel="whatsapp",
                idempotency_key="wamid.1",
            )
        )
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 429
    assert cause.tool_status() == "rate_limited"
    await client.aclose()


@pytest.mark.asyncio
async def test_whatsapp_cloud_port_network_error_raises_send_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppCloudPort(
        access_token="secret-token-value",
        phone_number_id="phone-123",
        graph_version="v25.0",
        client=client,
    )
    with pytest.raises(WhatsAppSendError, match="WhatsApp Cloud API send failed$") as exc_info:
        await port.send(
            OutboundMessage(
                conversation_id="972501234567",
                text="hello",
                channel="whatsapp",
                idempotency_key="wamid.1",
            )
        )
    assert "secret-token-value" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code is None
    assert cause.tool_status() == "retryable"
    await client.aclose()


class _FailingWhatsAppPort:
    async def send(self, message: OutboundMessage) -> None:
        raise WhatsAppSendError("WhatsApp Cloud API send failed: HTTP 502")


def test_whatsapp_send_failure_rolls_back_claim_for_retry(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    app.dependency_overrides[get_whatsapp_port] = lambda: _FailingWhatsAppPort()
    raw, headers = _signed_whatsapp_payload(message_id="wamid.fail.1")
    try:
        with TestClient(app) as client:
            first = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert first.status_code in {200, 502}
            second = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            if first.status_code == 502:
                assert second.status_code == 502
                assert second.json().get("duplicates") != 1
            else:
                assert second.status_code == 200
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
