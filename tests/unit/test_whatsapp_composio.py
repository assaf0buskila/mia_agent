"""WhatsApp ADR-016: Meta inbound, one Composio-or-Graph sender, no dual-send."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest
from app.api.composio import composio_user_matches, extract_trigger_slug
from app.api.deps import get_whatsapp_port
from app.api.whatsapp import parse_inbound_audio, parse_inbound_texts
from app.core.config import AutomationMode, Settings
from app.core.outbound import send_inbound_reply
from app.db.session import init_db
from app.domain.tools import AdapterHttpError
from app.integrations.base import DisabledMessagePort, OutboundMessage, RecordingMessagePort
from app.integrations.whatsapp import (
    COMPOSIO_SEND_TEMPLATE_TOOL,
    COMPOSIO_SEND_TEXT_TOOL,
    COMPOSIO_WHATSAPP_VERSION,
    WHATSAPP_INBOUND_TRIGGER_SLUGS,
    WHATSAPP_STATUS_TRIGGER,
    ComposioWhatsAppPort,
    WhatsAppCloudPort,
    WhatsAppMediaPort,
    WhatsAppSendError,
    build_whatsapp_media_port,
    build_whatsapp_port,
    parse_composio_whatsapp_inbound,
    whatsapp_template_send_allowed,
)
from app.main import app
from app.tools.registries.mia_preloaded_tools import PRELOADED_TOOL_NAMES
from fastapi.testclient import TestClient

COMPOSIO_SECRET = "composio-webhook-secret"


def _sign_composio_payload(
    payload: dict,
    *,
    secret: str = COMPOSIO_SECRET,
    webhook_id: str = "wh_wa",
    timestamp: int | None = None,
) -> tuple[bytes, dict[str, str]]:
    ts = timestamp if timestamp is not None else int(time.time())
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signed = f"{webhook_id}.{ts}.".encode() + raw
    digest = base64.b64encode(
        hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    ).decode("ascii")
    headers = {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": f"v1,{digest}",
    }
    return raw, headers


def test_status_trigger_normalizes_to_no_inbound() -> None:
    payload = {
        "metadata": {"trigger_slug": WHATSAPP_STATUS_TRIGGER, "user_id": "user-1"},
        "data": {"message_id": "wamid.x", "delivery_status": "delivered"},
    }
    assert extract_trigger_slug(payload) == WHATSAPP_STATUS_TRIGGER
    assert parse_composio_whatsapp_inbound(payload) == []


def test_unknown_whatsapp_slug_is_not_inbound() -> None:
    payload = {
        "metadata": {"trigger_slug": "WHATSAPP_NEW_MESSAGE"},
        "data": {"from": "972501234567", "text": "hi", "id": "wamid.fake"},
    }
    assert parse_composio_whatsapp_inbound(payload) == []


def test_meta_inbound_text_parser_unchanged() -> None:
    payload = {
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
        ]
    }
    items = parse_inbound_texts(payload)
    assert items == [{"id": "wamid.1", "from": "972501234567", "text": "hi"}]


def test_voice_note_parser_still_meta() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "972501234567",
                                    "id": "wamid.audio.1",
                                    "type": "audio",
                                    "audio": {"id": "media-1", "mime_type": "audio/ogg"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    items = parse_inbound_audio(payload)
    assert items[0]["media_id"] == "media-1"
    assert items[0]["id"] == "wamid.audio.1"


def test_build_port_composio_not_graph_when_both_creds(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_SENDER", "composio")
    monkeypatch.setenv("MIA_COMPOSIO_API_KEY", "cmp-key")
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "user-abc")
    monkeypatch.setenv("MIA_WHATSAPP_ACCESS_TOKEN", "graph-token")
    monkeypatch.setenv("MIA_WHATSAPP_PHONE_NUMBER_ID", "109999")
    port = build_whatsapp_port(Settings())
    assert isinstance(port, ComposioWhatsAppPort)
    assert not isinstance(port, WhatsAppCloudPort)


def test_build_port_direct_keeps_graph_when_composio_keys(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_SENDER", "direct")
    monkeypatch.setenv("MIA_COMPOSIO_API_KEY", "cmp-key")
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "user-abc")
    monkeypatch.setenv("MIA_WHATSAPP_ACCESS_TOKEN", "graph-token")
    monkeypatch.setenv("MIA_WHATSAPP_PHONE_NUMBER_ID", "109999")
    port = build_whatsapp_port(Settings())
    assert isinstance(port, WhatsAppCloudPort)


def test_build_port_composio_without_user_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_SENDER", "composio")
    monkeypatch.setenv("MIA_COMPOSIO_API_KEY", "cmp-key")
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "")
    monkeypatch.setenv("MIA_WHATSAPP_PHONE_NUMBER_ID", "109999")
    monkeypatch.setenv("MIA_WHATSAPP_ACCESS_TOKEN", "graph-token")
    port = build_whatsapp_port(Settings())
    assert isinstance(port, DisabledMessagePort)


def test_build_port_invalid_sender_falls_back_to_direct(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_SENDER", "both")
    monkeypatch.setenv("MIA_WHATSAPP_ACCESS_TOKEN", "graph-token")
    monkeypatch.setenv("MIA_WHATSAPP_PHONE_NUMBER_ID", "109999")
    port = build_whatsapp_port(Settings())
    assert isinstance(port, WhatsAppCloudPort)


def test_media_port_stays_graph_when_send_is_composio(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_SENDER", "composio")
    monkeypatch.setenv("MIA_WHATSAPP_ACCESS_TOKEN", "graph-token")
    media = build_whatsapp_media_port(Settings())
    assert isinstance(media, WhatsAppMediaPort)


def test_template_send_denied() -> None:
    assert whatsapp_template_send_allowed() is False
    assert COMPOSIO_SEND_TEMPLATE_TOOL == "WHATSAPP_SEND_TEMPLATE_MESSAGE"


@pytest.mark.asyncio
async def test_composio_whatsapp_send_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": {"messages": [{"id": "wamid.out"}]}, "successful": True}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    port = ComposioWhatsAppPort(
        api_key="cmp-test",
        user_id="user-abc",
        phone_number_id="109999",
        client=client,
    )
    await port.send(
        OutboundMessage(
            conversation_id="972501234567",
            text="hello",
            channel="whatsapp",
            idempotency_key="wamid.1",
            reply_to_id="wamid.orig",
        )
    )
    assert str(captured["url"]).endswith(f"/{COMPOSIO_SEND_TEXT_TOOL}")
    assert COMPOSIO_SEND_TEMPLATE_TOOL not in str(captured["url"])
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["user_id"] == "user-abc"
    assert body["version"] == COMPOSIO_WHATSAPP_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["phone_number_id"] == "109999"
    assert arguments["to_number"] == "972501234567"
    assert arguments["text"] == "hello"
    assert arguments["message_id"] == "wamid.orig"
    serialized = json.dumps(body)
    assert COMPOSIO_SEND_TEMPLATE_TOOL not in serialized
    assert "cmp-test" not in serialized
    await client.aclose()


@pytest.mark.asyncio
async def test_composio_whatsapp_send_http_401_raises() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401))
    )
    port = ComposioWhatsAppPort(
        api_key="secret-cmp-key",
        user_id="user-abc",
        phone_number_id="109999",
        client=client,
    )
    with pytest.raises(WhatsAppSendError, match="Composio send failed: HTTP 401") as exc_info:
        await port.send(
            OutboundMessage(
                conversation_id="972501234567",
                text="hello",
                channel="whatsapp",
                idempotency_key="wamid.1",
            )
        )
    assert "secret-cmp-key" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 401
    await client.aclose()


@pytest.mark.asyncio
async def test_composio_whatsapp_send_unsuccessful_body_raises() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"successful": False, "error": "nope"})
        )
    )
    port = ComposioWhatsAppPort(
        api_key="k",
        user_id="user-abc",
        phone_number_id="109999",
        client=client,
    )
    with pytest.raises(WhatsAppSendError):
        await port.send(
            OutboundMessage(
                conversation_id="972501234567",
                text="hello",
                channel="whatsapp",
                idempotency_key="wamid.1",
            )
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_shadow_blocks_prospect_composio_send() -> None:
    sent: list[OutboundMessage] = []

    class _Probe(ComposioWhatsAppPort):
        async def send(self, message: OutboundMessage) -> None:
            sent.append(message)

    port = _Probe(api_key="k", user_id="u", phone_number_id="1")
    ok = await send_inbound_reply(
        port=port,
        message=OutboundMessage(
            conversation_id="972501234567",
            text="hi",
            channel="whatsapp",
            idempotency_key="wamid.1",
        ),
        kill_switch=False,
        automation_mode=AutomationMode.SHADOW,
        actor_role="prospect",
    )
    assert ok is False
    assert sent == []


@pytest.mark.asyncio
async def test_owner_ack_not_blocked_by_shadow() -> None:
    sent: list[OutboundMessage] = []

    class _Probe:
        async def send(self, message: OutboundMessage) -> None:
            sent.append(message)

    ok = await send_inbound_reply(
        port=_Probe(),
        message=OutboundMessage(
            conversation_id="15555550100",
            text="ack",
            channel="whatsapp",
            idempotency_key="wamid.owner.1",
        ),
        kill_switch=False,
        automation_mode=AutomationMode.SHADOW,
        actor_role="owner",
    )
    assert ok is True
    assert len(sent) == 1


def test_composio_user_mismatch() -> None:
    payload = {"metadata": {"user_id": "other-user", "trigger_slug": "GMAIL_NEW_GMAIL_MESSAGE"}}
    assert composio_user_matches(payload, "user-abc") is False
    assert composio_user_matches(payload, "") is True
    assert composio_user_matches({"metadata": {}}, "user-abc") is True


def test_whatsapp_status_trigger_ignored_by_composio_webhook(monkeypatch) -> None:
    monkeypatch.setenv("MIA_COMPOSIO_WEBHOOK_SECRET", COMPOSIO_SECRET)
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "user-abc")
    init_db()
    payload = {
        "metadata": {
            "trigger_slug": WHATSAPP_STATUS_TRIGGER,
            "user_id": "user-abc",
        },
        "data": {"message_id": "wamid.x"},
    }
    raw, headers = _sign_composio_payload(payload)
    with TestClient(app) as client:
        response = client.post("/v1/composio/webhook", content=raw, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["processed"] == 0
    assert body["ignored"] is True


def test_wrong_composio_user_ignored(monkeypatch) -> None:
    monkeypatch.setenv("MIA_COMPOSIO_WEBHOOK_SECRET", COMPOSIO_SECRET)
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "user-abc")
    payload = {
        "metadata": {
            "trigger_slug": "GMAIL_NEW_GMAIL_MESSAGE",
            "user_id": "someone-else",
        },
        "data": {
            "message_id": "msg_x",
            "sender": "Lead <lead@example.com>",
            "subject": "Hi",
            "message_text": "hello",
        },
    }
    raw, headers = _sign_composio_payload(payload)
    with TestClient(app) as client:
        response = client.post("/v1/composio/webhook", content=raw, headers=headers)
    assert response.json()["ignored"] is True
    assert response.json()["processed"] == 0


def test_invalid_composio_signature_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MIA_COMPOSIO_WEBHOOK_SECRET", COMPOSIO_SECRET)
    raw = b'{"metadata":{"trigger_slug":"WHATSAPP_MESSAGE_STATUS_UPDATED_TRIGGER"}}'
    with TestClient(app) as client:
        response = client.post(
            "/v1/composio/webhook",
            content=raw,
            headers={
                "webhook-id": "wh_bad",
                "webhook-timestamp": str(int(time.time())),
                "webhook-signature": "v1,AAAA",
            },
        )
    assert response.status_code == 401
    assert response.json()["error"] == "webhook_rejected"


def test_meta_duplicate_inbound_still_dedupes(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setenv("MIA_WHATSAPP_SENDER", "composio")
    monkeypatch.setenv("MIA_COMPOSIO_API_KEY", "cmp")
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "user-abc")
    monkeypatch.setenv("MIA_WHATSAPP_PHONE_NUMBER_ID", "109999")
    init_db()
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
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
                                    "id": "wamid.dup.cmp.1",
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
    try:
        with TestClient(app) as client:
            first = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            second = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
    assert first.status_code == 200
    assert first.json()["processed"] == 1
    assert second.json()["duplicates"] == 1
    assert second.json()["processed"] == 0
    assert len(recorder.sent) == 1


def test_history_tool_is_not_an_inbox_and_not_pinned() -> None:
    assert "WHATSAPP_GET_MESSAGE_HISTORY" not in PRELOADED_TOOL_NAMES
    assert WHATSAPP_INBOUND_TRIGGER_SLUGS == frozenset()


def test_composio_send_failure_rolls_back_claim(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setenv("MIA_WHATSAPP_SENDER", "composio")

    class _FailingComposio:
        async def send(self, message: OutboundMessage) -> None:
            raise WhatsAppSendError("WhatsApp Composio send failed: HTTP 401")

    app.dependency_overrides[get_whatsapp_port] = lambda: _FailingComposio()
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
                                    "id": "wamid.cmp.fail.1",
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
    try:
        with TestClient(app) as client:
            first = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            second = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
    assert first.status_code == 502
    assert second.status_code == 502
    assert second.json().get("duplicates") != 1
