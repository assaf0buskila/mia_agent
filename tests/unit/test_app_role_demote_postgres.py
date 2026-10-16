"""Destructive proof restricted to the dedicated disposable 56216 cluster."""

import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "ops/gcp/vm/sql/app-role-demote.sql"

# Exact queries from V2-14 _validate_main_role_catalog (PR #157 reference copy).
LEDGER_FLAGS = (
    "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls "
    "OR NOT rolcanlogin FROM pg_roles WHERE rolname=:role"
)
LEDGER_OWNER = "SELECT datdba=to_regrole(:role) FROM pg_database WHERE datname=current_database()"
LEDGER_MEMBER = "SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=to_regrole(:role))"
LEDGER_RIGHTS = (
    "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='assaf_os' AND "
    "has_schema_privilege(:role, oid, 'USAGE,CREATE')) OR EXISTS("
    "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname='assaf_os' AND CASE "
    "WHEN c.relkind='S' THEN has_sequence_privilege(:role,c.oid,'USAGE,SELECT,UPDATE') "
    "WHEN c.relkind IN ('r','p','v','m','f') THEN "
    "has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,"
    "REFERENCES,TRIGGER') "
    "OR has_any_column_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,REFERENCES') "
    "ELSE false END) "
    "OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
    "WHERE n.nspname='assaf_os' AND has_function_privilege(:role,p.oid,'EXECUTE'))"
)


@pytest.fixture
def pg():
    raw = os.getenv("MIA_APP_ROLE_TEST_POSTGRES_URL", "")
    if not raw:
        pytest.skip("MIA_APP_ROLE_TEST_POSTGRES_URL unset")
    url = make_url(raw)
    assert url.host == "127.0.0.1" and url.port == 56216
    assert url.database == "mia_role_test" and url.username == "postgres"
    suffix = uuid4().hex[:16]
    database, role, owner = (prefix + suffix for prefix in ("v214a_", "mia_app_", "ledger_owner_"))
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    engine = app = None
    try:
        with admin.connect() as c:
            c.exec_driver_sql(f'CREATE DATABASE "{database}"')
            c.exec_driver_sql(f'CREATE ROLE "{owner}" NOLOGIN')
        engine = create_engine(url.set(database=database))
        with engine.begin() as c:
            c.exec_driver_sql(f'GRANT CREATE ON DATABASE "{database}" TO PUBLIC')
            c.exec_driver_sql(f'CREATE SCHEMA assaf_os AUTHORIZATION "{owner}"')
            c.exec_driver_sql("REVOKE ALL ON SCHEMA assaf_os FROM PUBLIC")
            c.exec_driver_sql("CREATE TABLE public.sample (id serial PRIMARY KEY, value text)")
            c.exec_driver_sql("INSERT INTO public.sample(value) VALUES ('preserved')")
            c.exec_driver_sql("CREATE SEQUENCE public.standalone")
            c.exec_driver_sql("CREATE VIEW public.sample_view AS SELECT * FROM public.sample")
            c.exec_driver_sql(
                "CREATE FUNCTION public.sample_func() RETURNS int LANGUAGE sql AS 'SELECT 1'"
            )
        app = create_engine(url.set(database=database, username=role))
        yield engine, app, role
    finally:
        if app:
            app.dispose()
        if engine:
            engine.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            c.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
            c.exec_driver_sql(f'DROP ROLE IF EXISTS "{owner}"')
        admin.dispose()


def provision(engine, role):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('mia.app_role', :role, true)"), {"role": role})
        c.exec_driver_sql(SQL.read_text(), execution_options={"no_parameters": True})


def test_ownership_exact_ledger_guard_and_rerun(pg):
    engine, app, role = pg
    provision(engine, role)
    with app.connect() as c:
        for query in (LEDGER_FLAGS, LEDGER_OWNER, LEDGER_MEMBER, LEDGER_RIGHTS):
            assert c.scalar(text(query), {"role": role}) is False
        assert c.scalar(text(LEDGER_FLAGS), {"role": "postgres"}) is True
        assert c.scalar(text("SELECT rolinherit FROM pg_roles WHERE rolname=current_user")) is False
        assert c.scalar(
            text(
                "SELECT bool_and(relowner=current_user::regrole) FROM pg_class "
                "WHERE relnamespace='public'::regnamespace"
            )
        )
        assert c.scalar(
            text(
                "SELECT bool_and(proowner=current_user::regrole) FROM pg_proc "
                "WHERE pronamespace='public'::regnamespace"
            )
        )
        assert c.scalar(text("SELECT value FROM sample_view")) == "preserved"
        assert not c.scalar(
            text("SELECT has_database_privilege(current_user,current_database(),'CREATE')")
        )
    catalog_queries = [
        "SELECT oid,xmin::text,datacl FROM pg_database ORDER BY oid",
        "SELECT oid,xmin::text,nspacl FROM pg_namespace ORDER BY oid",
        "SELECT oid,xmin::text,defaclacl FROM pg_default_acl ORDER BY oid",
        "SELECT oid,xmin::text,relowner,relacl FROM pg_class ORDER BY oid",
    ]
    with engine.connect() as c:
        catalog_before = [c.exec_driver_sql(query).all() for query in catalog_queries]
        before = c.exec_driver_sql(
            "SELECT oid, xmin::text, rolname FROM pg_authid ORDER BY oid"
        ).all()
        objects = c.exec_driver_sql("SELECT oid, relowner, relacl FROM pg_class ORDER BY oid").all()
        defaults = c.exec_driver_sql("SELECT * FROM pg_default_acl ORDER BY oid").all()
    provision(engine, role)
    with engine.connect() as c:
        assert catalog_before == [c.exec_driver_sql(query).all() for query in catalog_queries]
        assert (
            before
            == c.exec_driver_sql(
                "SELECT oid, xmin::text, rolname FROM pg_authid ORDER BY oid"
            ).all()
        )
        assert (
            objects
            == c.exec_driver_sql("SELECT oid, relowner, relacl FROM pg_class ORDER BY oid").all()
        )
        assert defaults == c.exec_driver_sql("SELECT * FROM pg_default_acl ORDER BY oid").all()


def test_deploy_migrations_and_runtime_after_demote(pg, monkeypatch, tmp_path):
    from app.core.config import get_settings
    from app.db import session
    from app.db.migrate import apply_migrations
    from app.workers import migrate

    engine, app, role = pg
    # Same metadata bootstrap as the real deployment, initially under the old superuser.
    monkeypatch.setenv("MIA_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    session.reset_engine()
    session.init_db()
    provision(engine, role)
    monkeypatch.setenv("MIA_DATABASE_URL", app.url.render_as_string(hide_password=False))
    session.reset_engine()
    try:
        assert get_settings().database_url == app.url.render_as_string(hide_password=False)
        migrate.main()
        assert session.database_ready()
        assert apply_migrations(session.get_engine()).applied == []
        pending = tmp_path / "20990101_post_demote.sql"
        pending.write_text(
            "ALTER TABLE sample ADD COLUMN later integer; CREATE TABLE future (id serial);"
        )
        monkeypatch.setattr("app.db.migrate.list_migration_files", lambda: [pending])
        assert apply_migrations(session.get_engine()).applied == [pending.name]
        with session.get_engine().begin() as c:
            c.exec_driver_sql("INSERT INTO sample(value,later) VALUES ('runtime',2)")
            assert c.exec_driver_sql("SELECT count(*) FROM sample").scalar() == 2
    finally:
        session.reset_engine()


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT USAGE ON SCHEMA assaf_os TO {role}",
        "GRANT SELECT ON assaf_os.secret TO {role}",
        "GRANT SELECT(value) ON assaf_os.secret TO {role}",
        "GRANT SELECT ON assaf_os.secret TO PUBLIC",
        "GRANT USAGE ON SEQUENCE assaf_os.secret_seq TO {role}",
        "GRANT EXECUTE ON FUNCTION assaf_os.secret_fn() TO {role}",
    ],
)
def test_posture_rejects_ledger_rights_even_without_schema_usage(pg, grant):
    from app.db.app_role_posture import inspect_posture, posture_ready

    engine, app, role = pg
    provision(engine, role)
    assert inspect_posture(app) == "app_role_ok"
    assert inspect_posture(engine) == "app_role_demote_required"
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE assaf_os.secret (value text)")
        c.exec_driver_sql("CREATE SEQUENCE assaf_os.secret_seq")
        c.exec_driver_sql(
            "CREATE FUNCTION assaf_os.secret_fn() RETURNS int LANGUAGE sql AS 'SELECT 1'"
        )
        c.exec_driver_sql("REVOKE ALL ON FUNCTION assaf_os.secret_fn() FROM PUBLIC")
        c.exec_driver_sql(grant.format(role=role))
    assert inspect_posture(app) == "app_role_ledger_access_unsafe"
    assert posture_ready(app, "observe") is True
    assert posture_ready(app, "enforce") is False
    with pytest.raises(DBAPIError) as failure:
        provision(engine, role)
    assert failure.value.orig.sqlstate == "P0001"
    assert failure.value.orig.diag.message_primary == "app_role_ledger_access_unsafe"


def test_future_objects_have_owner_rights_without_public_function_execution(pg):
    engine, app, role = pg
    provision(engine, role)
    with app.begin() as c:
        c.exec_driver_sql("CREATE TABLE new_table (id serial)")
        c.exec_driver_sql("CREATE FUNCTION new_function() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        c.exec_driver_sql("ALTER TABLE new_table ADD COLUMN value text")
        c.exec_driver_sql("INSERT INTO new_table(value) VALUES ('ok')")
        assert c.exec_driver_sql("SELECT new_function()").scalar() == 1
        assert c.exec_driver_sql("SELECT value FROM new_table").scalar() == "ok"
        assert c.exec_driver_sql(
            "SELECT NOT EXISTS (SELECT 1 FROM pg_proc p, "
                "LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a "
                "WHERE p.proname='new_function' "
            "AND a.grantee=0)"
        ).scalar()


@pytest.mark.parametrize(
    "attribute",
    [
        "SUPERUSER",
        "CREATEROLE",
        "CREATEDB",
        "REPLICATION",
        "BYPASSRLS",
    ],
)
def test_each_rejected_attribute(pg, attribute):
    from app.db.app_role_posture import inspect_posture, posture_ready

    engine, app, role = pg
    provision(engine, role)
    with engine.begin() as c:
        c.exec_driver_sql(f'ALTER ROLE "{role}" {attribute}')
    assert inspect_posture(app) == "app_role_demote_required"
    assert posture_ready(app) is True
    assert posture_ready(app, "enforce") is False


def test_vm_workflow_and_rollback_with_real_catalog(pg, tmp_path, monkeypatch):
    """Run the actual orchestration, redirecting its DB transport only to 56216."""
    engine, app, role = pg
    spec = importlib.util.spec_from_file_location("demote", ROOT / "ops/gcp/vm/app_role_demote.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    # The real VM policy requires UID 0; disposable test files belong to the runner.
    # Metadata acceptance/rejection is independently tested in the helper unit suite.
    monkeypatch.setattr(tool, "require_private_metadata", lambda info: None)

    def execute(sql):
        # Production pins mia_app; this fixture uses a unique cluster-global name.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            result = c.exec_driver_sql(
                sql.replace("mia_app", role), execution_options={"no_parameters": True}
            )
            if not result.returns_rows:
                return ""
            value = result.scalar()
            if isinstance(value, dict):
                return json.dumps(value)
            if isinstance(value, bool):
                return "t" if value else "f"
            return str(value)

    def authenticate(password):
        login = create_engine(app.url.set(password=password))
        try:
            with login.connect() as c:
                assert c.exec_driver_sql("SELECT current_user").scalar() == role
        finally:
            login.dispose()

    monkeypatch.setattr(tool, "authenticate", authenticate)
    runtime = tmp_path / "runtime.env"
    original = "MIA_DATABASE_URL=postgresql://mia:synthetic@127.0.0.1:5432/mia\n"
    runtime.write_text(original)
    assert tool.perform("check", tmp_path, execute)["role_exists"] is False
    tool.perform("stage", tmp_path, execute)
    assert runtime.read_text() == original
    with engine.connect() as c:
        before = c.execute(
            text("SELECT rolpassword FROM pg_authid WHERE rolname=:role"), {"role": role}
        ).scalar()
    tool.perform("stage", tmp_path, execute)
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT rolpassword FROM pg_authid WHERE rolname=:role"), {"role": role}
            ).scalar()
            == before
        )
    tool.perform("activate", tmp_path, execute)
    state = tool.perform("check", tmp_path, execute)
    assert all(
        state[k]
        for k in (
            "role_exists",
            "role_safe",
            "public_owned",
            "public_create",
            "not_database_owner",
            "no_membership",
            "active_is_app",
        )
    )
    assert state["ledger_access"] is False and state["database_create"] is False
    tool.perform("enforce", tmp_path, execute)
    assert tool.perform("check", tmp_path, execute)["enforce"] is True
    tool.perform("rollback", tmp_path, execute)
    assert original.strip() in runtime.read_text()
    assert tool.perform("check", tmp_path, execute)["active_is_app"] is False
    assert tool.perform("check", tmp_path, execute)["enforce"] is False
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT value FROM public.sample").scalar() == "preserved"
