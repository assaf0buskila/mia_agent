"""V2-08: owner session chains, sliding renewal and reuse detection (store level).

Frozen contract: V2_WAVE2_CONTRACTS.md section 2. Every store case runs on SQLite and, when
MIA_OWNER_OS_TEST_POSTGRES_URL names the disposable loopback database, on PostgreSQL with the
real migrations. Concurrency and migration cases are PostgreSQL only.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import app.services.owner_sessions as svc
import pytest
from app.core.config import Settings
from app.db import migrate
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.unit import test_owner_sessions as auth

OWNER = auth.OWNER_ID
WORKSPACE = auth.WORKSPACE
SCOPES = frozenset({"owner-os:read:contacts", "owner-os:read:reminders"})
IDLE = 43_200
ABSOLUTE = 1_209_600
ROOT = Path(__file__).resolve().parents[2]
SESSIONS_SQL = ROOT / "migrations" / "20261011_owner_os_sessions.sql"
CHAINS_SQL = ROOT / "migrations" / "20261015_owner_os_session_chains.sql"
_TIMESTAMPS = {
    "expires_at",
    "revoked_at",
    "created_at",
    "chain_started_at",
    "authenticated_at",
    "first_used_at",
    "rotated_at",
    "grace_until",
}

# The frozen contract text, statement by statement (whitespace-normalized).
CONTRACT_STATEMENTS = [
    "ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS chain_id VARCHAR(64) NULL",
    "ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS chain_started_at TIMESTAMPTZ NULL",
    "ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS authenticated_at TIMESTAMPTZ NULL",
    "ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS first_used_at TIMESTAMPTZ NULL",
    "ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMPTZ NULL",
    "ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS grace_until TIMESTAMPTZ NULL",
    "ALTER TABLE owner_os_sessions ADD COLUMN IF NOT EXISTS successor_hash VARCHAR(64) NULL",
    "CREATE INDEX IF NOT EXISTS ix_owner_os_sessions_chain ON owner_os_sessions(chain_id)",
    "ALTER TABLE owner_os_sessions ADD CONSTRAINT ck_owner_os_sessions_chain CHECK ( "
    "(chain_id IS NULL AND chain_started_at IS NULL AND authenticated_at IS NULL) "
    "OR (chain_id IS NOT NULL AND chain_started_at IS NOT NULL AND authenticated_at IS NOT NULL "
    "AND chain_started_at <= created_at))",
    "ALTER TABLE owner_os_sessions ADD CONSTRAINT ck_owner_os_sessions_rotation CHECK ( "
    "(rotated_at IS NULL) = (grace_until IS NULL) "
    "AND (grace_until IS NULL OR grace_until <= rotated_at + INTERVAL '60 seconds') "
    "AND (successor_hash IS NULL OR rotated_at IS NOT NULL))",
]


# --------------------------------------------------------------------------- fixtures


def _pg_url() -> str:
    url = os.getenv("MIA_OWNER_OS_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OWNER_OS_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1", "::1"}
    assert parsed.database == "owner_os_test"
    return url


@contextmanager
def _pg_schema(*migrations: Path) -> Iterator[tuple[sessionmaker[Session], object, str]]:
    url = _pg_url()
    admin = create_engine(url)
    schema = "os_renew_" + uuid4().hex[:16]
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as conn:
            for path in migrations:
                migrate._apply_file(conn, path)
        yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), engine, schema
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def factory(request) -> Iterator[sessionmaker[Session]]:
    if request.param == "postgresql":
        with _pg_schema(SESSIONS_SQL, CHAINS_SQL) as (pg_factory, _engine, _schema):
            yield pg_factory
        return
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    sqlite_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with sqlite_factory() as db:
        auth._schema(db)
    yield sqlite_factory
    engine.dispose()


@pytest.fixture
def t0() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


# --------------------------------------------------------------------------- helpers


def _ts(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash(token: str) -> str:
    return svc.credential_hash(token, purpose="session")


def _row(db: Session, token: str | None = None, *, session_hash: str | None = None) -> dict:
    found = db.execute(
        text("SELECT * FROM owner_os_sessions WHERE session_hash = :h"),
        {"h": session_hash or _hash(token or "")},
    ).mappings().one()
    return {key: (_ts(value) if key in _TIMESTAMPS else value) for key, value in found.items()}


def _chain(db: Session, chain_id: str) -> list[dict]:
    rows = db.execute(
        text("SELECT * FROM owner_os_sessions WHERE chain_id = :c ORDER BY created_at"),
        {"c": chain_id},
    ).mappings().all()
    return [{k: (_ts(v) if k in _TIMESTAMPS else v) for k, v in row.items()} for row in rows]


def _live_unrotated(db: Session, chain_id: str) -> list[dict]:
    return [r for r in _chain(db, chain_id) if r["rotated_at"] is None and r["revoked_at"] is None]


def _login(
    db: Session,
    now: datetime,
    *,
    auth_at: datetime | None = None,
    ttl: int = IDLE,
    previous: str = "",
    scopes: frozenset[str] = SCOPES,
):
    store = svc.OwnerSessionStore(db)
    challenge = store.issue_challenge(now=now)
    issued = store.create_session(
        owner_id=OWNER,
        workspace_id=WORKSPACE,
        scopes=scopes,
        proof_hash=secrets.token_hex(32),
        proof_authenticated_at=auth_at or now,
        challenge_cookie=challenge.cookie_token,
        challenge_csrf=challenge.csrf_token,
        previous_cookie=previous,
        ttl_seconds=ttl,
        now=now,
    )
    db.commit()
    return issued


def _allow(_record) -> None:
    return None


def _renew(db, token, now, *, idle=IDLE, absolute=ABSOLUTE, authorize=_allow):
    issued = svc.OwnerSessionStore(db).renew(
        token, idle_seconds=idle, absolute_seconds=absolute, authorize=authorize, now=now
    )
    db.commit()
    return issued


def _denied_renew(db, token, now, *, idle=IDLE, absolute=ABSOLUTE, authorize=_allow):
    with pytest.raises(svc.OwnerSessionDenied) as denied:
        svc.OwnerSessionStore(db).renew(
            token, idle_seconds=idle, absolute_seconds=absolute, authorize=authorize, now=now
        )
    # A denial that revokes the chain must already be durable: a rollback cannot undo it.
    db.rollback()
    return denied.value


def _use(db, token, now):
    record = svc.OwnerSessionStore(db).authenticate(token, now=now)
    db.commit()
    return record


def _legacy(db: Session, token: str, *, created_at: datetime, expires_at: datetime) -> None:
    """The previous image's exact INSERT: no chain columns at all."""
    db.execute(
        text(
            """
            INSERT INTO owner_os_sessions
                (session_hash, owner_id, workspace_id, login_proof_hash, scopes,
                 expires_at, revoked_at, created_at)
            VALUES
                (:session_hash, :owner_id, :workspace_id, :login_proof_hash, :scopes,
                 :expires_at, NULL, :created_at)
            """
        ),
        {
            "session_hash": _hash(token),
            "owner_id": OWNER,
            "workspace_id": WORKSPACE,
            "login_proof_hash": secrets.token_hex(32),
            "scopes": "owner-os:read:contacts",
            "expires_at": expires_at,
            "created_at": created_at,
        },
    )
    db.commit()


# --------------------------------------------------------------------------- settings


def test_constants_and_bounded_session_windows() -> None:
    assert svc.GRACE_SECONDS == 60
    assert svc.RENEW_AGE_SECONDS == 900
    assert svc.MIN_IDLE_SECONDS == 300 and svc.MAX_IDLE_SECONDS == 43_200
    assert svc.MIN_ABSOLUTE_SECONDS == 3_600 and svc.MAX_ABSOLUTE_SECONDS == 1_209_600
    assert not hasattr(svc, "MAX_SESSION_TTL_SECONDS"), "the hard 30-minute cap is replaced"

    defaults = Settings(_env_file=None)
    assert defaults.owner_os_session_idle_seconds == 43_200
    assert defaults.owner_os_session_absolute_seconds == 1_209_600
    # Stays declared so the existing mia-env still validates.
    assert defaults.owner_os_session_ttl_seconds == 1800
    Settings(_env_file=None, owner_os_session_ttl_seconds=1800)

    for bad in (
        {"owner_os_session_idle_seconds": 299},
        {"owner_os_session_idle_seconds": 43_201},
        {"owner_os_session_absolute_seconds": 3_599},
        {"owner_os_session_absolute_seconds": 1_209_601},
        {"owner_os_session_idle_seconds": 7_200, "owner_os_session_absolute_seconds": 3_600},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **bad)
    edge = Settings(
        _env_file=None, owner_os_session_idle_seconds=3_600, owner_os_session_absolute_seconds=3_600
    )
    assert edge.owner_os_session_absolute_seconds == edge.owner_os_session_idle_seconds


# --------------------------------------------------------------------------- login


def test_login_starts_a_chain_with_proof_time_and_idle_expiry(factory, t0) -> None:
    with factory() as db:
        issued = _login(db, t0, auth_at=t0 - timedelta(seconds=30))
        row = _row(db, issued.cookie_token)
    assert re.fullmatch(r"[0-9a-f]{32}", row["chain_id"])
    assert row["chain_started_at"] == t0 == row["created_at"]
    assert row["authenticated_at"] == t0 - timedelta(seconds=30)
    assert row["expires_at"] == t0 + timedelta(seconds=IDLE)
    for column in ("first_used_at", "rotated_at", "grace_until", "successor_hash", "revoked_at"):
        assert row[column] is None, column
    record = issued.record
    assert record.chain_id == row["chain_id"]
    assert record.created_at == t0 and record.chain_started_at == t0
    assert record.authenticated_at == t0 - timedelta(seconds=30)
    assert record.renew_after == t0 + timedelta(seconds=900)


def test_login_idle_window_is_capped_at_twelve_hours(factory, t0) -> None:
    with factory() as db:
        issued = _login(db, t0, ttl=10**9)
        assert _row(db, issued.cookie_token)["expires_at"] == t0 + timedelta(seconds=43_200)


def test_login_with_a_previous_cookie_revokes_its_whole_chain(factory, t0) -> None:
    with factory() as db:
        first = _login(db, t0)
        successor = _renew(db, first.cookie_token, t0 + timedelta(seconds=100))
        # The browser still holds the predecessor (inside its grace) when it signs in again.
        fresh = _login(db, t0 + timedelta(seconds=110), previous=first.cookie_token)
        chain = _chain(db, first.record.chain_id)
        assert len(chain) == 2 and all(r["revoked_at"] is not None for r in chain)
        for token in (first.cookie_token, successor.cookie_token):
            with pytest.raises(svc.OwnerSessionDenied):
                svc.OwnerSessionStore(db).authenticate(token, now=t0 + timedelta(seconds=111))
        assert _use(db, fresh.cookie_token, t0 + timedelta(seconds=111)).owner_id == OWNER
        assert fresh.record.chain_id != first.record.chain_id


# --------------------------------------------------------------------------- authenticate


def test_authenticate_records_first_use_once(factory, t0) -> None:
    with factory() as db:
        issued = _login(db, t0)
        store = svc.OwnerSessionStore(db)
        store.authenticate(issued.cookie_token, now=t0 + timedelta(seconds=5))
        assert store.first_use_recorded is True
        db.commit()
        again = svc.OwnerSessionStore(db)
        again.authenticate(issued.cookie_token, now=t0 + timedelta(seconds=9))
        assert again.first_use_recorded is False
        db.commit()
        assert _row(db, issued.cookie_token)["first_used_at"] == t0 + timedelta(seconds=5)


def test_renew_after_is_the_earlier_of_age_and_expiry_and_now_on_a_rotated_row(
    factory, t0
) -> None:
    with factory() as db:
        issued = _login(db, t0)
        record = _use(db, issued.cookie_token, t0 + timedelta(seconds=10))
        assert record.renew_after == t0 + timedelta(seconds=900)
        assert record.expires_at == t0 + timedelta(seconds=IDLE)
        short = _login(db, t0, ttl=300)
        assert short.record.renew_after == short.record.expires_at == t0 + timedelta(seconds=300)
        _renew(db, issued.cookie_token, t0 + timedelta(seconds=20))
        rotated = _use(db, issued.cookie_token, t0 + timedelta(seconds=25))
        assert rotated.rotated_at == t0 + timedelta(seconds=20)
        assert rotated.renew_after == t0 + timedelta(seconds=25)


def test_old_token_keeps_a_60_second_grace_then_reuse_revokes_the_chain(
    factory, t0, caplog
) -> None:
    with factory() as db:
        first = _login(db, t0)
        _use(db, first.cookie_token, t0 + timedelta(seconds=1))
        t1 = t0 + timedelta(seconds=1000)
        successor = _renew(db, first.cookie_token, t1)
        assert _use(db, first.cookie_token, t1 + timedelta(seconds=59)).owner_id == OWNER
        with caplog.at_level(logging.INFO):
            with pytest.raises(svc.OwnerSessionChainReuse):
                svc.OwnerSessionStore(db).authenticate(
                    first.cookie_token, now=t1 + timedelta(seconds=60)
                )
        db.rollback()
        chain = _chain(db, first.record.chain_id)
        assert len(chain) == 2 and all(r["revoked_at"] is not None for r in chain)
        with pytest.raises(svc.OwnerSessionDenied):
            svc.OwnerSessionStore(db).authenticate(
                successor.cookie_token, now=t1 + timedelta(seconds=61)
            )
    logged = caplog.text
    assert "reason=session_chain_reuse" in logged
    for secret in (first.cookie_token, successor.cookie_token, _hash(first.cookie_token),
                   _hash(successor.cookie_token), first.record.chain_id):
        assert secret not in logged


def test_expired_rows_are_denied_at_expiry(factory, t0) -> None:
    with factory() as db:
        issued = _login(db, t0, ttl=600)
        _use(db, issued.cookie_token, t0 + timedelta(seconds=599))
        with pytest.raises(svc.OwnerSessionDenied):
            svc.OwnerSessionStore(db).authenticate(
                issued.cookie_token, now=t0 + timedelta(seconds=600)
            )


# --------------------------------------------------------------------------- renew


def test_renew_copies_identity_and_scopes_verbatim_and_rotates(factory, t0) -> None:
    stored_scopes = "zz:not-configured,owner-os:read:contacts,assaf-os:read"
    with factory() as db:
        first = _login(db, t0, auth_at=t0 - timedelta(seconds=40))
        # Unsorted, with a scope no configuration knows: the copy must be byte-for-byte.
        db.execute(
            text("UPDATE owner_os_sessions SET scopes = :s WHERE session_hash = :h"),
            {"s": stored_scopes, "h": _hash(first.cookie_token)},
        )
        db.commit()
        before = _row(db, first.cookie_token)
        t1 = t0 + timedelta(seconds=1000)
        seen = []
        issued = _renew(db, first.cookie_token, t1, authorize=seen.append)
        new = _row(db, issued.cookie_token)
        old = _row(db, first.cookie_token)
    assert issued.cookie_token != first.cookie_token
    assert svc.session_csrf_token(issued.cookie_token, secret="s") != svc.session_csrf_token(
        first.cookie_token, secret="s"
    )
    assert new["scopes"] == stored_scopes
    for column in ("owner_id", "workspace_id", "login_proof_hash", "chain_id",
                   "chain_started_at", "authenticated_at"):
        assert new[column] == before[column], column
    assert new["created_at"] == t1
    assert new["expires_at"] == t1 + timedelta(seconds=IDLE)
    for column in ("revoked_at", "first_used_at", "rotated_at", "grace_until", "successor_hash"):
        assert new[column] is None, column
    assert old["rotated_at"] == t1
    assert old["grace_until"] == t1 + timedelta(seconds=60)
    assert old["expires_at"] == t1 + timedelta(seconds=60)
    assert old["successor_hash"] == _hash(issued.cookie_token)
    assert old["revoked_at"] is None
    record = issued.record
    assert record.scopes == frozenset(stored_scopes.split(","))
    assert record.session_hash == _hash(issued.cookie_token)
    assert record.renew_after == t1 + timedelta(seconds=900)
    assert record.expires_at == new["expires_at"] and record.created_at == t1
    # The re-check under the lock sees the presented row's identity, never request data.
    assert [(r.owner_id, r.workspace_id) for r in seen] == [(OWNER, WORKSPACE)]


def test_renewal_never_resets_chain_start_or_authenticated_at(factory, t0) -> None:
    with factory() as db:
        token = _login(db, t0, auth_at=t0 - timedelta(seconds=7)).cookie_token
        chain_id = _row(db, token)["chain_id"]
        for step in range(1, 4):
            moment = t0 + timedelta(seconds=1000 * step)
            _use(db, token, moment)
            token = _renew(db, token, moment + timedelta(seconds=1)).cookie_token
        rows = _chain(db, chain_id)
    assert len(rows) == 4
    assert {r["chain_started_at"] for r in rows} == {t0}
    assert {r["authenticated_at"] for r in rows} == {t0 - timedelta(seconds=7)}
    assert [r["created_at"] for r in rows] == sorted(r["created_at"] for r in rows)


def test_absolute_cap_bounds_the_successor_and_refuses_renewal_past_it(factory, t0) -> None:
    idle, absolute = 3_600, 7_200
    with factory() as db:
        first = _login(db, t0, ttl=idle)
        second = _renew(db, first.cookie_token, t0 + timedelta(seconds=3_000),
                        idle=idle, absolute=absolute)
        assert second.record.expires_at == t0 + timedelta(seconds=6_600)
        third = _renew(db, second.cookie_token, t0 + timedelta(seconds=6_000),
                       idle=idle, absolute=absolute)
        assert third.record.expires_at == t0 + timedelta(seconds=7_200)
        assert third.record.renew_after == t0 + timedelta(seconds=6_900)
        # Even a row whose own expiry was stretched cannot outlive the chain cap.
        db.execute(
            text("UPDATE owner_os_sessions SET expires_at = :e WHERE session_hash = :h"),
            {"e": t0 + timedelta(days=30), "h": _hash(third.cookie_token)},
        )
        db.commit()
        _denied_renew(db, third.cookie_token, t0 + timedelta(seconds=7_200),
                      idle=idle, absolute=absolute)
        assert len(_chain(db, first.record.chain_id)) == 3


def test_renew_refuses_missing_expired_revoked_and_legacy_rows(factory, t0) -> None:
    with factory() as db:
        _denied_renew(db, "never-issued", t0)
        expired = _login(db, t0, ttl=600)
        _denied_renew(db, expired.cookie_token, t0 + timedelta(seconds=600))
        assert len(_chain(db, expired.record.chain_id)) == 1

        revoked = _login(db, t0 + timedelta(seconds=1))
        principal = _use(db, revoked.cookie_token, t0 + timedelta(seconds=2))
        svc.OwnerSessionStore(db).revoke_all_for_authenticated_owner(
            principal=principal, now=t0 + timedelta(seconds=3)
        )
        db.commit()
        _denied_renew(db, revoked.cookie_token, t0 + timedelta(seconds=4))
        assert len(_chain(db, revoked.record.chain_id)) == 1


def test_legacy_rows_authenticate_until_their_old_expiry_and_never_renew(factory, t0) -> None:
    with factory() as db:
        _legacy(db, "legacy-cookie", created_at=t0, expires_at=t0 + timedelta(seconds=1800))
        record = _use(db, "legacy-cookie", t0 + timedelta(seconds=1799))
        assert record.chain_id is None and record.owner_id == OWNER
        assert record.renew_after == t0 + timedelta(seconds=900)
        _denied_renew(db, "legacy-cookie", t0 + timedelta(seconds=100))
        count = db.execute(text("SELECT COUNT(*) FROM owner_os_sessions")).scalar_one()
        assert count == 1
        with pytest.raises(svc.OwnerSessionDenied):
            svc.OwnerSessionStore(db).authenticate(
                "legacy-cookie", now=t0 + timedelta(seconds=1800)
            )


def test_renew_refuses_a_chain_or_proof_at_or_before_revoked_before(factory, t0) -> None:
    with factory() as db:
        chain = _login(db, t0)
        db.execute(
            text("INSERT INTO owner_os_revocations (owner_id, revoked_before) VALUES (:o, :r)"),
            {"o": OWNER, "r": t0 + timedelta(seconds=10)},
        )
        db.commit()
        # The row itself is not revoked: only the epoch says so.
        assert _row(db, chain.cookie_token)["revoked_at"] is None
        _denied_renew(db, chain.cookie_token, t0 + timedelta(seconds=20))
        db.execute(text("DELETE FROM owner_os_revocations"))
        db.commit()

        proof = _login(db, t0 + timedelta(seconds=30), auth_at=t0 - timedelta(seconds=100))
        db.execute(
            text("INSERT INTO owner_os_revocations (owner_id, revoked_before) VALUES (:o, :r)"),
            {"o": OWNER, "r": t0 - timedelta(seconds=50)},
        )
        db.commit()
        # chain_started_at is after the epoch, but the proof time is not.
        _denied_renew(db, proof.cookie_token, t0 + timedelta(seconds=40))
        assert len(_chain(db, proof.record.chain_id)) == 1


def test_renew_rechecks_the_owner_allowlist_under_the_lock(factory, t0) -> None:
    def removed_owner(_record):
        raise svc.OwnerSessionDenied("owner left the allow-list")

    with factory() as db:
        issued = _login(db, t0)
        _denied_renew(db, issued.cookie_token, t0 + timedelta(seconds=10), authorize=removed_owner)
        rows = _chain(db, issued.record.chain_id)
    assert len(rows) == 1 and rows[0]["rotated_at"] is None


def test_renew_rejects_an_invalid_window(factory, t0) -> None:
    with factory() as db:
        issued = _login(db, t0)
        for idle, absolute in ((0, ABSOLUTE), (IDLE + 1, ABSOLUTE), (7_200, 3_600),
                               (IDLE, ABSOLUTE + 1)):
            _denied_renew(db, issued.cookie_token, t0 + timedelta(seconds=10),
                          idle=idle, absolute=absolute)
        assert len(_chain(db, issued.record.chain_id)) == 1


def test_renewing_a_rotated_token_after_its_grace_revokes_the_chain(factory, t0, caplog) -> None:
    with factory() as db:
        first = _login(db, t0)
        t1 = t0 + timedelta(seconds=1000)
        successor = _renew(db, first.cookie_token, t1)
        with caplog.at_level(logging.INFO):
            denied = _denied_renew(db, first.cookie_token, t1 + timedelta(seconds=61))
        assert isinstance(denied, svc.OwnerSessionChainReuse)
        chain = _chain(db, first.record.chain_id)
        assert len(chain) == 2 and all(r["revoked_at"] is not None for r in chain)
        with pytest.raises(svc.OwnerSessionDenied):
            svc.OwnerSessionStore(db).authenticate(
                successor.cookie_token, now=t1 + timedelta(seconds=62)
            )
    assert "reason=session_chain_reuse" in caplog.text


def test_lost_response_retry_within_grace_retires_the_unused_successor(factory, t0) -> None:
    with factory() as db:
        first = _login(db, t0)
        _use(db, first.cookie_token, t0 + timedelta(seconds=1))
        t1 = t0 + timedelta(seconds=1000)
        lost = _renew(db, first.cookie_token, t1)
        retry = _renew(db, first.cookie_token, t1 + timedelta(seconds=30))
        retired = _row(db, lost.cookie_token)
        presented = _row(db, first.cookie_token)
        assert retired["rotated_at"] == retired["grace_until"] == t1 + timedelta(seconds=30)
        assert retired["expires_at"] == t1 + timedelta(seconds=30)
        assert retired["successor_hash"] is None and retired["revoked_at"] is None
        # The predecessor's grace is never extended by a retry.
        assert presented["rotated_at"] == t1
        assert presented["grace_until"] == t1 + timedelta(seconds=60)
        assert presented["expires_at"] == t1 + timedelta(seconds=60)
        assert presented["successor_hash"] == _hash(retry.cookie_token)
        live = _live_unrotated(db, first.record.chain_id)
        assert [r["session_hash"] for r in live] == [_hash(retry.cookie_token)]
        assert _use(db, retry.cookie_token, t1 + timedelta(seconds=31)).owner_id == OWNER


def test_presenting_a_retired_successor_revokes_the_chain(factory, t0) -> None:
    """A retired successor is a rotated row past its grace (contract authenticate rule 2)."""
    with factory() as db:
        first = _login(db, t0)
        t1 = t0 + timedelta(seconds=1000)
        lost = _renew(db, first.cookie_token, t1)
        retry = _renew(db, first.cookie_token, t1 + timedelta(seconds=30))
        with pytest.raises(svc.OwnerSessionChainReuse):
            svc.OwnerSessionStore(db).authenticate(
                lost.cookie_token, now=t1 + timedelta(seconds=31)
            )
        db.rollback()
        assert all(r["revoked_at"] is not None for r in _chain(db, first.record.chain_id))
        with pytest.raises(svc.OwnerSessionDenied):
            svc.OwnerSessionStore(db).authenticate(
                retry.cookie_token, now=t1 + timedelta(seconds=32)
            )


def test_a_used_successor_makes_the_presented_predecessor_revoke_the_chain(factory, t0) -> None:
    with factory() as db:
        first = _login(db, t0)
        t1 = t0 + timedelta(seconds=1000)
        successor = _renew(db, first.cookie_token, t1)
        _use(db, successor.cookie_token, t1 + timedelta(seconds=5))
        denied = _denied_renew(db, first.cookie_token, t1 + timedelta(seconds=10))
        assert isinstance(denied, svc.OwnerSessionChainReuse)
        chain = _chain(db, first.record.chain_id)
        assert len(chain) == 2 and all(r["revoked_at"] is not None for r in chain)


def test_repeated_retries_leave_exactly_one_live_unrotated_row(factory, t0) -> None:
    with factory() as db:
        first = _login(db, t0)
        t1 = t0 + timedelta(seconds=1000)
        issued = [_renew(db, first.cookie_token, t1 + timedelta(seconds=i)) for i in range(5)]
        live = _live_unrotated(db, first.record.chain_id)
        assert [r["session_hash"] for r in live] == [_hash(issued[-1].cookie_token)]
        assert _row(db, first.cookie_token)["successor_hash"] == _hash(issued[-1].cookie_token)
        assert len(_chain(db, first.record.chain_id)) == 6


def test_logout_revokes_the_whole_chain_including_a_predecessor_in_grace(factory, t0) -> None:
    with factory() as db:
        first = _login(db, t0)
        other = _login(db, t0)
        t1 = t0 + timedelta(seconds=1000)
        successor = _renew(db, first.cookie_token, t1)
        svc.OwnerSessionStore(db).revoke_current_chain(
            session_hash=_hash(first.cookie_token), owner_id=OWNER, now=t1 + timedelta(seconds=5)
        )
        db.commit()
        chain = _chain(db, first.record.chain_id)
        assert len(chain) == 2 and all(r["revoked_at"] is not None for r in chain)
        for token in (first.cookie_token, successor.cookie_token):
            with pytest.raises(svc.OwnerSessionDenied):
                svc.OwnerSessionStore(db).authenticate(token, now=t1 + timedelta(seconds=6))
        # Another chain of the same owner is a different device: it stays signed in.
        assert _use(db, other.cookie_token, t1 + timedelta(seconds=6)).owner_id == OWNER


def test_logout_of_a_legacy_row_revokes_only_that_row(factory, t0) -> None:
    with factory() as db:
        _legacy(db, "legacy-cookie", created_at=t0, expires_at=t0 + timedelta(seconds=1800))
        modern = _login(db, t0)
        svc.OwnerSessionStore(db).revoke_current_chain(
            session_hash=_hash("legacy-cookie"), owner_id=OWNER, now=t0 + timedelta(seconds=5)
        )
        db.commit()
        assert _row(db, "legacy-cookie")["revoked_at"] is not None
        assert _row(db, modern.cookie_token)["revoked_at"] is None


# --------------------------------------------------------------------- PostgreSQL only


def test_concurrent_renewals_leave_exactly_one_live_unrotated_row() -> None:
    with _pg_schema(SESSIONS_SQL, CHAINS_SQL) as (factory, _engine, _schema):
        with factory() as db:
            first = _login(db, datetime.now(UTC))
        workers = 6
        barrier = threading.Barrier(workers)
        results: list[object] = [None] * workers

        def attempt(index: int) -> None:
            with factory() as db:
                barrier.wait(timeout=10)
                try:
                    results[index] = _renew(db, first.cookie_token, None)
                except Exception as exc:  # pragma: no cover - reported by the assertion
                    results[index] = exc

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert all(isinstance(r, svc.IssuedSession) for r in results), results
        with factory() as db:
            live = _live_unrotated(db, first.record.chain_id)
            assert len(live) == 1
            assert len(_chain(db, first.record.chain_id)) == workers + 1
            assert _row(db, first.cookie_token)["successor_hash"] == live[0]["session_hash"]


def test_migration_text_is_the_frozen_contract_and_postgres_only() -> None:
    statements = migrate._split_statements(CHAINS_SQL.read_text(encoding="utf-8"))
    assert [" ".join(s.split()) for s in statements] == CONTRACT_STATEMENTS
    assert CHAINS_SQL.name in migrate.POSTGRES_ONLY
    assert b"\r" not in CHAINS_SQL.read_bytes()


def test_migration_skips_on_sqlite(tmp_path) -> None:
    from app.db.session import make_engine

    from tests.unit.test_migrate import _create_stub_tables

    engine = make_engine(f"sqlite:///{tmp_path / 'skip.db'}")
    try:
        _create_stub_tables(engine)
        summary = migrate.apply_migrations(engine)
        assert summary.failed == ""
        assert CHAINS_SQL.name in summary.skipped
    finally:
        engine.dispose()


def test_migration_on_postgres_keeps_legacy_rows_and_reruns(monkeypatch) -> None:
    now = datetime.now(UTC)
    with _pg_schema(SESSIONS_SQL) as (factory, engine, schema):
        with factory() as db:
            _legacy(db, "legacy-cookie", created_at=now - timedelta(minutes=5),
                    expires_at=now + timedelta(minutes=25))
        with engine.begin() as conn:
            migrate._apply_file(conn, CHAINS_SQL)
        with engine.begin() as conn:
            migrate._apply_file(conn, CHAINS_SQL)
        monkeypatch.setattr(migrate, "list_migration_files", lambda: [SESSIONS_SQL, CHAINS_SQL])
        first = migrate.apply_migrations(engine)
        assert first.failed == "" and first.applied == [SESSIONS_SQL.name, CHAINS_SQL.name]
        again = migrate.apply_migrations(engine)
        assert again.failed == "" and again.applied == []

        with engine.connect() as conn:
            columns = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
                        " WHERE table_schema = :s AND table_name = 'owner_os_sessions'"
                    ),
                    {"s": schema},
                )
            }
            constraints = set(
                conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint c JOIN pg_namespace n"
                        " ON n.oid = c.connamespace WHERE n.nspname = :s"
                    ),
                    {"s": schema},
                ).scalars()
            )
            index = conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = :s"
                    " AND indexname = 'ix_owner_os_sessions_chain'"
                ),
                {"s": schema},
            ).scalar()
        for column in ("chain_id", "successor_hash"):
            assert columns[column] == ("character varying", "YES"), column
        for column in ("chain_started_at", "authenticated_at", "first_used_at", "rotated_at",
                       "grace_until"):
            assert columns[column] == ("timestamp with time zone", "YES"), column
        assert {"ck_owner_os_sessions_chain", "ck_owner_os_sessions_rotation"} <= constraints
        assert index == "ix_owner_os_sessions_chain"

        with factory() as db:
            # The legacy row keeps authenticating until its old expiry and is never renewed.
            assert _use(db, "legacy-cookie", now).chain_id is None
            _denied_renew(db, "legacy-cookie", now)
            with pytest.raises(svc.OwnerSessionDenied):
                svc.OwnerSessionStore(db).authenticate(
                    "legacy-cookie", now=now + timedelta(minutes=25)
                )
            # The previous image can still insert after the migration.
            _legacy(db, "previous-image", created_at=now, expires_at=now + timedelta(minutes=30))

        valid = {
            "chain_id": "c" * 32,
            "chain_started_at": now - timedelta(seconds=10),
            "authenticated_at": now - timedelta(seconds=20),
            "rotated_at": now,
            "grace_until": now + timedelta(seconds=60),
            "successor_hash": "d" * 64,
        }
        broken = [
            {"chain_started_at": None},
            {"authenticated_at": None},
            {"chain_id": None},
            {"chain_started_at": now + timedelta(seconds=1)},
            {"grace_until": None},
            {"rotated_at": None, "successor_hash": None},
            {"grace_until": now + timedelta(seconds=61)},
            {"rotated_at": None, "grace_until": None},
        ]
        _insert_chain_row(engine, valid, now)
        for change in broken:
            with pytest.raises(IntegrityError):
                _insert_chain_row(engine, {**valid, **change}, now)


def _insert_chain_row(engine, values: dict, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO owner_os_sessions
                    (session_hash, owner_id, workspace_id, login_proof_hash, scopes,
                     expires_at, revoked_at, created_at, chain_id, chain_started_at,
                     authenticated_at, rotated_at, grace_until, successor_hash)
                VALUES
                    (:session_hash, :owner_id, 'w', :proof, 'owner-os:read:contacts',
                     :expires_at, NULL, :created_at, :chain_id, :chain_started_at,
                     :authenticated_at, :rotated_at, :grace_until, :successor_hash)
                """
            ),
            {
                "session_hash": secrets.token_hex(32),
                "owner_id": OWNER,
                "proof": secrets.token_hex(32),
                "expires_at": now + timedelta(seconds=60),
                "created_at": now,
                **values,
            },
        )
