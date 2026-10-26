"""Production ledger contract, exercised only on the named disposable database."""

import hashlib
import json
import os
import random
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from tests.unit.test_owner_os_postgres import os_pg as os_pg  # pytest fixture

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = "20261015_assaf_os_ledger.sql"
TABLES = {
    "os_workspaces", "os_authority", "os_leads", "os_clients", "os_works", "os_payments",
    "os_time", "os_expenses", "os_tasks", "os_proposals", "os_receipts", "os_events",
    "os_outbox", "os_mirror_state", "os_summary_monthly", "os_summary_cycle",
    "os_import_batches", "os_import_rows",
}


def test_ledger_is_an_explicit_allowlisted_migration():
    from app.db import os_ledger_migrate as ledger
    from app.db.migrate import POSTGRES_ONLY, list_migration_files

    assert ledger.MIGRATIONS == (
        MIGRATION, "20261016_assaf_os_ledger_core_v2.sql", "20261016_assaf_os_statements.sql",
        "20261016_assaf_os_assistant.sql", "20261024_assaf_os_issue_requests.sql",
        "20261025_assaf_os_assistant_followups.sql", "20261026_assaf_os_oauth.sql",
    )
    assert MIGRATION in POSTGRES_ONLY
    assert (ROOT / "migrations_assaf_os" / MIGRATION).is_file()
    assert not any("assaf_os" in path.read_text(encoding="utf-8")
                   for path in list_migration_files())
    assert "COPY migrations_assaf_os ./migrations_assaf_os" in (
        ROOT / "deploy/Dockerfile"
    ).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ledger_db():
    url = os.getenv("MIA_OS_LEDGER_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}
    assert parsed.database == "mia_ledger_test"
    assert parsed.username == "postgres"
    engine = create_engine(url)
    # This exact database is disposable. Reset it for the real fresh-worker proof.
    with engine.begin() as conn:
        assert 160000 <= int(conn.scalar(text("SHOW server_version_num"))) < 170000
        if conn.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='assaf_os_owner')"
        )):
            conn.execute(text(
                "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner "
                "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
            ))
        conn.execute(text("DROP SCHEMA IF EXISTS assaf_os CASCADE"))
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_normal_mia_migrate_creates_no_ledger_schema(ledger_db, monkeypatch, capsys):
    from app.db import session
    from app.workers import migrate

    assert not session.ledger_schema_ready(ledger_db)
    monkeypatch.setattr(session, "get_engine", lambda: ledger_db)
    monkeypatch.setattr(migrate, "get_engine", lambda: ledger_db)
    for _ in range(2):
        migrate.main()
        summary = json.loads(capsys.readouterr().out)
        assert summary["failed"] == ""
        assert not summary["skipped"]
        with ledger_db.connect() as conn:
            assert conn.scalar(text("SELECT to_regnamespace('assaf_os')")) is None
            assert conn.scalar(text("SELECT count(*) FROM public.schema_migrations")) > 0


@pytest.fixture
def migrated(ledger_db):
    from app.db.os_ledger_migrate import apply_ledger_migrations

    summary = apply_ledger_migrations(ledger_db)
    assert summary.failed == ""
    with ledger_db.connect() as conn, conn.begin():
        conn.execute(text(
            "INSERT INTO assaf_os.os_workspaces(workspace_id, owner_id, epoch, version) "
            "VALUES ('one', '12345', 1, 0), ('two', '12345', 1, 0)"
        ))
        yield conn
        conn.rollback()


def test_explicit_migration_is_repeatable_and_readiness_is_separate(migrated, ledger_db):
    from app.db.os_ledger_migrate import apply_ledger_migrations
    from app.db.session import ledger_schema_ready

    result = apply_ledger_migrations(ledger_db)
    assert result.applied == []
    assert result.already == [
        MIGRATION, "20261016_assaf_os_ledger_core_v2.sql", "20261016_assaf_os_statements.sql",
        "20261016_assaf_os_assistant.sql", "20261024_assaf_os_issue_requests.sql",
        "20261025_assaf_os_assistant_followups.sql", "20261026_assaf_os_oauth.sql",
    ]
    assert ledger_schema_ready(ledger_db)
    assert TABLES <= set(migrated.scalars(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='assaf_os'"
    )))
    assert not list(migrated.scalars(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'os_%'"
    )))
    required = {
        "os_authority": {"domain", "state", "epoch", "activated_at", "activated_by"},
        "os_proposals": {"kind", "source", "credential_id", "expires_at"},
        "os_receipts": {"kind", "commit_version", "undo_until", "undo_state", "undo_of"},
        "os_mirror_state": {"watermark", "verified_version", "last_verified_at", "scan_coverage"},
    }
    for entity in ("leads", "clients", "works", "payments", "time", "expenses", "tasks"):
        required["os_" + entity] = {"deleted_at", "deleted_by_request_id"}
    for table, columns in required.items():
        actual = set(migrated.scalars(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='assaf_os' AND table_name=:table"
        ), {"table": table}))
        assert columns <= actual, (table, columns - actual)


def record(conn, table, record_id="row", workspace="one", **extra):
    values = {
        "workspace_id": workspace, "record_id": record_id, "revision": 1,
        "inputs": "{}", "source": "OS", "created_at": "2026-10-15T00:00:00Z",
        "updated_at": "2026-10-15T00:00:00Z", **extra,
    }
    conn.execute(text(
        f"INSERT INTO assaf_os.os_{table} ({', '.join(values)}) "
        f"VALUES ({', '.join(':' + key for key in values)})"
    ), values)


@pytest.mark.parametrize("amount", [-1, 0, Decimal("1.5"), 9007199254740992])
def test_money_rejects_out_of_range_and_fractional_minor_units(migrated, amount):
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        record(migrated, "expenses", amount_minor=amount, source_type="actual",
               source_key="source", source_digest="a" * 64)


def test_valid_money_and_expense_source_unique_key(migrated):
    record(migrated, "expenses", amount_minor=1, source_type="actual",
           source_key="source", source_digest="a" * 64)
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        record(migrated, "expenses", record_id="duplicate", amount_minor=2,
               source_type="actual", source_key="source", source_digest="b" * 64)


def test_zero_work_agreement_matches_frozen_domain(migrated):
    from app.prototypes.assaf_os_domain import canonical_input

    assert canonical_input("works", {"agreed_amount_ils": 0}, creating=False) == {
        "agreed_amount_ils": 0,
    }
    record(migrated, "clients", record_id="client")
    record(migrated, "works", person_id="client", agreed_amount_minor=0)


@pytest.mark.parametrize("amount", [-1, Decimal("1.5"), 9007199254740992])
def test_work_agreement_rejects_invalid_minor_units(migrated, amount):
    record(migrated, "clients", record_id="client")
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        record(migrated, "works", person_id="client", agreed_amount_minor=amount)


def test_maximum_unicode_expense_source_key_preserves_uniqueness(migrated):
    from app.prototypes.assaf_os_domain import canonical_input

    rng = random.Random(14)
    key = "".join(chr(rng.randrange(0x4E00, 0x9FFF)) for _ in range(2000))
    assert canonical_input("expenses", {"source_key": key}, creating=False) == {
        "source_key": key,
    }
    record(migrated, "expenses", amount_minor=1, source_type="actual",
           source_key=key, source_digest="a" * 64)
    # Caller-controlled digest changes must not bypass the logical unique key.
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        record(migrated, "expenses", record_id="duplicate", amount_minor=1,
               source_type="actual", source_key=key, source_digest="b" * 64)
    record(migrated, "expenses", record_id="different-key", amount_minor=1,
           source_type="actual", source_key=key[:-1] + "a", source_digest="c" * 64)
    record(migrated, "expenses", record_id="different-type", amount_minor=1,
           source_type="other", source_key=key, source_digest="d" * 64)
    digest_query = text(
        "SELECT encode(assaf_os.expense_source_key_sha256(source_key), 'hex') "
        "FROM assaf_os.os_expenses WHERE workspace_id='one' AND record_id='row'"
    )
    expected_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert migrated.scalar(digest_query) == expected_digest
    migrated.execute(text("SET LOCAL client_encoding = 'LATIN1'"))
    assert migrated.scalar(digest_query) == expected_digest
    migrated.execute(text("SET LOCAL client_encoding = 'UTF8'"))


def test_cross_workspace_reference_and_bad_status_are_rejected(migrated):
    record(migrated, "clients", workspace="two")
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        record(migrated, "works", person_id="row")
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        record(migrated, "tasks", inputs=json.dumps({"status": "invalid"}))


def proposal(conn, *, source="MIA", kind="proposal", status="pending", **extra):
    values = {
        "workspace_id": "one", "proposal_id": uuid4().hex, "request_id": str(uuid4()),
        "request_digest": "a" * 64, "envelope_digest": "b" * 64, "owner_id": "12345",
        "epoch": 1, "approval_id": str(uuid4()), "rejection_id": str(uuid4()),
        "entity": "tasks", "record_id": "task", "expected_revision": 0,
        "mutations": "[]", "dependency_digest": "c" * 64, "source": source,
        "kind": kind, "status": status, "created_at": "2026-10-15T00:00:00Z",
        "expires_at": "2026-10-15T00:10:00Z", **extra,
    }
    conn.execute(text(
        f"INSERT INTO assaf_os.os_proposals ({', '.join(values)}) "
        f"VALUES ({', '.join(':' + key for key in values)})"
    ), values)
    return values["proposal_id"]


@pytest.mark.parametrize("changes", [
    {"source": "MIA", "kind": "owner_direct"},
    {"source": "MIA", "kind": "owner_undo"},
    {"source": "ASSISTANT_API", "kind": "proposal"},
    {"source": "ASSISTANT_PASTE", "kind": "statement_batch", "status": "committed"},
    {"status": "invalid"},
    {"expires_at": "2026-10-14T00:00:00Z"},
    {"expires_at": "2026-10-16T00:00:00Z"},
])
def test_proposal_guards(migrated, changes):
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        proposal(migrated, **changes)


def test_assistant_pending_at_insert_still_allows_terminal_update(migrated):
    proposal_id = proposal(migrated, source="ASSISTANT_API", kind="statement_batch")
    migrated.execute(text(
        "UPDATE assaf_os.os_proposals SET status='rejected' WHERE proposal_id=:id"
    ), {"id": proposal_id})
    proposal(migrated, source="OS_DIRECT", kind="owner_direct")


def receipt(conn, *, undo_of=None, version=1):
    proposal_id = proposal(conn)
    request_id = uuid4()
    conn.execute(text(
        "INSERT INTO assaf_os.os_receipts(workspace_id, request_id, proposal_id, epoch, "
        "kind, status, commit_version, completed_at, undo_of) VALUES "
        "('one', :request, :proposal, 1, :kind, 'committed', :version, now(), :undo_of)"
    ), {"request": request_id, "proposal": proposal_id, "kind": "owner_undo" if undo_of
        else "proposal", "version": version, "undo_of": undo_of})
    return request_id


def test_receipts_allow_only_one_undo_and_bind_workspace(migrated):
    first = receipt(migrated)
    receipt(migrated, undo_of=first, version=2)
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        receipt(migrated, undo_of=first, version=3)
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        migrated.execute(text(
            "UPDATE assaf_os.os_receipts SET workspace_id='two' WHERE request_id=:id"
        ), {"id": first})


def test_outbox_lease_shapes_and_unique_versions(migrated):
    decision = receipt(migrated)
    event = uuid4()
    migrated.execute(text(
        "INSERT INTO assaf_os.os_events(workspace_id,event_id,decision_id,commit_version,body) "
        "VALUES ('one',:event,:decision,1,'{}')"
    ), {"event": event, "decision": decision})
    migrated.execute(text(
        "INSERT INTO assaf_os.os_outbox(workspace_id,event_id,commit_version,body) "
        "VALUES ('one',:event,1,'{}')"
    ), {"event": event})
    for invalid in ("status='invalid'", "status='leased'", "attempts=-1", "lease_generation=-1"):
        with pytest.raises(SQLAlchemyError), migrated.begin_nested():
            migrated.execute(text("UPDATE assaf_os.os_outbox SET " + invalid))
    migrated.execute(text(
        "UPDATE assaf_os.os_outbox SET status='leased', lease_owner='worker', "
        "leased_at=now(), lease_generation=1, attempts=1"
    ))
    for status in ("unknown", "conflict", "pending", "sent"):
        migrated.execute(text(
            "UPDATE assaf_os.os_outbox SET status=:status, lease_owner=NULL, leased_at=NULL"
        ), {"status": status})
    another_decision = receipt(migrated, version=2)
    another_event = uuid4()
    migrated.execute(text(
        "INSERT INTO assaf_os.os_events(workspace_id,event_id,decision_id,commit_version,body) "
        "VALUES ('one',:event,:decision,2,'{}')"
    ), {"event": another_event, "decision": another_decision})
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        migrated.execute(text(
            "INSERT INTO assaf_os.os_outbox(workspace_id,event_id,commit_version,body) "
            "VALUES ('one',:event,1,'{}')"
        ), {"event": another_event})


def test_soft_delete_pairs_and_authority_activation_fields(migrated):
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        record(migrated, "clients", deleted_at="2026-10-15T00:00:00Z")
    for domain, state, epoch in (("invalid", "shadow", 1), ("core", "active", 1),
                                 ("core", "shadow", 0)):
        with pytest.raises(SQLAlchemyError), migrated.begin_nested():
            migrated.execute(text(
                "INSERT INTO assaf_os.os_authority(workspace_id,domain,state,epoch) "
                "VALUES ('one',:domain,:state,:epoch)"
            ), {"domain": domain, "state": state, "epoch": epoch})
    migrated.execute(text(
        "INSERT INTO assaf_os.os_authority(workspace_id,domain,state,epoch) "
        "VALUES ('one','core','shadow',1)"
    ))


def test_owner_role_and_public_privileges(migrated):
    owner = migrated.execute(text(
        "SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb, "
        "rolreplication, rolbypassrls FROM pg_roles WHERE rolname='assaf_os_owner'"
    )).one()
    assert owner == ("assaf_os_owner", False, False, False, False, False, False, False)
    assert migrated.scalar(text(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='assaf_os'"
    )) == "assaf_os_owner"
    assert not migrated.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_namespace n, "
        "LATERAL aclexplode(coalesce(n.nspacl, acldefault('n',n.nspowner))) a "
        "WHERE n.nspname='assaf_os' AND a.grantee=0)"
    ))
    assert not migrated.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace, "
        "LATERAL aclexplode(coalesce(p.proacl, acldefault('f',p.proowner))) a "
        "WHERE n.nspname='assaf_os' AND a.grantee=0)"
    ))


def test_non_postgres_is_refused_without_schema_changes():
    from app.db.os_ledger_migrate import apply_ledger_migrations

    engine = create_engine("sqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="postgres_required"):
            apply_ledger_migrations(engine)
    finally:
        engine.dispose()


def test_migration_fails_atomically_and_does_not_skip_duplicate_errors(ledger_db, monkeypatch):
    from app.db import os_ledger_migrate as ledger

    with ledger_db.begin() as conn:
        conn.execute(text("DROP SCHEMA assaf_os CASCADE"))
    scripts = ledger.migration_scripts()
    monkeypatch.setattr(ledger, "migration_scripts", lambda: [
        (scripts[0][0], scripts[0][1] + "\nSELECT missing_ledger_function();", scripts[0][2])
    ])
    with pytest.raises(SQLAlchemyError):
        ledger.apply_ledger_migrations(ledger_db)
    with ledger_db.connect() as conn:
        assert conn.scalar(text("SELECT to_regnamespace('assaf_os')")) is None
    monkeypatch.setattr(ledger, "migration_scripts", lambda: scripts)
    assert ledger.apply_ledger_migrations(ledger_db).applied == [
        MIGRATION, "20261016_assaf_os_ledger_core_v2.sql", "20261016_assaf_os_statements.sql",
        "20261016_assaf_os_assistant.sql", "20261024_assaf_os_issue_requests.sql",
        "20261025_assaf_os_assistant_followups.sql", "20261026_assaf_os_oauth.sql",
    ]


def test_unlisted_file_and_changed_applied_sql_are_refused(ledger_db, monkeypatch, tmp_path):
    from app.db import os_ledger_migrate as ledger

    scripts = ledger.migration_scripts()
    monkeypatch.setattr(ledger, "migration_scripts", lambda: [
        (scripts[0][0], scripts[0][1], "0" * 64)
    ])
    with pytest.raises(ValueError, match="digest_mismatch"):
        ledger.apply_ledger_migrations(ledger_db)
    monkeypatch.undo()
    (tmp_path / MIGRATION).write_text(scripts[0][1], encoding="utf-8", newline="\n")
    (tmp_path / "unexpected.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(ledger, "migration_directory", lambda: tmp_path)
    with pytest.raises(ValueError, match="not_allowlisted"):
        ledger.apply_ledger_migrations(ledger_db)


def test_existing_owner_os_roles_still_pass_after_ledger_migration(os_pg):
    from app.api.owner_sessions import _validate_runtime_role
    from app.db.os_ledger_migrate import apply_ledger_migrations
    from app.services.owner_os_action_policy import validate_action_role
    from app.services.owner_os_reads import _assert_select_only_role

    _, _, engine, urls = os_pg
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS assaf_os CASCADE"))
    apply_ledger_migrations(engine)
    validators = {
        "auth": _validate_runtime_role, "read": _assert_select_only_role,
        "action": validate_action_role,
    }
    for kind, validator in validators.items():
        restricted = create_engine(urls[kind])
        try:
            with Session(restricted) as db:
                validator(db)
                assert not db.scalar(text(
                    "SELECT has_schema_privilege(current_user,'assaf_os','USAGE,CREATE')"
                ))
            with restricted.connect() as conn, pytest.raises(SQLAlchemyError):
                conn.execute(text("SELECT * FROM assaf_os.os_workspaces"))
        finally:
            restricted.dispose()


def test_main_dsn_demote_gate_and_privilege_drift(ledger_db, monkeypatch, capsys):
    from app.db.os_ledger_migrate import apply_ledger_migrations, validate_main_role
    from app.workers import os_ledger

    apply_ledger_migrations(ledger_db)
    with pytest.raises(ValueError, match="not_separate"):
        validate_main_role(ledger_db, ledger_db)
    role = "ledger_main_" + uuid4().hex[:12]
    with ledger_db.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN NOINHERIT"))
    main = create_engine(ledger_db.url.set(username=role))
    try:
        validate_main_role(main, ledger_db)
        monkeypatch.setenv("MIA_BUILD_SHA", "a" * 40)
        monkeypatch.setenv("MIA_DATABASE_URL", main.url.render_as_string())
        monkeypatch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", ledger_db.url.render_as_string())
        os_ledger.main(["migrate", "--approved-sha", "a" * 40])
        assert json.loads(capsys.readouterr().out)["already"] == [
            MIGRATION, "20261016_assaf_os_ledger_core_v2.sql", "20261016_assaf_os_statements.sql",
            "20261016_assaf_os_assistant.sql", "20261024_assaf_os_issue_requests.sql",
            "20261025_assaf_os_assistant_followups.sql", "20261026_assaf_os_oauth.sql",
        ]
        with ledger_db.begin() as conn:
            conn.execute(text(f"GRANT USAGE ON SCHEMA assaf_os TO {role}"))
        with pytest.raises(ValueError, match="ledger_access_unsafe"):
            validate_main_role(main, ledger_db)
        with ledger_db.begin() as conn:
            conn.execute(text(f"REVOKE USAGE ON SCHEMA assaf_os FROM {role}"))
            conn.execute(text(f"ALTER ROLE {role} SUPERUSER"))
        with pytest.raises(ValueError, match="demote_required"):
            validate_main_role(main, ledger_db)
    finally:
        main.dispose()
        with ledger_db.begin() as conn:
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE {role}"))


def test_job_requires_exact_owner_go_before_any_database_call(monkeypatch, capsys):
    from app.workers import os_ledger

    def forbidden(*args, **kwargs):
        pytest.fail("database touched before owner go")

    monkeypatch.setattr(os_ledger, "create_engine", forbidden)
    monkeypatch.setenv("MIA_BUILD_SHA", "a" * 40)
    with pytest.raises(SystemExit) as result:
        os_ledger.main(["migrate", "--approved-sha", "b" * 40])
    assert result.value.code == 1
    assert "ledger_owner_go_sha_required" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        os_ledger.main(["activate", "--approved-sha", "a" * 40])


def test_explicit_job_uses_main_posture_gate_and_redacts_errors(monkeypatch, capsys):
    from app.workers import os_ledger

    engines = []

    def engine(url):
        value = create_engine("sqlite:///:memory:")
        engines.append(value)
        return value

    def refused(*args):
        raise ValueError("synthetic-sensitive-provider-payload")

    monkeypatch.setenv("MIA_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MIA_DATABASE_URL", "main-dsn")
    monkeypatch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", "privileged-dsn")
    monkeypatch.setattr(os_ledger, "create_engine", engine)
    monkeypatch.setattr(os_ledger, "validate_main_role", refused)
    monkeypatch.setattr(os_ledger, "apply_ledger_migrations", lambda _: pytest.fail("gate bypass"))
    with pytest.raises(SystemExit) as result:
        os_ledger.main(["migrate", "--approved-sha", "a" * 40])
    assert result.value.code == 1
    output = capsys.readouterr()
    assert output.err == "ledger_migration_refused\n"
    assert len(engines) == 2


def test_job_is_not_a_deploy_dependency_or_scheduled_job():
    compose = (ROOT / "ops/gcp/vm/compose.yaml").read_text(encoding="utf-8")
    script = (ROOT / "ops/gcp/vm/mia").read_text(encoding="utf-8")
    assert "profiles: [os-ledger]" in compose
    app_section = compose.split("\n  app:", 1)[1]
    assert "os-ledger" not in app_section
    assert "jobs=(mia-due-scan mia-ingest-knowledge)" in script
    assert 'compose up -d --remove-orphans postgres migrate app' in script
    assert '"$1" = migrate' in script
    assert '"$2" = --approved-sha' in script


def test_main_database_owner_is_not_a_demoted_role(ledger_db):
    from app.db.os_ledger_migrate import validate_main_role

    role = "ledger_dbowner_" + uuid4().hex[:12]
    with ledger_db.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN NOINHERIT"))
        conn.execute(text(f"ALTER DATABASE mia_ledger_test OWNER TO {role}"))
    main = create_engine(ledger_db.url.set(username=role))
    try:
        with pytest.raises(ValueError, match="database_owner_unsafe"):
            validate_main_role(main, ledger_db)
    finally:
        main.dispose()
        with ledger_db.begin() as conn:
            conn.execute(text("ALTER DATABASE mia_ledger_test OWNER TO postgres"))
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE {role}"))


def test_authority_supports_restoring(migrated):
    migrated.execute(text(
        "INSERT INTO assaf_os.os_authority(workspace_id,domain,state,epoch) "
        "VALUES ('one','core','restoring',1)"
    ))


def test_default_privilege_leak_rolls_back_before_commit(ledger_db, monkeypatch, capsys):
    from app.db.os_ledger_migrate import apply_ledger_migrations
    from app.workers import os_ledger

    apply_ledger_migrations(ledger_db)
    role = "ledger_defaults_" + uuid4().hex[:12]
    with ledger_db.begin() as conn:
        conn.execute(text("DROP SCHEMA assaf_os CASCADE"))
        conn.execute(text(f"CREATE ROLE {role} LOGIN NOINHERIT"))
        conn.execute(text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner GRANT SELECT ON TABLES TO {role}"
        ))
        conn.execute(text(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres GRANT USAGE ON SCHEMAS TO {role}"
        ))
    monkeypatch.setenv("MIA_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MIA_DATABASE_URL", ledger_db.url.set(username=role).render_as_string())
    monkeypatch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", ledger_db.url.render_as_string())
    try:
        with pytest.raises(SystemExit) as result:
            os_ledger.main(["migrate", "--approved-sha", "a" * 40])
        assert result.value.code == 1
        assert capsys.readouterr().err == (
            "ledger_migration_refused:app_role_ledger_access_unsafe\n"
        )
        with ledger_db.connect() as conn:
            assert conn.scalar(text("SELECT to_regnamespace('assaf_os')")) is None
    finally:
        with ledger_db.begin() as conn:
            conn.execute(text(
                "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner "
                f"REVOKE SELECT ON TABLES FROM {role}"
            ))
            conn.execute(text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres REVOKE USAGE ON SCHEMAS FROM {role}"
            ))
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE {role}"))


def test_entrypoint_reports_allowlisted_refusal(ledger_db, monkeypatch, capsys):
    from app.workers import os_ledger

    monkeypatch.setenv("MIA_BUILD_SHA", "a" * 40)
    for name in ("MIA_DATABASE_URL", "MIA_OS_LEDGER_MIGRATION_DATABASE_URL"):
        monkeypatch.setenv(name, ledger_db.url.render_as_string())
    with pytest.raises(SystemExit) as result:
        os_ledger.main(["migrate", "--approved-sha", "a" * 40])
    assert result.value.code == 1
    assert capsys.readouterr().err == "ledger_migration_refused:ledger_main_dsn_not_separate\n"


@pytest.mark.parametrize("grant", [
    "", "WITH ADMIN TRUE, SET FALSE, INHERIT FALSE",
    "WITH ADMIN FALSE, SET FALSE, INHERIT FALSE",
])
def test_owner_granted_to_another_login_is_refused(ledger_db, grant):
    from app.db.os_ledger_migrate import apply_ledger_migrations

    apply_ledger_migrations(ledger_db)
    role = "ledger_member_" + uuid4().hex[:12]
    with ledger_db.begin() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN NOINHERIT"))
        conn.execute(text(f"GRANT assaf_os_owner TO {role} {grant}"))
    try:
        with pytest.raises(ValueError, match="^ledger_owner_membership_unsafe$"):
            apply_ledger_migrations(ledger_db)
    finally:
        with ledger_db.begin() as conn:
            conn.execute(text(f"REVOKE assaf_os_owner FROM {role}"))
            conn.execute(text(f"DROP ROLE {role}"))


def test_owner_admin_only_grant_to_migration_session_is_allowed(ledger_db):
    from app.db.os_ledger_migrate import apply_ledger_migrations

    apply_ledger_migrations(ledger_db)
    with ledger_db.begin() as conn:
        assert not conn.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM pg_auth_members "
            "WHERE roleid='assaf_os_owner'::regrole AND member=session_user::regrole)"
        ))
        conn.execute(text(
            "GRANT assaf_os_owner TO postgres WITH ADMIN TRUE, SET FALSE, INHERIT FALSE"
        ))
    try:
        assert apply_ledger_migrations(ledger_db).already == [
            MIGRATION, "20261016_assaf_os_ledger_core_v2.sql",
            "20261016_assaf_os_statements.sql", "20261016_assaf_os_assistant.sql",
            "20261024_assaf_os_issue_requests.sql", "20261025_assaf_os_assistant_followups.sql",
            "20261026_assaf_os_oauth.sql",
        ]
    finally:
        with ledger_db.begin() as conn:
            conn.execute(text("REVOKE assaf_os_owner FROM postgres"))


def test_future_owner_functions_have_no_public_execute(ledger_db):
    from app.db.os_ledger_migrate import apply_ledger_migrations

    apply_ledger_migrations(ledger_db)
    with ledger_db.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE assaf_os_owner"))
        conn.execute(text(
            "CREATE FUNCTION assaf_os.r1_private_function() RETURNS integer "
            "LANGUAGE sql AS 'SELECT 1'"
        ))
        assert not conn.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM pg_proc p, "
            "LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a "
            "WHERE p.oid='assaf_os.r1_private_function()'::regprocedure "
            "AND a.grantee=0 AND a.privilege_type='EXECUTE')"
        ))
        conn.rollback()


@pytest.mark.parametrize("source", ["assistant_api", "Assistant_API", "ASSISTANT api", "A", "_OS"])
def test_proposal_source_cannot_bypass_assistant_guards(migrated, source):
    with pytest.raises(SQLAlchemyError), migrated.begin_nested():
        proposal(migrated, source=source, kind="proposal", status="committed")
