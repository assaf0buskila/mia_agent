"""Public Ask Mia POSTs are origin-bound and rate-limited."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from app.api.deps import get_transcription_port
from app.core.config import MiaEnv, Settings
from app.core.public_website import (
    LIMITS_PER_IP,
    LIMITS_PER_SESSION,
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


# --- Chunk H4B: Origin is the only thing this guard may key off ----------------
#
# ``enforce_public_website`` used to fall back, when a request carried no ``Origin``
# at all, to trusting ``Sec-Fetch-Site: same-origin|same-site`` OR an allowlisted
# ``Referer`` -- and then synthesised the origin from the request's own base URL.
# All three inputs are attacker supplied. ``Sec-Fetch-Site`` is a forbidden header
# name, so page JavaScript cannot set it and only a browser can; and a browser sets
# ``Origin`` on every request whose method is not GET/HEAD, which is every route
# behind this guard. The fallback therefore protected no real client while letting
# any non-browser client in with one forged header line.
#
# These use ``base_url`` = the public host, because the old fallback synthesised the
# origin from ``request.base_url``: against TestClient's default ``http://testserver``
# the forged request would be rejected for the wrong reason and the test would pass
# on the broken code.
_PUBLIC_HOST = "http://127.0.0.1:8000"
_FORGED = {"Sec-Fetch-Site": "same-origin"}


def test_forged_sec_fetch_site_cannot_stand_in_for_origin() -> None:
    """The must-not-break half is asserted in the same test: a real browser sends
    BOTH ``Origin`` and ``Sec-Fetch-Site``, so the header must not become fatal --
    only useless."""
    with TestClient(app, base_url=_PUBLIC_HOST) as client:
        browser = client.post(
            "/v1/website/sessions",
            headers={"Origin": _ALLOWED, "Sec-Fetch-Site": "cross-site"},
        )
        assert browser.status_code == 200

        with without_injected_website_origin():
            forged = client.post("/v1/website/sessions", headers=_FORGED)
        assert forged.status_code == 403
        assert forged.json()["detail"] == "origin not allowed"


def test_allowlisted_referer_cannot_stand_in_for_origin() -> None:
    """Same shape: a browser sends ``Referer`` alongside ``Origin``, and the value it
    sends is never load bearing here."""
    with TestClient(app, base_url=_PUBLIC_HOST) as client:
        browser = client.post(
            "/v1/website/sessions",
            headers={"Origin": _ALLOWED, "Referer": "https://evil.example/landing"},
        )
        assert browser.status_code == 200

        with without_injected_website_origin():
            forged = client.post(
                "/v1/website/sessions",
                headers={"Referer": f"{_ALLOWED}/pricing"},
            )
        assert forged.status_code == 403


def test_forged_sec_fetch_site_is_rejected_on_every_guarded_bucket() -> None:
    """The bypass was in the shared guard, so it reached every bucket. Sweep them all
    rather than pinning the one route the defect was written against."""
    port = FakeTranscriptionPort("hi")
    app.dependency_overrides[get_transcription_port] = lambda: port
    try:
        with TestClient(app, base_url=_PUBLIC_HOST) as client:
            session_id, headers = _session(client)
            forged = dict(_FORGED)
            forged["X-Mia-Session-Credential"] = headers["X-Mia-Session-Credential"]
            base = f"/v1/website/sessions/{session_id}"
            with without_injected_website_origin():
                assert (
                    client.post(
                        f"{base}/messages",
                        json={"text": "hi", "client_message_id": "forged-1"},
                        headers=forged,
                    ).status_code
                    == 403
                )
                assert (
                    client.post(
                        f"{base}/events",
                        json={"kind": "page_viewed", "path": "/"},
                        headers=forged,
                    ).status_code
                    == 403
                )
                assert client.post(f"{base}/handoff", headers=forged).status_code == 403
                assert client.post(f"{base}/end", headers=forged).status_code == 403
                assert (
                    client.post(
                        f"{base}/voice",
                        data={"client_message_id": "forged-voice"},
                        files={"file": _AUDIO},
                        headers=forged,
                    ).status_code
                    == 403
                )
            assert port.call_count == 0
    finally:
        app.dependency_overrides.pop(get_transcription_port, None)


def test_preview_harness_same_origin_post_still_passes() -> None:
    """/v1/website/preview is served from the public host and its widget posts back to
    the same origin. A same-origin POST still carries ``Origin`` (Fetch appends it for
    every non-GET/HEAD method), so the harness keeps working. The public host was the
    one origin the old fallback could synthesise and have accepted, so the forged shape
    is asserted here against that exact host."""
    with TestClient(app, base_url=_PUBLIC_HOST) as client:
        assert client.get("/v1/website/preview").status_code == 200
        same_origin = client.post(
            "/v1/website/sessions",
            headers={"Origin": _PUBLIC_HOST, "Sec-Fetch-Site": "same-origin"},
        )
        assert same_origin.status_code == 200

        with without_injected_website_origin():
            forged_same_host = client.post(
                "/v1/website/sessions",
                headers={
                    "Sec-Fetch-Site": "same-site",
                    "Referer": f"{_PUBLIC_HOST}/v1/website/preview",
                },
            )
        assert forged_same_host.status_code == 403


def test_guard_rejections_log_distinguishable_reason_codes(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard without a log is not done. Absent vs. present-but-unknown are different
    operational stories, and a 429 is a rejection too. Header values are attacker
    supplied and are never logged -- only the bucket and a fixed reason code."""
    caplog.set_level(logging.WARNING, logger="app.core.public_website")
    monkeypatch.setitem(LIMITS_PER_IP, "session", 1)
    with TestClient(app, base_url=_PUBLIC_HOST) as client:
        with without_injected_website_origin():
            assert client.post("/v1/website/sessions", headers=_FORGED).status_code == 403
        assert (
            client.post(
                "/v1/website/sessions",
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert client.post("/v1/website/sessions", headers={"Origin": _ALLOWED}).status_code == 200
        assert client.post("/v1/website/sessions", headers={"Origin": _ALLOWED}).status_code == 429

    logged = [record.getMessage() for record in caplog.records]
    assert any(
        "website origin bind rejected bucket=session reason=origin_header_absent" in line
        for line in logged
    ), logged
    assert any(
        "website origin bind rejected bucket=session reason=origin_not_allowlisted" in line
        for line in logged
    ), logged
    assert any(
        "website request rate limited bucket=session reason=rate_limit_per_ip" in line
        for line in logged
    ), logged
    assert not any("evil.example" in line or "Sec-Fetch-Site" in line for line in logged), logged


def test_session_bucket_rate_limit_logs_the_per_session_reason(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-session ceiling is a second, separately tunable rejection; it must not
    be indistinguishable from the per-IP one in the log."""
    caplog.set_level(logging.WARNING, logger="app.core.public_website")
    monkeypatch.setitem(LIMITS_PER_SESSION, "message", 1)
    with TestClient(app, base_url=_PUBLIC_HOST) as client:
        session_id, headers = _session(client)
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi", "client_message_id": "session-limit-1"},
            headers=headers,
        )
        assert first.status_code == 200
        second = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi", "client_message_id": "session-limit-2"},
            headers=headers,
        )
        assert second.status_code == 429

    logged = [record.getMessage() for record in caplog.records]
    assert any(
        "website request rate limited bucket=message reason=rate_limit_per_session" in line
        for line in logged
    ), logged
    assert not any(session_id in line for line in logged), logged
