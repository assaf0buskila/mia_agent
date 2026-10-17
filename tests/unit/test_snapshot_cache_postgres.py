"""V2-42: the snapshot cache upsert, locking and privileges on a real, disposable PostgreSQL.

Opt in with MIA_OWNER_OS_TEST_POSTGRES_URL (loopback, database owner_os_test). Each test gets its
own schema and its own unprivileged role, so nothing here can touch another database or table.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.services import assaf_os_snapshot_cache as cache
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from tests.services import snapshot_cache_support as support
from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

OWNER = auth.OWNER_ID
OTHER = "87654321"
LOGGER = "mia.os_snapshot_cache"
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.fixture
def pg(monkeypatch):
    url = os.getenv("MIA_OWNER_OS_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OWNER_OS_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1", "::1"}
    assert parsed.database == "owner_os_test"
    admin = create_engine(url)
    suffix = uuid4().hex[:16]
    schema, role = f"cache_{suffix}", f"cache_role_{suffix}"
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f"CREATE ROLE {role} LOGIN NOINHERIT PASSWORD 'synthetic_local'"))
        conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {role}'))
    engine = create_engine(
        url, pool_size=16, connect_args={"options": f"-csearch_path={schema}"}
    )
    support.apply_migration(engine, times=2)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cache, "get_session_factory", lambda: factory)
    try:
        yield SimpleNamespace(
            engine=engine,
            factory=factory,
            schema=schema,
            role=role,
            qualified=f'"{schema}".{support.TABLE}',
        )
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE {role}"))
        admin.dispose()


@pytest.fixture
def config():
    value = assaf.settings()
    value.telegram_owner_user_ids = f"{OWNER},{OTHER}"
    return value


def _snapshot(config, when: datetime, marker: str = ""):
    return support.snapshot_at(assaf._snapshot(config), when, marker=marker)


def _row(pg, config, owner: str = OWNER):
    return support.read_row(pg.factory, owner, config.assaf_os_workspace_id)


# ------------------------------------------------------------------ schema and privileges


def test_the_table_has_the_contract_columns_and_types(pg):
    with pg.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type, character_maximum_length, is_nullable "
                "FROM information_schema.columns WHERE table_schema = :s AND table_name = :t "
                "ORDER BY ordinal_position"
            ),
            {"s": pg.schema, "t": support.TABLE},
        ).all()
        key = conn.execute(
            text(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON kcu.constraint_name = tc.constraint_name "
                "AND kcu.table_schema = tc.table_schema "
                "WHERE tc.table_schema = :s AND tc.table_name = :t "
                "AND tc.constraint_type = 'PRIMARY KEY' ORDER BY kcu.ordinal_position"
            ),
            {"s": pg.schema, "t": support.TABLE},
        ).scalars().all()
    assert [tuple(row) for row in rows] == [
        ("owner_id", "character varying", 32, "NO"),
        ("workspace_id", "character varying", 128, "NO"),
        ("workbook_id", "character varying", 128, "NO"),
        ("mode", "character varying", 8, "NO"),
        ("api_version", "character varying", 32, "NO"),
        ("schema_version", "character varying", 64, "NO"),
        ("binding_hash", "character varying", 64, "NO"),
        ("snapshot_json", "text", None, "NO"),
        ("snapshot_sha256", "character varying", 64, "NO"),
        ("byte_length", "integer", None, "NO"),
        ("as_of", "timestamp with time zone", None, "NO"),
        ("stored_at", "timestamp with time zone", None, "NO"),
        ("version", "bigint", None, "NO"),
    ]
    assert key == ["owner_id", "workspace_id"]


def test_the_database_enforces_the_check_constraints(pg, config):
    base = _snapshot(config, datetime.now(UTC))
    for column, bad in (
        ("mode", "STAGING"),
        ("byte_length", 1),
        ("byte_length", support.MAX_CACHED_BYTES + 1),
        ("version", 0),
    ):
        with pytest.raises(IntegrityError):
            support.seed(pg.factory, config, OWNER, base, **{column: bad})
    assert support.row_count(pg.factory) == 0
    support.seed(pg.factory, config, OWNER, base, mode="LIVE", byte_length=261_120)
    assert support.row_count(pg.factory) == 1


def test_the_table_is_granted_to_nobody_but_its_owner(pg):
    with pg.engine.connect() as conn:
        owner = conn.execute(
            text("SELECT tableowner FROM pg_tables WHERE schemaname = :s AND tablename = :t"),
            {"s": pg.schema, "t": support.TABLE},
        ).scalar_one()
        grantees = set(
            conn.execute(
                text(
                    "SELECT grantee FROM information_schema.role_table_grants "
                    "WHERE table_schema = :s AND table_name = :t"
                ),
                {"s": pg.schema, "t": support.TABLE},
            ).scalars()
        )
        assert grantees == {owner}, "the grants list only the owner"
        current = conn.execute(text("SELECT current_user")).scalar_one()
        assert current == owner
        for privilege in PRIVILEGES:
            held = conn.execute(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": pg.role, "t": pg.qualified, "p": privilege},
            ).scalar_one()
            assert held is False, f"an unrelated runtime role must not hold {privilege}"
            public = conn.execute(
                text("SELECT has_table_privilege('public', :t, :p)"),
                {"t": pg.qualified, "p": privilege},
            ).scalar_one()
            assert public is False, f"PUBLIC must not hold {privilege}"


# --------------------------------------------------------------------------- the upsert


def test_the_upsert_stores_replaces_and_refuses_an_older_snapshot(pg, config, caplog):
    now = datetime.now(UTC)
    middle = _snapshot(config, now - timedelta(seconds=30), "middle")
    oldest = _snapshot(config, now - timedelta(seconds=90), "oldest")
    newest = _snapshot(config, now - timedelta(seconds=5), "newest")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        cache.remember_snapshot(config, OWNER, middle)
        cache.remember_snapshot(config, OWNER, oldest)
        assert _row(pg, config)["version"] == 1
        cache.remember_snapshot(config, OWNER, newest)
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        "os_snapshot_cache op=write outcome=stored reason=stored",
        "os_snapshot_cache op=write outcome=skipped reason=not_newer",
        "os_snapshot_cache op=write outcome=stored reason=stored",
    ]
    row = _row(pg, config)
    assert row["version"] == 2 and row["snapshot_json"] == support.canonical(newest)
    assert support.as_utc(row["as_of"]) == support.parse_time(newest["observed_at"])
    assert support.row_count(pg.factory) == 1


def test_a_changed_binding_overwrites_even_an_older_snapshot(pg, config):
    now = datetime.now(UTC)
    cache.remember_snapshot(config, OWNER, _snapshot(config, now - timedelta(seconds=5), "a"))
    config.assaf_os_hmac_secret = "rotated-test-secret-with-at-least-32-bytes"
    older = _snapshot(config, now - timedelta(seconds=120), "b")
    cache.remember_snapshot(config, OWNER, older)
    row = _row(pg, config)
    assert row["version"] == 2 and row["snapshot_json"] == support.canonical(older)
    assert row["binding_hash"] == support.config_binding(config, OWNER)


def test_a_timestamptz_row_round_trips_into_a_hit_and_expires_into_a_miss(pg, config):
    now = datetime.now(UTC)
    snapshot = _snapshot(config, now - timedelta(seconds=42), "pg")
    cache.remember_snapshot(config, OWNER, snapshot)
    hit = cache.read_cached_snapshot(config, OWNER)
    assert hit["cached"] is True and hit["snapshot"] == snapshot
    assert 41 <= hit["age_seconds"] <= 44 and hit["cache_version"] == 1
    assert cache.read_cached_snapshot(config, OTHER) == {"cached": False}
    support.seed(
        pg.factory,
        config,
        OWNER,
        _snapshot(config, now - timedelta(days=7, seconds=5)),
    )
    assert cache.read_cached_snapshot(config, OWNER) == {"cached": False}


def test_concurrent_writers_leave_the_newest_and_a_version_per_stored_write(pg, config, caplog):
    now = datetime.now(UTC) - timedelta(seconds=5)
    stamps = [now - timedelta(seconds=offset) for offset in range(12)]
    random.Random(11).shuffle(stamps)
    barrier = threading.Barrier(len(stamps))

    def write(stamp: datetime) -> None:
        snapshot = _snapshot(config, stamp, stamp.isoformat())
        barrier.wait(timeout=15)
        cache.remember_snapshot(config, OWNER, snapshot)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with ThreadPoolExecutor(max_workers=len(stamps)) as pool:
            list(pool.map(write, stamps))
    lines = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    stored = sum("outcome=stored" in line for line in lines)
    assert len(lines) == len(stamps) and stored >= 1
    assert all(
        line.endswith(("reason=stored", "reason=not_newer")) for line in lines
    ), lines
    row = _row(pg, config)
    assert support.row_count(pg.factory) == 1
    assert support.as_utc(row["as_of"]) == max(stamps)
    assert row["version"] == stored, "every stored write bumped the version exactly once"
    assert cache.read_cached_snapshot(config, OWNER)["snapshot"]["issues"] == [
        max(stamps).isoformat()
    ]


# ------------------------------------------------------------------------- lock timeouts


def test_a_locked_table_gives_up_after_the_short_lock_timeout(pg, config, caplog):
    now = datetime.now(UTC)
    cache.remember_snapshot(config, OWNER, _snapshot(config, now - timedelta(seconds=30), "kept"))
    before = _row(pg, config)
    holder = pg.engine.connect()
    transaction = holder.begin()
    try:
        holder.execute(text(f"LOCK TABLE {support.TABLE} IN ACCESS EXCLUSIVE MODE"))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            started = time.monotonic()
            cache.remember_snapshot(config, OWNER, _snapshot(config, now, "blocked"))
            write_seconds = time.monotonic() - started
            started = time.monotonic()
            missed = cache.read_cached_snapshot(config, OWNER)
            read_seconds = time.monotonic() - started
    finally:
        transaction.rollback()
        holder.close()
    assert 1.5 <= write_seconds <= 5.0, write_seconds
    assert 1.5 <= read_seconds <= 5.0, read_seconds
    assert missed == {"cached": False}
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        "os_snapshot_cache op=write outcome=failed reason=db_unavailable",
        "os_snapshot_cache op=read outcome=failed reason=db_unavailable",
    ]
    assert _row(pg, config) == before
    cache.remember_snapshot(config, OWNER, _snapshot(config, now, "after"))
    assert _row(pg, config)["version"] == 2, "the cache works again once the lock is gone"
