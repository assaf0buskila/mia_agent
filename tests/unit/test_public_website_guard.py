"""Public Ask Mia POSTs are origin-bound and rate-limited."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.api.deps import get_transcription_port
from app.core.config import Settings
from app.core.public_website import (
    LIMITS_PER_IP,
    allowed_website_origins,
    origin_allowed,
)
from app.integrations.transcribe import FakeTranscriptionPort
from app.main import app
from fastapi.testclient import TestClient

from tests.conftest import identify_website_visitor, without_injected_website_origin

_ALLOWED = "https://www.assafweb.com"
_APEX = "https://assafweb.com"
_AUDIO = ("note.webm", b"fake-webm-bytes", "audio/webm")


def test_widget_still_omits_credentials() -> None:
    source = Path("app/web/ask_mia.js").read_text(encoding="utf-8")
    assert "credentials: 'omit'" in source
    assert "credentials: 'include'" not in source


def test_allowed_origins_include_site_and_public_host() -> None:
    settings = Settings(
        cors_origins="https://www.assafweb.com,https://assafweb.com",
        public_base_url="https://mia.assafweb.com",
    )
    origins = allowed_website_origins(settings)
    assert _ALLOWED in origins
    assert _APEX in origins
    assert "https://mia.assafweb.com" in origins
    assert origin_allowed("", settings) is False
    assert origin_allowed("null", settings) is False
    assert origin_allowed("https://evil.example", settings) is False
    assert origin_allowed(f"{_ALLOWED}/", settings) is True


def test_session_without_origin_is_rejected() -> None:
    with without_injected_website_origin(), TestClient(app) as client:
        response = client.post("/v1/website/sessions")
        assert response.status_code == 403
        assert response.json()["detail"] == "origin not allowed"


def test_session_unknown_origin_is_rejected() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/website/sessions",
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "origin not allowed"


def test_allowed_origin_still_creates_session_and_message() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions", headers={"Origin": _ALLOWED})
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
            headers={"Origin": _ALLOWED},
        )
        assert reply.status_code == 200
        assert reply.json()["next_action"] in {"ask_need", "ask_contact", "answer"}


def test_apex_origin_is_allowed() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions", headers={"Origin": _APEX})
        assert created.status_code == 200


def test_message_without_origin_is_rejected() -> None:
    with TestClient(app) as client:
        session_id = client.post(
            "/v1/website/sessions", headers={"Origin": _ALLOWED}
        ).json()["session_id"]
        with without_injected_website_origin():
            reply = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "hi"},
            )
        assert reply.status_code == 403


def test_voice_without_origin_does_not_transcribe() -> None:
    port = FakeTranscriptionPort("hi")
    app.dependency_overrides[get_transcription_port] = lambda: port
    try:
        with TestClient(app) as client:
            session_id = client.post(
                "/v1/website/sessions", headers={"Origin": _ALLOWED}
            ).json()["session_id"]
            with without_injected_website_origin():
                reply = client.post(
                    f"/v1/website/sessions/{session_id}/voice",
                    files={"file": _AUDIO},
                )
            assert reply.status_code == 403
            assert port.call_count == 0
    finally:
        app.dependency_overrides.pop(get_transcription_port, None)


def test_voice_allowed_origin_still_works() -> None:
    port = FakeTranscriptionPort("hi")
    app.dependency_overrides[get_transcription_port] = lambda: port
    try:
        with TestClient(app) as client:
            session_id = client.post(
                "/v1/website/sessions", headers={"Origin": _ALLOWED}
            ).json()["session_id"]
            reply = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                files={"file": _AUDIO},
                headers={"Origin": _ALLOWED},
            )
            assert reply.status_code == 200
            assert reply.json()["heard"] == "hi"
            assert port.call_count == 1
    finally:
        app.dependency_overrides.pop(get_transcription_port, None)


def test_handoff_without_origin_is_rejected() -> None:
    with TestClient(app) as client:
        session_id = client.post(
            "/v1/website/sessions", headers={"Origin": _ALLOWED}
        ).json()["session_id"]
        with without_injected_website_origin():
            reply = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert reply.status_code == 403


def test_handoff_allowed_origin_still_works() -> None:
    with TestClient(app) as client:
        session_id = client.post(
            "/v1/website/sessions", headers={"Origin": _ALLOWED}
        ).json()["session_id"]
        identify_website_visitor(client, session_id, headers={"Origin": _ALLOWED})
        reply = client.post(
            f"/v1/website/sessions/{session_id}/handoff",
            headers={"Origin": _ALLOWED},
        )
        assert reply.status_code == 200
        assert "token" in reply.json()


def test_events_are_not_origin_bound() -> None:
    with TestClient(app) as client:
        session_id = client.post(
            "/v1/website/sessions", headers={"Origin": _ALLOWED}
        ).json()["session_id"]
        with without_injected_website_origin():
            reply = client.post(
                f"/v1/website/sessions/{session_id}/events",
                json={"kind": "page_viewed", "path": "/"},
            )
        assert reply.status_code == 200


def test_session_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(LIMITS_PER_IP, "session", 1)
    with TestClient(app) as client:
        first = client.post("/v1/website/sessions", headers={"Origin": _ALLOWED})
        assert first.status_code == 200
        second = client.post("/v1/website/sessions", headers={"Origin": _ALLOWED})
        assert second.status_code == 429
        assert second.json()["detail"] == "rate limited"
        assert second.headers.get("retry-after")


def test_handoff_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(LIMITS_PER_IP, "handoff", 1)
    with TestClient(app) as client:
        session_id = client.post(
            "/v1/website/sessions", headers={"Origin": _ALLOWED}
        ).json()["session_id"]
        identify_website_visitor(client, session_id, headers={"Origin": _ALLOWED})
        first = client.post(
            f"/v1/website/sessions/{session_id}/handoff",
            headers={"Origin": _ALLOWED},
        )
        assert first.status_code == 200
        second = client.post(
            f"/v1/website/sessions/{session_id}/handoff",
            headers={"Origin": _ALLOWED},
        )
        assert second.status_code == 429



def test_voice_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(LIMITS_PER_IP, "voice", 1)
    port = FakeTranscriptionPort("hi")
    app.dependency_overrides[get_transcription_port] = lambda: port
    try:
        with TestClient(app) as client:
            session_id = client.post(
                "/v1/website/sessions", headers={"Origin": _ALLOWED}
            ).json()["session_id"]
            first = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                files={"file": _AUDIO},
                headers={"Origin": _ALLOWED},
            )
            assert first.status_code == 200
            second = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                files={"file": _AUDIO},
                headers={"Origin": _ALLOWED},
            )
            assert second.status_code == 429
            assert port.call_count == 1
    finally:
        app.dependency_overrides.pop(get_transcription_port, None)
