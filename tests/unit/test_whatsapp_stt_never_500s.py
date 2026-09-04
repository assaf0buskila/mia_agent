"""A failing voice note must not take the whole WhatsApp webhook down.

`transcribe` raises `TranscriptionError` (a `MiaError`) and media download raises
`AdapterHttpError`. Neither is a `RuntimeError`, so the old `except RuntimeError`
let them escape the handler: the webhook answered 502, Composio retried, and every
message in that batch was delivered to the customer again.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from app.api.deps import (
    get_transcription_port,
    get_whatsapp_media_port,
    get_whatsapp_port,
)
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.transcribe import TranscriptionError
from app.integrations.whatsapp import FakeMediaPort
from app.main import app
from fastapi.testclient import TestClient


def _sign_payload(payload: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    return raw, {
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }


class ExplodingTranscriptionPort:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def enabled(self) -> bool:
        return True

    async def transcribe(self, **kwargs: object) -> object:
        self.calls += 1
        raise self._error


class ExplodingMediaPort:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def enabled(self) -> bool:
        return True

    async def download(self, media_id: str) -> tuple[bytes, str]:
        raise self._error


def _payload() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "972501234567",
                                    "id": "wamid.audio.boom",
                                    "type": "audio",
                                    "audio": {
                                        "id": "MEDIA_ID",
                                        "mime_type": "audio/ogg",
                                    },
                                },
                                {
                                    "from": "972501234567",
                                    "id": "wamid.text.survives",
                                    "type": "text",
                                    "text": {"body": "היי"},
                                },
                            ]
                        }
                    }
                ]
            }
        ],
    }


@pytest.mark.parametrize(
    "error",
    [
        TranscriptionError("OpenAI transcription failed: HTTP 500"),
        AdapterHttpError(503),
    ],
    ids=["transcription_error", "adapter_http_error"],
)
def test_failing_voice_note_does_not_500_the_webhook(monkeypatch, error) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: FakeMediaPort(
        {"MEDIA_ID": (b"audio-bytes", "audio/ogg")}
    )
    app.dependency_overrides[get_transcription_port] = lambda: ExplodingTranscriptionPort(
        error
    )
    raw, headers = _sign_payload(_payload())
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
        # 200, not 502: a retry would redeliver the whole batch to the customer.
        assert response.status_code == 200
    finally:
        for dep in (get_whatsapp_port, get_whatsapp_media_port, get_transcription_port):
            app.dependency_overrides.pop(dep, None)


def test_failing_media_download_does_not_500_the_webhook(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("MIA_WHATSAPP_APP_SECRET", "app-secret")
    recorder = RecordingMessagePort()
    app.dependency_overrides[get_whatsapp_port] = lambda: recorder
    app.dependency_overrides[get_whatsapp_media_port] = lambda: ExplodingMediaPort(
        AdapterHttpError(500)
    )
    raw, headers = _sign_payload(_payload())
    try:
        with TestClient(app) as client:
            response = client.post("/v1/whatsapp/webhook", content=raw, headers=headers)
        assert response.status_code == 200
    finally:
        for dep in (get_whatsapp_port, get_whatsapp_media_port):
            app.dependency_overrides.pop(dep, None)
