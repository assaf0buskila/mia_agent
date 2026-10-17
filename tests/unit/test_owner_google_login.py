"""Adversarial Google owner-login checks using only synthetic signed tokens."""

from __future__ import annotations

import time
from collections.abc import Callable

import app.api.owner_sessions as owner_sessions_api
import jwt
import pytest
from app.api.owner_sessions import (
    ACTION_SCOPES,
    CHALLENGE_COOKIE,
    SESSION_COOKIE,
    get_owner_session_db,
    router,
)
from app.core.config import Settings, get_settings
from app.services import owner_google_login
from app.services.owner_google_login import (
    GoogleJwksCache,
    GoogleJwksUnavailable,
    GoogleOwnerLoginDenied,
    verify_google_owner_token,
)
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.unit import test_owner_sessions as auth

CLIENT_ID = "google-client.apps.googleusercontent.com"
SUBJECT = "109876543210987654321"
KID = "synthetic-google-key"
SESSION_SECRET = "s" * 32


@pytest.fixture
def sessions():
    yield from auth.sessions.__wrapped__()


@pytest.fixture
def signed_token(monkeypatch) -> Callable[..., str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})

    def fetch():
        return {"keys": [jwk]}, 300

    monkeypatch.setattr(owner_google_login, "_fetch_google_jwks", fetch)
    monkeypatch.setattr(owner_google_login, "_JWKS", GoogleJwksCache())

    def issue(
        *,
        nonce: str = "nonce",
        issuer: str = "https://accounts.google.com",
        audience: str = CLIENT_ID,
        subject: str = SUBJECT,
        issued_at: int | None = None,
        expires_at: int | None = None,
        algorithm: str = "RS256",
        kid: str = KID,
        azp: str | None = CLIENT_ID,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "iat": issued_at if issued_at is not None else now,
            "exp": expires_at if expires_at is not None else now + 300,
            "nonce": nonce,
        }
        if azp is not None:
            claims["azp"] = azp
        key = private_key if algorithm == "RS256" else b"not-an-rsa-key" * 3
        return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})

    return issue


def _verify(token: str, *, nonce: str = "nonce"):
    return verify_google_owner_token(
        token,
        client_id=CLIENT_ID,
        expected_subject=SUBJECT,
        owner_id=auth.OWNER_ID,
        nonce=nonce,
    )


def _google_settings(*, actions: bool = True, enabled: bool = True) -> Settings:
    settings = auth._settings(actions=actions)
    settings.owner_os_google_enabled = enabled
    settings.owner_os_google_client_id = CLIENT_ID
    settings.owner_os_google_subject = SUBJECT
    settings.owner_os_google_owner_id = auth.OWNER_ID
    settings.owner_os_session_secret = SESSION_SECRET
    settings.telegram_bot_token = ""
    return settings


def test_good_google_token_is_bound_to_pinned_numeric_owner(signed_token) -> None:
    verified = _verify(signed_token())
    assert verified.owner_id == auth.OWNER_ID
    assert verified.subject == SUBJECT
    assert len(verified.proof_hash) == 64


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("issuer", "https://evil.example"),
        ("audience", "another-client"),
        ("algorithm", "HS256"),
        ("nonce", "wrong"),
        ("subject", "another-subject"),
        ("azp", "another-client"),
    ],
)
def test_google_token_rejects_wrong_signed_claims(signed_token, override, value) -> None:
    with pytest.raises(GoogleOwnerLoginDenied):
        _verify(signed_token(**{override: value}))


def test_google_token_rejects_expired_stale_and_unknown_kid(signed_token) -> None:
    now = int(time.time())
    for token in (
        signed_token(issued_at=now - 400, expires_at=now - 60),
        signed_token(issued_at=now - 301, expires_at=now + 60),
        signed_token(kid="unknown-key"),
    ):
        with pytest.raises(GoogleOwnerLoginDenied):
            _verify(token)


def test_unknown_kids_do_not_multiply_jwks_refreshes(monkeypatch) -> None:
    calls = 0
    clock = [100.0]
    document = {
        "keys": [
            {
                "kid": "known",
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "n": "synthetic-n",
                "e": "AQAB",
            }
        ]
    }

    def fetch():
        nonlocal calls
        calls += 1
        return document, 300

    monkeypatch.setattr(owner_google_login, "_fetch_google_jwks", fetch)
    monkeypatch.setattr(owner_google_login.time, "monotonic", lambda: clock[0])
    cache = GoogleJwksCache()
    with pytest.raises(GoogleOwnerLoginDenied):
        cache.get("unknown-one")
    with pytest.raises(GoogleOwnerLoginDenied):
        cache.get("unknown-two")
    assert calls == 1

    clock[0] += owner_google_login.UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS + 1
    with pytest.raises(GoogleOwnerLoginDenied):
        cache.get("unknown-three")
    assert calls == 2


def test_google_mapping_requires_numeric_existing_telegram_owner(sessions, signed_token) -> None:
    for owner_id in ("owner-name", "999"):
        settings = _google_settings()
        settings.owner_os_google_owner_id = owner_id
        with TestClient(auth._app(sessions, settings), base_url=auth.ORIGIN) as client:
            response = client.post(
                "/v1/owner/auth/google/login",
                headers={"Origin": auth.ORIGIN, "X-CSRF-Token": "nonce"},
                json={"id_token": signed_token()},
            )
        assert response.status_code == 503


def test_google_gate_fails_before_db_or_verifier_effects(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(router)
    settings = _google_settings(enabled=False)
    effects = {"db": 0, "verify": 0}

    def db_override():
        effects["db"] += 1
        raise AssertionError("db must not resolve while Google login is disabled")

    def verify_override(*_args, **_kwargs):
        effects["verify"] += 1
        raise AssertionError("verifier must not run while Google login is disabled")

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_owner_session_db] = db_override
    monkeypatch.setattr(owner_google_login, "verify_google_owner_token", verify_override)
    with TestClient(app, base_url=auth.ORIGIN) as client:
        response = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": "nonce"},
            json={"id_token": "secret-token"},
        )
    assert response.status_code == 404
    assert effects == {"db": 0, "verify": 0}


def test_invalid_or_expired_challenge_never_reaches_google_verification(
    sessions, monkeypatch
) -> None:
    effects = 0

    async def threadpool_override(*_args, **_kwargs):
        nonlocal effects
        effects += 1
        raise AssertionError("invalid challenge must stop before Google verification")

    monkeypatch.setattr(owner_sessions_api, "run_in_threadpool", threadpool_override)
    with TestClient(auth._app(sessions, _google_settings()), base_url=auth.ORIGIN) as client:
        missing = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": "arbitrary"},
            json={"id_token": "synthetic-secret-token"},
        )
        assert missing.status_code == 401

        client.cookies.set(CHALLENGE_COOKIE, "invalid-cookie", path="/")
        invalid = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": "arbitrary"},
            json={"id_token": "synthetic-secret-token"},
        )
        assert invalid.status_code == 401

        client.cookies.delete(CHALLENGE_COOKIE, path="/")
        challenge = client.get(
            "/v1/owner/auth/challenge", headers={"Origin": auth.ORIGIN}
        ).json()
        with sessions() as db:
            db.execute(
                text(
                    "UPDATE owner_os_login_challenges "
                    "SET expires_at = '2000-01-01 00:00:00'"
                )
            )
            db.commit()
        expired = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": challenge["csrf_token"]},
            json={"id_token": "synthetic-secret-token"},
        )
        assert expired.status_code == 401
    assert effects == 0


def test_pending_challenge_preflight_is_read_only(sessions) -> None:
    with sessions() as db:
        store = auth.OwnerSessionStore(db)
        challenge = store.issue_challenge()
        db.commit()
        store.validate_pending_challenge(
            challenge_cookie=challenge.cookie_token,
            challenge_csrf=challenge.csrf_token,
        )
        consumed_at = db.execute(
            text(
                "SELECT consumed_at FROM owner_os_login_challenges "
                "WHERE challenge_hash = :challenge_hash"
            ),
            {"challenge_hash": auth.challenge_hash(challenge.cookie_token, challenge.csrf_token)},
        ).scalar_one()
    assert consumed_at is None


def test_google_verification_runs_through_bounded_threadpool(
    sessions, signed_token, monkeypatch
) -> None:
    calls = 0
    real_threadpool = owner_sessions_api.run_in_threadpool

    async def tracked_threadpool(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await real_threadpool(*args, **kwargs)

    monkeypatch.setattr(owner_sessions_api, "run_in_threadpool", tracked_threadpool)
    with TestClient(auth._app(sessions, _google_settings()), base_url=auth.ORIGIN) as client:
        challenge = client.get(
            "/v1/owner/auth/challenge", headers={"Origin": auth.ORIGIN}
        ).json()
        response = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": challenge["csrf_token"]},
            json={"id_token": signed_token(nonce=challenge["csrf_token"])},
        )
    assert response.status_code == 200
    assert calls == 1


def test_google_login_reuses_challenge_session_replay_logout_and_revoke(
    sessions, signed_token
) -> None:
    settings = _google_settings(actions=True)
    with TestClient(auth._app(sessions, settings), base_url=auth.ORIGIN) as client:
        challenge = client.get(
            "/v1/owner/auth/challenge", headers={"Origin": auth.ORIGIN}
        ).json()
        token = signed_token(nonce=challenge["csrf_token"])
        login = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": challenge["csrf_token"]},
            json={"id_token": token},
        )
        assert login.status_code == 200, login.text
        assert login.json()["scopes"] == [auth.READ_SCOPE]
        assert not (set(login.json()["scopes"]) & ACTION_SCOPES)
        assert client.cookies.get(SESSION_COOKIE)
        assert client.cookies.get(CHALLENGE_COOKIE) is None
        assert client.get("/read", headers={"Origin": auth.ORIGIN}).status_code == 200

        second = client.get(
            "/v1/owner/auth/challenge", headers={"Origin": auth.ORIGIN}
        ).json()
        replay = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": second["csrf_token"]},
            json={"id_token": token},
        )
        assert replay.status_code == 401

        logout = client.post(
            "/v1/owner/auth/logout",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]},
        )
        assert logout.status_code == 204
        assert client.get("/read", headers={"Origin": auth.ORIGIN}).status_code == 401

        fresh_challenge = client.get(
            "/v1/owner/auth/challenge", headers={"Origin": auth.ORIGIN}
        ).json()
        fresh_login = client.post(
            "/v1/owner/auth/google/login",
            headers={
                "Origin": auth.ORIGIN,
                "X-CSRF-Token": fresh_challenge["csrf_token"],
            },
            json={"id_token": signed_token(nonce=fresh_challenge["csrf_token"])},
        )
        assert fresh_login.status_code == 200
        revoked = client.post(
            "/v1/owner/auth/revoke-all",
            headers={
                "Origin": auth.ORIGIN,
                "X-CSRF-Token": fresh_login.json()["csrf_token"],
            },
        )
        assert revoked.status_code == 204
        assert client.get("/read", headers={"Origin": auth.ORIGIN}).status_code == 401


def test_google_login_enforces_origin_csrf_and_never_echoes_or_logs_token(
    sessions, signed_token, monkeypatch, caplog
) -> None:
    settings = _google_settings()
    with TestClient(auth._app(sessions, settings), base_url=auth.ORIGIN) as client:
        challenge = client.get(
            "/v1/owner/auth/challenge", headers={"Origin": auth.ORIGIN}
        ).json()
        token = signed_token(nonce=challenge["csrf_token"])
        wrong_origin = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": "https://evil.example", "X-CSRF-Token": challenge["csrf_token"]},
            json={"id_token": token},
        )
        assert wrong_origin.status_code == 403
        wrong_csrf = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": "wrong"},
            json={"id_token": token},
        )
        assert wrong_csrf.status_code == 401
        assert token not in wrong_csrf.text

        monkeypatch.setattr(
            owner_google_login,
            "_fetch_google_jwks",
            lambda: (_ for _ in ()).throw(GoogleJwksUnavailable("unavailable")),
        )
        monkeypatch.setattr(owner_google_login, "_JWKS", GoogleJwksCache())
        unavailable = client.post(
            "/v1/owner/auth/google/login",
            headers={"Origin": auth.ORIGIN, "X-CSRF-Token": challenge["csrf_token"]},
            json={"id_token": token},
        )
        assert unavailable.status_code == 503
        assert token not in unavailable.text
        assert token not in caplog.text
