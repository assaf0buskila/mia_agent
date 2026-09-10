"""Defect B: different owner instructions must not return the same acknowledgment.

The old router promoted every unmatched sentence to one status digest, so six
unrelated Hebrew instructions came back identical. These tests pin the repair:
reads answer with real data, unclassified requests say so, and only greetings and
status pings share the digest.
"""


import pytest
from app.api.owner import process_owner_texts as process_inbound_texts
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import ACTION_PROPOSAL_HANDOFF, ACTION_WEBSITE_EDIT
from app.domain.events import Channel
from app.domain.memory import ROLE_MIA, ConversationTurn
from app.domain.owner.followups import (
    needs_data_anchor,
    resolve_owner_reference,
    routed_owner_text,
)
from app.domain.owner.reads import (
    format_pending_approvals_ack,
    format_website_conversations_ack,
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
            _StubApproval(
                lead_id=None,
                action=ACTION_WEBSITE_EDIT,
                resource_id="assafweb-home",
            ),
        ]
    )
    ack = format_pending_approvals_ack(store)
    assert "מחכים לאישור: 2" in ack
    assert "lead_abc" in ack
    assert "assafweb-home" in ack
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
    shallow_line = next(index for index, line in enumerate(lines) if "lead_shallow" in line)
    assert deep_line < shallow_line


def test_website_conversations_read_says_when_it_only_sampled() -> None:
    snapshots = [SalesState(lead_id=f"lead_{index}", workflow_known=True) for index in range(25)]
    ack = format_website_conversations_ack(_StubStore(snapshots=snapshots))
    assert "שיחות מהאתר: 25" in ack
    assert "בדקתי 20 אחרונות" in ack


def _mia(text: str) -> ConversationTurn:
    return ConversationTurn(role=ROLE_MIA, text=text)


def _owner(text: str) -> ConversationTurn:
    return ConversationTurn(role="owner", text=text)


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






def test_a_drill_down_may_use_the_data_anchor_but_a_pronoun_may_not() -> None:
    """Only "what's most interesting?" can be answered from the ranking.

    "Check with him" names a person Mia has to have already mentioned. Letting it
    fall back to whatever ranks highest would aim an instruction at a stranger.
    """
    assert (
        resolve_owner_reference("מה הכי מעניין?", history=[], fallback_lead_id="lead_abc123def456")
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
