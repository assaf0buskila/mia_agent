"""Telegram voice adapter: one authenticated note reaches the one OwnerGraph."""

from __future__ import annotations

import json
from itertools import count
from typing import Any

import httpx
import pytest
from app.api import owner as owner_api
from app.api import telegram as telegram_api
from app.api.deps import get_telegram_port, get_transcription_port
from app.core.config import Settings
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain import owner_brain as owner_brain_module
from app.domain.owner_brain import OwnerBrainResult
from app.integrations.base import RecordingMessagePort
from app.integrations.telegram import TelegramMediaError, TelegramPort, parse_telegram_update
from app.integrations.transcribe import FakeTranscriptionPort, TranscriptionError, TranscriptResult
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

OWNER_ID = "551122"
WEBHOOK_SECRET = "telegram-voice-test-secret"
_UPDATE_IDS = count(10_000)


def _fresh_update_id() -> int:
    """Avoid process-lifetime SQLite webhook collisions on same-process test reruns."""
    return next(_UPDATE_IDS)


class RecordingTelegramVoicePort(RecordingMessagePort):
    def __init__(self) -> None:
        super().__init__()
        self.downloaded_file_ids: list[str] = []

    async def download_voice(
        self, file_id: str, *, declared_mime_type: str = "", declared_filename: str = ""
    ) -> tuple[bytes, str, str]:
        del declared_mime_type, declared_filename
        self.downloaded_file_ids.append(file_id)
        return b"synthetic-ogg", "audio/ogg", "note.ogg"


class FailingTranscriptionPort:
    async def transcribe(
        self, *, audio: bytes, mime_type: str, filename: str = "note.ogg"
    ) -> object:
        del audio, mime_type, filename
        raise TranscriptionError("provider said: transcript and token must stay private")


class CapturingTranscriptionPort:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    async def transcribe(
        self, *, audio: bytes, mime_type: str, filename: str = "note.ogg"
    ) -> TranscriptResult:
        self.calls.append((audio, mime_type, filename))
        return TranscriptResult(text="בדיקת מסמך קולי", stt_provider="fake", stt_model="fake")


class InvalidMimeTelegramVoicePort(RecordingMessagePort):
    def __init__(self, mime_type: str) -> None:
        super().__init__()
        self.mime_type = mime_type
        self.downloaded_file_ids: list[str] = []

    async def download_voice(
        self, file_id: str, *, declared_mime_type: str = "", declared_filename: str = ""
    ) -> tuple[bytes, str, str]:
        del declared_mime_type, declared_filename
        self.downloaded_file_ids.append(file_id)
        return b"not-audio", self.mime_type, "note.ogg"


class AlternateTelegramVoicePort(RecordingMessagePort):
    def __init__(self, audio: object, mime_type: object) -> None:
        super().__init__()
        self.audio = audio
        self.mime_type = mime_type
        self.downloaded_file_ids: list[str] = []

    async def download_voice(
        self, file_id: str, *, declared_mime_type: str = "", declared_filename: str = ""
    ) -> tuple[bytes, str, str]:
        del declared_mime_type, declared_filename
        self.downloaded_file_ids.append(file_id)
        return self.audio, self.mime_type, "note.ogg"  # type: ignore[return-value]


class MalformedTelegramVoicePort(RecordingMessagePort):
    def __init__(self, result: object) -> None:
        super().__init__()
        self.result = result
        self.downloaded_file_ids: list[str] = []

    async def download_voice(
        self, file_id: str, *, declared_mime_type: str = "", declared_filename: str = ""
    ) -> tuple[bytes, str, str]:
        del declared_mime_type, declared_filename
        self.downloaded_file_ids.append(file_id)
        return self.result  # type: ignore[return-value]


class FailingDownloadTelegramVoicePort(RecordingMessagePort):
    async def download_voice(
        self, file_id: str, *, declared_mime_type: str = "", declared_filename: str = ""
    ) -> tuple[bytes, str, str]:
        del file_id, declared_mime_type, declared_filename
        raise TelegramMediaError("private provider detail")


class FailFirstReplyTelegramVoicePort(RecordingTelegramVoicePort):
    def __init__(self) -> None:
        super().__init__()
        self.send_attempts = 0

    async def send(self, message) -> None:
        self.send_attempts += 1
        if self.send_attempts == 1:
            raise RuntimeError("private Telegram failure detail")
        await super().send(message)


def _voice_update(*, update_id: int, file_id: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "from": {"id": int(OWNER_ID), "username": "not-an-authority"},
            "chat": {"id": int(OWNER_ID), "type": "private"},
            "voice": {"file_id": file_id, "mime_type": "audio/ogg"},
        },
    }


def _configure_owner(monkeypatch) -> None:
    monkeypatch.setenv("MIA_TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", OWNER_ID)
    monkeypatch.setenv("MIA_TELEGRAM_BOT_TOKEN", "test-bot-token")


def _patch_owner_loop(monkeypatch, handler) -> None:
    monkeypatch.setattr(Settings, "owner_agent_ready", lambda self: True)
    monkeypatch.setattr(owner_brain_module, "answer_owner", handler)
    monkeypatch.setattr(owner_api, "answer_owner", handler)


@pytest.mark.asyncio
async def test_voice_failures_map_to_fixed_content_free_stages() -> None:
    item = {"file_id": "voice-file-stage-test"}

    _, unavailable, _ = await telegram_api._transcribe_telegram_voice(
        item=dict(item),
        media=object(),
        transcribe_port=FakeTranscriptionPort("unused"),
    )
    _, download_failed, _ = await telegram_api._transcribe_telegram_voice(
        item=dict(item),
        media=FailingDownloadTelegramVoicePort(),
        transcribe_port=FakeTranscriptionPort("unused"),
    )
    _, media_rejected, _ = await telegram_api._transcribe_telegram_voice(
        item=dict(item),
        media=InvalidMimeTelegramVoicePort("text/plain"),
        transcribe_port=FakeTranscriptionPort("unused"),
    )
    _, stt_failed, _ = await telegram_api._transcribe_telegram_voice(
        item=dict(item),
        media=RecordingTelegramVoicePort(),
        transcribe_port=FailingTranscriptionPort(),
    )

    assert (unavailable, download_failed, media_rejected, stt_failed) == (
        "download_unavailable",
        "download_failed",
        "media_rejected",
        "stt_failed",
    )


def _assert_one_failed_voice_outcome(*, update_id: int) -> None:
    db = get_session_factory()()
    provider_event_id = f"{update_id}:tool:voice_transcribe"
    try:
        row = LeadStore(db).get_canonical_event(
            provider="telegram",
            provider_event_id=provider_event_id,
        )
        assert row is not None
        assert json.loads(row.payload_json) == {
            "tool": "voice_transcribe",
            "status": "empty",
            "result_count": 0,
        }
        count = db.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(CanonicalEventRow.provider_event_id == provider_event_id)
        )
        assert count == 1
    finally:
        db.close()


def test_voice_note_downloads_transcribes_reaches_owner_graph_and_escapes_html(monkeypatch) -> None:
    """A real webhook request proves the complete channel path, not a helper-only hop."""
    _configure_owner(monkeypatch)
    init_db()
    telegram = RecordingTelegramVoicePort()
    transcribe = FakeTranscriptionPort("תבדקי את התזכורת")
    graph_inputs: list[dict[str, Any]] = []

    def owner_graph_result(**kwargs: object) -> OwnerBrainResult:
        graph_inputs.append(dict(kwargs))
        return OwnerBrainResult("<owner reply & verified>", True, ())

    _patch_owner_loop(monkeypatch, owner_graph_result)
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: transcribe
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            response = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-701"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert response.status_code == 200
        assert response.json()["accepted"] is True
        assert telegram.downloaded_file_ids == ["voice-file-701"]
        assert transcribe.call_count == 1
        assert len(graph_inputs) == 1
        assert graph_inputs[0]["owner_text"] == "תבדקי את התזכורת"
        assert telegram.sent[0].text == "&lt;owner reply &amp; verified&gt;"
        assert telegram.sent[0].parse_mode == "HTML"
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_voice_transcription_failure_is_visible_classified_and_does_not_enter_owner_graph(
    monkeypatch, caplog
) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = RecordingTelegramVoicePort()
    graph_calls: list[object] = []
    monkeypatch.setattr(owner_api, "answer_owner", lambda **kwargs: graph_calls.append(kwargs))
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: FailingTranscriptionPort()
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            response = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-702"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert response.status_code == 200
        assert response.json() == {"accepted": True, "duplicate": False}
        assert telegram.sent[0].text == (
            "לא הצלחתי לתמלל את ההודעה הקולית. אפשר לנסות שוב או לשלוח טקסט."
        )
        assert telegram.sent[0].parse_mode == "HTML"
        assert not graph_calls
        rendered = str(telegram.sent) + str(response.json())
        assert "provider said" not in rendered
        assert "transcript and token" not in rendered
        assert "policy=stt_failed" in caplog.text
        assert "provider said" not in caplog.text
        assert "transcript and token" not in caplog.text
        _assert_one_failed_voice_outcome(update_id=update_id)
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_retried_voice_transcription_failure_sends_one_visible_reply(monkeypatch) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = RecordingTelegramVoicePort()
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: FailingTranscriptionPort()
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            first = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-703"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
            retry = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-703"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert first.json()["accepted"] is True
        assert retry.json() == {
            "accepted": False,
            "duplicate": True,
            "voice": True,
        }
        assert len(telegram.sent) == 1
        _assert_one_failed_voice_outcome(update_id=update_id)
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_voice_failure_reply_send_failure_stays_retryable(monkeypatch) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = FailFirstReplyTelegramVoicePort()
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: FailingTranscriptionPort()
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            first = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-reply-retry"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
            retry = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-reply-retry"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
            duplicate = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-reply-retry"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )

        assert first.json()["accepted"] is True
        assert retry.json()["accepted"] is True
        assert duplicate.json()["duplicate"] is True
        assert telegram.send_attempts == 2
        assert len(telegram.sent) == 1
        assert "private Telegram failure detail" not in first.text
        _assert_one_failed_voice_outcome(update_id=update_id)
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_voice_failure_commit_happens_before_reply_and_retry_sends_once(
    monkeypatch,
) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = RecordingTelegramVoicePort()
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: FailingTranscriptionPort()
    original_commit = Session.commit
    commit_calls = 0

    def fail_first_commit(session: Session) -> None:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise SQLAlchemyError("private database detail")
        original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_first_commit)
    update_id = _fresh_update_id()
    try:
        with TestClient(app) as client:
            first = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-commit-retry"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
            assert first.status_code == 503
            assert first.json() == {"detail": "telegram processing temporarily unavailable"}
            assert telegram.sent == []

            retry = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-commit-retry"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
            duplicate = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-commit-retry"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )

        assert retry.status_code == 200
        assert retry.json()["accepted"] is True
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        assert len(telegram.sent) == 1
        assert "private database detail" not in first.text
        assert "private database detail" not in str(telegram.sent)
        _assert_one_failed_voice_outcome(update_id=update_id)
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_retried_voice_success_claims_before_download_stt_graph_and_reply(monkeypatch) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = RecordingTelegramVoicePort()
    transcribe = FakeTranscriptionPort("תבדקי את התזכורת")
    graph_calls: list[object] = []
    _patch_owner_loop(
        monkeypatch,
        lambda **kwargs: graph_calls.append(kwargs) or OwnerBrainResult("ok", True, ()),
    )
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: transcribe
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            first = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-704"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
            retry = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-704"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert first.json()["accepted"] is True
        assert retry.json()["duplicate"] is True
        assert telegram.downloaded_file_ids == ["voice-file-704"]
        assert transcribe.call_count == 1
        assert len(graph_calls) == len(telegram.sent) == 1
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "text/html",
        "application/json",
        "image/png",
        "audio/ogg; broken",
    ],
)
async def test_telegram_download_voice_rejects_non_audio_and_malformed_content_types(
    content_type: str | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/note.bin"}})
        headers = {} if content_type is None else {"content-type": content_type}
        return httpx.Response(200, content=b"not-audio", headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = TelegramPort(bot_token="test-bot-token", client=client)
        with pytest.raises(TelegramMediaError):
            await port.download_voice("voice-file-invalid-mime")


@pytest.mark.asyncio
async def test_telegram_download_voice_normalizes_supported_audio_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/note.ogg"}})
        return httpx.Response(
            200,
            content=b"audio",
            headers={"content-type": "Audio/Ogg; codecs=opus"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = TelegramPort(bot_token="test-bot-token", client=client)
        audio, mime_type, filename = await port.download_voice("voice-file-valid-mime")
    assert audio == b"audio"
    assert mime_type == "audio/ogg"
    assert filename == "note.ogg"


@pytest.mark.asyncio
async def test_telegram_download_voice_uses_declared_mime_when_cdn_is_generic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/note.ogg"}})
        return httpx.Response(
            200, content=b"audio", headers={"content-type": "application/octet-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = TelegramPort(bot_token="test-bot-token", client=client)
        audio, mime_type, filename = await port.download_voice(
            "voice-file-generic-cdn", declared_mime_type="audio/ogg"
        )
    assert (audio, mime_type, filename) == (b"audio", "audio/ogg", "note.ogg")


@pytest.mark.asyncio
async def test_telegram_download_audio_uses_mp3_path_when_mime_is_missing_and_cdn_generic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "documents/owner.mp3"}}
            )
        return httpx.Response(
            200, content=b"audio", headers={"content-type": "application/octet-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = TelegramPort(bot_token="test-bot-token", client=client)
        audio, mime_type, filename = await port.download_voice(
            "audio-file-missing-mime", declared_filename="owner.mp3"
        )
    assert (audio, mime_type, filename) == (b"audio", "audio/mpeg", "owner.mp3")


@pytest.mark.asyncio
async def test_telegram_download_audio_uses_declared_mp3_name_with_generic_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "documents/file.bin"}}
            )
        return httpx.Response(
            200, content=b"audio", headers={"content-type": "application/octet-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = TelegramPort(bot_token="test-bot-token", client=client)
        audio, mime_type, filename = await port.download_voice(
            "audio-file-generic-path", declared_filename="owner.mp3"
        )
    assert (audio, mime_type, filename) == (b"audio", "audio/mpeg", "owner.mp3")


@pytest.mark.asyncio
async def test_telegram_download_audio_uses_its_real_filename_and_extension() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "audio/track.mp3"}}
            )
        return httpx.Response(200, content=b"audio", headers={"content-type": "audio/mpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = TelegramPort(bot_token="test-bot-token", client=client)
        _audio, mime_type, filename = await port.download_voice(
            "audio-file-mp3", declared_mime_type="audio/mpeg", declared_filename="song.mp3"
        )
    assert (mime_type, filename) == ("audio/mpeg", "track.mp3")


def test_parse_telegram_audio_document_as_voice_input() -> None:
    parsed = parse_telegram_update(
        {
            "update_id": 991,
            "message": {
                "message_id": 992,
                "from": {"id": int(OWNER_ID)},
                "chat": {"id": int(OWNER_ID)},
                "document": {
                    "file_id": "audio-document-991",
                    "file_name": "voice-note.webm",
                    "mime_type": "audio/webm",
                },
            },
        }
    )
    assert parsed is not None
    assert parsed["file_id"] == "audio-document-991"
    assert parsed["mime_type"] == "audio/webm"
    assert parsed["file_name"] == "voice-note.webm"


def test_audio_document_without_mime_keeps_filename_as_format_evidence() -> None:
    parsed = parse_telegram_update(
        {
            "update_id": 997,
            "message": {
                "message_id": 998,
                "from": {"id": int(OWNER_ID)},
                "chat": {"id": int(OWNER_ID)},
                "document": {"file_id": "audio-document-997", "file_name": "owner.mp3"},
            },
        }
    )
    assert parsed is not None
    assert parsed["mime_type"] == ""
    assert parsed["file_name"] == "owner.mp3"


def test_non_audio_telegram_document_does_not_masquerade_as_voice() -> None:
    parsed = parse_telegram_update(
        {
            "update_id": 993,
            "message": {
                "message_id": 994,
                "from": {"id": int(OWNER_ID)},
                "chat": {"id": int(OWNER_ID)},
                "document": {
                    "file_id": "pdf-993",
                    "file_name": "brief.pdf",
                    "mime_type": "application/pdf",
                },
            },
        }
    )
    assert parsed is not None
    assert parsed["file_id"] == ""


def test_unsupported_audio_document_reaches_truthful_media_validation() -> None:
    parsed = parse_telegram_update(
        {
            "update_id": 995,
            "message": {
                "message_id": 996,
                "from": {"id": int(OWNER_ID)},
                "chat": {"id": int(OWNER_ID)},
                "document": {
                    "file_id": "unsupported-audio-995",
                    "file_name": "recording.vendor-codec",
                    "mime_type": "audio/x-vendor-codec",
                },
            },
        }
    )
    assert parsed is not None
    assert parsed["file_id"] == "unsupported-audio-995"


def test_audio_document_reaches_stt_and_owner_graph(monkeypatch) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = RecordingTelegramVoicePort()
    transcribe = CapturingTranscriptionPort()
    graph_inputs: list[dict[str, object]] = []
    _patch_owner_loop(
        monkeypatch,
        lambda **kwargs: graph_inputs.append(kwargs) or OwnerBrainResult("ok", True, ()),
    )
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: transcribe
    update_id = _fresh_update_id()
    payload = _voice_update(update_id=update_id, file_id="audio-document-route")
    payload["message"].pop("voice")
    payload["message"]["document"] = {
        "file_id": "audio-document-route",
        "file_name": "owner-note.mp3",
        "mime_type": "audio/mpeg",
    }
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/telegram/webhook",
                json=payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert response.status_code == 200
        assert transcribe.calls == [(b"synthetic-ogg", "audio/ogg", "note.ogg")]
        assert graph_inputs[0]["owner_text"] == "בדיקת מסמך קולי"
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("media_case", ["empty", "oversize"])
async def test_telegram_download_voice_rejects_empty_and_oversize_audio(media_case: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/note.ogg"}})
        content = b"" if media_case == "empty" else b"x" * 16_000_001
        return httpx.Response(200, content=content, headers={"content-type": "audio/ogg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        port = TelegramPort(bot_token="test-bot-token", client=client)
        with pytest.raises(TelegramMediaError):
            await port.download_voice(f"voice-file-{media_case}")


@pytest.mark.parametrize(
    "mime_type",
    [
        "text/html",
        "application/json",
        "image/png",
        "application/octet-stream",
        "",
        "audio/ogg; broken",
    ],
)
def test_invalid_voice_mime_is_visible_and_never_reaches_stt_or_owner_graph(
    monkeypatch,
    mime_type: str,
) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = InvalidMimeTelegramVoicePort(mime_type)
    transcribe = FakeTranscriptionPort("must not be used")
    graph_calls: list[object] = []
    monkeypatch.setattr(owner_api, "answer_owner", lambda **kwargs: graph_calls.append(kwargs))
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: transcribe
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            response = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-file-invalid-mime"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert response.status_code == 200
        assert response.json()["accepted"] is True
        assert telegram.downloaded_file_ids == ["voice-file-invalid-mime"]
        assert transcribe.call_count == 0
        assert graph_calls == []
        assert telegram.sent[0].text == (
            "לא הצלחתי לתמלל את ההודעה הקולית. אפשר לנסות שוב או לשלוח טקסט."
        )
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


@pytest.mark.parametrize(
    ("media_case", "mime_type"),
    [
        ("empty", "audio/ogg"),
        ("oversize", "audio/ogg"),
        ("valid", "text/plain"),
        ("valid", ""),
        ("valid", "audio/ogg; broken"),
        ("valid", None),
        ("not-bytes", "audio/ogg"),
    ],
)
def test_alternate_voice_port_cannot_bypass_media_validation(
    monkeypatch, media_case: str, mime_type: object
) -> None:
    _configure_owner(monkeypatch)
    init_db()
    audio: object = {
        "empty": b"",
        "oversize": b"x" * 16_000_001,
        "valid": b"voice",
        "not-bytes": "not-bytes",
    }[media_case]
    telegram = AlternateTelegramVoicePort(audio, mime_type)
    transcribe = FakeTranscriptionPort("must not be used")
    graph_calls: list[object] = []
    monkeypatch.setattr(owner_api, "answer_owner", lambda **kwargs: graph_calls.append(kwargs))
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: transcribe
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            response = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="alternate-voice"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert response.json()["accepted"] is True
        assert telegram.downloaded_file_ids == ["alternate-voice"]
        assert transcribe.call_count == 0
        assert graph_calls == []
        assert len(telegram.sent) == 1
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


@pytest.mark.parametrize(
    "malformed_result",
    [
        None,
        (b"voice",),
        (b"voice", "audio/ogg", "note.ogg", "extra"),
        "not-a-pair",
        {"audio": b"voice", "mime": "audio/ogg"},
    ],
)
def test_malformed_voice_download_result_is_visible_once_and_never_reaches_stt_or_graph(
    monkeypatch, malformed_result: object
) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = MalformedTelegramVoicePort(malformed_result)
    transcribe = FakeTranscriptionPort("must not be used")
    graph_calls: list[object] = []
    monkeypatch.setattr(owner_api, "answer_owner", lambda **kwargs: graph_calls.append(kwargs))
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: transcribe
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            first = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="malformed-voice"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
            retry = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="malformed-voice"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert first.json()["accepted"] is True
        assert retry.json()["duplicate"] is True
        assert telegram.downloaded_file_ids == ["malformed-voice"]
        assert transcribe.call_count == 0
        assert graph_calls == []
        assert len(telegram.sent) == 1
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


def test_empty_voice_transcript_is_stt_failure_not_empty_hello(monkeypatch) -> None:
    _configure_owner(monkeypatch)
    init_db()
    telegram = RecordingTelegramVoicePort()
    graph_calls: list[object] = []
    monkeypatch.setattr(owner_api, "answer_owner", lambda **kwargs: graph_calls.append(kwargs))
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: FakeTranscriptionPort("   ")
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            response = client.post(
                "/v1/telegram/webhook",
                json=_voice_update(update_id=update_id, file_id="voice-empty-1"),
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert response.status_code == 200
        assert telegram.sent[0].text.startswith("לא הצלחתי לתמלל")
        assert graph_calls == []
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)


class RecordingTelegramPhotoPort(RecordingMessagePort):
    def __init__(self) -> None:
        super().__init__()
        self.downloaded_photos: list[str] = []

    async def download_photo(self, file_id: str) -> tuple[bytes, str]:
        self.downloaded_photos.append(file_id)
        return b"jpeg-bytes", "image/jpeg"


def test_parse_telegram_photo_keeps_caption_and_file_id() -> None:
    parsed = parse_telegram_update(
        {
            "update_id": 1001,
            "message": {
                "message_id": 1002,
                "from": {"id": int(OWNER_ID)},
                "chat": {"id": int(OWNER_ID)},
                "caption": "תראי את זה",
                "photo": [
                    {"file_id": "small", "width": 90, "height": 90},
                    {"file_id": "large-photo", "width": 800, "height": 800},
                ],
            },
        }
    )
    assert parsed is not None
    assert parsed["photo_file_id"] == "large-photo"
    assert parsed["text"] == "תראי את זה"
    assert parsed["file_id"] == ""


def test_owner_photo_is_seen_and_reaches_owner_graph(monkeypatch) -> None:
    from app.workers import telegram_owner as telegram_owner_module

    _configure_owner(monkeypatch)
    init_db()
    telegram = RecordingTelegramPhotoPort()
    graph_inputs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telegram_owner_module,
        "_describe_owner_image",
        lambda payload, mime: "צילום מסך של ווידג'ט צ'אט",
    )
    _patch_owner_loop(
        monkeypatch,
        lambda **kwargs: graph_inputs.append(dict(kwargs))
        or OwnerBrainResult("ראיתי את התמונה", True, ()),
    )
    app.dependency_overrides[get_telegram_port] = lambda: telegram
    app.dependency_overrides[get_transcription_port] = lambda: FakeTranscriptionPort("unused")
    try:
        update_id = _fresh_update_id()
        with TestClient(app) as client:
            response = client.post(
                "/v1/telegram/webhook",
                json={
                    "update_id": update_id,
                    "message": {
                        "message_id": update_id + 100,
                        "from": {"id": int(OWNER_ID)},
                        "chat": {"id": int(OWNER_ID), "type": "private"},
                        "photo": [{"file_id": "photo-live-1", "width": 320, "height": 320}],
                    },
                },
                headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            )
        assert response.status_code == 200
        assert telegram.downloaded_photos == ["photo-live-1"]
        assert graph_inputs
        assert "צילום מסך" in graph_inputs[0]["owner_text"]
        assert "פה. מה צריך" not in telegram.sent[0].text
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)
        app.dependency_overrides.pop(get_transcription_port, None)
