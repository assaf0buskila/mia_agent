"""V2-15 real PostgreSQL ACL mutations; no production connection is permitted."""

import base64
import hashlib
import hmac
import importlib.util
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest
from app.db.os_ledger_migrate import TABLES, apply_ledger_migrations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "migrations_assaf_os" / "20261015_assaf_os_ledger.sql"
DENIED = "^ledger_role_denied$"
SHA = "a" * 40
POSTURE = "NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS"
ADMIN_ONLY = "WITH ADMIN TRUE, SET FALSE, INHERIT FALSE"
OWNED_TABLES = tuple(sorted(TABLES | {"schema_migrations"}))
OWNED_FUNCTIONS = (
    "expense_source_key_sha256(text)", "require_pending_assistant_proposal_insert()",
)
ROLES = ("assaf_os_ledger", "assaf_os_retention", "v215_main", "v215_other")

# V2_PG_CONTRACTS.md §18.2, spelled out here independently of role_policy.py.
COMMON = (
    "revision", "inputs", "business_date", "source", "updated_at", "deleted_at",
    "deleted_by_request_id",
)
RECORD_TYPED = {
    "os_leads": ("person_id",),
    "os_clients": (),
    "os_works": ("person_id", "agreed_amount_minor", "currency"),
    "os_payments": ("person_id", "work_id", "amount_minor", "currency", "original_payment_id"),
    "os_time": ("person_id", "work_id", "minutes"),
    "os_expenses": ("amount_minor", "currency", "source_type", "source_key", "source_digest"),
    "os_tasks": ("person_id", "work_id", "due_date"),
}
OTHER_UPDATES = {
    "os_workspaces": ("version", "summaries", "updated_at"),
    "os_proposals": ("status",),
    "os_outbox": (
        "status", "attempts", "next_attempt_at", "lease_owner", "lease_generation",
        "leased_at", "last_error_code", "updated_at",
    ),
    "os_mirror_state": (
        "status", "projected_version", "verified_version", "last_attempt_at",
        "last_verified_at", "last_error_code", "watermark", "scan_coverage", "updated_at",
    ),
    "os_summary_monthly": ("version", "summary", "calculated_at"),
    "os_summary_cycle": ("version", "starts_on", "ends_on", "summary", "calculated_at"),
    "os_import_batches": ("status", "quarantine", "completed_at"),
    "os_import_rows": ("record_id", "status", "canonical_record", "quarantine"),
}
NEVER_UPDATABLE = {"workspace_id", "record_id", "created_at"}


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


def sql_columns() -> dict[str, dict[str, bool]]:
    """Column name -> generated, per CREATE TABLE block of the frozen V2-14 SQL."""
    tables: dict[str, dict[str, bool]] = {}
    current = None
    for line in SQL.read_text(encoding="utf-8").splitlines():
        start = re.fullmatch(r"CREATE TABLE assaf_os\.(\w+) \(", line)
        if start:
            current = tables.setdefault(start.group(1), {})
        elif current is not None and line.startswith(");"):
            current = None
        elif current is not None and (column := re.match(r"    ([a-z_][a-z0-9_]*) [A-Z]", line)):
            current[column.group(1)] = "GENERATED ALWAYS" in line
    return tables


def scram_matches(verifier: str, password: str) -> bool:
    """RFC 5802 StoredKey check of a PostgreSQL SCRAM-SHA-256 verifier."""
    match = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)", verifier or "")
    if not match:
        return False
    iterations, salt, stored, _ = match.groups()
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(salt), int(iterations)
    )
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    return base64.b64encode(hashlib.sha256(client_key).digest()).decode() == stored


# --------------------------------------------------------------------------- map pins


def test_column_updates_is_exactly_the_frozen_map():
    expected = {name: COMMON + typed for name, typed in RECORD_TYPED.items()} | OTHER_UPDATES
    assert dict(policy().COLUMN_UPDATES) == expected
    assert policy().RECORD_UPDATE_COLUMNS == COMMON
    assert set(policy().TABLE_PRIVILEGES) == TABLES
    assert "os_authority" not in policy().COLUMN_UPDATES
    for immutable in ("os_receipts", "os_events", "schema_migrations"):
        assert immutable not in policy().COLUMN_UPDATES
    assert "epoch" not in policy().COLUMN_UPDATES["os_workspaces"]


def test_table_privileges_are_exactly_the_frozen_map():
    # §18.2: every table is SELECT + INSERT for the runtime role except os_authority
    # and os_workspaces, which are SELECT only. The workspace row is created once by the
    # privileged AuthorityAdmin (`mia os-authority init`, §5.4), never by the runtime.
    read_only = {"os_authority", "os_workspaces"}
    assert dict(policy().TABLE_PRIVILEGES) == {
        name: frozenset({"SELECT"} if name in read_only else {"SELECT", "INSERT"})
        for name in TABLES
    }
    statements = policy().ledger_grant_statements()
    assert "GRANT SELECT ON TABLE assaf_os.os_workspaces TO assaf_os_ledger;" in statements
    assert not [s for s in statements if "INSERT" in s and "os_workspaces" in s]


def test_column_updates_are_pinned_to_the_migration_sql():
    columns = sql_columns()
    assert set(columns) == TABLES
    for table, updates in policy().COLUMN_UPDATES.items():
        assert len(updates) == len(set(updates)), table
        assert set(updates) <= set(columns[table]), table
        assert not set(updates) & {"workspace_id", "created_at"}, table
        assert not any(columns[table][name] for name in updates), table
    for table in RECORD_TYPED:
        # Every record column is updatable except the identity, created_at and the
        # generated os_tasks.status, exactly as §18.2's "never updatable" list says.
        mutable = {name for name, generated in columns[table].items()
                   if not generated and name not in NEVER_UPDATABLE}
        assert set(policy().COLUMN_UPDATES[table]) == mutable, table
    assert columns["os_tasks"]["status"] is True


def test_map_covers_the_prototype_and_import_write_sets():
    from app.prototypes.assaf_os_ledger import MODELS

    for entity, model in MODELS.items():
        table = "os_" + entity
        # _apply_mutations writes every non-identity prototype column on each edit,
        # including source (rewritten from the proposal) and expenses.source_digest.
        written = {column.name for column in model.__table__.columns} - NEVER_UPDATABLE
        assert written <= set(policy().COLUMN_UPDATES[table]), table
        # FORK-DELETE tombstones.
        assert {"deleted_at", "deleted_by_request_id"} <= set(
            policy().COLUMN_UPDATES[table]
        ), table
        assert "source" in policy().COLUMN_UPDATES[table]
    assert "source_digest" in policy().COLUMN_UPDATES["os_expenses"]
    # §22 E1-rev: set_import_status writes status, completed_at and (for
    # quarantined) quarantine. Rows are insert-only through the typed API.
    assert {"status", "completed_at", "quarantine"} <= set(
        policy().COLUMN_UPDATES["os_import_batches"]
    )
    assert {"SELECT", "INSERT"} == policy().TABLE_PRIVILEGES["os_import_rows"]


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


# ------------------------------------------------------------------ provisioning (no DB)


def test_provision_targets_are_pinned():
    p = provision()
    assert p.MODES == ("provision", "reapply")
    assert p._TARGETS == {
        "local-v2-15": p.Target("127.0.0.1", 56215, "mia_ledger_test", "postgres", "v215_main"),
        # FORK-LEDGER-DSN: the VM's loopback PostgreSQL, the superuser migration
        # identity, V2-14a's demoted main role, and the VM-owned runtime env file.
        "mia-vm": p.Target(
            "127.0.0.1", 5432, "mia", "mia", "mia_app", "/etc/mia/runtime.env",
        ),
    }
    assert p._TARGETS["local-v2-15"].runtime_env is None


@pytest.mark.parametrize("name", ["local-v2-15", "mia-vm"])
@pytest.mark.parametrize("mode", ["provision", "reapply"])
def test_provision_dry_run_exact_and_no_connection(name, mode, monkeypatch, capsys):
    p = provision()
    monkeypatch.setattr(p, "create_engine", lambda *a, **k: pytest.fail("dry run connected"))
    monkeypatch.setattr(p, "runtime_credential", lambda *a, **k: pytest.fail("dry run read env"))
    p.main(["--target", name, "--mode", mode])
    output = capsys.readouterr().out
    statements = p.provision_statements(name, mode)
    assert output == "\n".join(statements) + "\n"
    target = p._TARGETS[name]
    grants = {line for line in statements if line.startswith("GRANT ")}
    assert grants == set(policy().ledger_grant_statements()) | {
        f"GRANT CONNECT ON DATABASE {target.database} TO assaf_os_ledger;",
        f"GRANT assaf_os_owner TO {target.migration_role} {ADMIN_ONLY};",
    }
    assert statements[-len(policy().ledger_grant_statements()):] == (
        policy().ledger_grant_statements()
    )
    creates = [line for line in statements if line.startswith("CREATE ROLE ")]
    assert creates == ([f"CREATE ROLE assaf_os_owner NOLOGIN {POSTURE};"] if mode == "reapply"
                       else []) + [
        f"CREATE ROLE assaf_os_ledger LOGIN {POSTURE};",
        f"CREATE ROLE assaf_os_retention NOLOGIN {POSTURE};",
    ]
    assert f"REVOKE TEMPORARY ON DATABASE {target.database} FROM PUBLIC;" in statements
    assert "ALTER SCHEMA assaf_os OWNER TO assaf_os_owner;" in statements
    for table in OWNED_TABLES:
        assert f"ALTER TABLE assaf_os.{table} OWNER TO assaf_os_owner;" in statements
    for function in OWNED_FUNCTIONS:
        assert f"ALTER FUNCTION assaf_os.{function} OWNER TO assaf_os_owner;" in statements
    for kind in ("SCHEMA assaf_os", "ALL TABLES IN SCHEMA assaf_os",
                 "ALL SEQUENCES IN SCHEMA assaf_os", "ALL ROUTINES IN SCHEMA assaf_os"):
        assert (f"REVOKE ALL ON {kind} FROM PUBLIC, assaf_os_ledger, assaf_os_retention;"
                in statements)
    assert ("ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner "
            "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;") in statements
    for forbidden in ("DELETE", "TRUNCATE", "PASSWORD", "postgresql", "assaf_os_retention TO"):
        assert forbidden not in output


@pytest.mark.parametrize("args", [
    [], ["--target", "unknown"],
    ["--target", "mia-vm", "--apply"],
    ["--target", "mia-vm", "--mode", "reapply", "--apply"],
    ["--target", "local-v2-15", "--mode", "repair"],
    ["--target", "local-v2-15", "--apply", "--approved-sha", "bad"],
    ["--target", "local-v2-15", "--mode", "reapply", "--apply", "--approved-sha", "b" * 40],
])
def test_provision_refuses_unpinned_or_unapproved(args, monkeypatch, capsys):
    p = provision()
    monkeypatch.setenv("MIA_BUILD_SHA", SHA)
    monkeypatch.setattr(p, "create_engine", lambda *a, **k: pytest.fail("refusal connected"))
    monkeypatch.setattr(p, "runtime_credential", lambda *a, **k: pytest.fail("refusal read env"))
    with pytest.raises(SystemExit) as refused:
        p.main(args)
    assert refused.value.code in {1, 2}
    if "--apply" in args and "--approved-sha" in args or args[-1:] == ["--apply"]:
        assert "ledger_owner_go_sha_required" in capsys.readouterr().err


def test_runtime_credential_is_staged_once_and_keeps_the_env_file(tmp_path):
    p = provision()
    env = tmp_path / "runtime.env"
    original = "MIA_DATABASE_URL=postgresql://keep@127.0.0.1:5432/mia\nMIA_APP_ROLE_POSTURE=enforce\n"
    env.write_text(original, encoding="utf-8", newline="\n")
    os.chmod(env, 0o600)
    target = p.Target("127.0.0.1", 5432, "mia", "mia", "mia_app", str(env))
    assert p.runtime_credential(target, create=False) is None
    assert env.read_text(encoding="utf-8") == original
    password = p.runtime_credential(target)
    assert re.fullmatch(r"[0-9a-f]{64}", password)
    content = env.read_bytes().decode("utf-8")
    assert content == original + (
        f"MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://assaf_os_ledger:{password}"
        "@127.0.0.1:5432/mia\n"
    )
    if os.name == "posix":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    # A retry reuses the staged credential and never rotates or rewrites it.
    assert p.runtime_credential(target) == password
    assert p.runtime_credential(target, create=False) == password
    assert env.read_bytes().decode("utf-8") == content
    for bad in (
        content + f"MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://assaf_os_ledger:{password}"
                  "@127.0.0.1:5432/mia\n",
        original + "MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://assaf_os_ledger:short"
                   "@127.0.0.1:5432/mia\n",
        original + f"MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://mia:{password}"
                   "@127.0.0.1:5432/mia\n",
        original + f"MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://assaf_os_ledger:{password}"
                   "@10.0.0.1:5432/mia\n",
        original + f"MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://assaf_os_ledger:{password}"
                   "@127.0.0.1:5432/other?sslmode=disable\n",
    ):
        env.write_text(bad, encoding="utf-8", newline="\n")
        os.chmod(env, 0o600)
        with pytest.raises(ValueError, match="^ledger_runtime_env_invalid$"):
            p.runtime_credential(target)
        assert env.read_text(encoding="utf-8") == bad
    missing = p.Target("127.0.0.1", 5432, "mia", "mia", "mia_app", str(tmp_path / "absent.env"))
    with pytest.raises(ValueError):
        p.runtime_credential(missing)
    assert not (tmp_path / "absent.env").exists()
    if os.name == "posix":
        env.write_text(original, encoding="utf-8")
        os.chmod(env, 0o644)
        with pytest.raises(ValueError, match="^ledger_runtime_env_invalid$"):
            p.runtime_credential(target)
        link = tmp_path / "link.env"
        link.symlink_to(env)
        os.chmod(env, 0o600)
        linked = p.Target("127.0.0.1", 5432, "mia", "mia", "mia_app", str(link))
        with pytest.raises(ValueError, match="^ledger_runtime_env_invalid$"):
            p.runtime_credential(linked)


# ------------------------------------------------------------------ PostgreSQL posture


def settle(admin, database):
    """Independent oracle for the provisioned posture (deliberately not provision_ledger).

    Shape of the §3.4 conformance fixture: the three roles, the ADMIN-only migration
    edge, PUBLIC without TEMP and role_policy.ledger_grant_statements() applied directly.
    """
    with admin.begin() as c:
        for name, login in (("assaf_os_ledger", "LOGIN"), ("assaf_os_retention", "NOLOGIN")):
            if c.scalar(text("SELECT to_regrole(:name) IS NULL"), {"name": name}):
                c.execute(text(f"CREATE ROLE {name} {login} {POSTURE}"))
        c.execute(text(f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC"))
        c.execute(text(f"GRANT assaf_os_owner TO postgres {ADMIN_ONLY}"))
        c.execute(text("ALTER SCHEMA assaf_os OWNER TO assaf_os_owner"))
        for table in OWNED_TABLES:
            c.execute(text(f"ALTER TABLE assaf_os.{table} OWNER TO assaf_os_owner"))
        for function in OWNED_FUNCTIONS:
            c.execute(text(f"ALTER FUNCTION assaf_os.{function} OWNER TO assaf_os_owner"))
        for kind in ("SCHEMA assaf_os", "ALL TABLES IN SCHEMA assaf_os",
                     "ALL SEQUENCES IN SCHEMA assaf_os", "ALL ROUTINES IN SCHEMA assaf_os"):
            c.execute(text(
                f"REVOKE ALL ON {kind} FROM PUBLIC, assaf_os_ledger, assaf_os_retention"
            ))
        c.execute(text(
            "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner "
            "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        ))
        c.execute(text(f"REVOKE ALL ON DATABASE {database} FROM assaf_os_ledger"))
        for statement in policy().ledger_grant_statements():
            c.execute(text(statement))


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
    with admin.begin() as c:
        # Never adopt/drop a role or membership from another lane.
        assert not c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=ANY(:names))"),
                            {"names": list(ROLES)})
        assert not c.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE roleid='assaf_os_owner'::regrole)"
        ))
        public_temp = c.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM pg_database d, LATERAL aclexplode("
            "COALESCE(d.datacl, acldefault('d', d.datdba))) a WHERE d.datname=current_database() "
            "AND a.grantee=0 AND a.privilege_type='TEMPORARY')"
        ))
        for name in ROLES[2:]:
            c.execute(text(f"CREATE ROLE {name} LOGIN NOINHERIT"))
    settle(admin, parsed.database)
    runtime = create_engine(parsed.set(username="assaf_os_ledger"))
    main = create_engine(parsed.set(username="v215_main"))
    yield admin, runtime, main
    runtime.dispose()
    main.dispose()
    with admin.begin() as c:
        for name in ROLES:
            if c.scalar(text("SELECT to_regrole(:name) IS NOT NULL"), {"name": name}):
                c.execute(text(f"DROP OWNED BY {name}"))
                c.execute(text(f"DROP ROLE {name}"))
        c.execute(text("REVOKE assaf_os_owner FROM postgres"))
        if public_temp:
            c.execute(text(f"GRANT TEMPORARY ON DATABASE {parsed.database} TO PUBLIC"))
    admin.dispose()


def validate(roles, migration_role="postgres"):
    _, runtime, main = roles
    with Session(runtime) as db:
        policy().validate_ledger_role(db, main, migration_role=migration_role)


def catalog(c, migration_role="postgres"):
    policy().validate_ledger_catalog(c, main_role="v215_main", migration_role=migration_role)


def test_exact_grants_pass(roles):
    validate(roles)


def test_migration_role_is_derived_from_the_single_admin_only_member(roles):
    admin, runtime, main = roles
    # §5.5: the runtime guard passes no migration identity at all.
    with Session(runtime) as db:
        policy().validate_ledger_role(db, main)
    with admin.connect() as c, c.begin():
        catalog(c, None)
        with pytest.raises(ValueError, match=DENIED):
            catalog(c, "v215_other")  # an explicit pin must equal the derived member
        c.rollback()
    with admin.connect() as c, c.begin():
        c.execute(text(f"GRANT assaf_os_owner TO v215_other {ADMIN_ONLY}"))
        for explicit in (None, "postgres", "v215_other"):
            with pytest.raises(ValueError, match=DENIED):
                catalog(c, explicit)
        c.rollback()
    with admin.connect() as c, c.begin():
        c.execute(text("REVOKE assaf_os_owner FROM postgres"))
        for explicit in (None, "postgres"):
            with pytest.raises(ValueError, match=DENIED):
                catalog(c, explicit)
        c.rollback()
    validate(roles, None)


# r1 review must-fix repros (rules, triggers, out-of-schema dependents, owner objects,
# parameter ACLs, role settings) and minors (temp shadowing, main TEMP). Each must
# make the validator refuse.
REVIEW_R1_MUTATIONS = [
    # 1. rules and triggers on assaf_os relations
    "CREATE RULE v215_rule AS ON INSERT TO assaf_os.os_summary_monthly "
    "DO ALSO DELETE FROM assaf_os.os_events WHERE workspace_id = NEW.workspace_id",
    "CREATE FUNCTION public.v215_trg() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER "
    "SET search_path = pg_catalog AS $$ BEGIN DELETE FROM assaf_os.os_events "
    "WHERE workspace_id = NEW.workspace_id; RETURN NEW; END $$; "
    "REVOKE ALL ON FUNCTION public.v215_trg() FROM PUBLIC; "
    "CREATE TRIGGER v215_t AFTER INSERT ON assaf_os.os_summary_cycle "
    "FOR EACH ROW EXECUTE FUNCTION public.v215_trg()",
    "CREATE TRIGGER v215_t2 BEFORE INSERT ON assaf_os.os_events "
    "FOR EACH ROW EXECUTE FUNCTION assaf_os.require_pending_assistant_proposal_insert()",
    "CREATE CONSTRAINT TRIGGER v215_t3 AFTER INSERT ON assaf_os.os_proposals "
    "FOR EACH ROW EXECUTE FUNCTION assaf_os.require_pending_assistant_proposal_insert()",
    "ALTER TABLE assaf_os.os_proposals DISABLE TRIGGER os_proposals_assistant_insert_pending",
    "ALTER TABLE assaf_os.os_proposals "
    "ENABLE REPLICA TRIGGER os_proposals_assistant_insert_pending",
    "DROP TRIGGER os_proposals_assistant_insert_pending ON assaf_os.os_proposals",
    "ALTER TABLE assaf_os.os_events ENABLE ROW LEVEL SECURITY",
    "CREATE POLICY v215_policy ON assaf_os.os_events USING (true)",
    # 2. objects outside assaf_os that depend on it, and owner objects outside it
    "CREATE VIEW public.v215_v AS SELECT * FROM assaf_os.os_events; "
    "GRANT SELECT, DELETE ON public.v215_v TO v215_main",
    "CREATE MATERIALIZED VIEW public.v215_mv AS SELECT workspace_id FROM assaf_os.os_events",
    "CREATE FUNCTION public.v215_body() RETURNS bigint LANGUAGE sql "
    "BEGIN ATOMIC SELECT count(*) FROM assaf_os.os_events; END; "
    "REVOKE ALL ON FUNCTION public.v215_body() FROM PUBLIC",
    "CREATE TABLE public.v215_fk (w varchar(128) "
    "REFERENCES assaf_os.os_workspaces(workspace_id))",
    "CREATE TABLE public.v215_rowtype (e assaf_os.os_events)",
    "CREATE STATISTICS public.v215_stats ON workspace_id, commit_version "
    "FROM assaf_os.os_events",
    "CREATE PUBLICATION v215_pub FOR TABLE assaf_os.os_events",
    "CREATE FUNCTION public.v215_default() RETURNS jsonb LANGUAGE sql "
    "AS $$SELECT '{}'::jsonb$$; "
    "REVOKE ALL ON FUNCTION public.v215_default() FROM PUBLIC; "
    "ALTER TABLE assaf_os.os_events ALTER COLUMN body SET DEFAULT public.v215_default()",
    "CREATE FUNCTION public.v215_f() RETURNS void LANGUAGE sql SECURITY DEFINER "
    "AS 'DELETE FROM assaf_os.os_events'; "
    "REVOKE ALL ON FUNCTION public.v215_f() FROM PUBLIC; "
    "ALTER FUNCTION public.v215_f() OWNER TO assaf_os_owner; "
    "GRANT EXECUTE ON FUNCTION public.v215_f() TO v215_main",
    "CREATE FUNCTION public.v215_sd() RETURNS void LANGUAGE plpgsql SECURITY DEFINER "
    "AS $$BEGIN DELETE FROM assaf_os.os_events; END$$; "
    "REVOKE ALL ON FUNCTION public.v215_sd() FROM PUBLIC; "
    "GRANT EXECUTE ON FUNCTION public.v215_sd() TO v215_main",
    # Added after the red run (self-review): EXECUTE is not the boundary, because a
    # trigger or event trigger invokes a SECURITY DEFINER function without that check.
    "CREATE FUNCTION public.v215_sdt() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER "
    "AS $$BEGIN DELETE FROM assaf_os.os_events; RETURN NEW; END$$; "
    "REVOKE ALL ON FUNCTION public.v215_sdt() FROM PUBLIC; "
    "CREATE TABLE public.v215_tt (x int); ALTER TABLE public.v215_tt OWNER TO v215_main; "
    "CREATE TRIGGER v215_tt AFTER INSERT ON public.v215_tt "
    "FOR EACH ROW EXECUTE FUNCTION public.v215_sdt()",
    "CREATE FUNCTION public.v215_evt() RETURNS event_trigger LANGUAGE plpgsql "
    "SECURITY DEFINER AS $$BEGIN DELETE FROM assaf_os.os_events; END$$; "
    "REVOKE ALL ON FUNCTION public.v215_evt() FROM PUBLIC; "
    "CREATE EVENT TRIGGER v215_evt ON ddl_command_end EXECUTE FUNCTION public.v215_evt()",
    "CREATE TABLE public.v215_owned (x int); "
    "ALTER TABLE public.v215_owned OWNER TO assaf_os_owner",
    "CREATE SCHEMA v215_owner_schema AUTHORIZATION assaf_os_owner",
    # 3. parameter ACLs and role settings
    "GRANT SET ON PARAMETER session_replication_role TO assaf_os_ledger",
    "GRANT SET ON PARAMETER session_replication_role TO PUBLIC",
    "GRANT SET ON PARAMETER session_replication_role TO v215_main",
    "GRANT SET ON PARAMETER session_replication_role TO assaf_os_retention",
    "GRANT ALTER SYSTEM ON PARAMETER work_mem TO assaf_os_ledger",
    "ALTER ROLE assaf_os_ledger SET session_replication_role = 'replica'",
    "ALTER ROLE assaf_os_ledger IN DATABASE mia_ledger_test SET work_mem = '1MB'",
    "ALTER ROLE assaf_os_retention SET work_mem = '1MB'",
    "ALTER ROLE ALL SET session_replication_role = 'replica'",
    "ALTER DATABASE mia_ledger_test SET session_replication_role = 'replica'",
    # Shared-object footprint of the runtime role beyond CONNECT on this database.
    "GRANT CREATE ON TABLESPACE pg_default TO assaf_os_ledger",
    "GRANT CONNECT ON DATABASE postgres TO assaf_os_ledger",
    "SELECT lo_create(215215); ALTER LARGE OBJECT 215215 OWNER TO assaf_os_ledger",
    "SELECT lo_create(215216); GRANT SELECT ON LARGE OBJECT 215216 TO assaf_os_ledger",
    # 4. a temporary relation cannot shadow a catalog; the main role has no TEMP
    "GRANT v215_other TO v215_main; "
    "CREATE TEMP TABLE pg_auth_members AS SELECT * FROM pg_catalog.pg_auth_members LIMIT 0",
    "GRANT TEMP ON DATABASE mia_ledger_test TO v215_main",
]

# Review of #170 r2 (round 1), major: privileges that reach assaf_os through the predefined
# roles rather than through an ACL, ownership or pg_depend. The first entry is the reviewer's
# reproduction verbatim: a SECURITY DEFINER function owned by a pg_write_all_data member
# deletes ledger rows for the main role, while a direct DELETE by the main role fails.
REVIEW_R2_MUTATIONS = [
    "CREATE ROLE v215_px NOLOGIN NOINHERIT; "
    "GRANT pg_write_all_data, pg_read_all_data TO v215_px WITH INHERIT TRUE; "
    "CREATE FUNCTION public.v215_pxf() RETURNS void LANGUAGE sql SECURITY DEFINER "
    "SET search_path = pg_catalog "
    "AS 'DELETE FROM assaf_os.os_workspaces WHERE workspace_id=''v215probe'''; "
    "REVOKE ALL ON FUNCTION public.v215_pxf() FROM PUBLIC; "
    "ALTER FUNCTION public.v215_pxf() OWNER TO v215_px; "
    "GRANT EXECUTE ON FUNCTION public.v215_pxf() TO v215_main; "
    "GRANT USAGE ON SCHEMA public TO v215_main",
    # pg_write_all_data alone: an unqualified DELETE works the same way.
    "CREATE ROLE v215_pw NOLOGIN NOINHERIT; GRANT pg_write_all_data TO v215_pw WITH INHERIT TRUE; "
    "CREATE FUNCTION public.v215_pwf() RETURNS void LANGUAGE sql SECURITY DEFINER "
    "SET search_path = pg_catalog AS 'DELETE FROM assaf_os.os_events'; "
    "REVOKE ALL ON FUNCTION public.v215_pwf() FROM PUBLIC; "
    "ALTER FUNCTION public.v215_pwf() OWNER TO v215_pw; "
    "GRANT EXECUTE ON FUNCTION public.v215_pwf() TO v215_main",
    # A second LOGIN identity with write access, set-only (no INHERIT) or inherited.
    "CREATE ROLE v215_ps LOGIN NOINHERIT; "
    "GRANT pg_write_all_data TO v215_ps WITH INHERIT FALSE, SET TRUE",
    "CREATE ROLE v215_pi LOGIN; GRANT pg_write_all_data TO v215_pi WITH INHERIT TRUE",
    # A second LOGIN identity with read access to every ledger row.
    "CREATE ROLE v215_pr LOGIN; GRANT pg_read_all_data TO v215_pr",
    "GRANT pg_write_all_data TO v215_other",
    "GRANT pg_read_all_data TO v215_other",
    # Transitive membership, with no INHERIT anywhere on the chain.
    "CREATE ROLE v215_pg NOLOGIN NOINHERIT; GRANT pg_write_all_data TO v215_pg; "
    "GRANT v215_pg TO v215_other WITH INHERIT FALSE, SET TRUE",
    # The same class one step further: file and program access reach the data files
    # (or run as the server's OS user) without any ACL on assaf_os.
    "GRANT pg_read_server_files TO v215_other",
    "GRANT pg_write_server_files TO v215_other",
    "GRANT pg_execute_server_program TO v215_other",
    # Minor: the one allowed trigger and both owned functions are pinned, not just named.
    "DROP TRIGGER os_proposals_assistant_insert_pending ON assaf_os.os_proposals; "
    "CREATE TRIGGER os_proposals_assistant_insert_pending BEFORE INSERT ON assaf_os.os_proposals "
    "FOR EACH ROW WHEN (false) "
    "EXECUTE FUNCTION assaf_os.require_pending_assistant_proposal_insert()",
    "DROP TRIGGER os_proposals_assistant_insert_pending ON assaf_os.os_proposals; "
    "CREATE TRIGGER os_proposals_assistant_insert_pending BEFORE INSERT ON assaf_os.os_proposals "
    "FOR EACH ROW EXECUTE FUNCTION assaf_os.require_pending_assistant_proposal_insert('x')",
    "DROP TRIGGER os_proposals_assistant_insert_pending ON assaf_os.os_proposals; "
    "CREATE TRIGGER os_proposals_assistant_insert_pending "
    "BEFORE INSERT OR UPDATE OF status ON assaf_os.os_proposals "
    "FOR EACH ROW EXECUTE FUNCTION assaf_os.require_pending_assistant_proposal_insert()",
    "ALTER TABLE assaf_os.os_proposals ENABLE ALWAYS TRIGGER os_proposals_assistant_insert_pending",
    "CREATE OR REPLACE FUNCTION assaf_os.require_pending_assistant_proposal_insert() "
    "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    "CREATE OR REPLACE FUNCTION assaf_os.require_pending_assistant_proposal_insert() "
    "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN DELETE FROM assaf_os.os_events; "
    "RETURN NEW; END $$",
    "ALTER FUNCTION assaf_os.require_pending_assistant_proposal_insert() SET search_path = public",
    "CREATE OR REPLACE FUNCTION assaf_os.expense_source_key_sha256(value text) RETURNS bytea "
    "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path = pg_catalog "
    "AS $$ SELECT sha256(convert_to(value || 'x', 'UTF8')) $$",
    "ALTER FUNCTION assaf_os.expense_source_key_sha256(text) SET search_path = public",
    "ALTER FUNCTION assaf_os.expense_source_key_sha256(text) RESET search_path",
    "ALTER FUNCTION assaf_os.expense_source_key_sha256(text) VOLATILE",
    # §18.2 (found while re-verifying the grant map): os_workspaces is SELECT only.
    "GRANT INSERT ON assaf_os.os_workspaces TO assaf_os_ledger",
]


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
    # r2: the baseline now holds the ADMIN-only migration edge, so r1's bare
    # "GRANT assaf_os_owner TO postgres" is a no-op; widening that edge replaces it.
    "GRANT assaf_os_owner TO postgres WITH SET TRUE",
    "GRANT assaf_os_owner TO postgres WITH INHERIT TRUE",
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
    "REVOKE UPDATE (source) ON assaf_os.os_clients FROM assaf_os_ledger",
    "REVOKE UPDATE (source_digest) ON assaf_os.os_expenses FROM assaf_os_ledger",
    "REVOKE EXECUTE ON FUNCTION assaf_os.expense_source_key_sha256(text) FROM assaf_os_ledger",
    "CREATE TABLE public.v215_extra (id integer); "
    "GRANT SELECT ON public.v215_extra TO assaf_os_ledger",
    *REVIEW_R1_MUTATIONS,
    *REVIEW_R2_MUTATIONS,
])
def test_extra_or_missing_privilege_fails(roles, mutation):
    admin, _, _ = roles
    # Native catalog mutation and validation on the SAME uncommitted transaction.
    with admin.connect() as c, c.begin():
        c.exec_driver_sql(mutation)
        with pytest.raises(ValueError, match=DENIED):
            catalog(c)
        c.rollback()
    validate(roles)


def sql_function_bodies() -> dict[str, str]:
    """Function name -> dollar-quoted body, per CREATE FUNCTION of the frozen V2-14 SQL."""
    return {
        match.group(1): match.group(2)
        for match in re.finditer(
            r"CREATE FUNCTION assaf_os\.(\w+)\([^)]*\)\s.*?\bAS \$\$(.*?)\$\$;",
            SQL.read_text(encoding="utf-8"), re.S,
        )
    }


def test_function_pins_are_the_frozen_migration_sql():
    """The validator pins each owned function's body and settings; they equal the SQL."""
    pins = policy().FUNCTION_PINS
    assert tuple(pins) == policy().OWNED_FUNCTIONS == OWNED_FUNCTIONS
    bodies = sql_function_bodies()
    assert set(bodies) == {identity.split("(")[0] for identity in pins}
    for identity, pin in pins.items():
        assert pin.source == bodies[identity.split("(")[0]], identity
    sha = pins["expense_source_key_sha256(text)"]
    assert (sha.language, sha.volatility, sha.strict, sha.returns, sha.config) == (
        "sql", "i", True, "bytea", ("search_path=pg_catalog",)
    )
    trigger = pins["require_pending_assistant_proposal_insert()"]
    assert (trigger.language, trigger.volatility, trigger.strict, trigger.returns,
            trigger.config) == ("plpgsql", "v", False, "trigger", None)
    # What the SQL declares for each function header, spelled here independently.
    sql = SQL.read_text(encoding="utf-8")
    assert "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE\nSET search_path = pg_catalog" in sql
    assert "RETURNS trigger\nLANGUAGE plpgsql\nAS $$" in sql


def test_privileged_predefined_roles_are_the_reviewed_set():
    """Data and OS reach by membership; absent roles (pg_maintain before PG17) are no-ops."""
    assert set(policy()._PRIVILEGED_PREDEFINED) == {
        "pg_read_all_data", "pg_write_all_data", "pg_maintain",
        "pg_read_server_files", "pg_write_server_files", "pg_execute_server_program",
    }


def test_secdef_owner_privilege_check_stands_without_the_membership_check(roles, monkeypatch):
    """Defence in depth: with the membership rule off, the reviewer's probe is still refused,
    because the owner of a SECURITY DEFINER routine holds privileges on assaf_os."""
    monkeypatch.setattr(policy(), "_PRIVILEGED_PREDEFINED", ())
    with roles[0].connect() as c, c.begin():
        c.exec_driver_sql(REVIEW_R2_MUTATIONS[0])
        with pytest.raises(ValueError, match=DENIED):
            catalog(c)
        c.rollback()


def test_unprivileged_secdef_owner_and_monitoring_roles_are_accepted(roles):
    """No false positives on what production may hold: the main role owns public objects
    (and may own SECURITY DEFINER routines, with nothing on assaf_os), and monitoring roles
    read statistics only."""
    with roles[0].connect() as c, c.begin():
        c.exec_driver_sql(
            "CREATE FUNCTION public.v215_okf() RETURNS int LANGUAGE sql SECURITY DEFINER "
            "SET search_path = pg_catalog AS 'SELECT 1'; "
            "REVOKE ALL ON FUNCTION public.v215_okf() FROM PUBLIC; "
            "ALTER FUNCTION public.v215_okf() OWNER TO v215_main; "
            "GRANT pg_monitor, pg_read_all_settings, pg_read_all_stats, pg_stat_scan_tables "
            "TO v215_other"
        )
        catalog(c)
        c.rollback()


def test_ledger_session_must_run_in_origin_replication_role(roles):
    """ALTER SYSTEM or postgresql.conf can set session_replication_role = replica, which
    switches off foreign keys and the assistant trigger and which neither the role-setting
    nor the database-setting check can see. The ledger session itself is asserted."""
    admin, _, main = roles
    for replication_role in ("origin", "replica"):
        with admin.connect() as conn:
            try:
                # The superuser sets the value first; the session then becomes the runtime
                # role and keeps it, which is what a cluster-wide setting looks like.
                conn.exec_driver_sql(f"SET session_replication_role = '{replication_role}'")
                conn.exec_driver_sql("SET SESSION AUTHORIZATION assaf_os_ledger")
                with Session(bind=conn) as db:
                    if replication_role == "origin":
                        policy().validate_ledger_role(db, main, migration_role="postgres")
                    else:
                        with pytest.raises(ValueError, match=DENIED):
                            policy().validate_ledger_role(db, main, migration_role="postgres")
            finally:
                conn.rollback()
    validate(roles)


def test_owner_admin_only_exception(roles):
    with roles[0].connect() as c, c.begin():
        c.execute(text(f"GRANT assaf_os_owner TO postgres {ADMIN_ONLY}"))
        catalog(c)
        c.rollback()


def test_connect_on_this_database_is_the_only_database_grant(roles):
    with roles[0].connect() as c, c.begin():
        c.execute(text("GRANT CONNECT ON DATABASE mia_ledger_test TO assaf_os_ledger"))
        catalog(c)
        c.rollback()


def test_main_dsn_is_mandatory_distinct_and_demoted(roles):
    for main in (roles[0], roles[1], create_engine("sqlite://")):
        with Session(roles[1]) as db, pytest.raises(ValueError, match=DENIED):
            policy().validate_ledger_role(db, main, migration_role="postgres")


def test_impersonated_runtime_is_refused(roles):
    with Session(roles[0]) as db:
        db.execute(text("SET LOCAL ROLE assaf_os_ledger"))
        with pytest.raises(ValueError, match=DENIED):
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
            with Session(roles[1]) as db, pytest.raises(ValueError, match=DENIED):
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
        with Session(roles[1]) as db, pytest.raises(ValueError, match=DENIED):
            policy().validate_ledger_role(db, roles[2], migration_role="postgres")


@contextmanager
def workspace(admin, workspace_id):
    """The workspace row exists before the runtime role touches anything.

    §18.2: the runtime role cannot INSERT into os_workspaces. The privileged
    AuthorityAdmin creates it once (`mia os-authority init`, §5.4), so tests do too.
    """
    insert = ("INSERT INTO assaf_os.os_workspaces(workspace_id,owner_id,epoch) "
              "VALUES (:w,'123',1)")
    with admin.begin() as c:
        c.execute(text(insert), {"w": workspace_id})
    try:
        yield
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM assaf_os.os_workspaces WHERE workspace_id=:w"),
                      {"w": workspace_id})


def test_database_enforces_history_and_named_updates(roles):
    from sqlalchemy.exc import SQLAlchemyError
    with workspace(roles[0], "v215"), roles[1].connect() as c, c.begin():
        c.execute(text("UPDATE assaf_os.os_workspaces SET version=1 WHERE workspace_id='v215'"))
        for sql in (
            "INSERT INTO assaf_os.os_workspaces(workspace_id,owner_id,epoch) "
            "VALUES ('v215x','123',1)",
            "DELETE FROM assaf_os.os_workspaces", "TRUNCATE assaf_os.os_events",
            "UPDATE assaf_os.os_receipts SET undo_state='undone'",
            "UPDATE assaf_os.os_events SET body='{}'",
            "UPDATE assaf_os.os_workspaces SET owner_id='456'",
            "UPDATE assaf_os.os_workspaces SET epoch=2",
            "UPDATE assaf_os.os_authority SET state='fenced'",
            "SELECT * FROM assaf_os.schema_migrations",
        ):
            with pytest.raises(SQLAlchemyError), c.begin_nested():
                c.execute(text(sql))
        c.rollback()


def test_column_map_matches_live_catalog(roles):
    with roles[0].connect() as c:
        live = {}
        for table, column, generated in c.execute(text(
            "SELECT c.relname, a.attname, a.attgenerated <> '' FROM pg_attribute a "
            "JOIN pg_class c ON c.oid=a.attrelid WHERE c.relnamespace='assaf_os'::regnamespace "
            "AND c.relkind='r' AND a.attnum > 0 AND NOT a.attisdropped"
        )):
            live.setdefault(table, {})[column] = generated
    assert {name: columns for name, columns in live.items() if name in TABLES} == sql_columns()
    for table, updates in policy().COLUMN_UPDATES.items():
        assert set(updates) <= set(live[table]), table


WS = "v215e"
SEED = (
    "INSERT INTO assaf_os.os_clients(workspace_id,record_id,revision,inputs,source,created_at,"
    f"updated_at) VALUES ('{WS}','C1',1,'{{}}','IMPORT',now(),now())",
    "INSERT INTO assaf_os.os_leads(workspace_id,record_id,revision,inputs,source,created_at,"
    f"updated_at,person_id) VALUES ('{WS}','L1',1,'{{}}','IMPORT',now(),now(),'P1')",
    "INSERT INTO assaf_os.os_works(workspace_id,record_id,revision,inputs,source,created_at,"
    "updated_at,person_id,agreed_amount_minor,currency) "
    f"VALUES ('{WS}','W1',1,'{{}}','IMPORT',now(),now(),'C1',100,'ILS')",
    "INSERT INTO assaf_os.os_payments(workspace_id,record_id,revision,inputs,source,created_at,"
    "updated_at,person_id,work_id,amount_minor,currency) "
    f"VALUES ('{WS}','Y1',1,'{{}}','IMPORT',now(),now(),'C1','W1',100,'ILS')",
    "INSERT INTO assaf_os.os_time(workspace_id,record_id,revision,inputs,source,created_at,"
    f"updated_at,person_id,work_id,minutes) VALUES ('{WS}','T1',1,'{{}}','IMPORT',now(),now(),"
    "'C1','W1',30)",
    "INSERT INTO assaf_os.os_expenses(workspace_id,record_id,revision,inputs,source,created_at,"
    "updated_at,amount_minor,currency,source_type,source_key,source_digest) "
    f"VALUES ('{WS}','E1',1,'{{}}','IMPORT',now(),now(),100,'ILS','manual','k1','d1')",
    "INSERT INTO assaf_os.os_tasks(workspace_id,record_id,revision,inputs,source,created_at,"
    f"updated_at,person_id,work_id,due_date) VALUES ('{WS}','K1',1,'{{}}','IMPORT',now(),now(),"
    "'C1','W1','2026-10-20')",
)


def test_runtime_role_edits_imported_rows_on_every_record_table(roles):
    """§20 V2-15 gate: an owner edit of an imported row succeeds as the runtime role."""
    from sqlalchemy.exc import SQLAlchemyError
    with workspace(roles[0], WS), roles[1].connect() as c, c.begin():
        assert c.scalar(text("SELECT current_user")) == "assaf_os_ledger"
        for statement in SEED:
            c.execute(text(statement))
        c.execute(text(
            "SELECT 1 FROM assaf_os.os_workspaces WHERE workspace_id=:w FOR UPDATE"
        ), {"w": WS})
        for table, typed in RECORD_TYPED.items():
            # The §18.2 columns as spelled in this test, not role_policy's own map.
            assignments = ", ".join(
                "source = 'OS_DIRECT'" if column == "source"
                else "revision = revision + 1" if column == "revision"
                else "updated_at = now()" if column == "updated_at"
                else f"{column} = {column}"
                for column in COMMON + typed
            )
            updated = c.execute(text(
                f"UPDATE assaf_os.{table} SET {assignments} WHERE workspace_id=:w"
            ), {"w": WS})
            assert updated.rowcount == 1, table
        c.execute(text(
            "UPDATE assaf_os.os_tasks SET deleted_at=now(), "
            "deleted_by_request_id='00000000-0000-4000-8000-000000000001' WHERE workspace_id=:w"
        ), {"w": WS})
        for sql in (
            "UPDATE assaf_os.os_clients SET record_id='C2'",
            "UPDATE assaf_os.os_clients SET workspace_id='other'",
            "UPDATE assaf_os.os_clients SET created_at=now()",
            "UPDATE assaf_os.os_tasks SET status='x'",
            "DELETE FROM assaf_os.os_clients",
        ):
            with pytest.raises(SQLAlchemyError), c.begin_nested():
                c.execute(text(sql))
        # §22 E1-rev status machine, with only the §18.2 import UPDATE columns.
        batches = {name: f"00000000-0000-4000-8000-00000000010{i}"
                   for i, name in enumerate(("imported", "quarantined", "failed"))}
        for name, batch in batches.items():
            c.execute(text(
                "INSERT INTO assaf_os.os_import_batches(workspace_id,batch_id,source_binding,"
                "source_digest,authority_epoch,provenance) VALUES (:w,:b,'wb',:d,1,'{}')"
            ), {"w": WS, "b": batch, "d": "digest-" + name})
        c.execute(text(
            "INSERT INTO assaf_os.os_import_rows(workspace_id,batch_id,row_number,entity,"
            "source_digest,status,raw_record) VALUES (:w,:b,1,'clients','r1','staged','{}')"
        ), {"w": WS, "b": batches["imported"]})
        for sql, batch in (
            ("SET status='validated'", "imported"),
            ("SET status='imported', completed_at=now()", "imported"),
            ("SET status='quarantined', quarantine='{\"rows\": 1}', completed_at=now()",
             "quarantined"),
            ("SET status='failed', completed_at=now()", "failed"),
        ):
            updated = c.execute(text(
                f"UPDATE assaf_os.os_import_batches {sql} WHERE workspace_id=:w AND batch_id=:b"
            ), {"w": WS, "b": batches[batch]})
            assert updated.rowcount == 1, sql
        for sql in (
            "UPDATE assaf_os.os_import_batches SET provenance='{}'",
            "UPDATE assaf_os.os_import_batches SET source_digest='x'",
            "UPDATE assaf_os.os_import_rows SET raw_record='{}'",
            "DELETE FROM assaf_os.os_import_rows",
        ):
            with pytest.raises(SQLAlchemyError), c.begin_nested():
                c.execute(text(sql))
        c.rollback()


# ------------------------------------------- provisioning entrypoints (run last: they
# drop and re-provision the module's roles; settle() restores the baseline after each)


def drop_runtime_roles(admin, runtime):
    runtime.dispose()
    with admin.begin() as c:
        for name in ("assaf_os_ledger", "assaf_os_retention"):
            c.execute(text(f"DROP OWNED BY {name}"))
            c.execute(text(f"DROP ROLE {name}"))
        c.execute(text("REVOKE assaf_os_owner FROM postgres"))


def role_state(admin):
    with admin.connect() as c:
        return (
            c.execute(text(
                "SELECT rolname FROM pg_roles WHERE rolname IN "
                "('assaf_os_ledger','assaf_os_retention') ORDER BY 1"
            )).scalars().all(),
            c.scalar(text(
                "SELECT count(*) FROM pg_auth_members WHERE roleid='assaf_os_owner'::regrole"
            )),
        )


def ledger_password(admin):
    with admin.connect() as c:
        return c.scalar(text("SELECT rolpassword FROM pg_authid WHERE rolname='assaf_os_ledger'"))


def posture(admin):
    """Ownership, ACLs, default ACLs, memberships and password hashes, as text."""
    with admin.connect() as c:
        return c.execute(text("""
            SELECT 'schema', n.nspname, pg_get_userbyid(n.nspowner), n.nspacl::text
              FROM pg_namespace n WHERE n.nspname='assaf_os'
            UNION ALL SELECT 'relation', c.relname, pg_get_userbyid(c.relowner), c.relacl::text
              FROM pg_class c WHERE c.relnamespace=to_regnamespace('assaf_os')
            UNION ALL SELECT 'column', c.relname || '.' || a.attname, '', a.attacl::text
              FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
              WHERE c.relnamespace=to_regnamespace('assaf_os') AND a.attacl IS NOT NULL
            UNION ALL SELECT 'function', p.oid::regprocedure::text, pg_get_userbyid(p.proowner),
              p.proacl::text FROM pg_proc p WHERE p.pronamespace=to_regnamespace('assaf_os')
            UNION ALL SELECT 'default', pg_get_userbyid(d.defaclrole), d.defaclobjtype::text,
              d.defaclacl::text FROM pg_default_acl d
            UNION ALL SELECT 'database', d.datname, '', d.datacl::text
              FROM pg_database d WHERE d.datname=current_database()
            UNION ALL SELECT 'edge', pg_get_userbyid(m.member), pg_get_userbyid(m.grantor),
              m.admin_option::text || m.set_option::text || m.inherit_option::text
              FROM pg_auth_members m WHERE m.roleid='assaf_os_owner'::regrole
            UNION ALL SELECT 'role', r.rolname, '', md5(coalesce(r.rolpassword, ''))
              FROM pg_authid r WHERE r.rolname IN ('assaf_os_ledger','assaf_os_retention')
            ORDER BY 1, 2, 3
        """)).all()


def pin(p, monkeypatch, admin, name="local-v2-15", database=None, runtime_env=None):
    url = admin.url.set(database=database or admin.url.database)
    monkeypatch.setattr(p, "_TARGETS", {name: p.Target(
        url.host, url.port, url.database, "postgres", "v215_main", runtime_env,
    )})
    monkeypatch.setenv("MIA_BUILD_SHA", SHA)
    monkeypatch.setenv("MIA_DATABASE_URL", url.set(username="v215_main").render_as_string(False))
    monkeypatch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", url.render_as_string(False))
    return url


def test_provision_apply_creates_validates_and_rolls_back(roles, monkeypatch):
    admin, runtime, main = roles
    p = provision()
    url = pin(p, monkeypatch, admin)
    args = ["--target", "local-v2-15", "--apply", "--approved-sha", SHA]
    try:
        drop_runtime_roles(admin, runtime)
        # Wrong target: refused before any DDL.
        monkeypatch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL",
                           url.set(port=1).render_as_string(False))
        with pytest.raises(SystemExit) as denied:
            p.main(args)
        assert denied.value.code == 1
        assert role_state(admin) == ([], 0)
        monkeypatch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", url.render_as_string(False))
        # The post-DDL catalog audit refuses: the whole transaction rolls back.
        validator = p.validate_ledger_catalog
        def refuse(*args, **kwargs):
            raise ValueError("ledger_role_denied")
        monkeypatch.setattr(p, "validate_ledger_catalog", refuse)
        with pytest.raises(SystemExit):
            p.main(args)
        assert role_state(admin) == ([], 0)
        monkeypatch.setattr(p, "validate_ledger_catalog", validator)
        # A main DSN that is not demoted fails the in-transaction audit: rolled back.
        with admin.begin() as c:
            c.execute(text("ALTER ROLE v215_main CREATEDB"))
        try:
            with pytest.raises(SystemExit):
                p.main(args)
        finally:
            with admin.begin() as c:
                c.execute(text("ALTER ROLE v215_main NOCREATEDB"))
        assert role_state(admin) == ([], 0)
        p.main(args)
        assert role_state(admin) == (["assaf_os_ledger", "assaf_os_retention"], 1)
        validate(roles)
        validate(roles, None)
        # The local pin assigns no password; provision never adopts an existing role.
        assert ledger_password(admin) is None
        before = posture(admin)
        with pytest.raises(SystemExit) as again:
            p.main(args)
        assert again.value.code == 1
        assert posture(admin) == before
    finally:
        settle(admin, url.database)
    validate(roles)


def test_production_pin_apply_writes_runtime_env_only(roles, monkeypatch, tmp_path, capsys):
    admin, runtime, _ = roles
    p = provision()
    env = tmp_path / "runtime.env"
    original = "MIA_DATABASE_URL=postgresql://keep@127.0.0.1:5432/mia\nMIA_APP_ROLE_POSTURE=enforce\n"
    url = pin(p, monkeypatch, admin, name="mia-vm", runtime_env=str(env))
    args = ["--target", "mia-vm", "--apply", "--approved-sha", SHA]
    dsn = re.compile(
        rf"MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://assaf_os_ledger:([0-9a-f]{{64}})"
        rf"@{re.escape(url.host)}:{url.port}/{url.database}"
    )
    try:
        drop_runtime_roles(admin, runtime)
        # An unparseable staged DSN refuses before anything is created or written.
        bad = original + "MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://assaf_os_ledger:x@h/d\n"
        env.write_text(bad, encoding="utf-8", newline="\n")
        os.chmod(env, 0o600)
        with pytest.raises(SystemExit):
            p.main(args)
        assert role_state(admin) == ([], 0)
        assert env.read_text(encoding="utf-8") == bad
        env.write_text(original, encoding="utf-8", newline="\n")
        os.chmod(env, 0o600)
        capsys.readouterr()
        p.main(args)
        printed = capsys.readouterr()
        content = env.read_bytes().decode("utf-8")
        lines = content.splitlines()
        assert lines[:2] == original.splitlines() and len(lines) == 3
        password = dsn.fullmatch(lines[2]).group(1)
        for secret in (password, "postgresql", "SCRAM-SHA-256"):
            assert secret not in printed.out + printed.err
        if os.name == "posix":
            assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert scram_matches(ledger_password(admin), password)
        assert role_state(admin) == (["assaf_os_ledger", "assaf_os_retention"], 1)
        validate(roles)
        # reapply keeps the password and never rewrites the env file.
        verifier = ledger_password(admin)
        p.main(["--target", "mia-vm", "--mode", "reapply", "--apply", "--approved-sha", SHA])
        assert ledger_password(admin) == verifier
        assert env.read_bytes().decode("utf-8") == content
        # A retry after the roles are gone reuses the staged credential.
        drop_runtime_roles(admin, runtime)
        p.main(args)
        assert env.read_bytes().decode("utf-8") == content
        assert scram_matches(ledger_password(admin), password)
        assert ledger_password(admin) != verifier  # fresh salt, same password
    finally:
        settle(admin, url.database)
    validate(roles)


def test_reapply_restores_posture_idempotently_and_keeps_passwords(roles, monkeypatch, capsys):
    admin, _, _ = roles
    p = provision()
    url = pin(p, monkeypatch, admin)
    reapply = ["--target", "local-v2-15", "--mode", "reapply", "--apply", "--approved-sha", SHA]
    try:
        with admin.begin() as c:
            c.execute(text("ALTER ROLE assaf_os_ledger PASSWORD 'v215-synthetic-password'"))
            # What `pg_restore --no-owner --no-privileges` as the superuser leaves
            # behind, plus a stale extra grant and PUBLIC TEMP.
            c.execute(text("ALTER SCHEMA assaf_os OWNER TO postgres"))
            for table in OWNED_TABLES:
                c.execute(text(f"ALTER TABLE assaf_os.{table} OWNER TO postgres"))
            for function in OWNED_FUNCTIONS:
                c.execute(text(f"ALTER FUNCTION assaf_os.{function} OWNER TO postgres"))
            c.execute(text("REVOKE ALL ON ALL TABLES IN SCHEMA assaf_os FROM assaf_os_ledger"))
            c.execute(text("REVOKE ALL ON SCHEMA assaf_os FROM assaf_os_ledger"))
            c.execute(text("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA assaf_os TO PUBLIC"))
            c.execute(text(
                "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner "
                "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
            ))
            c.execute(text(f"GRANT TEMPORARY ON DATABASE {url.database} TO PUBLIC"))
            c.execute(text("GRANT UPDATE (body) ON assaf_os.os_events TO assaf_os_ledger"))
        password = ledger_password(admin)
        assert scram_matches(password, "v215-synthetic-password")
        with pytest.raises(ValueError, match=DENIED):
            validate(roles)
        p.main(reapply)
        validate(roles)
        validate(roles, None)
        first = posture(admin)
        assert ledger_password(admin) == password
        assert ("schema", "assaf_os", "assaf_os_owner") == first[
            next(i for i, row in enumerate(first) if row[0] == "schema")
        ][:3]
        assert {row[2] for row in first if row[0] in {"relation", "function"}} == {
            "assaf_os_owner"
        }
        capsys.readouterr()
        p.main(reapply)
        assert "CREATE ROLE" not in capsys.readouterr().out  # existing roles are adopted
        assert posture(admin) == first
        assert ledger_password(admin) == password
    finally:
        settle(admin, url.database)
        with admin.begin() as c:
            c.execute(text("ALTER ROLE assaf_os_ledger PASSWORD NULL"))
    validate(roles)


def test_reapply_without_ledger_schema_is_a_noop(roles, monkeypatch, capsys):
    admin, _, _ = roles
    name = "v215_r2_noschema"
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        assert not c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=:n)"),
                            {"n": name})
        c.execute(text(f"CREATE DATABASE {name}"))
    empty = create_engine(admin.url.set(database=name))
    try:
        p = provision()
        pin(p, monkeypatch, admin, database=name)
        before = posture(empty), role_state(admin)
        capsys.readouterr()
        p.main(["--target", "local-v2-15", "--mode", "reapply", "--apply", "--approved-sha", SHA])
        assert capsys.readouterr().out == p.NOOP + "\n"
        assert (posture(empty), role_state(admin)) == before
        with empty.connect() as c:
            assert c.scalar(text("SELECT to_regnamespace('assaf_os')")) is None
    finally:
        empty.dispose()
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    validate(roles)
