import json
from datetime import UTC, datetime

import pytest
from app.api.inbound import process_inbound_texts
from app.db.models import IdempotencyRow, OwnerTaskRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import (
    ACTION_ANALYZE,
    ACTION_FOLLOW_UP,
    ACTION_NONE,
    CONDITION_IF_NOT_REPLIED,
    CONDITION_NONE,
    TRIGGER_DUE_DATE,
    TRIGGER_NONE,
    TRIGGER_SPEND_THRESHOLD,
    parse_due_at,
    plan_owner_commitment,
)
from app.domain.events import Channel
from app.domain.owner_tasks import (
    OwnerTaskType,
    ack_for_owner_task,
    classify_owner_task,
    promote_unclassified_text_to_status,
)
from app.integrations.base import RecordingMessagePort
from app.integrations.sheets import FakeSheetsPort
from sqlalchemy import func, select

from tests.unit.sales_copy import assert_discovery_reply


def test_classify_sales_follow_up() -> None:
    decision = classify_owner_task("Schedule a follow-up with Daniel tomorrow.")
    assert decision.task_type == OwnerTaskType.SALES
    assert decision.needs_clarification is False
    due_at = parse_due_at(
        "Schedule a follow-up with Daniel tomorrow.",
        now=datetime.now(UTC),
    )
    ack = ack_for_owner_task(decision, due_at=due_at)
    assert "משימת מכירות" in ack
    assert "לא ביצעתי" in ack
    if due_at:
        assert "ל־" in ack

def test_classify_analytics_campaign_budget() -> None:
    decision = classify_owner_task("pause the campaign budget")
    assert decision.task_type == OwnerTaskType.ANALYTICS
    assert decision.needs_clarification is False
    ack = ack_for_owner_task(decision)
    assert "משימת אנליטיקה" in ack
    assert "תקציבים או מודעות במטא" in ack


def test_classify_research() -> None:
    decision = classify_owner_task("Do competitor research on Acme Corp")
    assert decision.task_type == OwnerTaskType.RESEARCH
    assert decision.needs_clarification is False


def test_classify_zero_match_needs_clarification() -> None:
    decision = classify_owner_task("remind me about the thing later")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert decision.matched_types == []
    ack = ack_for_owner_task(decision)
    assert "מה שהבנתי" in ack
    assert "לא הצלחתי לסווג" in ack
    assert "לא מבצעת" in ack
    assert "הקולית" not in ack


def test_classify_unclassifiable_hmm() -> None:
    decision = classify_owner_task("hmm")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert decision.matched_types == []
    ack = ack_for_owner_task(decision)
    assert "מה שהבנתי" in ack
    assert "לא הצלחתי לסווג" in ack
    assert "לא מבצעת" in ack
    assert "הקולית" not in ack
    voice = ack_for_owner_task(decision, inbound_source="audio")
    assert "לא תפסתי את ההקלטה" in voice
    assert "לא מבצעת כלום" in voice
    assert "הודעה הקולית" not in voice
    promoted = promote_unclassified_text_to_status(decision, inbound_source=None)
    assert promoted.task_type == OwnerTaskType.OWNER_STATUS
    assert promoted.needs_clarification is False
    assert promote_unclassified_text_to_status(
        decision, inbound_source="audio"
    ).needs_clarification is True
    assert "תכתוב בטקסט" not in voice
    assert "אפשר לבקש" not in voice


def test_classify_remember_linkedin_understanding_check() -> None:
    decision = classify_owner_task("remember to check my linkedin")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert decision.matched_types == ["linkedin", "preference"]
    ack = ack_for_owner_task(decision)
    assert "מה שהבנתי" in ack
    assert "לינקדאין או העדפה" in ack
    assert "לא מבצעת" in ack


def test_classify_two_types_needs_clarification() -> None:
    decision = classify_owner_task("pause the ads and update the lead")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert decision.matched_types == ["analytics", "sales"]


def test_classify_ignores_keywords_inside_other_words() -> None:
    decision = classify_owner_task("ideal leadership metadata later")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True


def test_classify_hebrew_research_token() -> None:
    decision = classify_owner_task("תעשי מחקר על המתחרים")
    assert decision.task_type == OwnerTaskType.RESEARCH
    assert decision.needs_clarification is False


def test_classify_hebrew_sales_follow_up() -> None:
    decision = classify_owner_task("תעקבי אחרי דניאל מחר")
    assert decision.task_type == OwnerTaskType.SALES
    assert decision.needs_clarification is False


def test_classify_hebrew_preference() -> None:
    decision = classify_owner_task("מעכשיו אל תגידי דחוף")
    assert decision.task_type == OwnerTaskType.PREFERENCE
    assert decision.needs_clarification is False


def test_classify_campaign_pause_with_id_is_approval() -> None:
    decision = classify_owner_task("pause campaign 1203300000001")
    assert decision.task_type == OwnerTaskType.APPROVAL
    assert decision.needs_clarification is False


def test_classify_campaign_pause_without_id_needs_clarification() -> None:
    decision = classify_owner_task("pause campaign")
    assert decision.task_type == OwnerTaskType.APPROVAL
    assert decision.needs_clarification is True
    ack = ack_for_owner_task(decision, text="pause campaign")
    assert "מה מזהה הקמפיין" in ack


def test_classify_approve_the_proposal() -> None:
    decision = classify_owner_task("approve the proposal")
    assert decision.task_type == OwnerTaskType.APPROVAL
    assert decision.needs_clarification is False


def test_classify_reject_the_proposal() -> None:
    decision = classify_owner_task("reject the proposal")
    assert decision.task_type == OwnerTaskType.APPROVAL
    assert decision.needs_clarification is False


def test_classify_hebrew_approve_proposal() -> None:
    decision = classify_owner_task("אשר את ההצעה")
    assert decision.task_type == OwnerTaskType.APPROVAL
    assert decision.needs_clarification is False


def test_classify_preference_plus_approval_clarification() -> None:
    decision = classify_owner_task("from now on approve the proposal")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert decision.matched_types == ["approval", "preference"]


def test_classify_bare_approve_not_approval() -> None:
    decision = classify_owner_task("approve")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert "approval" not in decision.matched_types


def test_plan_approval_commitment_all_none() -> None:
    text = "approve the proposal lead_abc123456789"
    decision = classify_owner_task("approve the proposal")
    plan = plan_owner_commitment(decision=decision, text=text, due_at=None)
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_NONE


def test_classify_hebrew_analytics_ads_budget() -> None:
    decision = classify_owner_task("מה מצב תקציב המודעות")
    assert decision.task_type == OwnerTaskType.ANALYTICS
    assert decision.needs_clarification is False


def test_classify_hebrew_research_competitor() -> None:
    decision = classify_owner_task("חפשי על המתחרה החדש")
    assert decision.task_type == OwnerTaskType.RESEARCH
    assert decision.needs_clarification is False


def test_classify_hebrew_support_site_down() -> None:
    decision = classify_owner_task("האתר נפל")
    assert decision.task_type == OwnerTaskType.SUPPORT
    assert decision.needs_clarification is False


def test_classify_hebrew_support_invoice_not_sales() -> None:
    decision = classify_owner_task("תשלחי חשבונית ללקוח")
    assert decision.task_type == OwnerTaskType.SUPPORT
    assert decision.needs_clarification is False


def test_classify_hebrew_meeting_debrief() -> None:
    decision = classify_owner_task("אחרי הפגישה דיברנו עם יעל")
    assert decision.task_type == OwnerTaskType.MEETING_DEBRIEF
    assert decision.needs_clarification is False


def test_classify_hebrew_dual_sales_analytics() -> None:
    decision = classify_owner_task("תעקבי אחרי הליד ותבדקי את הקמפיין")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert decision.matched_types == ["analytics", "sales"]


def test_classify_hebrew_tikun_is_not_preference() -> None:
    decision = classify_owner_task("יש תיקון קטן בטקסט")
    assert decision.needs_clarification is True
    assert decision.task_type == OwnerTaskType.NOTE


def test_classify_hebrew_hotzaa_is_not_analytics() -> None:
    decision = classify_owner_task("מה ההוצאה החודשית על המשרד")
    assert decision.needs_clarification is True
    assert "analytics" not in decision.matched_types


def test_classify_linkedin() -> None:
    decision = classify_owner_task("how's my linkedin")
    assert decision.task_type == OwnerTaskType.LINKEDIN
    assert decision.needs_clarification is False
    ack = ack_for_owner_task(decision)
    assert "משימת לינקדאין" in ack
    assert "לא אפרסם, לא אגיב ולא אשלח הודעות בלינקדאין" in ack


def test_classify_linkedin_and_research_needs_clarification() -> None:
    decision = classify_owner_task("linkedin research on Acme")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    decision2 = classify_owner_task("Do competitor research on my linkedin profile")
    assert decision2.task_type == OwnerTaskType.NOTE
    assert decision2.needs_clarification is True


def test_plan_sales_follow_up_if_not_replied_no_name_on_row() -> None:
    text = "Schedule a follow-up with Daniel tomorrow if he has not replied."
    decision = classify_owner_task(text)
    due_at = parse_due_at(text, now=datetime.now(UTC))
    plan = plan_owner_commitment(decision=decision, text=text, due_at=due_at)
    assert plan.trigger == TRIGGER_DUE_DATE
    assert plan.condition == CONDITION_IF_NOT_REPLIED
    assert plan.action == ACTION_FOLLOW_UP
    assert "Daniel" not in plan.trigger
    assert "Daniel" not in plan.condition
    assert "Daniel" not in plan.action


def test_plan_hebrew_sales_if_not_replied() -> None:
    text = "תעקבי מחר אם לא יענה"
    decision = classify_owner_task(text)
    due_at = parse_due_at(text, now=datetime.now(UTC))
    plan = plan_owner_commitment(decision=decision, text=text, due_at=due_at)
    assert plan.trigger == TRIGGER_DUE_DATE
    assert plan.condition == CONDITION_IF_NOT_REPLIED
    assert plan.action == ACTION_FOLLOW_UP


def test_plan_analytics_campaign_spend_no_trigger() -> None:
    text = "how's the campaign spend"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(decision=decision, text=text, due_at=None)
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_ANALYZE


def test_ack_spend_threshold_analytics() -> None:
    text = "analyze the campaign after spend reaches the threshold"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(decision=decision, text=text, due_at=None)
    ack = ack_for_owner_task(decision, trigger=plan.trigger)
    assert "תקציב המוגדר" in ack
    assert "לא ביצעתי" in ack
    assert "threshold" not in ack.lower()
    assert "5000" not in ack


def test_plan_analytics_spend_threshold_when_spend_reaches() -> None:
    text = "analyze the campaign after spend reaches the threshold"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(decision=decision, text=text, due_at=None)
    assert plan.trigger == TRIGGER_SPEND_THRESHOLD
    assert plan.action == ACTION_ANALYZE


def test_plan_preference_commitment_all_none() -> None:
    text = "from now on never say tomorrow"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(decision=decision, text=text, due_at=None)
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_NONE


def test_plan_understanding_check_commitment_all_none() -> None:
    text = "remind me tomorrow about the thing"
    decision = classify_owner_task(text)
    plan = plan_owner_commitment(decision=decision, text=text, due_at=None)
    assert plan.trigger == TRIGGER_NONE
    assert plan.condition == CONDITION_NONE
    assert plan.action == ACTION_NONE


def test_ack_if_not_replied_hebrew_suffix_no_daniel() -> None:
    text = "Schedule a follow-up with Daniel tomorrow if he has not replied."
    decision = classify_owner_task(text)
    due_at = parse_due_at(text, now=datetime.now(UTC))
    plan = plan_owner_commitment(decision=decision, text=text, due_at=due_at)
    ack = ack_for_owner_task(decision, due_at=due_at, condition=plan.condition)
    assert "רק אם לא תהיה תשובה" in ack
    assert "לא ביצעתי" in ack
    assert "Daniel" not in ack


@pytest.mark.asyncio
async def test_owner_inbound_persists_structured_commitment() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        owner_text = (
            "Schedule a follow-up with Daniel tomorrow if he has not replied."
        )
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.commitment.1",
                    "from": "972509990030",
                    "text": owner_text,
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990030"},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.commitment.1"
        )
        assert task is not None
        assert task.task_type == "sales"
        assert task.status == "logged"
        assert task.summary == ""
        assert "Daniel" not in (task.summary or "")
        assert task.trigger == TRIGGER_DUE_DATE
        assert task.condition == CONDITION_IF_NOT_REPLIED
        assert task.action == ACTION_FOLLOW_UP
        expected_due = parse_due_at(owner_text, now=datetime.now(UTC))
        assert task.due_at == expected_due
        assert len(port.sent) == 1
        assert "רק אם לא תהיה תשובה" in port.sent[0].text
        assert "לא ביצעתי" in port.sent[0].text
        assert "Daniel" not in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_persists_spend_threshold_commitment() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        owner_text = "analyze the campaign after spend reaches the threshold"
        event_id = "evt.owner.spend.inbound.1"
        owner_phone = "972509990211"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": event_id,
                    "from": owner_phone,
                    "text": owner_text,
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
        assert task.task_type == "analytics"
        assert task.status == "logged"
        assert task.trigger == TRIGGER_SPEND_THRESHOLD
        assert task.action == ACTION_ANALYZE
        assert task.due_at is None
        assert store.get_campaign_pacing() is None
        assert len(port.sent) == 1
        assert "תקציב המוגדר" in port.sent[0].text
        assert "לא ביצעתי" in port.sent[0].text
        assert "קצב:" not in port.sent[0].text
        assert owner_text not in port.sent[0].text
        assert "threshold" not in port.sent[0].text.lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_audio_persists_task_no_sheets_no_sales_graph() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.task.1",
                    "from": "972509990002",
                    "text": "Schedule a follow-up with Daniel tomorrow.",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990002"},
            sheets=sheets,
        )
        db.commit()
        owner_text = "Schedule a follow-up with Daniel tomorrow."
        task = store.get_owner_task(provider="whatsapp", provider_event_id="evt.owner.task.1")
        assert task is not None
        assert task.task_type == "sales"
        assert task.status == "logged"
        expected_due = parse_due_at(owner_text, now=datetime.now(UTC))
        assert task.due_at == expected_due
        assert sheets.rows == {}
        assert len(port.sent) == 1
        assert "משימת מכירות" in port.sent[0].text
        assert "Daniel" not in port.sent[0].text
        if expected_due:
            assert "ל־" in port.sent[0].text
        assert "how the business works" not in port.sent[0].text
        assert "יום רגיל בעסק" not in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_without_date_no_due_at() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.no.date.1",
                    "from": "972509990020",
                    "text": "pause the campaign budget",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990020"},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.no.date.1"
        )
        assert task is not None
        assert task.due_at is None
        assert len(port.sent) == 1
        assert "ל־" not in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_understanding_check_no_due_at() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.clarify.due.1",
                    "from": "972509990021",
                    "text": "remind me tomorrow about the thing",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990021"},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.clarify.due.1"
        )
        assert task is not None
        # Unmatched long text stays a NOTE for the owner agent (ADR-030). Tests
        # have no model, so the canned fallback is the Understanding Check.
        # Writes stay off; no due date is parsed; the digest is not dumped.
        assert task.status == "needs_clarification"
        assert task.task_type == "note"
        assert task.due_at is None
        assert len(port.sent) == 1
        assert "ל־" not in port.sent[0].text
        assert "לא כתבתי כלום" not in port.sent[0].text
        assert "קונסולת הבעלים" not in port.sent[0].text
        assert "אפשר לבקש" not in port.sent[0].text
        assert "משפך" not in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_preference_does_not_persist_due_at() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.pref.due.1",
                    "from": "972509990022",
                    "text": "from now on talk shorter tomorrow",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990022"},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.pref.due.1"
        )
        assert task is not None
        assert task.task_type == "preference"
        assert task.due_at is None
        assert len(port.sent) == 1
        assert "ל־" not in port.sent[0].text
        assert "הצעת העדפה" in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_save_owner_task_idempotent() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        kwargs = {
            "provider": "whatsapp",
            "provider_event_id": "evt.owner.task.dup",
            "channel": "whatsapp",
            "external_id": "972509990003",
            "task_type": "sales",
            "status": "logged",
        }
        store.save_owner_task(**kwargs)
        store.save_owner_task(**kwargs)
        db.commit()
        task = store.get_owner_task(provider="whatsapp", provider_event_id="evt.owner.task.dup")
        assert task is not None
        assert task.task_type == "sales"
        count = db.scalar(
            select(func.count())
            .select_from(OwnerTaskRow)
            .where(
                OwnerTaskRow.provider == "whatsapp",
                OwnerTaskRow.provider_event_id == "evt.owner.task.dup",
            )
        )
        assert count == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_analytics_budget_logged_not_executed() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.analytics.1",
                    "from": "972509990004",
                    "text": "pause the campaign budget",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990004"},
            sheets=sheets,
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.analytics.1"
        )
        assert task is not None
        assert task.task_type == "analytics"
        assert task.status == "logged"
        assert sheets.rows == {}
        ack = port.sent[0].text
        assert "תקציבים או מודעות במטא" in ack
        assert "how the business works" not in ack
        assert "יום רגיל בעסק" not in ack
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_text_analytics_logged_not_sales_graph() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.text.analytics.1",
                    "from": "972509990011",
                    "text": "pause the campaign budget",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990011"},
            sheets=sheets,
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.text.analytics.1"
        )
        assert task is not None
        assert task.task_type == "analytics"
        assert task.status == "logged"
        assert sheets.rows == {}
        ack = port.sent[0].text
        assert "תקציבים או מודעות במטא" in ack
        assert "how the business works" not in ack
        assert "יום רגיל בעסק" not in ack
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_text_hmm_understanding_check_no_sales_graph() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.text.hmm.1",
                    "from": "972509990012",
                    "text": "hmm",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990012"},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.text.hmm.1"
        )
        assert task is not None
        assert task.task_type == "owner_status"
        assert task.status == "logged"
        ack = port.sent[0].text
        assert ack == "היי אסף, אני כאן."
        assert "קונסולת הבעלים" not in ack
        assert "סיכום יומי" not in ack
        assert "מה שהבנתי" not in ack
        assert "how the business works" not in ack
        assert "יום רגיל בעסק" not in ack
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_audio_hmm_understanding_check_stays() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.audio.hmm.1",
                    "from": "972509990022",
                    "text": "hmm",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990022"},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.audio.hmm.1"
        )
        assert task is not None
        assert task.task_type == "owner_status"
        assert task.status == "logged"
        ack = port.sent[0].text
        assert "אני כאן" in ack
        assert "הודעה הקולית" not in ack
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_text_not_saved_as_transcript() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.text.no.transcript.1",
                    "from": "972509990013",
                    "text": "pause the campaign budget",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990013"},
        )
        db.commit()
        assert (
            store.get_transcript(
                provider="whatsapp", provider_event_id="evt.owner.text.no.transcript.1"
            )
            is None
        )
        assert len(port.sent) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_audio_no_owner_task_sales_graph_runs() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.prospect.audio.1",
                    "from": "972501111111",
                    "text": "hi there",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={"972509990004"},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.prospect.audio.1"
        )
        assert task is None
        assert len(port.sent) == 1
        assert_discovery_reply(port.sent[0].text)
    finally:
        db.close()


def test_classify_calendar_phrase_english() -> None:
    decision = classify_owner_task("check my calendar")
    assert decision.task_type == OwnerTaskType.CALENDAR
    assert decision.needs_clarification is False


def test_classify_calendar_phrase_hebrew() -> None:
    decision = classify_owner_task("מה פנוי ביומן")
    assert decision.task_type == OwnerTaskType.CALENDAR
    assert decision.needs_clarification is False


@pytest.mark.asyncio
async def test_owner_inbound_persists_owner_task_idempotency_claim() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        event_id = "evt.owner.idem.claim.1"
        owner_phone = "972509990401"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": event_id,
                    "from": owner_phone,
                    "text": "pause the campaign budget",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={owner_phone},
        )
        db.commit()
        claim_key = f"whatsapp:{event_id}"
        row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == "owner_task",
                IdempotencyRow.key == claim_key,
            )
        ).one()
        assert row.status == "completed"
        assert json.loads(row.result_json) == {"ok": True}
        assert store.get_owner_task(provider="whatsapp", provider_event_id=event_id) is not None
        assert len(port.sent) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_duplicate_save_owner_task_after_claim_no_second_in_flight() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        claim_key = "whatsapp:evt.owner.store.dup.1"
        assert store.claim_operation(scope="owner_task", key=claim_key) is True
        kwargs = {
            "provider": "whatsapp",
            "provider_event_id": "evt.owner.store.dup.1",
            "channel": "whatsapp",
            "external_id": "972509990402",
            "task_type": "sales",
            "status": "logged",
        }
        store.save_owner_task(**kwargs)
        store.complete_operation(
            scope="owner_task",
            key=claim_key,
            result_json='{"ok": true}',
        )
        assert store.claim_operation(scope="owner_task", key=claim_key) is False
        store.save_owner_task(**kwargs)
        db.commit()
        count = db.scalar(
            select(func.count())
            .select_from(OwnerTaskRow)
            .where(
                OwnerTaskRow.provider == "whatsapp",
                OwnerTaskRow.provider_event_id == "evt.owner.store.dup.1",
            )
        )
        assert count == 1
        in_flight = db.scalar(
            select(func.count())
            .select_from(IdempotencyRow)
            .where(
                IdempotencyRow.scope == "owner_task",
                IdempotencyRow.key == claim_key,
                IdempotencyRow.status == "in_flight",
            )
        )
        assert in_flight == 0
    finally:
        db.close()


def test_transcribed_audio_pending_approvals_classifies_like_typed_text() -> None:
    decision = classify_owner_task("מה מחכה לאישור?")
    promoted = promote_unclassified_text_to_status(
        decision, inbound_source="audio", text="מה מחכה לאישור?"
    )
    assert promoted.task_type == OwnerTaskType.PENDING_APPROVALS
    assert promoted.needs_clarification is False


def test_empty_audio_stays_on_the_understanding_check() -> None:
    decision = classify_owner_task("")
    promoted = promote_unclassified_text_to_status(
        decision, inbound_source="audio", text=""
    )
    assert promoted.task_type == OwnerTaskType.NOTE
    assert promoted.needs_clarification is True
    ack = ack_for_owner_task(promoted, inbound_source="audio")
    assert "לא תפסתי את ההקלטה" in ack
    assert "אפשר לבקש" not in ack
    assert "תכתוב בטקסט" not in ack


def test_long_unmatched_sentence_stays_a_note_for_the_agent() -> None:
    text = "תבדקי למה הפניות מהאתר נתקעו"
    decision = classify_owner_task(text)
    promoted = promote_unclassified_text_to_status(
        decision, inbound_source=None, text=text
    )
    assert promoted.task_type == OwnerTaskType.NOTE
    assert promoted.needs_clarification is True
    assert promote_unclassified_text_to_status(
        decision, inbound_source="audio", text=text
    ).task_type == OwnerTaskType.NOTE


def test_email_check_is_not_a_snapshot() -> None:
    text = "אני רוצה שתבדקי את המייל שלי"
    decision = classify_owner_task(text)
    promoted = promote_unclassified_text_to_status(
        decision, inbound_source=None, text=text
    )
    assert promoted.task_type == OwnerTaskType.NOTE
    assert promoted.task_type != OwnerTaskType.OPERATOR_SNAPSHOT
    assert promoted.task_type != OwnerTaskType.GMAIL_SUMMARY


def test_short_mail_paraphrases_stay_notes_for_the_agent() -> None:
    for text in (
        "תבדקי את המייל",
        "check my inbox",
        "can you look at my emails",
        "תוכלי להסתכל על המיילים",
        "יש משהו חדש בתיבה",
    ):
        decision = classify_owner_task(text)
        promoted = promote_unclassified_text_to_status(
            decision, inbound_source=None, text=text
        )
        assert promoted.task_type == OwnerTaskType.NOTE, text
        assert promoted.task_type != OwnerTaskType.OWNER_STATUS, text
        assert promoted.task_type != OwnerTaskType.GMAIL_SUMMARY, text
        assert promoted.task_type != OwnerTaskType.GMAIL_DRAFT, text


def test_gmail_draft_classifies() -> None:
    decision = classify_owner_task("שלח מייל ל dane@example.com נושא: היי והתוכן שלום")
    assert decision.task_type == OwnerTaskType.GMAIL_DRAFT
    assert decision.needs_clarification is False


def test_greeting_ack_is_a_short_hello() -> None:
    decision = classify_owner_task("היי מיה")
    promoted = promote_unclassified_text_to_status(
        decision, inbound_source=None, text="היי מיה"
    )
    assert promoted.task_type == OwnerTaskType.OWNER_STATUS
    ack = ack_for_owner_task(promoted)
    assert ack == "היי אסף, אני כאן."
    assert "אפשר לבקש" not in ack
    assert "משפך" not in ack

