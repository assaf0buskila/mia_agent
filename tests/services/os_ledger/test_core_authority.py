"""Authority on PostgreSQL (V2-16, §5.4): the state machine beyond conformance, core_state, and
the lock order that keeps a transition and a write from overlapping.

The legal and illegal edges, the epoch bumps and their idempotency run in
``conformance/test_authority.py`` on both implementations. Here: what only PostgreSQL can show.
"""

import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from app.services.os_ledger import api, authority, guard, role_policy
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from tests.fakes.os_ledger_builders import commit_direct, ctx, fresh_workspace, task
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE
from tests.services.os_ledger.conftest import PG_URL_ENV, _prepare, build_pg_env, pg_enabled


@pytest.fixture
def pg(request):
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    return _prepare(build_pg_env(os.environ[PG_URL_ENV]))


def settings_of(env, **changes):
    settings = env.transaction.__self__.settings
    return SimpleNamespace(**({
        "owner_os_ledger_database_url": settings.owner_os_ledger_database_url,
        "database_url": settings.database_url, "assaf_os_workspace_id": WORKSPACE,
    } | changes))


def admin_engine(env):
    return env.ensure_workspace.__self__._engine


def test_core_authority_state_is_none_without_a_dsn():
    assert api.core_authority_state(SimpleNamespace(owner_os_ledger_database_url="")) is None


def test_core_state_reads_the_core_row_and_is_none_without_a_workspace_row(pg):
    assert api.core_authority_state(settings_of(pg)) == "active"
    assert api.core_authority_state(settings_of(pg, assaf_os_workspace_id="absent")) is None
    pg.transition(WORKSPACE, "core", "fenced", expected_epoch=1, actor_owner_id=OWNER_ID)
    assert authority.core_state(settings_of(pg)) == "fenced"


def test_core_state_is_ledger_unavailable_when_the_database_fails(pg):
    settings = settings_of(pg)
    gone = settings.owner_os_ledger_database_url.rsplit("/", 1)[0] + "/v216_no_such_database"
    with pytest.raises(api.LedgerError) as caught:
        api.core_authority_state(settings_of(pg, owner_os_ledger_database_url=gone))
    assert caught.value.code == "ledger_unavailable"


# ---- the fence's read is bounded (the 30 s cold-decide budget) ---------------------------------


@pytest.fixture
def silent_database():
    """A TCP listener that completes the handshake in the kernel backlog and never says a byte:
    a database that went away mid-connection. Returns the port."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        guard.dispose_engines()


def silent_settings(port):
    dsn = f"postgresql+psycopg://assaf_os_ledger@127.0.0.1:{port}/mia_ledger_test"
    return SimpleNamespace(
        owner_os_ledger_database_url=dsn, database_url=dsn.replace("assaf_os_ledger", "mia_app"),
        assaf_os_workspace_id=WORKSPACE)


def test_the_fence_timeouts_are_short_against_the_cold_decide_budget():
    # libpq reads a connect_timeout below 2 s as 2 s; the budget a cold decide has is 30 s.
    assert 2 <= guard.CONNECT_TIMEOUT_S <= 5
    assert 0 < authority.FENCE_STATEMENT_TIMEOUT_MS <= 5000


def test_core_state_gives_up_on_a_silent_database_within_the_connect_timeout(
        silent_database, monkeypatch):
    """Its own connection (the guard is stubbed out to isolate the read): bounded, then
    ledger_unavailable, never a wait on the operating system's own connect timeout."""
    assert guard.CONNECT_TIMEOUT_S <= 5
    monkeypatch.setattr(authority, "guard_for",
                        lambda *args: SimpleNamespace(ensure=lambda: None))
    started = time.monotonic()
    with pytest.raises(api.LedgerError) as caught:
        authority.core_state(silent_settings(silent_database))
    assert time.monotonic() - started < guard.CONNECT_TIMEOUT_S + 6
    assert (caught.value.code, caught.value.reason) == ("ledger_unavailable", "read_failed")


def test_core_state_behind_the_guard_also_gives_up_on_a_silent_database(silent_database):
    """The §5.5 guard's first validation connects too, so it is bounded the same way (and keeps
    its sanitized refusal: validate_ledger_role turns every catalog failure into one code)."""
    assert guard.CONNECT_TIMEOUT_S <= 5
    started = time.monotonic()
    with pytest.raises(api.LedgerError) as caught:
        authority.core_state(silent_settings(silent_database))
    assert time.monotonic() - started < guard.CONNECT_TIMEOUT_S + 6
    assert (caught.value.code, caught.value.reason) == (
        "ledger_unavailable", role_policy.DENIED)


def test_core_state_is_cut_off_by_the_statement_timeout_when_the_table_is_locked(pg):
    """A migration holding ACCESS EXCLUSIVE on os_authority must not hold a fence read for the
    whole cold-decide budget: the statement timeout (lock waits included) ends it."""
    settings = settings_of(pg)
    assert api.core_authority_state(settings) == "active"  # the guard has passed and is cached
    locked, release = threading.Event(), threading.Event()

    def migration():
        with admin_engine(pg).begin() as c:
            c.execute(text("LOCK TABLE assaf_os.os_authority IN ACCESS EXCLUSIVE MODE"))
            locked.set()
            release.wait(12)

    holder = threading.Thread(target=migration)
    holder.start()
    try:
        assert locked.wait(10)
        started = time.monotonic()
        with pytest.raises(api.LedgerError) as caught:
            authority.core_state(settings)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(15)
    assert (caught.value.code, caught.value.reason) == ("ledger_unavailable", "read_failed")
    assert elapsed < authority.FENCE_STATEMENT_TIMEOUT_MS / 1000 + 4
    assert authority.core_state(settings) == "active"  # the lock gone, it reads again


@pytest.mark.parametrize("statement", [
    "UPDATE assaf_os.os_authority SET state = 'active'",
    "UPDATE assaf_os.os_workspaces SET epoch = epoch + 1",
    "UPDATE assaf_os.os_workspaces SET statement_pepper_fp = repeat('a', 64)",
    "UPDATE assaf_os.os_receipts SET reason_codes = '[]'",
    "DELETE FROM assaf_os.os_events",
    "INSERT INTO assaf_os.os_authority (workspace_id, domain, state, epoch) "
    "VALUES ('x', 'core', 'active', 1)",
])
def test_the_runtime_role_cannot_change_authority_epoch_or_history(pg, statement):
    runtime = create_engine(settings_of(pg).owner_os_ledger_database_url)
    try:
        with pytest.raises(ProgrammingError) as caught, runtime.begin() as c:
            c.execute(text(statement))
        assert caught.value.orig.sqlstate == "42501"  # insufficient_privilege
    finally:
        runtime.dispose()


def test_a_transition_takes_the_workspace_lock_and_waits_for_an_open_write(pg):
    order = []
    entered, release = threading.Event(), threading.Event()

    def writer():
        with pg.transaction(ctx(), domain="core", kind="owner_direct"):
            entered.set()
            release.wait(10)
            order.append("write")

    def fence():
        pg.transition(WORKSPACE, "core", "fenced", expected_epoch=1, actor_owner_id=OWNER_ID)
        order.append("fence")

    first = threading.Thread(target=writer)
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=fence)
    second.start()
    time.sleep(0.5)
    assert order == []  # the transition waits on the os_workspaces row lock
    release.set()
    first.join(10)
    second.join(10)
    assert order == ["write", "fence"] and pg.state(WORKSPACE).states["core"] == "fenced"


def _waiting(engine, count):
    for _ in range(100):
        with engine.connect() as c:
            if c.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND datname = current_database()")).scalar() >= count:
                return True
        time.sleep(0.05)
    return False


def test_a_write_queued_behind_a_fence_sees_the_fence(pg):
    """A READ COMMITTED statement keeps its start snapshot, so the authority rows are read by a
    separate statement after the lock: a write that waited behind a transition sees its result."""
    entered, release = threading.Event(), threading.Event()
    outcomes = {}

    def holder():
        with pg.transaction(ctx(), domain="core", kind="owner_direct"):
            entered.set()
            release.wait(10)

    def fence():
        pg.transition(WORKSPACE, "core", "fenced", expected_epoch=1, actor_owner_id=OWNER_ID)

    def late_write():
        try:
            commit_direct(pg, ctx(), [task("TAS_late")])
            outcomes["write"] = "committed"
        except api.LedgerError as exc:
            outcomes["write"] = exc.code

    threads = [threading.Thread(target=holder)]
    threads[0].start()
    assert entered.wait(10)
    threads.append(threading.Thread(target=fence))
    threads[1].start()
    assert _waiting(admin_engine(pg), 1)
    threads.append(threading.Thread(target=late_write))
    threads[2].start()
    assert _waiting(admin_engine(pg), 2)
    release.set()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert outcomes == {"write": "authority_not_active"}
    assert pg.rows("os_tasks") == [] and pg.state(WORKSPACE).states["core"] == "fenced"


def test_d2_a_bump_retry_is_a_no_op_once_its_target_holds_at_the_next_epoch(pg):
    """PG keeps no "last bump" marker, so D2 is judged by the state: a stale caller (epoch E)
    asking for a bump target that already holds at E+1 gets the snapshot, not a refusal."""
    fresh_workspace(pg, "ws-d2", core="restoring")
    bumped = pg.transition("ws-d2", "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    assert pg.transition("ws-d2", "core", "active", expected_epoch=1,
                         actor_owner_id=OWNER_ID) == bumped
    with pytest.raises(api.LedgerError) as caught:  # not a bump target: still refused
        pg.transition("ws-d2", "core", "fenced", expected_epoch=1, actor_owner_id=OWNER_ID)
    assert (caught.value.code, caught.value.reason) == ("authority_changed", "epoch_mismatch")


def test_admin_calls_refuse_bad_arguments_before_any_write(pg):
    before = pg.rows("os_workspaces")
    for call in (
        lambda: pg.ensure_workspace("ws-x", "not-numeric", 1),
        lambda: pg.ensure_workspace("ws-x", OWNER_ID, 2),
        lambda: pg.transition(WORKSPACE, "nowhere", "active", expected_epoch=1,
                              actor_owner_id=OWNER_ID),
        lambda: pg.transition(WORKSPACE, "core", "everywhere", expected_epoch=1,
                              actor_owner_id=OWNER_ID),
        lambda: pg.pin_statement_pepper(WORKSPACE, "XYZ"),
    ):
        with pytest.raises(api.LedgerError) as caught:
            call()
        assert caught.value.code == "ledger_integrity_failed"
    assert pg.rows("os_workspaces") == before


def test_a_nested_admin_call_inside_a_transaction_fails_loudly_instead_of_deadlocking(pg):
    with pytest.raises(AssertionError, match="ledger_invariant:nested_transaction"):
        with pg.transaction(ctx(), domain="core", kind="owner_direct"):
            pg.bump_epoch(WORKSPACE, expected_epoch=1)
    assert pg.state(WORKSPACE).epoch == 1
