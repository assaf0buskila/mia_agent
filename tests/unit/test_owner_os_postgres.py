"""Opt-in end-to-end proof using only a disposable, loopback PostgreSQL database."""

import hashlib
import hmac
import json
import os
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.migrate import _split_statements
from app.db.models import CrmContactRow, CrmIdentityRow
from app.db.reminders import OwnerReminderRow
from app.services.owner_os_action_policy import (
    ACTION_SEQUENCE_PRIVILEGES,
    ACTION_TABLE_PRIVILEGES,
)
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

ORIGIN = "https://os.example.test"
OWNER = "12345"
OTHER = "67890"
TOKEN = "synthetic-telegram-local-test-only"
AUTH = "/v1/owner/auth"
OS_API = "/v1/owner/os"


def proof(owner=OWNER, **changes):
    data = {"id": owner, "first_name": uuid4().hex, "auth_date": int(time.time())}
    data.update(changes)
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(),
                            hashlib.sha256).hexdigest()
    return json.dumps(data)


@pytest.fixture
def os_pg():
    url = os.getenv("MIA_OWNER_OS_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OWNER_OS_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"localhost", "127.0.0.1", "::1"}
    assert parsed.database == "owner_os_test"
    admin = create_engine(url)
    suffix = uuid4().hex[:16]
    schema = "os_" + suffix
    roles = {kind: f"os_{kind}_{suffix}" for kind in ("auth", "read", "action")}
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
        c.execute(text('REVOKE TEMPORARY, CREATE ON DATABASE owner_os_test FROM PUBLIC'))
        for role in roles.values():
            c.execute(text(f"CREATE ROLE {role} LOGIN NOINHERIT PASSWORD 'synthetic_local'"))
            c.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {role}'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    migration = Path(__file__).resolve().parents[2] / "migrations/20261011_owner_os_sessions.sql"
    with engine.begin() as c:
        for _ in range(2):
            for sql in _split_statements(migration.read_text(encoding="utf-8")):
                c.execute(text(sql))
        grants = {
            "owner_os_login_challenges": "SELECT, INSERT, UPDATE",
            "owner_os_login_proofs": "INSERT",
            "owner_os_sessions": "SELECT, INSERT, UPDATE",
            "owner_os_revocations": "SELECT, INSERT, UPDATE",
            "owner_os_auth_attempts": "SELECT, INSERT",
        }
        for table, rights in grants.items():
            c.execute(text(f"GRANT {rights} ON {table} TO {roles['auth']}"))
        c.execute(text("GRANT USAGE, SELECT ON owner_os_auth_attempts_id_seq "
                       f"TO {roles['auth']}"))
        for table in ("crm_contacts", "crm_activities", "owner_reminders",
                      "idempotency_records", "approvals"):
            c.execute(text(f"GRANT SELECT ON {table} TO {roles['read']}"))
        for table, rights in ACTION_TABLE_PRIVILEGES.items():
            c.execute(text(f"GRANT {', '.join(rights)} ON {table} TO {roles['action']}"))
        for seq, rights in ACTION_SEQUENCE_PRIVILEGES.items():
            c.execute(text(f"GRANT {', '.join(rights)} ON {seq} TO {roles['action']}"))
        now = datetime.now(UTC).isoformat()
        c.execute(CrmContactRow.__table__.insert().values(
            id="seed_contact", revision=1,
            fields_json=json.dumps({"name": "לקוח בדיקה", "email": "synthetic@example.test",
                                    "next_step": "בדיקת דרישות", "date": "2026-10-11",
                                    "status": "won", "source": "other"}),
            source_ref="synthetic-local", created_at=now, updated_at=now,
        ))
    urls = {k: parsed.set(username=v, password="synthetic_local",
                         query={"options": f"-csearch_path={schema}"}).render_as_string(
                             hide_password=False) for k, v in roles.items()}
    settings = Settings(
        _env_file=None, env="test", owner_os_enabled=True, owner_os_actions_enabled=True,
        owner_os_origin=ORIGIN, owner_os_workspace_id="assafweb",
        owner_os_session_database_url=urls["auth"], owner_os_read_database_url=urls["read"],
        owner_os_action_database_url=urls["action"], owner_os_cursor_secret="x" * 48,
        owner_os_session_scopes=("owner-os:read:contacts,owner-os:read:activities,"
                                 "owner-os:read:reminders,quick_leads.create,reminders.write"),
        telegram_bot_token=TOKEN, telegram_owner_user_ids=f"{OWNER},{OTHER}",
        cors_origins=ORIGIN, database_url="sqlite:///:memory:", reminders_enabled=False,
        reminder_delivery_enabled=False, crm_delivery_enabled=False,
    )
    from app.main import app
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    try:
        yield client, settings, engine, urls
    finally:
        client.close()
        app.dependency_overrides.clear()
        from app.api.owner_os_actions import _action_engine
        from app.api.owner_sessions import _session_factory
        _session_factory(urls["auth"])[0].dispose()
        _action_engine(urls["action"]).dispose()
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            for role in roles.values():
                c.execute(text(f"DROP OWNED BY {role}"))
                c.execute(text(f"DROP ROLE {role}"))
        admin.dispose()


def login(client, login_proof=None):
    challenge = client.get(AUTH + "/challenge")
    assert challenge.status_code == 200, challenge.text
    result = client.post(AUTH + "/login", json={"login_proof": login_proof or proof()},
                         headers={"X-CSRF-Token": challenge.json()["csrf_token"]})
    assert result.status_code == 200, result.text
    return result.json()["csrf_token"]


def test_auth_reads_no_cache_no_provider_and_unknown_date(os_pg, monkeypatch, caplog):
    client, settings, engine, urls = os_pg
    from app.api import deps
    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized provider call")
    for name in ("build_sheets_port", "build_calendar_port", "build_telegram_port",
                 "build_research_port", "build_linkedin_port"):
        monkeypatch.setattr(deps, name, forbidden)
    assert client.get(OS_API + "/contacts").status_code == 401
    csrf = login(client)
    for route in ("/today?sections=next_actions", "/contacts", "/contacts/seed_contact",
                  "/contacts/seed_contact/activities", "/reminders"):
        result = client.get(OS_API + route)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "private, no-store"
    detail = client.get(OS_API + "/contacts/seed_contact").json()["data"]
    assert detail["status"]["pipeline_stage"] is None
    assert detail["status"]["qualification"] is None
    assert detail["next_action"]["due_date"] is None
    assert "synthetic@example.test" not in caplog.text
    assert TOKEN not in caplog.text and csrf not in caplog.text
    assert client.post(AUTH + "/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get(OS_API + "/contacts").status_code == 401


def test_auth_negative_expiry_rotation_revocation(os_pg):
    client, settings, engine, _ = os_pg
    signed = proof()
    csrf = login(client, signed)
    old = client.cookies.get("__Host-mia-owner")
    assert client.get(OS_API + "/contacts", headers={"Origin": "null"}).status_code == 403
    assert client.post(AUTH + "/logout").status_code == 403
    challenge = client.get(AUTH + "/challenge").json()
    replay = client.post(AUTH + "/login", json={"login_proof": signed},
                         headers={"X-CSRF-Token": challenge["csrf_token"]})
    assert replay.status_code == 401
    csrf = login(client)
    client.cookies.clear()
    client.cookies.set("__Host-mia-owner", old)
    assert client.get(OS_API + "/contacts").status_code == 401
    client.cookies.clear()
    csrf = login(client)
    assert client.post(AUTH + "/revoke-all", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get(OS_API + "/contacts").status_code == 401
    with engine.begin() as c:
        c.execute(text("DELETE FROM owner_os_revocations"))
    login(client)
    with engine.begin() as c:
        c.execute(text("UPDATE owner_os_sessions SET expires_at=now()-interval '1 minute'"))
    assert client.get(OS_API + "/contacts").status_code == 401


def test_owner_scopes_and_unavailable_fail_closed(os_pg):
    client, settings, engine, urls = os_pg
    login(client)
    settings.telegram_owner_user_ids = OTHER
    assert client.get(OS_API + "/contacts").status_code == 401
    settings.telegram_owner_user_ids = f"{OWNER},{OTHER}"
    settings.owner_os_session_scopes = "owner-os:read:reminders"
    assert client.get(OS_API + "/contacts").status_code == 403
    settings.owner_os_session_scopes = "owner-os:read:contacts"
    settings.owner_os_read_database_url = "postgresql+psycopg://x:x@127.0.0.1:1/absent"
    result = client.get(OS_API + "/contacts")
    assert result.status_code == 503
    assert "data" not in result.json() and "x:x" not in result.text


def test_reader_actual_write_ddl_sequence_function_denial(os_pg):
    client, settings, engine, urls = os_pg
    reader = create_engine(urls["read"])
    login(client)
    try:
        for sql in ("UPDATE crm_contacts SET revision=2", "DELETE FROM crm_contacts",
                    "INSERT INTO crm_contacts(id) VALUES ('forbidden')",
                    "CREATE TABLE forbidden(id int)", "CREATE TEMP TABLE forbidden(id int)",
                    "SELECT nextval('approvals_id_seq')"):
            with reader.connect() as c, pytest.raises(SQLAlchemyError):
                c.execute(text(sql))
        with engine.begin() as c:
            c.execute(text("CREATE FUNCTION unsafe() RETURNS int LANGUAGE sql "
                           "SECURITY DEFINER AS 'SELECT 1'"))
        with reader.connect() as c:
            # PG defaults PUBLIC EXECUTE; API must reject this privilege drift.
            assert c.scalar(text(
                "SELECT has_function_privilege(current_user,'unsafe()','EXECUTE')"
            ))
        from app.services.owner_os_reads import OwnerOsUnsafeDatabase, read_snapshot
        with pytest.raises(OwnerOsUnsafeDatabase), read_snapshot(settings):
            pytest.fail("unsafe callable role was accepted")
        assert client.get(OS_API + "/contacts").status_code == 503
    finally:
        reader.dispose()


def test_quick_lead_committed_receipt_retry_cross_owner_and_stale_proof(os_pg):
    client, settings, engine, _ = os_pg
    signed = proof()
    csrf = login(client, signed)
    headers = {"X-CSRF-Token": csrf, "X-AssafWeb-Owner-Login": signed}
    data = {"request_id": str(uuid4()), "name": "בדיקת ליד", "phone": "0501234567",
            "need": "בדיקת חיבור בלבד", "source": "phone_call"}
    proposal = client.post(OS_API + "/quick-leads/proposals", json=data, headers=headers)
    assert proposal.status_code == 202, proposal.text
    approval = proposal.json()["approval_id"]
    accepted = client.post(OS_API + "/quick-leads/approvals/" + approval, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "created"
    # Discard the approval response: recovery and exact retry must find one contact.
    recovered = client.get(OS_API + "/quick-leads/receipts/" + data["request_id"])
    assert recovered.json() == accepted.json()
    replay = client.post(OS_API + "/quick-leads/approvals/" + approval, headers=headers)
    assert replay.json() == accepted.json()
    with engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM crm_contacts")) == 2
    expired = {**headers, "X-AssafWeb-Owner-Login": proof(auth_date=int(time.time()) - 601)}
    assert client.post(OS_API + "/quick-leads/proposals", json=data,
                       headers=expired).status_code == 401
    other_csrf = login(client, proof(OTHER))
    other_headers = {"X-CSRF-Token": other_csrf, "X-AssafWeb-Owner-Login": signed}
    assert client.post(OS_API + "/quick-leads/approvals/" + approval,
                       headers=other_headers).status_code == 401
    assert client.get(OS_API + "/quick-leads/receipts/" + data["request_id"]).status_code == 404


def test_quick_lead_post_commit_response_loss(os_pg, monkeypatch):
    from app.api import owner_os_actions
    from fastapi import HTTPException
    client, settings, engine, _ = os_pg
    signed = proof()
    csrf = login(client, signed)
    headers = {"X-CSRF-Token": csrf, "X-AssafWeb-Owner-Login": signed}
    data = {"request_id": str(uuid4()), "name": "Synthetic timeout", "phone": "0509876543",
            "need": "Synthetic test", "source": "phone_call"}
    proposal = client.post(OS_API + "/quick-leads/proposals", json=data, headers=headers)
    assert proposal.status_code == 202
    approval = proposal.json()["approval_id"]
    original = owner_os_actions._persist
    drop = True
    def lost_response(db, result, response):
        nonlocal drop
        committed = original(db, result, response)
        if drop and committed["status"] == "created":
            drop = False
            raise HTTPException(504, "timeout")
        return committed
    monkeypatch.setattr(owner_os_actions, "_persist", lost_response)
    failed = client.post(OS_API + "/quick-leads/approvals/" + approval, headers=headers)
    assert failed.status_code == 504
    recovered = client.get(OS_API + "/quick-leads/receipts/" + data["request_id"])
    assert recovered.status_code == 200 and recovered.json()["status"] == "created"
    retry = client.post(OS_API + "/quick-leads/approvals/" + approval, headers=headers)
    assert retry.json() == recovered.json()
    with engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM crm_contacts")) == 2
        assert c.scalar(text("SELECT count(*) FROM crm_activities "
                             "WHERE action='manual_lead_captured'")) == 1


def test_reminder_owner_revision_dispatch_and_naive_time(os_pg):
    client, settings, engine, _ = os_pg
    csrf = login(client)
    headers = {"X-CSRF-Token": csrf}
    due = datetime.now(UTC) + timedelta(hours=4)
    data = {"request_id": str(uuid4()), "text": "תזכורת בדיקה", "scheduled_at": due.isoformat()}
    created = client.post(OS_API + "/reminders", json=data, headers=headers)
    assert created.status_code == 200, created.text
    rid = created.json()["reminder_id"]
    path = OS_API + "/reminders/" + rid
    assert client.post(OS_API + "/reminders", json={**data, "text": "שינוי"},
                       headers=headers).status_code == 409
    naive = client.post(path + "/snooze", json={"expected_revision": 1,
                       "scheduled_at": "2030-01-01T12:00:00"}, headers=headers)
    assert naive.status_code == 422
    changed = client.post(path + "/snooze", json={"expected_revision": 1,
                          "scheduled_at": (due + timedelta(hours=1)).isoformat()}, headers=headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    assert client.post(path + "/complete", json={"expected_revision": 1},
                       headers=headers).status_code == 409
    with engine.begin() as c:
        c.execute(text("UPDATE owner_reminders SET delivery_state='dispatching' WHERE id=:id"),
                  {"id": rid})
    assert client.post(path + "/complete", json={"expected_revision": 2},
                       headers=headers).status_code == 409
    other_csrf = login(client, proof(OTHER))
    assert client.post(path + "/complete", json={"expected_revision": 2},
                       headers={"X-CSRF-Token": other_csrf}).status_code == 404
    read = client.get(OS_API + "/reminders")
    assert read.status_code == 200 and read.json()["data"]["items"] == []
    csrf = login(client)
    with engine.begin() as c:
        c.execute(text("UPDATE owner_reminders SET delivery_state='pending' WHERE id=:id"),
                  {"id": rid})
    rescheduled = client.post(path + "/reschedule", headers={"X-CSRF-Token": csrf},
                              json={"expected_revision": 2, "scheduled_at": due.isoformat()})
    assert rescheduled.status_code == 200 and rescheduled.json()["revision"] == 3
    completed = client.post(path + "/complete", headers={"X-CSRF-Token": csrf},
                            json={"expected_revision": 3})
    assert completed.status_code == 200 and completed.json()["revision"] == 4
    assert completed.json()["status"] == "completed"


def test_boundary_errors_do_not_echo_pii_or_proof(os_pg):
    client, settings, engine, _ = os_pg
    csrf = login(client)
    result = client.post(OS_API + "/reminders", json={"text": "PRIVATE-CUSTOMER-SECRET"},
                         headers={"X-CSRF-Token": csrf})
    assert result.status_code == 422
    assert "PRIVATE-CUSTOMER-SECRET" not in result.text
    assert result.headers["cache-control"] == "private, no-store"
    assert client.get(AUTH + "/session?token=SECRET").status_code == 400
    duplicate = client.post(AUTH + "/login", content='{"login_proof":"a","login_proof":"b"}',
                            headers={"Content-Type": "application/json"})
    assert duplicate.status_code == 422
    big = client.post(AUTH + "/login", content=b"x" * 17_000,
                      headers={"Content-Type": "application/json"})
    assert big.status_code == 413


def test_pg_pagination_activity_and_unrelated_owner_cursor(os_pg):
    client, settings, engine, _ = os_pg
    login(client)
    with engine.begin() as c:
        stamp = datetime.now(UTC).isoformat()
        for contact_id in ("page_a", "page_b", "page_c"):
            c.execute(CrmContactRow.__table__.insert().values(
                id=contact_id, revision=1, fields_json='{"name":"Synthetic"}',
                created_at=stamp, updated_at=stamp,
            ))
        c.execute(text("INSERT INTO crm_activities "
                       "(id,contact_id,occurred_at,who,channel,action,result,source_ref,"
                       "created_at) "
                       "VALUES ('activity_test','seed_contact',:stamp,'owner','manual',"
                       "'note','Synthetic activity','synthetic-activity',:stamp)"),
                  {"stamp": stamp})
    first = client.get(OS_API + "/contacts?limit=2")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["coverage"] == "partial" and body["pagination"]["has_more"]
    cursor = body["pagination"]["next_cursor"]
    second = client.get(OS_API + "/contacts?limit=2&cursor=" + cursor)
    assert second.status_code == 200, second.text
    ids = [row["contact_id"] for row in body["data"]["items"]]
    ids += [row["contact_id"] for row in second.json()["data"]["items"]]
    assert len(set(ids)) == len(ids) == 4
    activity = client.get(OS_API + "/contacts/seed_contact/activities")
    assert activity.json()["data"]["items"][0]["activity_id"] == "activity_test"
    login(client, proof(OTHER))
    foreign_cursor = client.get(OS_API + "/contacts?limit=2&cursor=" + cursor)
    assert foreign_cursor.status_code == 400
    assert foreign_cursor.json()["error"]["code"] == "invalid_query"
    assert "data" not in foreign_cursor.json()


def test_pg_role_membership_column_permission_and_actions_off(os_pg):
    client, settings, engine, urls = os_pg
    csrf = login(client)
    settings.owner_os_actions_enabled = False
    assert client.post(OS_API + "/reminders", json={},
                       headers={"X-CSRF-Token": csrf}).status_code == 403
    reader_role = make_url(urls["read"]).username
    with engine.begin() as c:
        c.execute(text(f"GRANT UPDATE(revision) ON crm_contacts TO {reader_role}"))
    result = client.get(OS_API + "/contacts")
    assert result.status_code == 503
    with engine.begin() as c:
        c.execute(text(f"REVOKE UPDATE(revision) ON crm_contacts FROM {reader_role}"))
        c.execute(text(f"GRANT pg_read_all_data TO {reader_role}"))
    assert client.get(OS_API + "/contacts").status_code == 503
    with engine.begin() as c:
        c.execute(text(f"REVOKE pg_read_all_data FROM {reader_role}"))


def test_auth_atomic_concurrent_proof_consumption(os_pg):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.main import app
    clients = [TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) for _ in range(2)]
    try:
        challenges = [c.get(AUTH + "/challenge").json()["csrf_token"] for c in clients]
        signed = proof()
        barrier = Barrier(2)
        def attempt(index):
            barrier.wait(timeout=5)
            return clients[index].post(AUTH + "/login", json={"login_proof": signed},
                                       headers={"X-CSRF-Token": challenges[index]}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        assert sorted(results) == [200, 401]
        with os_pg[2].connect() as c:
            assert c.scalar(text("SELECT count(*) FROM owner_os_sessions")) == 1
    finally:
        for client in clients:
            client.close()



def test_action_role_rejects_effective_privilege_drift(os_pg):
    from app.api.owner_os_actions import action_session
    from fastapi import HTTPException

    _, settings, engine, urls = os_pg
    role = make_url(urls["action"]).username
    schema = make_url(urls["action"]).query["options"].split("=")[1]
    attacks = [
        (f"GRANT SELECT ON owner_os_sessions TO {role}",
         f"REVOKE SELECT ON owner_os_sessions FROM {role}"),
        (f"GRANT UPDATE(revision) ON crm_contacts TO {role}",
         f"REVOKE UPDATE(revision) ON crm_contacts FROM {role}"),
        (f"GRANT DELETE ON crm_contacts TO {role}",
         f"REVOKE DELETE ON crm_contacts FROM {role}"),
        (f"GRANT SELECT ON approvals_id_seq TO {role}",
         f"REVOKE SELECT ON approvals_id_seq FROM {role}"),
        (f"GRANT pg_write_all_data TO {role}", f"REVOKE pg_write_all_data FROM {role}"),
        (f"ALTER ROLE {role} INHERIT", f"ALTER ROLE {role} NOINHERIT"),
        (f'GRANT CREATE ON SCHEMA "{schema}" TO {role}',
         f'REVOKE CREATE ON SCHEMA "{schema}" FROM {role}'),
        (f"GRANT TEMPORARY ON DATABASE owner_os_test TO {role}",
         f"REVOKE TEMPORARY ON DATABASE owner_os_test FROM {role}"),
        ("CREATE FUNCTION unsafe_action() RETURNS int LANGUAGE SQL SECURITY DEFINER "
         "AS 'SELECT 1'", "DROP FUNCTION unsafe_action()"),
    ]
    with action_session(settings) as db:
        assert db.scalar(text("SELECT 1")) == 1
    for grant, revoke in attacks:
        with engine.begin() as c:
            c.execute(text(grant))
        try:
            with pytest.raises(HTTPException) as rejected, action_session(settings):
                pytest.fail("unsafe action role must reject before business logic")
            assert rejected.value.status_code == 503
        finally:
            with engine.begin() as c:
                c.execute(text(revoke))
    with action_session(settings) as db:
        assert db.scalar(text("SELECT 1")) == 1


def test_action_duplicate_and_identity_conflict_with_narrow_role(os_pg):
    client, _, engine, _ = os_pg
    signed = proof()
    csrf = login(client, signed)
    headers = {"X-CSRF-Token": csrf, "X-AssafWeb-Owner-Login": signed}
    def propose(email, phone=""):
        return client.post(OS_API + "/quick-leads/proposals", headers=headers, json={
            "request_id": str(uuid4()), "name": "Synthetic", "need": "Local check",
            "email": email, "phone": phone, "source": "other",
        })
    with engine.begin() as c:
        c.execute(CrmIdentityRow.__table__.insert().values(
            id="seed_email", contact_id="seed_contact", kind="email",
            normalized_value="synthetic@example.test", source_ref="synthetic-local",
            created_at=datetime.now(UTC).isoformat(),
        ))
    duplicate = propose("synthetic@example.test")
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["status"] == "duplicate"
    with engine.begin() as c:
        stamp = datetime.now(UTC).isoformat()
        c.execute(CrmContactRow.__table__.insert().values(
            id="collision_contact", revision=1, fields_json='{"phone":"+972521234567"}',
            created_at=stamp, updated_at=stamp,
        ))
        c.execute(CrmIdentityRow.__table__.insert().values(
            id="collision_phone", contact_id="collision_contact", kind="phone",
            normalized_value="+972521234567", source_ref="synthetic-local", created_at=stamp,
        ))
    conflict = propose("synthetic@example.test", "+972521234567")
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["status"] == "identity_conflict"
    with engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM crm_contacts")) == 2
        assert c.scalar(text("SELECT count(*) FROM crm_issues")) == 1

def test_frozen_frontend_bff_to_postgres(os_pg):
    frontend = os.getenv("MIA_OWNER_OS_FRONTEND_ROOT", "")
    if not frontend:
        pytest.skip("frozen mobile frontend path required")
    with os_pg[2].begin() as c:
        now = datetime.now(UTC)
        c.execute(OwnerReminderRow.__table__.insert().values(
            id="f" * 32, owner_id=OWNER, source_event_id="synthetic-emoji",
            text="😀" * 300, scheduled_at=now + timedelta(hours=1),
            timezone="Asia/Jerusalem", status="active", revision=1,
            delivery_state="pending", created_at=now, updated_at=now,
        ))
    import uvicorn
    from app.main import app
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.02)
        assert server.started
        script = Path(__file__).resolve().parents[1] / "integration/owner_os_bff_e2e.mjs"
        result = subprocess.run(
            ["node", str(script), f"http://127.0.0.1:{port}", frontend],
            input=json.dumps({"proof": proof()}), text=True, capture_output=True, timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS frozen OwnerClient/DTO" in result.stdout
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
