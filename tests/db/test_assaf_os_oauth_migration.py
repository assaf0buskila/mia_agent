"""V2-38b OAuth ledger schema, exercised only on the named disposable PostgreSQL."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from app.db import os_ledger_migrate as ledger
from app.services.os_ledger import role_policy
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

MIGRATION = "20261026_assaf_os_oauth.sql"
TABLE = "os_assistant_oauth_requests"
CLIENT_ID = "https://chatgpt.com/oauth/client.json"
REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
SCOPE = "assaf-os:assistant"


def test_oauth_migration_and_role_maps_are_allowlisted_exactly():
    assert ledger.MIGRATIONS[-1] == MIGRATION
    assert TABLE in ledger.TABLES
    assert role_policy.ADDENDUM_COLUMN_UPDATES[TABLE] == (
        "status",
        "chain_binding",
        "approved_at",
        "code_hash",
        "code_expires_at",
        "code_used_at",
        "credential_id",
    )
    assert TABLE not in role_policy.RETENTION_TABLE_PRIVILEGES
    assert TABLE not in role_policy.RETENTION_COLUMN_SELECTS
    statements = role_policy.ledger_grant_statements()
    assert (
        "GRANT INSERT, SELECT ON TABLE assaf_os.os_assistant_oauth_requests "
        "TO assaf_os_ledger;"
    ) in statements
    assert (
        "GRANT UPDATE (status, chain_binding, approved_at, code_hash, code_expires_at, "
        "code_used_at, credential_id) ON TABLE assaf_os.os_assistant_oauth_requests "
        "TO assaf_os_ledger;"
    ) in statements
    assert not any(
        TABLE in statement and ("DELETE" in statement or "TRUNCATE" in statement)
        for statement in statements
    )


@pytest.fixture(scope="module")
def oauth_db():
    url = os.getenv("MIA_OS_LEDGER_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}
    assert parsed.port in {5432, 56264}
    assert parsed.database == "mia_ledger_test"
    assert parsed.username == "postgres"
    engine = create_engine(url)
    with engine.begin() as conn:
        if conn.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='assaf_os_owner')"
        )):
            conn.execute(text(
                "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner "
                "GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
            ))
        conn.execute(text("DROP SCHEMA IF EXISTS assaf_os CASCADE"))
    summary = ledger.apply_ledger_migrations(engine)
    assert summary.failed == ""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO assaf_os.os_workspaces(workspace_id, owner_id, epoch, version) "
            "VALUES ('oauth-test', '12345', 1, 0) ON CONFLICT (workspace_id) DO NOTHING"
        ))
    yield engine
    engine.dispose()


def test_oauth_columns_and_code_lookup_index_are_exact(oauth_db):
    with oauth_db.connect() as conn:
        columns = conn.execute(text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema='assaf_os' "
            "AND table_name=:table ORDER BY ordinal_position"
        ), {"table": TABLE}).all()
        indexes = set(conn.scalars(text(
            "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i "
            "JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "WHERE n.nspname='assaf_os' AND t.relname=:table AND NOT i.indisprimary"
        ), {"table": TABLE}))
        audience = conn.execute(text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema='assaf_os' AND table_name='os_assistant_credentials' "
            "AND column_name='audience'"
        )).one()

    assert [row[0] for row in columns] == [
        "workspace_id", "oauth_request_id", "owner_id", "client_id", "redirect_uri",
        "code_challenge", "scope", "resource", "state", "epoch", "status",
        "chain_binding", "created_at", "deadline", "approved_at", "code_hash",
        "code_expires_at", "code_used_at", "credential_id",
    ]
    assert all(row[2] == "NO" for row in columns[:11])
    assert columns[11][2] == "YES"
    assert columns[12][2:] == ("NO", "now()")
    assert all(row[2] == "YES" for row in columns[14:])
    assert audience == ("text", "YES", None)
    assert indexes == {
        "CREATE UNIQUE INDEX os_assistant_oauth_code_hash_key ON "
        "assaf_os.os_assistant_oauth_requests USING btree (workspace_id, code_hash) "
        "WHERE (code_hash IS NOT NULL)"
    }


def _request_values(**changes):
    values = {
        "workspace_id": "oauth-test",
        "oauth_request_id": "OR_" + uuid4().hex,
        "owner_id": "12345",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": "A" * 42 + "_",
        "scope": SCOPE,
        "resource": "https://test.example/v1/assistant/mcp",
        "state": "opaque state ~ ?x=1&y=2",
        "epoch": 1,
        "status": "pending_owner",
        "created_at": "2026-10-26T00:00:00Z",
        "deadline": "2026-10-26T00:10:00Z",
    }
    values.update(changes)
    return values


INSERT = """
INSERT INTO assaf_os.os_assistant_oauth_requests (
    workspace_id, oauth_request_id, owner_id, client_id, redirect_uri, code_challenge,
    scope, resource, state, epoch, status, created_at, deadline
) VALUES (
    :workspace_id, :oauth_request_id, :owner_id, :client_id, :redirect_uri, :code_challenge,
    :scope, :resource, :state, :epoch, :status, :created_at, :deadline
)
"""


def test_oauth_request_preserves_raw_printable_state_and_allows_configured_resource(oauth_db):
    values = _request_values()
    with oauth_db.begin() as conn:
        conn.execute(text(INSERT), values)
        row = conn.execute(text(
            "SELECT state, resource, client_id, redirect_uri, status FROM "
            "assaf_os.os_assistant_oauth_requests WHERE workspace_id=:workspace_id "
            "AND oauth_request_id=:oauth_request_id"
        ), values).one()
    assert tuple(row) == (
        values["state"], values["resource"], CLIENT_ID, REDIRECT_URI, "pending_owner",
    )


@pytest.mark.parametrize(("change", "constraint"), [
    ({"oauth_request_id": "OR_bad"}, "os_oauth_requests_id_check"),
    ({"owner_id": "owner"}, "os_oauth_requests_owner_id_check"),
    ({"client_id": "https://evil.example/client"}, "os_oauth_requests_client_check"),
    ({"redirect_uri": "https://evil.example/callback"}, "os_oauth_requests_redirect_check"),
    ({"code_challenge": "A" * 42 + "+"}, "os_oauth_requests_challenge_check"),
    ({"scope": "other"}, "os_oauth_requests_scope_check"),
    ({"resource": ""}, "os_oauth_requests_resource_check"),
    ({"state": "line\nbreak"}, "os_oauth_requests_state_check"),
    ({"state": "x" * 513}, "os_oauth_requests_state_check"),
    ({"epoch": 0}, "os_oauth_requests_epoch_check"),
    ({"status": "minted"}, "os_oauth_requests_status_check"),
    ({"deadline": "2026-10-26T00:09:59Z"}, "os_oauth_requests_deadline_check"),
])
def test_oauth_request_checks_reject_untrusted_shapes(oauth_db, change, constraint):
    with oauth_db.begin() as conn, pytest.raises(IntegrityError, match=constraint):
        conn.execute(text(INSERT), _request_values(**change))


def test_oauth_nullable_hashes_are_shape_checked_without_blocking_cancelled_evidence(oauth_db):
    values = _request_values(
        status="cancelled",
        chain_binding="a" * 64,
        code_hash="b" * 64,
        code_expires_at="2026-10-26T00:11:00Z",
        code_used_at="2026-10-26T00:12:00Z",
    )
    extended = INSERT.replace(
        "status, created_at, deadline",
        "status, created_at, deadline, chain_binding, code_hash, code_expires_at, code_used_at",
    ).replace(
        ":status, :created_at, :deadline",
        ":status, :created_at, :deadline, :chain_binding, :code_hash, :code_expires_at, "
        ":code_used_at",
    )
    with oauth_db.begin() as conn:
        conn.execute(text(extended), values)
        with pytest.raises(IntegrityError, match="os_oauth_requests_code_hash_check"):
            with conn.begin_nested():
                conn.execute(text(extended), {
                    **values,
                    "oauth_request_id": "OR_" + uuid4().hex,
                    "code_hash": "not-a-hash",
                })
        with pytest.raises(IntegrityError, match="os_oauth_requests_chain_binding_check"):
            with conn.begin_nested():
                conn.execute(text(extended), {
                    **values,
                    "oauth_request_id": "OR_" + uuid4().hex,
                    "chain_binding": "not-a-binding",
                })
