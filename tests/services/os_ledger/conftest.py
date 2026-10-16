"""Fixtures for the ledger conformance suite (V2-16f).

``ledger`` is parametrized over ``["fake"]``, plus ``"pg"`` only when BOTH hold (§3.4):
  1. ``app.services.os_ledger.core`` exists (V2-16 has landed), and
  2. ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set.
A pg case is never skipped. The PG-less ``checks`` job generates none. The sentinel
(``test_pg_sentinel.py``) fails when both conditions hold and fewer than ``MIN_PG_CASES`` pg
cases were collected; ``pytest_collection_modifyitems`` below counts them.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE, LedgerEnv, build_fake_env

PG_URL_ENV = "MIA_OS_LEDGER_TEST_POSTGRES_URL"
_COUNTS_KEY = pytest.StashKey[dict]()


def core_module_present() -> bool:
    try:
        return importlib.util.find_spec("app.services.os_ledger.core") is not None
    except (ImportError, ValueError):
        return False


def pg_enabled() -> bool:
    return core_module_present() and bool(os.environ.get(PG_URL_ENV))


LEDGER_CASES = ["fake", *(["pg"] if pg_enabled() else [])]


def pytest_collection_modifyitems(config, items):
    counts = {"fake": 0, "pg": 0}
    for item in items:
        callspec = getattr(item, "callspec", None)
        if callspec is None or "conformance" not in item.path.parts:
            continue
        case = callspec.params.get("ledger")
        if case in counts:
            counts[case] += 1
    config.stash[_COUNTS_KEY] = counts


@pytest.fixture
def case_counts(request):
    return request.config.stash[_COUNTS_KEY]


def _prepare(env: LedgerEnv) -> LedgerEnv:
    """The standard workspace: epoch 1, ``core`` active, ``statements`` shadow."""
    env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    return env


def build_pg_env(url: str) -> LedgerEnv:
    """V2-16 FILLS THIS IN. The conformance suite must then pass on PG unchanged.

    Required, in this order (contract §6, §3.4, §5.5, V2-15 r2 ``role_policy.py``):
      1. apply the V2-14/14b migrations to the disposable database at ``url``;
      2. create the three ledger roles and apply ``role_policy.ledger_grant_statements()``
         directly. NEVER call ``provision_ledger`` (its targets are pinned to 127.0.0.1:56215).
         ``validate_ledger_role(..., migration_role=None)`` derives the migration identity, so the
         fixture must also arrange these preconditions:
           2a. ``GRANT assaf_os_owner TO <migration identity> WITH ADMIN TRUE, SET FALSE,
               INHERIT FALSE``, with exactly one such member (zero or several is
               ``ledger_role_denied``);
           2b. ``REVOKE TEMP ON DATABASE <test db> FROM PUBLIC``;
      3. create a demoted test main role, ``LOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB
         NOREPLICATION NOBYPASSRLS``, with no USAGE or other rights on ``assaf_os`` and no CREATE
         or TEMP, so the §5.5 guard passes; connect as the runtime role;
      4. build ``core.build_ledger(settings)`` where ``settings`` reads the ``Knobs``, and assert
         that ``role_policy.validate_ledger_role(db, main_engine, migration_role=None)`` passes
         BEFORE returning (a fixture that skips this proves nothing about the guard);
      5. return a ``LedgerEnv`` whose ``rows``/``version``/``now``/``advance`` run on the
         privileged DSN. Conformance relies on two seams: V2-16 reads ``api.LIVE_IDS_LIMIT`` at
         call time (the cases monkeypatch it instead of creating 100k rows), and it has a frozen,
         injectable clock (``advance`` shifts the ``tx.now`` the ledger uses, and cases compare
         ``deleted_at == ledger.now()``).
    ``role_policy`` lands with V2-15 and is absent on this base, so it is imported lazily here.

    Counting rule: every case under ``conformance/`` that takes the ``ledger`` fixture counts
    toward ``MIN_PG_CASES`` in ``test_pg_sentinel.py``. V2-24's §11.1 service tests are written
    against this same fixture, so they MUST live under ``conformance/`` and raise that constant;
    the sentinel recounts the directory and fails when the two disagree.
    """
    import atexit
    import re
    import sys
    from datetime import timedelta
    from pathlib import Path
    from uuid import UUID

    from app.db.os_ledger_migrate import OWNER_ROLE, RUNTIME_ROLE, TABLES, apply_ledger_migrations
    from app.services.os_ledger import api, authority, core, guard, role_policy
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    from tests.fakes.os_ledger_env import NOW, Knobs

    main_role = "v216_main"
    posture = "NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS"
    # One setup per process and URL (roles are cluster-global); every call resets the data.
    state = build_pg_env.__dict__.setdefault("state", {})
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}, "a disposable loopback database"
    assert re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", parsed.database or ""), "a plain database name"
    database = parsed.database

    def finish():
        """Undo the setup: no role, membership or grant of this fixture outlives the run."""
        setup = state.pop(url, None)
        if setup is None:
            return
        core.dispose_engines()
        admin = setup["admin"]
        try:
            with admin.begin() as c:
                c.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename = ANY(:roles) AND pid <> pg_backend_pid()"
                ), {"roles": [RUNTIME_ROLE, main_role]})
                c.execute(text("TRUNCATE " + ", ".join(
                    f"assaf_os.{name}" for name in sorted(TABLES)) + " CASCADE"))
                if setup["granted"]:
                    c.execute(text(f"REVOKE {OWNER_ROLE} FROM {setup['migration_role']}"))
                if setup["revoked_temp"]:
                    c.execute(text(f"GRANT TEMPORARY ON DATABASE {database} TO PUBLIC"))
                for name in setup["created"]:
                    c.execute(text(f"DROP OWNED BY {name}"))
                    c.execute(text(f"DROP ROLE {name}"))
        finally:
            admin.dispose()

    if url not in state:
        admin = create_engine(parsed)
        apply_ledger_migrations(admin)  # 1
        created, granted, revoked_temp = [], False, False
        with admin.begin() as c:
            migration_role = c.scalar(text("SELECT current_user"))
            for name, login in ((RUNTIME_ROLE, "LOGIN NOINHERIT"),
                                (role_policy.RETENTION_ROLE, "LOGIN NOINHERIT"),
                                (main_role, "LOGIN")):  # 2 and 3
                if c.scalar(text("SELECT to_regrole(:name) IS NULL"), {"name": name}):
                    c.execute(text(f"CREATE ROLE {name} {login} {posture}"))
                    created.append(name)
            if not c.scalar(text(  # 2a: the single ADMIN-only member is the migration identity
                "SELECT EXISTS (SELECT 1 FROM pg_auth_members WHERE roleid = to_regrole(:owner) "
                "AND member = to_regrole(:member) AND admin_option AND NOT set_option "
                "AND NOT inherit_option)"), {"owner": OWNER_ROLE, "member": migration_role}):
                c.execute(text(f"GRANT {OWNER_ROLE} TO {migration_role} "
                               "WITH ADMIN TRUE, SET FALSE, INHERIT FALSE"))
                granted = True
            if c.scalar(text(  # 2b
                "SELECT EXISTS (SELECT 1 FROM pg_database d, LATERAL aclexplode(COALESCE("
                "d.datacl, acldefault('d', d.datdba))) a WHERE d.datname = current_database() "
                "AND a.grantee = 0 AND a.privilege_type = 'TEMPORARY')")):
                c.execute(text(f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC"))
                revoked_temp = True
            c.execute(text(f"REVOKE ALL ON DATABASE {database} FROM {RUNTIME_ROLE}"))
            for statement in role_policy.ledger_grant_statements():
                c.execute(text(statement))
        state[url] = {"admin": admin, "created": created, "granted": granted,
                      "revoked_temp": revoked_temp, "migration_role": migration_role,
                      "finish": finish}
        atexit.register(finish)
        # Tear down before the first test outside this directory runs (or after the last one),
        # so a later suite (e.g. test_os_ledger_role_policy.py) never finds these roles.
        request = sys._getframe(1).f_locals.get("request")
        config = getattr(request, "config", None)
        if config is not None and "plugin" not in state:
            here = Path(__file__).resolve().parent

            class Teardown:
                @pytest.hookimpl(trylast=True)
                def pytest_runtest_teardown(self, item, nextitem):
                    if nextitem is None or here not in Path(nextitem.path).resolve().parents:
                        for key, setup in list(state.items()):
                            if key != "plugin":
                                setup["finish"]()

            state["plugin"] = Teardown()
            config.pluginmanager.register(state["plugin"], "v2-16-pg-ledger-teardown")

    admin = state[url]["admin"]
    with admin.begin() as c:  # a clean ledger for every case; grants and roles stay
        c.execute(text("TRUNCATE " + ", ".join(
            f"assaf_os.{name}" for name in sorted(TABLES)) + " CASCADE"))
    runtime_dsn = parsed.set(username=RUNTIME_ROLE, password=None).render_as_string(
        hide_password=False)
    main_dsn = parsed.set(username=main_role, password=None).render_as_string(
        hide_password=False)
    knobs = Knobs()
    offset = [timedelta()]

    def now():
        return NOW + offset[0]

    def advance(seconds):
        offset[0] += timedelta(seconds=seconds)

    class Settings:
        """The §3.3 fields the ledger reads at call time, backed by the Knobs."""

        database_url = main_dsn
        owner_os_ledger_database_url = runtime_dsn
        assaf_os_workspace_id = WORKSPACE
        owner_os_cursor_secret = ""
        assaf_os_ledger_enabled = property(lambda self: knobs.enabled)
        kill_switch = property(lambda self: knobs.kill_switch)
        assaf_os_backend = property(lambda self: knobs.backend)
        assaf_os_authority_epoch = property(lambda self: knobs.settings_epoch)
        assaf_os_freeze = property(lambda self: knobs.freeze)
        owner_undo_window_seconds = property(lambda self: knobs.undo_window_s)

    ledger = core.build_ledger(Settings(), clock=now)  # 4
    with Session(guard.engine_for(runtime_dsn)) as db:
        role_policy.validate_ledger_role(db, guard.engine_for(main_dsn), migration_role=None)
    admin_api = authority.PgAuthorityAdmin(admin, clock=now)
    admin_names = {"ensure_workspace", "state", "transition", "bump_epoch",
                   "pin_statement_pepper"}

    class Impl:
        def __getattr__(self, name):
            return getattr(admin_api if name in admin_names else ledger, name)

    order = {
        "os_workspaces": "created_at, workspace_id", "os_authority": "workspace_id, domain",
        "os_proposals": "created_at, proposal_id", "os_receipts": "commit_version",
        "os_events": "commit_version", "os_outbox": "commit_version",
        "os_import_batches": "created_at, batch_id", "os_import_rows": "batch_id, row_number",
        "os_summary_monthly": "workspace_id, month_start", "os_mirror_state": "workspace_id",
    } | {"os_" + entity: "created_at, record_id" for entity in api.ENTITIES}

    def rows(table):
        assert table in TABLES, table
        with admin.connect() as c:
            result = c.execute(text(
                f"SELECT * FROM assaf_os.{table} ORDER BY {order.get(table, '1')}"
            )).mappings().all()
        return [{k: str(v) if isinstance(v, UUID) else v for k, v in row.items()}
                for row in result]

    def version(workspace):
        with admin.connect() as c:
            value = c.scalar(text("SELECT version FROM assaf_os.os_workspaces "
                                  "WHERE workspace_id = :ws"), {"ws": workspace})
        if value is None:
            raise api.LedgerError("ledger_unavailable", reason="workspace_missing")
        return int(value)

    return LedgerEnv("pg", Impl(), knobs, rows=rows, version=version, now=now, advance=advance)


@pytest.fixture(params=LEDGER_CASES)
def ledger(request) -> LedgerEnv:
    if request.param == "fake":
        return _prepare(build_fake_env())
    if request.param == "pg":
        return _prepare(build_pg_env(os.environ[PG_URL_ENV]))
    raise AssertionError("unknown ledger case")
