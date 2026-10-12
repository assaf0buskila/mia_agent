from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.db.base import Base
from app.db.models import CrmActivityRow, CrmContactRow, CrmFormIntakeReceiptRow, CrmOutboxRow
from app.services.business_ledger_crm_projection import (
    CrmProjectionInvalid,
    build_test_crm_projection,
    build_unknown_test_crm_projection,
)
from app.services.form_intake import FormIntakePayloadConflict, FormIntakeService
from app.services.owner_os_reads import CursorCodec, OwnerOsReader, envelope, section
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 10, 12, 8, 0, 0, 123456, tzinfo=UTC)
WORKSPACE = "assaf-ledger-TEST"


def _reader(db) -> OwnerOsReader:
    reader = object.__new__(OwnerOsReader)
    reader.session = db
    reader.owner_id = "123456789"
    reader.workspace_id = "assafweb"
    reader.cursor = CursorCodec("projection-test-secret-at-least-thirty-two-bytes")
    reader.observed_at = NOW.isoformat().replace("+00:00", "Z")
    return reader


def _synthetic_contacts_envelope(reader: OwnerOsReader) -> dict[str, object]:
    page = reader.list_contacts(limit=50)
    result = envelope(
        reader,
        data=section(page, coverage_scope="stored_pg_contacts", first_page=True),
        coverage="complete",
        coverage_scope="stored_pg_contacts",
        pagination_value={"next_cursor": None, "has_more": False, "page_complete": True},
    )
    # The projection has no production caller. This explicit bit is set only by the
    # owned local synthetic harness after the canonical DTO has been produced.
    result["synthetic"] = True
    return result


def test_intake_replay_conflict_and_owner_dto_feed_minimal_projection() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    source_id = str(uuid4())
    fields = {
        "name": "דנה",
        "phone": "+972501234567",
        "email": "dana@example.test",
        "source": "AssafWeb form / home",
        "status": "new",
        "next_step": "לחזור לפונה מחר",
        "summary": "טקסט פרטי שאסור להשליך ל-Sheets",
    }
    try:
        with factory() as db:
            service = FormIntakeService(db)
            first = service.capture(
                source_id=source_id,
                payload_sha256="a" * 64,
                submitted_at="2026-10-12T08:00:00Z",
                fields=fields,
                summary="brief private visitor text",
                recipient_ids=(),
            )
            db.commit()
            replay = service.capture(
                source_id=source_id,
                payload_sha256="a" * 64,
                submitted_at="2026-10-12T08:00:00Z",
                fields=fields,
                summary="brief private visitor text",
                recipient_ids=(),
            )
            assert first.status == "captured"
            assert replay.status == "replayed"
            assert (replay.contact_id, replay.activity_id) == (first.contact_id, first.activity_id)
            with pytest.raises(FormIntakePayloadConflict):
                service.capture(
                    source_id=source_id,
                    payload_sha256="b" * 64,
                    submitted_at="2026-10-12T08:00:00Z",
                    fields=fields,
                    summary="changed",
                    recipient_ids=(),
                )
            db.rollback()
            assert db.scalar(select(func.count()).select_from(CrmFormIntakeReceiptRow)) == 1
            assert db.scalar(select(func.count()).select_from(CrmContactRow)) == 1
            assert db.scalar(select(func.count()).select_from(CrmActivityRow)) == 1
            assert db.scalar(select(func.count()).select_from(CrmOutboxRow)) == 2

            canonical = _synthetic_contacts_envelope(_reader(db))
            projection = build_test_crm_projection(canonical, workspace_id=WORKSPACE, now=NOW)
            assert projection["projection_status"] == "known"
            assert projection["projection_revision"] == 1
            assert projection["source"]["coverage"] == "complete"
            assert projection["source"]["observed_at"] == "2026-10-12T08:00:00.123Z"
            assert projection["contacts"] == [
                {
                    "contact_id": first.contact_id,
                    "revision": 1,
                    "name": "דנה",
                    "source_label": "AssafWeb form / home",
                    "next_action_state": "recorded",
                }
            ]
            serialized = json.dumps(projection, ensure_ascii=False)
            for forbidden in (
                "+972501234567",
                "dana@example.test",
                "טקסט פרטי",
                "brief private",
                "לחזור לפונה מחר",
                "amount_minor",
                "payment",
            ):
                assert forbidden not in serialized
    finally:
        engine.dispose()


def test_projection_requires_synthetic_fresh_complete_canonical_envelope() -> None:
    contact = CrmContactRow(
        id="crm_test_contact",
        revision=2,
        fields_json=json.dumps({"name": "דנה", "source": "synthetic", "next_step": "call"}),
        source_ref="private",
        conversation_id="private",
        created_at="2026-10-12T07:59:00Z",
        updated_at="2026-10-12T07:59:00Z",
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    CrmContactRow.__table__.create(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    try:
        with factory() as db:
            db.add(contact)
            db.commit()
            valid = _synthetic_contacts_envelope(_reader(db))
            assert build_test_crm_projection(valid, workspace_id=WORKSPACE, now=NOW)["contacts"]
            cases = []
            nonsynthetic = dict(valid, synthetic=False)
            cases.append(nonsynthetic)
            partial = dict(valid, coverage="partial")
            cases.append(partial)
            stale = dict(
                valid, observed_at=(NOW - timedelta(seconds=301)).isoformat().replace("+00:00", "Z")
            )
            cases.append(stale)
            bad_item = json.loads(json.dumps(valid))
            bad_item["data"]["items"][0]["visitor_text"] = "must reject"
            cases.append(bad_item)
            for candidate in cases:
                with pytest.raises(CrmProjectionInvalid):
                    build_test_crm_projection(candidate, workspace_id=WORKSPACE, now=NOW)
    finally:
        engine.dispose()


def test_unknown_projection_is_explicit_and_cas_bound() -> None:
    unknown = build_unknown_test_crm_projection(
        workspace_id=WORKSPACE,
        reason_code="source_unavailable",
        observed_at=NOW,
    )
    assert unknown["projection_status"] == "unknown"
    assert unknown["contacts"] == []
    assert unknown["source"]["coverage"] == "unknown"
    assert unknown["reason_code"] == "source_unavailable"
    assert unknown["expected_snapshot_hash"] is None
    with pytest.raises(CrmProjectionInvalid):
        build_unknown_test_crm_projection(
            workspace_id=WORKSPACE,
            reason_code="source_unavailable",
            expected_snapshot_hash=None,
            expected_projection_revision=1,
            observed_at=NOW,
        )
