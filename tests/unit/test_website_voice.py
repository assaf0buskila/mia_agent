"""Website widget voice: STT then the existing sales graph."""

from __future__ import annotations

import json

from app.api.deps import get_transcription_port
from app.api.website import _MAX_AUDIO_BYTES
from app.db.models import CanonicalEventRow, VoiceTranscriptRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.integrations.transcribe import FakeTranscriptionPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

_AUDIO = ("note.webm", b"fake-webm-bytes", "audio/webm")


def _override_stt(text: str) -> FakeTranscriptionPort:
    port = FakeTranscriptionPort(text)
    app.dependency_overrides[get_transcription_port] = lambda: port
    return port


def _clear_stt() -> None:
    app.dependency_overrides.pop(get_transcription_port, None)


def test_website_voice_transcribes_then_sales_reply() -> None:
    fake = _override_stt("hi")
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            reply = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                files={"file": _AUDIO},
            )
            assert reply.status_code == 200
            body = reply.json()
            assert set(body) == {"lead_id", "next_action", "message", "heard"}
            assert body["heard"] == "hi"
            assert body["next_action"] == "understand_workflow"
            assert "יום רגיל בעסק" in body["message"]
            assert fake.call_count == 1
        init_db()
        db = get_session_factory()()
        try:
            store = LeadStore(db)
            rows = list(
                db.scalars(
                    select(CanonicalEventRow).where(
                        CanonicalEventRow.conversation_id == session_id
                    )
                )
            )
            in_rows = [row for row in rows if row.event_type == "message_in"]
            assert len(in_rows) == 1
            assert json.loads(in_rows[0].payload_json) == {"text": "hi"}
            transcript = store.get_transcript(
                provider="website",
                provider_event_id=in_rows[0].provider_event_id,
            )
            assert transcript is not None
            assert transcript.transcript == "hi"
            assert transcript.stt_provider == "fake"
            assert transcript.actor_role == "prospect"
            assert transcript.retention_status == "text_only"
            voice_tools = [
                row
                for row in rows
                if row.event_type == "tool_result"
                and json.loads(row.payload_json).get("tool") == "voice_transcribe"
            ]
            assert len(voice_tools) == 1
            tool_payload = json.loads(voice_tools[0].payload_json)
            assert tool_payload == {
                "tool": "voice_transcribe",
                "status": "ok",
                "result_count": 1,
            }
            assert "hi" not in voice_tools[0].payload_json
        finally:
            db.close()
    finally:
        _clear_stt()


def test_website_voice_empty_audio_is_4xx() -> None:
    fake = _override_stt("hi")
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            reply = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                files={"file": ("note.webm", b"", "audio/webm")},
            )
            assert reply.status_code == 400
            assert fake.call_count == 0
    finally:
        _clear_stt()


def test_website_voice_too_large_is_413() -> None:
    fake = _override_stt("hi")
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            reply = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                files={
                    "file": (
                        "note.webm",
                        b"x" * (_MAX_AUDIO_BYTES + 1),
                        "audio/webm",
                    )
                },
            )
            assert reply.status_code == 413
            assert fake.call_count == 0
    finally:
        _clear_stt()


def test_website_voice_kill_switch_does_not_send_audio_to_model(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    fake = _override_stt("clinic missed calls all day uniquely")
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            reply = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                files={"file": _AUDIO},
            )
            assert reply.status_code == 503
            assert fake.call_count == 0
        init_db()
        db = get_session_factory()()
        try:
            in_rows = list(
                db.scalars(
                    select(CanonicalEventRow).where(
                        CanonicalEventRow.conversation_id == session_id,
                        CanonicalEventRow.event_type == "message_in",
                    )
                )
            )
            assert in_rows == []
            transcripts = list(
                db.scalars(
                    select(VoiceTranscriptRow).where(
                        VoiceTranscriptRow.external_id == session_id
                    )
                )
            )
            assert transcripts == []
        finally:
            db.close()
    finally:
        _clear_stt()


def test_website_voice_unknown_session_does_not_transcribe() -> None:
    fake = _override_stt("hi")
    try:
        with TestClient(app) as client:
            reply = client.post(
                "/v1/website/sessions/web_aaaaaaaaaaaaaaaa/voice",
                files={"file": _AUDIO},
            )
            assert reply.status_code == 404
            assert fake.call_count == 0
    finally:
        _clear_stt()
