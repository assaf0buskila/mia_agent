"""The real HTTP boundary: signed identity, exact approval and commit receipts."""

import hashlib
import hmac
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from app.api.deps import get_db
from app.api.quick_leads import router, verify_owner_login
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.models import ApprovalRow, CrmActivityRow, CrmContactRow, CrmOutboxRow
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

OWNER = "12345"
TOKEN = "test-only-token"
ORIGIN = "https://pwa.example.test"
PREFIX = "/v1/owner/quick-leads"


def login_proof(**changes) -> str:
    data = {"id": OWNER, "first_name": "Owner", "auth_date": str(int(time.time()))}
    data.update(changes)
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    data["hash"] = hmac.new(
        hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256
    ).hexdigest()
    return json.dumps(data)


def headers(**changes) -> dict:
    return {"Origin": ORIGIN, "X-AssafWeb-Owner-Login": login_proof(**changes)}


def lead(**changes) -> dict:
    data = {
        "request_id": str(uuid4()), "name": "דנה", "phone": "0501234567",
        "need": "אתר לעסק", "source": "phone_call",
    }
    data.update(changes)
    return data


@pytest.fixture(params=["sqlite", "postgres"])
def api(tmp_path, request):
    admin = None
    schema = "quick_lead_" + uuid4().hex
    if request.param == "postgres":
        url = os.environ.get("MIA_QUICK_LEAD_TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("local MIA_QUICK_LEAD_TEST_POSTGRES_URL required")
        parsed = make_url(url)
        assert parsed.host in {"localhost", "127.0.0.1", "::1"}
        assert parsed.database == "quick_lead_test"
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'quick_leads.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = Settings(
        _env_file=None, telegram_bot_token=TOKEN, telegram_owner_user_ids=OWNER,
        cors_origins=ORIGIN,
    )
    app = FastAPI()
    app.include_router(router)

    def database():
        with factory() as db:
            try:
                yield db
            except Exception:
                db.rollback()
                raise

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client, factory, settings
    engine.dispose()
    if admin is not None:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def count(factory, model):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_real_http_create_commits_and_replay_returns_same_contact(api, caplog):
    client, factory, _settings = api
    payload = lead(business="עסק דנה", next_action="להתקשר", follow_up_date="2026-10-20")
    proposed = client.post(f"{PREFIX}/proposals", json=payload, headers=headers())
    assert proposed.status_code == 202, proposed.text
    assert proposed.headers["cache-control"] == "no-store"
    assert count(factory, CrmContactRow) == 0
    approval_id = proposed.json()["approval_id"]
    result = client.post(f"{PREFIX}/approvals/{approval_id}", headers=headers())
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "created"
    contact_id = result.json()["contact_id"]
    with factory() as db:
        row = db.get(CrmContactRow, contact_id)
        fields = json.loads(row.fields_json)
        assert fields["status"] == "lead"
        assert fields["date"] == "2026-10-20"
        assert fields["source"] == "phone_call"
        assert fields["business"] == "עסק דנה"
        destinations = set(db.scalars(select(CrmOutboxRow.destination)))
        assert destinations <= {"contacts", "activity"}
    replay = client.post(f"{PREFIX}/approvals/{approval_id}", headers=headers())
    assert replay.json()["contact_id"] == contact_id
    assert count(factory, CrmContactRow) == 1
    assert count(factory, CrmActivityRow) == 1
    assert "עסק דנה" not in caplog.text


@pytest.mark.parametrize("changes", [
    {"name": " "}, {"phone": "", "email": ""}, {"need": ""},
    {"phone": "abc0501234567"}, {"phone": "05012"},
    {"phone": "050123456"}, {"phone": "+97250123456"},
    {"phone": "(0501234567"},
    {"email": "invalid", "phone": "0501234567"},
    {"source": "website"}, {"status": "paying_customer"}, {"actor_id": OWNER},
])
def test_bad_inputs_do_not_persist(api, changes):
    client, factory, _settings = api
    result = client.post(f"{PREFIX}/proposals", json=lead(**changes), headers=headers())
    assert result.status_code == 422, result.text
    assert count(factory, ApprovalRow) == 0
    assert count(factory, CrmContactRow) == 0


def test_upstream_phone_policy_applies_to_capture_and_duplicate(api):
    client, factory, configuration = api
    configuration.crm_israeli_phone_normalization_enabled = True
    payload = lead()
    proposed = client.post(f"{PREFIX}/proposals", json=payload, headers=headers())
    assert proposed.status_code == 202, proposed.text
    result = client.post(
        f"{PREFIX}/approvals/{proposed.json()['approval_id']}", headers=headers()
    )
    assert result.status_code == 200, result.text
    duplicate = client.post(f"{PREFIX}/proposals", json=lead(), headers=headers())
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate.json()["contact_id"] == result.json()["contact_id"]
    assert count(factory, CrmContactRow) == 1


@pytest.mark.parametrize("proof", [
    "", "{}", '{"id":"12345","id":"12345"}',
    login_proof(id="99999", username="owner"),
    login_proof(auth_date=str(int(time.time()) - 601)),
    login_proof(auth_date=str(int(time.time()) + 120)),
    login_proof().replace('"Owner"', '"Forged"'),
])
def test_owner_auth_cannot_be_forged(api, proof):
    client, factory, _settings = api
    result = client.post(
        f"{PREFIX}/proposals", json=lead(),
        headers={"Origin": ORIGIN, "X-AssafWeb-Owner-Login": proof},
    )
    assert result.status_code == 401, result.text
    assert count(factory, ApprovalRow) == 0


@pytest.mark.parametrize("origin", [None, "null", "https://attacker.example"])
def test_origin_is_required(api, origin):
    client, factory, _settings = api
    request_headers = {"X-AssafWeb-Owner-Login": login_proof()}
    if origin is not None:
        request_headers["Origin"] = origin
    result = client.post(f"{PREFIX}/proposals", json=lead(), headers=request_headers)
    assert result.status_code == 403
    assert count(factory, ApprovalRow) == 0


def test_unconfigured_owner_is_denied(api):
    client, _factory, settings = api
    settings.telegram_owner_user_ids = ""
    assert client.post(
        f"{PREFIX}/proposals", json=lead(), headers=headers()
    ).status_code == 401


def test_another_allowlisted_owner_cannot_approve_a_proposal(api):
    client, factory, settings = api
    settings.telegram_owner_user_ids = OWNER + ",67890"
    proposed = client.post(f"{PREFIX}/proposals", json=lead(), headers=headers()).json()
    result = client.post(
        f"{PREFIX}/approvals/{proposed['approval_id']}", headers=headers(id="67890")
    )
    assert result.status_code == 409, result.text
    assert result.json()["status"] == "unbound"
    assert count(factory, CrmContactRow) == 0


def test_approval_cannot_replace_the_stored_fields(api):
    client, factory, _settings = api
    proposed = client.post(f"{PREFIX}/proposals", json=lead(), headers=headers()).json()
    result = client.post(
        f"{PREFIX}/approvals/{proposed['approval_id']}",
        json={"phone": "0529876543", "name": "Replacement", "status": "customer"},
        headers=headers(),
    )
    assert result.json()["status"] == "created", result.text
    with factory() as db:
        fields = json.loads(db.get(CrmContactRow, result.json()["contact_id"]).fields_json)
        assert fields["name"] == "דנה"
        assert fields["phone"] == "+972501234567"
        assert fields["status"] == "lead"


def test_login_values_and_boundaries():
    settings = Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_owner_user_ids=OWNER)
    now = int(time.time())
    assert verify_owner_login(login_proof(auth_date=str(now - 600)), settings, now=now).actor_id
    for proof in [login_proof(id=True), login_proof(first_name="x\nid=12345"), "x" * 4097]:
        with pytest.raises(HTTPException):
            verify_owner_login(proof, settings, now=now)


def test_failure_to_commit_cannot_confirm_creation(api, monkeypatch):
    client, factory, _settings = api
    proposed = client.post(f"{PREFIX}/proposals", json=lead(), headers=headers()).json()
    original_commit = Session.commit

    def broken_commit(self):
        raise SQLAlchemyError("synthetic database failure")

    monkeypatch.setattr(Session, "commit", broken_commit)
    result = client.post(f"{PREFIX}/approvals/{proposed['approval_id']}", headers=headers())
    assert result.status_code == 503, result.text
    assert result.json() == {"detail": "persistence_failed"}
    monkeypatch.setattr(Session, "commit", original_commit)
    assert count(factory, CrmContactRow) == 0
    assert count(factory, CrmActivityRow) == 0
    result = client.post(f"{PREFIX}/approvals/{proposed['approval_id']}", headers=headers())
    assert result.json()["status"] == "created"


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_submissions_and_approvals(api, same_request):
    client, factory, _settings = api
    payloads = [lead(), lead()]
    if same_request:
        payloads[1] = payloads[0]
    barrier = Barrier(2)

    def propose(payload):
        barrier.wait(timeout=5)
        return client.post(f"{PREFIX}/proposals", json=payload, headers=headers())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(propose, payloads))
    assert all(result.status_code == 202 for result in results), [r.text for r in results]
    ids = [result.json()["approval_id"] for result in results]
    assert count(factory, ApprovalRow) == (1 if same_request else 2)
    barrier = Barrier(2)

    def approve(approval_id):
        barrier.wait(timeout=5)
        return client.post(f"{PREFIX}/approvals/{approval_id}", headers=headers())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, ids))
    statuses = [result.json()["status"] for result in results]
    assert statuses.count("created") == (2 if same_request else 1), statuses
    if not same_request:
        assert "target_changed" in statuses
    assert count(factory, CrmContactRow) == 1
    assert count(factory, CrmActivityRow) == 1
