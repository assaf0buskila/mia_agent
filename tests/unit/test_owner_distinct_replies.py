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

    def list_captured_website_leads(
        self, *, occurred_from: str = "", occurred_to: str = "", limit: int = 200
    ):
        del occurred_from, occurred_to, limit
        return []

    def count_captured_website_leads(
        self, *, occurred_from: str = "", occurred_to: str = ""
    ) -> int:
        del occurred_from, occurred_to
        return 0

    def list_undelivered_captured_website_leads(self, *, limit: int = 12) -> list[str]:
        del limit
        return []


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
