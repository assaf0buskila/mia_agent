"""V2-37 on PostgreSQL only: the grants and the CHECKs the limiter and the keys rely on (D1, §4,
§18.2). This module is collected only when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set
(``conftest.collect_ignore``), so a run without the URL has no skipped case, and a run with it
runs every case here.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.os_ledger_migrate import RUNTIME_ROLE
from app.services.os_ledger.api import LedgerError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.services.os_ledger.assistant.support import IP_A


def privilege(aw, sql: str) -> bool:
    with aw.w._admin_engine.connect() as conn:
        return bool(conn.execute(text(sql)).scalar())


def test_the_runtime_role_has_exactly_the_audit_and_key_privileges_d1_needs(pg_aw):
    aw = pg_aw
    audit = "assaf_os.os_assistant_audit"
    keys = "assaf_os.os_assistant_credentials"
    role = RUNTIME_ROLE

    def has(table: str, what: str) -> bool:
        return privilege(aw, f"SELECT has_table_privilege('{role}', '{table}', '{what}')")

    assert has(audit, "SELECT") and has(audit, "INSERT")
    assert not has(audit, "UPDATE") and not has(audit, "DELETE")
    assert has(keys, "SELECT") and has(keys, "INSERT")
    assert not has(keys, "DELETE") and not has(keys, "UPDATE")  # no table-wide UPDATE
    for column, allowed in (("revoked_at", True), ("last_used_at", True), ("use_count", True),
                            ("secret_hash", False), ("scopes", False), ("expires_at", False),
                            ("owner_id", False), ("label", False)):
        assert privilege(aw, f"SELECT has_column_privilege('{role}', '{keys}', '{column}', "
                             "'UPDATE')") is allowed, column


def test_the_limiter_connection_cannot_rewrite_or_delete_an_audit_row(pg_aw):
    aw = pg_aw
    issued = aw.issue()
    caller = aw.authenticate(issued["secret"])
    aw.gateway.run(caller, "context", lambda facts: {})
    for statement in ("UPDATE assaf_os.os_assistant_audit SET ok = false",
                      "DELETE FROM assaf_os.os_assistant_audit",
                      "UPDATE assaf_os.os_assistant_credentials SET secret_hash = repeat('0', 64)",
                      "UPDATE assaf_os.os_assistant_credentials SET scopes = 'x'"):
        with pytest.raises(LedgerError) as caught:
            with aw.store._inner._begin() as conn:
                conn.execute(text(statement))
        # SQLSTATE 42501, insufficient_privilege, mapped by the ledger's database_error
        assert (caught.value.code, caught.value.reason) == (
            "ledger_unavailable", "db_error_42501"), statement
    assert len(aw.audit()) == 1 and aw.credential(issued["credential_id"])["scopes"].startswith(
        "merchant_rules:read")


def test_the_credential_row_check_pins_the_three_scopes(pg_aw):
    aw = pg_aw
    issued = aw.issue()
    with aw.w._admin_engine.begin() as conn:
        with pytest.raises(DBAPIError) as caught:
            conn.execute(text(
                "UPDATE assaf_os.os_assistant_credentials SET scopes = 'statement:propose "
                "decide:approve' WHERE credential_id = :id"), {"id": issued["credential_id"]})
    assert "os_assistant_credentials_scopes_check" in str(caught.value.orig)


def test_the_expiry_check_refuses_more_than_ninety_days(pg_aw):
    aw = pg_aw
    issued = aw.issue(days=90)
    row = aw.credential(issued["credential_id"])
    with aw.w._admin_engine.begin() as conn:
        with pytest.raises(DBAPIError) as caught:
            conn.execute(text(
                "UPDATE assaf_os.os_assistant_credentials SET expires_at = created_at + "
                "interval '91 days' WHERE credential_id = :id"), {"id": row["credential_id"]})
    assert "expiry_max_check" in str(caught.value.orig)


def test_an_audit_row_may_name_an_unknown_credential_and_never_a_bad_address_hash(pg_aw):
    aw = pg_aw
    aw.seed_audit(1, ip_hmac=aw.ip_hmac(IP_A), credential_id="AC_" + "9" * 32)  # no foreign key
    with aw.w._admin_engine.begin() as conn:
        with pytest.raises(DBAPIError) as caught:
            conn.execute(text(
                "INSERT INTO assaf_os.os_assistant_audit (workspace_id, audit_id, occurred_at, "
                "operation, reason, ok, client_ip_hmac) VALUES ('test', :id, now(), 'bearer', "
                "'x', false, '203.0.113.10')"), {"id": str(uuid.uuid4())})
    assert "client_ip_hmac_check" in str(caught.value.orig)


# ---------------------------------------------------------------------------------------------
# review r1 MAJOR-2 over HTTP, with a real database failure in the accounting transaction


@pytest.fixture
def pg_client(pg_aw, monkeypatch):
    from app.api.assistant import os as assistant_os
    from app.core.config import get_settings
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(assistant_os, "gateway_for", lambda settings: pg_aw.gateway)
    app.dependency_overrides[get_settings] = lambda: pg_aw.config
    try:
        yield TestClient(app, base_url="https://api.assafweb.test", client=("203.0.113.5", 50000))
    finally:
        app.dependency_overrides.pop(get_settings, None)


def post_batch(client, secret):
    from tests.services.os_ledger.statements.support import batch, encode, three_lines

    return client.post("/v1/assistant/os/statement-batches",
                       content=encode(batch(three_lines())),
                       headers={"Authorization": f"Bearer {secret}",
                                "Content-Type": "application/json"})


def test_a_lock_timeout_in_the_usage_update_after_the_submit_is_still_a_200_with_the_batch(
        pg_aw, pg_client, caplog):
    caplog.set_level("INFO")
    issued = pg_aw.issue()
    with pg_aw.accounting_fault("record", issued["credential_id"]):  # a held row lock: 3 s timeout
        response = post_batch(pg_client, issued["secret"])
    assert response.status_code == 200, response.text
    body = response.json()
    [proposal] = pg_aw.proposals()
    assert proposal["proposal_id"] == body["proposal_id"] and proposal["status"] == "pending"
    assert "not_started" not in response.text
    assert "os_assistant op=audit outcome=failed reason=audit_write_failed" in caplog.text
    assert pg_aw.audit() == [] and pg_aw.credential(issued["credential_id"])["use_count"] == 0


def test_a_lock_timeout_on_admission_is_a_503_not_started_and_no_batch_exists(pg_aw, pg_client):
    issued = pg_aw.issue()
    with pg_aw.accounting_fault("admit", issued["credential_id"]):  # the key lock is held
        response = post_batch(pg_client, issued["secret"])
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert (error["code"], error["write_outcome"]) == ("ledger_unavailable", "not_started")
    assert pg_aw.proposals() == []
