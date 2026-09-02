from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.api.deps import get_sheets_port
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, build_meeting_booked_event
from app.domain.kpis import KPI_EVENT_TYPES, compute_weekly_kpi, week_start_on
from app.integrations.sheets import FakeSheetsPort, KpiMirrorRow, mirror_kpi
from app.main import app
from fastapi.testclient import TestClient
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


def test_compute_after_website_session_and_message() -> None:
    init_db()
    fake = FakeSheetsPort()
    app.dependency_overrides[get_sheets_port] = lambda: fake
    try:
        with TestClient(app) as client:
            session = client.post("/v1/website/sessions").json()
            session_id = session["session_id"]
            response = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "tell me about automation"},
            )
            assert response.status_code == 200
            assert response.json()["next_action"] == "ask_contact"
            assert fake.kpi_rows == {}
            assert fake.rows == {}
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)


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


def test_mirror_kpi_kill_switch_skips_port() -> None:
    class ExplodingSheetsPort:
        def upsert_lead(self, row: object) -> None:
            del row

        def upsert_source(self, row: object) -> None:
            del row

        def upsert_follow_up(self, row: object) -> None:
            del row

        def upsert_deal(self, row: object) -> None:
            del row

        def upsert_meeting(self, row: object) -> None:
            del row

        def upsert_activity(self, row: object) -> None:
            del row

        def upsert_kpi(self, row: object) -> None:
            raise RuntimeError("kpi mirror must not run when kill switch is on")

        def upsert_content(self, row: object) -> None:
            del row

        def upsert_budget(self, row: object) -> None:
            del row

        def upsert_performance(self, row: object) -> None:
            del row

    written = mirror_kpi(
        sheets=ExplodingSheetsPort(),  # type: ignore[arg-type]
        row=KpiMirrorRow(
            week_start="2026-08-17",
            leads=1,
            meetings_offered=0,
            handoffs=0,
            messages_in=1,
            follow_ups_pending=0,
        ),
        kill_switch=True,
    )
    assert written is False


def test_mirror_kpi_rejects_invalid_week_start() -> None:
    port = FakeSheetsPort()
    written = mirror_kpi(
        sheets=port,
        row=KpiMirrorRow(
            week_start="not-a-date",
            leads=1,
            meetings_offered=0,
            handoffs=0,
            messages_in=1,
            follow_ups_pending=0,
        ),
        kill_switch=False,
    )
    assert written is False
    assert port.kpi_rows == {}


def test_website_identify_then_sell_does_not_mirror_kpis() -> None:
    init_db()
    fake = FakeSheetsPort()
    app.dependency_overrides[get_sheets_port] = lambda: fake
    try:
        with TestClient(app) as client:
            created = client.post("/v1/website/sessions")
            session_id = created.json()["session_id"]
            clinic = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "We run a clinic and miss calls all day."},
            )
            assert clinic.status_code == 200
            identified = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={"text": "let's book a meeting", "phone": "0501234567"},
            )
            assert identified.json()["next_action"] == "handoff"
        assert fake.kpi_rows == {}
        assert fake.rows == {}
    finally:
        app.dependency_overrides.pop(get_sheets_port, None)
