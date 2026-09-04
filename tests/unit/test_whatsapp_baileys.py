"""The Baileys transport, built but not deployed.

Baileys is the reverse-engineered WhatsApp Web protocol, so this path is opt-in and
must fail closed: not configured means nothing sends and nothing is accepted, never
an open door. Mia's behaviour must also not depend on which WhatsApp transport is in
use, so inbound normalises into the same item shape the Meta webhook produces.
"""

from __future__ import annotations

import httpx
import pytest
from app.api.baileys import parse_baileys_messages
from app.core.config import Settings
from app.core.errors import WebhookRejected
from app.integrations.base import DisabledMessagePort, OutboundMessage
from app.integrations.whatsapp import (
    BaileysWhatsAppPort,
    WhatsAppSendError,
    build_whatsapp_port,
)

TOKEN = "shared-secret"


def _settings(**over) -> Settings:
    base = {
        "_env_file": None,
        "whatsapp_sender": "baileys",
        "whatsapp_baileys_url": "http://sidecar:8088",
        "whatsapp_baileys_token": TOKEN,
    }
    base.update(over)
    return Settings(**base)


def _message() -> OutboundMessage:
    return OutboundMessage(
        conversation_id="972500001111",
        text="שלום",
        channel="whatsapp",
        idempotency_key="wamid.baileys.1",
    )


def test_disabled_unless_fully_configured() -> None:
    assert isinstance(build_whatsapp_port(Settings(_env_file=None)), DisabledMessagePort)
    assert isinstance(
        build_whatsapp_port(_settings(whatsapp_baileys_token="")), DisabledMessagePort
    )
    assert isinstance(
        build_whatsapp_port(_settings(whatsapp_baileys_url="")), DisabledMessagePort
    )
    assert isinstance(build_whatsapp_port(_settings()), BaileysWhatsAppPort)


@pytest.mark.asyncio
async def test_send_posts_the_token_and_the_text() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"sent": True})

    port = BaileysWhatsAppPort(
        base_url="http://sidecar:8088/",
        token=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await port.send(_message())
    assert seen["url"] == "http://sidecar:8088/send"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert "972500001111" in str(seen["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, json={"sent": False}),
        httpx.Response(200, json={"sent": False}),
        httpx.Response(200, text="not json"),
    ],
    ids=["http_error", "sidecar_refused", "malformed"],
)
async def test_a_failed_send_raises_like_every_other_whatsapp_adapter(response) -> None:
    port = BaileysWhatsAppPort(
        base_url="http://sidecar:8088",
        token=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: response)),
    )
    with pytest.raises(WhatsAppSendError):
        await port.send(_message())


def test_inbound_normalises_into_the_shared_item_shape() -> None:
    items = parse_baileys_messages(
        {"messages": [{"id": "ABC", "from": "972500001111", "text": "היי"}]}
    )
    assert items == [
        {"id": "baileys:ABC", "from": "972500001111", "text": "היי"}
    ]


def test_inbound_drops_junk_without_losing_the_batch() -> None:
    items = parse_baileys_messages(
        {
            "messages": [
                {"id": "", "from": "972500001111", "text": "no id"},
                {"id": "B", "from": "", "text": "no sender"},
                {"id": "C", "from": "972500001111", "text": "   "},
                "not a dict",
                {"id": "D", "from": "972500001111", "text": "good"},
            ]
        }
    )
    assert [item["id"] for item in items] == ["baileys:D"]


def test_inbound_ignores_a_payload_that_is_not_ours() -> None:
    assert parse_baileys_messages(None) == []
    assert parse_baileys_messages({}) == []
    assert parse_baileys_messages({"messages": "nope"}) == []


def test_long_text_is_capped() -> None:
    items = parse_baileys_messages(
        {"messages": [{"id": "E", "from": "972500001111", "text": "x" * 9000}]}
    )
    assert len(items[0]["text"]) == 4000


def test_the_webhook_fails_closed_without_a_configured_token(monkeypatch) -> None:
    from app.api.baileys import _verify

    monkeypatch.delenv("MIA_WHATSAPP_BAILEYS_TOKEN", raising=False)
    with pytest.raises(WebhookRejected):
        _verify(f"Bearer {TOKEN}")


def test_the_webhook_rejects_a_wrong_token(monkeypatch) -> None:
    from app.api.baileys import _verify

    monkeypatch.setenv("MIA_WHATSAPP_BAILEYS_TOKEN", TOKEN)
    with pytest.raises(WebhookRejected):
        _verify("Bearer wrong")
    with pytest.raises(WebhookRejected):
        _verify(None)
    _verify(f"Bearer {TOKEN}")
