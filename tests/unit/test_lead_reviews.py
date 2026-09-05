import importlib
import inspect

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.db.models import LeadReviewRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import (
    ACTION_LOG,
    CONDITION_NONE,
    TRIGGER_NONE,
    plan_owner_commitment,
)
from app.domain.events import Channel
from app.domain.lead_reviews import (
    apply_lead_review_policy,
    apply_owner_lead_review,
    build_lead_review_snapshot,
    format_lead_review,
)
from app.domain.owner.tasks import (
    OwnerTaskType,
    ack_for_owner_task,
    classify_owner_task,
)
from app.integrations.base import RecordingMessagePort
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


def test_classify_lead_review_with_lead_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=PROSPECT_PHONE)
        db.commit()
        decision = classify_owner_task(f"lead review {lead_id}")
        assert decision.task_type == OwnerTaskType.LEAD_REVIEW
        assert decision.needs_clarification is False
        assert decision.matched_types == ["lead_review"]
        assert decision.task_type != OwnerTaskType.SALES
    finally:
        db.close()


def test_classify_lead_review_without_id_needs_clarification() -> None:
    decision = classify_owner_task("סקירת ליד")
    assert decision.task_type == OwnerTaskType.LEAD_REVIEW
    assert decision.needs_clarification is True
    ack = ack_for_owner_task(decision)
    assert "מה מזהה הליד" in ack


def test_classify_meeting_debrief_not_lead_review() -> None:
    decision = classify_owner_task("סיכום פגישה lead_abc123456789")
    assert decision.task_type == OwnerTaskType.MEETING_DEBRIEF
    assert decision.needs_clarification is False


def test_classify_daily_brief_not_lead_review() -> None:
    decision = classify_owner_task("daily brief")
    assert decision.task_type == OwnerTaskType.DAILY_BRIEF
    assert decision.needs_clarification is False


def test_classify_instagram_content_is_analytics() -> None:
    decision = classify_owner_task("analyze instagram content")
    assert decision.task_type == OwnerTaskType.ANALYTICS
    assert decision.needs_clarification is False


def test_classify_review_lead_id_after_scrub_still_lead_review() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=PROSPECT_PHONE_2)
        db.commit()
        decision = classify_owner_task(f"review {lead_id}")
        assert decision.task_type == OwnerTaskType.LEAD_REVIEW
        assert decision.needs_clarification is False
        assert decision.task_type != OwnerTaskType.SALES
    finally:
        db.close()


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
        assert "פעולה הבאה:" in text
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
        apply_lead_review_policy(
            store, snapshot=snapshot, kill_switch=False, demo_active=False
        )
        db.commit()
        row = store.get_lead_review(lead_id)
        assert row is not None
        assert row.lead_id == lead_id
        assert row.fit == snapshot.fit
        _delete_review(db, lead_id)
        apply_lead_review_policy(
            store, snapshot=snapshot, kill_switch=True, demo_active=False
        )
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


@pytest.mark.asyncio
async def test_owner_inbound_lead_review_persist_and_ack() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        lead_id = _open_lead(
            store,
            external_id="review_lead_inbound_1",
            channel=Channel.WHATSAPP,
        )
        db.commit()
        event_id = "evt.owner.review.inbound.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": event_id,
                "from": OWNER_PHONE,
                "text": f"lead review {lead_id}",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
        )
        db.commit()
        task = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert task is not None
        assert task.task_type == "lead_review"
        assert task.due_at is None
        assert task.trigger == TRIGGER_NONE
        row = store.get_lead_review(lead_id)
        assert row is not None
        assert len(port.sent) == 1
        assert "סקירת ליד" in port.sent[0].text
        assert "לא שלחתי" in port.sent[0].text
        assert "@" not in port.sent[0].text
        assert PHONE_IN_TEXT not in port.sent[0].text
    finally:
        if "lead_id" in locals():
            _delete_review(db, lead_id)
        db.close()


def test_lead_reviews_module_never_imports_message_port_or_metaads() -> None:
    module = importlib.import_module("app.domain.lead_reviews")
    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "METAADS" not in source
    assert "MetaAds" not in source


def test_require_alive_lead_review() -> None:
    require_alive(CapabilityId.LEAD_REVIEW)


def test_plan_lead_review_trigger_none() -> None:
    text = "lead review today"
    decision = classify_owner_task("lead review lead_abc123456789")
    plan = plan_owner_commitment(
        decision=decision,
        text=text,
        due_at="2026-08-21",
    )
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_LOG
