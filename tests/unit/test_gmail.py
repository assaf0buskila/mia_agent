import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest
from app.core.errors import WebhookRejected
from app.core.webhooks import verify_composio_signature
from app.domain.tools import AdapterHttpError
from app.integrations.gmail import (
    COMPOSIO_FETCH_MESSAGE_TOOL,
    COMPOSIO_GMAIL_VERSION,
    ComposioGmailPort,
    DisabledGmailPort,
    FakeGmailPort,
    InboundEmail,
    build_gmail_port,
    build_inbound_text,
    hydrate_gmail_item,
    parse_sender_email,
)

COMPOSIO_SECRET = "composio-webhook-secret"
GMAIL_TRIGGER = "GMAIL_NEW_GMAIL_MESSAGE"


def _sign_composio_payload(
    payload: dict,
    *,
    secret: str = COMPOSIO_SECRET,
    webhook_id: str = "wh_123",
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


def _gmail_trigger_payload(
    *,
    message_id: str = "msg_1",
    sender: str = "Lead <lead@example.com>",
    subject: str = "Hello",
    message_text: str = "hi",
    trigger_slug: str = GMAIL_TRIGGER,
) -> dict:
    return {
        "type": "composio.trigger.message",
        "metadata": {"trigger_slug": trigger_slug},
        "data": {
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "message_text": message_text,
            "thread_id": "thread_1",
        },
    }


def test_composio_signature_match_mismatch_empty_secret_stale() -> None:
    body = b'{"id":"1"}'
    webhook_id = "wh_test"
    ts = 1_700_000_000
    signed = f"{webhook_id}.{ts}.".encode() + body
    digest = base64.b64encode(hmac.new(b"test-secret", signed, hashlib.sha256).digest()).decode(
        "ascii"
    )
    verify_composio_signature(
        secret="test-secret",
        body=body,
        webhook_id=webhook_id,
        webhook_timestamp=str(ts),
        webhook_signature=f"v1,{digest}",
        now=ts,
    )
    with pytest.raises(WebhookRejected):
        verify_composio_signature(
            secret="test-secret",
            body=body,
            webhook_id=webhook_id,
            webhook_timestamp=str(ts),
            webhook_signature="v1,wrong",
            now=ts,
        )
    with pytest.raises(WebhookRejected):
        verify_composio_signature(
            secret="",
            body=body,
            webhook_id=webhook_id,
            webhook_timestamp=str(ts),
            webhook_signature=f"v1,{digest}",
            now=ts,
        )
    with pytest.raises(WebhookRejected):
        verify_composio_signature(
            secret="test-secret",
            body=body,
            webhook_id=webhook_id,
            webhook_timestamp=str(ts),
            webhook_signature=f"v1,{digest}",
            now=ts + 10_000,
        )


def test_gmail_helpers_parse_sender_and_body() -> None:
    assert parse_sender_email("Lead Name <lead@example.com>") == "lead@example.com"
    assert parse_sender_email("plain@example.com") == "plain@example.com"
    assert build_inbound_text(subject="Hello", message_text="hi") == "Hello\nhi"


def test_hydrate_gmail_item_fills_empty_body_from_fake_port() -> None:
    item = {"id": "msg.hydrate.1", "from": "hydrate@example.com", "text": ""}
    port = FakeGmailPort(
        {
            "msg.hydrate.1": InboundEmail(
                message_id="msg.hydrate.1",
                sender="hydrate@example.com",
                subject="Fetched subject",
                text="Fetched body",
                thread_id="thread_hydrate_1",
            )
        }
    )
    hydrated = hydrate_gmail_item(item, port)
    assert hydrated["text"] == "Fetched subject\nFetched body"
    assert hydrated["thread_id"] == "thread_hydrate_1"


def test_composio_gmail_port_http_401_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    client = httpx.Client(transport=transport)
    port = ComposioGmailPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.fetch_message("msg.gmail.http.401.1")
    assert exc_info.value.status_code == 401


class _RaisingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.HTTPError("network error")


def test_composio_gmail_port_network_error_raises_adapter_error() -> None:
    port = ComposioGmailPort(
        api_key="cmp-test",
        user_id="user-123",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.fetch_message("msg.gmail.network.1")
    assert exc_info.value.status_code is None


def test_hydrate_gmail_item_reraises_adapter_http_error() -> None:
    class HttpErrorGmailPort:
        def fetch_message(self, message_id: str) -> InboundEmail | None:
            del message_id
            raise AdapterHttpError(401)

    item = {"id": "msg.hydrate.http.401.1", "from": "hydrate@example.com", "text": ""}
    with pytest.raises(AdapterHttpError) as exc_info:
        hydrate_gmail_item(item, HttpErrorGmailPort())
    assert exc_info.value.status_code == 401


def test_composio_gmail_fetch_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {
                    "subject": "Hi",
                    "snippet": "body text",
                    "threadId": "thread_fetch_shape",
                },
                "error": None,
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioGmailPort(
        api_key="cmp-test",
        user_id="user-abc",
        client=client,
    )
    result = port.fetch_message("msg.fetch.shape.1")
    assert result is not None
    assert result.text == "body text"
    assert str(captured["url"]).endswith(f"/{COMPOSIO_FETCH_MESSAGE_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["user_id"] == "user-abc"
    assert body["version"] == COMPOSIO_GMAIL_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["message_id"] == "msg.fetch.shape.1"
    assert arguments["format"] == "full"
    assert "user_id" not in arguments
    serialized = json.dumps(body)
    assert "GMAIL_SEND" not in serialized.upper()
    assert "DELETE" not in serialized.upper()


def test_build_gmail_port_disabled_without_credentials() -> None:
    from app.core.config import Settings

    port = build_gmail_port(Settings(composio_api_key="", composio_user_id=""))
    assert isinstance(port, DisabledGmailPort)


def test_fake_gmail_port_returns_configured_message_or_none() -> None:
    port = FakeGmailPort(
        {
            "msg_1": InboundEmail(
                message_id="msg_1",
                sender="lead@example.com",
                subject="Hi",
                text="body",
                thread_id="t1",
            )
        }
    )
    message = port.fetch_message("msg_1")
    assert message is not None
    assert message.sender == "lead@example.com"
    assert port.fetch_message("missing") is None
