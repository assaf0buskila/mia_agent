"""V2-08: POST /v1/owner/auth/refresh and the additive renew_after key (HTTP level).

Frozen contract: V2_WAVE2_CONTRACTS.md section 2. SQLite cases run the real router behind the
real OwnerOSBoundary. PostgreSQL cases run the whole app under the narrow runtime role when
MIA_OWNER_OS_TEST_POSTGRES_URL names the disposable loopback database.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import re
import threading
from datetime import UTC, datetime, timedelta

import app.api.owner_sessions as api
import app.services.owner_sessions as svc
import pytest
from app.api.owner_os_boundary import OwnerOSBoundary, owner_http_error, owner_validation_error
from app.api.owner_sessions import SESSION_COOKIE
from app.core.config import get_settings
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_os_postgres as pg
from tests.unit import test_owner_sessions as auth
from tests.unit.test_owner_os_postgres import os_pg as os_pg  # pytest fixture

sessions = auth.sessions
REFRESH = "/v1/owner/auth/refresh"
SESSION = "/v1/owner/auth/session"
ORIGIN = {"Origin": auth.ORIGIN}
HOST = "owner.assafweb.test"
PG_HOST = "os.example.test"
KEYS = {"authenticated", "scopes", "expires_at", "renew_after", "csrf_token", "business_binding"}
_TIMESTAMPS = {"expires_at", "revoked_at", "created_at", "chain_started_at", "authenticated_at",
               "first_used_at", "rotated_at", "grace_until"}


# --------------------------------------------------------------------------- helpers


def _app(factory, settings):
    app = auth._app(factory, settings)
    app.add_middleware(OwnerOSBoundary)
    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    return app


def _browser(factory, settings) -> TestClient:
    return TestClient(_app(factory, settings), base_url=auth.ORIGIN)


def _use_cookie(client: TestClient, value: str, host: str = HOST) -> None:
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, value, domain=host, path="/")


def _refresh(client: TestClient, csrf: str | None, **headers):
    sent = {**ORIGIN, **headers}
    if csrf is not None:
        sent["X-CSRF-Token"] = csrf
    return client.post(REFRESH, headers=sent)


def _hash(token: str) -> str:
    return svc.credential_hash(token, purpose="session")


def _ts(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: str) -> datetime:
    return _ts(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _row(factory, token: str) -> dict:
    with factory() as db:
        found = db.execute(
            text("SELECT * FROM owner_os_sessions WHERE session_hash = :h"), {"h": _hash(token)}
        ).mappings().one()
    return {k: (_ts(v) if k in _TIMESTAMPS else v) for k, v in found.items()}


def _rows(factory) -> list[dict]:
    with factory() as db:
        found = db.execute(text("SELECT * FROM owner_os_sessions")).mappings().all()
    return [{k: (_ts(v) if k in _TIMESTAMPS else v) for k, v in r.items()} for r in found]


def _update(factory, token: str, **values) -> None:
    assignments = ", ".join(f"{key} = :{key}" for key in values)
    with factory() as db:
        db.execute(
            text(f"UPDATE owner_os_sessions SET {assignments} WHERE session_hash = :h"),
            {**values, "h": _hash(token)},
        )
        db.commit()


def _set_cookie(response) -> tuple[str, str, dict[str, str | None]]:
    values = response.headers.get_list("set-cookie")
    assert len(values) == 1, values
    parts = [part.strip() for part in values[0].split(";")]
    name, _, value = parts[0].partition("=")
    attributes: dict[str, str | None] = {}
    for part in parts[1:]:
        key, separator, raw = part.partition("=")
        attributes[key.strip().lower()] = raw.strip() if separator else None
    return name, value, attributes


def _code(response) -> str:
    body = response.json()
    return body["error"]["code"] if "error" in body else body["detail"]


def _assert_cleared(response) -> None:
    name, value, attributes = _set_cookie(response)
    assert name == SESSION_COOKIE
    assert value in {"", '""'}
    assert attributes.get("max-age") == "0"
    assert attributes.get("path") == "/" and "domain" not in attributes
    assert "secure" in attributes and "httponly" in attributes
    assert (attributes.get("samesite") or "").lower() == "strict"


def _secret(settings) -> str:
    return api._session_secret(settings)


def _assert_max_age(attributes, expires_at: datetime, sent: datetime, answered: datetime) -> int:
    """Max-Age = expires_at - (clock when Set-Cookie was built), floored, never the row age."""
    max_age = int(attributes["max-age"])
    assert int((expires_at - answered).total_seconds()) <= max_age
    assert max_age <= int((expires_at - sent).total_seconds())
    return max_age


# ------------------------------------------------------------------ success contract


def test_refresh_rotates_cookie_and_csrf_and_returns_exactly_the_contract_body(sessions) -> None:
    config = assaf.settings()  # assaf-os scopes give a non-null business_binding
    with _browser(sessions, config) as client:
        logged = auth._login(client)
        old = client.cookies.get(SESSION_COOKIE)
        status = client.get(SESSION, headers=ORIGIN).json()
        sent = datetime.now(UTC)
        response = _refresh(client, logged["csrf_token"])
        answered = datetime.now(UTC)
        assert response.status_code == 200, response.text
        new = client.cookies.get(SESSION_COOKIE)
        after = client.get(SESSION, headers=ORIGIN)
    body = response.json()
    assert set(body) == KEYS
    assert body["authenticated"] is True
    assert re.fullmatch(r"[0-9a-f]{64}", body["csrf_token"])
    assert body["csrf_token"] == svc.session_csrf_token(new, secret=_secret(config))
    assert new != old and body["csrf_token"] != logged["csrf_token"]
    assert body["scopes"] == status["scopes"] == sorted(status["scopes"])
    assert re.fullmatch(r"[0-9a-f]{64}", body["business_binding"])
    assert body["business_binding"] == status["business_binding"]

    name, value, attributes = _set_cookie(response)
    assert name == SESSION_COOKIE and value == new
    assert set(attributes) == {"max-age", "path", "secure", "httponly", "samesite"}
    assert attributes["path"] == "/" and attributes["samesite"].lower() == "strict"
    assert attributes["secure"] is None and attributes["httponly"] is None

    row = _row(sessions, new)
    assert row["expires_at"] - row["created_at"] == timedelta(seconds=43_200)
    # Max-Age is expires_at minus the clock when Set-Cookie was built.
    _assert_max_age(attributes, row["expires_at"], sent, answered)
    assert _iso(body["expires_at"]) == row["expires_at"]
    assert _iso(body["renew_after"]) == min(
        row["created_at"] + timedelta(seconds=900), row["expires_at"]
    )
    assert "auth" in response.headers["server-timing"]
    assert response.headers["cache-control"] == "private, no-store"

    previous = _row(sessions, old)
    assert previous["successor_hash"] == _hash(new)
    assert previous["grace_until"] == previous["rotated_at"] + timedelta(seconds=60)
    assert after.status_code == 200 and after.json()["csrf_token"] == body["csrf_token"]


def test_refresh_requires_owner_origin_and_the_presented_cookies_csrf(sessions) -> None:
    settings = auth._settings()
    with _browser(sessions, settings) as client:
        logged = auth._login(client)
        csrf = logged["csrf_token"]
        foreign = svc.session_csrf_token("another-cookie", secret=_secret(settings))
        cases = [
            (client.post(REFRESH, headers={"X-CSRF-Token": csrf}), 403, "origin_denied"),
            (_refresh(client, csrf, Origin="https://evil.example"), 403, "origin_denied"),
            (_refresh(client, None), 403, "csrf_denied"),
            (_refresh(client, "0" * 64), 403, "csrf_denied"),
            (_refresh(client, foreign), 403, "csrf_denied"),
            (_refresh(client, csrf.upper()), 403, "csrf_denied"),
        ]
    for response, status, code in cases:
        assert response.status_code == status and _code(response) == code
        assert "server-timing" not in response.headers
        assert not response.headers.get_list("set-cookie")
    assert len(_rows(sessions)) == 1 and _rows(sessions)[0]["rotated_at"] is None


def test_a_predecessor_in_grace_retries_with_its_own_csrf(sessions) -> None:
    with _browser(sessions, auth._settings()) as client:
        logged = auth._login(client)
        old = client.cookies.get(SESSION_COOKIE)
        first = _refresh(client, logged["csrf_token"])
        assert first.status_code == 200
        lost = client.cookies.get(SESSION_COOKIE)
        # The response was lost: the browser still holds the predecessor.
        _use_cookie(client, old)
        assert _refresh(client, first.json()["csrf_token"]).status_code == 403
        retry = _refresh(client, logged["csrf_token"])
        assert retry.status_code == 200, retry.text
        current = client.cookies.get(SESSION_COOKIE)
        status = client.get(SESSION, headers=ORIGIN)
    assert current not in {old, lost}
    assert status.status_code == 200 and status.json()["csrf_token"] == retry.json()["csrf_token"]
    live = [r for r in _rows(sessions) if r["rotated_at"] is None and r["revoked_at"] is None]
    assert [r["session_hash"] for r in live] == [_hash(current)]
    assert _row(sessions, lost)["rotated_at"] == _row(sessions, lost)["grace_until"]


def test_refresh_rejects_a_query_and_a_body(sessions) -> None:
    with _browser(sessions, auth._settings()) as client:
        csrf = auth._login(client)["csrf_token"]
        query = client.post(REFRESH + "?x=1", headers={**ORIGIN, "X-CSRF-Token": csrf})
        json_body = client.post(
            REFRESH,
            headers={**ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "application/json"},
            content=b"{}",
        )
        text_body = client.post(
            REFRESH,
            headers={**ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "text/plain"},
            content=b"refresh",
        )
    assert (query.status_code, _code(query)) == (400, "invalid_query")
    assert (json_body.status_code, _code(json_body)) == (422, "validation_error")
    assert (text_body.status_code, _code(text_body)) == (415, "validation_error")
    assert all(r["rotated_at"] is None for r in _rows(sessions))
    # The handler enforces both even without the boundary in front of it.
    with TestClient(auth._app(sessions, auth._settings()), base_url=auth.ORIGIN) as bare:
        # A distinct proof: the first login's proof is consumed.
        second_proof = auth._proof(auth_date=int(datetime.now(UTC).timestamp()) - 30)
        csrf = auth._login(bare, proof=second_proof)["csrf_token"]
        bare_query = bare.post(REFRESH + "?x=1", headers={**ORIGIN, "X-CSRF-Token": csrf})
        bare_body = bare.post(
            REFRESH,
            headers={**ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "application/json"},
            content=b'{"extend": true}',
        )
    assert (bare_query.status_code, _code(bare_query)) == (400, "invalid_query")
    assert (bare_body.status_code, _code(bare_body)) == (422, "validation_error")
    assert all(r["rotated_at"] is None for r in _rows(sessions))


# ------------------------------------------------------------------- denials


@pytest.mark.parametrize(
    "case",
    ["no_cookie", "unknown", "revoked", "expired", "legacy", "absolute_cap", "owner_removed",
     "username_only", "workspace_changed", "rotated_after_grace"],
)
def test_unauthenticated_refresh_is_401_and_clears_the_cookie(sessions, case) -> None:
    settings = auth._settings()
    app = _app(sessions, settings)
    with TestClient(app, base_url=auth.ORIGIN) as client:
        logged = auth._login(client)
        cookie = client.cookies.get(SESSION_COOKIE)
        csrf = logged["csrf_token"]
        row = _row(sessions, cookie)
        if case == "no_cookie":
            client.cookies.clear()
        elif case == "unknown":
            _use_cookie(client, "never-issued-cookie")
            csrf = svc.session_csrf_token("never-issued-cookie", secret=_secret(settings))
        elif case == "revoked":
            _update(sessions, cookie, revoked_at=row["created_at"])
        elif case == "expired":
            _update(sessions, cookie, expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        elif case == "legacy":
            _update(sessions, cookie, chain_id=None, chain_started_at=None, authenticated_at=None)
        elif case == "absolute_cap":
            _update(sessions, cookie,
                    chain_started_at=row["created_at"] - timedelta(seconds=1_209_600))
        elif case == "owner_removed":
            app.dependency_overrides[get_settings] = lambda: auth._settings(owners="999")
        elif case == "username_only":
            # A username in the allow-list grants nothing: only the numeric id counts.
            app.dependency_overrides[get_settings] = lambda: auth._settings(owners="assaf_owner")
        elif case == "workspace_changed":
            app.dependency_overrides[get_settings] = lambda: auth.Settings(
                **{**auth._settings().model_dump(), "owner_os_workspace_id": "elsewhere"}
            )
        elif case == "rotated_after_grace":
            assert _refresh(client, csrf).status_code == 200
            _use_cookie(client, cookie)
            past = datetime.now(UTC) - timedelta(seconds=120)
            _update(sessions, cookie, rotated_at=past, grace_until=past + timedelta(seconds=60))
        response = _refresh(client, csrf)
    assert response.status_code == 401, response.text
    assert _code(response) == "owner_auth_required"
    assert "server-timing" not in response.headers
    _assert_cleared(response)
    rows = _rows(sessions)
    if case == "rotated_after_grace":
        assert len(rows) == 2 and all(r["revoked_at"] is not None for r in rows)
    else:
        assert len(rows) == 1 and rows[0]["successor_hash"] is None


def test_malformed_or_duplicate_cookie_is_400_invalid_cookie(sessions) -> None:
    with _browser(sessions, auth._settings()) as client:
        csrf = auth._login(client)["csrf_token"]
        cookie = client.cookies.get(SESSION_COOKIE)
        client.cookies.clear()
        duplicate = client.post(
            REFRESH,
            headers={**ORIGIN, "X-CSRF-Token": csrf,
                     "Cookie": f"{SESSION_COOKIE}={cookie}; {SESSION_COOKIE}=other"},
        )
        oversized = client.post(
            REFRESH,
            headers={**ORIGIN, "X-CSRF-Token": csrf, "Cookie": f"{SESSION_COOKIE}={'a' * 300}"},
        )
    for response in (duplicate, oversized):
        assert (response.status_code, _code(response)) == (400, "invalid_cookie")
    assert all(r["rotated_at"] is None for r in _rows(sessions))


def test_refresh_is_not_counted_as_an_auth_attempt(sessions) -> None:
    def attempts() -> int:
        with sessions() as db:
            return db.execute(text("SELECT COUNT(*) FROM owner_os_auth_attempts")).scalar_one()

    with _browser(sessions, auth._settings()) as client:
        csrf = auth._login(client)["csrf_token"]
        before = attempts()
        for _ in range(3):
            response = _refresh(client, csrf)
            assert response.status_code == 200
            csrf = response.json()["csrf_token"]
        assert _refresh(client, "0" * 64).status_code == 403
        client.cookies.clear()
        assert _refresh(client, csrf).status_code == 401
    assert attempts() == before


def test_a_store_failure_is_503_and_issues_nothing(sessions, monkeypatch, caplog) -> None:
    def broken(self, *args, **kwargs):
        raise SQLAlchemyError("synthetic failure with secret detail")

    with _browser(sessions, auth._settings()) as client:
        csrf = auth._login(client)["csrf_token"]
        monkeypatch.setattr(svc.OwnerSessionStore, "renew", broken)
        with caplog.at_level(logging.INFO):
            response = _refresh(client, csrf)
    assert (response.status_code, _code(response)) == (503, "owner_auth_unavailable")
    assert not response.headers.get_list("set-cookie")
    assert "synthetic failure" not in caplog.text and "synthetic failure" not in response.text
    assert len(_rows(sessions)) == 1


# ------------------------------------------------------- renew_after and logout


def test_login_and_session_expose_renew_after_and_a_rotated_row_is_due(sessions) -> None:
    with _browser(sessions, auth._settings()) as client:
        logged = auth._login(client)
        old = client.cookies.get(SESSION_COOKIE)
        row = _row(sessions, old)
        status = client.get(SESSION, headers=ORIGIN)
        assert _refresh(client, logged["csrf_token"]).status_code == 200
        _use_cookie(client, old)
        before = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=2)
        rotated = client.get(SESSION, headers=ORIGIN)
        after = datetime.now(UTC) + timedelta(seconds=2)
    assert set(logged) == KEYS
    assert _iso(logged["renew_after"]) == row["created_at"] + timedelta(seconds=900)
    assert set(status.json()) == KEYS
    assert status.json()["renew_after"] == logged["renew_after"]
    assert rotated.status_code == 200
    due = _iso(rotated.json()["renew_after"])
    assert before <= due <= after
    assert due <= _iso(rotated.json()["expires_at"])
    assert due < row["created_at"] + timedelta(seconds=900)


def _session_cookie_attributes(response) -> dict[str, str | None]:
    values = [
        value for value in response.headers.get_list("set-cookie")
        if value.startswith(SESSION_COOKIE + "=")
    ]
    assert len(values) == 1
    attributes: dict[str, str | None] = {}
    for part in values[0].split(";")[1:]:
        key, separator, raw = part.strip().partition("=")
        attributes[key.lower()] = raw if separator else None
    return attributes


def _slow(monkeypatch, name: str, seconds: float) -> None:
    """Delay the response after the store call, before Set-Cookie is built."""
    import time

    original = getattr(svc.OwnerSessionStore, name)

    def delayed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        time.sleep(seconds)
        return result

    monkeypatch.setattr(svc.OwnerSessionStore, name, delayed)


def test_login_cookie_max_age_is_the_idle_window(sessions) -> None:
    settings = auth._settings()
    settings.owner_os_session_idle_seconds = 3_600
    with _browser(sessions, settings) as client:
        challenge = client.get("/v1/owner/auth/challenge", headers=ORIGIN).json()
        sent = datetime.now(UTC)
        response = client.post(
            "/v1/owner/auth/login",
            headers={**ORIGIN, "X-CSRF-Token": challenge["csrf_token"]},
            json={"login_proof": auth._proof()},
        )
        answered = datetime.now(UTC)
    assert response.status_code == 200
    row = _rows(sessions)[0]
    assert row["expires_at"] - row["created_at"] == timedelta(seconds=3_600)
    _assert_max_age(_session_cookie_attributes(response), row["expires_at"], sent, answered)


def test_a_delayed_login_response_shortens_max_age(sessions, monkeypatch) -> None:
    _slow(monkeypatch, "create_session", 2.2)
    with _browser(sessions, auth._settings()) as client:
        challenge = client.get("/v1/owner/auth/challenge", headers=ORIGIN).json()
        sent = datetime.now(UTC)
        response = client.post(
            "/v1/owner/auth/login",
            headers={**ORIGIN, "X-CSRF-Token": challenge["csrf_token"]},
            json={"login_proof": auth._proof()},
        )
        answered = datetime.now(UTC)
    assert response.status_code == 200
    row = _rows(sessions)[0]
    max_age = _assert_max_age(
        _session_cookie_attributes(response), row["expires_at"], sent, answered
    )
    lifetime = int((row["expires_at"] - row["created_at"]).total_seconds())
    assert max_age <= lifetime - 2, "the cookie must not outlive the server session"


def test_a_delayed_refresh_response_shortens_max_age(sessions, monkeypatch) -> None:
    with _browser(sessions, auth._settings()) as client:
        csrf = auth._login(client)["csrf_token"]
        _slow(monkeypatch, "renew", 2.2)
        sent = datetime.now(UTC)
        response = _refresh(client, csrf)
        answered = datetime.now(UTC)
        new = client.cookies.get(SESSION_COOKIE)
    assert response.status_code == 200
    successor = _row(sessions, new)
    max_age = _assert_max_age(_set_cookie(response)[2], successor["expires_at"], sent, answered)
    lifetime = int((successor["expires_at"] - successor["created_at"]).total_seconds())
    assert max_age <= lifetime - 2


def test_logout_from_a_predecessor_in_grace_revokes_the_whole_chain(sessions) -> None:
    with _browser(sessions, auth._settings()) as client:
        logged = auth._login(client)
        old = client.cookies.get(SESSION_COOKIE)
        assert _refresh(client, logged["csrf_token"]).status_code == 200
        new = client.cookies.get(SESSION_COOKIE)
        _use_cookie(client, old)
        logout = client.post(
            "/v1/owner/auth/logout", headers={**ORIGIN, "X-CSRF-Token": logged["csrf_token"]}
        )
        assert logout.status_code == 204
        _use_cookie(client, new)
        assert client.get(SESSION, headers=ORIGIN).status_code == 401
        _use_cookie(client, old)
        assert client.get(SESSION, headers=ORIGIN).status_code == 401
    rows = _rows(sessions)
    assert len(rows) == 2 and all(r["revoked_at"] is not None for r in rows)


# ------------------------------------------------------------- scopes and identity


def test_scopes_are_stored_intersect_configured_and_never_widened(sessions) -> None:
    wide = auth._settings(actions=True)
    app = _app(sessions, wide)
    with TestClient(app, base_url=auth.ORIGIN) as client:
        csrf = auth._login(client)["csrf_token"]
        stored = _rows(sessions)[0]["scopes"]
        assert stored == "owner-os:read:contacts,quick_leads.create"
        app.dependency_overrides[get_settings] = lambda: auth._settings()
        narrowed = _refresh(client, csrf)
        assert narrowed.status_code == 200
        assert narrowed.json()["scopes"] == [auth.READ_SCOPE]
        broader = auth._settings(actions=True)
        broader.owner_os_session_scopes += ",owner-os:read:reminders,owner-os:read:activities"
        app.dependency_overrides[get_settings] = lambda: broader
        restored = _refresh(client, narrowed.json()["csrf_token"])
    assert restored.status_code == 200
    # Configuration may only narrow what is returned; it never adds to the stored set.
    assert restored.json()["scopes"] == [auth.READ_SCOPE, auth.ACTION_SCOPE]
    assert {r["scopes"] for r in _rows(sessions)} == {stored}


def test_a_username_is_never_consulted() -> None:
    for function in (svc.OwnerSessionStore.renew, svc.OwnerSessionStore.authenticate,
                     api.refresh_session, api._record_principal):
        assert "username" not in inspect.getsource(function).lower()


def test_refresh_logs_carry_no_credentials(sessions, caplog) -> None:
    settings = auth._settings()
    with caplog.at_level(logging.DEBUG):
        with _browser(sessions, settings) as client:
            logged = auth._login(client)
            old = client.cookies.get(SESSION_COOKIE)
            response = _refresh(client, logged["csrf_token"])
            new = client.cookies.get(SESSION_COOKIE)
            _use_cookie(client, old)
            past = datetime.now(UTC) - timedelta(seconds=120)
            _update(sessions, old, rotated_at=past, grace_until=past + timedelta(seconds=60))
            assert _refresh(client, logged["csrf_token"]).status_code == 401
    assert response.status_code == 200
    assert "reason=session_chain_reuse" in caplog.text
    for secret in (old, new, logged["csrf_token"], response.json()["csrf_token"], _hash(old),
                   _hash(new), hashlib.sha256(old.encode()).hexdigest()):
        assert secret not in caplog.text


def test_max_age_is_expires_at_minus_now_near_the_absolute_cap(sessions, monkeypatch) -> None:
    with _browser(sessions, auth._settings()) as client:
        csrf = auth._login(client)["csrf_token"]
        old = client.cookies.get(SESSION_COOKIE)
        row = _row(sessions, old)
        started = row["created_at"] - timedelta(seconds=1_209_600 - 1_000)
        _update(sessions, old, chain_started_at=started)
        # A slow response near the cap: the cookie must still end at the cap, not later.
        _slow(monkeypatch, "renew", 2.2)
        sent = datetime.now(UTC)
        response = _refresh(client, csrf)
        answered = datetime.now(UTC)
        new = client.cookies.get(SESSION_COOKIE)
    assert response.status_code == 200
    successor = _row(sessions, new)
    cap = started + timedelta(seconds=1_209_600)
    assert successor["expires_at"] == cap == _iso(response.json()["expires_at"])
    max_age = _assert_max_age(_set_cookie(response)[2], cap, sent, answered)
    assert max_age <= int((cap - successor["created_at"]).total_seconds()) - 2
    assert 0 < max_age <= 1_000
    assert _iso(response.json()["renew_after"]) == min(
        successor["created_at"] + timedelta(seconds=900), cap
    )


def test_an_image_without_the_chain_columns_fails_closed() -> None:
    """New code against the old schema must answer 503, never authenticate."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        auth._schema(db)
        db.execute(text("DROP INDEX ix_owner_os_sessions_chain"))
        for column in ("chain_id", "chain_started_at", "authenticated_at", "first_used_at",
                       "rotated_at", "grace_until", "successor_hash"):
            db.execute(text(f"ALTER TABLE owner_os_sessions DROP COLUMN {column}"))
        db.execute(
            text(
                "INSERT INTO owner_os_sessions (session_hash, owner_id, workspace_id,"
                " login_proof_hash, scopes, expires_at, revoked_at, created_at)"
                " VALUES (:h, :o, :w, :p, 'owner-os:read:contacts', :e, NULL, :c)"
            ),
            {"h": _hash("old-schema-cookie"), "o": auth.OWNER_ID, "w": auth.WORKSPACE,
             "p": "a" * 64, "e": datetime.now(UTC) + timedelta(minutes=20),
             "c": datetime.now(UTC)},
        )
        db.commit()
    settings = auth._settings()
    try:
        with _browser(factory, settings) as client:
            _use_cookie(client, "old-schema-cookie")
            csrf = svc.session_csrf_token("old-schema-cookie", secret=_secret(settings))
            status = client.get(SESSION, headers=ORIGIN)
            read = client.get("/read", headers=ORIGIN)
            refresh = _refresh(client, csrf)
            client.cookies.clear()
            challenge = client.get("/v1/owner/auth/challenge", headers=ORIGIN).json()
            login = client.post(
                "/v1/owner/auth/login",
                headers={**ORIGIN, "X-CSRF-Token": challenge["csrf_token"]},
                json={"login_proof": auth._proof()},
            )
        for response in (status, refresh, login):
            assert (response.status_code, _code(response)) == (503, "owner_auth_unavailable")
        assert read.status_code == 503
    finally:
        engine.dispose()


# ------------------------------------------- revocation epoch (contract erratum 2)


def _insert_legacy(factory, token: str, *, owner: str = auth.OWNER_ID,
                   workspace: str = auth.WORKSPACE) -> None:
    """A pre-V2-08 row, exactly as the previous image inserted it (no chain columns)."""
    now = datetime.now(UTC)
    with factory() as db:
        db.execute(
            text(
                "INSERT INTO owner_os_sessions (session_hash, owner_id, workspace_id,"
                " login_proof_hash, scopes, expires_at, revoked_at, created_at)"
                " VALUES (:h, :o, :w, :p, 'owner-os:read:contacts', :e, NULL, :c)"
            ),
            {"h": _hash(token), "o": owner, "w": workspace, "p": "c" * 64,
             "e": now + timedelta(minutes=25), "c": now - timedelta(minutes=5)},
        )
        db.commit()


@pytest.mark.parametrize("epoch", ["revoke_all_then_restore", "restore_epoch"])
@pytest.mark.parametrize("kind", ["chain", "legacy"])
def test_a_cookie_at_or_before_revoked_before_is_refused_everywhere(sessions, kind, epoch) -> None:
    settings = auth._settings()
    with _browser(sessions, settings) as client:
        if kind == "chain":
            csrf = auth._login(client)["csrf_token"]
            cookie = client.cookies.get(SESSION_COOKIE)
        else:
            cookie = "legacy-restored-cookie"
            _insert_legacy(sessions, cookie)
            _use_cookie(client, cookie)
            csrf = svc.session_csrf_token(cookie, secret=_secret(settings))
        assert client.get(SESSION, headers=ORIGIN).status_code == 200, "live before the epoch"
        if epoch == "revoke_all_then_restore":
            revoked = client.post(
                "/v1/owner/auth/revoke-all", headers={**ORIGIN, "X-CSRF-Token": csrf}
            )
            assert revoked.status_code == 204
            # A backup restored afterwards brings the rows back without revoked_at.
            with sessions() as db:
                db.execute(text("UPDATE owner_os_sessions SET revoked_at = NULL"))
                db.commit()
        else:
            # The restore runbook (V2-10) moves the owner's epoch to the restore time.
            with sessions() as db:
                db.execute(
                    text("INSERT INTO owner_os_revocations (owner_id, revoked_before)"
                         " VALUES (:o, :r)"),
                    {"o": auth.OWNER_ID, "r": datetime.now(UTC)},
                )
                db.commit()
        _use_cookie(client, cookie)
        status = client.get(SESSION, headers=ORIGIN)
        read = client.get("/read", headers=ORIGIN)
        refresh = _refresh(client, csrf)
    for response in (status, read, refresh):
        assert response.status_code == 401, response.text
        assert _code(response) == "owner_auth_required"
        assert "server-timing" not in response.headers
    _assert_cleared(refresh)
    assert all(r["successor_hash"] is None for r in _rows(sessions))


# ------------------------------------------------------------------ PostgreSQL


def _pg_hash_row(engine, token: str) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT * FROM owner_os_sessions WHERE session_hash = :h"), {"h": _hash(token)}
            ).mappings().one()
        )


def _pg_chain(engine, chain_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text("SELECT * FROM owner_os_sessions WHERE chain_id = :c"), {"c": chain_id}
            ).mappings()
        ]


def test_pg_refresh_under_the_runtime_role_rotates_and_enforces_the_grace(os_pg) -> None:
    client, settings, engine, _urls = os_pg
    csrf = pg.login(client)
    old = client.cookies.get(SESSION_COOKIE)
    assert client.get(pg.AUTH + "/session").status_code == 200
    refreshed = client.post(pg.AUTH + "/refresh", headers={"X-CSRF-Token": csrf})
    assert refreshed.status_code == 200, refreshed.text
    assert set(refreshed.json()) == KEYS
    new = client.cookies.get(SESSION_COOKIE)
    assert new != old
    assert client.get(pg.OS_API + "/contacts").status_code == 200
    _use_cookie(client, old, PG_HOST)
    assert client.get(pg.OS_API + "/contacts").status_code == 200, "in-flight grace"
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE owner_os_sessions SET rotated_at = now() - interval '61 seconds',"
                " grace_until = now() - interval '1 second',"
                " expires_at = now() - interval '1 second' WHERE session_hash = :h"
            ),
            {"h": _hash(old)},
        )
    assert client.get(pg.OS_API + "/contacts").status_code == 401
    _use_cookie(client, new, PG_HOST)
    assert client.get(pg.OS_API + "/contacts").status_code == 401
    chain = _pg_chain(engine, _pg_hash_row(engine, new)["chain_id"])
    assert len(chain) == 2 and all(row["revoked_at"] is not None for row in chain)


def test_pg_concurrent_refreshes_leave_one_live_successor(os_pg) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.main import app

    client, settings, engine, _urls = os_pg
    csrf = pg.login(client)
    cookie = client.cookies.get(SESSION_COOKIE)
    workers = 4
    clients = [TestClient(app, base_url=pg.ORIGIN, headers={"Origin": pg.ORIGIN})
               for _ in range(workers)]
    try:
        for each in clients:
            each.cookies.set(SESSION_COOKIE, cookie, domain=PG_HOST, path="/")
        barrier = threading.Barrier(workers)

        def attempt(index: int) -> int:
            barrier.wait(timeout=10)
            return clients[index].post(
                pg.AUTH + "/refresh", headers={"X-CSRF-Token": csrf}
            ).status_code

        with ThreadPoolExecutor(max_workers=workers) as pool:
            statuses = list(pool.map(attempt, range(workers)))
    finally:
        for each in clients:
            each.close()
    assert statuses == [200] * workers
    chain = _pg_chain(engine, _pg_hash_row(engine, cookie)["chain_id"])
    live = [r for r in chain if r["rotated_at"] is None and r["revoked_at"] is None]
    assert len(chain) == workers + 1 and len(live) == 1
    assert _pg_hash_row(engine, cookie)["successor_hash"] == live[0]["session_hash"]


def test_pg_logout_revokes_the_chain_and_refresh_is_not_an_auth_attempt(os_pg) -> None:
    client, settings, engine, _urls = os_pg
    csrf = pg.login(client)
    old = client.cookies.get(SESSION_COOKIE)
    with engine.connect() as conn:
        before = conn.scalar(text("SELECT count(*) FROM owner_os_auth_attempts"))
    assert client.post(pg.AUTH + "/refresh", headers={"X-CSRF-Token": csrf}).status_code == 200
    new = client.cookies.get(SESSION_COOKIE)
    _use_cookie(client, old, PG_HOST)
    assert client.post(pg.AUTH + "/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    for token in (new, old):
        _use_cookie(client, token, PG_HOST)
        assert client.get(pg.AUTH + "/session").status_code == 401
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM owner_os_auth_attempts")) == before


def test_pg_new_image_without_the_chain_columns_fails_closed() -> None:
    from tests.services.test_owner_session_renewal import SESSIONS_SQL, _pg_schema

    now = datetime.now(UTC)
    with _pg_schema(SESSIONS_SQL) as (factory, _engine, _schema):
        with factory() as db:
            db.execute(
                text(
                    "INSERT INTO owner_os_sessions (session_hash, owner_id, workspace_id,"
                    " login_proof_hash, scopes, expires_at, revoked_at, created_at)"
                    " VALUES (:h, :o, :w, :p, 'owner-os:read:contacts', :e, NULL, :c)"
                ),
                {"h": _hash("old-schema-cookie"), "o": auth.OWNER_ID, "w": auth.WORKSPACE,
                 "p": "b" * 64, "e": now + timedelta(minutes=20), "c": now},
            )
            db.commit()
        settings = auth._settings()
        with _browser(factory, settings) as client:
            _use_cookie(client, "old-schema-cookie")
            csrf = svc.session_csrf_token("old-schema-cookie", secret=_secret(settings))
            status = client.get(SESSION, headers=ORIGIN)
            refresh = _refresh(client, csrf)
            read = client.get("/read", headers=ORIGIN)
    for response in (status, refresh):
        assert (response.status_code, _code(response)) == (503, "owner_auth_unavailable")
    assert read.status_code == 503


@pytest.mark.parametrize("kind", ["chain", "legacy"])
def test_pg_a_cookie_at_or_before_revoked_before_is_refused_everywhere(os_pg, kind) -> None:
    client, settings, engine, _urls = os_pg
    if kind == "chain":
        csrf = pg.login(client)
        cookie = client.cookies.get(SESSION_COOKIE)
    else:
        cookie = "legacy-restored-cookie"
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO owner_os_sessions (session_hash, owner_id, workspace_id,"
                    " login_proof_hash, scopes, expires_at, revoked_at, created_at)"
                    " VALUES (:h, :o, 'assafweb', :p, 'owner-os:read:contacts', :e, NULL, :c)"
                ),
                {"h": _hash(cookie), "o": pg.OWNER, "p": "d" * 64,
                 "e": now + timedelta(minutes=25), "c": now - timedelta(minutes=5)},
            )
        _use_cookie(client, cookie, PG_HOST)
        csrf = svc.session_csrf_token(cookie, secret=_secret(settings))
    assert client.get(pg.OS_API + "/contacts").status_code == 200, "live before the epoch"
    revoked = client.post(pg.AUTH + "/revoke-all", headers={"X-CSRF-Token": csrf})
    assert revoked.status_code == 204
    # Restoring an older backup brings the rows back without revoked_at.
    with engine.begin() as conn:
        conn.execute(text("UPDATE owner_os_sessions SET revoked_at = NULL"))
    _use_cookie(client, cookie, PG_HOST)
    status = client.get(pg.AUTH + "/session")
    read = client.get(pg.OS_API + "/contacts")
    refresh = client.post(pg.AUTH + "/refresh", headers={"X-CSRF-Token": csrf})
    for response in (status, read, refresh):
        assert response.status_code == 401, response.text
        assert "server-timing" not in response.headers
    assert _code(status) == _code(refresh) == "owner_auth_required"
    _assert_cleared(refresh)
    with engine.connect() as conn:
        assert conn.scalar(
            text("SELECT count(*) FROM owner_os_sessions WHERE successor_hash IS NOT NULL")
        ) == 0
