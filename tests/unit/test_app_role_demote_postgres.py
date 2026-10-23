"""Destructive proof restricted to the dedicated disposable 56216 cluster."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from app.db.os_ledger_migrate import _validate_main_role_catalog
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "ops/gcp/vm/sql/app-role-demote.sql"
# The disposable server is a container: pg_dump/pg_restore run inside it when not on PATH.
CONTAINER = os.getenv("MIA_APP_ROLE_TEST_PG_CONTAINER", "assaf-os-v2-14a-pg")


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


@pytest.fixture
def restored(pg):
    """A FRESH database next to the source, as `mia restore` leaves it after CREATE DATABASE."""
    engine, app, role = pg
    name = "v214a_restore_" + uuid4().hex[:12]
    admin = create_engine(engine.url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    super_engine = create_engine(engine.url.set(database=name))
    app_engine = create_engine(app.url.set(database=name))
    try:
        yield name, super_engine, app_engine
    finally:
        app_engine.dispose()
        super_engine.dispose()
        with admin.connect() as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def provision(engine, role, *, reapply=False):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('mia.app_role', :role, true)"), {"role": role})
        if reapply:
            c.execute(text("SELECT set_config('mia.app_role_reapply', 'on', true)"))
        c.exec_driver_sql(SQL.read_text(), execution_options={"no_parameters": True})


def load_tool(monkeypatch):
    spec = importlib.util.spec_from_file_location("demote", ROOT / "ops/gcp/vm/app_role_demote.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    # The real VM policy requires UID 0; disposable test files belong to the runner.
    # Metadata acceptance/rejection is independently tested in the helper unit suite.
    monkeypatch.setattr(tool, "require_private_metadata", lambda info: None)
    return tool


def executor(engine, role):
    """The tool's DB transport, redirected to one database on the disposable 56216 server."""

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

    return execute


def pg_tool(name, *args, data=None):
    """Run pg_dump/pg_restore from PATH, else inside the disposable container (as the VM does)."""
    exe = shutil.which(name)
    if exe:
        command = [exe, "-h", "127.0.0.1", "-p", "56216", "-U", "postgres", *args]
    else:
        docker = shutil.which("docker")
        assert docker, f"{name} is not on PATH and docker is unavailable"
        command = [docker, "exec", "-i", CONTAINER, name, "-U", "postgres", *args]
    done = subprocess.run(command, input=data, capture_output=True, check=False, timeout=180)
    assert done.returncode == 0, f"{name} exit {done.returncode}: {done.stderr.decode()[:400]}"
    return done.stdout


def dump_and_restore(source, target):
    """`mia backup` then `mia restore`: custom-format dump, then the VM's exact restore flags."""
    dump = pg_tool("pg_dump", "-d", source, "--format=custom")
    assert dump.startswith(b"PGDMP")
    pg_tool(
        "pg_restore", "-d", target, "--no-owner", "--no-privileges", "--exit-on-error", data=dump
    )


def sqlstate(engine, statement):
    """SQLSTATE of a failing statement, or None when it succeeds (and is rolled back)."""
    try:
        with engine.connect() as c:
            c.exec_driver_sql(statement)
            c.rollback()
    except DBAPIError as failure:
        return failure.orig.sqlstate
    return None


def catalog_snapshot(engine, role):
    queries = [
        "SELECT oid,xmin::text,datacl FROM pg_database WHERE datname=current_database()",
        "SELECT oid,xmin::text,nspowner,nspacl FROM pg_namespace ORDER BY oid",
        "SELECT oid,xmin::text,defaclacl FROM pg_default_acl ORDER BY oid",
        "SELECT oid,xmin::text,relowner,relacl FROM pg_class ORDER BY oid",
        "SELECT oid,xmin::text,proowner,proacl FROM pg_proc WHERE oid>=16384 ORDER BY oid",
    ]
    with engine.connect() as c:
        rows = [c.exec_driver_sql(query).all() for query in queries]
        rows.append(
            c.execute(
                text("SELECT oid,xmin::text,rolpassword FROM pg_authid WHERE rolname=:role"),
                {"role": role},
            ).all()
        )
    return rows


def seed_restore_objects(engine):
    """Public tables, sequences and views on top of the fixture's sample/standalone/view."""
    with engine.begin() as c:
        c.exec_driver_sql(
            "CREATE TABLE public.child (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "sample_id int REFERENCES public.sample(id), note text)"
        )
        c.exec_driver_sql("INSERT INTO public.child(sample_id, note) VALUES (1, 'kept')")
        c.exec_driver_sql(
            "CREATE VIEW public.child_view AS SELECT c.note, s.value "
            "FROM public.child c JOIN public.sample s ON s.id = c.sample_id"
        )


def test_ownership_exact_ledger_guard_and_rerun(pg):
    engine, app, role = pg
    provision(engine, role)
    with app.connect() as c:
        # V2-14's real validator, not a copy: it raises on any unsafe attribute or ledger access.
        _validate_main_role_catalog(c, role)
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
    before = catalog_snapshot(engine, role)
    provision(engine, role)
    assert before == catalog_snapshot(engine, role)


def test_real_validator_passes_the_demoted_role_and_fails_a_superuser(pg):
    engine, app, role = pg
    provision(engine, role)
    with app.connect() as c:
        assert _validate_main_role_catalog(c, role) is None
    with engine.connect() as c:
        superuser = c.scalar(text("SELECT current_user"))
        assert c.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")) is True
        with pytest.raises(ValueError, match="^app_role_demote_required$"):
            _validate_main_role_catalog(c, superuser)


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
    # Not reapply: the first demote still refuses a role that already reaches the ledger.
    with pytest.raises(DBAPIError) as failure:
        provision(engine, role)
    assert failure.value.orig.sqlstate == "P0001"
    assert failure.value.orig.diag.message_primary == "app_role_ledger_access_unsafe"


def test_posture_uses_the_real_validator_end_to_end(pg):
    from app.db.app_role_posture import inspect_posture

    engine, app, role = pg
    provision(engine, role)
    assert inspect_posture(app) == "app_role_ok"
    with engine.begin() as c:
        c.exec_driver_sql(f'GRANT pg_monitor TO "{role}"')
    assert inspect_posture(app) == "app_role_membership_unsafe"
    with engine.begin() as c:
        c.exec_driver_sql(f'REVOKE pg_monitor FROM "{role}"')
        c.exec_driver_sql(f'ALTER DATABASE "{engine.url.database}" OWNER TO "{role}"')
    assert inspect_posture(app) == "app_role_database_owner_unsafe"


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
    tool = load_tool(monkeypatch)
    execute = executor(engine, role)

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


# --- reapply: the post-restore repair (V2-10's `mia restore` post-step calls it) -----------------


def test_restore_drill_reapply_restores_ownership_and_grants(pg, restored, tmp_path, monkeypatch):
    """The V2-14a half of V2-10's drill: dump, restore as the VM does, then reapply."""
    from app.db.app_role_posture import inspect_posture

    engine, app, role = pg
    name, restored_super, restored_app = restored
    tool = load_tool(monkeypatch)
    monkeypatch.setattr(tool, "authenticate", lambda password: None)
    seed_restore_objects(engine)
    runtime = tmp_path / "runtime.env"
    runtime.write_text("MIA_DATABASE_URL=postgresql://mia:synthetic@127.0.0.1:5432/mia\n")

    # Source: the real stage + activate, so the posture the restore must preserve is real.
    tool.perform("stage", tmp_path, executor(engine, role))
    tool.perform("activate", tmp_path, executor(engine, role))
    with app.connect() as c:
        _validate_main_role_catalog(c, role)
        source_rows = c.exec_driver_sql("SELECT count(*) FROM public.sample").scalar()

    dump_and_restore(engine.url.database, name)

    # The role is cluster-global, so it survived. Everything database-local did not.
    with restored_super.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname=:r"), {"r": role}) == 1
        assert c.scalar(text("SELECT count(*) FROM public.sample")) == source_rows
        assert not c.scalar(
            text(
                "SELECT bool_or(relowner=CAST(:r AS regrole)) FROM pg_class "
                "WHERE relnamespace='public'::regnamespace"
            ),
            {"r": role},
        )
        assert not c.scalar(text("SELECT has_schema_privilege(:r,'public','CREATE')"), {"r": role})
    # The posture check that matters before the repair: mia_app cannot read its own tables.
    for statement in (
        "SELECT * FROM public.sample",
        "SELECT * FROM public.child",
        "SELECT * FROM public.sample_view",
        "SELECT * FROM public.child_view",
        "INSERT INTO public.sample(value) VALUES ('denied')",
        "SELECT nextval('public.standalone')",
        "CREATE TABLE public.denied (id int)",
    ):
        assert sqlstate(restored_app, statement) == "42501", statement
    with restored_super.connect() as c:
        password_before = c.scalar(
            text("SELECT rolpassword FROM pg_authid WHERE rolname=:r"), {"r": role}
        )

    files = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = tool.perform("reapply", tmp_path, executor(restored_super, role))
    assert result == {"reapplied": True, "role_exists": True, "ledger_access": False}
    assert files == {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    # After the repair: the real validator passes and mia_app can read and write.
    with restored_app.connect() as c:
        assert _validate_main_role_catalog(c, role) is None
    assert inspect_posture(restored_app) == "app_role_ok"
    with restored_app.begin() as c:
        assert c.scalar(text("SELECT count(*) FROM public.sample")) == source_rows
        c.exec_driver_sql("INSERT INTO public.sample(value) VALUES ('after-restore')")
        c.exec_driver_sql("INSERT INTO public.child(sample_id, note) VALUES (1, 'after-restore')")
        assert c.scalar(text("SELECT nextval('public.standalone')")) == 1
        assert c.scalar(text("SELECT count(*) FROM public.sample_view")) == source_rows + 1
        assert c.scalar(text("SELECT count(*) FROM public.child_view")) == 2
        # The migration path: mia-migrate runs as mia_app and must ALTER and CREATE.
        c.exec_driver_sql("ALTER TABLE public.sample ADD COLUMN later integer")
        c.exec_driver_sql("CREATE TABLE public.future (id serial PRIMARY KEY)")
        c.exec_driver_sql("INSERT INTO public.future DEFAULT VALUES")
        c.exec_driver_sql(
            "CREATE FUNCTION public.after_restore() RETURNS int LANGUAGE sql AS 'SELECT 1'"
        )
        assert c.scalar(
            text(
                "SELECT NOT EXISTS (SELECT 1 FROM pg_proc p, "
                "LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a "
                "WHERE p.proname='after_restore' AND a.grantee=0)"
            )
        )
    with restored_app.connect() as c:
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
        assert not c.scalar(
            text("SELECT has_database_privilege(current_user,current_database(),'CREATE')")
        )
    with restored_super.connect() as c:
        # reapply never creates a role or rotates a password.
        assert password_before == c.scalar(
            text("SELECT rolpassword FROM pg_authid WHERE rolname=:r"), {"r": role}
        )

    # A second reapply is a no-op: no catalog row changes, and the result is the same.
    before = catalog_snapshot(restored_super, role)
    again = tool.perform("reapply", tmp_path, executor(restored_super, role))
    assert again == result
    assert before == catalog_snapshot(restored_super, role)
    assert files == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_reapply_after_restore_leaves_assaf_os_to_the_ledger_regrant(
    pg, restored, tmp_path, monkeypatch
):
    """A restore resurrects PUBLIC EXECUTE on ledger functions. reapply defers it to V2-15."""
    from app.db.app_role_posture import inspect_posture

    engine, app, role = pg
    name, restored_super, restored_app = restored
    tool = load_tool(monkeypatch)
    monkeypatch.setattr(tool, "authenticate", lambda password: None)
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE assaf_os.items (id int)")
        c.exec_driver_sql(
            "CREATE FUNCTION assaf_os.helper() RETURNS int LANGUAGE sql AS 'SELECT 1'"
        )
        c.exec_driver_sql("REVOKE ALL ON FUNCTION assaf_os.helper() FROM PUBLIC")
    (tmp_path / "runtime.env").write_text(
        "MIA_DATABASE_URL=postgresql://mia:synthetic@127.0.0.1:5432/mia\n"
    )
    tool.perform("stage", tmp_path, executor(engine, role))
    assert inspect_posture(app) == "app_role_ok"

    dump_and_restore(engine.url.database, name)

    ledger = (
        "SELECT 'n', oid::text, nspowner::text, nspacl::text FROM pg_namespace "
        "WHERE nspname='assaf_os' UNION ALL "
        "SELECT 'c', oid::text, relowner::text, relacl::text FROM pg_class "
        "WHERE relnamespace='assaf_os'::regnamespace UNION ALL "
        "SELECT 'p', oid::text, proowner::text, proacl::text FROM pg_proc "
        "WHERE pronamespace='assaf_os'::regnamespace ORDER BY 1, 2"
    )
    with restored_super.connect() as c:
        assert (
            c.scalar(
                text("SELECT has_function_privilege(:r,'assaf_os.helper()','EXECUTE')"), {"r": role}
            )
            is True
        )
        ledger_before = c.exec_driver_sql(ledger).all()
    with restored_app.connect() as c:
        with pytest.raises(ValueError, match="^app_role_ledger_access_unsafe$"):
            _validate_main_role_catalog(c, role)

    # The stage-time guard would refuse here; the post-restore repair must not abort the chain.
    result = tool.perform("reapply", tmp_path, executor(restored_super, role))
    assert result == {"reapplied": True, "role_exists": True, "ledger_access": True}
    with restored_super.connect() as c:
        assert c.exec_driver_sql(ledger).all() == ledger_before  # assaf_os is never touched
    assert inspect_posture(restored_app) == "app_role_ledger_access_unsafe"
    assert sqlstate(restored_app, "SELECT count(*) FROM public.sample") is None

    # Stand-in for V2-15's `provision_ledger --mode reapply`, which re-applies the grant map.
    with restored_super.begin() as c:
        c.exec_driver_sql("REVOKE ALL ON FUNCTION assaf_os.helper() FROM PUBLIC")
    with restored_app.connect() as c:
        assert _validate_main_role_catalog(c, role) is None
    assert inspect_posture(restored_app) == "app_role_ok"


def test_reapply_is_a_noop_when_the_demote_never_ran(pg, tmp_path, monkeypatch):
    engine, app, role = pg
    tool = load_tool(monkeypatch)
    before = catalog_snapshot(engine, role)
    assert tool.perform("reapply", tmp_path, executor(engine, role)) == {
        "reapplied": False,
        "role_exists": False,
    }
    with engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname=:r"), {"r": role}) == 0
    assert before == catalog_snapshot(engine, role)
    assert list(tmp_path.iterdir()) == []


def test_reapply_sql_never_creates_the_role(pg):
    engine, app, role = pg
    with pytest.raises(DBAPIError) as failure:
        provision(engine, role, reapply=True)
    assert failure.value.orig.diag.message_primary == "app_role_reapply_role_missing"
    with engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM pg_roles WHERE rolname=:r"), {"r": role}) == 0


@pytest.mark.parametrize(
    "statement,reason",
    [
        ('ALTER ROLE "{role}" SUPERUSER', "app_role_existing_unsafe"),
        ('ALTER ROLE "{role}" CREATEDB', "app_role_existing_unsafe"),
        ('ALTER ROLE "{role}" INHERIT', "app_role_existing_unsafe"),
        ('GRANT pg_monitor TO "{role}"', "app_role_membership_unsafe"),
        ('ALTER DATABASE "{database}" OWNER TO "{role}"', "app_role_database_owner_unsafe"),
    ],
)
def test_reapply_keeps_every_other_guard(pg, statement, reason):
    engine, app, role = pg
    provision(engine, role)
    with engine.begin() as c:
        c.exec_driver_sql(statement.format(role=role, database=engine.url.database))
    with pytest.raises(DBAPIError) as failure:
        provision(engine, role, reapply=True)
    assert failure.value.orig.diag.message_primary == reason


def test_reapply_stops_atomically_on_an_extension_owned_object(pg, tmp_path, monkeypatch):
    engine, app, role = pg
    tool = load_tool(monkeypatch)
    monkeypatch.setattr(tool, "authenticate", lambda password: None)
    (tmp_path / "runtime.env").write_text(
        "MIA_DATABASE_URL=postgresql://mia:synthetic@127.0.0.1:5432/mia\n"
    )
    tool.perform("stage", tmp_path, executor(engine, role))
    with engine.begin() as c:
        # What a restore leaves behind: objects owned by the restoring superuser, one of them
        # created by an extension. Reapply must stop for review and change nothing.
        c.exec_driver_sql("CREATE TABLE public.late (id int)")
        c.exec_driver_sql("CREATE EXTENSION citext WITH SCHEMA public")
    before = catalog_snapshot(engine, role)
    with pytest.raises(DBAPIError) as failure:
        tool.perform("reapply", tmp_path, executor(engine, role))
    assert failure.value.orig.diag.message_primary == "app_role_extension_object_requires_review"
    assert before == catalog_snapshot(engine, role)
    with engine.connect() as c:
        assert c.scalar(
            text(
                "SELECT relowner::regrole::text <> :r "
                "FROM pg_class WHERE oid='public.late'::regclass"
            ),
            {"r": role},
        )
