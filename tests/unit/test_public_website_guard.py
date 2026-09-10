"""Public Ask Mia POSTs are origin-bound and rate-limited."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.api.deps import get_transcription_port
from app.core.config import MiaEnv, Settings
from app.core.public_website import (
    LIMITS_PER_IP,
    allowed_website_origins,
    origin_allowed,
)
from app.integrations.transcribe import FakeTranscriptionPort
from app.main import app
from fastapi.testclient import TestClient

from tests.conftest import without_injected_website_origin

_ALLOWED = "https://www.assafweb.com"
_APEX = "https://assafweb.com"
_AUDIO = ("note.webm", b"fake-webm-bytes", "audio/webm")


def _session(client: TestClient) -> tuple[str, dict[str, str]]:
    created = client.post("/v1/website/sessions", headers={"Origin": _ALLOWED}).json()
    return created["session_id"], {
        "Origin": _ALLOWED,
        "X-Mia-Session-Credential": created["session_credential"],
    }


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
        body = created.json()
        session_id = body["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi", "client_message_id": "origin-message"},
            headers={
                "Origin": _ALLOWED,
                "X-Mia-Session-Credential": body["session_credential"],
            },
        )
        assert reply.status_code == 200
        assert reply.json()["next_action"] in {"ask_need", "ask_contact", "answer"}


def test_apex_origin_is_allowed() -> None:
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions", headers={"Origin": _APEX})
        assert created.status_code == 200


def test_message_without_origin_is_rejected() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions", headers={"Origin": _ALLOWED}).json()[
            "session_id"
        ]
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
            session_id = client.post("/v1/website/sessions", headers={"Origin": _ALLOWED}).json()[
                "session_id"
            ]
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
            session_id, headers = _session(client)
            reply = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                data={"client_message_id": "voice-"},
                files={"file": _AUDIO},
                headers=headers,
            )
            assert reply.status_code == 200
            assert reply.json()["heard"] == "hi"
            assert port.call_count == 1
    finally:
        app.dependency_overrides.pop(get_transcription_port, None)


def test_handoff_without_origin_is_rejected() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions", headers={"Origin": _ALLOWED}).json()[
            "session_id"
        ]
        with without_injected_website_origin():
            reply = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert reply.status_code == 403


def test_handoff_allowed_origin_still_works() -> None:
    with TestClient(app) as client:
        session_id, headers = _session(client)
        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "רוצה להמשיך עם אסף",
                "email": "guard@example.com",
                "client_message_id": "handoff-capture",
            },
            headers=headers,
        )
        assert captured.status_code == 200
        reply = client.post(
            f"/v1/website/sessions/{session_id}/handoff",
            headers=headers,
        )
        assert reply.status_code == 200
        assert "token" in reply.json()


def test_events_without_origin_are_rejected() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions", headers={"Origin": _ALLOWED}).json()[
            "session_id"
        ]
        with without_injected_website_origin():
            reply = client.post(
                f"/v1/website/sessions/{session_id}/events",
                json={"kind": "page_viewed", "path": "/"},
            )
        assert reply.status_code == 403
        assert reply.json()["detail"] == "origin not allowed"


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
        session_id, headers = _session(client)
        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "רוצה להמשיך עם אסף",
                "email": "limiter@example.com",
                "client_message_id": "limit-capture",
            },
            headers=headers,
        )
        assert captured.status_code == 200
        first = client.post(
            f"/v1/website/sessions/{session_id}/handoff",
            headers=headers,
        )
        assert first.status_code == 200
        second = client.post(
            f"/v1/website/sessions/{session_id}/handoff",
            headers=headers,
        )
        assert second.status_code == 429


def test_voice_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(LIMITS_PER_IP, "voice", 1)
    port = FakeTranscriptionPort("hi")
    app.dependency_overrides[get_transcription_port] = lambda: port
    try:
        with TestClient(app) as client:
            session_id, headers = _session(client)
            first = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                data={"client_message_id": "voice-limit-1"},
                files={"file": _AUDIO},
                headers=headers,
            )
            assert first.status_code == 200
            second = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                data={"client_message_id": "voice-limit-2"},
                files={"file": _AUDIO},
                headers=headers,
            )
            assert second.status_code == 429
            assert port.call_count == 1
    finally:
        app.dependency_overrides.pop(get_transcription_port, None)


def test_client_ip_uses_validated_alb_appended_peer_in_production() -> None:
    from types import SimpleNamespace

    from app.core.public_website import client_ip

    production = Settings(_env_file=None, env=MiaEnv.PROD)
    request = SimpleNamespace(
        headers={"x-forwarded-for": "198.51.100.10, 203.0.113.195"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    assert client_ip(request, settings=production) == "203.0.113.195"

    cf_spoof = SimpleNamespace(
        headers={"cf-connecting-ip": "192.0.2.50"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    assert client_ip(cf_spoof, settings=production) == "10.0.0.1"


def test_client_ip_ignores_untrusted_forwarded_headers_outside_production() -> None:
    from types import SimpleNamespace

    from app.core.public_website import client_ip

    development = Settings(_env_file=None, env=MiaEnv.DEV)
    request = SimpleNamespace(
        headers={
            "x-forwarded-for": "198.51.100.10, 203.0.113.195",
            "cf-connecting-ip": "192.0.2.50",
        },
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert client_ip(request, settings=development) == "127.0.0.1"


def test_client_ip_rejects_invalid_production_forwarded_peer() -> None:
    from types import SimpleNamespace

    from app.core.public_website import client_ip

    production = Settings(_env_file=None, env=MiaEnv.PROD)
    request = SimpleNamespace(
        headers={"x-forwarded-for": "198.51.100.10, attacker-controlled"},
        client=SimpleNamespace(host="198.51.100.10"),
    )
    assert client_ip(request, settings=production) == "unknown"


def test_spoofed_xff_prefix_cannot_rotate_production_rate_limit_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.public_website as public_website

    monkeypatch.setitem(LIMITS_PER_IP, "session", 1)
    monkeypatch.setattr(
        public_website,
        "get_settings",
        lambda: Settings(_env_file=None, env=MiaEnv.PROD),
    )
    with TestClient(app) as client:
        first = client.post(
            "/v1/website/sessions",
            headers={
                "Origin": _ALLOWED,
                "X-Forwarded-For": "198.51.100.10, 203.0.113.195",
            },
        )
        assert first.status_code == 200
        second = client.post(
            "/v1/website/sessions",
            headers={
                "Origin": _ALLOWED,
                "X-Forwarded-For": "198.51.100.11, 203.0.113.195",
            },
        )
        assert second.status_code == 429
