from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, build_meeting_booked_event
from app.domain.kpis import KPI_EVENT_TYPES, compute_weekly_kpi, week_start_on
from sqlalchemy import delete


def test_kpi_event_types_excludes_owner_brief_types() -> None:
    assert "meeting_booked" not in KPI_EVENT_TYPES
    assert "meeting_cancellation_requested" not in KPI_EVENT_TYPES


def test_week_start_monday_for_thursday_asia_jerusalem() -> None:
    thursday = datetime(2026, 8, 20, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    assert week_start_on(now=thursday, timezone="Asia/Jerusalem") == "2026-08-17"


def test_week_start_invalid_timezone_returns_none() -> None:
    assert week_start_on(now=datetime.now(UTC), timezone="Not/A_Zone") is None


def test_compute_invalid_timezone_returns_none() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert compute_weekly_kpi(store, timezone="Not/A_Zone") is None
    finally:
        db.close()


def test_count_canonical_events_counts_meeting_booked() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = "lead_k1p2i3b4o5o6"
        event = build_meeting_booked_event(
            provider="website",
            channel=Channel.WEBSITE,
            lead_id=lead_id,
            conversation_id="sess_kpi_book001",
            scheduled_at="2026-08-22T07:00:00+00:00",
            occurred_at=datetime.now(UTC),
        )
        store.save_canonical_event(provider="website", event=event)
        db.commit()
        occurred = event.occurred_at.isoformat()
        count = store.count_canonical_events(
            event_type="meeting_booked",
            occurred_from=occurred,
            occurred_to="2999-01-01T00:00:00+00:00",
        )
        assert count >= 1
    finally:
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == f"{lead_id}:booked"
            )
        )
        db.commit()
        db.close()


def test_count_canonical_events_rejects_unknown_type() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        store.open_channel_lead(channel=Channel.WEBSITE, external_id="kpi_count_guard_1")
        db.commit()
        assert (
            store.count_canonical_events(
                event_type="behavior",
                occurred_from="1970-01-01T00:00:00+00:00",
                occurred_to="2999-01-01T00:00:00+00:00",
            )
            == 0
        )
    finally:
        db.close()


def test_count_follow_ups_rejects_unknown_status() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.count_follow_ups(status="sent") == 0
    finally:
        db.close()


def test_compute_excludes_events_outside_current_week() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        store.open_channel_lead(
            channel=Channel.WEBSITE,
            external_id="kpi_week_bounds_1",
        )
        db.commit()
        snapshot = compute_weekly_kpi(store, timezone="Asia/Jerusalem")
        assert snapshot is not None
        before_leads = snapshot.leads
        old_instant = datetime(2020, 1, 6, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        old_snapshot = compute_weekly_kpi(
            store,
            timezone="Asia/Jerusalem",
            now=old_instant,
        )
        assert old_snapshot is not None
        assert old_snapshot.leads == 0
        current = compute_weekly_kpi(store, timezone="Asia/Jerusalem")
        assert current is not None
        assert current.leads == before_leads
    finally:
        db.close()
