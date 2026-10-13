"""Adversarial checks for the standalone owner browser-session boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.api.owner_sessions as owner_sessions_api
import pytest
from app.api.owner_sessions import (
    ACTION_SCOPES,
    CHALLENGE_COOKIE,
    SESSION_COOKIE,
    OwnerSessionPrincipal,
    get_owner_session_db,
    owner_mutation_scope,
    owner_scope,
    router,
)
from app.core.config import Settings, get_settings
from app.services.owner_sessions import (
    OwnerSessionDenied,
    OwnerSessionRateLimited,
    OwnerSessionReplay,
    OwnerSessionStore,
    challenge_hash,
    credential_hash,
    login_proof_hash,
    session_csrf_token,
)
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

OWNER_ID = "12345678"
TOKEN = "telegram-test-token"
ORIGIN = "https://owner.assafweb.test"
WORKSPACE = "assafweb"
READ_SCOPE = "owner-os:read:contacts"
ACTION_SCOPE = "quick_leads.create"


def _schema(session: Session) -> None:
    for statement in (
        """
        CREATE TABLE owner_os_login_challenges (
          challenge_hash TEXT PRIMARY KEY, expires_at TIMESTAMP NOT NULL,
          consumed_at TIMESTAMP, created_at TIMESTAMP NOT NULL)
        """,
        """
        CREATE TABLE owner_os_login_proofs (
          proof_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
          consumed_at TIMESTAMP NOT NULL)
        """,
        """
        CREATE TABLE owner_os_sessions (
          session_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL, login_proof_hash TEXT NOT NULL, scopes TEXT NOT NULL,
          expires_at TIMESTAMP NOT NULL, revoked_at TIMESTAMP,
          created_at TIMESTAMP NOT NULL)
        """,
        """
        CREATE TABLE owner_os_revocations (
          owner_id TEXT PRIMARY KEY, revoked_before TIMESTAMP NOT NULL)
        """,
        """
        CREATE TABLE owner_os_auth_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          occurred_at TIMESTAMP NOT NULL)
        """,
    ):
        session.execute(text(statement))
    session.commit()


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        _schema(db)
    yield factory
    engine.dispose()


def _settings(*, actions: bool = False, owners: str = OWNER_ID) -> Settings:
    scopes = READ_SCOPE
    if actions:
        scopes += "," + ACTION_SCOPE
    return Settings(
        _env_file=None,
        owner_os_enabled=True,
        owner_os_actions_enabled=actions,
        owner_os_origin=ORIGIN,
        owner_os_workspace_id=WORKSPACE,
        owner_os_session_database_url="postgresql://dedicated.invalid/owner_os",
        owner_os_session_ttl_seconds=1800,
        owner_os_session_scopes=scopes,
        telegram_bot_token=TOKEN,
        telegram_owner_user_ids=owners,
    )


def _proof(*, owner_id: str = OWNER_ID, auth_date: int | None = None) -> str:
    data = {
        "id": owner_id,
        "first_name": "Owner",
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
    }
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return json.dumps(data, separators=(",", ":"))


def _proof_hash(proof: str) -> str:
    return login_proof_hash(json.loads(proof)["hash"])


def _create_session(
    db: Session,
    *,
    proof: str | None = None,
    previous_cookie: str = "",
    now: datetime | None = None,
):
    moment = now or datetime.now(UTC)
    proof = proof or _proof(auth_date=int(moment.timestamp()))
    store = OwnerSessionStore(db)
    challenge = store.issue_challenge(now=moment)
    issued = store.create_session(
        owner_id=OWNER_ID,
        workspace_id=WORKSPACE,
        scopes=frozenset({READ_SCOPE}),
        proof_hash=_proof_hash(proof),
        proof_authenticated_at=datetime.fromtimestamp(int(json.loads(proof)["auth_date"]), tz=UTC),
        challenge_cookie=challenge.cookie_token,
        challenge_csrf=challenge.csrf_token,
        previous_cookie=previous_cookie,
        ttl_seconds=1800,
        now=moment,
    )
    return challenge, issued


def _app(factory: sessionmaker[Session], settings: Settings) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_owner_session_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/read")
    def read_route(
        principal: OwnerSessionPrincipal = Depends(owner_scope(READ_SCOPE)),
    ) -> dict[str, str]:
        return {"owner_id": principal.owner_id}

    @app.post("/write")
    def write_route(
        principal: OwnerSessionPrincipal = Depends(
            owner_mutation_scope(ACTION_SCOPE, require_fresh_login=True)
        ),
    ) -> dict[str, str]:
        return {"owner_id": principal.owner_id}

    return app


def _login(client: TestClient, *, proof: str | None = None) -> dict:
    challenge = client.get("/v1/owner/auth/challenge", headers={"Origin": ORIGIN})
    assert challenge.status_code == 200
    challenge_cookie = "\n".join(challenge.headers.get_list("set-cookie"))
    assert "Secure" in challenge_cookie
    assert "HttpOnly" in challenge_cookie
    assert "SameSite=strict" in challenge_cookie
    assert "Domain=" not in challenge_cookie
    csrf = challenge.json()["csrf_token"]
    response = client.post(
        "/v1/owner/auth/login",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={"login_proof": proof or _proof()},
    )
    assert response.status_code == 200, response.text
    session_cookies = "\n".join(response.headers.get_list("set-cookie"))
    assert "Secure" in session_cookies
    assert "HttpOnly" in session_cookies
    assert "SameSite=strict" in session_cookies
    assert "Domain=" not in session_cookies
    return response.json()


def test_hashes_are_domain_separated_and_no_raw_credential_is_stored(sessions) -> None:
    raw = "raw-browser-secret"
    assert credential_hash(raw, purpose="session") != raw
    assert credential_hash(raw, purpose="session") != credential_hash(raw, purpose="challenge")
    assert raw not in challenge_hash(raw, "csrf")

    with sessions() as db:
        challenge, issued = _create_session(db)
        db.commit()
        rows = " ".join(
            str(row)
            for table in (
                "owner_os_login_challenges",
                "owner_os_login_proofs",
                "owner_os_sessions",
            )
            for row in db.execute(text(f"SELECT * FROM {table}")).all()
        )
    assert challenge.cookie_token not in rows
    assert challenge.csrf_token not in rows
    assert issued.cookie_token not in rows


def test_login_consumption_rotation_and_replay_are_transactional(sessions) -> None:
    moment = datetime.now(UTC)
    proof_one = _proof(auth_date=int(moment.timestamp()))
    with sessions() as db:
        first_challenge, first = _create_session(db, proof=proof_one, now=moment)
        db.commit()
        assert OwnerSessionStore(db).authenticate(first.cookie_token).owner_id == OWNER_ID

        second_challenge = OwnerSessionStore(db).issue_challenge(now=moment + timedelta(seconds=1))
        db.commit()
        with pytest.raises(OwnerSessionReplay):
            OwnerSessionStore(db).create_session(
                owner_id=OWNER_ID,
                workspace_id=WORKSPACE,
                scopes=frozenset({READ_SCOPE}),
                proof_hash=_proof_hash(proof_one),
                proof_authenticated_at=moment,
                challenge_cookie=second_challenge.cookie_token,
                challenge_csrf=second_challenge.csrf_token,
                previous_cookie=first.cookie_token,
                ttl_seconds=1800,
                now=moment + timedelta(seconds=1),
            )
        db.rollback()

        proof_two = _proof(auth_date=int(moment.timestamp()) + 2)
        second = OwnerSessionStore(db).create_session(
            owner_id=OWNER_ID,
            workspace_id=WORKSPACE,
            scopes=frozenset({READ_SCOPE}),
            proof_hash=_proof_hash(proof_two),
            proof_authenticated_at=moment + timedelta(seconds=2),
            challenge_cookie=second_challenge.cookie_token,
            challenge_csrf=second_challenge.csrf_token,
            previous_cookie=first.cookie_token,
            ttl_seconds=1800,
            now=moment + timedelta(seconds=2),
        )
        db.commit()
        with pytest.raises(OwnerSessionDenied):
            OwnerSessionStore(db).authenticate(first.cookie_token)
        assert OwnerSessionStore(db).authenticate(second.cookie_token).owner_id == OWNER_ID
        assert first_challenge.cookie_token not in str(
            db.execute(text("SELECT * FROM owner_os_login_challenges")).all()
        )


def test_revoke_all_orders_against_login_and_rejects_pre_revoke_grants(sessions) -> None:
    moment = datetime.now(UTC).replace(microsecond=0)
    with sessions() as db:
        _, first = _create_session(db, now=moment)
        old_challenge = OwnerSessionStore(db).issue_challenge(now=moment)
        db.commit()
        record = OwnerSessionStore(db).authenticate(first.cookie_token, now=moment)
        OwnerSessionStore(db).revoke_all_for_authenticated_owner(
            principal=record, now=moment + timedelta(seconds=1)
        )
        db.commit()
        with pytest.raises(OwnerSessionDenied):
            OwnerSessionStore(db).authenticate(
                first.cookie_token, now=moment + timedelta(seconds=2)
            )
        with pytest.raises(OwnerSessionReplay):
            OwnerSessionStore(db).create_session(
                owner_id=OWNER_ID,
                workspace_id=WORKSPACE,
                scopes=frozenset({READ_SCOPE}),
                proof_hash="a" * 64,
                proof_authenticated_at=moment,
                challenge_cookie=old_challenge.cookie_token,
                challenge_csrf=old_challenge.csrf_token,
                previous_cookie="",
                ttl_seconds=1800,
                now=moment + timedelta(seconds=2),
            )


def test_late_older_revoke_all_cannot_regress_owner_epoch(sessions) -> None:
    moment = datetime.now(UTC).replace(microsecond=0)
    with sessions() as db:
        _, issued = _create_session(db, now=moment)
        challenge = OwnerSessionStore(db).issue_challenge(now=moment + timedelta(seconds=2))
        db.commit()
        principal = OwnerSessionStore(db).authenticate(issued.cookie_token, now=moment)

        store = OwnerSessionStore(db)
        store.revoke_all_for_authenticated_owner(
            principal=principal,
            now=moment + timedelta(seconds=10),
        )
        db.commit()
        # Simulate a transaction that started earlier but acquired the owner lock later.
        store.revoke_all_for_authenticated_owner(
            principal=principal,
            now=moment + timedelta(seconds=5),
        )
        db.commit()

        raw_epoch = db.execute(
            text(
                "SELECT revoked_before FROM owner_os_revocations "
                "WHERE owner_id = :owner_id"
            ),
            {"owner_id": OWNER_ID},
        ).scalar_one()
        epoch = raw_epoch if isinstance(raw_epoch, datetime) else datetime.fromisoformat(raw_epoch)
        assert epoch.replace(tzinfo=epoch.tzinfo or UTC) == moment + timedelta(seconds=10)

        with pytest.raises(OwnerSessionReplay):
            store.create_session(
                owner_id=OWNER_ID,
                workspace_id=WORKSPACE,
                scopes=frozenset({READ_SCOPE}),
                proof_hash="b" * 64,
                proof_authenticated_at=moment + timedelta(seconds=6),
                challenge_cookie=challenge.cookie_token,
                challenge_csrf=challenge.csrf_token,
                previous_cookie="",
                ttl_seconds=1800,
                now=moment + timedelta(seconds=11),
            )


def test_expiry_and_durable_auth_attempt_budget_fail_closed(sessions) -> None:
    moment = datetime.now(UTC)
    with sessions() as db:
        _, issued = _create_session(db, now=moment)
        db.commit()
        with pytest.raises(OwnerSessionDenied):
            OwnerSessionStore(db).authenticate(
                issued.cookie_token,
                now=moment + timedelta(seconds=1801),
            )
        for _ in range(3):
            OwnerSessionStore(db).record_auth_attempt(
                "challenge", limit=3, window_seconds=60, now=moment
            )
        with pytest.raises(OwnerSessionRateLimited):
            OwnerSessionStore(db).record_auth_attempt(
                "challenge", limit=3, window_seconds=60, now=moment
            )


def test_http_login_session_scope_and_logout(sessions) -> None:
    settings = _settings()
    with TestClient(_app(sessions, settings), base_url=ORIGIN) as client:
        logged_in = _login(client)
        assert logged_in["authenticated"] is True
        assert logged_in["scopes"] == [READ_SCOPE]
        assert client.cookies.get(SESSION_COOKIE)
        assert client.cookies.get(CHALLENGE_COOKIE) is None

        status = client.get("/v1/owner/auth/session", headers={"Origin": ORIGIN})
        assert status.status_code == 200
        assert status.json()["csrf_token"] == logged_in["csrf_token"]
        assert client.get("/read", headers={"Origin": ORIGIN}).status_code == 200

        denied = client.get("/read", headers={"Origin": "https://evil.example"})
        assert denied.status_code == 403
        assert denied.headers["cache-control"] == "no-store"

        logout = client.post(
            "/v1/owner/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": logged_in["csrf_token"]},
        )
        assert logout.status_code == 204
        assert client.get("/read", headers={"Origin": ORIGIN}).status_code == 401


def test_http_rechecks_current_allowlist_and_workspace(sessions) -> None:
    settings = _settings()
    app = _app(sessions, settings)
    with TestClient(app, base_url=ORIGIN) as client:
        _login(client)
        app.dependency_overrides[get_settings] = lambda: _settings(owners="999")
        assert client.get("/read", headers={"Origin": ORIGIN}).status_code == 401

        app.dependency_overrides[get_settings] = lambda: Settings(
            **{
                **_settings().model_dump(),
                "owner_os_workspace_id": "another-workspace",
            }
        )
        assert client.get("/read", headers={"Origin": ORIGIN}).status_code == 401


def test_action_requires_gate_csrf_and_fresh_matching_signed_proof(sessions) -> None:
    settings = _settings(actions=True)
    with TestClient(_app(sessions, settings), base_url=ORIGIN) as client:
        signed_proof = _proof()
        session = _login(client, proof=signed_proof)
        base_headers = {"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        assert client.post("/write", headers=base_headers).status_code == 401
        assert (
            client.post(
                "/write",
                headers={**base_headers, "X-AssafWeb-Owner-Login": _proof(owner_id="999")},
            ).status_code
            == 401
        )
        accepted = client.post(
            "/write",
            headers={**base_headers, "X-AssafWeb-Owner-Login": signed_proof},
        )
        assert accepted.status_code == 200
        different_valid_proof = _proof(auth_date=int(time.time()) + 1)
        rejected = client.post(
            "/write",
            headers={**base_headers, "X-AssafWeb-Owner-Login": different_valid_proof},
        )
        assert rejected.status_code == 401


def test_action_scope_is_stripped_when_actions_gate_is_off(sessions) -> None:
    settings = _settings(actions=False)
    settings.owner_os_session_scopes = READ_SCOPE + "," + ACTION_SCOPE
    with TestClient(_app(sessions, settings), base_url=ORIGIN) as client:
        session = _login(client)
        response = client.post(
            "/write",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": session["csrf_token"],
                "X-AssafWeb-Owner-Login": _proof(),
            },
        )
        assert response.status_code == 403


def test_revoke_all_blocks_the_exact_action_proof_and_session(sessions) -> None:
    settings = _settings(actions=True)
    with TestClient(_app(sessions, settings), base_url=ORIGIN) as client:
        signed_proof = _proof()
        session = _login(client, proof=signed_proof)
        old_cookie = client.cookies.get(SESSION_COOKIE)
        headers = {"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]}
        revoked = client.post("/v1/owner/auth/revoke-all", headers=headers)
        assert revoked.status_code == 204

        client.cookies.set(SESSION_COOKIE, old_cookie, path="/")
        rejected = client.post(
            "/write",
            headers={**headers, "X-AssafWeb-Owner-Login": signed_proof},
        )
        assert rejected.status_code == 401


def test_duplicate_security_cookie_content_type_body_and_origin_fail_closed(sessions) -> None:
    with TestClient(_app(sessions, _settings()), base_url=ORIGIN) as client:
        assert client.get("/v1/owner/auth/challenge").status_code == 403
        challenge = client.get("/v1/owner/auth/challenge", headers={"Origin": ORIGIN}).json()
        duplicate = client.get(
            "/v1/owner/auth/session",
            headers={"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}=a; {SESSION_COOKIE}=b"},
        )
        assert duplicate.status_code == 400
        wrong_type = client.post(
            "/v1/owner/auth/login",
            headers={"Origin": ORIGIN, "X-CSRF-Token": challenge["csrf_token"]},
            content="proof",
        )
        assert wrong_type.status_code == 415
        too_large = client.post(
            "/v1/owner/auth/login",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": challenge["csrf_token"],
                "Content-Type": "application/json",
                "Content-Length": "9000",
            },
            content=b"{}",
        )
        assert too_large.status_code == 413
        assert "proof" not in too_large.text


def test_session_csrf_is_deterministic_but_secret_bound() -> None:
    first = session_csrf_token("cookie", secret="one")
    assert first == session_csrf_token("cookie", secret="one")
    assert first != session_csrf_token("cookie", secret="two")
    assert ACTION_SCOPE in ACTION_SCOPES


def test_session_engine_bounds_connect_and_pool_waits(monkeypatch) -> None:
    captured: dict = {}
    engine = create_engine("sqlite:///:memory:")

    def fake_create_engine(url: str, **kwargs):
        captured.update({"url": url, **kwargs})
        return engine

    owner_sessions_api._session_factory.cache_clear()
    monkeypatch.setattr(owner_sessions_api, "create_engine", fake_create_engine)
    built, _ = owner_sessions_api._session_factory("postgresql://runtime@db/owner")
    assert built is engine
    assert captured["connect_args"] == {"connect_timeout": 5}
    assert captured["pool_timeout"] == 5
    assert captured["pool_pre_ping"] is True
    owner_sessions_api._session_factory.cache_clear()
    engine.dispose()


def test_owner_lock_bounds_statement_and_lock_contention() -> None:
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    OwnerSessionStore(db)._lock_owner(OWNER_ID)
    statements = [str(call.args[0]) for call in db.execute.call_args_list]
    assert statements == [
        "SET LOCAL statement_timeout = '8s'",
        "SET LOCAL lock_timeout = '8s'",
        "SELECT pg_advisory_xact_lock(hashtext(:key))",
    ]
