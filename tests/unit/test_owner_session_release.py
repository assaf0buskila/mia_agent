"""OS-RUNTIME F3: ``release_session_db`` hands the owner-session connection back after auth.

The dependency chain keeps the owner-session transaction and its pooled connection open until the
request ends. A route that only calls the Apps Script afterwards (``/v1/owner/os/unified/*``) opts
in with ``release_session_db=True``: once the session, scopes, Origin, CSRF, fresh login and
mutation proof have ALL passed, the session is rolled back (only reads remain) and closed.
Every other route keeps the old lifetime.
"""

from __future__ import annotations

import logging

import app.api.owner_sessions as owner_sessions_api
import pytest
from app.api.owner_sessions import (
    OwnerSessionPrincipal,
    get_owner_session_db,
    owner_mutation_scope,
    owner_scope,
    router,
)
from app.core.config import get_settings
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
ORIGIN = {"Origin": auth.ORIGIN}


class _Recorder(Session):
    """A real session that logs the calls the release is made of."""

    events: list[str] = []

    def rollback(self):
        _Recorder.events.append("rollback")
        return super().rollback()

    def close(self):
        _Recorder.events.append("close")
        return super().close()


def _app(sessions, settings) -> tuple[FastAPI, list[str]]:
    _Recorder.events = []
    recording = sessionmaker(bind=sessions.kw["bind"], class_=_Recorder, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)

    def override_db():
        with recording() as db:
            yield db

    app.dependency_overrides[get_owner_session_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    handler_view: list[str] = []

    def at_handler() -> None:
        handler_view[:] = list(_Recorder.events)

    @app.get("/default")
    def default_route(
        principal: OwnerSessionPrincipal = Depends(owner_scope(auth.READ_SCOPE)),
    ) -> dict[str, str]:
        at_handler()
        return {"owner_id": principal.owner_id}

    @app.get("/released")
    def released_route(
        principal: OwnerSessionPrincipal = Depends(
            owner_scope(auth.READ_SCOPE, release_session_db=True)
        ),
    ) -> dict[str, str]:
        at_handler()
        return {"owner_id": principal.owner_id}

    @app.post("/released-write")
    def released_write(
        principal: OwnerSessionPrincipal = Depends(
            owner_mutation_scope(
                auth.ACTION_SCOPE, require_fresh_login=True, release_session_db=True
            )
        ),
    ) -> dict[str, str]:
        at_handler()
        return {"owner_id": principal.owner_id}

    return app, handler_view


def test_the_default_keeps_the_session_for_the_handler(sessions):
    config = auth._settings()
    app, at_handler = _app(sessions, config)
    with TestClient(app, base_url=auth.ORIGIN) as browser:
        auth._login(browser)
        _Recorder.events.clear()
        assert browser.get("/default", headers=ORIGIN).status_code == 200
    assert at_handler == [], "the default route must not release the session"


def test_a_released_read_route_is_closed_before_the_handler_runs(sessions):
    config = auth._settings()
    app, at_handler = _app(sessions, config)
    with TestClient(app, base_url=auth.ORIGIN) as browser:
        auth._login(browser)
        _Recorder.events.clear()
        response = browser.get("/released", headers=ORIGIN)
    assert response.status_code == 200 and response.json() == {"owner_id": auth.OWNER_ID}
    assert at_handler == ["rollback", "close"]
    # The dependency's own finally closes again; closing twice is harmless.
    assert _Recorder.events[-1] == "close"


def test_the_release_comes_after_csrf_fresh_login_and_the_mutation_proof(sessions, monkeypatch):
    config = auth._settings(actions=True)
    app, at_handler = _app(sessions, config)
    attach = owner_sessions_api._attach_mutation_proof

    def recording_attach(principal):
        _Recorder.events.append("proof")
        return attach(principal)

    monkeypatch.setattr(owner_sessions_api, "_attach_mutation_proof", recording_attach)
    with TestClient(app, base_url=auth.ORIGIN) as browser:
        login = auth._login(browser)
        headers = {
            **ORIGIN,
            "X-CSRF-Token": login["csrf_token"],
            "X-AssafWeb-Owner-Login": auth._proof(),
        }
        _Recorder.events.clear()
        response = browser.post("/released-write", headers=headers)
    # The fresh-login check re-reads the session through the same db: a release before it would
    # have made this 401. It passed, the mutation proof was attached, and only then the release.
    assert response.status_code == 200, response.text
    assert at_handler == ["proof", "rollback", "close"]


@pytest.mark.parametrize("case", ["csrf", "origin", "fresh_login", "no_session"])
def test_a_refusal_never_reaches_the_handler(sessions, case):
    config = auth._settings(actions=True)
    app, at_handler = _app(sessions, config)
    with TestClient(app, base_url=auth.ORIGIN) as browser:
        login = auth._login(browser)
        headers = {
            **ORIGIN,
            "X-CSRF-Token": login["csrf_token"],
            "X-AssafWeb-Owner-Login": auth._proof(),
        }
        if case == "csrf":
            headers["X-CSRF-Token"] = "w" * 64
        elif case == "origin":
            headers["Origin"] = "https://attacker.invalid"
        elif case == "fresh_login":
            headers["X-AssafWeb-Owner-Login"] = auth._proof(owner_id="99999999")
        elif case == "no_session":
            browser.cookies.clear()
        response = browser.post("/released-write", headers=headers)
    assert response.status_code in {401, 403}, (case, response.status_code)
    assert at_handler == [], "a refused request reached the handler"


def test_a_failing_rollback_is_logged_and_the_session_is_still_closed(
    sessions, monkeypatch, caplog
):
    config = auth._settings()
    app, at_handler = _app(sessions, config)

    def broken_rollback(self):
        _Recorder.events.append("rollback")
        raise SQLAlchemyError("synthetic rollback failure SENTINEL-9f3c")

    with TestClient(app, base_url=auth.ORIGIN) as browser:
        auth._login(browser)
        monkeypatch.setattr(_Recorder, "rollback", broken_rollback)
        _Recorder.events.clear()
        with caplog.at_level(logging.INFO, logger=owner_sessions_api.__name__):
            response = browser.get("/released", headers=ORIGIN)
    assert response.status_code == 200
    assert at_handler == ["rollback", "close"]
    assert "owner_auth reason=session_release_failed" in caplog.text
    assert "SENTINEL-9f3c" not in caplog.text
