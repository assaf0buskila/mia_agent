from __future__ import annotations

from pathlib import Path

import pytest
from app.db.base import Base
from app.db.migrate import _apply_file
from app.db.models import CrmActivityRow, CrmFormIntakeReceiptRow
from app.db.store import LeadStore
from app.services.form_intake import FormIntakeService
from app.workers.mark_test_activities import (
    ActivitySelectionError,
    mark_reviewed_test_activities,
)
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, autoflush=False, expire_on_commit=False)


def _capture_pair(sessions: sessionmaker[Session]):
    sources = ("10000000-0000-4000-8000-000000000001", "10000000-0000-4000-8000-000000000002")
    with sessions() as session:
        service = FormIntakeService(session)
        results = tuple(
            service.capture(
                source_id=source,
                payload_sha256=str(index) * 64,
                submitted_at=f"2026-10-16T13:3{index}:00+00:00",
                fields={"name": "Reviewed QA", "phone": "0500000000", "source": "learn"},
                summary="Reviewed QA form submission",
                recipient_ids=(),
            )
            for index, source in enumerate(sources, start=1)
        )
        session.commit()
        assert results[0].contact_id == results[1].contact_id
        return (
            tuple(
                (source, result.activity_id)
                for source, result in zip(sources, results, strict=True)
            ),
            results[0].contact_id,
        )


def test_guarded_flags_exclude_only_the_two_activities_and_preserve_records(sessions) -> None:
    pairs, contact_id = _capture_pair(sessions)
    with sessions() as session:
        real = FormIntakeService(session).capture(
            source_id="20000000-0000-4000-8000-000000000001",
            payload_sha256="3" * 64,
            submitted_at="2026-10-16T13:40:00+00:00",
            fields={"name": "Real lead", "phone": "0500000000", "source": "learn"},
            summary="Real Learn lead",
            recipient_ids=(),
        )
        session.commit()
        assert real.contact_id == contact_id

        store = LeadStore(session)
        assert store.count_captured_website_leads() == 1
        assert [lead.occurred_at for lead in store.list_captured_website_leads()] == [
            "2026-10-16T13:40:00+00:00"
        ]

        dry_run = mark_reviewed_test_activities(
            session, source_activity_pairs=pairs, contact_id=contact_id
        )
        assert dry_run.dry_run is True
        assert dry_run.matched == 2
        assert dry_run.changed == 0
        assert session.scalars(select(CrmActivityRow.is_test)).all() == [False, False, False]

        applied = mark_reviewed_test_activities(
            session, source_activity_pairs=pairs, contact_id=contact_id, apply=True
        )
        assert applied.dry_run is False
        assert applied.changed == 2
        assert session.query(CrmFormIntakeReceiptRow).count() == 3
        activities = session.scalars(select(CrmActivityRow).order_by(CrmActivityRow.id)).all()
        assert len(activities) == 3
        flagged = [row for row in activities if row.is_test]
        assert {row.id for row in flagged} == {activity_id for _, activity_id in pairs}
        assert session.get(CrmActivityRow, real.activity_id).is_test is False
        assert all(row.channel == "form" and row.action == "contact_captured" for row in activities)
        assert store.count_captured_website_leads() == 1
        retained = store.list_captured_website_leads()
        assert [lead.contact_id for lead in retained] == [real.contact_id]
        assert [lead.occurred_at for lead in retained] == ["2026-10-16T13:40:00+00:00"]
        assert store.count_captured_website_leads(
            occurred_from="2026-10-16T13:30:00+00:00",
            occurred_to="2026-10-16T13:40:00+00:00",
        ) == 0
        assert store.list_captured_website_leads(
            occurred_from="2026-10-16T13:30:00+00:00",
            occurred_to="2026-10-16T13:40:00+00:00",
        ) == []
        assert store.count_captured_website_leads(
            occurred_from="2026-10-16T13:40:00+00:00",
            occurred_to="2026-10-16T13:41:00+00:00",
        ) == 1

        repeated = mark_reviewed_test_activities(
            session, source_activity_pairs=pairs, contact_id=contact_id, apply=True
        )
        assert repeated.changed == 0


def test_two_test_only_activities_leave_no_captured_lead(sessions) -> None:
    pairs, contact_id = _capture_pair(sessions)
    with sessions() as session:
        mark_reviewed_test_activities(
            session,
            source_activity_pairs=pairs,
            contact_id=contact_id,
            apply=True,
        )

        store = LeadStore(session)
        assert store.count_captured_website_leads() == 0
        assert store.list_captured_website_leads() == []


def test_guard_mismatch_aborts_without_flags(sessions) -> None:
    pairs, contact_id = _capture_pair(sessions)
    mismatched = (pairs[0], (pairs[1][0], "activity_wrong_but_distinct"))
    with sessions() as session:
        with pytest.raises(ActivitySelectionError, match="receipt relationship mismatch"):
            mark_reviewed_test_activities(
                session,
                source_activity_pairs=mismatched,
                contact_id=contact_id,
                apply=True,
            )
        assert session.scalars(select(CrmActivityRow.is_test)).all() == [False, False]


def test_migration_preserves_existing_activity_as_live(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE crm_contacts (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE crm_activities ("
                "id VARCHAR(64) PRIMARY KEY, contact_id VARCHAR(64) NOT NULL, "
                "occurred_at VARCHAR(64) NOT NULL, who VARCHAR(160) NOT NULL DEFAULT '', "
                "channel VARCHAR(32) NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '', "
                "result TEXT NOT NULL DEFAULT '', source_ref VARCHAR(255) NOT NULL, "
                "created_at VARCHAR(64) NOT NULL DEFAULT '')"
            )
        )
        connection.execute(text("INSERT INTO crm_contacts (id) VALUES ('contact_existing')"))
        connection.execute(
            text(
                "INSERT INTO crm_activities "
                "(id, contact_id, occurred_at, source_ref) VALUES "
                "('activity_existing', 'contact_existing', '2026-10-16', 'existing')"
            )
        )
        migration = Path(__file__).resolve().parents[2] / "migrations" / (
            "20261016_crm_activity_is_test.sql"
        )
        _apply_file(connection, migration)

    assert "is_test" in {column["name"] for column in inspect(engine).get_columns("crm_activities")}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT is_test FROM crm_activities")) in (False, 0)
