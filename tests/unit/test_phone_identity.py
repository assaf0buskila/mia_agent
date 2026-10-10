from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.db.base import Base
from app.db.models import CrmActivityRow, CrmContactRow, CrmIdentityRow, CrmIssueRow, CrmOutboxRow
from app.db.session import sqlalchemy_database_url
from app.services.crm_v2 import (
    CONTACT_FIELDS,
    WRITER_OWNER,
    WRITER_PUBLIC,
    CrmError,
    CrmPhoneNormalizationRequired,
    CrmService,
    normalize_phone,
)
from app.services.form_intake import FormIntakeCaptureConflict, FormIntakeService
from app.services.phone_identity import normalize_new_input_phone
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0501234567", "+972501234567"),
        ("050-123-4567", "+972501234567"),
        ("+972 50 123 4567", "+972501234567"),
        ("052.765.4321", "+972527654321"),
        ("+1 (202) 555-0123", "+12025550123"),
        ("+44 20 7946 0958", "+442079460958"),
        ("+97222345678", "+97222345678"),
        ("022345678", "022345678"),
        # A bare country prefix is deliberately not guessed to be international.
        ("972501234567", "972501234567"),
    ],
)
def test_new_input_comparison_keys(value, expected):
    assert normalize_new_input_phone(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "050123456",
        "05012345678",
        "+9720501234567",
        "+97250123456",
        "+9725012345678",
        "++972501234567",
        "0501234567 ext 12",
        "call 0501234567",
        "0501234567\n0527654321",
        "050１２３４５６７",
        "+0123456789",
        "123",
        "+120255501234567890",
        "050(1234567",
    ],
)
def test_invalid_new_input_does_not_invent_identity(value):
    assert normalize_new_input_phone(value) == ""


def test_default_flag_and_legacy_normalizer_unchanged(monkeypatch):
    monkeypatch.delenv("MIA_CRM_ISRAELI_PHONE_NORMALIZATION_ENABLED", raising=False)
    assert Settings(_env_file=None).crm_israeli_phone_normalization_enabled is False
    assert normalize_phone("050-123-4567") == "0501234567"
    assert normalize_phone("+972 50 123 4567") == "+972501234567"


def test_enabled_policy_preserves_original_fields_and_frozen_identity(sessions):
    with sessions() as session:
        service = CrmService(session, normalize_israeli_phones=True)
        first = service.capture_site_lead(
            {"phone": "0501234567", "name": "original", "business": "private"},
            conversation_id="first-session",
            source_ref="site:first:event-1",
            summary="private text",
            recipient_ids=(),
        )
        session.commit()
        contact_id = first.contact.id
        identity = session.scalars(select(CrmIdentityRow)).one()
        identity_id = identity.id
        stored = session.get(CrmContactRow, contact_id).fields_json
        second = service.capture_site_lead(
            {"phone": "+972501234567", "name": "replacement"},
            conversation_id="another-session",
            source_ref="site:another:event-1",
            summary="another private text",
            recipient_ids=(),
        )
        session.commit()
        assert second.contact.id == contact_id
        assert session.get(CrmContactRow, contact_id).fields_json == stored
        assert json.loads(stored)["phone"] == "0501234567"
        identity = session.scalars(select(CrmIdentityRow)).one()
        assert identity.id == identity_id
        assert identity.normalized_value == "+972501234567"
        assert session.scalar(select(func.count()).select_from(CrmContactRow)) == 1
        assert session.scalar(select(func.count()).select_from(CrmActivityRow)) == 2


def test_enabled_normalizer_validates_before_legacy_digit_stripping(sessions):
    with sessions() as session:
        with pytest.raises(CrmError, match="invalid phone"):
            CrmService(session, normalize_israeli_phones=True).capture(
                {"phone": "call 0501234567"},
                writer=WRITER_PUBLIC,
                conversation_id="session",
                source_ref="event",
            )
        assert session.scalar(select(func.count()).select_from(CrmContactRow)) == 0


def test_historical_local_keys_block_activation_without_mutation(sessions):
    with sessions() as session:
        old = CrmService(session, normalize_israeli_phones=False).capture(
            {"phone": "0501234567"},
            writer=WRITER_OWNER,
            source_ref="old-event",
        )
        session.commit()
        original_fields = session.get(CrmContactRow, old.contact.id).fields_json
        original_identities = [
            (row.id, row.contact_id, row.normalized_value)
            for row in session.scalars(select(CrmIdentityRow))
        ]
        with pytest.raises(CrmPhoneNormalizationRequired, match="reviewed identity migration"):
            CrmService(session, normalize_israeli_phones=True).capture(
                {"phone": "+972501234567"},
                writer=WRITER_PUBLIC,
                conversation_id="new-session",
                source_ref="new-event",
            )
        assert session.get(CrmContactRow, old.contact.id).fields_json == original_fields
        assert [
            (row.id, row.contact_id, row.normalized_value)
            for row in session.scalars(select(CrmIdentityRow))
        ] == original_identities
        assert session.scalar(select(func.count()).select_from(CrmContactRow)) == 1


@pytest.mark.parametrize("legacy_key", ["", "invalid-phone"])
def test_invalid_persisted_identity_also_blocks_activation(sessions, legacy_key):
    with sessions() as session:
        prior = CrmService(session, normalize_israeli_phones=False).capture(
            {"email": "identity@example.test"}, writer=WRITER_OWNER, source_ref="prior"
        )
        session.add(
            CrmIdentityRow(
                id="identity_" + uuid4().hex,
                kind="phone",
                normalized_value=legacy_key,
                contact_id=prior.contact.id,
                source_ref="prior",
                created_at="2026-10-10T12:00:00+00:00",
            )
        )
        session.commit()
        with pytest.raises(CrmPhoneNormalizationRequired):
            CrmService(session, normalize_israeli_phones=True).capture(
                {"phone": "+972501234567"}, writer=WRITER_OWNER, source_ref="new"
            )
        session.rollback()
        assert session.scalar(select(func.count()).select_from(CrmContactRow)) == 1


def test_conflicting_canonical_identities_remain_explicit_conflicts(sessions):
    with sessions() as session:
        service = CrmService(session, normalize_israeli_phones=True)
        first = service.capture(
            {"phone": "+972501234567"},
            writer=WRITER_OWNER,
            source_ref="phone-contact",
        )
        second = service.capture(
            {"email": "different@example.test"},
            writer=WRITER_OWNER,
            source_ref="email-contact",
        )
        session.commit()
        prior = {row.id: row.fields_json for row in session.scalars(select(CrmContactRow))}
        result = service.capture(
            {"phone": "0501234567", "email": "different@example.test"},
            writer=WRITER_PUBLIC,
            conversation_id="untrusted",
            source_ref="conflict-event",
        )
        assert result.status == "conflict"
        assert result.contact is None
        assert result.issue_ids
        assert {row.id: row.fields_json for row in session.scalars(select(CrmContactRow))} == prior
        assert first.contact.id != second.contact.id


def test_phone_conflict_resolution_uses_canonical_collision_key(sessions):
    with sessions() as session:
        service = CrmService(session, normalize_israeli_phones=True)
        first = service.capture({"phone": "+972501234567"}, writer=WRITER_OWNER, source_ref="first")
        second = service.capture(
            {"phone": "+972527654321"}, writer=WRITER_OWNER, source_ref="second"
        )
        issue = CrmIssueRow(
            id="issue_" + uuid4().hex,
            contact_id=second.contact.id,
            issue_type="field_conflict",
            field_name="phone",
            database_value="+972527654321",
            sheet_value="0501234567",
            status="open",
            created_at="2026-10-10T12:00:00+00:00",
        )
        session.add(issue)
        session.commit()
        original = session.get(CrmContactRow, second.contact.id).fields_json
        with pytest.raises(CrmError, match="belongs to another contact"):
            service.resolve_conflict(
                issue.id, resolution="sheet", expected_contact_revision=second.contact.revision
            )
        session.rollback()
        assert session.get(CrmIssueRow, issue.id).status == "open"
        assert session.get(CrmContactRow, second.contact.id).fields_json == original
        assert {
            (row.contact_id, row.normalized_value)
            for row in session.scalars(select(CrmIdentityRow))
        } == {(first.contact.id, "+972501234567"), (second.contact.id, "+972527654321")}


def test_sheet_import_cannot_migrate_existing_phone_identities(sessions):
    with sessions() as session:
        old = CrmService(session, normalize_israeli_phones=False).capture(
            {"phone": "0501234567"}, writer=WRITER_OWNER, source_ref="historical"
        )
        session.commit()
        before = session.get(CrmContactRow, old.contact.id).fields_json
        cells = [old.contact.fields.get(name, "") for name in CONTACT_FIELDS] + [old.contact.id]
        with pytest.raises(CrmPhoneNormalizationRequired):
            CrmService(session, normalize_israeli_phones=True).import_sheet_contact(
                cells, row_number=2
            )
        session.rollback()
        assert session.get(CrmContactRow, old.contact.id).fields_json == before
        assert [
            (row.contact_id, row.normalized_value)
            for row in session.scalars(select(CrmIdentityRow))
        ] == [(old.contact.id, "0501234567")]


def test_enabled_sheet_import_raises_explicit_equivalent_phone_conflict(sessions):
    with sessions() as session:
        service = CrmService(session, normalize_israeli_phones=True)
        first = service.capture({"phone": "+972501234567"}, writer=WRITER_OWNER, source_ref="first")
        second = service.capture(
            {"phone": "+972527654321"}, writer=WRITER_OWNER, source_ref="second"
        )
        session.commit()
        before = session.get(CrmContactRow, second.contact.id).fields_json
        fields = dict(second.contact.fields, phone="0501234567")
        result = service.import_sheet_contact(
            [fields.get(name, "") for name in CONTACT_FIELDS] + [second.contact.id], row_number=2
        )
        session.commit()
        assert result.status == "conflict"
        assert result.issue_ids
        assert session.get(CrmContactRow, second.contact.id).fields_json == before
        assert session.scalar(select(func.count()).select_from(CrmContactRow)) == 2
        assert {
            (row.contact_id, row.normalized_value)
            for row in session.scalars(select(CrmIdentityRow))
        } == {(first.contact.id, "+972501234567"), (second.contact.id, "+972527654321")}


def test_resolved_phone_preserves_field_representation_and_canonical_key(sessions):
    with sessions() as session:
        service = CrmService(session, normalize_israeli_phones=True)
        captured = service.capture(
            {"phone": "+972527654321"}, writer=WRITER_OWNER, source_ref="first"
        )
        issue = CrmIssueRow(
            id="issue_" + uuid4().hex,
            contact_id=captured.contact.id,
            issue_type="field_conflict",
            field_name="phone",
            database_value="+972527654321",
            sheet_value="0501234567",
            status="open",
            created_at="2026-10-10T12:00:00+00:00",
        )
        session.add(issue)
        session.commit()
        service.resolve_conflict(issue.id, resolution="sheet")
        session.commit()
        assert (
            json.loads(session.get(CrmContactRow, captured.contact.id).fields_json)["phone"]
            == "0501234567"
        )
        assert session.scalars(select(CrmIdentityRow)).one().normalized_value == "+972501234567"


def test_repeated_intake_reuses_receipt_activity_and_jobs(sessions, monkeypatch):
    monkeypatch.setenv("MIA_CRM_ISRAELI_PHONE_NORMALIZATION_ENABLED", "true")
    with sessions() as session:
        intake = FormIntakeService(session)
        kwargs = dict(
            source_id=str(uuid4()),
            payload_sha256="a" * 64,
            submitted_at="2026-10-10T12:00:00+00:00",
            fields={"phone": "+972501234567"},
            summary="private text",
            recipient_ids=("123",),
        )
        first = intake.capture(**kwargs)
        session.commit()
        before_jobs = list(session.scalars(select(CrmOutboxRow.id)))
        second = intake.capture(**kwargs)
        session.commit()
        assert second.status == "replayed"
        assert (second.contact_id, second.activity_id) == (first.contact_id, first.activity_id)
        assert list(session.scalars(select(CrmOutboxRow.id))) == before_jobs


def test_intake_migration_block_is_a_conflict_not_new_contact(sessions, monkeypatch):
    with sessions() as session:
        CrmService(session, normalize_israeli_phones=False).capture(
            {"phone": "0501234567"},
            writer=WRITER_OWNER,
            source_ref="old-event",
        )
        session.commit()
        monkeypatch.setenv("MIA_CRM_ISRAELI_PHONE_NORMALIZATION_ENABLED", "true")
        with pytest.raises(FormIntakeCaptureConflict):
            FormIntakeService(session).capture(
                source_id=str(uuid4()),
                payload_sha256="a" * 64,
                submitted_at="2026-10-10T12:00:00+00:00",
                fields={"phone": "+972501234567"},
                summary="",
                recipient_ids=(),
            )
        session.rollback()
        assert session.scalar(select(func.count()).select_from(CrmContactRow)) == 1


@contextmanager
def _postgres_sessions():
    url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("MIA_TEST_POSTGRES_URL not configured; disposable PostgreSQL required")
    admin = create_engine(sqlalchemy_database_url(url))
    schema = "p0a_phone_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        sqlalchemy_database_url(url),
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_postgres_concurrent_local_and_international_events_resolve_one_contact():
    with _postgres_sessions() as factory:
        barrier = Barrier(2)

        def capture(index):
            barrier.wait(timeout=10)
            with factory() as session:
                result = CrmService(session, normalize_israeli_phones=True).capture_site_lead(
                    {"phone": ("0501234567", "+972501234567")[index]},
                    conversation_id=f"session-{index}",
                    source_ref=f"event-{index}",
                    summary="private text",
                    recipient_ids=("123",),
                )
                session.commit()
                return result.contact.id

        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(capture, range(2)))
        assert ids[0] == ids[1]
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(CrmContactRow)) == 1
            assert session.scalar(select(func.count()).select_from(CrmIdentityRow)) == 1
            assert session.scalar(select(func.count()).select_from(CrmActivityRow)) == 2
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(CrmOutboxRow)
                    .where(CrmOutboxRow.destination == "telegram")
                )
                == 2
            )
