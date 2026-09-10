import inspect
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.capabilities import CapabilityId, require_alive
from app.db.models import CanonicalEventRow, OwnerBriefRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import (
    Channel,
    build_meeting_booked_event,
    build_meeting_cancellation_requested_event,
)
from app.domain.kpis import KPI_EVENT_TYPES
from app.domain.owner.briefs import (
    apply_owner_brief_policy,
    compute_daily_brief,
    format_daily_brief,
)
from sqlalchemy import delete


def test_compute_invalid_timezone_returns_none() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert compute_daily_brief(store, timezone="Not/A_Zone") is None
    finally:
        db.close()


def test_compute_leads_increment_for_today() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        timezone = "Asia/Jerusalem"
        before = compute_daily_brief(store, timezone=timezone)
        assert before is not None
        before_leads = before.leads
        store.open_channel_lead(
            channel=Channel.WEBSITE,
            external_id="brief_daily_lead_1",
        )
        db.commit()
        after = compute_daily_brief(store, timezone=timezone)
        assert after is not None
        assert after.leads >= before_leads + 1
        old_instant = datetime(2020, 1, 6, 12, 0, tzinfo=ZoneInfo(timezone))
        old_snapshot = compute_daily_brief(
            store,
            timezone=timezone,
            now=old_instant,
        )
        assert old_snapshot is not None
        assert old_snapshot.leads == 0
    finally:
        db.close()


def test_format_daily_brief_hebrew_labels_no_lead_ids() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        snapshot = compute_daily_brief(store, timezone="Asia/Jerusalem")
        assert snapshot is not None
        text = format_daily_brief(snapshot)
        assert "סיכום יומי" in text
        assert "לידים:" in text
        assert "פגישות הוצעו:" in text
        assert "פגישות נקבעו:" in text
        assert "בקשות ביטול:" in text
        assert "הודעות נכנסות:" in text
        assert "מעקבים לביצוע היום:" in text
        assert "לא ביצעתי משימות ולא שלחתי מעקבים." in text
        assert "@" not in text
        assert "lead_" not in text
    finally:
        db.close()


def test_apply_owner_brief_policy_persists_by_brief_date() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        timezone = "Asia/Jerusalem"
        snapshot = compute_daily_brief(store, timezone=timezone)
        assert snapshot is not None
        apply_owner_brief_policy(
            store,
            snapshot=snapshot,
            kill_switch=False,
            demo_active=False,
        )
        db.commit()
        row = store.get_owner_brief(snapshot.brief_date)
        assert row is not None
        assert row.leads == snapshot.leads
        assert row.follow_ups_due == snapshot.follow_ups_due
        assert row.meetings_booked == snapshot.meetings_booked
        assert row.cancellation_requests == snapshot.cancellation_requests
    finally:
        if "snapshot" in locals() and snapshot is not None:
            db.execute(delete(OwnerBriefRow).where(OwnerBriefRow.brief_date == snapshot.brief_date))
            db.commit()
        db.close()


def test_apply_owner_brief_policy_kill_switch_skips_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        snapshot = compute_daily_brief(store, timezone="Asia/Jerusalem")
        assert snapshot is not None
        before = store.get_owner_brief(snapshot.brief_date)
        before_leads = None if before is None else before.leads
        apply_owner_brief_policy(
            store,
            snapshot=snapshot,
            kill_switch=True,
            demo_active=False,
        )
        db.commit()
        after = store.get_owner_brief(snapshot.brief_date)
        if before is None:
            assert after is None
        else:
            assert after is not None
            assert after.leads == before_leads
    finally:
        db.close()


def test_apply_owner_brief_policy_demo_skips_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        snapshot = compute_daily_brief(store, timezone="Asia/Jerusalem")
        assert snapshot is not None
        before = store.get_owner_brief(snapshot.brief_date)
        before_leads = None if before is None else before.leads
        apply_owner_brief_policy(
            store,
            snapshot=snapshot,
            kill_switch=False,
            demo_active=True,
        )
        db.commit()
        after = store.get_owner_brief(snapshot.brief_date)
        if before is None:
            assert after is None
        else:
            assert after is not None
            assert after.leads == before_leads
    finally:
        db.close()


def test_count_follow_ups_due_on_unknown_status_returns_zero() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.count_follow_ups_due_on(due_on="2026-08-21", status="sent") == 0
        assert store.count_follow_ups_due_on(due_on="not-a-date", status="pending") == 0
    finally:
        db.close()


def test_kpi_event_types_unchanged() -> None:
    assert KPI_EVENT_TYPES == (
        "lead_created",
        "meeting_offered",
        "handoff",
        "message_in",
    )


def test_compute_meetings_booked_increment_for_today() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        timezone = "Asia/Jerusalem"
        lead_id = "lead_b1f2b3o4o0k1"
        before = compute_daily_brief(store, timezone=timezone)
        assert before is not None
        before_booked = before.meetings_booked
        event = build_meeting_booked_event(
            provider="website",
            channel=Channel.WEBSITE,
            lead_id=lead_id,
            conversation_id="sess_brfbook001",
            scheduled_at="2026-08-22T07:00:00+00:00",
            occurred_at=datetime.now(UTC),
        )
        store.save_canonical_event(provider="website", event=event)
        db.commit()
        after = compute_daily_brief(store, timezone=timezone)
        assert after is not None
        assert after.meetings_booked >= before_booked + 1
        old_instant = datetime(2020, 1, 6, 12, 0, tzinfo=ZoneInfo(timezone))
        old_snapshot = compute_daily_brief(
            store,
            timezone=timezone,
            now=old_instant,
        )
        assert old_snapshot is not None
        assert old_snapshot.meetings_booked == 0
    finally:
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == f"{lead_id}:booked"
            )
        )
        db.commit()
        db.close()


def test_compute_cancellation_requests_increment_for_today() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        timezone = "Asia/Jerusalem"
        lead_id = "lead_c1a2n3c4e5l6"
        before = compute_daily_brief(store, timezone=timezone)
        assert before is not None
        before_requests = before.cancellation_requests
        event = build_meeting_cancellation_requested_event(
            provider="website",
            channel=Channel.WEBSITE,
            lead_id=lead_id,
            conversation_id="sess_brfcanc001",
            occurred_at=datetime.now(UTC),
        )
        store.save_canonical_event(provider="website", event=event)
        db.commit()
        after = compute_daily_brief(store, timezone=timezone)
        assert after is not None
        assert after.cancellation_requests >= before_requests + 1
        old_instant = datetime(2020, 1, 6, 12, 0, tzinfo=ZoneInfo(timezone))
        old_snapshot = compute_daily_brief(
            store,
            timezone=timezone,
            now=old_instant,
        )
        assert old_snapshot is not None
        assert old_snapshot.cancellation_requests == 0
    finally:
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == f"{lead_id}:cancellation_requested"
            )
        )
        db.commit()
        db.close()




def test_owner_briefs_module_no_message_or_meta_ports() -> None:
    import app.domain.owner.briefs as module

    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "METAADS" not in source
    assert "MetaAds" not in source
    assert "CREATE_EVENT" not in source


def test_require_alive_owner_brief() -> None:
    require_alive(CapabilityId.OWNER_BRIEF)
