"""Synthetic HTTP proof of AssafWeb intake -> canonical CRM -> private Owner OS."""

import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from app.api.deps import get_db
from app.api.form_intake import AssafWebFormLead, _canonical_payload_hash
from app.db.models import (
    AiRunRow,
    CanonicalEventRow,
    CrmActivityRow,
    CrmContactConversationRow,
    CrmContactRow,
    CrmFormIntakeReceiptRow,
    CrmIdentityRow,
    CrmIssueRow,
    CrmOutboxRow,
    OwnerNotificationRecipientClaimRow,
)
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from tests.unit.test_owner_os_postgres import (
    AUTH,
    ORIGIN,
    OS_API,
    OTHER,
    login,
    proof,
)
from tests.unit.test_owner_os_postgres import os_pg as os_pg  # pytest fixture

INTAKE = "/v1/internal/assafweb/form-leads"
SECRET = "synthetic-intake-local-only"
MODELS = (
    CrmFormIntakeReceiptRow,
    CrmContactRow,
    CrmActivityRow,
    CrmIdentityRow,
    CrmContactConversationRow,
    CrmOutboxRow,
    CrmIssueRow,
    OwnerNotificationRecipientClaimRow,
    AiRunRow,
    CanonicalEventRow,
)


def payload(**changes):
    value = {
        "source_id": str(uuid4()),
        "submitted_at": "2026-10-12T09:30:00+03:00",
        "interest": "business",
        "form": "sherut",
        "submission_type": "production",
        "name": "ליד סינתטי",
        "phone_e164": "+972501234567",
        "business": "עסק סינתטי",
        "note": "בקשת בדיקה סינתטית",
        "page": "/sherut",
        "attribution": {"utm_source": "synthetic-local"},
    }
    value.update(changes)
    return value


def post(client, value):
    return client.post(INTAKE, json=value, headers={"X-AssafWeb-Intake-Secret": SECRET})


def snapshot(factory):
    with factory() as db:
        return {
            model.__tablename__: list(db.execute(
                select(model.__table__).order_by(*model.__table__.primary_key)
            ).mappings())
            for model in MODELS
        }


@pytest.fixture
def chain(os_pg, monkeypatch):
    client, settings, engine, _urls = os_pg
    settings.owner_os_actions_enabled = False
    settings.assafweb_form_intake_secret = SECRET
    monkeypatch.setattr("app.api.form_intake.get_settings", lambda: settings)
    factory = sessionmaker(bind=engine, autoflush=False)

    def request_db():
        with factory() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def forbidden(*args, **kwargs):
        pytest.fail("intake/owner reads must never start workers or providers")

    for name in (
        "build_sheets_port", "build_calendar_port", "build_telegram_port",
        "build_research_port", "build_linkedin_port", "build_transcription_port",
    ):
        monkeypatch.setattr(f"app.api.deps.{name}", forbidden)
    monkeypatch.setattr("app.workers.crm_runtime.start_crm_runtime", forbidden)
    monkeypatch.setattr("app.workers.reminders.start_reminder_runtime", forbidden)
    app.dependency_overrides[get_db] = request_db
    try:
        yield client, settings, engine, factory
    finally:
        app.dependency_overrides.pop(get_db, None)


def owner_reads(client, ids):
    routes = (
        ("/contacts", "stored_pg_contacts"),
        (f"/contacts/{ids['contact_id']}", "stored_pg_contact_detail"),
        (f"/contacts/{ids['contact_id']}/activities", "stored_pg_contact_activities"),
    )
    bodies = []
    for route, coverage_scope in routes:
        result = client.get(OS_API + route)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["api_version"] == "owner-os.v1"
        assert body["source"] == "mia_postgresql"
        assert body["owner_scope"] == "self"
        assert body["coverage_scope"] == coverage_scope
        assert body["coverage"] == "complete"
        assert body["freshness"] == "fresh"
        assert body["request_id"] and body["snapshot_id"] and body["observed_at"]
        assert result.headers["cache-control"] == "private, no-store"
        bodies.append(body)
    assert ids["contact_id"] in {item["contact_id"] for item in bodies[0]["data"]["items"]}
    assert bodies[1]["data"]["contact_id"] == ids["contact_id"]
    assert [item["activity_id"] for item in bodies[2]["data"]["items"]] == [ids["activity_id"]]
    assert bodies[2]["data"]["items"][0]["contact_id"] == ids["contact_id"]
    return bodies


def test_capture_receipt_replay_conflict_and_owner_reads_share_one_chain(
    chain, caplog, record_property,
):
    client, _settings, _engine, factory = chain
    initial = snapshot(factory)
    value = payload()
    captured = post(client, value)
    assert captured.status_code == 200, captured.text
    ids = captured.json()
    assert ids["status"] == "captured"
    with factory() as db:
        receipt = db.get(CrmFormIntakeReceiptRow, value["source_id"])
        assert receipt.source_id == value["source_id"]
        assert receipt.payload_sha256 == _canonical_payload_hash(
            AssafWebFormLead.model_validate(value)
        )
        assert (receipt.contact_id, receipt.activity_id) == (ids["contact_id"], ids["activity_id"])
        contact = db.get(CrmContactRow, ids["contact_id"])
        activity = db.get(CrmActivityRow, ids["activity_id"])
        assert contact.source_ref == f"assafweb-form:{value['source_id']}"
        assert activity.source_ref == f"assafweb-form:{value['source_id']}:activity"
        assert activity.contact_id == contact.id
        assert activity.channel == "form" and activity.action == "contact_captured"
        assert activity.occurred_at == "2026-10-12T06:30:00+00:00"
        assert json.loads(contact.fields_json)["summary"] == value["note"]
        conversation = db.scalars(select(CrmContactConversationRow)).one()
        assert conversation.conversation_id == f"form:{value['source_id']}"
        assert conversation.contact_id == contact.id
        jobs = db.scalars(select(CrmOutboxRow)).all()
        assert sorted(job.destination for job in jobs) == ["activity", "contacts", "telegram",
                                                          "telegram"]
        assert all(job.status == "pending" and job.attempts == 0 for job in jobs)
    stored = snapshot(factory)
    for table in ("crm_contacts", "crm_activities", "crm_form_intake_receipts"):
        assert len(stored[table]) == len(initial[table]) + 1
    assert stored["owner_notification_recipient_claims"] == []
    assert stored["ai_runs"] == [] and stored["canonical_events"] == []

    # New requests use separate DB sessions; replay includes normalized JSON ordering.
    replay = post(client, dict(reversed(list(value.items()))))
    assert replay.status_code == 200
    assert replay.json() == {**ids, "status": "replayed"}
    assert snapshot(factory) == stored
    conflict = post(client, {**value, "note": "changed synthetic payload"})
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "source_id payload conflict"}
    assert snapshot(factory) == stored

    login(client)
    bodies = owner_reads(client, ids)
    assert bodies[1]["data"]["summary"] == value["note"]
    assert bodies[1]["data"]["need"] == "פתרון AI לעסק"
    assert snapshot(factory) == stored
    assert SECRET not in caplog.text and value["phone_e164"] not in caplog.text
    # Persist only synthetic correlation IDs in an optional local JUnit report.
    for key, value in {"source_id": value["source_id"],
                       "contact_id": ids["contact_id"], "activity_id": ids["activity_id"],
                       "api_version": bodies[1]["api_version"]}.items():
        record_property(key, value)


@pytest.mark.parametrize("submission_type", ["test", "internal"])
def test_nonproduction_is_excluded_before_capture_even_with_consumed_source_id(
    chain, monkeypatch, submission_type,
):
    client, _settings, _engine, factory = chain
    value = payload()
    assert post(client, value).status_code == 200
    stored = snapshot(factory)

    def forbidden(*args, **kwargs):
        pytest.fail("nonproduction must not enter CRM capture")

    monkeypatch.setattr("app.api.form_intake.FormIntakeService.capture", forbidden)
    for source_id in (value["source_id"], str(uuid4())):
        ignored = post(client, {**value, "source_id": source_id,
                                "submission_type": submission_type})
        assert ignored.status_code == 200
        assert ignored.json() == {"status": "ignored"}
        assert snapshot(factory) == stored


@pytest.mark.parametrize("failure_point", ["receipt", "commit"])
def test_postgres_failure_rolls_back_whole_capture_and_same_uuid_can_retry(
    chain, monkeypatch, failure_point,
):
    client, _settings, _engine, factory = chain
    stored = snapshot(factory)
    value = payload()

    def failing_db():
        with factory() as db:
            try:
                if failure_point == "receipt":
                    # An actual PostgreSQL constraint fails after CRM has been flushed.
                    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                    original_flush = db.flush

                    def fail_receipt(*args, **kwargs):
                        for row in db.new:
                            if isinstance(row, CrmFormIntakeReceiptRow):
                                row.activity_id = "missing-synthetic-activity"
                        return original_flush(*args, **kwargs)

                    db.flush = fail_receipt
                yield db
                if failure_point == "commit":
                    receipt = db.get(CrmFormIntakeReceiptRow, value["source_id"])
                    assert receipt is not None
                    assert db.get(CrmContactRow, receipt.contact_id) is not None
                    assert db.get(CrmActivityRow, receipt.activity_id) is not None
                    # Another transaction cannot see any of these uncommitted rows.
                    assert snapshot(factory) == stored
                    raise RuntimeError("synthetic failure before commit")
                db.commit()
            except Exception:
                db.rollback()
                raise

    previous = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = failing_db
    unsafe_client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
    try:
        result = post(unsafe_client, value)
        assert result.status_code >= 500
    finally:
        unsafe_client.close()
        app.dependency_overrides[get_db] = previous
    assert snapshot(factory) == stored
    retried = post(client, value)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "captured"
    with factory() as db:
        receipt = db.get(CrmFormIntakeReceiptRow, value["source_id"])
        assert receipt is not None and receipt.contact_id == retried.json()["contact_id"]


@pytest.mark.parametrize("route", ["/contacts", "/contacts/seed_contact",
                                   "/contacts/seed_contact/activities"])
@pytest.mark.parametrize("case", ["anonymous", "forged", "removed_owner",
                                  "session_missing_scope", "current_scope_removed"])
def test_unauthorized_or_unscoped_session_never_opens_crm_reader(chain, monkeypatch, route, case):
    client, settings, _engine, factory = chain
    stored = snapshot(factory)
    if case == "forged":
        client.cookies.set("__Host-mia-owner", "synthetic-forged", domain="os.example.test",
                           path="/")
    elif case not in {"anonymous", "forged"}:
        if case == "session_missing_scope":
            settings.owner_os_session_scopes = "owner-os:read:reminders"
        login(client)
        if case == "removed_owner":
            settings.telegram_owner_user_ids = OTHER
        if case == "current_scope_removed":
            settings.owner_os_session_scopes = "owner-os:read:reminders"

    @contextmanager
    def forbidden_reader(*args, **kwargs):
        pytest.fail("authorization must reject before opening the CRM read snapshot")
        yield

    monkeypatch.setattr("app.api.owner_os.read_snapshot", forbidden_reader)
    result = client.get(OS_API + route)
    assert result.status_code == (403 if "scope" in case else 401)
    assert result.json()["api_version"] == "owner-os.v1"
    assert "data" not in result.json()
    assert snapshot(factory) == stored


def test_nonowner_signed_login_cannot_obtain_crm_access(chain, monkeypatch):
    client, _settings, _engine, factory = chain
    stored = snapshot(factory)
    challenge = client.get(AUTH + "/challenge").json()
    result = client.post(AUTH + "/login", json={"login_proof": proof(owner="999999")},
                         headers={"X-CSRF-Token": challenge["csrf_token"]})
    assert result.status_code == 401
    assert client.get(OS_API + "/contacts").status_code == 401
    assert snapshot(factory) == stored


@pytest.mark.parametrize("changes", [
    {"business": "א" * 200},
    {"note": "א" * 2_000},
    {"note": "😀" * 2_000},
    {"attribution": {f"utm_{field}": "😀" * 200
                     for field in ("source", "medium", "campaign", "content", "term")}},
])
def test_valid_intake_text_limits_remain_readable_without_changing_canonical_data(chain, changes):
    client, _settings, _engine, factory = chain
    value = payload(**changes)
    result = post(client, value)
    assert result.status_code == 200, result.text
    with factory() as db:
        contact = db.get(CrmContactRow, result.json()["contact_id"])
        fields = json.loads(contact.fields_json)
        assert fields["business"] == value["business"]
        assert fields["summary"] == value["note"]
    stored = snapshot(factory)
    login(client)
    bodies = owner_reads(client, result.json())
    assert snapshot(factory) == stored
    detail = bodies[1]["data"]
    for field, maximum in (("business", 160), ("summary", 1_000)):
        text_value = detail[field]
        assert len(text_value.encode("utf-16-le")) // 2 <= maximum
        original = value["business" if field == "business" else "note"]
        if len(original.encode("utf-16-le")) // 2 > maximum:
            assert text_value.endswith("…")
            assert f"{field}_text_excerpt" in detail["record_quality"]["reason_codes"]
            assert field in detail["record_quality"]["text_excerpts"]
    activity = bodies[2]["data"]["items"][0]
    assert len(activity["result"].encode("utf-16-le")) // 2 <= 2_000
    if changes.get("note", "").startswith("😀") or "attribution" in changes:
        assert activity["result_truncated"] is True
        assert activity["result_presentation"] == "excerpt"
        assert activity["result"].endswith("…")
