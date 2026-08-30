import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest
from app.api.deps import (
    get_transcription_port,
    get_whatsapp_media_port,
    get_whatsapp_port,
)
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import parse_due_at
from app.domain.owner_tasks import ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.transcribe import (
    FakeTranscriptionPort,
    OpenAITranscribePort,
    TranscriptionError,
    duration_ms_from_seconds,
    sanitize_confidence,
    sanitize_language,
    sanitize_stt_model,
    sanitize_stt_provider,
)
from app.integrations.whatsapp import FakeMediaPort, WhatsAppMediaError, WhatsAppMediaPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.unit.sales_copy import assert_discovery_reply


def _sign_payload(payload: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    return raw, headers


def _audio_payload(*, message_id: str = "wamid.audio.1", from_phone: str = "972501234567") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": message_id,
                                    "type": "audio",
                                    "audio": {
                                        "id": "MEDIA_ID",
                                        "mime_type": "audio/ogg; codecs=opus",
                                        "voice": True,
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }


def _assert_voice_transcribe_tool_result(
    *,
    provider_event_id: str,
    lead_id: str | None,
    transcript_must_not_appear: str,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        row = LeadStore(db).get_canonical_event(
            provider="whatsapp",
            provider_event_id=f"{provider_event_id}:tool:voice_transcribe",
        )
        assert row is not None
        assert row.event_type == "tool_result"
        assert row.lead_id == lead_id
        payload = json.loads(row.payload_json)
        assert set(payload.keys()) == {"tool", "status", "result_count"}
        assert payload == {
            "tool": "voice_transcribe",
            "status": "ok",
            "result_count": 1,
        }
        assert "text" not in payload
        assert transcript_must_not_appear not in row.payload_json
        count = db.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(
                CanonicalEventRow.provider_event_id
                == f"{provider_event_id}:tool:voice_transcribe"
            )
        )
        assert count == 1
    finally:
        db.close()


def test_whatsapp_prospect_audio_persists_voice_transcribe_tool_result(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    distinctive = "clinic missed calls all day uniquely"
    fake_media = FakeMediaPort({"MEDIA_ID": (b"audio-bytes", "audio/ogg")})
    fake_transcribe = FakeTranscriptionPort(distinctive)
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: fake_media
    app.dependency_overrides[get_transcription_port] = lambda: fake_transcribe
    raw, headers = _sign_payload(
        _audio_payload(message_id="wamid.audio.tool", from_phone="972501111111")
    )
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            assert response.json()["processed"] == 1
        init_db()
        db = get_session_factory()()
        try:
            tool_row = LeadStore(db).get_canonical_event(
                provider="whatsapp",
                provider_event_id="wamid.audio.tool:tool:voice_transcribe",
            )
            assert tool_row is not None
            prospect_lead_id = tool_row.lead_id
        finally:
            db.close()
        _assert_voice_transcribe_tool_result(
            provider_event_id="wamid.audio.tool",
            lead_id=prospect_lead_id,
            transcript_must_not_appear=distinctive,
        )
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        app.dependency_overrides.pop(get_whatsapp_media_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_whatsapp_audio_transcribed_and_replies_like_text(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    fake_media = FakeMediaPort({"MEDIA_ID": (b"audio-bytes", "audio/ogg")})
    fake_transcribe = FakeTranscriptionPort("hi")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: fake_media
    app.dependency_overrides[get_transcription_port] = lambda: fake_transcribe
    raw, headers = _sign_payload(_audio_payload(message_id="wamid.audio.hi"))
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["processed"] == 1
            assert body["sent"] is True
            assert fake_transcribe.call_count == 1
            assert len(recorder.sent) == 1
            assert_discovery_reply(recorder.sent[0].text)
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        app.dependency_overrides.pop(get_whatsapp_media_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_whatsapp_duplicate_audio_transcribes_once(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    fake_media = FakeMediaPort({"MEDIA_ID": (b"audio-bytes", "audio/ogg")})
    fake_transcribe = FakeTranscriptionPort("hi")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: fake_media
    app.dependency_overrides[get_transcription_port] = lambda: fake_transcribe
    raw, headers = _sign_payload(_audio_payload(message_id="wamid.audio.dup"))
    try:
        with TestClient(app) as client:
            first = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert first.json()["processed"] == 1
            assert fake_transcribe.call_count == 1
            second = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            body = second.json()
            assert body["duplicates"] == 1
            assert body["processed"] == 0
            assert fake_transcribe.call_count == 1
        init_db()
        db = get_session_factory()()
        try:
            tool_row = LeadStore(db).get_canonical_event(
                provider="whatsapp",
                provider_event_id="wamid.audio.dup:tool:voice_transcribe",
            )
            assert tool_row is not None
            prospect_lead_id = tool_row.lead_id
        finally:
            db.close()
        _assert_voice_transcribe_tool_result(
            provider_event_id="wamid.audio.dup",
            lead_id=prospect_lead_id,
            transcript_must_not_appear="hi",
        )
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        app.dependency_overrides.pop(get_whatsapp_media_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_whatsapp_owner_audio_acks_without_sales_graph(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setenv("MIA_WHATSAPP_OWNER_PHONES", "972509999999")
    fake_media = FakeMediaPort({"MEDIA_ID": (b"audio-bytes", "audio/ogg")})
    fake_transcribe = FakeTranscriptionPort("Schedule a follow-up with Daniel tomorrow.")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: fake_media
    app.dependency_overrides[get_transcription_port] = lambda: fake_transcribe
    raw, headers = _sign_payload(
        _audio_payload(message_id="wamid.audio.owner", from_phone="972509999999")
    )
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            assert response.json()["processed"] == 1
            assert len(recorder.sent) == 1
            owner_text = "Schedule a follow-up with Daniel tomorrow."
            decision = classify_owner_task(owner_text)
            due_at = parse_due_at(owner_text, now=datetime.now(UTC))
            expected = ack_for_owner_task(decision, due_at=due_at)
            assert recorder.sent[0].text == expected
            assert "how the business works" not in recorder.sent[0].text
            assert "יום רגיל בעסק" not in recorder.sent[0].text
            init_db()
            db = get_session_factory()()
            try:
                row = LeadStore(db).get_transcript(
                    provider="whatsapp", provider_event_id="wamid.audio.owner"
                )
                assert row is not None
                assert row.actor_role == "owner"
                assert row.transcript == "Schedule a follow-up with Daniel tomorrow."
                assert row.stt_provider == "fake"
                assert row.stt_model == "fake"
                assert row.language == ""
                assert row.duration_ms == 0
                assert row.confidence == ""
                assert row.cost_usd == 0
                assert row.retention_status == "text_only"
            finally:
                db.close()
            _assert_voice_transcribe_tool_result(
                provider_event_id="wamid.audio.owner",
                lead_id=None,
                transcript_must_not_appear="Schedule a follow-up with Daniel tomorrow.",
            )
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        app.dependency_overrides.pop(get_whatsapp_media_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)
        monkeypatch.delenv("MIA_WHATSAPP_OWNER_PHONES", raising=False)


def test_whatsapp_disabled_transcription_skips_audio_keeps_text(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.delenv("MIA_OPENAI_API_KEY", raising=False)
    fake_media = FakeMediaPort({"MEDIA_ID": (b"audio-bytes", "audio/ogg")})
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: fake_media
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
                                    "id": "wamid.audio.skip",
                                    "type": "audio",
                                    "audio": {"id": "MEDIA_ID", "mime_type": "audio/ogg"},
                                },
                                {
                                    "from": "972501234567",
                                    "id": "wamid.text.ok",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                },
                            ]
                        }
                    }
                ]
            }
        ],
    }
    raw, headers = _sign_payload(payload)
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            body = response.json()
            assert body["processed"] == 1
            assert len(recorder.sent) == 1
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        app.dependency_overrides.pop(get_whatsapp_media_port, None)


@pytest.mark.asyncio
async def test_openai_transcribe_port_posts_multipart() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = request.content.decode(errors="replace")
        return httpx.Response(200, json={"text": "hello from audio"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client,
    )
    result = await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    await client.aclose()

    assert "/v1/audio/transcriptions" in str(captured["url"])
    assert captured["authorization"] == "Bearer secret-token-value"
    assert "gpt-transcribe" in str(captured["body"])
    # gpt-transcribe documents `json` and `text` only; `verbose_json` is a whisper-1
    # format. Sending it made every owner voice note fail with a 400 that the caller
    # swallowed into "I didn't catch that".
    body = str(captured["body"])
    assert "verbose_json" not in body
    assert "json" in body
    # It also takes a plural `languages` array and must never receive both forms.
    assert "languages[]" in body
    assert '\nlanguage"' not in body
    assert result.text == "hello from audio"
    assert result.stt_provider == "openai"
    assert result.stt_model == "gpt-transcribe"
    assert result.language == ""
    assert result.duration_ms == 0


@pytest.mark.asyncio
async def test_openai_transcribe_port_primary_failure_uses_fallback() -> None:
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode(errors="replace")
        if "gpt-transcribe" in body:
            models.append("gpt-transcribe")
            return httpx.Response(500, json={"error": {"message": "busy"}})
        if "gpt-4o-mini-transcribe" in body:
            models.append("gpt-4o-mini-transcribe")
            return httpx.Response(200, json={"text": "from fallback"})
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        fallback_model="gpt-4o-mini-transcribe",
        client=client,
    )
    result = await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    await client.aclose()
    assert result.text == "from fallback"
    assert result.stt_provider == "openai"
    assert result.stt_model == "gpt-4o-mini-transcribe"
    assert models == ["gpt-transcribe", "gpt-4o-mini-transcribe"]


@pytest.mark.asyncio
async def test_openai_transcribe_port_error_omits_token() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": {"message": "Invalid OAuth"}})
    )
    client = httpx.AsyncClient(transport=transport)
    port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client,
    )
    with pytest.raises(TranscriptionError, match="HTTP 401") as exc_info:
        await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    assert "secret-token-value" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 401
    assert cause.tool_status() == "unauthorized"
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_transcribe_port_http_429_is_rate_limited() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(429, json={"error": {"message": "Rate limit"}})
    )
    client = httpx.AsyncClient(transport=transport)
    port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client,
    )
    with pytest.raises(TranscriptionError, match="HTTP 429") as exc_info:
        await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 429
    assert cause.tool_status() == "rate_limited"
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_transcribe_port_network_error_raises_transcription_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client,
    )
    with pytest.raises(TranscriptionError, match="OpenAI transcription failed$") as exc_info:
        await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    assert "secret-token-value" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code is None
    assert cause.tool_status() == "retryable"
    await client.aclose()


@pytest.mark.asyncio
async def test_whatsapp_media_download_allowlists_host() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/MEDIA123"):
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/media/download",
                    "mime_type": "audio/ogg",
                },
            )
        return httpx.Response(200, content=b"audio-data", headers={"content-type": "audio/ogg"})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppMediaPort(
        access_token="secret-token-value",
        graph_version="v25.0",
        client=client,
    )
    data, mime = await port.download("MEDIA123")
    await client.aclose()
    assert data == b"audio-data"
    assert mime == "audio/ogg"
    assert any("lookaside.fbsbx.com" in url for url in calls)


@pytest.mark.asyncio
async def test_whatsapp_media_download_rejects_evil_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"url": "https://evil.example/steal", "mime_type": "audio/ogg"},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppMediaPort(
        access_token="secret-token-value",
        graph_version="v25.0",
        client=client,
    )
    with pytest.raises(WhatsAppMediaError, match="allowlisted"):
        await port.download("MEDIA123")
    await client.aclose()


@pytest.mark.asyncio
async def test_whatsapp_media_metadata_http_401_is_unauthorized() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": {"message": "Invalid OAuth"}})
    )
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppMediaPort(
        access_token="secret-token-value",
        graph_version="v25.0",
        client=client,
    )
    with pytest.raises(WhatsAppMediaError, match="HTTP 401") as exc_info:
        await port.download("MEDIA123")
    assert "secret-token-value" not in str(exc_info.value)
    cause = exc_info.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == 401
    assert cause.tool_status() == "unauthorized"
    await client.aclose()


@pytest.mark.asyncio
async def test_whatsapp_media_missing_url_has_no_adapter_http_cause() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"mime_type": "audio/ogg"})
    )
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppMediaPort(
        access_token="secret-token-value",
        graph_version="v25.0",
        client=client,
    )
    with pytest.raises(WhatsAppMediaError, match="missing url") as exc_info:
        await port.download("MEDIA123")
    assert not isinstance(exc_info.value.__cause__, AdapterHttpError)
    await client.aclose()


@pytest.mark.asyncio
async def test_openai_transcribe_port_verbose_json_parses_metadata() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"text": "שלום", "language": "he", "duration": 1.5},
        )
    )
    client = httpx.AsyncClient(transport=transport)
    port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client,
    )
    result = await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    await client.aclose()
    assert result.text == "שלום"
    assert result.stt_provider == "openai"
    assert result.stt_model == "gpt-transcribe"
    assert result.language == "he"
    assert result.duration_ms == 1500


@pytest.mark.asyncio
async def test_openai_transcribe_port_text_only_payload() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"text": "hello"})
    )
    client = httpx.AsyncClient(transport=transport)
    port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client,
    )
    result = await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    await client.aclose()
    assert result.text == "hello"
    assert result.stt_provider == "openai"
    assert result.stt_model == "gpt-transcribe"
    assert result.language == ""
    assert result.duration_ms == 0


@pytest.mark.asyncio
async def test_openai_transcribe_port_sanitizes_language() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"text": "ok", "language": "HACK", "duration": "bad"},
        )
    )
    client = httpx.AsyncClient(transport=transport)
    port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client,
    )
    result = await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    await client.aclose()
    assert result.language == ""
    assert result.duration_ms == 0

    transport_he_il = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"text": "ok", "language": "he-IL"},
        )
    )
    client_he_il = httpx.AsyncClient(transport=transport_he_il)
    port_he_il = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client_he_il,
    )
    result_he_il = await port_he_il.transcribe(audio=b"abc", mime_type="audio/ogg")
    await client_he_il.aclose()
    assert result_he_il.language == "he-IL"


@pytest.mark.asyncio
async def test_fake_transcription_port_stamps_metadata() -> None:
    port = FakeTranscriptionPort("note text")
    result = await port.transcribe(audio=b"abc", mime_type="audio/ogg")
    assert result.text == "note text"
    assert result.stt_provider == "fake"
    assert result.stt_model == "fake"
    assert result.confidence == ""
    assert result.duration_ms == 0


def test_stt_sanitize_helpers() -> None:
    assert sanitize_stt_provider("openai") == "openai"
    assert sanitize_stt_provider("fake") == "fake"
    assert sanitize_stt_provider("other") == ""
    assert sanitize_stt_model("gpt-transcribe") == "gpt-transcribe"
    assert sanitize_stt_model("bad model") == ""
    assert sanitize_language("he") == "he"
    assert sanitize_language("he-IL") == "he-IL"
    assert sanitize_language("HACK") == ""
    assert duration_ms_from_seconds(1.5) == 1500
    assert duration_ms_from_seconds("2") == 2000
    assert duration_ms_from_seconds("bad") == 0
    assert duration_ms_from_seconds(86401) == 86_400_000
    assert sanitize_confidence(0) == "0"
    assert sanitize_confidence(1) == "1"
    assert sanitize_confidence(0.87) == "0.87"
    assert sanitize_confidence(True) == ""
    assert sanitize_confidence(2) == ""
    assert sanitize_confidence(1.5) == ""
    assert sanitize_confidence("hack") == ""
    assert sanitize_confidence(float("nan")) == ""
    assert sanitize_confidence(float("inf")) == ""
    assert sanitize_confidence("nan") == ""


def test_whatsapp_fake_stt_metadata_persisted_on_voice_transcript_row(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    fake_media = FakeMediaPort({"MEDIA_ID": (b"audio-bytes", "audio/ogg")})
    fake_transcribe = FakeTranscriptionPort("metadata check transcript")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: fake_media
    app.dependency_overrides[get_transcription_port] = lambda: fake_transcribe
    raw, headers = _sign_payload(
        _audio_payload(message_id="wamid.stt.meta.1", from_phone="972509997801")
    )
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            assert response.json()["processed"] == 1
        init_db()
        db = get_session_factory()()
        try:
            row = LeadStore(db).get_transcript(
                provider="whatsapp", provider_event_id="wamid.stt.meta.1"
            )
            assert row is not None
            assert row.transcript == "metadata check transcript"
            assert row.stt_provider == "fake"
            assert row.stt_model == "fake"
            assert row.language == ""
            assert row.duration_ms == 0
            assert row.confidence == ""
            assert row.cost_usd == 0
            assert row.retention_status == "text_only"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        app.dependency_overrides.pop(get_whatsapp_media_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


@pytest.mark.asyncio
async def test_openai_transcribe_port_parses_confidence() -> None:
    transport_ok = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"text": "hello", "confidence": 0.87},
        )
    )
    client_ok = httpx.AsyncClient(transport=transport_ok)
    port_ok = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client_ok,
    )
    result_ok = await port_ok.transcribe(audio=b"abc", mime_type="audio/ogg")
    await client_ok.aclose()
    assert result_ok.confidence == "0.87"

    for bad_confidence in (1.5, "hack"):
        transport_bad = httpx.MockTransport(
            lambda _request, c=bad_confidence: httpx.Response(
                200,
                json={"text": "hello", "confidence": c},
            )
        )
        client_bad = httpx.AsyncClient(transport=transport_bad)
        port_bad = OpenAITranscribePort(
            api_key="secret-token-value",
            model="gpt-transcribe",
            client=client_bad,
        )
        result_bad = await port_bad.transcribe(audio=b"abc", mime_type="audio/ogg")
        await client_bad.aclose()
        assert result_bad.confidence == ""

    transport_missing = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"text": "hello"})
    )
    client_missing = httpx.AsyncClient(transport=transport_missing)
    port_missing = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client_missing,
    )
    result_missing = await port_missing.transcribe(audio=b"abc", mime_type="audio/ogg")
    await client_missing.aclose()
    assert result_missing.confidence == ""


def test_whatsapp_openai_confidence_persisted_on_voice_transcript_row(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    fake_media = FakeMediaPort({"MEDIA_ID": (b"audio-bytes", "audio/ogg")})
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"text": "confidence row check", "confidence": 0.87},
        )
    )
    client = httpx.AsyncClient(transport=transport)
    openai_port = OpenAITranscribePort(
        api_key="secret-token-value",
        model="gpt-transcribe",
        client=client,
    )
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: fake_media
    app.dependency_overrides[get_transcription_port] = lambda: openai_port
    raw, headers = _sign_payload(
        _audio_payload(message_id="wamid.stt.conf.1", from_phone="972509997802")
    )
    try:
        with TestClient(app) as test_client:
            response = test_client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            assert response.json()["processed"] == 1
        init_db()
        db = get_session_factory()()
        try:
            row = LeadStore(db).get_transcript(
                provider="whatsapp", provider_event_id="wamid.stt.conf.1"
            )
            assert row is not None
            assert row.confidence == "0.87"
            assert row.cost_usd == 0
            assert row.retention_status == "text_only"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        app.dependency_overrides.pop(get_whatsapp_media_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


@pytest.mark.asyncio
async def test_whatsapp_media_download_rejects_http_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"url": "http://lookaside.fbsbx.com/media/download", "mime_type": "audio/ogg"},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = WhatsAppMediaPort(
        access_token="secret-token-value",
        graph_version="v25.0",
        client=client,
    )
    with pytest.raises(WhatsAppMediaError, match="allowlisted"):
        await port.download("MEDIA123")
    await client.aclose()
