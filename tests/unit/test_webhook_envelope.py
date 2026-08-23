import hashlib
import hmac
import json

from app.api.deps import (
    get_instagram_port,
    get_transcription_port,
    get_whatsapp_media_port,
    get_whatsapp_port,
)
from app.db.models import WebhookEventRow
from app.db.session import get_session_factory, init_db
from app.integrations.base import RecordingMessagePort
from app.integrations.transcribe import FakeTranscriptionPort
from app.integrations.whatsapp import FakeMediaPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select


def _signed_whatsapp_text(
    *,
    message_id: str,
    from_phone: str,
    body: str,
) -> tuple[bytes, dict[str, str]]:
    payload = {
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
                                    "type": "text",
                                    "text": {"body": body},
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


def _signed_whatsapp_audio(
    *,
    message_id: str,
    from_phone: str,
) -> tuple[bytes, dict[str, str]]:
    payload = {
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
                                        "id": "MEDIA_ENVL",
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
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    return raw, headers


def _signed_instagram_payload(
    *,
    sender_id: str,
    message_id: str = "mid.envl.1",
    text: str = "bonus text should not matter",
    referral: dict | None = None,
    omit_message: bool = False,
) -> tuple[bytes, dict[str, str]]:
    msg_event: dict = {
        "sender": {"id": sender_id},
        "recipient": {"id": "ig-account-123"},
    }
    if not omit_message:
        msg_event["message"] = {"mid": message_id, "text": text, "is_echo": False}
    if referral is not None:
        msg_event["referral"] = referral
    payload = {
        "object": "instagram",
        "entry": [{"id": "ig-account-123", "messaging": [msg_event]}],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"ig-app-secret", raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    return raw, headers


def _webhook_row(db, *, provider: str, provider_event_id: str) -> WebhookEventRow:
    row = db.scalars(
        select(WebhookEventRow).where(
            WebhookEventRow.provider == provider,
            WebhookEventRow.provider_event_id == provider_event_id,
        )
    ).one_or_none()
    assert row is not None
    return row


def test_whatsapp_inbound_webhook_envelope_no_pii(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    from_phone = "972509991001"
    distinctive = "envl_secret_visitor_phrase_xyz"
    message_id = "envl.wh.text.1"
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    raw, headers = _signed_whatsapp_text(
        message_id=message_id,
        from_phone=from_phone,
        body=distinctive,
    )
    init_db()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            assert response.json()["processed"] == 1
        db = get_session_factory()()
        try:
            row = _webhook_row(db, provider="whatsapp", provider_event_id=message_id)
            assert row.channel == "whatsapp"
            assert row.envelope_kind == "text"
            dump = (
                f"{row.provider}|{row.provider_event_id}|{row.status}|"
                f"{row.channel}|{row.envelope_kind}|{row.claimed_at}"
            )
            assert distinctive not in dump
            assert from_phone not in dump
            assert "@" not in dump
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)


def test_whatsapp_audio_webhook_envelope_kind(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    from_phone = "972509991002"
    message_id = "envl.wh.audio.1"
    transcript = "envl audio transcript unique"
    fake_media = FakeMediaPort({"MEDIA_ENVL": (b"audio-bytes", "audio/ogg")})
    fake_transcribe = FakeTranscriptionPort(transcript)
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: fake_media
    app.dependency_overrides[get_transcription_port] = lambda: fake_transcribe
    raw, headers = _signed_whatsapp_audio(message_id=message_id, from_phone=from_phone)
    init_db()
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
            assert response.status_code == 200
            assert response.json()["processed"] == 1
        db = get_session_factory()()
        try:
            row = _webhook_row(db, provider="whatsapp", provider_event_id=message_id)
            assert row.channel == "whatsapp"
            assert row.envelope_kind == "audio"
            dump = f"{row.channel}|{row.envelope_kind}|{row.status}"
            assert transcript not in dump
            assert from_phone not in dump
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_whatsapp_port, None)
        app.dependency_overrides.pop(get_whatsapp_media_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_instagram_igref_webhook_envelope_referral(monkeypatch) -> None:
    monkeypatch.setenv("MIA_INSTAGRAM_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_INSTAGRAM_APP_SECRET", "ig-app-secret")
    igsid = "igsid-envl-ref-001"
    provider_event_id = f"igref:{igsid}:1234567890"
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_instagram_port] = lambda: recorder
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
            assert response.json()["processed"] == 1
        db = get_session_factory()()
        try:
            row = _webhook_row(db, provider="instagram", provider_event_id=provider_event_id)
            assert row.channel == "instagram"
            assert row.envelope_kind == "referral"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_instagram_port, None)
