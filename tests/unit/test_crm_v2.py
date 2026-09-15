from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from typing import Literal
from uuid import uuid4

import pytest
from app.db.base import Base
from app.db.models import (
    CrmActivityRow,
    CrmContactConversationRow,
    CrmContactRow,
    CrmIdentityRow,
    CrmIssueContactRow,
    CrmIssueRow,
    CrmOutboxRow,
    CrmSyncSnapshotRow,
)
from app.integrations.sheets import FakeSheetsPort
from app.services.crm_v2 import ActivityInput, CrmError, CrmRevisionConflict, CrmService
from app.workers.crm_delivery import CrmDeliveryWorker
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Match production: PostgreSQL enforces foreign keys and app/db/session.py
    # disables autoflush. The old default fixture had neither, which hid a child
    # row being flushed before its parent in production.
    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        engine.dispose()


def test_capture_atomically_persists_stable_contact_activity_and_destinations(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        result = CrmService(session).capture_site_lead(
            {"name": "נועה", "phone": "050-123-4567", "business": "סטודיו"},
            conversation_id="session-17",
            source_ref="site:session-17:message-4",
            summary="ביקשה שיחת המשך",
            recipient_ids=("123", "456"),
        )
        assert result.contact is not None
        assert result.contact.id.startswith("crm_")
        assert result.activity is not None
        assert result.activity.id.startswith("activity_")
        assert result.contact.id != result.activity.id
        destinations = session.scalars(select(CrmOutboxRow.destination)).all()
        assert sorted(destinations) == ["activity", "contacts", "telegram", "telegram"]
        session.rollback()

    with sessions() as session:
        assert session.scalars(select(CrmContactRow)).all() == []
        assert session.scalars(select(CrmOutboxRow)).all() == []


def test_repeated_source_and_identity_are_idempotent(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        service = CrmService(session)
        first = service.capture(
            {"email": "Person@Example.com", "name": "One"},
            source_ref="owner:update:1",
            activity=ActivityInput(source_ref="owner:update:1:activity"),
        )
        session.commit()
        second = service.capture(
            {"email": "person@example.com", "name": "One"},
            source_ref="owner:update:1",
            activity=ActivityInput(source_ref="owner:update:1:activity"),
        )
        session.commit()
        assert first.contact is not None and second.contact is not None
        assert first.contact.id == second.contact.id
        assert len(session.scalars(select(CrmActivityRow)).all()) == 1
        assert len(session.scalars(select(CrmOutboxRow)).all()) == 2


def test_identity_collision_does_not_mutate_either_contact_or_enqueue_projection(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session)
        left = service.capture({"phone": "0501111111", "name": "Left"}, source_ref="seed:left")
        right = service.capture(
            {"email": "right@example.com", "name": "Right"}, source_ref="seed:right"
        )
        session.commit()
        assert left.contact is not None and right.contact is not None
        before_revision = left.contact.revision
        before_jobs = len(session.scalars(select(CrmOutboxRow)).all())

        collision = service.capture(
            {"phone": "0501111111", "email": "right@example.com", "name": "Changed"},
            source_ref="owner:collision",
        )
        session.commit()

        assert collision.status == "conflict"
        assert service.lookup(contact_id=left.contact.id)[0].fields["name"] == "Left"
        assert service.lookup(contact_id=left.contact.id)[0].revision == before_revision
        assert service.lookup(contact_id=right.contact.id)[0].fields["name"] == "Right"
        assert len(session.scalars(select(CrmOutboxRow)).all()) == before_jobs
        assert session.get(CrmIssueRow, collision.issue_ids[0]) is not None


def test_identity_collision_blocks_later_projections_for_every_involved_contact(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session)
        phone_owner = service.capture(
            {"phone": "0501111112", "name": "Phone owner"}, source_ref="seed:phone"
        )
        email_owner = service.capture(
            {"email": "email-owner@example.com", "name": "Email owner"},
            source_ref="seed:email",
        )
        assert phone_owner.contact is not None and email_owner.contact is not None
        collision = service.capture(
            {"phone": "0501111112", "email": "email-owner@example.com"},
            source_ref="capture:collision",
        )
        assert collision.status == "conflict"

        service.capture(
            {"phone": "0501111112", "name": "Later phone edit"},
            source_ref="later:phone",
        )
        service.capture(
            {"email": "email-owner@example.com", "name": "Later email edit"},
            source_ref="later:email",
        )
        session.commit()

        linked_ids = set(
            session.scalars(
                select(CrmIssueContactRow.contact_id).where(
                    CrmIssueContactRow.issue_id == collision.issue_ids[0]
                )
            ).all()
        )
        assert linked_ids == {phone_owner.contact.id, email_owner.contact.id}
        for contact_id in linked_ids:
            issues = service.list_conflicts(contact_id=contact_id)
            assert [issue.id for issue in issues] == [collision.issue_ids[0]]
            statuses = session.scalars(
                select(CrmOutboxRow.status).where(
                    CrmOutboxRow.aggregate_id == contact_id,
                    CrmOutboxRow.destination == "contacts",
                )
            ).all()
            assert statuses and set(statuses) == {"conflict"}


def test_absence_bound_create_rejects_identity_created_after_proposal(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session)
        snapshot = service.snapshot_identity({"email": "new@example.com"})
        assert snapshot.contact_id == "" and snapshot.revision == 0
        service.capture({"email": "new@example.com"}, source_ref="race:winner")
        session.commit()
        with pytest.raises(CrmRevisionConflict):
            service.capture(
                {"email": "new@example.com", "name": "Late"},
                source_ref="race:late",
                expected_revision=snapshot.revision,
            )


def test_three_way_merge_accepts_disjoint_changes_and_pauses_same_field(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session)
        created = service.capture(
            {"phone": "0502222222", "name": "Base", "business": "Old"},
            source_ref="seed",
        )
        assert created.contact is not None
        service.mark_contact_synced(created.contact.id, row_number=2)
        session.commit()

        updated = service.capture(
            {"phone": "0502222222", "business": "Database"}, source_ref="owner:edit"
        )
        assert updated.contact is not None
        sheet = ["Sheet Name", "0502222222", "", "", "Sheet Business"]
        sheet.extend([""] * (14 - len(sheet)))
        sheet.append(updated.contact.id)
        merged = service.import_sheet_contact(sheet, row_number=2)
        session.commit()

        assert merged.status == "conflict"
        assert merged.contact is not None
        assert merged.contact.fields["name"] == "Sheet Name"
        assert merged.contact.fields["business"] == "Database"
        conflicts = service.list_conflicts(contact_id=merged.contact.id)
        assert [(item.issue_type, item.field_name) for item in conflicts] == [
            ("field_conflict", "business")
        ]


def test_bootstrap_reads_beyond_first_hundred_without_clearing(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    for index in range(135):
        row = [f"Person {index}", f"050{index:07d}"] + [""] * 12
        sheets.locked_contacts.append(row)
    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)

    assert worker.sync_from_sheets(force=True) == 135
    assert len(sheets.locked_contacts) == 135
    with sessions() as session:
        assert len(session.scalars(select(CrmContactRow)).all()) == 135
        assert all(row.id.startswith("crm_") for row in session.scalars(select(CrmContactRow)))


def test_legacy_bootstrap_re_finds_reordered_snapshot_and_only_backfills_id(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    original = ["Legacy", "050-765-4321", "legacy@example.com"] + [""] * 11
    sheets.locked_contacts.append(list(original))
    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)

    assert worker.sync_from_sheets(force=True) == 1
    sheets.locked_contacts.insert(0, ["Other", "0500000000"] + [""] * 12)
    run = worker.run_once(limit=1)

    assert run.confirmed == 1
    assert len(sheets.locked_contacts) == 2
    assert sheets.locked_contacts[1][:14] == original
    assert sheets.locked_contacts[1][14].startswith("crm_")
    assert sheets.locked_contacts[0] == ["Other", "0500000000"] + [""] * 12


def test_changed_legacy_snapshot_creates_issue_without_append_or_overwrite(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    sheets.locked_contacts.append(["Legacy", "0507654321"] + [""] * 12)
    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    worker.sync_from_sheets(force=True)
    sheets.locked_contacts[0][0] = "Owner changed"

    run = worker.run_once(limit=1)

    assert run.conflicts == 1
    assert len(sheets.locked_contacts) == 1
    assert len(sheets.locked_contacts[0]) == 14
    with sessions() as session:
        issue = session.scalars(
            select(CrmIssueRow).where(CrmIssueRow.issue_type == "legacy_binding_changed")
        ).one()
        assert issue.status == "open"


class _AmbiguousOnceSheets(FakeSheetsPort):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def append_crm_contact(self, cells: list[str]) -> None:
        self.calls += 1
        if self.calls == 1:
            raise OSError("connection dropped after request")
        super().append_crm_contact(cells)


def test_unknown_write_is_read_back_before_retry(sessions: sessionmaker[Session]) -> None:
    sheets = _AmbiguousOnceSheets()
    with sessions() as session:
        CrmService(session).capture({"phone": "0503333333"}, source_ref="capture")
        session.commit()
    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)

    first = worker.run_once(force_import=True, limit=1)
    assert first.unknown == 1
    assert sheets.calls == 1
    second = worker.run_once(limit=1)
    assert second.unknown == 1
    assert sheets.calls == 1
    with sessions() as session:
        job = session.scalars(
            select(CrmOutboxRow).where(CrmOutboxRow.destination == "contacts")
        ).one()
        assert job.status == "unknown"


def test_expired_lease_becomes_unknown_and_does_not_blind_send(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    with sessions() as session:
        CrmService(session).capture({"phone": "0504444444"}, source_ref="capture")
        job = session.scalars(select(CrmOutboxRow)).one()
        job.status = "in_flight"
        job.lease_owner = "dead-worker"
        job.lease_expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        session.commit()
    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    run = worker.run_once(force_import=True, limit=1)
    assert run.unknown == 1
    assert not any(operation[0] == "contact_v2" for operation in sheets.owner_operations)


def test_finish_refuses_to_complete_a_lease_no_longer_owned(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets, worker_id="worker-a")
    with sessions() as session:
        CrmService(session).capture({"phone": "0504545454"}, source_ref="capture")
        session.commit()
    job_id = worker._claim_one()
    assert job_id is not None

    with sessions() as session:
        job = session.get(CrmOutboxRow, job_id)
        assert job is not None
        job.lease_owner = "worker-b"
        # Another worker's takeover arrives as committed database state; the fixture
        # (like production) does not autoflush, so make the simulated change visible.
        session.flush()
        with pytest.raises(RuntimeError, match="lease ownership changed"):
            worker._finish(session, job, "confirmed")
        session.rollback()

    with sessions() as session:
        job = session.get(CrmOutboxRow, job_id)
        assert job is not None
        assert job.status == "in_flight"
        assert job.lease_owner == "worker-a"


def test_open_contact_conflict_pauses_every_later_projection(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session)
        created = service.capture({"phone": "0505555555", "business": "Base"}, source_ref="seed")
        assert created.contact is not None
        service.mark_contact_synced(created.contact.id, row_number=2)
        service.capture({"phone": "0505555555", "business": "Database"}, source_ref="db-change")
        sheet = ["", "0505555555", "", "", "Sheet"] + [""] * 9 + [created.contact.id]
        assert service.import_sheet_contact(sheet, row_number=2).status == "conflict"
        later = service.capture({"phone": "0505555555", "name": "Later"}, source_ref="later-change")
        assert later.contact is not None
        session.commit()

        statuses = session.scalars(
            select(CrmOutboxRow.status).where(CrmOutboxRow.destination == "contacts")
        ).all()
        assert statuses and set(statuses) == {"conflict"}


@pytest.mark.parametrize(
    ("resolution", "value", "expected_business"),
    (("database", None, "Database"), ("value", "Approved value", "Approved value")),
)
def test_conflict_resolution_converges_through_exact_observed_sheet_write(
    sessions: sessionmaker[Session],
    resolution: Literal["database", "value"],
    value: str | None,
    expected_business: str,
) -> None:
    sheets = FakeSheetsPort()
    with sessions() as session:
        service = CrmService(session)
        created = service.capture({"phone": "0505656565", "business": "Base"}, source_ref="seed")
        assert created.contact is not None
        contact_id = created.contact.id
        base_cells = [
            created.contact.fields[name]
            for name in (
                "name",
                "phone",
                "email",
                "date",
                "business",
                "source",
                "language",
                "want",
                "status",
                "summary",
                "next_step",
                "created",
                "updated",
                "pinged",
            )
        ]
        sheets.locked_contacts.append(base_cells + [contact_id])
        service.mark_contact_synced(contact_id, row_number=2)
        session.scalars(select(CrmOutboxRow)).one().status = "confirmed"
        changed = service.capture(
            {"phone": "0505656565", "business": "Database"}, source_ref="db-change"
        )
        assert changed.contact is not None
        sheets.locked_contacts[0][4] = "Sheet"
        imported = service.import_sheet_contact(sheets.locked_contacts[0], row_number=2)
        assert imported.status == "conflict"
        issue_id = imported.issue_ids[0]
        session.commit()

        service.resolve_conflict(
            issue_id,
            resolution=resolution,
            value=value,
            expected_contact_revision=changed.contact.revision,
        )
        session.commit()
        assert session.get(CrmIssueRow, issue_id).status == "resolving"
        assert [conflict.id for conflict in service.list_conflicts(contact_id=contact_id)] == [
            issue_id
        ]

    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    run = worker.run_once(force_import=True, limit=5)

    assert run.claimed == 1
    assert run.confirmed == 1
    assert sheets.locked_contacts[0][4] == expected_business
    with sessions() as session:
        service = CrmService(session)
        assert service.list_conflicts(contact_id=contact_id) == []
        assert service.lookup(contact_id=contact_id)[0].fields["business"] == expected_business
    repeated = worker.run_once(force_import=True, limit=5)
    assert repeated.claimed == 0
    with sessions() as session:
        assert CrmService(session).list_conflicts(contact_id=contact_id) == []


def test_conflict_resolution_rejects_sheet_change_after_approval_observation(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    with sessions() as session:
        service = CrmService(session)
        created = service.capture({"phone": "0505757575", "business": "Base"}, source_ref="seed")
        assert created.contact is not None
        contact_id = created.contact.id
        cells = [
            created.contact.fields[name]
            for name in (
                "name",
                "phone",
                "email",
                "date",
                "business",
                "source",
                "language",
                "want",
                "status",
                "summary",
                "next_step",
                "created",
                "updated",
                "pinged",
            )
        ]
        sheets.locked_contacts.append(cells + [contact_id])
        service.mark_contact_synced(contact_id, row_number=2)
        session.scalars(select(CrmOutboxRow)).one().status = "confirmed"
        changed = service.capture(
            {"phone": "0505757575", "business": "Database"}, source_ref="db-change"
        )
        assert changed.contact is not None
        sheets.locked_contacts[0][4] = "Observed Sheet"
        imported = service.import_sheet_contact(sheets.locked_contacts[0], row_number=2)
        issue_id = imported.issue_ids[0]
        session.commit()
        service.resolve_conflict(
            issue_id,
            resolution="database",
            expected_contact_revision=changed.contact.revision,
        )
        session.commit()

    sheets.locked_contacts[0][4] = "Later Sheet edit"
    run = CrmDeliveryWorker(session_factory=sessions, sheets=sheets).run_once(
        force_import=True, limit=5
    )

    assert run.claimed == 0
    assert sheets.locked_contacts[0][4] == "Later Sheet edit"
    with sessions() as session:
        issue = session.get(CrmIssueRow, issue_id)
        assert issue is not None
        assert issue.status == "open"
        assert issue.sheet_value == "Later Sheet edit"
        assert [
            conflict.id for conflict in CrmService(session).list_conflicts(contact_id=contact_id)
        ] == [issue_id]
        jobs = session.scalars(
            select(CrmOutboxRow).where(CrmOutboxRow.aggregate_id == contact_id)
        ).all()
        assert all(job.status in {"confirmed", "conflict"} for job in jobs)


def test_multiple_field_resolutions_share_one_projection_and_allow_later_edits(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    with sessions() as session:
        service = CrmService(session)
        created = service.capture(
            {"phone": "0505858585", "name": "Base name", "business": "Base business"},
            source_ref="seed",
        )
        assert created.contact is not None
        contact_id = created.contact.id
        cells = [
            created.contact.fields[name]
            for name in (
                "name",
                "phone",
                "email",
                "date",
                "business",
                "source",
                "language",
                "want",
                "status",
                "summary",
                "next_step",
                "created",
                "updated",
                "pinged",
            )
        ]
        sheets.locked_contacts.append(cells + [contact_id])
        service.mark_contact_synced(contact_id, row_number=2)
        session.scalars(select(CrmOutboxRow)).one().status = "confirmed"
        changed = service.capture(
            {"phone": "0505858585", "name": "Database name", "business": "Database business"},
            source_ref="db-change",
        )
        assert changed.contact is not None
        sheets.locked_contacts[0][0] = "Sheet name"
        sheets.locked_contacts[0][4] = "Sheet business"
        imported = service.import_sheet_contact(sheets.locked_contacts[0], row_number=2)
        assert len(imported.issue_ids) == 2
        issues = {
            session.get(CrmIssueRow, issue_id).field_name: issue_id
            for issue_id in imported.issue_ids
        }
        session.commit()

        first = service.resolve_conflict(
            issues["name"],
            resolution="database",
            expected_contact_revision=changed.contact.revision,
        )
        session.commit()

    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    blocked = worker.run_once(force_import=True, limit=5)
    assert blocked.claimed == 0
    assert sheets.locked_contacts[0][0] == "Sheet name"
    with sessions() as session:
        service = CrmService(session)
        service.resolve_conflict(
            issues["business"],
            resolution="value",
            value="Approved business",
            expected_contact_revision=first.revision,
        )
        session.commit()

    delivered = worker.run_once(force_import=True, limit=5)
    assert (delivered.claimed, delivered.confirmed) == (1, 1)
    assert sheets.locked_contacts[0][0] == "Database name"
    assert sheets.locked_contacts[0][4] == "Approved business"
    with sessions() as session:
        service = CrmService(session)
        assert service.list_conflicts(contact_id=contact_id) == []
        service.capture(
            {"phone": "0505858585", "summary": "Later database edit"},
            source_ref="later-db",
        )
        session.commit()
    sheets.locked_contacts[0][4] = "Later sheet edit"
    later = worker.run_once(force_import=True, limit=5)
    assert later.confirmed == 1
    with sessions() as session:
        current = CrmService(session).lookup(contact_id=contact_id)[0]
        assert current.fields["business"] == "Later sheet edit"
        assert current.fields["summary"] == "Later database edit"


def test_issue_created_after_claim_blocks_contact_effect(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    with sessions() as session:
        created = CrmService(session).capture({"phone": "0505959595"}, source_ref="seed")
        assert created.contact is not None
        contact_id = created.contact.id
        session.commit()
    job_id = worker._claim_one()
    assert job_id is not None
    with sessions() as session:
        CrmService(session).record_projection_issue(
            contact_id, issue_type="destination_identity_collision"
        )
        session.commit()

    assert worker._deliver(job_id) == "conflict"
    assert not any(operation[0] == "contact_v2" for operation in sheets.owner_operations)


def test_previously_synchronized_contact_deleted_after_claim_is_not_recreated(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    with sessions() as session:
        service = CrmService(session)
        created = service.capture(
            {"phone": "0506161616", "business": "Original"},
            source_ref="seed",
        )
        assert created.contact is not None
        contact_id = created.contact.id
        cells = [created.contact.fields[name] for name in (
            "name", "phone", "email", "date", "business", "source", "language",
            "want", "status", "summary", "next_step", "created", "updated", "pinged",
        )]
        sheets.locked_contacts.append(cells + [contact_id])
        service.mark_contact_synced(contact_id, row_number=2)
        session.scalars(select(CrmOutboxRow)).one().status = "confirmed"
        updated = service.capture(
            {"phone": "0506161616", "business": "Updated"},
            source_ref="database:update",
        )
        assert updated.contact is not None
        session.commit()

    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    job_id = worker._claim_one()
    assert job_id is not None
    sheets.locked_contacts.clear()

    assert worker._deliver(job_id) == "conflict"
    assert not any(operation[0] == "contact_v2" for operation in sheets.owner_operations)
    with sessions() as session:
        issues = CrmService(session).list_conflicts(contact_id=contact_id)
        assert [issue.issue_type for issue in issues] == ["missing_sheet_row"]
        assert session.get(CrmOutboxRow, job_id).status == "conflict"


def test_never_synchronized_contact_may_still_append(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    with sessions() as session:
        created = CrmService(session).capture(
            {"phone": "0506262626", "business": "New"},
            source_ref="new-contact",
        )
        assert created.contact is not None
        contact_id = created.contact.id
        session.commit()

    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    job_id = worker._claim_one()
    assert job_id is not None
    assert worker._deliver(job_id) == "confirmed"
    assert len(sheets.locked_contacts) == 1
    assert sheets.locked_contacts[0][14] == contact_id
    with sessions() as session:
        assert CrmService(session).list_conflicts(contact_id=contact_id) == []


def test_destination_time_owner_edit_is_imported_and_never_overwritten(
    sessions: sessionmaker[Session],
) -> None:
    sheets = FakeSheetsPort()
    with sessions() as session:
        service = CrmService(session)
        created = service.capture(
            {"name": "Base", "phone": "0506666666", "business": "Old"},
            source_ref="seed",
        )
        assert created.contact is not None
        contact_id = created.contact.id
        base_cells = [
            created.contact.fields[name]
            for name in (
                "name",
                "phone",
                "email",
                "date",
                "business",
                "source",
                "language",
                "want",
                "status",
                "summary",
                "next_step",
                "created",
                "updated",
                "pinged",
            )
        ]
        sheets.locked_contacts.append(base_cells + [contact_id])
        service.mark_contact_synced(contact_id, row_number=2)
        session.scalars(select(CrmOutboxRow)).one().status = "confirmed"
        service.capture({"phone": "0506666666", "business": "Database"}, source_ref="db-change")
        session.commit()

    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    job_id = worker._claim_one()
    assert job_id is not None
    sheets.locked_contacts[0][0] = "Owner"
    assert worker._deliver(job_id) == "conflict"

    assert sheets.locked_contacts[0][0] == "Owner"
    assert sheets.locked_contacts[0][4] == "Old"
    with sessions() as session:
        current = CrmService(session).lookup(contact_id=contact_id)[0]
        assert current.fields["name"] == "Owner"
        assert current.fields["business"] == "Database"


class _UnavailableSheets(FakeSheetsPort):
    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        raise OSError("Sheets unavailable")


def test_failed_import_blocks_contacts_but_independent_telegram_still_delivers(
    sessions: sessionmaker[Session],
) -> None:
    delivered: list[str] = []
    with sessions() as session:
        CrmService(session).capture_site_lead(
            {"phone": "0507777777"},
            conversation_id="website-77",
            source_ref="site:77",
            recipient_ids=("123",),
        )
        session.commit()
    worker = CrmDeliveryWorker(
        session_factory=sessions,
        sheets=_UnavailableSheets(),
        allowed_telegram_recipient_ids=frozenset({"123"}),
        telegram_handler=lambda payload, reconcile: (
            delivered.append(str(payload["conversation_id"])) or "confirmed"
        ),
    )

    run = worker.run_once(force_import=True, limit=5)

    assert run.import_failed is True
    assert delivered == ["website-77"]
    with sessions() as session:
        contact_job = session.scalars(
            select(CrmOutboxRow).where(CrmOutboxRow.destination == "contacts")
        ).one()
        assert contact_job.status == "pending"


def _legacy_sheet_row(**fields: str) -> list[str]:
    from app.services.crm_v2 import CONTACT_FIELDS

    return [fields.get(name, "") for name in CONTACT_FIELDS] + [""]


def test_duplicate_legacy_sheet_rows_become_an_issue_and_leads_still_deliver(
    sessions: sessionmaker[Session],
) -> None:
    """Production 2026-09-11: two legacy rows sharing a phone raised a foreign-key
    violation on every import, which aborted every delivery cycle."""
    sheets = FakeSheetsPort()
    sheets.locked_contacts = [
        _legacy_sheet_row(name="Dana", phone="0501231234"),
        _legacy_sheet_row(name="Dana Levi", phone="0501231234"),
    ]
    delivered: list[str] = []
    with sessions() as session:
        CrmService(session).capture_site_lead(
            {"phone": "0509990000"},
            conversation_id="website-88",
            source_ref="site:88",
            recipient_ids=("123",),
        )
        session.commit()
    worker = CrmDeliveryWorker(
        session_factory=sessions,
        sheets=sheets,
        allowed_telegram_recipient_ids=frozenset({"123"}),
        telegram_handler=lambda payload, reconcile: (
            delivered.append(str(payload["conversation_id"])) or "confirmed"
        ),
    )

    run = worker.run_once(force_import=True, limit=10)

    assert run.import_failed is False
    assert delivered == ["website-88"]
    with sessions() as session:
        issue = session.scalars(
            select(CrmIssueRow).where(CrmIssueRow.issue_type == "legacy_row_collision")
        ).one()
        assert session.get(CrmIssueContactRow, (issue.id, issue.contact_id)) is not None


def test_new_issue_is_flushed_before_its_contact_link_is_added(
    sessions: sessionmaker[Session],
) -> None:
    # Without an ORM relationship the flush order of these two inserts is arbitrary,
    # so assert the ordering directly instead of hoping a flush happens to fail.
    with sessions() as session:
        service = CrmService(session)
        created = service.capture({"phone": "0507070707"}, source_ref="fk:seed")
        assert created.contact is not None
        session.commit()
        service._issue(contact_id=created.contact.id, issue_type="probe")
        pending = {type(obj).__name__ for obj in session.new}
        assert "CrmIssueRow" not in pending
        assert "CrmIssueContactRow" in pending
        session.commit()


class _DatabaseErrorSheets(FakeSheetsPort):
    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SELECT 1", {}, Exception("database unavailable"))


def test_database_error_during_import_does_not_stop_telegram_delivery(
    sessions: sessionmaker[Session],
) -> None:
    delivered: list[str] = []
    with sessions() as session:
        CrmService(session).capture_site_lead(
            {"phone": "0508880000"},
            conversation_id="website-89",
            source_ref="site:89",
            recipient_ids=("123",),
        )
        session.commit()
    worker = CrmDeliveryWorker(
        session_factory=sessions,
        sheets=_DatabaseErrorSheets(),
        allowed_telegram_recipient_ids=frozenset({"123"}),
        telegram_handler=lambda payload, reconcile: (
            delivered.append(str(payload["conversation_id"])) or "confirmed"
        ),
    )

    run = worker.run_once(force_import=True, limit=5)

    assert run.import_failed is True
    assert delivered == ["website-89"]
    with sessions() as session:
        contact_job = session.scalars(
            select(CrmOutboxRow).where(CrmOutboxRow.destination == "contacts")
        ).one()
        assert contact_job.status == "pending"


def test_identity_conflict_resolution_cannot_take_another_contacts_identity(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session)
        left = service.capture({"phone": "0508888888"}, source_ref="left")
        right = service.capture({"phone": "0509999999"}, source_ref="right")
        assert left.contact is not None and right.contact is not None
        issue = CrmIssueRow(
            id="issue-phone",
            contact_id=left.contact.id,
            issue_type="field_conflict",
            field_name="phone",
            base_value="0508888888",
            database_value="0508888888",
            sheet_value="0509999999",
            status="open",
            created_at=datetime.now(UTC).isoformat(),
        )
        session.add(issue)
        session.commit()

        with pytest.raises(CrmError, match="another contact"):
            service.resolve_conflict("issue-phone", resolution="sheet")
        session.rollback()
        owners = dict(
            session.execute(
                select(CrmIdentityRow.normalized_value, CrmIdentityRow.contact_id)
            ).all()
        )
        assert owners["0508888888"] == left.contact.id
        assert owners["0509999999"] == right.contact.id
        assert session.get(CrmIssueRow, "issue-phone").status == "open"


def test_identity_conflict_resolution_reconciles_the_identity_index(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session)
        created = service.capture({"phone": "0501212121"}, source_ref="seed")
        assert created.contact is not None
        session.add(
            CrmIssueRow(
                id="issue-free-phone",
                contact_id=created.contact.id,
                issue_type="field_conflict",
                field_name="phone",
                base_value="0501212121",
                database_value="0501212121",
                sheet_value="0503434343",
                status="open",
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        session.commit()

        resolved = service.resolve_conflict("issue-free-phone", resolution="sheet")
        session.commit()

        assert resolved.fields["phone"] == "0503434343"
        owners = dict(
            session.execute(
                select(CrmIdentityRow.normalized_value, CrmIdentityRow.contact_id)
            ).all()
        )
        assert "0501212121" not in owners
        assert owners["0503434343"] == created.contact.id


def test_finish_rechecks_current_lease_owner(sessions: sessionmaker[Session]) -> None:
    sheets = FakeSheetsPort()
    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets, worker_id="worker-a")
    with sessions() as session:
        CrmService(session).capture({"phone": "0504545454"}, source_ref="seed")
        session.commit()
    job_id = worker._claim_one()
    assert job_id is not None
    with sessions() as session:
        job = session.get(CrmOutboxRow, job_id)
        assert job is not None
        job.lease_owner = "worker-b"
        # Another worker's takeover arrives as committed database state; the fixture
        # (like production) does not autoflush, so make the simulated change visible.
        session.flush()
        with pytest.raises(RuntimeError, match="lease ownership changed"):
            worker._finish(session, job, "confirmed")
        session.rollback()


def test_every_conversation_capture_keeps_a_scoped_contact_association(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session)
        first = service.capture_site_lead(
            {"email": "returning@example.com", "name": "Same"},
            conversation_id="conversation-a",
            source_ref="site:a",
        )
        second = service.capture_site_lead(
            {"email": "returning@example.com", "name": "Same"},
            conversation_id="conversation-b",
            source_ref="site:b",
        )
        session.commit()

        assert first.contact is not None and second.contact is not None
        assert first.contact.id == second.contact.id
        assert service.lookup_for_conversation("conversation-a").id == first.contact.id
        assert service.lookup_for_conversation("conversation-b").id == first.contact.id
        assert service.lookup_for_conversation("conversation-c") is None
        assert len(service.list_contact_conversations(first.contact.id)) == 2
        assert len(session.scalars(select(CrmContactConversationRow)).all()) == 2


@pytest.mark.skipif(not os.getenv("MIA_TEST_POSTGRES_URL"), reason="test PostgreSQL DSN not set")
def test_postgres_revision_compare_and_swap_uses_dedicated_schema() -> None:
    schema = f"test_crm_{uuid4().hex}"
    admin = create_engine(os.environ["MIA_TEST_POSTGRES_URL"])
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ["MIA_TEST_POSTGRES_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory() as session:
            created = CrmService(session).capture(
                {"email": "postgres@example.com", "name": "Before"},
                source_ref="postgres:create",
            )
            session.commit()
            assert created.contact is not None
            revision = created.contact.revision
        barrier = Barrier(2)

        def race(name: str) -> str:
            with factory() as contender:
                barrier.wait(timeout=5)
                try:
                    CrmService(contender).capture(
                        {"email": "postgres@example.com", "name": name},
                        source_ref=f"postgres:{name.casefold()}",
                        expected_revision=revision,
                    )
                    contender.commit()
                    return "won"
                except CrmRevisionConflict:
                    contender.rollback()
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(race, ("First", "Second")))
        assert sorted(outcomes) == ["conflict", "won"]

        create_barrier = Barrier(2)

        def race_create(name: str) -> str:
            with factory() as contender:
                create_barrier.wait(timeout=5)
                try:
                    CrmService(contender).capture(
                        {"email": "new-race@example.com", "name": name},
                        source_ref=f"postgres:create:{name.casefold()}",
                        expected_revision=0,
                    )
                    contender.commit()
                    return "won"
                except CrmRevisionConflict:
                    contender.rollback()
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            create_outcomes = list(pool.map(race_create, ("First", "Second")))
        assert sorted(create_outcomes) == ["conflict", "won"]
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.skipif(not os.getenv("MIA_TEST_POSTGRES_URL"), reason="test PostgreSQL DSN not set")
def test_postgres_issue_creation_serializes_with_final_contact_effect() -> None:
    schema = f"test_crm_effect_{uuid4().hex}"
    admin = create_engine(os.environ["MIA_TEST_POSTGRES_URL"])
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ["MIA_TEST_POSTGRES_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
    )
    release_issue = Event()
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        sheets = FakeSheetsPort()
        worker = CrmDeliveryWorker(session_factory=factory, sheets=sheets)
        with factory() as session:
            created = CrmService(session).capture(
                {"phone": "0506060606"}, source_ref="postgres:seed"
            )
            assert created.contact is not None
            contact_id = created.contact.id
            session.commit()
        job_id = worker._claim_one()
        assert job_id is not None
        issue_locked = Event()
        delivery_started = Event()

        def create_issue_while_holding_serialization_lock() -> None:
            with factory() as session:
                CrmService(session).record_projection_issue(
                    contact_id, issue_type="destination_identity_collision"
                )
                issue_locked.set()
                assert release_issue.wait(timeout=5)
                session.commit()

        def deliver_after_claim() -> str:
            delivery_started.set()
            return worker._deliver(job_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            issue_future = pool.submit(create_issue_while_holding_serialization_lock)
            assert issue_locked.wait(timeout=5)
            delivery_future = pool.submit(deliver_after_claim)
            assert delivery_started.wait(timeout=5)
            assert not delivery_future.done()
            assert not any(operation[0] == "contact_v2" for operation in sheets.owner_operations)
            release_issue.set()
            issue_future.result(timeout=5)
            assert delivery_future.result(timeout=5) == "conflict"
        assert not any(operation[0] == "contact_v2" for operation in sheets.owner_operations)
    finally:
        release_issue.set()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_refresh_pending_site_brief_updates_pending_job_activity_and_fields(
    sessions: sessionmaker[Session],
) -> None:
    """Chunk C3a: a same-turn ``submit_lead`` call must reach the already-built brief.

    ``capture_site_lead`` builds the Telegram outbox payload and the capture Activity
    before the website surface's model turn runs. ``refresh_pending_site_brief`` is the
    only path that may rewrite that still-pending text afterwards, and it must update
    the job payload, the Activity result and the contact's next_step/name fields
    together without ever enqueuing a second job.
    """
    with sessions() as session:
        service = CrmService(session)
        result = service.capture_site_lead(
            {"phone": "0501112222", "business": "סטודיו"},
            conversation_id="session-refresh-1",
            source_ref="site:session-refresh-1:msg-1",
            summary="פנייה חדשה מהאתר\nהשלב הבא המומלץ: לחזור לפונה",
            recipient_ids=("123",),
        )
        contact_id = result.contact.id
        telegram_job_ids = [
            row.id
            for row in session.scalars(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact_id,
                    CrmOutboxRow.destination == "telegram",
                )
            ).all()
        ]
        assert len(telegram_job_ids) == 1

        new_summary = (
            "פנייה חדשה מהאתר\n"
            "השלב הבא המומלץ: לתאם שיחה"
        )
        service.refresh_pending_site_brief(
            contact_id=contact_id,
            job_ids=telegram_job_ids,
            summary=new_summary,
            next_step="לתאם שיחה",
            name="נועה",
            source_ref="site:session-refresh-1:msg-1",
        )

        job = session.get(CrmOutboxRow, telegram_job_ids[0])
        assert json.loads(job.payload_json)["text"] == new_summary
        activity = session.scalars(
            select(CrmActivityRow).where(
                CrmActivityRow.source_ref == "site:session-refresh-1:msg-1:activity"
            )
        ).one()
        assert activity.result == new_summary
        contact = session.get(CrmContactRow, contact_id)
        fields = json.loads(contact.fields_json)
        assert fields["next_step"] == "לתאם שיחה"
        assert fields["name"] == "נועה"
        # Review fix (P1): the earlier version of this test asserted the total
        # outbox row count never changed, which encoded a bug -- refreshing
        # next_step/name bumps the contact's revision, and the Contacts Sheet
        # projection is keyed by revision, so a fresh `contacts` sync job at the
        # new revision is required or the Sheet worker sees a payload/row
        # revision mismatch and permanently conflicts the projection. Only the
        # Telegram job must never duplicate.
        telegram_jobs_after = list(
            session.scalars(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact_id,
                    CrmOutboxRow.destination == "telegram",
                )
            ).all()
        )
        assert len(telegram_jobs_after) == 1
        assert contact.revision == 2
        contacts_job = session.scalars(
            select(CrmOutboxRow).where(
                CrmOutboxRow.aggregate_id == contact_id,
                CrmOutboxRow.destination == "contacts",
                CrmOutboxRow.dedupe_key == f"contacts:{contact_id}:{contact.revision}",
            )
        ).one()
        contacts_payload = json.loads(contacts_job.payload_json)
        assert contacts_payload["revision"] == contact.revision
        assert contacts_payload["cells"][0] == "נועה"  # name
        assert contacts_payload["cells"][10] == "לתאם שיחה"  # next_step


def test_refresh_pending_site_brief_refuses_non_pending_or_foreign_jobs(
    sessions: sessionmaker[Session],
) -> None:
    """A job already sent (or belonging to a different contact) is never touched."""
    with sessions() as session:
        service = CrmService(session)
        result_a = service.capture_site_lead(
            {"phone": "0503334444"},
            conversation_id="session-refresh-2",
            source_ref="site:session-refresh-2:msg-1",
            summary="original a",
            recipient_ids=("123",),
        )
        result_b = service.capture_site_lead(
            {"phone": "0505556666"},
            conversation_id="session-refresh-3",
            source_ref="site:session-refresh-3:msg-1",
            summary="original b",
            recipient_ids=("123",),
        )
        contact_a = result_a.contact.id
        contact_b = result_b.contact.id
        job_a = session.scalars(
            select(CrmOutboxRow).where(
                CrmOutboxRow.aggregate_id == contact_a,
                CrmOutboxRow.destination == "telegram",
            )
        ).one()
        job_b = session.scalars(
            select(CrmOutboxRow).where(
                CrmOutboxRow.aggregate_id == contact_b,
                CrmOutboxRow.destination == "telegram",
            )
        ).one()
        # Simulate a prior turn's job that has already gone out.
        job_a.status = "confirmed"
        session.flush()

        service.refresh_pending_site_brief(
            contact_id=contact_a,
            job_ids=[job_a.id, job_b.id],
            summary="rewritten",
            next_step="new step",
            name="",
            source_ref="site:session-refresh-2:msg-1",
        )

        session.expire_all()
        refreshed_a = session.get(CrmOutboxRow, job_a.id)
        refreshed_b = session.get(CrmOutboxRow, job_b.id)
        assert json.loads(refreshed_a.payload_json)["text"] == "original a"
        assert refreshed_a.status == "confirmed"
        # job_b belongs to a different contact than the one passed in, even though
        # it is pending, and must never be modified.
        assert json.loads(refreshed_b.payload_json)["text"] == "original b"


def test_contact_view_fills_created_and_updated_in_owner_local_iso(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        fixed_now = datetime(2026, 9, 6, 13, 16, 47, tzinfo=UTC)
        service = CrmService(session, now=fixed_now, timezone="Asia/Jerusalem")
        created = service.capture({"phone": "0509999001", "name": "Dana"}, source_ref="seed:ts")
        session.commit()
        assert created.contact is not None
        # Asia/Jerusalem is UTC+3 in September (DST).
        assert created.contact.fields["created"] == "2026-09-06T16:16:47+03:00"
        assert created.contact.fields["updated"] == "2026-09-06T16:16:47+03:00"

        later = datetime(2026, 9, 7, 6, 0, 0, tzinfo=UTC)
        updater = CrmService(session, now=later, timezone="Asia/Jerusalem")
        updated = updater.capture(
            {"phone": "0509999001", "business": "Studio"}, source_ref="seed:ts:2"
        )
        session.commit()
        assert updated.contact is not None
        # created_at never moves after the row exists; updated_at tracks the edit.
        assert updated.contact.fields["created"] == "2026-09-06T16:16:47+03:00"
        assert updated.contact.fields["updated"] == "2026-09-07T09:00:00+03:00"


def test_owner_edit_to_other_field_produces_no_conflict_from_timestamp_columns(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session, now=datetime(2026, 9, 6, 13, 0, 0, tzinfo=UTC))
        created = service.capture(
            {"phone": "0509999002", "name": "Base", "business": "Old"}, source_ref="seed"
        )
        assert created.contact is not None
        service.mark_contact_synced(created.contact.id, row_number=2)
        session.commit()

        # The live Sheet shows real, non-empty text in the created/updated cells
        # (written there by a prior projection), while the owner edits only "business".
        displayed_created = created.contact.fields["created"]
        displayed_updated = created.contact.fields["updated"]
        sheet = [
            "Base",
            "0509999002",
            "",
            "",
            "New Business",
            "",
            "",
            "",
            "",
            "",
            "",
            displayed_created,
            displayed_updated,
            "",
        ]
        sheet.append(created.contact.id)
        assert len(sheet) == 15
        merged = service.import_sheet_contact(sheet, row_number=2)
        session.commit()

        assert merged.status != "conflict"
        assert merged.contact is not None
        assert merged.contact.fields["business"] == "New Business"
        conflicts = service.list_conflicts(contact_id=merged.contact.id)
        assert all(item.field_name not in {"created", "updated"} for item in conflicts)


def test_owner_typed_timestamp_does_not_overwrite_db_timestamps(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        service = CrmService(session, now=datetime(2026, 9, 6, 13, 0, 0, tzinfo=UTC))
        created = service.capture(
            {"phone": "0509999003", "name": "Base"}, source_ref="seed"
        )
        assert created.contact is not None
        service.mark_contact_synced(created.contact.id, row_number=2)
        session.commit()
        real_created = created.contact.fields["created"]
        real_updated = created.contact.fields["updated"]

        # The owner types garbage into the created/updated cells directly.
        sheet = ["Base", "0509999003"] + [""] * 9
        sheet.extend(["2001-01-01T00:00:00+00:00", "2001-01-01T00:00:00+00:00", ""])
        sheet.append(created.contact.id)
        assert len(sheet) == 15
        merged = service.import_sheet_contact(sheet, row_number=2)
        session.commit()

        assert merged.contact is not None
        # System timestamps are untouched -- the owner's typed value never lands in
        # fields_json and never reaches the projected view.
        assert merged.contact.fields["created"] == real_created
        assert merged.contact.fields["updated"] == real_updated
        raw_row = session.get(CrmContactRow, created.contact.id)
        stored = json.loads(raw_row.fields_json)
        assert stored.get("created", "") == ""
        assert stored.get("updated", "") == ""


def test_owner_edit_to_updated_cell_self_heals_and_later_field_still_delivers(
    sessions: sessionmaker[Session],
) -> None:
    """Regression: an owner edit to the system-owned 'updated' cell must not wedge
    this contact's projection forever. Before the fix, `destination_changed`
    compared indices 11/12 like any other column, so the owner's typed text never
    matched `cells` (the system's fresh value) and the job conflicted every cycle
    with no self-correcting re-enqueue.
    """
    sheets = FakeSheetsPort()
    with sessions() as session:
        service = CrmService(session)
        created = service.capture({"phone": "0509991234", "name": "Dana"}, source_ref="seed")
        assert created.contact is not None
        contact_id = created.contact.id
        session.commit()

    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    first = worker.run_once()
    assert (first.claimed, first.confirmed) == (1, 1)

    row = next(r for r in sheets.locked_contacts if r[14] == contact_id)
    row[12] = "owner typed this"  # owner edits the "updated" cell directly

    reimport = worker.run_once(force_import=True)
    assert reimport.conflicts == 0  # the merge ignores the owner's edit here, no issue

    with sessions() as session:
        CrmService(session).capture(
            {"phone": "0509991234", "want": "more clients"}, source_ref="later"
        )
        session.commit()

    total_confirmed = 0
    for _ in range(3):
        run = worker.run_once(force_import=True)
        total_confirmed += run.confirmed
    assert total_confirmed == 1
    row = next(r for r in sheets.locked_contacts if r[14] == contact_id)
    assert row[7] == "more clients"  # "want" actually landed
    assert row[12] != "owner typed this"  # system value won, not stuck on the edit


def test_delivery_projects_cleanly_despite_mismatched_timestamp_snapshot_base(
    sessions: sessionmaker[Session],
) -> None:
    """A live-style row whose CrmSyncSnapshotRow base for created/updated differs
    from what is currently on the destination (e.g. a legacy UTC '+00:00' base vs a
    freshly-computed local '+03:00' cell) must never block an unrelated field write.
    """
    sheets = FakeSheetsPort()
    with sessions() as session:
        service = CrmService(session)
        created = service.capture({"phone": "0509995678", "name": "Yossi"}, source_ref="seed2")
        assert created.contact is not None
        contact_id = created.contact.id
        session.commit()

    worker = CrmDeliveryWorker(session_factory=sessions, sheets=sheets)
    first = worker.run_once()
    assert (first.claimed, first.confirmed) == (1, 1)

    with sessions() as session:
        for name in ("created", "updated"):
            snapshot = session.get(CrmSyncSnapshotRow, (contact_id, name))
            assert snapshot is not None
            snapshot.value = "2020-01-01T00:00:00+00:00"  # stale/mismatched base
        session.commit()

    with sessions() as session:
        CrmService(session).capture(
            {"phone": "0509995678", "business": "Studio"}, source_ref="seed2:2"
        )
        session.commit()

    run = worker.run_once(force_import=True)
    assert (run.claimed, run.confirmed, run.conflicts) == (1, 1, 0)
    row = next(r for r in sheets.locked_contacts if r[14] == contact_id)
    assert row[4] == "Studio"
