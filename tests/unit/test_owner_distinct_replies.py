"""Defect B: different owner instructions must not return the same acknowledgment.

The old router promoted every unmatched sentence to one status digest, so six
unrelated Hebrew instructions came back identical. These tests pin the repair:
reads answer with real data, unclassified requests say so, and only greetings and
status pings share the digest.
"""

import re

import pytest
from app.api.inbound import process_inbound_texts
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import ACTION_PROPOSAL_HANDOFF
from app.domain.events import Channel
from app.domain.memory import ROLE_MIA, ConversationTurn
from app.domain.owner_followups import (
    needs_data_anchor,
    resolve_owner_reference,
    routed_owner_text,
)
from app.domain.owner_reads import (
    format_pending_approvals_ack,
    format_website_conversations_ack,
)
from app.domain.owner_tasks import (
    OwnerTaskType,
    ack_for_owner_task,
    classify_owner_task,
    promote_unclassified_text_to_status,
)
from app.domain.sales import FitLevel, PainLevel, SalesState
from app.integrations.base import RecordingMessagePort

_OWNER_ID = "700100201"

_DEFECT_B_MESSAGES: tuple[str, ...] = (
    "מה קרה היום?",
    "תראה לי לידים חמים",
    "תספרי לי על ליד מסוים",
    "מה מחכה לאישור?",
    "תנתחי את השיחות מהאתר",
    "מחר תבדקי אם הליד חזר אלינו",
)


async def _owner_reply(
    store: LeadStore,
    port: RecordingMessagePort,
    text: str,
    tag: str,
    *,
    source: str = "",
) -> str:
    item = {"id": f"evt.owner.distinct.{tag}", "from": _OWNER_ID, "text": text}
    if source:
        item["source"] = source
    await process_inbound_texts(
        provider="telegram",
        channel=Channel.TELEGRAM,
        items=[item],
        store=store,
        port=port,
        kill_switch=False,
        owner_ids={_OWNER_ID},
    )
    return port.sent[-1].text


@pytest.mark.asyncio
async def test_six_owner_instructions_get_six_distinct_replies() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        replies: list[str] = []
        for index, text in enumerate(_DEFECT_B_MESSAGES):
            replies.append(await _owner_reply(store, port, text, str(index)))
            db.commit()
        assert len(set(replies)) == len(_DEFECT_B_MESSAGES)
        for reply in replies:
            assert "קונסולת הבעלים" not in reply
    finally:
        db.close()


@pytest.mark.asyncio
async def test_pending_approvals_question_answers_with_data_not_a_digest() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        reply = await _owner_reply(store, port, "מה מחכה לאישור?", "approvals")
        db.commit()
        assert "לאישור" in reply
        assert "קונסולת הבעלים" not in reply
    finally:
        db.close()


@pytest.mark.asyncio
async def test_website_conversations_question_answers_with_data_not_a_digest() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        reply = await _owner_reply(store, port, "תנתחי את השיחות מהאתר", "site")
        db.commit()
        assert "שיחות מהאתר" in reply
        assert "קונסולת הבעלים" not in reply
    finally:
        db.close()


class _StubApproval:
    def __init__(self, *, lead_id: str | None, action: str, resource_id: str = "") -> None:
        self.lead_id = lead_id
        self.action = action
        self.resource_id = resource_id
        self.approval_id = "apr_stub"


class _StubStore:
    """Only the read methods the owner answers use. Keeps ordering deterministic."""

    def __init__(
        self,
        *,
        approvals: list[_StubApproval] | None = None,
        snapshots: list[SalesState] | None = None,
    ) -> None:
        self._approvals = approvals or []
        self._snapshots = snapshots or []

    def list_all_pending_approvals(self) -> list[_StubApproval]:
        return self._approvals

    def list_sales_snapshots(self, *, limit: int = 20) -> list[SalesState]:
        return self._snapshots[:limit]

    def count_sales_snapshots(self) -> int:
        return len(self._snapshots)


def test_pending_approvals_read_is_empty_when_nothing_waits() -> None:
    assert "אין כרגע" in format_pending_approvals_ack(_StubStore())


def test_pending_approvals_read_lists_subjects_and_refuses_blanket_approval() -> None:
    store = _StubStore(
        approvals=[
            _StubApproval(lead_id="lead_abc", action=ACTION_PROPOSAL_HANDOFF),
            _StubApproval(lead_id=None, action="campaign_write", resource_id="camp_9"),
        ]
    )
    ack = format_pending_approvals_ack(store)
    assert "מחכים לאישור: 2" in ack
    assert "lead_abc" in ack
    assert "camp_9" in ack
    assert "None" not in ack
    assert "לא מאשרת הכל ביחד" in ack


def test_website_conversations_read_is_empty_before_any_conversation() -> None:
    assert "אין עדיין" in format_website_conversations_ack(_StubStore())


def test_website_conversations_read_ranks_the_deepest_conversation_first() -> None:
    shallow = SalesState(lead_id="lead_shallow", workflow_known=True)
    deep = SalesState(
        lead_id="lead_deep",
        workflow_known=True,
        manual_step_known=True,
        impact_confirmed=True,
        pain_level=PainLevel.P3,
        fit=FitLevel.POSSIBLE,
        whatsapp_handoff_offered=True,
    )
    ack = format_website_conversations_ack(_StubStore(snapshots=[shallow, deep]))
    assert "שיחות מהאתר: 2" in ack
    assert "discovery משמעותי 1" in ack
    assert "הוצע וואטסאפ 1" in ack
    lines = ack.splitlines()
    deep_line = next(index for index, line in enumerate(lines) if "lead_deep" in line)
    shallow_line = next(
        index for index, line in enumerate(lines) if "lead_shallow" in line
    )
    assert deep_line < shallow_line


def test_website_conversations_read_says_when_it_only_sampled() -> None:
    snapshots = [
        SalesState(lead_id=f"lead_{index}", workflow_known=True) for index in range(25)
    ]
    ack = format_website_conversations_ack(_StubStore(snapshots=snapshots))
    assert "שיחות מהאתר: 25" in ack
    assert "בדקתי 20 אחרונות" in ack


def test_a_real_request_is_never_promoted_to_the_status_digest() -> None:
    text = "מחר תבדקי אם הלקוח חזר אלינו על ההצעה"
    decision = classify_owner_task(text)
    promoted = promote_unclassified_text_to_status(
        decision, inbound_source=None, text=text
    )
    assert promoted.task_type == OwnerTaskType.OPERATOR_SNAPSHOT
    assert promoted.needs_clarification is False
    assert "owner_status" not in promoted.matched_types


def test_greetings_and_status_pings_still_get_the_digest() -> None:
    for text in ("היי", "מה המצב", "hey", "status?"):
        decision = classify_owner_task(text)
        promoted = promote_unclassified_text_to_status(
            decision, inbound_source=None, text=text
        )
        assert promoted.task_type == OwnerTaskType.OWNER_STATUS, text


def test_pending_approvals_question_is_a_read_not_an_approval_decision() -> None:
    """Asking what is waiting must never resolve to the approval decide path."""
    for text in ("מה מחכה לאישור?", "what needs approval", "אישורים ממתינים"):
        decision = classify_owner_task(text)
        assert decision.task_type == OwnerTaskType.PENDING_APPROVALS, text
        assert decision.needs_clarification is False


def test_lead_lookup_without_an_id_asks_which_lead() -> None:
    decision = classify_owner_task("תספרי לי על ליד מסוים")
    assert decision.task_type == OwnerTaskType.LEAD_REVIEW
    assert decision.needs_clarification is True


def _mia(text: str) -> ConversationTurn:
    return ConversationTurn(role=ROLE_MIA, text=text)


def _owner(text: str) -> ConversationTurn:
    return ConversationTurn(role="owner", text=text)


def test_drill_down_after_a_list_opens_the_lead_mia_just_named() -> None:
    history = [
        _owner("תנתחי את השיחות מהאתר"),
        _mia("שיחות מהאתר: 2\nהכי מעניינות:\nlead_abc123def456 · workflow"),
    ]
    routed = routed_owner_text("מה הכי מעניין?", history=history)
    assert "lead_abc123def456" in routed
    assert classify_owner_task(routed).task_type == OwnerTaskType.LEAD_REVIEW
    assert classify_owner_task(routed).needs_clarification is False


def test_pronoun_instruction_binds_to_the_lead_from_the_previous_turn() -> None:
    history = [_mia("סקירת ליד lead_abc123def456: workflow ידוע")]
    routed = routed_owner_text("תפוס אותו", history=history)
    assert routed.endswith("lead_abc123def456")


def test_reference_is_not_resolved_without_history() -> None:
    assert routed_owner_text("מה הכי מעניין?", history=[]) == "מה הכי מעניין?"
    assert resolve_owner_reference("תפוס אותו", history=[]) is None


def test_owner_text_is_never_read_for_a_lead_id_reference() -> None:
    """Only Mia's own replies can anchor a reference, so a typo cannot invent one."""
    history = [_owner("lead_abc123def456")]
    assert resolve_owner_reference("מה הכי מעניין?", history=history) is None


def test_approval_and_scope_instructions_are_never_resolved_from_memory() -> None:
    history = [_mia("סקירת ליד lead_abc123def456: workflow ידוע")]
    for text in ("אשר אותו", "approve it", "סמן אישי אותו", "never automate him"):
        assert resolve_owner_reference(text, history=history) is None, text


def test_an_explicit_id_is_left_alone() -> None:
    history = [_mia("סקירת ליד lead_999888777666")]
    text = "תספרי לי על הליד lead_abc123def456"
    assert routed_owner_text(text, history=history) == text


@pytest.mark.asyncio
async def test_telegram_follow_up_question_uses_the_previous_turn() -> None:
    """Phase 5: a second owner message must build on the first, not restart."""
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="session-owner-memory-1"
        )
        store.save_sales(
            SalesState(
                lead_id=lead_id,
                workflow_known=True,
                manual_step_known=True,
                impact_confirmed=True,
                pain_level=PainLevel.P3,
                fit=FitLevel.POSSIBLE,
            )
        )
        db.commit()
        port = RecordingMessagePort()
        first = await _owner_reply(store, port, "תנתחי את השיחות מהאתר", "memory.1")
        db.commit()
        assert "שיחות מהאתר" in first
        second = await _owner_reply(store, port, "מה הכי מעניין?", "memory.2")
        db.commit()
        assert second != first
        assert "קונסולת הבעלים" not in second
        assert "סקירת ליד" in second or "lead_" in second
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_phase_five_owner_thread_carries_one_subject_across_three_turns() -> (
    None
):
    """The exact goal transcript: brief, then drill down, then an instruction.

    Each turn has to stay about the same conversation without Assaf repeating an
    id, and the third turn must confirm rather than send.
    """
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="session-owner-thread-1"
        )
        store.save_sales(
            SalesState(
                lead_id=lead_id,
                workflow_known=True,
                manual_step_known=True,
                impact_confirmed=True,
                pain_level=PainLevel.P3,
                discovery_turns=4,
            )
        )
        db.commit()
        port = RecordingMessagePort()
        brief = await _owner_reply(store, port, "מה קרה היום?", "thread.1")
        db.commit()
        # The brief answers with website activity but stays lead-id free.
        assert "באתר" in brief
        assert "lead_" not in brief
        drill = await _owner_reply(store, port, "מה הכי מעניין?", "thread.2")
        db.commit()
        # No id was named, so the drill-down has to find the subject in the data.
        subject = re.search(r"lead_[0-9a-f]{12}", drill)
        assert subject is not None, drill
        assert drill != brief
        instruction = await _owner_reply(store, port, "תבדקי איתו את זה", "thread.3")
        db.commit()
        # The instruction stays on the conversation the drill-down opened.
        assert subject.group(0) in instruction
        assert "לא הצלחתי לסווג" not in instruction
        assert "לא אשלח בלי אישור" in instruction
        assert len({brief, drill, instruction}) == 3
        # Confirming is not sending: nothing left the owner channel.
        assert all(message.channel == Channel.TELEGRAM.value for message in port.sent)
    finally:
        db.close()


def test_a_drill_down_may_use_the_data_anchor_but_a_pronoun_may_not() -> None:
    """Only "what's most interesting?" can be answered from the ranking.

    "Check with him" names a person Mia has to have already mentioned. Letting it
    fall back to whatever ranks highest would aim an instruction at a stranger.
    """
    assert (
        resolve_owner_reference(
            "מה הכי מעניין?", history=[], fallback_lead_id="lead_abc123def456"
        )
        == "lead_abc123def456"
    )
    assert (
        resolve_owner_reference(
            "תבדקי איתו את זה", history=[], fallback_lead_id="lead_abc123def456"
        )
        is None
    )


def test_only_a_drill_down_asks_for_the_data_anchor() -> None:
    """Guards the lookup the inbound path skips when it cannot be used."""
    assert needs_data_anchor("מה הכי מעניין?") is True
    assert needs_data_anchor("תבדקי איתו את זה") is False
    assert needs_data_anchor("מה קרה היום?") is False
    assert needs_data_anchor("תספרי לי על הליד lead_abc123def456") is False
    assert needs_data_anchor("אשר אותו") is False


def test_the_transcript_beats_the_data_anchor() -> None:
    history = [_mia("סקירת ליד lead_999888777666")]
    resolved = resolve_owner_reference(
        "מה הכי מעניין?", history=history, fallback_lead_id="lead_abc123def456"
    )
    assert resolved == "lead_999888777666"


def test_an_outreach_instruction_without_a_subject_does_not_invent_one() -> None:
    decision = classify_owner_task("תבדקי איתו את זה")
    assert decision.task_type != OwnerTaskType.LEAD_OUTREACH
    assert decision.needs_clarification is True


def test_an_outreach_instruction_confirms_and_never_reports_a_send() -> None:
    text = "תבדקי איתו את זה lead_abc123def456"
    decision = classify_owner_task(text)
    assert decision.task_type == OwnerTaskType.LEAD_OUTREACH
    assert decision.needs_clarification is True
    ack = ack_for_owner_task(decision, text=text)
    assert "lead_abc123def456" in ack
    assert "לא אשלח בלי אישור" in ack


def test_an_outreach_phrase_never_shadows_an_approval_or_a_scope_change() -> None:
    for text in (
        "אשר את ההצעה lead_abc123def456",
        "סמן אישי 972501234567",
        "human takeover lead_abc123def456",
    ):
        assert classify_owner_task(text).task_type != OwnerTaskType.LEAD_OUTREACH, text


def test_daily_brief_and_hot_leads_combine_into_one_snapshot() -> None:
    for text in (
        "מה קרה היום ומי הכי חם?",
        "what happened today and who is hottest",
    ):
        decision = classify_owner_task(text)
        assert decision.task_type == OwnerTaskType.OPERATOR_SNAPSHOT, text
        assert decision.needs_clarification is False
        assert "daily_brief" in decision.matched_types
        assert "hot_leads" in decision.matched_types


def test_daily_brief_plus_a_write_type_stays_exclusive() -> None:
    decision = classify_owner_task("daily brief and campaign spend")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert "analytics" in decision.matched_types
    assert "daily_brief" in decision.matched_types


@pytest.mark.asyncio
async def test_voice_note_pending_approvals_hits_data_not_understanding_check() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        reply = await _owner_reply(
            store, port, "מה מחכה לאישור?", "voice.approvals", source="audio"
        )
        db.commit()
        assert "לאישור" in reply
        assert "מה שהבנתי" not in reply
        assert "קונסולת הבעלים" not in reply
        assert "אפשר לבקש" not in reply
    finally:
        db.close()


@pytest.mark.asyncio
async def test_empty_audio_does_not_write_or_dump_a_menu() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        reply = await _owner_reply(store, port, "", "voice.empty", source="audio")
        db.commit()
        task = store.get_owner_task(
            provider="telegram", provider_event_id="evt.owner.distinct.voice.empty"
        )
        assert task is not None
        assert task.task_type == "note"
        assert task.status == "needs_clarification"
        assert "לא תפסתי את ההקלטה" in reply
        assert "אפשר לבקש" not in reply
        assert "תכתוב בטקסט" not in reply
        assert "קונסולת הבעלים" not in reply
        assert all(message.channel == Channel.TELEGRAM.value for message in port.sent)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_combined_daily_and_hot_leads_is_one_grounded_reply() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        reply = await _owner_reply(
            store, port, "מה קרה היום ומי הכי חם?", "combine.daily.hot"
        )
        db.commit()
        assert "היום:" in reply
        assert "לידים חמים" in reply
        assert "לא כתבתי כלום" in reply
        assert "מה שהבנתי" not in reply
        assert "קונסולת הבעלים" not in reply
        assert "אפשר לבקש" not in reply
        assert "COMPOSIO" not in reply
        assert "gmail.users" not in reply
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unclassified_long_sentence_gets_snapshot_not_understanding_check() -> (
    None
):
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        reply = await _owner_reply(
            store, port, "תבדקי למה הפניות מהאתר נתקעו", "unclassified.snapshot"
        )
        db.commit()
        task = store.get_owner_task(
            provider="telegram",
            provider_event_id="evt.owner.distinct.unclassified.snapshot",
        )
        assert task is not None
        assert task.task_type == "operator_snapshot"
        assert task.status == "logged"
        assert "לא כתבתי כלום" in reply
        assert "מה שהבנתי" not in reply
        assert "קונסולת הבעלים" not in reply
        assert "אפשר לבקש" not in reply
        assert "היום:" in reply or "לידים חמים" in reply or "לאישור" in reply
    finally:
        db.close()

