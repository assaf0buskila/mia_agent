import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from app.api.deps import get_db
from app.core.config import Settings
from app.db.base import Base
from app.db.migrate import apply_migrations
from app.db.models import (
    AiRunRow,
    CanonicalEventRow,
    CrmActivityRow,
    CrmContactConversationRow,
    CrmContactRow,
    CrmFormIntakeReceiptRow,
    CrmIssueRow,
    CrmOutboxRow,
    OwnerNotificationRecipientClaimRow,
)
from app.db.session import make_engine
from app.main import app
from app.services.crm_v2 import WRITER_OWNER, CrmService
from app.services.form_intake import FormIntakeService
from app.workers.crm_runtime import telegram_receipt_handler
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

SECRET = "test-form-intake-secret"


def _payload(**updates):
    payload = {
        "source_id": str(uuid4()),
        "submitted_at": "2026-10-08T09:30:00+03:00",
        "interest": "learning",
        "form": "learn_hero",
        "submission_type": "production",
        "name": "דנה",
        "phone_e164": "+972501234567",
        "business": "סטודיו דנה",
        "note": "רוצה להבין מה כולל המסלול",
        "page": "/learn?utm_source=google",
        "attribution": {
            "utm_source": "google",
            "utm_medium": "cpc",
            "utm_campaign": "learn-first",
            "utm_content": "hero",
            "utm_term": "ai course",
        },
    }
    payload.update(updates)
    return payload


@pytest.fixture
def intake_client(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_db():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    settings = Settings(
        _env_file=None,
        assafweb_form_intake_secret=SECRET,
        telegram_owner_user_ids="111,222",
    )
    monkeypatch.setattr("app.api.form_intake.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client, factory
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def _post(client: TestClient, payload: dict, *, secret: str = SECRET):
    return client.post(
        "/v1/internal/assafweb/form-leads",
        json=payload,
        headers={"X-AssafWeb-Intake-Secret": secret},
    )


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _telegram_payload(factory, recipient_id: str = "111") -> dict:
    with factory() as session:
        jobs = session.scalars(
            select(CrmOutboxRow).where(CrmOutboxRow.destination == "telegram")
        ).all()
        return next(
            json.loads(job.payload_json)
            for job in jobs
            if json.loads(job.payload_json)["recipient_id"] == recipient_id
        )


def test_disabled_and_auth_happen_before_body_parsing(intake_client, monkeypatch) -> None:
    client, _factory = intake_client
    monkeypatch.setattr(
        "app.api.form_intake.get_settings",
        lambda: Settings(_env_file=None, assafweb_form_intake_secret=""),
    )
    disabled = client.post(
        "/v1/internal/assafweb/form-leads",
        content=b"not-json",
        headers={"content-type": "application/json"},
    )
    assert disabled.status_code == 503

    monkeypatch.setattr(
        "app.api.form_intake.get_settings",
        lambda: Settings(_env_file=None, assafweb_form_intake_secret=SECRET),
    )
    compared_lengths = []

    def reject_digest(expected: bytes, supplied: bytes) -> bool:
        compared_lengths.append((len(expected), len(supplied)))
        return False

    monkeypatch.setattr("app.api.form_intake.hmac.compare_digest", reject_digest)
    unauthorized = client.post(
        "/v1/internal/assafweb/form-leads",
        content=b"x" * 20_000,
        headers={
            "content-type": "application/json",
            "X-AssafWeb-Intake-Secret": "wrong",
        },
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json() == {"detail": "unauthorized"}
    assert compared_lengths == [(32, 32)]


def test_authenticated_body_is_bounded_before_json_parse(intake_client) -> None:
    client, _factory = intake_client
    response = client.post(
        "/v1/internal/assafweb/form-leads",
        content=b"x" * 20_000,
        headers={
            "content-type": "application/json",
            "X-AssafWeb-Intake-Secret": SECRET,
        },
    )
    assert response.status_code == 413


def test_commit_failure_cannot_acknowledge_or_leave_partial_rows(intake_client) -> None:
    _client, factory = intake_client
    normal_override = app.dependency_overrides[get_db]

    def failing_commit_db():
        session = factory()
        try:
            yield session

            def fail_commit() -> None:
                raise RuntimeError("injected commit failure")

            session.commit = fail_commit
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = failing_commit_db
    try:
        response = _post(TestClient(app, raise_server_exceptions=False), _payload())
    finally:
        app.dependency_overrides[get_db] = normal_override

    assert response.status_code >= 500
    assert response.status_code // 100 != 2
    with factory() as session:
        assert _count(session, CrmFormIntakeReceiptRow) == 0
        assert _count(session, CrmContactRow) == 0
        assert _count(session, CrmActivityRow) == 0
        assert _count(session, CrmOutboxRow) == 0


def test_capture_is_atomic_deterministic_and_uses_existing_outbox(intake_client) -> None:
    client, factory = intake_client
    response = _post(client, _payload())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "captured"
    assert set(response.json()) == {"status", "contact_id", "activity_id"}

    with factory() as session:
        assert _count(session, CrmFormIntakeReceiptRow) == 1
        assert _count(session, CrmContactRow) == 1
        assert _count(session, CrmContactConversationRow) == 1
        assert _count(session, CrmActivityRow) == 1
        assert _count(session, CrmOutboxRow) == 4  # Contacts, Activity, two Telegram owners.
        assert _count(session, CanonicalEventRow) == 0
        assert _count(session, AiRunRow) == 0

        contact = session.scalars(select(CrmContactRow)).one()
        fields = json.loads(contact.fields_json)
        assert fields["source"] == "AssafWeb form / learn_hero"
        assert fields["want"] == "Learn"
        assert fields["status"] == "new"
        assert fields["next_step"] == "לחזור לפונה לגבי מסלול הלמידה"
        activity = session.scalars(select(CrmActivityRow)).one()
        assert activity.channel == "form"
        assert activity.action == "contact_captured"
        assert "source=google | medium=cpc | campaign=learn-first" in activity.result
        assert "content=hero | term=ai course" in activity.result
        destinations = sorted(row.destination for row in session.scalars(select(CrmOutboxRow)))
        assert destinations == ["activity", "contacts", "telegram", "telegram"]


def test_form_telegram_receipt_is_bounded_and_replay_safe(intake_client) -> None:
    client, factory = intake_client
    assert _post(client, _payload()).status_code == 200
    payload = _telegram_payload(factory)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_owner_user_ids="111,222",
    )
    sends = []
    handler = telegram_receipt_handler(
        factory,
        settings,
        transport=lambda *args: sends.append(args),
    )

    assert handler(payload, False) == "confirmed"
    assert handler(payload, False) == "confirmed"
    assert handler(payload, True) == "confirmed"
    assert len(sends) == 1
    with factory() as session:
        claim = session.scalars(select(OwnerNotificationRecipientClaimRow)).one()
        assert claim.lead_id.startswith("form:")
        assert len(claim.lead_id) <= 40
        assert claim.notification_key.startswith("form-ping:")
        assert claim.delivery_status == "accepted"


def test_form_telegram_ambiguous_send_keeps_claim_and_never_retries(intake_client) -> None:
    client, factory = intake_client
    assert _post(client, _payload()).status_code == 200
    payload = _telegram_payload(factory)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_owner_user_ids="111,222",
    )
    attempts = []

    def uncertain(*args) -> None:
        attempts.append(args)
        raise TimeoutError("ambiguous transport result")

    handler = telegram_receipt_handler(factory, settings, transport=uncertain)
    assert handler(payload, False) == "unknown"
    assert handler(payload, False) == "unknown"
    assert handler(payload, True) == "unknown"
    assert len(attempts) == 1
    with factory() as session:
        claim = session.scalars(select(OwnerNotificationRecipientClaimRow)).one()
        assert len(claim.lead_id) <= 40
        assert claim.delivery_status == "pending"


def test_identical_retry_replays_without_any_new_rows(intake_client) -> None:
    client, factory = intake_client
    payload = _payload()
    first = _post(client, payload)
    normalized_retry = json.loads(json.dumps(payload))
    normalized_retry["submitted_at"] = "2026-10-08T06:30:00Z"
    normalized_retry["name"] = f"  {payload['name']}  "
    normalized_retry["attribution"]["utm_source"] = " google "
    second = _post(client, normalized_retry)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {
        "status": "replayed",
        "contact_id": first.json()["contact_id"],
        "activity_id": first.json()["activity_id"],
    }
    with factory() as session:
        assert _count(session, CrmFormIntakeReceiptRow) == 1
        assert _count(session, CrmActivityRow) == 1
        assert _count(session, CrmOutboxRow) == 4


def test_same_source_id_with_changed_payload_is_immutable_conflict(intake_client) -> None:
    client, factory = intake_client
    payload = _payload()
    assert _post(client, payload).status_code == 200
    changed = dict(payload, note="טקסט שונה")
    response = _post(client, changed)
    assert response.status_code == 409
    assert response.json() == {"detail": "source_id payload conflict"}
    with factory() as session:
        assert _count(session, CrmFormIntakeReceiptRow) == 1
        assert _count(session, CrmActivityRow) == 1
        assert _count(session, CrmOutboxRow) == 4


def test_future_email_identity_is_optional_and_normalized(intake_client) -> None:
    client, factory = intake_client
    response = _post(
        client,
        _payload(phone_e164="", email="Dana@Example.COM"),
    )
    assert response.status_code == 200, response.text
    with factory() as session:
        contact = session.scalars(select(CrmContactRow)).one()
        fields = json.loads(contact.fields_json)
        assert fields["phone"] == ""
        assert fields["email"] == "dana@example.com"


@pytest.mark.parametrize("submission_type", ["test", "internal"])
def test_nonproduction_submission_is_ignored_without_writes(
    intake_client, submission_type: str
) -> None:
    client, factory = intake_client
    response = _post(client, _payload(submission_type=submission_type))
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    with factory() as session:
        assert _count(session, CrmFormIntakeReceiptRow) == 0
        assert _count(session, CrmContactRow) == 0
        assert _count(session, CrmActivityRow) == 0
        assert _count(session, CrmOutboxRow) == 0


def test_public_form_preserves_existing_owner_contact_fields(intake_client) -> None:
    client, factory = intake_client
    with factory() as session:
        owner = CrmService(session).capture(
            {"name": "שם בעלים", "phone": "+972501234567", "status": "qualified"},
            writer=WRITER_OWNER,
            source_ref="owner:test",
        )
        owner_id = owner.contact.id
        session.commit()

    response = _post(client, _payload(name="שם תוקף", business="עסק תוקף"))
    assert response.status_code == 200
    assert response.json()["contact_id"] == owner_id
    with factory() as session:
        contact = session.get(CrmContactRow, owner_id)
        fields = json.loads(contact.fields_json)
        assert fields["name"] == "שם בעלים"
        assert fields["status"] == "qualified"
        assert fields["business"] == ""
        assert _count(session, CrmActivityRow) == 1
        assert _count(session, CrmFormIntakeReceiptRow) == 1


def test_identity_conflict_rolls_back_receipt_activity_and_issue(intake_client) -> None:
    client, factory = intake_client
    with factory() as session:
        CrmService(session).capture(
            {"phone": "+972501234567"}, writer=WRITER_OWNER, source_ref="owner:phone"
        )
        CrmService(session).capture(
            {"email": "other@example.com"}, writer=WRITER_OWNER, source_ref="owner:email"
        )
        session.commit()
    response = _post(client, _payload(email="other@example.com"))
    assert response.status_code == 409
    with factory() as session:
        assert _count(session, CrmFormIntakeReceiptRow) == 0
        assert _count(session, CrmActivityRow) == 0
        assert _count(session, CrmIssueRow) == 0


def test_schema_is_closed_and_shared_secret_route_never_redirects(intake_client) -> None:
    client, _factory = intake_client
    response = _post(client, _payload(unexpected="value"))
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid form lead"}

    trailing = client.post(
        "/v1/internal/assafweb/form-leads/",
        json=_payload(),
        headers={"X-AssafWeb-Intake-Secret": SECRET},
        follow_redirects=False,
    )
    assert trailing.status_code == 404
    assert "location" not in trailing.headers


@pytest.mark.skipif(not os.getenv("MIA_TEST_POSTGRES_URL"), reason="test PostgreSQL DSN not set")
def test_postgres_concurrent_retry_creates_one_capture(monkeypatch) -> None:
    """Opt-in proof of the production advisory-lock path on an isolated schema."""
    url = os.environ["MIA_TEST_POSTGRES_URL"]
    schema = "mia_form_intake_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        root = Path(__file__).resolve().parents[2]
        paths = [
            root / "migrations" / "20260910_crm_v2.sql",
            root / "migrations" / "20261008_assafweb_form_intake_receipts.sql",
        ]
        monkeypatch.setattr("app.db.migrate.list_migration_files", lambda: paths)
        migration = apply_migrations(engine)
        assert not migration.failed
        assert migration.applied == [path.name for path in paths]
        source_id = str(uuid4())
        barrier = Barrier(2)

        def capture_once() -> str:
            with factory() as session:
                barrier.wait()
                result = FormIntakeService(session).capture(
                    source_id=source_id,
                    payload_sha256="a" * 64,
                    submitted_at="2026-10-08T06:30:00+00:00",
                    fields={"phone": "+972501234567", "source": "AssafWeb form / home"},
                    summary="bounded summary",
                    recipient_ids=(),
                )
                session.commit()
                return result.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(executor.map(lambda _index: capture_once(), range(2)))
        assert statuses == ["captured", "replayed"]
        with factory() as session:
            assert _count(session, CrmFormIntakeReceiptRow) == 1
            assert _count(session, CrmContactRow) == 1
            assert _count(session, CrmActivityRow) == 1
            assert _count(session, CrmOutboxRow) == 2
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
