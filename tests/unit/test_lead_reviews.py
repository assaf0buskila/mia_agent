import importlib
import inspect

from app.core.capabilities import CapabilityId, require_alive
from app.db.models import LeadReviewRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.lead_reviews import (
    apply_lead_review_policy,
    apply_owner_lead_review,
    build_lead_review_snapshot,
    format_lead_review,
)
from app.domain.sales import NextAction
from sqlalchemy import delete

OWNER_PHONE = "972509990511"
PROSPECT_PHONE = "972509990521"
PROSPECT_PHONE_2 = "972509990522"
UNKNOWN_LEAD_ID = "lead_deadbeefdead"
EMAIL_IN_TEXT = "daniel@example.com"
PHONE_IN_TEXT = "972501234567"


def _open_lead(store: LeadStore, *, external_id: str, channel: Channel = Channel.WHATSAPP) -> str:
    _, lead_id = store.open_channel_lead(channel=channel, external_id=external_id)
    return lead_id


def _delete_review(db, lead_id: str) -> None:
    db.execute(delete(LeadReviewRow).where(LeadReviewRow.lead_id == lead_id))
    db.commit()


def test_format_lead_review_hebrew_no_pii() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id="review_lead_fmt_1")
        db.commit()
        snapshot = build_lead_review_snapshot(store, lead_id=lead_id)
        assert snapshot is not None
        text = format_lead_review(snapshot)
        assert "סקירת ליד" in text
        assert lead_id in text
        assert "התאמה:" in text
        assert snapshot.next_action == ""
        assert "פעולה היסטורית אחרונה: טרם נקבעה" in text
        assert "לא ביצעתי כלום ולא שלחתי הודעה." in text
        assert "@" not in text
        assert PHONE_IN_TEXT not in text
        assert EMAIL_IN_TEXT not in text
    finally:
        db.close()


def test_apply_owner_lead_review_unknown_lead() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        ack = apply_owner_lead_review(
            store,
            text=f"lead review {UNKNOWN_LEAD_ID}",
            kill_switch=False,
            demo_active=False,
        )
        assert ack is not None
        assert "לא מצאתי" in ack
        assert store.get_lead_review(UNKNOWN_LEAD_ID) is None
    finally:
        db.close()


def test_apply_lead_review_persist_and_kill_switch() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id="review_lead_persist_1")
        db.commit()
        snapshot = build_lead_review_snapshot(store, lead_id=lead_id)
        assert snapshot is not None
        apply_lead_review_policy(store, snapshot=snapshot, kill_switch=False, demo_active=False)
        db.commit()
        row = store.get_lead_review(lead_id)
        assert row is not None
        assert row.lead_id == lead_id
        assert row.fit == snapshot.fit
        _delete_review(db, lead_id)
        apply_lead_review_policy(store, snapshot=snapshot, kill_switch=True, demo_active=False)
        db.commit()
        assert store.get_lead_review(lead_id) is None
        formatted = apply_owner_lead_review(
            store,
            text=f"lead review {lead_id}",
            kill_switch=True,
            demo_active=False,
        )
        assert formatted is not None
        assert "סקירת ליד" in formatted
    finally:
        if "lead_id" in locals():
            _delete_review(db, lead_id)
        db.close()


def test_snapshot_preserves_recorded_historical_action() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id="review_lead_historical_1")
        store.upsert_lead_review(
            lead_id=lead_id,
            stage="qualified",
            fit="good",
            pain_level=3,
            next_action=NextAction.OFFER_MEETING.value,
            missing_fields="",
            follow_up_status="",
            follow_up_due_at="",
            meeting_status="",
            deal_stage="",
            conversation_killed=False,
        )
        db.commit()

        snapshot = build_lead_review_snapshot(store, lead_id=lead_id)

        assert snapshot is not None
        assert snapshot.next_action == NextAction.OFFER_MEETING.value
        assert "פעולה היסטורית אחרונה: הצעת פגישה" in format_lead_review(snapshot)
    finally:
        if "lead_id" in locals():
            _delete_review(db, lead_id)
        db.close()


def test_apply_owner_lead_review_demo_returns_none() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id="review_lead_demo_1")
        db.commit()
        result = apply_owner_lead_review(
            store,
            text=f"lead review {lead_id}",
            kill_switch=False,
            demo_active=True,
        )
        assert result is None
        assert store.get_lead_review(lead_id) is None
    finally:
        db.close()




def test_lead_reviews_module_never_imports_message_port_or_metaads() -> None:
    module = importlib.import_module("app.domain.lead_reviews")
    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "METAADS" not in source
    assert "MetaAds" not in source


def test_require_alive_lead_review() -> None:
    require_alive(CapabilityId.LEAD_REVIEW)
