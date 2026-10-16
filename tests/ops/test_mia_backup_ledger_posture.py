"""V2-10 ledger posture: a restore must give schema assaf_os back its owner and its grants.

`mia restore` and the drill both run pg_restore with --no-owner --no-privileges, so a restored
assaf_os belongs to the restoring role and its functions fall back to the default PUBLIC
EXECUTE. ledger_posture re-owns, revokes, re-applies the reviewed grant set and then checks the
result. The SQL is proven twice: its shape with fakes, and its behaviour on a real PostgreSQL
(skipped unless MIA_OWNER_OS_TEST_POSTGRES_URL is set, like the other database proofs).
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from uuid import uuid4

import backup_lib
import ledger_posture
import pytest

from tests.ops.fakes import FakeRunner

GRANTS = """\
-- the reviewed V2-15 grant set (a stand-in for the real file)
GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA assaf_os TO assaf_os_ledger;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA assaf_os TO assaf_os_ledger;
"""


def error_code(call) -> str:
    with pytest.raises(backup_lib.BackupError) as raised:
        call()
    return raised.value.code


def test_the_roles_and_schema_are_the_ones_v2_14_and_v2_15_name():
    assert ledger_posture.SCHEMA == "assaf_os"
    assert ledger_posture.OWNER_ROLE == "assaf_os_owner"
    assert ledger_posture.LEDGER_ROLE == "assaf_os_ledger"
    assert ledger_posture.POSTURE_CHECKS == (
        "schema_owner",
        "object_owner",
        "public_grants",
        "other_grantees",
        "ledger_destructive",
    )


# --- the grant file --------------------------------------------------------------------------


def test_a_grant_file_is_split_into_statements_and_comments_are_dropped():
    statements = ledger_posture.parse_grants(GRANTS)
    assert statements == [
        "GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger",
        "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA assaf_os TO assaf_os_ledger",
        "GRANT USAGE ON ALL SEQUENCES IN SCHEMA assaf_os TO assaf_os_ledger",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "REVOKE ALL ON SCHEMA assaf_os FROM PUBLIC;",
        "GRANT UPDATE (state) ON assaf_os.os_proposals TO assaf_os_ledger;",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA assaf_os\n  REVOKE ALL ON TABLES FROM PUBLIC;",
        "grant select on assaf_os.os_events to assaf_os_ledger;",
    ],
)
def test_grants_revokes_and_default_privileges_on_assaf_os_are_accepted(text):
    assert len(ledger_posture.parse_grants(text)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "-- only a comment\n",
        "DROP TABLE assaf_os.os_events;",
        "DROP DATABASE mia;",
        "DELETE FROM assaf_os.os_events;",
        "SELECT pg_sleep(60);",
        "COMMIT;",
        "ALTER ROLE assaf_os_ledger SUPERUSER;",
        "GRANT assaf_os_owner TO mia;",  # a membership grant is not a schema grant
        "GRANT SELECT ON public.leads TO assaf_os_ledger;",  # not assaf_os
        "GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger; DROP TABLE public.leads;",
        "GRANT SELECT ON assaf_os.x TO a /* hidden */;",
        "GRANT SELECT ON assaf_os.x TO a; \\c other",
        "GRANT SELECT ON assaf_os.x TO 'quoted';",
        "GRANT SELECT ON assaf_os.x TO $$a$$;",
    ],
)
def test_anything_else_in_a_grant_file_is_refused(text):
    assert error_code(lambda: ledger_posture.parse_grants(text)) == "ledger_grants_invalid"


# --- the generated SQL -----------------------------------------------------------------------


def test_apply_reowns_everything_revokes_public_and_reapplies_the_grants_in_that_order():
    statements = ledger_posture.parse_grants(GRANTS)
    sql = ledger_posture.apply_sql(statements)
    needles = [
        "ALTER SCHEMA assaf_os OWNER TO assaf_os_owner",
        "REVOKE ALL ON SCHEMA assaf_os FROM PUBLIC",
        "REVOKE ALL ON ALL TABLES IN SCHEMA assaf_os FROM PUBLIC",
        "REVOKE ALL ON ALL SEQUENCES IN SCHEMA assaf_os FROM PUBLIC",
        "REVOKE ALL ON ALL ROUTINES IN SCHEMA assaf_os FROM PUBLIC",
        "GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger",
        "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA assaf_os TO assaf_os_ledger",
    ]
    positions = [sql.index(needle) for needle in needles]
    assert positions == sorted(positions)
    for kind in (
        "TABLE",
        "VIEW",
        "MATERIALIZED VIEW",
        "FOREIGN TABLE",
        "SEQUENCE",
        "PROCEDURE",
        "FUNCTION",
        "TYPE",
    ):
        assert kind in sql, kind
    assert "deptype = 'i'" in sql  # identity sequences follow their table; altering one errors
    assert (
        "'DOMAIN'" in sql and "FROM PUBLIC" in sql
    )  # enum and domain types default to PUBLIC USAGE


def test_apply_touches_only_the_assaf_os_schema_and_never_deletes_or_drops():
    sql = ledger_posture.apply_sql(ledger_posture.parse_grants(GRANTS))
    for word in ("DROP ", "DELETE ", "TRUNCATE", "public.", "CASCADE", "REASSIGN", "pg_database"):
        assert word not in sql, word
    # One -c string is already one transaction: no BEGIN; or COMMIT; statements of our own.
    assert not re.search(r"\bBEGIN\s*;", sql, re.IGNORECASE)
    assert "COMMIT" not in sql.upper()


def test_the_grants_are_applied_after_the_revokes_so_they_are_not_undone():
    sql = ledger_posture.apply_sql(["GRANT SELECT ON assaf_os.x TO assaf_os_ledger"])
    assert sql.rindex("REVOKE ALL ON ALL ROUTINES") < sql.index("GRANT SELECT ON assaf_os.x")


def test_role_names_are_plain_identifiers_before_they_reach_any_sql():
    for bad in ["", "Owner", "a-b", "x; DROP DATABASE mia", "a b", "1abc", "a" * 64]:
        assert (
            error_code(lambda bad=bad: ledger_posture.apply_sql([], owner=bad))
            == "ledger_role_invalid"
        )
        assert (
            error_code(lambda bad=bad: ledger_posture.posture_sql(ledger=bad))
            == "ledger_role_invalid"
        )
        assert (
            error_code(lambda bad=bad: ledger_posture.ensure_roles_sql(owner=bad))
            == "ledger_role_invalid"
        )


def test_the_posture_query_returns_one_count_per_check_in_a_fixed_order():
    sql = ledger_posture.posture_sql()
    positions = [sql.index(f"AS {name}") for name in ledger_posture.POSTURE_CHECKS]
    assert positions == sorted(positions)
    assert sql.lstrip().startswith(("WITH", "SELECT"))
    writes = r"\b(INSERT\s+INTO|DELETE\s+FROM|UPDATE\s+\w+\s+SET|DROP|ALTER|CREATE|GRANT|REVOKE)\b"
    assert not re.search(writes, sql, re.IGNORECASE)  # the verifier reads and never writes
    assert "IS DISTINCT FROM" in sql  # a missing owner role must count as a violation
    assert "acldefault" in sql  # a NULL ACL is the default one, which includes PUBLIC EXECUTE


def test_the_scratch_roles_are_created_only_when_missing_and_cannot_log_in():
    sql = ledger_posture.ensure_roles_sql()
    assert sql.count("IF NOT EXISTS") == 2 and sql.count("NOLOGIN") == 2
    assert "SUPERUSER" not in sql.replace("NOSUPERUSER", "")


def test_posture_output_is_parsed_and_a_malformed_answer_is_not_a_pass():
    assert ledger_posture.parse_posture("0|0|0|0|0\n") == dict.fromkeys(
        ledger_posture.POSTURE_CHECKS, 0
    )
    assert ledger_posture.parse_posture("1|0|2|0|0")["public_grants"] == 2
    for bad in ["", "0|0|0", "a|b|c|d|e", "0|0|0|0|-1", "0|0|0|0|0|0", "ERROR"]:
        assert (
            error_code(lambda bad=bad: ledger_posture.parse_posture(bad))
            == "ledger_posture_unreadable"
        )


# --- evaluate, with a scripted database -------------------------------------------------------


class Db:
    """Answers the three kinds of statement evaluate sends, and records them in order."""

    def __init__(self, present=True, posture="0|0|0|0|0", fail=(), present_rc=0):
        self.present, self.posture, self.fail, self.present_rc = present, posture, fail, present_rc
        self.sent: list[str] = []

    def __call__(self, sql: str) -> backup_lib.Result:
        self.sent.append(sql)
        if any(part in sql for part in self.fail):
            return backup_lib.Result(1, b"", b"ERROR: simulated")
        if "AS ledger_destructive" in sql:
            return backup_lib.Result(0, self.posture.encode() + b"\n")
        if "count(*)" in sql and "nspname = 'assaf_os'" in sql:
            return backup_lib.Result(self.present_rc, b"1\n" if self.present else b"0\n")
        return backup_lib.Result(0)

    def kinds(self) -> list[str]:
        names = []
        for sql in self.sent:
            if "AS ledger_destructive" in sql:
                names.append("posture")
            elif "count(*)" in sql:
                names.append("present")
            elif "CREATE ROLE" in sql:
                names.append("roles")
            elif "ALTER SCHEMA" in sql:
                names.append("apply")
        return names


def outcome(db, **options):
    options.setdefault("grants_text", GRANTS)
    return ledger_posture.evaluate(db, **options)


def named(result) -> dict[str, bool]:
    return {check["name"]: check["ok"] for check in result.checks}


def test_a_database_without_the_ledger_schema_needs_nothing():
    db = Db(present=False)
    result = outcome(db)
    assert result.passed and result.present is False
    assert db.kinds() == ["present"] and named(result) == {"ledger_schema": True}


def test_a_ledger_schema_with_no_grant_file_fails_closed_and_changes_nothing():
    db = Db()
    result = outcome(db, grants_text=None)
    assert not result.passed and result.present is True
    assert named(result) == {"ledger_schema": True, "ledger_grants": False}
    assert db.kinds() == ["present"]


def test_a_bad_grant_file_fails_closed_and_changes_nothing():
    db = Db()
    result = outcome(db, grants_text="DROP TABLE assaf_os.os_events;")
    assert not result.passed and named(result)["ledger_grants"] is False
    assert db.kinds() == ["present"]


def test_apply_runs_roles_then_reown_then_posture_and_passes():
    db = Db()
    result = outcome(db, create_roles=True)
    assert result.passed and result.present is True
    assert db.kinds() == ["present", "roles", "apply", "posture"]
    assert named(result) == {
        "ledger_schema": True,
        "ledger_grants": True,
        "ledger_roles": True,
        "ledger_reown_regrant": True,
        "ledger_posture": True,
    }
    assert "GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA assaf_os TO assaf_os_ledger" in db.sent[2]


def test_roles_are_not_created_unless_asked():
    db = Db()
    outcome(db)
    assert "roles" not in db.kinds()


def test_a_failed_reown_stops_before_the_posture_check():
    db = Db(fail=("ALTER SCHEMA",))
    result = outcome(db)
    assert not result.passed and named(result)["ledger_reown_regrant"] is False
    assert "posture" not in db.kinds()


def test_a_failed_role_creation_stops_before_anything_is_changed():
    db = Db(fail=("CREATE ROLE",))
    result = outcome(db, create_roles=True)
    assert not result.passed and named(result)["ledger_roles"] is False
    assert db.kinds() == ["present", "roles"]


@pytest.mark.parametrize(
    ("posture", "failing"),
    [
        ("1|0|0|0|0", "schema_owner"),
        ("0|2|0|0|0", "object_owner"),
        ("0|0|3|0|0", "public_grants"),
        ("0|0|0|1|0", "other_grantees"),
        ("0|0|0|0|1", "ledger_destructive"),
    ],
)
def test_every_posture_violation_fails_the_restore_and_names_the_check(posture, failing):
    result = outcome(Db(posture=posture))
    assert not result.passed and named(result)["ledger_posture"] is False
    assert failing in next(c for c in result.checks if c["name"] == "ledger_posture")["detail"]
    assert result.violations[failing] > 0


def test_an_unreadable_posture_answer_fails_the_restore():
    result = outcome(Db(posture="garbage"))
    assert not result.passed and named(result)["ledger_posture"] is False


def test_a_failing_schema_probe_is_a_failed_check_not_a_pass():
    result = outcome(Db(present_rc=1))
    assert not result.passed and named(result) == {"ledger_schema": False}


def test_verify_only_reads_and_does_not_need_a_grant_file():
    db = Db()
    result = ledger_posture.evaluate(db, grants_text=None, apply=False)
    assert result.passed and db.kinds() == ["present", "posture"]
    failing = ledger_posture.evaluate(Db(posture="0|0|1|0|0"), grants_text=None, apply=False)
    assert not failing.passed


# --- the command line (the production restore path) ---------------------------------------------


def cli(arguments, db=None):
    db = db or Db()

    def handler(call):
        statement = call.argv[call.argv.index("-c") + 1]
        return db(statement)

    runner = FakeRunner(handler, real=())
    return ledger_posture.main(arguments, runner=runner), runner, db


@pytest.fixture
def grants_file(tmp_path):
    path = tmp_path / "ledger-grants.sql"
    path.write_text(GRANTS, encoding="utf-8")
    return path


def test_plan_prints_the_sql_and_runs_nothing(grants_file, capsys):
    code, runner, _ = cli(["plan", "--grants-file", str(grants_file)])
    out = capsys.readouterr().out
    assert code == 0 and runner.calls == []
    assert "ALTER SCHEMA assaf_os OWNER TO assaf_os_owner" in out and "AS ledger_destructive" in out


def test_apply_runs_through_compose_against_the_production_database(grants_file, capsys):
    code, runner, db = cli(["apply", "--grants-file", str(grants_file)])
    assert code == 0 and db.kinds() == ["present", "apply", "posture"]
    for call in runner.calls:
        assert call.argv[:11] == [
            "docker",
            "compose",
            "-p",
            "mia",
            "-f",
            "/opt/mia/compose.yaml",
            "--env-file",
            "/opt/mia/tag.env",
            "exec",
            "-T",
            "postgres",
        ]
        assert call.argv[11:17] == ["psql", "-U", "mia", "-d", "mia", "-v"]
        assert "ON_ERROR_STOP=1" in call.argv
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True and report["schema_present"] is True


def test_apply_never_creates_roles_in_production(grants_file):
    _, _, db = cli(["apply", "--grants-file", str(grants_file)])
    assert "roles" not in db.kinds()


def test_apply_exits_one_when_the_posture_does_not_pass(grants_file, capsys):
    code, _, _ = cli(["apply", "--grants-file", str(grants_file)], Db(posture="0|0|1|0|0"))
    assert code == 1 and json.loads(capsys.readouterr().out)["passed"] is False


def test_apply_without_a_grant_file_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as raised:
        cli(["apply"])
    assert raised.value.code == 2


def test_apply_with_a_missing_or_unsafe_grant_file_changes_nothing(tmp_path, capsys):
    code, runner, _ = cli(["apply", "--grants-file", str(tmp_path / "nope.sql")])
    assert code == 2 and runner.calls == []
    bad = tmp_path / "bad.sql"
    bad.write_text("DROP TABLE assaf_os.os_events;", encoding="utf-8")
    code, runner, _ = cli(["apply", "--grants-file", str(bad)])
    assert code == 2 and runner.calls == []


def test_verify_reads_only(capsys):
    code, runner, db = cli(["verify"])
    assert code == 0 and db.kinds() == ["present", "posture"]
    assert not any("ALTER" in c.text or "GRANT" in c.text for c in runner.calls)


def test_on_a_vm_without_the_ledger_schema_apply_is_a_pass_that_changes_nothing(grants_file):
    code, _, db = cli(["apply", "--grants-file", str(grants_file)], Db(present=False))
    assert code == 0 and db.kinds() == ["present"]


# --- a real PostgreSQL --------------------------------------------------------------------------


@pytest.fixture
def pg():
    url = os.getenv("MIA_OWNER_OS_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OWNER_OS_TEST_POSTGRES_URL required")
    psycopg = pytest.importorskip("psycopg")
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1", "::1"}
    assert parsed.database == "owner_os_test"

    def dsn(database: str) -> str:
        return parsed.set(drivername="postgresql", database=database).render_as_string(
            hide_password=False
        )

    suffix = uuid4().hex[:12]
    database = f"v210_{suffix}"
    roles = SimpleNamespace(
        owner=f"v210_owner_{suffix}", ledger=f"v210_ledger_{suffix}", other=f"v210_other_{suffix}"
    )
    admin = psycopg.connect(dsn(parsed.database), autocommit=True)
    admin.execute(f"CREATE DATABASE {database}")
    for role in (roles.owner, roles.ledger, roles.other):
        admin.execute(f"CREATE ROLE {role} NOLOGIN")
    conn = psycopg.connect(dsn(database), autocommit=True)
    # What pg_restore --no-owner --no-privileges leaves behind: everything owned by the
    # restoring role, every ACL at its default.
    conn.execute("""
        CREATE SCHEMA assaf_os;
        CREATE TABLE assaf_os.os_workspaces (id serial PRIMARY KEY, name text);
        CREATE TABLE assaf_os.os_events (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                                         body text);
        CREATE VIEW assaf_os.os_summary AS SELECT id FROM assaf_os.os_workspaces;
        CREATE TYPE assaf_os.os_state AS ENUM ('shadow', 'active');
        CREATE FUNCTION assaf_os.os_version() RETURNS int LANGUAGE sql AS 'SELECT 1';
    """)

    def run_sql(sql: str) -> backup_lib.Result:
        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchall() if cursor.description else []
        except psycopg.Error as error:
            return backup_lib.Result(1, b"", str(error).encode())
        return backup_lib.Result(
            0, "\n".join("|".join(str(v) for v in row) for row in rows).encode()
        )

    def scalar(sql: str):
        return conn.execute(sql).fetchone()[0]

    yield SimpleNamespace(run=run_sql, scalar=scalar, roles=roles, conn=conn)
    conn.close()
    admin.execute(f"DROP DATABASE {database} WITH (FORCE)")
    for role in (roles.owner, roles.ledger, roles.other):
        admin.execute(f"DROP ROLE {role}")
    admin.close()


def real(pg, grants_text, **options):
    return ledger_posture.evaluate(
        pg.run, grants_text=grants_text, owner=pg.roles.owner, ledger=pg.roles.ledger, **options
    )


def grants_for(pg) -> str:
    return (
        f"GRANT USAGE ON SCHEMA assaf_os TO {pg.roles.ledger};\n"
        f"GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA assaf_os TO {pg.roles.ledger};\n"
        f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA assaf_os TO {pg.roles.ledger};\n"
        f"GRANT EXECUTE ON ALL ROUTINES IN SCHEMA assaf_os TO {pg.roles.ledger};\n"
    )


def test_pg_a_freshly_restored_ledger_schema_is_detected_as_unsafe(pg):
    result = real(pg, None, apply=False)
    assert not result.passed
    assert result.violations["schema_owner"] == 1
    assert result.violations["object_owner"] >= 5  # tables, view, type, function, sequences
    assert result.violations["public_grants"] >= 2  # function EXECUTE and enum USAGE are PUBLIC


def test_pg_apply_reowns_revokes_and_regrants_and_the_posture_passes(pg):
    result = real(pg, grants_for(pg))
    assert result.passed, result.checks
    assert all(v == 0 for v in result.violations.values())
    owner = pg.roles.owner
    assert (
        pg.scalar("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'assaf_os'")
        == owner
    )
    assert (
        pg.scalar(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = 'assaf_os' AND c.relkind IN ('r','v','S') "
            f"AND pg_get_userbyid(c.relowner) <> '{owner}'"
        )
        == 0
    )
    assert (
        pg.scalar("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'os_version'")
        == owner
    )
    assert (
        pg.scalar("SELECT pg_get_userbyid(typowner) FROM pg_type WHERE typname = 'os_state'")
        == owner
    )
    ledger, other = pg.roles.ledger, pg.roles.other
    assert pg.scalar(f"SELECT has_table_privilege('{ledger}', 'assaf_os.os_events', 'INSERT')")
    assert not pg.scalar(f"SELECT has_table_privilege('{ledger}', 'assaf_os.os_events', 'DELETE')")
    assert not pg.scalar(
        f"SELECT has_table_privilege('{ledger}', 'assaf_os.os_events', 'TRUNCATE')"
    )
    assert not pg.scalar(f"SELECT has_table_privilege('{other}', 'assaf_os.os_events', 'SELECT')")
    assert not pg.scalar(
        f"SELECT has_function_privilege('{other}', 'assaf_os.os_version()', 'EXECUTE')"
    )
    assert not pg.scalar(f"SELECT has_schema_privilege('{other}', 'assaf_os', 'USAGE')")


def test_pg_apply_is_repeatable(pg):
    assert real(pg, grants_for(pg)).passed
    assert real(pg, grants_for(pg)).passed
    assert real(pg, None, apply=False).passed


def test_pg_a_grant_set_that_lets_the_ledger_delete_fails_the_posture(pg):
    grants = (
        grants_for(pg) + f"GRANT DELETE ON ALL TABLES IN SCHEMA assaf_os TO {pg.roles.ledger};\n"
    )
    result = real(pg, grants)
    assert not result.passed and result.violations["ledger_destructive"] > 0


@pytest.mark.parametrize(
    ("statement", "failing"),
    [
        ("GRANT SELECT ON assaf_os.os_workspaces TO PUBLIC", "public_grants"),
        ("GRANT EXECUTE ON FUNCTION assaf_os.os_version() TO PUBLIC", "public_grants"),
        ("GRANT USAGE ON TYPE assaf_os.os_state TO PUBLIC", "public_grants"),
        ("GRANT SELECT ON assaf_os.os_workspaces TO {other}", "other_grantees"),
        ("GRANT UPDATE (name) ON assaf_os.os_workspaces TO {other}", "other_grantees"),
        ("GRANT USAGE ON SCHEMA assaf_os TO {other}", "other_grantees"),
        ("ALTER TABLE assaf_os.os_events OWNER TO {other}", "object_owner"),
        ("ALTER SCHEMA assaf_os OWNER TO {other}", "schema_owner"),
    ],
)
def test_pg_drift_after_a_good_restore_is_caught(pg, statement, failing):
    assert real(pg, grants_for(pg)).passed
    pg.conn.execute(statement.format(other=pg.roles.other))
    result = real(pg, None, apply=False)
    assert not result.passed and result.violations[failing] > 0


def test_pg_a_missing_owner_role_is_a_violation_not_a_silent_pass(pg):
    result = ledger_posture.evaluate(
        pg.run, grants_text=None, apply=False, owner="v210_no_such_role", ledger=pg.roles.ledger
    )
    assert not result.passed and result.violations["schema_owner"] == 1


def test_pg_a_database_without_the_schema_passes(pg):
    pg.conn.execute("DROP SCHEMA assaf_os CASCADE")
    result = real(pg, None)
    assert result.passed and result.present is False


def test_pg_roles_for_a_scratch_server_are_created_once_and_without_login(pg):
    pg.conn.execute(f"DROP ROLE IF EXISTS {pg.roles.other}")
    sql = ledger_posture.ensure_roles_sql(owner=pg.roles.owner, ledger=pg.roles.other)
    assert pg.run(sql).returncode == 0 and pg.run(sql).returncode == 0
    assert not pg.scalar(f"SELECT rolcanlogin FROM pg_roles WHERE rolname = '{pg.roles.other}'")
    assert pg.scalar(f"SELECT rolinherit FROM pg_roles WHERE rolname = '{pg.roles.other}'") is False
