import hashlib
import hmac
import json

import httpx
import pytest
from app.api.deps import get_instagram_port
from app.api.instagram import parse_inbound_texts
from app.core.config import Settings
from app.db.models import CanonicalEventRow, WebhookEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.deals import CONFIDENCE_IG, apply_deal_policy
from app.domain.events import Channel, EventType, build_attribution_event
from app.domain.tools import AdapterHttpError
from app.integrations.base import DisabledMessagePort, OutboundMessage, RecordingMessagePort
from app.integrations.instagram import InstagramCloudPort, InstagramSendError, build_instagram_port
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.sales_copy import assert_discovery_reply


def _signed_instagram_payload(
    *,
    message_id: str = "mid.1",
    text: str = "hello",
    is_echo: bool = False,
    sender_id: str = "igsid-456",
    story_id: str | None = None,
    story_url: str | None = None,
    referral: dict | None = None,
    message_referral: dict | None = None,
    omit_message: bool = False,
) -> tuple[bytes, dict[str, str]]:
    msg_event: dict = {
        "sender": {"id": sender_id},
        "recipient": {"id": "ig-account-123"},
    }
    if not omit_message:
        message: dict = {
            "mid": message_id,
            "text": text,
            "is_echo": is_echo,
        }
        if story_id is not None or story_url is not None:
            story: dict = {}
            if story_id is not None:
                story["id"] = story_id
            if story_url is not None:
                story["url"] = story_url
            message["reply_to"] = {"story": story}
        if message_referral is not None:
            message["referral"] = message_referral
        msg_event["message"] = message
    if referral is not None:
        msg_event["referral"] = referral

    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-account-123",
                "messaging": [msg_event],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"ig-app-secret", raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    return raw, headers


def _attribution_for_lead(db, lead_id: str) -> CanonicalEventRow | None:
    return db.scalars(
        select(CanonicalEventRow).where(
            CanonicalEventRow.lead_id == lead_id,
            CanonicalEventRow.event_type == EventType.ATTRIBUTION.value,
        )
    ).one_or_none()


def _events_for_lead(db, lead_id: str, event_type: str) -> list[CanonicalEventRow]:
    return list(
        db.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.lead_id == lead_id,
                CanonicalEventRow.event_type == event_type,
            )
        )
    )


def _lead_id_for_igsid(db, igsid: str) -> str:
    store = LeadStore(db)
    _, lead_id = store.open_channel_lead(channel=Channel.INSTAGRAM, external_id=igsid)
    return lead_id


def test_instagram_verify_and_inbound_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    raw, headers = _signed_instagram_payload()
    with TestClient(app) as client:
        challenge = client.get(
            "/v1/instagram/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "abc",
            },
        )
        assert challenge.status_code == 200
        assert challenge.text == "abc"
        first = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        assert first.status_code == 200
        assert first.json()["processed"] == 1
        assert first.json()["sent"] is False
        second = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        assert second.json()["duplicates"] == 1
        assert second.json()["processed"] == 0


def test_instagram_auto_approved_does_not_send_without_reply_flag(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    monkeypatch.setenv("MIA_AUTO_REPLY_INSTAGRAM", "false")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_instagram_port] = lambda: recorder
    raw, headers = _signed_instagram_payload(message_id="mid.noflag.1")
    try:
        with TestClient(app) as client:
            first = client.post("/v1/instagram/webhook", content=raw, headers=headers)
            assert first.status_code == 200
            body = first.json()
            assert body["processed"] == 1
            assert body["sent"] is False
            assert recorder.sent == []
    finally:
        app.dependency_overrides.pop(get_instagram_port, None)


def test_instagram_inbound_sends_via_recording_port(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    monkeypatch.setenv("MIA_AUTO_REPLY_INSTAGRAM", "true")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_instagram_port] = lambda: recorder
    raw, headers = _signed_instagram_payload(message_id="mid.recording.1")
    try:
        with TestClient(app) as client:
            first = client.post("/v1/instagram/webhook", content=raw, headers=headers)
            assert first.status_code == 200
            body = first.json()
            assert body["processed"] == 1
            assert body["sent"] is True
            assert body["sent_count"] == 1
            assert len(recorder.sent) == 1
            message = recorder.sent[0]
            assert message.conversation_id == "igsid-456"
            assert message.reply_to_id == "mid.recording.1"
            assert_discovery_reply(message.text)
            second = client.post("/v1/instagram/webhook", content=raw, headers=headers)
            assert second.json()["duplicates"] == 1
            assert second.json()["sent"] is False
            assert len(recorder.sent) == 1
    finally:
        app.dependency_overrides.pop(get_instagram_port, None)


def test_instagram_skips_echo_messages(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_instagram_port] = lambda: recorder
    raw, headers = _signed_instagram_payload(message_id="mid.echo.1", is_echo=True)
    try:
        with TestClient(app) as client:
            response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            assert response.json()["processed"] == 0
            assert recorder.sent == []
    finally:
        app.dependency_overrides.pop(get_instagram_port, None)


def test_instagram_kill_switch_skips_processing(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_instagram_port] = lambda: recorder
    raw, headers = _signed_instagram_payload(message_id="mid.killed.1")
    try:
        with TestClient(app) as client:
            response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["killed"] is True
            assert body["processed"] == 0
            assert body["sent"] is False
            assert recorder.sent == []
    finally:
        app.dependency_overrides.pop(get_instagram_port, None)
        monkeypatch.delenv("MIA_KILL_SWITCH", raising=False)


@pytest.mark.asyncio
async def test_instagram_cloud_port_posts_text_message() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"message_id": "mid.out"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = InstagramCloudPort(
        access_token="secret-token-value",
        account_id="ig-account-123",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=client,
    )
    await port.send(
        OutboundMessage(
            conversation_id="igsid-456",
            text="hello",
            channel="instagram",
            idempotency_key="mid.1",
            reply_to_id="mid.1",
        )
    )
    await client.aclose()

    assert "graph.instagram.com/v26.0/ig-account-123/messages" in str(captured["url"])
    assert captured["authorization"] == "Bearer secret-token-value"
    body = captured["body"]
    assert body["recipient"] == {"id": "igsid-456"}
    assert body["message"] == {"text": "hello"}
    assert body["reply_to"] == {"mid": "mid.1"}


@pytest.mark.asyncio
async def test_instagram_cloud_port_error_omits_token() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": {"message": "Invalid OAuth"}})
    )
    client = httpx.AsyncClient(transport=transport)
    port = InstagramCloudPort(
        access_token="secret-token-value",
        account_id="ig-account-123",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=client,
    )
    with pytest.raises(InstagramSendError, match="HTTP 401") as exc_info:
        await port.send(
            OutboundMessage(
                conversation_id="igsid-456",
                text="hello",
                channel="instagram",
                idempotency_key="mid.1",
            )
        )
    assert "secret-token-value" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 401
    assert cause.tool_status() == "unauthorized"
    await client.aclose()


@pytest.mark.asyncio
async def test_instagram_cloud_port_http_429_is_rate_limited() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(429, json={"error": {"message": "Rate limit"}})
    )
    client = httpx.AsyncClient(transport=transport)
    port = InstagramCloudPort(
        access_token="secret-token-value",
        account_id="ig-account-123",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=client,
    )
    with pytest.raises(InstagramSendError, match="HTTP 429") as exc_info:
        await port.send(
            OutboundMessage(
                conversation_id="igsid-456",
                text="hello",
                channel="instagram",
                idempotency_key="mid.1",
            )
        )
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 429
    assert cause.tool_status() == "rate_limited"
    await client.aclose()


@pytest.mark.asyncio
async def test_instagram_cloud_port_network_error_raises_send_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = InstagramCloudPort(
        access_token="secret-token-value",
        account_id="ig-account-123",
        graph_version="v26.0",
        graph_host="graph.instagram.com",
        client=client,
    )
    with pytest.raises(InstagramSendError, match="Instagram Graph API send failed$") as exc_info:
        await port.send(
            OutboundMessage(
                conversation_id="igsid-456",
                text="hello",
                channel="instagram",
                idempotency_key="mid.1",
            )
        )
    assert "secret-token-value" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code is None
    assert cause.tool_status() == "retryable"
    await client.aclose()


def test_instagram_cloud_port_rejects_unknown_graph_host() -> None:
    with pytest.raises(InstagramSendError, match="unsupported Instagram graph host") as exc_info:
        InstagramCloudPort(
            access_token="secret-token-value",
            account_id="ig-account-123",
            graph_version="v26.0",
            graph_host="evil.example",
        )
    assert not isinstance(exc_info.value.__cause__, AdapterHttpError)


def test_build_instagram_port_invalid_host_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_SENDER", "direct")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCOUNT_ID", "ig-account-123")
    monkeypatch.setenv("MIA_INSTAGRAM_GRAPH_HOST", "evil.example")
    port = build_instagram_port(Settings())
    assert isinstance(port, DisabledMessagePort)


def test_build_instagram_port_manychat_is_disabled_even_with_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_SENDER", "manychat")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCESS_TOKEN", "token")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCOUNT_ID", "ig-account-123")
    settings = Settings()
    port = build_instagram_port(settings)
    assert isinstance(port, DisabledMessagePort)


def test_parse_inbound_texts_story_reply_attribution_fields() -> None:
    payload = json.loads(
        _signed_instagram_payload(
            message_id="mid.attr.parse.1",
            text="loved your story",
            story_id="story.media.abc123",
        )[0]
    )
    items = parse_inbound_texts(payload)
    assert len(items) == 1
    assert items[0]["ig_content_id"] == "story.media.abc123"
    assert items[0]["ig_trigger_source"] == "STORY"
    assert "url" not in items[0]


def test_instagram_story_reply_persists_attribution(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    igsid = "igsid-attr-997001"
    raw, headers = _signed_instagram_payload(
        message_id="mid.attr.story.1",
        text="nice story",
        sender_id=igsid,
        story_id="story.media.abc123",
        story_url="https://cdn.example/expired.jpg",
    )
    init_db()
    with TestClient(app) as client:
        response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        assert response.status_code == 200
        assert response.json()["processed"] == 1
    db = get_session_factory()()
    try:
        lead_id = _lead_id_for_igsid(db, igsid)
        row = _attribution_for_lead(db, lead_id)
        assert row is not None
        payload = json.loads(row.payload_json)
        assert payload == {
            "ig_content_id": "story.media.abc123",
            "ig_trigger_source": "STORY",
        }
        assert "url" not in payload
    finally:
        db.close()


def test_instagram_shortlinks_referral_persists_attribution(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    igsid = "igsid-attr-997002"
    raw, headers = _signed_instagram_payload(
        message_id="mid.attr.short.1",
        text="from link",
        sender_id=igsid,
        referral={"source": "SHORTLINKS", "ref": "campaign_ref-1"},
    )
    init_db()
    with TestClient(app) as client:
        response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        assert response.status_code == 200
    db = get_session_factory()()
    try:
        lead_id = _lead_id_for_igsid(db, igsid)
        payload = json.loads(_attribution_for_lead(db, lead_id).payload_json)
        assert payload == {
            "ig_trigger_source": "SHORTLINKS",
            "ig_ref": "campaign_ref-1",
        }
    finally:
        db.close()


def test_instagram_ads_referral_persists_meta_ids_without_media(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    igsid = "igsid-attr-997003"
    raw, headers = _signed_instagram_payload(
        message_id="mid.attr.ads.1",
        text="from ad",
        sender_id=igsid,
        referral={
            "source": "ADS",
            "ad_id": "120210987654321",
            "ads_context_data": {
                "post_id": "17841456789012345",
                "photo_url": "https://cdn.example/ad.jpg",
                "ad_title": "Summer sale",
            },
        },
    )
    init_db()
    with TestClient(app) as client:
        response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        assert response.status_code == 200
    db = get_session_factory()()
    try:
        lead_id = _lead_id_for_igsid(db, igsid)
        payload = json.loads(_attribution_for_lead(db, lead_id).payload_json)
        assert payload == {
            "ig_trigger_source": "ADS",
            "meta_ad_id": "120210987654321",
            "meta_post_id": "17841456789012345",
        }
        serialized = json.dumps(payload)
        assert "photo_url" not in serialized
        assert "ad_title" not in serialized
    finally:
        db.close()


def test_instagram_invalid_ref_not_persisted(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    igsid = "igsid-attr-997004"
    raw, headers = _signed_instagram_payload(
        message_id="mid.attr.badref.1",
        text="hello",
        sender_id=igsid,
        referral={"source": "SHORTLINKS", "ref": "bad @ ref"},
    )
    init_db()
    with TestClient(app) as client:
        response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        assert response.status_code == 200
    db = get_session_factory()()
    try:
        lead_id = _lead_id_for_igsid(db, igsid)
        row = _attribution_for_lead(db, lead_id)
        assert row is not None
        payload = json.loads(row.payload_json)
        assert payload == {"ig_trigger_source": "SHORTLINKS"}
        assert "ig_ref" not in payload
    finally:
        db.close()


def test_instagram_empty_text_story_only_persists_without_sales_out(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_instagram_port] = lambda: recorder
    igsid = "igsid-attr-997005"
    raw, headers = _signed_instagram_payload(
        message_id="mid.attr.empty.1",
        text="",
        sender_id=igsid,
        story_id="story.media.only",
    )
    init_db()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["processed"] == 1
            assert body["sent"] is False
            assert recorder.sent == []
        db = get_session_factory()()
        try:
            lead_id = _lead_id_for_igsid(db, igsid)
            assert _attribution_for_lead(db, lead_id) is not None
            message_in = _events_for_lead(db, lead_id, EventType.MESSAGE_IN.value)
            message_out = _events_for_lead(db, lead_id, EventType.MESSAGE_OUT.value)
            assert message_in == []
            assert message_out == []
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_instagram_port, None)


def test_instagram_attribution_first_write_wins(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    igsid = "igsid-attr-997006"
    first_raw, first_headers = _signed_instagram_payload(
        message_id="mid.attr.first.1",
        text="first",
        sender_id=igsid,
        story_id="story.first",
    )
    second_raw, second_headers = _signed_instagram_payload(
        message_id="mid.attr.second.1",
        text="second",
        sender_id=igsid,
        story_id="story.second",
    )
    init_db()
    with TestClient(app) as client:
        client.post("/v1/instagram/webhook", content=first_raw, headers=first_headers)
        client.post("/v1/instagram/webhook", content=second_raw, headers=second_headers)
    db = get_session_factory()()
    try:
        lead_id = _lead_id_for_igsid(db, igsid)
        payload = json.loads(_attribution_for_lead(db, lead_id).payload_json)
        assert payload["ig_content_id"] == "story.first"
    finally:
        db.close()


def test_parse_inbound_texts_referral_only_synthetic_id() -> None:
    payload = json.loads(
        _signed_instagram_payload(
            sender_id="igsid-ref-997010",
            omit_message=True,
            referral={
                "source": "ADS",
                "ad_id": "1234567890",
                "ads_context_data": {"post_id": "17841456789012345"},
            },
        )[0]
    )
    items = parse_inbound_texts(payload)
    assert len(items) == 1
    item = items[0]
    assert item["id"].startswith("igref:")
    assert item["id"] == "igref:igsid-ref-997010:1234567890"
    assert item["from"] == "igsid-ref-997010"
    assert item["text"] == ""
    assert item["meta_ad_id"] == "1234567890"
    assert item["meta_post_id"] == "17841456789012345"
    assert "url" not in item
    assert "photo_url" not in item


def test_parse_inbound_texts_no_mid_no_attribution_dropped() -> None:
    payload = json.loads(
        _signed_instagram_payload(
            sender_id="igsid-ref-997011",
            omit_message=True,
        )[0]
    )
    assert parse_inbound_texts(payload) == []


def test_parse_inbound_texts_no_mid_no_sender_dropped() -> None:
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-account-123",
                "messaging": [
                    {
                        "recipient": {"id": "ig-account-123"},
                        "referral": {
                            "source": "ADS",
                            "ad_id": "1234567890",
                        },
                    }
                ],
            }
        ],
    }
    assert parse_inbound_texts(payload) == []


def test_parse_inbound_texts_real_mid_not_rewritten() -> None:
    payload = json.loads(
        _signed_instagram_payload(
            message_id="mid.keep.1",
            text="hello",
            sender_id="igsid-ref-997012",
            referral={"source": "ADS", "ad_id": "1234567890"},
        )[0]
    )
    items = parse_inbound_texts(payload)
    assert len(items) == 1
    assert items[0]["id"] == "mid.keep.1"
    assert not items[0]["id"].startswith("igref:")


def test_instagram_referral_only_persists_attribution_without_sales(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_instagram_port] = lambda: recorder
    igsid = "igsid-ref-997013"
    raw, headers = _signed_instagram_payload(
        sender_id=igsid,
        omit_message=True,
        referral={
            "source": "ADS",
            "ad_id": "1234567890",
            "ads_context_data": {"post_id": "17841456789012345"},
        },
    )
    init_db()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["processed"] == 1
            assert body["sent"] is False
            assert recorder.sent == []
        db = get_session_factory()()
        try:
            lead_id = _lead_id_for_igsid(db, igsid)
            row = _attribution_for_lead(db, lead_id)
            assert row is not None
            webhook = db.scalars(
                select(WebhookEventRow).where(
                    WebhookEventRow.provider == "instagram",
                    WebhookEventRow.provider_event_id == "igref:igsid-ref-997013:1234567890",
                )
            ).one_or_none()
            assert webhook is not None
            assert webhook.status == "processed"
            payload = json.loads(row.payload_json)
            assert payload == {
                "ig_trigger_source": "ADS",
                "meta_ad_id": "1234567890",
                "meta_post_id": "17841456789012345",
            }
            assert _events_for_lead(db, lead_id, EventType.MESSAGE_IN.value) == []
            assert _events_for_lead(db, lead_id, EventType.MESSAGE_OUT.value) == []
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_instagram_port, None)


def test_instagram_referral_only_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    igsid = "igsid-ref-997014"
    raw, headers = _signed_instagram_payload(
        sender_id=igsid,
        omit_message=True,
        referral={
            "source": "ADS",
            "ad_id": "1234567890",
            "ads_context_data": {"post_id": "17841456789012345"},
        },
    )
    init_db()
    with TestClient(app) as client:
        first = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        assert first.status_code == 200
        assert first.json()["processed"] == 1
        second = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        second_body = second.json()
        assert second_body["processed"] == 0 or second_body["duplicates"] == 1
    db = get_session_factory()()
    try:
        lead_id = _lead_id_for_igsid(db, igsid)
        rows = _events_for_lead(db, lead_id, EventType.ATTRIBUTION.value)
        assert len(rows) == 1
    finally:
        db.close()


def test_instagram_shortlinks_referral_only_synthetic_id(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    igsid = "igsid-ref-997015"
    raw, headers = _signed_instagram_payload(
        sender_id=igsid,
        omit_message=True,
        referral={"source": "SHORTLINKS", "ref": "campaign_ref-2"},
    )
    init_db()
    with TestClient(app) as client:
        response = client.post("/v1/instagram/webhook", content=raw, headers=headers)
        assert response.status_code == 200
        assert response.json()["processed"] == 1
    db = get_session_factory()()
    try:
        lead_id = _lead_id_for_igsid(db, igsid)
        row = _attribution_for_lead(db, lead_id)
        assert row is not None
        webhook = db.scalars(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == "instagram",
                WebhookEventRow.provider_event_id == "igref:igsid-ref-997015:campaign_ref-2",
            )
        ).one_or_none()
        assert webhook is not None
        payload = json.loads(row.payload_json)
        assert payload == {
            "ig_trigger_source": "SHORTLINKS",
            "ig_ref": "campaign_ref-2",
        }
    finally:
        db.close()


def test_ig_attributed_lead_deal_confidence_is_ig() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        igsid = "igsid-attr-997901"
        _, lead_id = store.open_channel_lead(channel=Channel.INSTAGRAM, external_id=igsid)
        store.save_canonical_event(
            provider="instagram",
            event=build_attribution_event(
                provider="instagram",
                channel=Channel.INSTAGRAM,
                lead_id=lead_id,
                conversation_id=igsid,
                payload={
                    "ig_content_id": "story.media.deal",
                    "ig_trigger_source": "STORY",
                },
            ),
        )
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.INSTAGRAM,
            action="offer_meeting",
            kill_switch=False,
        )
        db.commit()
        deal = store.get_deal(lead_id)
        assert deal is not None
        assert deal.attribution_confidence == CONFIDENCE_IG
        assert deal.attribution_confidence != "utm"
    finally:
        db.close()


def test_build_instagram_port_composio_when_sender_and_keys(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_SENDER", "composio")
    monkeypatch.setenv("MIA_COMPOSIO_API_KEY", "cmp-key")
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "user-abc")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCESS_TOKEN", "graph-token")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCOUNT_ID", "17841400000000000")
    port = build_instagram_port(Settings())
    from app.integrations.instagram import ComposioInstagramPort

    assert isinstance(port, ComposioInstagramPort)


def test_build_instagram_port_composio_without_keys_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_SENDER", "composio")
    monkeypatch.setenv("MIA_COMPOSIO_API_KEY", "")
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCESS_TOKEN", "graph-token")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCOUNT_ID", "17841400000000000")
    port = build_instagram_port(Settings())
    assert isinstance(port, DisabledMessagePort)


def test_build_instagram_port_direct_keeps_graph(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_SENDER", "direct")
    monkeypatch.setenv("MIA_COMPOSIO_API_KEY", "cmp-key")
    monkeypatch.setenv("MIA_COMPOSIO_USER_ID", "user-abc")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCESS_TOKEN", "graph-token")
    monkeypatch.setenv("MIA_INSTAGRAM_ACCOUNT_ID", "17841400000000000")
    port = build_instagram_port(Settings())
    assert isinstance(port, InstagramCloudPort)


@pytest.mark.asyncio
async def test_composio_instagram_send_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"message_id": "mid.cmp"}, "successful": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from app.integrations.instagram import (
        COMPOSIO_INSTAGRAM_VERSION,
        COMPOSIO_SEND_TEXT_TOOL,
        ComposioInstagramPort,
    )

    port = ComposioInstagramPort(
        api_key="cmp-test",
        user_id="user-abc",
        account_id="17841400000000000",
        client=client,
    )
    await port.send(
        OutboundMessage(
            conversation_id="igsid-456",
            text="hello",
            channel="instagram",
            idempotency_key="mid.1",
            reply_to_id="mid.orig",
        )
    )
    assert str(captured["url"]).endswith(f"/{COMPOSIO_SEND_TEXT_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["user_id"] == "user-abc"
    assert body["version"] == COMPOSIO_INSTAGRAM_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["recipient_id"] == "igsid-456"
    assert arguments["text"] == "hello"
    assert arguments["ig_user_id"] == "17841400000000000"
    assert arguments["reply_to_message_id"] == "mid.orig"
    serialized = json.dumps(body)
    assert "CREATE_POST" not in serialized
    assert "cmp-test" not in serialized
    await client.aclose()


@pytest.mark.asyncio
async def test_composio_instagram_send_http_401_raises() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401))
    )
    from app.integrations.instagram import ComposioInstagramPort

    port = ComposioInstagramPort(
        api_key="secret-cmp-key",
        user_id="user-abc",
        client=client,
    )
    with pytest.raises(InstagramSendError, match="Composio send failed: HTTP 401") as exc_info:
        await port.send(
            OutboundMessage(
                conversation_id="igsid-456",
                text="hello",
                channel="instagram",
                idempotency_key="mid.1",
            )
        )
    assert "secret-cmp-key" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 401
    await client.aclose()
