"""The §5.5 guard of the PG ledger (V2-16): every refusal is ``ledger_unavailable``.

Per engine: ``role_policy.validate_ledger_role(db, main_engine, migration_role=None)`` and ``SHOW
fsync`` = on, cached, re-run every 10 minutes and after any new pooled connection. Per transaction,
in the lock's round trip: ``synchronous_commit = on`` and ``current_user = session_user =
assaf_os_ledger``. The PostgreSQL cases need ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` (CI's
os-ledger-migration-postgres job sets it, so they never skip there).
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from app.services.os_ledger import api, core, guard, role_policy
from sqlalchemy import event, text

from tests.fakes.os_ledger_builders import commit_direct, ctx, task
from tests.services.os_ledger.conftest import PG_URL_ENV, _prepare, build_pg_env, pg_enabled


@pytest.fixture
def pg(request):
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    return _prepare(build_pg_env(os.environ[PG_URL_ENV]))


def parts(env):
    ledger = env.transaction.__self__
    return ledger, ledger.settings, env.ensure_workspace.__self__._engine


@contextmanager
def refused(reason):
    with pytest.raises(api.LedgerError) as caught:
        yield
    assert (caught.value.code, caught.value.reason) == ("ledger_unavailable", reason)
    assert caught.value.write_outcome == "not_started"


def open_tx(ledger):
    with ledger.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        return tx.version


def settings_like(settings, **changes):
    fields = {name: getattr(settings, name) for name in (
        "database_url", "owner_os_ledger_database_url", "assaf_os_workspace_id",
        "owner_os_cursor_secret", "assaf_os_ledger_enabled", "kill_switch", "assaf_os_backend",
        "assaf_os_authority_epoch", "assaf_os_freeze", "owner_undo_window_seconds")}
    return SimpleNamespace(**(fields | changes))


# ---- no database needed --------------------------------------------------------------------


def test_build_ledger_refuses_a_missing_or_foreign_dsn():
    with refused("ledger_dsn_missing"):
        core.build_ledger(SimpleNamespace(owner_os_ledger_database_url="",
                                          database_url="postgresql+psycopg://u@h/d"))
    with refused("main_dsn_missing"):
        core.build_ledger(SimpleNamespace(owner_os_ledger_database_url="postgresql+psycopg://u@h/d",
                                          database_url=""))
    with refused("ledger_dsn_invalid"):
        core.build_ledger(SimpleNamespace(owner_os_ledger_database_url="sqlite:///:memory:",
                                          database_url="postgresql+psycopg://u@h/d"))
    with refused("main_dsn_invalid"):
        core.build_ledger(SimpleNamespace(owner_os_ledger_database_url="postgresql+psycopg://u@h/d2",
                                          database_url="sqlite:///:memory:"))


def test_api_build_ledger_delegates_to_core():
    with refused("ledger_dsn_missing"):
        api.build_ledger(SimpleNamespace(owner_os_ledger_database_url="", database_url=""))


@pytest.mark.parametrize("identity", [
    ("postgres", "postgres"), ("assaf_os_ledger", "postgres"), ("postgres", "assaf_os_ledger"),
    (None, None),
])
def test_the_per_transaction_identity_check_refuses_any_other_role(identity):
    with refused("guard_identity"):
        guard.check_identity(*identity)
    guard.check_identity("assaf_os_ledger", "assaf_os_ledger")


# ---- PostgreSQL ------------------------------------------------------------------------------


def test_the_guard_validates_once_and_caches_until_ten_minutes_or_a_new_connection(
        pg, monkeypatch):
    ledger, _, _ = parts(pg)
    engine_guard = ledger._guard
    calls = []
    real = role_policy.validate_ledger_role
    monkeypatch.setattr(role_policy, "validate_ledger_role",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    clock = [1000.0]
    monkeypatch.setattr(engine_guard, "_clock", lambda: clock[0])
    engine_guard.invalidate()
    open_tx(ledger)
    open_tx(ledger)
    assert len(calls) == 1
    clock[0] += guard.REVALIDATE_SECONDS - 1
    open_tx(ledger)
    assert len(calls) == 1
    clock[0] += 2
    open_tx(ledger)
    assert len(calls) == 2
    engine_guard._on_connect()  # what the pool's "connect" event calls on any new connection
    open_tx(ledger)
    assert len(calls) == 3


def test_a_runtime_dsn_that_is_not_the_ledger_role_is_refused(pg):
    _, settings, _ = parts(pg)
    impostor = settings_like(settings, owner_os_ledger_database_url=settings.database_url)
    with refused(role_policy.DENIED):
        open_tx(core.build_ledger(impostor))


def test_a_main_dsn_that_is_not_demoted_is_refused(pg):
    _, settings, admin = parts(pg)
    superuser = admin.url.render_as_string(hide_password=False)
    with refused(role_policy.DENIED):
        open_tx(core.build_ledger(settings_like(settings, database_url=superuser)))


def test_an_extra_privilege_on_the_runtime_role_is_refused(pg):
    ledger, _, admin = parts(pg)
    try:
        with admin.begin() as c:
            c.execute(text("GRANT DELETE ON assaf_os.os_tasks TO assaf_os_ledger"))
        ledger._guard.invalidate()
        with refused(role_policy.DENIED):
            open_tx(ledger)
        with refused(role_policy.DENIED):
            pg.head(ctx())  # reads stand behind the same guard
    finally:
        with admin.begin() as c:
            c.execute(text("REVOKE DELETE ON assaf_os.os_tasks FROM assaf_os_ledger"))
        ledger._guard.invalidate()
    assert open_tx(ledger) == 0


def test_fsync_off_is_refused(pg, monkeypatch):
    ledger, _, _ = parts(pg)
    monkeypatch.setattr(guard, "FSYNC_SQL", "SELECT 'off'")
    ledger._guard.invalidate()
    with refused("fsync_off"):
        open_tx(ledger)
    monkeypatch.undo()
    ledger._guard.invalidate()
    assert open_tx(ledger) == 0


def test_an_unreachable_database_is_refused_before_any_write(pg):
    _, settings, _ = parts(pg)
    gone = settings.owner_os_ledger_database_url.rsplit("/", 1)[0] + "/v216_no_such_database"
    with refused(role_policy.DENIED):  # validate_ledger_role sanitizes every catalog failure
        open_tx(core.build_ledger(settings_like(settings, owner_os_ledger_database_url=gone)))


def test_every_transaction_runs_with_synchronous_commit_on(pg):
    ledger, _, _ = parts(pg)
    engine = ledger._engine

    def session_off(dbapi_connection, _record):
        dbapi_connection.autocommit = True
        dbapi_connection.execute("SET synchronous_commit = off")
        dbapi_connection.autocommit = False

    event.listen(engine, "connect", session_off)
    try:
        engine.dispose()  # every new pooled connection now starts with synchronous_commit off
        with engine.connect() as c:
            assert c.execute(text("SHOW synchronous_commit")).scalar() == "off"
        with ledger.transaction(ctx(), domain="core", kind="owner_direct") as tx:
            assert tx.connection.execute(text("SHOW synchronous_commit")).scalar() == "on"
            assert tx.connection.execute(text("SELECT current_user")).scalar() == (
                "assaf_os_ledger")
        commit_direct(pg, ctx(), [task()])
    finally:
        event.remove(engine, "connect", session_off)
        engine.dispose()


def test_a_disabled_ledger_refuses_writes_and_reads(pg):
    """Expiry still runs while disabled (conformance: expire admission under every switch)."""
    pg.knobs.enabled = False
    with refused("ledger_disabled"):
        open_tx(pg.transaction.__self__)
    with refused("ledger_disabled"):
        pg.head(ctx())
