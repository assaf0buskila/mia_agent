from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.db.base import Base
from app.db.models import CrmContactRow
from app.db.reminders import OwnerReminderRow
from app.domain.reminder_time import ReminderTimeError, parse_reminder_time
from app.services.reminders import (
    ReminderConflict,
    ReminderContactAmbiguous,
    ReminderDispatching,
    ReminderService,
    ReminderValidationError,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 10, 11, 8, 12, 47, tzinfo=UTC)


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("remind me tomorrow at 11:30", datetime(2026, 10, 12, 8, 30, tzinfo=UTC)),
        ("תזכירי לי מחר ב-11:30", datetime(2026, 10, 12, 8, 30, tzinfo=UTC)),
        ("2026-10-12 at 11:30 am", datetime(2026, 10, 12, 8, 30, tzinfo=UTC)),
        ("12/10/2026 at 11:30 pm", datetime(2026, 10, 12, 20, 30, tzinfo=UTC)),
        ("next week at 11:30", datetime(2026, 10, 18, 8, 30, tzinfo=UTC)),
        ("בשבוע הבא ב-11:30", datetime(2026, 10, 18, 8, 30, tzinfo=UTC)),
    ],
)
def test_parse_exact_english_and_hebrew_times(text: str, expected: datetime) -> None:
    assert parse_reminder_time(text, now=NOW) == expected


def test_relative_time_preserves_minute_precision() -> None:
    assert parse_reminder_time("remind me in 15 minutes", now=NOW) == datetime(
        2026, 10, 11, 8, 27, tzinfo=UTC
    )
    assert parse_reminder_time("תזכירי לי בעוד 2 שעות", now=NOW) == datetime(
        2026, 10, 11, 10, 12, tzinfo=UTC
    )
    assert parse_reminder_time("snooze for 2 hours", now=NOW) == datetime(
        2026, 10, 11, 10, 12, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("remind me at 11:30", "missing_date"),
        ("remind me tomorrow", "missing_time"),
        ("remind me tomorrow at 11", "ambiguous_time"),
        ("tomorrow at 11:30 and today at 12:30", "ambiguous_time"),
        ("tomorrow at 11:30 and 12:30", "ambiguous_time"),
        ("tomorrow at 25:00", "invalid_time"),
        ("today at 10:00", "past"),
        ("in 15 minutes tomorrow at 11:30", "ambiguous_time"),
        ("in 2 hours and 30 minutes", "ambiguous_time"),
        ("in 2 hours at 7", "ambiguous_time"),
        ("בעוד 2 שעות ב7", "ambiguous_time"),
        ("next week Monday at 11:30", "ambiguous_time"),
        ("בשבוע הבא ביום שני ב-11:30", "ambiguous_time"),
    ],
)
def test_parse_rejects_incomplete_ambiguous_invalid_and_past(text: str, reason: str) -> None:
    with pytest.raises(ReminderTimeError) as caught:
        parse_reminder_time(text, now=NOW)
    assert caught.value.reason == reason


def test_dst_gap_fold_and_explicit_offset_resolution() -> None:
    with pytest.raises(ReminderTimeError) as gap:
        parse_reminder_time("2026-03-27 at 02:30", now=datetime(2026, 1, 1, tzinfo=UTC))
    assert gap.value.reason == "dst_gap"

    with pytest.raises(ReminderTimeError) as fold:
        parse_reminder_time("2026-10-25 at 01:30", now=datetime(2026, 1, 1, tzinfo=UTC))
    assert fold.value.reason == "dst_fold"

    assert parse_reminder_time(
        "2026-10-25 at 01:30 +03:00", now=datetime(2026, 1, 1, tzinfo=UTC)
    ) == datetime(2026, 10, 24, 22, 30, tzinfo=UTC)
    assert parse_reminder_time(
        "2026-10-25 at 01:30 +02:00", now=datetime(2026, 1, 1, tzinfo=UTC)
    ) == datetime(2026, 10, 24, 23, 30, tzinfo=UTC)


def _create(service: ReminderService, **overrides):
    values = {
        "owner_id": "123",
        "source_event_id": "event-1",
        "text": "Call Dana",
        "scheduled_at": NOW + timedelta(hours=1),
        "now": NOW,
    }
    values.update(overrides)
    return service.create(**values)


def test_create_commits_and_duplicate_source_returns_original(sessions) -> None:
    with sessions() as db:
        first = _create(ReminderService(db))
        duplicate = _create(
            ReminderService(db), text="materially different", scheduled_at=NOW + timedelta(days=2)
        )
        assert first.created is True
        assert duplicate.created is False
        assert duplicate.reminder.id == first.reminder.id
        assert duplicate.reminder.text == "Call Dana"
        replay_after_due = _create(
            ReminderService(db),
            scheduled_at=NOW + timedelta(hours=1),
            now=NOW + timedelta(hours=2),
        )
        assert replay_after_due.created is False
        assert replay_after_due.reminder.id == first.reminder.id
        db.rollback()
    with sessions() as db:
        assert db.scalars(select(OwnerReminderRow)).one().id == first.reminder.id


def test_contact_lookup_is_exact_unambiguous_and_never_invented(sessions) -> None:
    with sessions() as db:
        db.add_all(
            [
                CrmContactRow(id="crm-a", fields_json='{"name":"Dana","email":"D@x.com"}'),
                CrmContactRow(id="crm-b", fields_json='{"name":"Dana","phone":"0501234567"}'),
            ]
        )
        db.commit()
        with pytest.raises(ReminderContactAmbiguous) as ambiguous:
            _create(ReminderService(db), contact_query="dana")
        assert {item.id for item in ambiguous.value.candidates} == {"crm-a", "crm-b"}
        with pytest.raises(ReminderValidationError, match="contact_not_found"):
            _create(ReminderService(db), source_event_id="event-2", contact_query="Nobody")
        unlinked = _create(
            ReminderService(db),
            source_event_id="event-3",
            contact_query="Nobody",
            explicit_unlinked=True,
        )
        assert unlinked.contact_unlinked is True
        linked = _create(
            ReminderService(db), source_event_id="event-4", contact_query="d@X.com"
        )
        assert linked.reminder.contact_id == "crm-a"


def test_owner_filter_cas_dispatch_and_terminal_state(sessions) -> None:
    with sessions() as db:
        created = _create(ReminderService(db)).reminder
        service = ReminderService(db)
        assert service.get(owner_id="999", reminder_id=created.id) is None
        with pytest.raises(ReminderConflict):
            service.change(
                owner_id="123", reminder_id=created.id, action="complete", expected_revision=2
            )

        row = db.get(OwnerReminderRow, created.id)
        row.delivery_state = "dispatching"
        db.commit()
        with pytest.raises(ReminderDispatching):
            service.change(
                owner_id="123", reminder_id=created.id, action="complete", expected_revision=1
            )

        row.delivery_state = "accepted"
        db.commit()
        changed = service.change(
            owner_id="123",
            reminder_id=created.id,
            action="snooze",
            expected_revision=1,
            scheduled_at=NOW + timedelta(hours=2),
            now=NOW,
        ).reminder
        assert changed.revision == 2 and changed.delivery_state == "pending"
        completed = service.change(
            owner_id="123", reminder_id=created.id, action="complete", expected_revision=2, now=NOW
        ).reminder
        assert completed.status == "completed"
        with pytest.raises(ReminderConflict):
            service.change(
                owner_id="123",
                reminder_id=created.id,
                action="reschedule",
                expected_revision=3,
                scheduled_at=NOW + timedelta(days=1),
                now=NOW,
            )


def test_snapshot_counts_are_owner_scoped_and_operational(sessions) -> None:
    with sessions() as db:
        first = _create(ReminderService(db), scheduled_at=NOW + timedelta(minutes=1)).reminder
        _create(ReminderService(db), owner_id="456", source_event_id="other-owner")
        row = db.get(OwnerReminderRow, first.id)
        row.scheduled_at = NOW - timedelta(minutes=1)
        row.delivery_state = "unknown"
        db.commit()
        snapshot = ReminderService(db).snapshot(owner_id="123", now=NOW)
        assert snapshot.counts["active"] == 1
        assert snapshot.counts["unknown"] == 1
        assert snapshot.counts["due_pending"] == 0
        assert snapshot.upcoming == ()
