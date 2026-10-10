from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    CrmActivityRow,
    CrmContactRow,
    CrmFormIntakeReceiptRow,
    CrmIdentityRow,
    CrmIssueContactRow,
    CrmIssueRow,
    CrmOutboxRow,
    CrmSyncSnapshotRow,
    CrmWorkerStateRow,
    OwnerNotificationRecipientClaimRow,
)
from app.domain.handoff.delivery import (
    KIND_HOT_LEAD_LEGACY,
    KIND_WEBSITE_HANDOFF_DELIVERY,
    form_ping_scope,
)
from app.integrations.sheets import FakeSheetsPort
from app.services.crm_diagnostics import build_crm_diagnostics
from app.services.phone_identity import normalize_new_input_phone
from app.tools.owner.crm import _crm_operational_health
from app.tools.registries.owner_tools import get_tool
from app.workers.crm_delivery import CrmDeliveryWorker
from scripts.crm_reconcile_readonly import main as diagnostics_main
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
CONTACT_ID = "crm_" + "a" * 32
ACTIVITY_ID = "activity_" + "b" * 32


def test_observed_sheet_only_ids_are_reported_even_with_partial_sheet_coverage() -> None:
    _engine, factory = _database()
    contact_orphan = "crm_" + "f" * 32
    activity_orphan = "activity_" + "f" * 32
    sheets = FakeSheetsPort()
    sheets.locked_contacts = [[""] * 14 + [contact_orphan]]
    sheets.locked_activity = [[""] * 5 + [activity_orphan]]
    with factory() as session:
        _seed(session)
        session.commit()
        report = build_crm_diagnostics(session, sheets=sheets, now=NOW, max_rows=2)
    assert report["sheets"]["contacts"]["complete"] is False
    assert report["reconciliation"]["sheet_only_contact_ids"] == [contact_orphan]
    assert report["reconciliation"]["sheet_only_activity_ids"] == [activity_orphan]
    assert report["reconciliation"]["missing_claims_are_uncertain"] is True
    assert report["reconciliation"]["missing_activity_ids"] == []


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_primary_notification_without_claim_still_identifies_affected_lead(status) -> None:
    _engine, factory = _database()
    source_id = "66666666-6666-6666-6666-666666666666"
    with factory() as session:
        _seed(session)
        activity = session.get(CrmActivityRow, ACTIVITY_ID)
        activity.channel = "form"
        activity.source_ref = f"assafweb-form:{source_id}:activity"
        session.add_all(
            [
                CrmFormIntakeReceiptRow(
                    source_id=source_id,
                    payload_sha256="f" * 64,
                    contact_id=CONTACT_ID,
                    activity_id=ACTIVITY_ID,
                    created_at=NOW.isoformat(),
                ),
                CrmOutboxRow(
                    id="job_" + "d" * 32,
                    dedupe_key="primary",
                    aggregate_type="contact",
                    aggregate_id=CONTACT_ID,
                    destination="telegram",
                    payload_json=json.dumps(
                        {"recipient_id": "12345", "conversation_id": f"form:{source_id}"}
                    ),
                    status=status,
                    created_at=NOW.isoformat(),
                ),
            ]
        )
        session.commit()
        report = build_crm_diagnostics(session, now=NOW, recipient_ids={"12345"})
    assert report["notifications"]["not_attempted"] == 1
    assert report["notifications"]["receipts_confirmed"] == 0
    assert report["notifications"]["attention_contact_ids"] == [CONTACT_ID]
    assert report["notifications"]["attention_activity_ids"] == [ACTIVITY_ID]
    assert report["notifications"]["job_status_counts"][status] == 1


def test_notification_representatives_and_unsupported_sources_are_deterministic() -> None:
    _engine, factory = _database()
    conversation = "77777777-7777-7777-7777-777777777777"
    with factory() as session:
        _seed(session)
        first = session.get(CrmActivityRow, ACTIVITY_ID)
        first.source_ref = f"site:{conversation}:first:activity"
        for index in range(1, 4):
            session.add(
                CrmActivityRow(
                    id="activity_" + str(index) * 32,
                    contact_id=CONTACT_ID,
                    occurred_at=NOW.isoformat(),
                    who="",
                    channel="website",
                    action="contact_captured",
                    result="",
                    source_ref=(
                        f"site:{conversation}:later:activity"
                        if index == 1
                        else f"unsupported:{index}"
                    ),
                    created_at=NOW.isoformat(),
                )
            )
        session.commit()
        before = build_crm_diagnostics(session, now=NOW, recipient_ids={"12345"})
        session.execute(text("PRAGMA reverse_unordered_selects = ON"))
        after = build_crm_diagnostics(session, now=NOW, recipient_ids={"12345"})
    assert before == after
    assert after["notifications"]["attention_activity_ids"] == [ACTIVITY_ID]
    assert after["notifications"]["unsupported_source_scope_count"] == 2


def _database():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed(session: Session) -> None:
    fields = {
        "name": "private name",
        "phone": "0501234567",
        "email": "private@example.com",
        "status": "new",
    }
    session.add(
        CrmContactRow(
            id=CONTACT_ID,
            revision=3,
            fields_json=json.dumps(fields),
            source_ref="private source",
            conversation_id="private conversation",
            created_at="2026-10-10T08:00:00+00:00",
            updated_at="2026-10-10T09:00:00+00:00",
        )
    )
    session.add(
        CrmActivityRow(
            id=ACTIVITY_ID,
            contact_id=CONTACT_ID,
            occurred_at="2026-10-10T09:00:00+00:00",
            who="private",
            channel="website",
            action="contact_captured",
            result="private result",
            source_ref="form:11111111-1111-1111-1111-111111111111",
            created_at="2026-10-10T09:00:00+00:00",
        )
    )
    for field_name in ("name", "phone", "email", "status"):
        session.add(
            CrmSyncSnapshotRow(
                contact_id=CONTACT_ID,
                field_name=field_name,
                value=fields[field_name],
                contact_revision=2,
                sheet_row=2,
                synced_at="2026-10-10T08:30:00+00:00",
            )
        )
    session.add(
        CrmIdentityRow(
            id="identity_" + "1" * 32,
            contact_id=CONTACT_ID,
            kind="phone",
            normalized_value="0501234567",
            source_ref="private",
            created_at="2026-10-10T08:00:00+00:00",
        )
    )
    session.commit()


def test_diagnostics_are_select_only_and_do_not_mutate_loaded_rows() -> None:
    engine, factory = _database()
    with factory() as session:
        _seed(session)
        contact = session.get(CrmContactRow, CONTACT_ID)
        before = dict(contact.__dict__)
        statements: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):  # noqa: ANN001
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(engine, "before_cursor_execute", capture)
        report = build_crm_diagnostics(
            session,
            now=NOW,
            phone_normalizer=normalize_new_input_phone,
        )
        event.remove(engine, "before_cursor_execute", capture)

        assert statements and set(statements) == {"SELECT"}
        assert dict(contact.__dict__) == before
        assert report["read_only"] is True
        assert report["phone_normalization_readiness"]["legacy_keys_requiring_migration"] == 1
        difference = report["reconciliation"]["contact_differences"][0]
        assert difference["snapshot_revision_drift"] is True
        assert difference["snapshot_revisions"] == [2]


def test_report_never_contains_private_or_malicious_metadata_and_groups_duplicates() -> None:
    _engine, factory = _database()
    secret = "050-SECRET-private@example.com"
    with factory() as session:
        _seed(session)
        for suffix in ("1", "2"):
            issue = CrmIssueRow(
                id="issue_" + suffix * 32,
                contact_id=CONTACT_ID,
                issue_type=secret,
                field_name="phone",
                base_value=secret,
                database_value=secret,
                sheet_value=secret,
                details_json=json.dumps({"source_ref": secret}),
                status="open",
                created_at=secret,
            )
            session.add(issue)
            session.flush()
            session.add(CrmIssueContactRow(issue_id=issue.id, contact_id=CONTACT_ID))
        session.commit()

        report = build_crm_diagnostics(session, now=NOW)
        rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
        assert secret not in rendered
        assert "private name" not in rendered
        assert "private@example.com" not in rendered
        assert report["issues"]["raw_rows"] == 2
        assert report["issues"]["distinct_unresolved_observations"] == 1
        assert report["issues"]["groups"][0]["duplicate_row_count"] == 2
        assert report["issues"]["groups"][0]["issue_type"] == "unknown_issue"


class _FailingPagedSheets(FakeSheetsPort):
    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        if start_row > 2:
            raise RuntimeError("provider secret")
        return [["private"] * 14 + [f"bad-{index}"] for index in range(limit)]

    def read_crm_activity_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        raise RuntimeError("provider secret")


class _ExtentGapSheets(FakeSheetsPort):
    def crm_sheet_row_extent(self, *, sheet_kind: str) -> int:
        return 102 if sheet_kind == "contacts" else 1

    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        if start_row == 2:
            return []
        if start_row == 102:
            return [[""] * 14 + [CONTACT_ID]]
        return []


class _ObservedPartialSheets(FakeSheetsPort):
    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        del limit
        return [["changed"] + [""] * 13 + [CONTACT_ID]] if start_row == 2 else []

    def read_crm_activity_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        del limit
        if start_row != 2:
            return []
        row = ["changed", "", "", "", "", ACTIVITY_ID]
        return [row, list(row)]


def test_sheet_reader_does_not_stop_at_empty_range_before_row_102() -> None:
    _engine, factory = _database()
    with factory() as session:
        _seed(session)
        report = build_crm_diagnostics(
            session,
            sheets=_ExtentGapSheets(),
            now=NOW,
            max_rows=150,
        )
    assert report["sheets"]["contacts"]["status"] == "complete"
    assert report["sheets"]["contacts"]["ranges_read"] == 2
    assert report["sheets"]["contacts"]["rows_seen"] == 1
    assert all(
        item["sheet_state"] != "missing" for item in report["reconciliation"]["contact_differences"]
    )


def test_bounded_sheet_failure_is_uncertain_and_never_claims_missing() -> None:
    _engine, factory = _database()
    with factory() as session:
        _seed(session)
        report = build_crm_diagnostics(
            session,
            sheets=_FailingPagedSheets(),
            now=NOW,
            max_rows=150,
        )
    assert report["sheets"]["contacts"]["status"] == "read_failed"
    assert report["sheets"]["contacts"]["rows_seen"] == 100
    assert report["sheets"]["activity"]["status"] == "read_failed"
    assert report["reconciliation"]["missing_activity_ids"] == []
    assert report["reconciliation"]["missing_claims_are_uncertain"] is True
    assert all(
        difference["sheet_state"] == "unavailable"
        for difference in report["reconciliation"]["contact_differences"]
    )


def test_partial_sheet_scan_keeps_observed_drift_and_duplicate_evidence() -> None:
    _engine, factory = _database()
    with factory() as session:
        _seed(session)
        report = build_crm_diagnostics(
            session,
            sheets=_ObservedPartialSheets(),
            now=NOW,
            max_rows=10,
        )
    contact = report["reconciliation"]["contact_differences"][0]
    activity = report["reconciliation"]["activity_differences"][0]
    assert report["sheets"]["contacts"]["complete"] is False
    assert contact["sheet_state"] == "present"
    assert "name" in contact["database_vs_sheet_fields"]
    assert contact["uncertain"] is True
    assert activity["sheet_state"] == "duplicate"
    assert "occurred_at" in activity["database_vs_sheet_fields"]
    assert activity["uncertain"] is True


def test_form_receipt_and_notification_receipt_coverage() -> None:
    _engine, factory = _database()
    source_id = "11111111-1111-1111-1111-111111111111"
    recipient = "12345"
    conversation = f"form:{source_id}"
    lead_id, key = form_ping_scope(conversation)
    with factory() as session:
        _seed(session)
        activity = session.get(CrmActivityRow, ACTIVITY_ID)
        activity.channel = "form"
        activity.source_ref = f"assafweb-form:{source_id}:activity"
        session.add(
            CrmFormIntakeReceiptRow(
                source_id=source_id,
                payload_sha256="c" * 64,
                contact_id=CONTACT_ID,
                activity_id=ACTIVITY_ID,
                created_at=NOW.isoformat(),
            )
        )
        session.add(
            OwnerNotificationRecipientClaimRow(
                kind=KIND_WEBSITE_HANDOFF_DELIVERY,
                lead_id=lead_id,
                notification_key=key,
                recipient_id=recipient,
                claimed_at=NOW.isoformat(),
                delivery_status="accepted",
            )
        )
        session.add(
            CrmOutboxRow(
                id="job_" + "d" * 32,
                dedupe_key="telegram:one",
                aggregate_type="contact",
                aggregate_id=CONTACT_ID,
                destination="telegram",
                payload_json=json.dumps(
                    {"recipient_id": recipient, "conversation_id": conversation, "text": "private"}
                ),
                status="confirmed",
                created_at=NOW.isoformat(),
            )
        )
        session.add(
            CrmOutboxRow(
                id="job_" + "e" * 32,
                dedupe_key="telegram:two",
                aggregate_type="contact",
                aggregate_id=CONTACT_ID,
                destination="telegram",
                payload_json=json.dumps(
                    {"recipient_id": "67890", "conversation_id": conversation, "text": "private"}
                ),
                status="unknown",
                created_at=NOW.isoformat(),
            )
        )
        session.commit()
        report = build_crm_diagnostics(
            session,
            now=NOW,
            recipient_ids={recipient, "67890"},
        )

    assert report["form_intake"]["receipts"] == 1
    assert report["form_intake"]["valid_correspondence"] == 1
    assert report["form_intake"]["semantic_mismatch"] == 0
    assert report["notifications"]["expected_intents"] == 2
    assert report["notifications"]["receipts_confirmed"] == 1
    assert report["notifications"]["receipts_missing"] == 1


def test_expected_notification_is_missing_even_when_outbox_job_was_deleted() -> None:
    _engine, factory = _database()
    source_id = "22222222-2222-2222-2222-222222222222"
    with factory() as session:
        _seed(session)
        activity = session.get(CrmActivityRow, ACTIVITY_ID)
        activity.channel = "form"
        activity.source_ref = f"assafweb-form:{source_id}:activity"
        session.add(
            CrmFormIntakeReceiptRow(
                source_id=source_id,
                payload_sha256="d" * 64,
                contact_id=CONTACT_ID,
                activity_id=ACTIVITY_ID,
                created_at=NOW.isoformat(),
            )
        )
        session.commit()
        report = build_crm_diagnostics(
            session,
            now=NOW,
            recipient_ids={"12345"},
        )
    assert report["notifications"]["expected_intents"] == 1
    assert report["notifications"]["jobs_missing"] == 1
    assert report["notifications"]["not_attempted"] == 1
    assert report["notifications"]["receipts_confirmed"] == 0
    assert report["notifications"]["attention_contact_ids"] == [CONTACT_ID]
    assert report["notifications"]["attention_activity_ids"] == [ACTIVITY_ID]


def test_site_capture_activity_independently_creates_expected_notification() -> None:
    _engine, factory = _database()
    conversation = "55555555-5555-5555-5555-555555555555"
    with factory() as session:
        _seed(session)
        activity = session.get(CrmActivityRow, ACTIVITY_ID)
        activity.source_ref = f"site:{conversation}:message-1:activity"
        session.commit()
        report = build_crm_diagnostics(
            session,
            now=NOW,
            recipient_ids={"12345"},
        )
    assert report["notifications"]["expected_intents"] == 1
    assert report["notifications"]["jobs_missing"] == 1


def test_unparseable_site_capture_scope_is_explicitly_unknown() -> None:
    _engine, factory = _database()
    with factory() as session:
        _seed(session)
        activity = session.get(CrmActivityRow, ACTIVITY_ID)
        activity.source_ref = "unsupported-private-source"
        session.commit()
        report = build_crm_diagnostics(session, now=NOW, recipient_ids={"12345"})
    assert report["notifications"]["expected_intents"] == 0
    assert report["notifications"]["unsupported_source_scope_count"] == 1
    assert report["notifications"]["unsupported_source_activity_ids"] == [ACTIVITY_ID]
    assert report["notifications"]["coverage_scope"] == "primary_handoff_only"
    assert report["notifications"]["update_intent_measurement"] == "unmeasured"


def test_legacy_notification_claim_is_conservatively_unknown() -> None:
    _engine, factory = _database()
    source_id = "33333333-3333-3333-3333-333333333333"
    conversation = f"form:{source_id}"
    lead_id, key = form_ping_scope(conversation)
    with factory() as session:
        _seed(session)
        activity = session.get(CrmActivityRow, ACTIVITY_ID)
        activity.channel = "form"
        activity.source_ref = f"assafweb-form:{source_id}:activity"
        session.add_all(
            [
                CrmFormIntakeReceiptRow(
                    source_id=source_id,
                    payload_sha256="e" * 64,
                    contact_id=CONTACT_ID,
                    activity_id=ACTIVITY_ID,
                    created_at=NOW.isoformat(),
                ),
                OwnerNotificationRecipientClaimRow(
                    kind=KIND_HOT_LEAD_LEGACY,
                    lead_id=lead_id,
                    notification_key=key,
                    recipient_id="12345",
                    claimed_at=NOW.isoformat(),
                    delivery_status="accepted",
                ),
            ]
        )
        session.commit()
        report = build_crm_diagnostics(
            session,
            now=NOW,
            recipient_ids={"12345"},
        )
    assert report["notifications"]["legacy_claims_unknown"] == 1
    assert report["notifications"]["receipts_confirmed"] == 0


def test_form_receipt_requires_semantically_matching_activity() -> None:
    _engine, factory = _database()
    source_id = "44444444-4444-4444-4444-444444444444"
    with factory() as session:
        _seed(session)
        session.add(
            CrmFormIntakeReceiptRow(
                source_id=source_id,
                payload_sha256="f" * 64,
                contact_id=CONTACT_ID,
                activity_id=ACTIVITY_ID,
                created_at=NOW.isoformat(),
            )
        )
        session.commit()
        report = build_crm_diagnostics(session, now=NOW)
    assert report["form_intake"]["receipts_missing_activity"] == 0
    assert report["form_intake"]["semantic_mismatch"] == 1
    assert report["form_intake"]["valid_correspondence"] == 0


def test_full_aggregates_surface_rows_after_five_thousand_historical_rows() -> None:
    _engine, factory = _database()
    with factory() as session:
        _seed(session)
        session.bulk_insert_mappings(
            CrmIssueRow,
            [
                {
                    "id": f"issue_{index:032x}",
                    "contact_id": CONTACT_ID,
                    "issue_type": "field_conflict",
                    "field_name": "status",
                    "base_value": "",
                    "database_value": "private",
                    "sheet_value": "private",
                    "details_json": "{}",
                    "status": "resolved",
                    "resolution": "database",
                    "created_at": f"2026-01-01T00:00:{index % 60:02d}+00:00",
                    "resolved_at": "2026-01-02T00:00:00+00:00",
                }
                for index in range(5_001)
            ],
        )
        session.add(
            CrmIssueRow(
                id="issue_" + "f" * 32,
                contact_id=CONTACT_ID,
                issue_type="missing_sheet_row",
                status="open",
                created_at="2026-10-10T09:59:00+00:00",
            )
        )
        session.bulk_insert_mappings(
            CrmOutboxRow,
            [
                {
                    "id": f"job_{index:032x}",
                    "dedupe_key": f"historical:{index}",
                    "aggregate_type": "contact",
                    "aggregate_id": CONTACT_ID,
                    "destination": "contacts",
                    "payload_json": "{}",
                    "status": "confirmed",
                    "attempts": 1,
                    "next_attempt_at": "",
                    "lease_owner": "",
                    "lease_expires_at": "",
                    "last_attempt_at": "",
                    "confirmed_at": "2026-01-01T00:00:00+00:00",
                    "last_error": "",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
                for index in range(5_001)
            ],
        )
        session.add(
            CrmOutboxRow(
                id="job_" + "f" * 32,
                dedupe_key="latest:pending",
                aggregate_type="contact",
                aggregate_id=CONTACT_ID,
                destination="contacts",
                payload_json="{}",
                status="pending",
                created_at="2026-10-10T09:00:00+00:00",
            )
        )
        session.commit()
        report = build_crm_diagnostics(session, now=NOW, max_rows=50)

    assert report["issues"]["raw_rows"] == 5_002
    assert report["issues"]["resolved_rows"] == 5_001
    assert report["issues"]["distinct_unresolved_observations"] == 1
    assert report["issues"]["groups"][0]["issue_type"] == "missing_sheet_row"
    assert report["issues"]["affected_contact_count"] == 1
    assert report["outbox"]["by_destination"]["contacts"]["confirmed"] == 5_001
    assert report["outbox"]["by_destination"]["contacts"]["pending"] == 1
    assert report["outbox"]["oldest_undelivered_age_seconds"]["contacts"] == 3_600


class _ImportFailureSheets(FakeSheetsPort):
    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        raise RuntimeError("unavailable")


def test_worker_records_success_and_failed_import_without_false_success() -> None:
    _engine, factory = _database()
    worker = CrmDeliveryWorker(
        session_factory=factory,
        sheets=FakeSheetsPort(),
        now=lambda: NOW,
    )
    result = worker.run_once(force_import=True)
    assert result.import_failed is False
    with factory() as session:
        assert session.get(CrmWorkerStateRow, "crm_worker_heartbeat").value == "completed"
        assert session.get(CrmWorkerStateRow, "crm_last_completed_cycle") is not None
        assert session.get(CrmWorkerStateRow, "crm_last_successful_cycle") is not None
        assert session.get(CrmWorkerStateRow, "crm_last_successful_delivery") is None
        successful_at = session.get(CrmWorkerStateRow, "crm_last_successful_cycle").updated_at

    failing = CrmDeliveryWorker(
        session_factory=factory,
        sheets=_ImportFailureSheets(),
        now=lambda: NOW,
    )
    result = failing.run_once(force_import=True)
    assert result.import_failed is True
    with factory() as session:
        assert (
            session.get(CrmWorkerStateRow, "crm_worker_heartbeat").value
            == "completed_with_failures"
        )
        assert (
            session.get(CrmWorkerStateRow, "crm_last_successful_cycle").updated_at == successful_at
        )


def test_telemetry_database_failure_is_swallowed() -> None:
    worker = CrmDeliveryWorker(
        session_factory=lambda: (_ for _ in ()).throw(RuntimeError("db unavailable")),
        sheets=FakeSheetsPort(),
        now=lambda: NOW,
    )
    worker._record_worker_state("crm_worker_heartbeat", "running")


def _seed_pending_activity_delivery(factory) -> None:  # noqa: ANN001
    with factory() as session:
        _seed(session)
        session.add(
            CrmWorkerStateRow(
                key="contacts_last_import",
                value=NOW.isoformat(),
                updated_at=NOW.isoformat(),
            )
        )
        session.add(
            CrmOutboxRow(
                id="job_" + "9" * 32,
                dedupe_key=f"activity:{ACTIVITY_ID}",
                aggregate_type="activity",
                aggregate_id=ACTIVITY_ID,
                destination="activity",
                payload_json=json.dumps(
                    {
                        "activity_id": ACTIVITY_ID,
                        "contact_id": CONTACT_ID,
                        "cells": [
                            "2026-10-10T09:00:00+00:00",
                            "private",
                            "website",
                            "contact_captured",
                            "private result",
                            ACTIVITY_ID,
                        ],
                    }
                ),
                status="pending",
                next_attempt_at=NOW.isoformat(),
                created_at=NOW.isoformat(),
            )
        )
        session.commit()


def test_confirmed_sheet_delivery_records_delivery_and_projection_telemetry() -> None:
    _engine, factory = _database()
    _seed_pending_activity_delivery(factory)
    sheets = FakeSheetsPort()
    worker = CrmDeliveryWorker(session_factory=factory, sheets=sheets, now=lambda: NOW)

    result = worker.run_once()

    assert result.confirmed == 1
    assert len(sheets.locked_activity) == 1
    with factory() as session:
        assert session.get(CrmOutboxRow, "job_" + "9" * 32).status == "confirmed"
        assert session.get(CrmWorkerStateRow, "crm_last_successful_delivery") is not None
        assert session.get(CrmWorkerStateRow, "crm_last_successful_sheets_projection") is not None


def test_telemetry_flush_failures_do_not_change_or_repeat_delivery() -> None:
    _engine, factory = _database()
    _seed_pending_activity_delivery(factory)
    sheets = FakeSheetsPort()

    def reject_telemetry(session, _flush_context, _instances):  # noqa: ANN001
        rows = [*session.new, *session.dirty]
        if any(isinstance(row, CrmWorkerStateRow) and row.key.startswith("crm_") for row in rows):
            raise RuntimeError("telemetry unavailable")

    event.listen(factory.class_, "before_flush", reject_telemetry)
    try:
        worker = CrmDeliveryWorker(session_factory=factory, sheets=sheets, now=lambda: NOW)
        first = worker.run_once()
        second = worker.run_once()
    finally:
        event.remove(factory.class_, "before_flush", reject_telemetry)

    assert first.confirmed == 1
    assert second.confirmed == 0
    assert len(sheets.locked_activity) == 1
    with factory() as session:
        assert session.get(CrmOutboxRow, "job_" + "9" * 32).status == "confirmed"


def test_owner_health_requires_numeric_allowlisted_actor_and_never_reads_sheets() -> None:
    _engine, factory = _database()
    with factory() as session:
        _seed(session)
        settings = Settings(_env_file=None, telegram_owner_user_ids="12345")
        base = {
            "store": SimpleNamespace(session=session),
            "settings": settings,
            "sheets": _FailingPagedSheets(),
        }
        denied = _crm_operational_health(
            SimpleNamespace(
                **base,
                principal=Principal.owner(source="telegram", actor_id=""),
            ),
            {},
        )
        allowed = _crm_operational_health(
            SimpleNamespace(
                **base,
                principal=Principal.owner(source="telegram", actor_id="12345"),
            ),
            {},
        )
    assert denied.ok is False
    assert allowed.ok is True
    assert "קריאה בלבד" in allowed.text
    assert "private" not in allowed.text
    assert get_tool("crm_operational_health") is not None


def test_cli_reads_existing_sqlite_fixture_without_creating_or_writing(
    tmp_path, monkeypatch, capsys
) -> None:
    database_path = tmp_path / "fixture.sqlite3"
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MIA_DATABASE_URL", f"sqlite+pysqlite:///{database_path}")

    assert diagnostics_main(["--environment", "test", "--max-rows", "10"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["report"]["read_only"] is True
