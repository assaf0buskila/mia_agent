import inspect
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.db.models import ContentIdeaRow, ContentInsightRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import (
    ACTION_LOG,
    CONDITION_NONE,
    TRIGGER_NONE,
    plan_owner_commitment,
)
from app.domain.content_ideas import (
    apply_content_idea_policy,
    apply_owner_content_ideas,
    compute_content_idea_snapshot,
    format_content_ideas_ack,
)
from app.domain.events import Channel
from app.domain.followups import follow_up_due_on
from app.domain.owner.tasks import OwnerTaskType, classify_owner_task
from app.integrations.base import RecordingMessagePort
from sqlalchemy import delete

FROZEN_NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
TIMEZONE = "Asia/Jerusalem"
MEDIA_REELS = "900011"
MEDIA_IMAGE = "900012"
MEDIA_VIDEO = "900013"


def _idea_date() -> str:
    return follow_up_due_on(now=FROZEN_NOW, timezone=TIMEZONE, offset_days=0)


def _seed_insight(
    store: LeadStore,
    *,
    media_id: str,
    media_type: str,
    lead_signals: int,
    views: str = "",
) -> None:
    store.upsert_content_insight(
        media_id=media_id,
        media_type=media_type,
        views=views,
        reach="",
        likes="",
        comments="",
        saved="",
        lead_signals=lead_signals,
    )


def _cleanup(db, idea_date: str, media_ids: list[str]) -> None:
    for media_id in media_ids:
        db.execute(
            delete(ContentInsightRow).where(ContentInsightRow.media_id == media_id)
        )
    db.execute(delete(ContentIdeaRow).where(ContentIdeaRow.idea_date == idea_date))
    db.commit()


def test_classify_content_ideas_english() -> None:
    decision = classify_owner_task("send me content ideas")
    assert decision.task_type == OwnerTaskType.CONTENT_IDEA
    assert decision.needs_clarification is False
    assert decision.matched_types == ["content_idea"]


def test_classify_content_ideas_hebrew() -> None:
    decision = classify_owner_task("תני לי רעיונות לתוכן")
    assert decision.task_type == OwnerTaskType.CONTENT_IDEA
    assert decision.needs_clarification is False


def test_classify_content_performance_still_analytics() -> None:
    decision = classify_owner_task("content performance")
    assert decision.task_type == OwnerTaskType.ANALYTICS
    assert decision.needs_clarification is False


def test_classify_bare_ideas_not_content_idea() -> None:
    decision = classify_owner_task("some ideas for tomorrow")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert "content_idea" not in decision.matched_types


def test_classify_bare_reayonot_not_content_idea() -> None:
    decision = classify_owner_task("יש לי רעיונות")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert "content_idea" not in decision.matched_types


def test_classify_content_ideas_first_pass() -> None:
    decision = classify_owner_task("content ideas and instagram content")
    assert decision.task_type == OwnerTaskType.CONTENT_IDEA
    assert decision.needs_clarification is False
    assert decision.matched_types == ["content_idea"]


def test_plan_content_idea_trigger_none_even_with_due_at() -> None:
    text = "content ideas today"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(
        decision=decision,
        text=text,
        due_at="2026-08-21",
    )
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_LOG


def test_compute_ranks_reels_before_image() -> None:
    init_db()
    db = get_session_factory()()
    idea_date = _idea_date()
    try:
        store = LeadStore(db)
        _seed_insight(
            store,
            media_id=MEDIA_IMAGE,
            media_type="IMAGE",
            lead_signals=1,
            views="100",
        )
        _seed_insight(
            store,
            media_id=MEDIA_REELS,
            media_type="REELS",
            lead_signals=50,
            views="50",
        )
        db.commit()
        snapshot = compute_content_idea_snapshot(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert snapshot is not None
        assert snapshot.idea_date == idea_date
        assert snapshot.kinds[0] == "more_reels"
        ack = format_content_ideas_ack(snapshot)
        assert "עוד רילס" in ack
        assert "אלה רעיונות בלבד. לא כתבתי פוסט ולא פרסמתי." in ack
        assert MEDIA_REELS not in ack
        assert MEDIA_IMAGE not in ack
    finally:
        _cleanup(db, idea_date, [MEDIA_REELS, MEDIA_IMAGE])
        db.close()


def test_empty_insights_persist_empty_kinds() -> None:
    init_db()
    db = get_session_factory()()
    idea_date = _idea_date()
    try:
        store = LeadStore(db)
        store.list_content_insights = lambda: []
        before = store.get_content_idea(idea_date)
        ack = apply_owner_content_ideas(
            store,
            timezone=TIMEZONE,
            kill_switch=False,
            demo_active=False,
            now=FROZEN_NOW,
        )
        db.commit()
        assert ack is not None
        assert "אין נתוני ביצועי תוכן. לא יצרתי רעיונות." in ack
        row = store.get_content_idea(idea_date)
        assert row is not None
        assert row.kinds == []
        if before is None:
            assert row is not None
    finally:
        _cleanup(db, idea_date, [])
        db.close()


def test_missing_views_ranks_after_row_with_views_same_signals() -> None:
    init_db()
    db = get_session_factory()()
    idea_date = _idea_date()
    try:
        store = LeadStore(db)
        _seed_insight(
            store,
            media_id=MEDIA_VIDEO,
            media_type="VIDEO",
            lead_signals=2,
            views="",
        )
        _seed_insight(
            store,
            media_id=MEDIA_IMAGE,
            media_type="IMAGE",
            lead_signals=2,
            views="10",
        )
        db.commit()
        snapshot = compute_content_idea_snapshot(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert snapshot is not None
        assert snapshot.kinds[0] == "more_image"
    finally:
        _cleanup(db, idea_date, [MEDIA_VIDEO, MEDIA_IMAGE])
        db.close()


def test_missing_views_does_not_beat_lower_signal_with_views() -> None:
    init_db()
    db = get_session_factory()()
    idea_date = _idea_date()
    try:
        store = LeadStore(db)
        _seed_insight(
            store,
            media_id=MEDIA_REELS,
            media_type="REELS",
            lead_signals=50,
            views="",
        )
        _seed_insight(
            store,
            media_id=MEDIA_IMAGE,
            media_type="IMAGE",
            lead_signals=1,
            views="999",
        )
        db.commit()
        snapshot = compute_content_idea_snapshot(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert snapshot is not None
        assert snapshot.kinds[0] == "more_reels"
    finally:
        _cleanup(db, idea_date, [MEDIA_REELS, MEDIA_IMAGE])
        db.close()


def test_apply_content_idea_policy_kill_switch_skips_persist() -> None:
    init_db()
    db = get_session_factory()()
    idea_date = _idea_date()
    try:
        store = LeadStore(db)
        _seed_insight(
            store,
            media_id=MEDIA_REELS,
            media_type="REELS",
            lead_signals=2,
        )
        db.commit()
        snapshot = compute_content_idea_snapshot(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert snapshot is not None
        before = store.get_content_idea(idea_date)
        ack = apply_owner_content_ideas(
            store,
            timezone=TIMEZONE,
            kill_switch=True,
            demo_active=False,
            now=FROZEN_NOW,
        )
        db.commit()
        assert ack is not None
        assert "אלה רעיונות בלבד. לא כתבתי פוסט ולא פרסמתי." in ack
        after = store.get_content_idea(idea_date)
        assert after == before
    finally:
        _cleanup(db, idea_date, [MEDIA_REELS])
        db.close()


def test_apply_owner_content_ideas_demo_returns_none_no_persist() -> None:
    init_db()
    db = get_session_factory()()
    idea_date = _idea_date()
    try:
        store = LeadStore(db)
        _seed_insight(
            store,
            media_id=MEDIA_REELS,
            media_type="REELS",
            lead_signals=2,
        )
        db.commit()
        before = store.get_content_idea(idea_date)
        result = apply_owner_content_ideas(
            store,
            timezone=TIMEZONE,
            kill_switch=False,
            demo_active=True,
            now=FROZEN_NOW,
        )
        db.commit()
        assert result is None
        after = store.get_content_idea(idea_date)
        assert after == before
    finally:
        _cleanup(db, idea_date, [MEDIA_REELS])
        db.close()


def test_apply_content_idea_policy_persists_by_idea_date() -> None:
    init_db()
    db = get_session_factory()()
    idea_date = _idea_date()
    try:
        store = LeadStore(db)
        _seed_insight(
            store,
            media_id=MEDIA_REELS,
            media_type="REELS",
            lead_signals=2,
        )
        db.commit()
        snapshot = compute_content_idea_snapshot(
            store,
            timezone=TIMEZONE,
            now=FROZEN_NOW,
        )
        assert snapshot is not None
        apply_content_idea_policy(
            store,
            snapshot=snapshot,
            kill_switch=False,
            demo_active=False,
        )
        db.commit()
        row = store.get_content_idea(idea_date)
        assert row is not None
        assert row.kinds == snapshot.kinds
    finally:
        _cleanup(db, idea_date, [MEDIA_REELS])
        db.close()


def test_content_ideas_module_no_llm_or_publish_ports() -> None:
    import app.domain.content_ideas as module

    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "InstagramInsights" not in source
    assert "openai" not in source.lower()


def test_require_alive_content_ideas() -> None:
    require_alive(CapabilityId.CONTENT_IDEAS)


@pytest.mark.asyncio
async def test_owner_inbound_content_ideas_ack_and_persist() -> None:
    init_db()
    db = get_session_factory()()
    idea_date = _idea_date()
    owner_phone = "972509994601"
    event_id = "evt.ideas.owner.1"
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        _seed_insight(
            store,
            media_id=MEDIA_REELS,
            media_type="REELS",
            lead_signals=2,
        )
        db.commit()
        with patch("app.domain.content_ideas.datetime") as mock_dt:
            mock_dt.now.return_value = FROZEN_NOW
            mock_dt.UTC = UTC
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[
                    {
                        "id": event_id,
                        "from": owner_phone,
                        "text": "content ideas",
                    }
                ],
                store=store,
                port=port,
                kill_switch=False,
                owner_ids={owner_phone},
            )
        db.commit()
        task = store.get_owner_task(provider="whatsapp", provider_event_id=event_id)
        assert task is not None
        assert task.task_type == "content_idea"
        assert task.due_at is None
        assert len(port.sent) == 1
        ack = port.sent[0].text
        assert "רעיונות לתוכן (לא פוסטים מוכנים):" in ack
        assert "אלה רעיונות בלבד. לא כתבתי פוסט ולא פרסמתי." in ack
        assert MEDIA_REELS not in ack
        row = store.get_content_idea(idea_date)
        assert row is not None
        assert row.kinds == ["more_reels"]
    finally:
        _cleanup(db, idea_date, [MEDIA_REELS])
        db.close()
