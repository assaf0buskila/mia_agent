"""Provisioning safety boundaries; all credentials and provider calls are synthetic."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def provision(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "ops/assaf-os/provision_auth.py"
    spec = importlib.util.spec_from_file_location("assaf_os_provision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = {
        "MIA_ENV": "prod", "MIA_PUBLIC_BASE_URL": "https://mia.example.test",
        "MIA_TELEGRAM_BOT_TOKEN": "synthetic-existing-bot",
        "MIA_TELEGRAM_OWNER_USER_IDS": "123456",
        "MIA_UNRELATED_SETTING": "retain-this-value",
    }
    monkeypatch.setattr(module, "settings", lambda: dict(baseline))
    monkeypatch.setattr(module, "verify_vm", lambda: None)
    monkeypatch.setattr(module, "owner_subject", lambda: "12345678901234567890")
    return module, baseline


def test_preflight_never_publishes_or_creates(provision, monkeypatch):
    module, _ = provision
    queries = []
    monkeypatch.setattr(module, "postgres", lambda sql, database="postgres":
                        queries.append((sql, database)) or "")
    monkeypatch.setattr(module, "command", lambda *a, **k: pytest.fail("unexpected mutation"))
    result = module.provision()
    assert result["status"] == "preflight_ready"
    assert result["new_server"] is False and result["features_enabled"] is False
    assert len(queries) == 1 and queries[0][0].startswith("SELECT")


def test_baseline_drift_aborts_before_publication(provision, monkeypatch):
    module, baseline = provision
    reads = iter([dict(baseline), {**baseline, "MIA_UNRELATED_SETTING": "owner-new-value"}])
    monkeypatch.setattr(module, "settings", lambda: next(reads))
    monkeypatch.setattr(module, "postgres", lambda sql, database="postgres": "")
    monkeypatch.setattr(module, "command", lambda *a, **k: pytest.fail("must not publish"))
    with pytest.raises(RuntimeError, match="settings_baseline_changed"):
        module.provision(True)


def test_existing_configuration_never_rotates(provision, monkeypatch):
    module, baseline = provision
    monkeypatch.setattr(module, "settings", lambda:
                        {**baseline, "MIA_ASSAF_OS_HMAC_SECRET": "synthetic-existing"})
    monkeypatch.setattr(module, "postgres", lambda *a, **k: pytest.fail("target must not mutate"))
    with pytest.raises(RuntimeError, match="already_configured_do_not_rotate"):
        module.provision(True)


def test_unknown_database_is_rejected_before_ssh(provision, monkeypatch):
    module, _ = provision
    monkeypatch.setattr(module, "command", lambda *a, **k: pytest.fail("no outbound call"))
    with pytest.raises(RuntimeError, match="database_denied"):
        module.postgres("SELECT 1", "mia")


def test_actual_posture_queries_reject_broad_role(provision, monkeypatch):
    module, _ = provision
    observed = []
    monkeypatch.setattr(module, "postgres", lambda sql, database="postgres":
                        observed.append((sql, database)) or "t|f|f|f|f|f\nt|t|t\nt|t|t\n")
    with pytest.raises(RuntimeError, match="auth_role_posture_failed"):
        module.verify_role_posture()
    assert observed[0][1] == "assaf_os_auth"
    assert "SET LOCAL ROLE assaf_os_session" in observed[0][0]
    assert "has_column_privilege" in observed[0][0]
    assert "pg_auth_members" in observed[0][0]


# V2-08 erratum: the live owner_os_sessions table is in assaf_os_auth, which mia-migrate never
# reaches. The session-chain columns get there only through this explicit, guarded step.
CHAINS = Path(__file__).resolve().parents[2] / "migrations/20261015_owner_os_session_chains.sql"
CHAIN_COLUMNS = ("chain_id", "chain_started_at", "authenticated_at", "first_used_at",
                 "rotated_at", "grace_until", "successor_hash")


def _fake_auth_db(calls, verified="7\n2\n"):
    def fake(sql, database="postgres"):
        calls.append((sql, database))
        return verified if "information_schema.columns" in sql else ""
    return fake


def test_session_chain_migration_dry_run_is_a_no_op(provision, monkeypatch):
    module, _ = provision
    monkeypatch.setattr(module, "postgres", lambda *a, **k: pytest.fail("dry run must not query"))
    monkeypatch.setattr(module, "command", lambda *a, **k: pytest.fail("no outbound call"))
    monkeypatch.setattr(module, "verify_role_posture", lambda: pytest.fail("dry run only"))
    result = module.migrate_session_chains()
    assert result["status"] == "session_chains_dry_run"
    assert result["database"] == "assaf_os_auth" and result["grants_changed"] is False
    assert result["migration"] == CHAINS.name


def test_session_chain_migration_targets_only_the_auth_database_and_is_idempotent(
    provision, monkeypatch
):
    from app.db.migrate import _split_statements

    module, _ = provision
    calls, posture = [], []
    monkeypatch.setattr(module, "postgres", _fake_auth_db(calls))
    monkeypatch.setattr(module, "command", lambda *a, **k: pytest.fail("no other command"))
    monkeypatch.setattr(module, "verify_role_posture", lambda: posture.append("checked"))
    first = module.migrate_session_chains(True)
    second = module.migrate_session_chains(True)
    assert first == second and first["status"] == "session_chains_applied"
    assert first["role_posture"] == "pass" and first["grants_changed"] is False
    assert posture == ["checked", "checked"]
    assert {database for _, database in calls} == {"assaf_os_auth"}
    applied = [sql for sql, _ in calls if "ADD COLUMN" in sql]
    assert len(applied) == 2 and applied[0] == applied[1] == module.session_chains_sql()
    sql = applied[0]
    assert sql.startswith("BEGIN;\n") and sql.endswith("COMMIT;\n")
    for statement in _split_statements(CHAINS.read_text(encoding="utf-8")):
        assert statement in sql
    for column in CHAIN_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column} " in sql
    # Each ADD CONSTRAINT runs only when the constraint is absent, so a re-run is a no-op.
    assert sql.count("IF NOT EXISTS (SELECT 1 FROM pg_constraint") == 2
    for name in ("ck_owner_os_sessions_chain", "ck_owner_os_sessions_rotation"):
        assert f"conname = '{name}'" in sql
    assert "GRANT " not in sql and "REVOKE " not in sql and "DROP " not in sql


def test_session_chain_migration_fails_closed_on_verification(provision, monkeypatch):
    module, _ = provision
    calls = []
    monkeypatch.setattr(module, "postgres", _fake_auth_db(calls, verified="6\n2\n"))
    monkeypatch.setattr(module, "verify_role_posture", lambda: pytest.fail("verify first"))
    with pytest.raises(RuntimeError, match="session_chains_verification_failed"):
        module.migrate_session_chains(True)


def test_session_chain_migration_refuses_a_changed_file(provision, monkeypatch, tmp_path):
    module, _ = provision
    changed = tmp_path / CHAINS.name
    changed.write_text(CHAINS.read_text(encoding="utf-8") + "\nDROP TABLE owner_os_sessions;\n",
                       encoding="utf-8")
    monkeypatch.setattr(module, "SESSION_CHAINS_SQL", changed)
    monkeypatch.setattr(module, "postgres", lambda *a, **k: pytest.fail("must not apply"))
    with pytest.raises(RuntimeError, match="session_chains_migration_changed"):
        module.migrate_session_chains(True)


def test_session_chain_sql_applies_twice_on_disposable_postgres(provision):
    import os
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    import psycopg
    from app.db.migrate import _apply_file
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    module, _ = provision
    url = os.getenv("MIA_OWNER_OS_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OWNER_OS_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1", "::1"}
    assert parsed.database == "owner_os_test"
    schema = "os_provision_" + uuid4().hex[:16]
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as conn:
            _apply_file(conn, CHAINS.parent / "20261011_owner_os_sessions.sql")
            now = datetime.now(UTC)
            conn.execute(
                text(
                    "INSERT INTO owner_os_sessions (session_hash, owner_id, workspace_id,"
                    " login_proof_hash, scopes, expires_at, revoked_at, created_at)"
                    " VALUES ('legacy', '123456', 'assaf-os', :p, 'assaf-os:read', :e, NULL, :c)"
                ),
                {"p": "a" * 64, "e": now + timedelta(minutes=20), "c": now},
            )
        with psycopg.connect(
            host=parsed.host, port=parsed.port or 5432, user=parsed.username,
            dbname=parsed.database, options=f"-csearch_path={schema}", autocommit=True,
        ) as conn:
            # The legacy row (chain_id NULL) must not block either CHECK constraint.
            for _ in range(2):
                conn.execute(module.session_chains_sql())
            columns, constraints = module.session_chains_verification_queries()
            counts = [conn.execute(columns).fetchone()[0], conn.execute(constraints).fetchone()[0]]
            legacy = conn.execute(
                "SELECT chain_id, rotated_at FROM owner_os_sessions WHERE session_hash = 'legacy'"
            ).fetchone()
        assert counts == [7, 2]
        assert legacy == (None, None)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
