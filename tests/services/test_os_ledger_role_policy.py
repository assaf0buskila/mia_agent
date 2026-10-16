"""V2-15 real PostgreSQL ACL mutations; no production connection is permitted."""

import importlib.util
import os
from pathlib import Path

import pytest
from app.db.os_ledger_migrate import apply_ledger_migrations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]


def policy():
    from app.services.os_ledger import role_policy
    return role_policy


def provision():
    spec = importlib.util.spec_from_file_location(
        "provision_ledger", ROOT / "ops/assaf-os/provision_ledger.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def roles():
    url = os.getenv("MIA_OS_LEDGER_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}
    assert parsed.database == "mia_ledger_test" and parsed.username == "postgres"
    assert not parsed.query
    admin = create_engine(parsed)
    apply_ledger_migrations(admin)
    names = ("assaf_os_ledger", "assaf_os_retention", "v215_main", "v215_other")
    with admin.begin() as c:
        # Never adopt/drop a role from another lane.
        assert not c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=ANY(:names))"),
                            {"names": list(names)})
        for name in names[2:]:
            c.execute(text(f"CREATE ROLE {name} LOGIN NOINHERIT"))
    # Execute the real apply entrypoint on the disposable database, including
    # wrong-target refusal and post-DDL validation rollback. CI uses port 5432.
    p = provision()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(p, "_TARGETS", {"local-v2-15": (
            parsed.host, parsed.port, parsed.database, "postgres", "v215_main",
        )})
        patch.setenv("MIA_BUILD_SHA", "a" * 40)
        patch.setenv("MIA_DATABASE_URL", str(parsed.set(username="v215_main")))
        patch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", str(parsed.set(port=1)))
        args = ["--target", "local-v2-15", "--apply", "--approved-sha", "a" * 40]
        with pytest.raises(SystemExit) as denied:
            p.main(args)
        assert denied.value.code == 1
        patch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", url)
        validator = p.validate_ledger_catalog
        def refuse(*args, **kwargs):
            raise ValueError("ledger_role_denied")
        patch.setattr(p, "validate_ledger_catalog", refuse)
        with pytest.raises(SystemExit):
            p.main(args)
        with admin.connect() as c:
            assert c.scalar(text("SELECT to_regrole('assaf_os_ledger')")) is None
            assert c.scalar(text("SELECT to_regrole('assaf_os_retention')")) is None
        patch.setattr(p, "validate_ledger_catalog", validator)
        p.main(args)
    runtime = create_engine(parsed.set(username="assaf_os_ledger"))
    main = create_engine(parsed.set(username="v215_main"))
    yield admin, runtime, main
    runtime.dispose()
    main.dispose()
    with admin.begin() as c:
        for name in names:
            c.execute(text(f"DROP OWNED BY {name}"))
            c.execute(text(f"DROP ROLE {name}"))
    admin.dispose()


def validate(roles):
    _, runtime, main = roles
    with Session(runtime) as db:
        policy().validate_ledger_role(db, main, migration_role="postgres")


def test_exact_grants_pass(roles):
    validate(roles)


@pytest.mark.parametrize("mutation", [
    "GRANT DELETE ON assaf_os.os_leads TO assaf_os_ledger",
    "GRANT TRUNCATE ON assaf_os.os_events TO assaf_os_ledger",
    "GRANT UPDATE ON assaf_os.os_clients TO assaf_os_ledger",
    "GRANT REFERENCES ON assaf_os.os_clients TO assaf_os_ledger",
    "GRANT TRIGGER ON assaf_os.os_clients TO assaf_os_ledger",
    "GRANT UPDATE (record_id) ON assaf_os.os_clients TO assaf_os_ledger",
    "GRANT UPDATE (body) ON assaf_os.os_events TO assaf_os_ledger",
    "GRANT UPDATE (undo_state) ON assaf_os.os_receipts TO assaf_os_ledger",
    "GRANT SELECT (filename) ON assaf_os.schema_migrations TO assaf_os_ledger",
    "GRANT SELECT ON assaf_os.os_clients TO assaf_os_ledger WITH GRANT OPTION",
    "GRANT UPDATE (inputs) ON assaf_os.os_clients TO assaf_os_ledger WITH GRANT OPTION",
    "GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger WITH GRANT OPTION",
    "GRANT CREATE ON SCHEMA assaf_os TO assaf_os_ledger",
    "GRANT USAGE ON SCHEMA public TO assaf_os_ledger",
    "GRANT CREATE ON SCHEMA assaf_os TO v215_main",
    "GRANT CREATE ON DATABASE mia_ledger_test TO assaf_os_ledger",
    "GRANT TEMP ON DATABASE mia_ledger_test TO assaf_os_ledger",
    "GRANT TEMP ON DATABASE mia_ledger_test TO PUBLIC",
    "GRANT CONNECT ON DATABASE mia_ledger_test TO assaf_os_ledger WITH GRANT OPTION",
    "CREATE SCHEMA v215_schema; GRANT USAGE ON SCHEMA v215_schema TO PUBLIC",
    "GRANT SELECT ON assaf_os.os_events TO PUBLIC",
    "GRANT USAGE ON SCHEMA assaf_os TO PUBLIC",
    "GRANT EXECUTE ON FUNCTION assaf_os.expense_source_key_sha256(text) TO PUBLIC",
    "GRANT EXECUTE ON FUNCTION assaf_os.require_pending_assistant_proposal_insert() "
    "TO assaf_os_ledger",
    "CREATE SEQUENCE assaf_os.v215_seq; "
    "GRANT USAGE ON SEQUENCE assaf_os.v215_seq TO assaf_os_ledger",
    "CREATE SEQUENCE public.v215_seq; "
    "GRANT UPDATE ON SEQUENCE public.v215_seq TO assaf_os_ledger",
    "CREATE FUNCTION public.v215_func() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "GRANT EXECUTE ON FUNCTION assaf_os.expense_source_key_sha256(text) "
    "TO assaf_os_ledger WITH GRANT OPTION",
    "GRANT SELECT (body) ON assaf_os.os_events TO PUBLIC",
    "GRANT USAGE ON SCHEMA assaf_os TO assaf_os_retention",
    "GRANT assaf_os_ledger TO v215_main",
    "GRANT v215_other TO assaf_os_ledger",
    "GRANT assaf_os_ledger TO v215_other WITH ADMIN TRUE, SET FALSE, INHERIT FALSE",
    "GRANT v215_other TO assaf_os_owner",
    "GRANT assaf_os_owner TO v215_other",
    "GRANT assaf_os_owner TO postgres",
    "GRANT assaf_os_retention TO v215_other",
    "GRANT SELECT ON assaf_os.os_events TO assaf_os_retention",
    "ALTER ROLE assaf_os_retention LOGIN",
    "ALTER ROLE assaf_os_owner LOGIN",
    "ALTER ROLE assaf_os_owner INHERIT",
    "ALTER ROLE assaf_os_ledger INHERIT",
    "ALTER ROLE assaf_os_ledger SUPERUSER",
    "ALTER ROLE assaf_os_ledger CREATEROLE",
    "ALTER ROLE assaf_os_ledger CREATEDB",
    "ALTER ROLE assaf_os_ledger REPLICATION",
    "ALTER ROLE assaf_os_ledger BYPASSRLS",
    "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner GRANT EXECUTE ON FUNCTIONS TO PUBLIC",
    "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner IN SCHEMA assaf_os "
    "GRANT SELECT ON TABLES TO PUBLIC",
    "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner GRANT SELECT ON TABLES TO assaf_os_ledger",
    "ALTER DEFAULT PRIVILEGES FOR ROLE v215_other IN SCHEMA assaf_os "
    "GRANT SELECT ON TABLES TO v215_main",
    "ALTER DEFAULT PRIVILEGES FOR ROLE v215_other "
    "GRANT USAGE ON SEQUENCES TO assaf_os_ledger",
    "ALTER SCHEMA assaf_os OWNER TO v215_other",
    "ALTER TABLE assaf_os.os_clients OWNER TO assaf_os_ledger",
    "ALTER FUNCTION assaf_os.expense_source_key_sha256(text) SECURITY DEFINER",
    "GRANT USAGE ON SCHEMA assaf_os TO v215_main",
    "GRANT SELECT (body) ON assaf_os.os_events TO v215_main",
    "GRANT EXECUTE ON FUNCTION assaf_os.expense_source_key_sha256(text) TO v215_main",
    "ALTER ROLE v215_main SUPERUSER",
    "GRANT assaf_os_owner TO v215_main",
    "REVOKE INSERT ON assaf_os.os_events FROM assaf_os_ledger",
    "REVOKE USAGE ON SCHEMA assaf_os FROM assaf_os_ledger",
    "REVOKE SELECT ON assaf_os.os_authority FROM assaf_os_ledger",
    "REVOKE UPDATE (inputs) ON assaf_os.os_clients FROM assaf_os_ledger",
    "REVOKE EXECUTE ON FUNCTION assaf_os.expense_source_key_sha256(text) FROM assaf_os_ledger",
    "CREATE TABLE public.v215_extra (id integer); "
    "GRANT SELECT ON public.v215_extra TO assaf_os_ledger",
])
def test_extra_or_missing_privilege_fails(roles, mutation):
    admin, _, main = roles
    # Native catalog mutation and validation on the SAME uncommitted transaction.
    # SET LOCAL ROLE lets the validator see the transaction while retaining the real
    # session-user mismatch guard (tested separately below) through its catalog API.
    with admin.connect() as c, c.begin():
        c.exec_driver_sql(mutation)
        with pytest.raises(ValueError, match="^ledger_role_denied$"):
            policy().validate_ledger_catalog(c, main_role="v215_main", migration_role="postgres")
        c.rollback()
    validate(roles)


def test_owner_admin_only_exception(roles):
    with roles[0].connect() as c, c.begin():
        c.execute(text(
            "GRANT assaf_os_owner TO postgres WITH ADMIN TRUE, SET FALSE, INHERIT FALSE"
        ))
        policy().validate_ledger_catalog(c, main_role="v215_main", migration_role="postgres")
        c.rollback()


def test_main_dsn_is_mandatory_distinct_and_demoted(roles):
    for main in (roles[0], roles[1], create_engine("sqlite://")):
        with Session(roles[1]) as db, pytest.raises(ValueError, match="^ledger_role_denied$"):
            policy().validate_ledger_role(db, main, migration_role="postgres")


def test_impersonated_runtime_is_refused(roles):
    with Session(roles[0]) as db:
        db.execute(text("SET LOCAL ROLE assaf_os_ledger"))
        with pytest.raises(ValueError, match="^ledger_role_denied$"):
            policy().validate_ledger_role(db, roles[2], migration_role="postgres")


def test_other_database_and_impersonated_main_are_refused(roles):
    other_url = os.getenv("MIA_OWNER_OS_TEST_POSTGRES_URL")
    if not other_url:
        # The ledger suite may run alone with only its own DB configured. An
        # impersonated main on that database still exercises the identity guard.
        other_url = str(roles[0].url.set(database="postgres"))
    parsed = make_url(other_url)
    assert parsed.host in {"localhost", "127.0.0.1", "::1"} and not parsed.query
    assert parsed.database in {"owner_os_test", "postgres"}
    from sqlalchemy import event
    impersonated = create_engine(roles[0].url)
    @event.listens_for(impersonated, "connect")
    def set_role(conn, _):
        conn.execute("SET ROLE v215_main")
    other = create_engine(parsed)
    try:
        for engine in (other, impersonated):
            with Session(roles[1]) as db, pytest.raises(ValueError, match="^ledger_role_denied$"):
                policy().validate_ledger_role(db, engine, migration_role="postgres")
    finally:
        other.dispose()
        impersonated.dispose()


def test_refusal_is_sanitized_on_catalog_failure(roles):
    from sqlalchemy.exc import SQLAlchemyError
    with pytest.MonkeyPatch.context() as patch:
        def fail(*args, **kwargs):
            raise SQLAlchemyError("private provider payload")
        patch.setattr(roles[2], "connect", fail)
        with Session(roles[1]) as db, pytest.raises(ValueError, match="^ledger_role_denied$"):
            policy().validate_ledger_role(db, roles[2], migration_role="postgres")


def test_runtime_setting_and_migration_hook_agree(monkeypatch):
    from app.core.config import Settings
    from app.db.os_ledger_migrate import RUNTIME_DSN_ENV
    assert RUNTIME_DSN_ENV == "MIA_OWNER_OS_LEDGER_DATABASE_URL"
    monkeypatch.delenv(RUNTIME_DSN_ENV, raising=False)
    assert Settings(_env_file=None).owner_os_ledger_database_url == ""
    monkeypatch.setenv(RUNTIME_DSN_ENV, "postgresql://private-synthetic-value")
    settings = Settings(_env_file=None)
    assert settings.owner_os_ledger_database_url == "postgresql://private-synthetic-value"
    assert "private-synthetic-value" not in repr(settings)


def test_database_enforces_history_and_named_updates(roles):
    from sqlalchemy.exc import SQLAlchemyError
    with roles[1].connect() as c, c.begin():
        c.execute(text(
            "INSERT INTO assaf_os.os_workspaces(workspace_id,owner_id,epoch) "
            "VALUES ('v215','123',1)"
        ))
        c.execute(text("UPDATE assaf_os.os_workspaces SET version=1 WHERE workspace_id='v215'"))
        for sql in (
            "DELETE FROM assaf_os.os_workspaces", "TRUNCATE assaf_os.os_events",
            "UPDATE assaf_os.os_receipts SET undo_state='undone'",
            "UPDATE assaf_os.os_events SET body='{}'",
            "UPDATE assaf_os.os_workspaces SET owner_id='456'",
            "UPDATE assaf_os.os_authority SET state='fenced'",
            "SELECT * FROM assaf_os.schema_migrations",
        ):
            with pytest.raises(SQLAlchemyError), c.begin_nested():
                c.execute(text(sql))
        c.rollback()


def test_provision_dry_run_exact_and_no_connection(monkeypatch, capsys):
    p = provision()
    monkeypatch.setattr(p, "create_engine", lambda *a, **k: pytest.fail("dry run connected"))
    p.main(["--target", "local-v2-15"])
    output = capsys.readouterr().out
    assert output == "\n".join(p.provision_statements("mia_ledger_test")) + "\n"
    assert set(line for line in output.splitlines() if line.startswith("GRANT ")) == set(
        policy().ledger_grant_statements()
    )
    assert "DELETE" not in output and "TRUNCATE" not in output


@pytest.mark.parametrize("args", [[], ["--target", "unknown"],
    ["--target", "mia-vm", "--apply"],
    ["--target", "local-v2-15", "--apply", "--approved-sha", "bad"]])
def test_provision_refuses_unpinned_or_unapproved(args, monkeypatch):
    p = provision()
    monkeypatch.setattr(p, "create_engine", lambda *a, **k: pytest.fail("refusal connected"))
    with pytest.raises(SystemExit):
        p.main(args)
