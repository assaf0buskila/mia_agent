"""Shared V2-19 HTTP fixtures (tests only, no network): the real ``app.main.app`` with an in-memory
owner-session store, a live owner session, and a FakeLedger world for ``api.build_ledger``."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from app.api.owner_sessions import SESSION_COOKIE, get_owner_session_db
from app.core.config import MiaEnv, Settings, get_settings
from app.main import app
from app.services.owner_sessions import OwnerSessionStore, login_proof_hash, session_csrf_token
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE, LedgerEnv, build_fake_fixture
from tests.unit.test_owner_sessions import _schema

ORIGIN = "https://owner.assafweb.test"
SESSION_WORKSPACE = "assafweb"
SECRET = "s" * 32
OS_SCOPES = ("assaf-os:read", "assaf-os:propose", "assaf-os:approve", "assaf-os:direct")


def owner_settings(**changes) -> Settings:
    """Every owner surface on, the ledger bound to the FakeLedger workspace, postgres backend."""
    values = dict(
        _env_file=None, env=MiaEnv.TEST,
        owner_os_enabled=True, owner_os_actions_enabled=True, owner_os_origin=ORIGIN,
        owner_os_workspace_id=SESSION_WORKSPACE,
        owner_os_session_database_url="postgresql://dedicated.invalid/owner_os",
        owner_os_session_secret=SECRET, owner_os_session_scopes=",".join(OS_SCOPES),
        telegram_owner_user_ids=OWNER_ID, telegram_bot_token="telegram-test-token",
        assaf_os_enabled=True, assaf_os_backend="postgres", assaf_os_workspace_id=WORKSPACE,
        assaf_os_authority_epoch=1, assaf_os_ledger_enabled=True,
        assaf_os_owner_direct_enabled=True, kill_switch=False,
    )
    return Settings(**(values | changes))


def session_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        _schema(db)
    return factory


def open_session(factory, *, scopes=OS_SCOPES, owner: str = OWNER_ID) -> tuple[str, str]:
    """A live owner session: ``(Cookie header value, CSRF token)``."""
    moment = datetime.now(UTC)
    with factory() as db:
        store = OwnerSessionStore(db)
        challenge = store.issue_challenge(now=moment)
        issued = store.create_session(
            owner_id=owner, workspace_id=SESSION_WORKSPACE, scopes=frozenset(scopes),
            proof_hash=login_proof_hash(secrets.token_hex(32)), proof_authenticated_at=moment,
            challenge_cookie=challenge.cookie_token, challenge_csrf=challenge.csrf_token,
            previous_cookie="", ttl_seconds=1800, now=moment,
        )
        db.commit()
    csrf = session_csrf_token(issued.cookie_token, secret=SECRET)
    return f"{SESSION_COOKIE}={issued.cookie_token}", csrf


class OwnerApp:
    """``with OwnerApp(settings) as owner:`` the real app, its session store and settings bound."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or owner_settings()
        self.factory = session_factory()

    def __enter__(self) -> OwnerApp:
        def db():
            with self.factory() as session:
                yield session

        app.dependency_overrides[get_owner_session_db] = db
        app.dependency_overrides[get_settings] = lambda: self.settings
        self.client = TestClient(app, base_url=ORIGIN)
        return self

    def __exit__(self, *_exc) -> None:
        app.dependency_overrides.pop(get_owner_session_db, None)
        app.dependency_overrides.pop(get_settings, None)

    def login(self, *, scopes=OS_SCOPES) -> dict[str, str]:
        """Headers of a full browser request: Origin, the session cookie and its CSRF token."""
        cookie, csrf = open_session(self.factory, scopes=scopes)
        return {"Origin": ORIGIN, "Cookie": cookie, "X-CSRF-Token": csrf}


def ledger_world() -> LedgerEnv:
    """A FakeLedger workspace at epoch 1 with ``core`` active, wrapped as a ``LedgerEnv``."""
    fake, knobs = build_fake_fixture()
    env = LedgerEnv("fake", fake, knobs, rows=fake.rows, version=fake.version, now=fake._now,
                    advance=fake.advance)
    env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    env.fake = fake
    return env

