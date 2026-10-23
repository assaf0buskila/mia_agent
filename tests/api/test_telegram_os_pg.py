"""V2-28: Telegram callbacks for ledger cards (V2_PG_CONTRACTS.md sections 13 and 20).

The callback commits the ApprovalRow CAS, then the worker thread decides on the ledger with the
decision UUID stored in the card's parameters. Recovery is a receipt read by that same UUID: a
ledger card never becomes a sticky ``unknown``. FakeLedger only; no Telegram or network call.
"""

from __future__ import annotations

import pytest
from app.api import telegram as telegram_api
from app.db.store import LeadStore
from app.domain.owner.callbacks import approval_token
from app.integrations.base import RecordingMessagePort

from tests.services.os_v2_28_support import (
    OWNER,
    LedgerWorld,
    committed_probe,
    ledger_owner_action,
    make_store_factory,
    provider_claims,
)


class _CallbackPort(RecordingMessagePort):
    def __init__(self) -> None:
        super().__init__()
        self.answer_texts: list[str] = []
        self.edited: list[str] = []

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        self.answer_texts.append(text)

    async def edit_message_text(self, *, chat_id, message_id, text, parse_mode=None,
                                clear_markup=True) -> None:
        self.edited.append(text)


@pytest.fixture
def factory(tmp_path, monkeypatch):
    built = make_store_factory(tmp_path)
    # The decision worker opens its own session from the application factory.
    monkeypatch.setattr("app.db.session.get_session_factory", lambda: built)
    return built


@pytest.fixture
def world(monkeypatch, factory):
    world = LedgerWorld().install(monkeypatch)
    world.authorize.committed_probe = committed_probe(factory)
    return world


def _callback(token: str, *, decision: str = "ok", query: str = "q-1") -> dict:
    return {
        "callback_query_id": query,
        "from": OWNER,
        "data": f"{decision}:{approval_token(token)}",
        "chat_id": OWNER,
        "chat_type": "private",
        "message_id": "71",
    }


async def _press(factory, world, callback, port) -> dict:
    db = factory()
    try:
        return await telegram_api._handle_callback(
            callback=callback, port=port, owner_ids={OWNER}, db=db, settings=world.settings
        )
    finally:
        db.close()


def _card(factory, world):
    session = factory()
    try:
        row = world.propose_lead()
        proposal = ledger_owner_action(LeadStore(session), world.settings, row)
        session.commit()
        return row, proposal
    finally:
        session.close()


async def test_approve_callback_commits_the_cas_then_decides_on_the_ledger(factory, world):
    row, proposal = _card(factory, world)
    port = _CallbackPort()
    result = await _press(factory, world, _callback(proposal.approval_id), port)
    assert result["processed"] == 1
    assert world.ledger.named("decide") == [("decide", "TELEGRAM_APPROVAL", row.approval_id)]
    assert [(r["request_id"], r["status"]) for r in world.receipts()] == [
        (row.approval_id, "committed")
    ]
    assert "נקלט" in port.answer_texts[0]
    assert "Assaf OS" in port.edited[-1]


async def test_a_duplicate_callback_writes_once(factory, world):
    row, proposal = _card(factory, world)
    port = _CallbackPort()
    await _press(factory, world, _callback(proposal.approval_id, query="q-a"), port)
    await _press(factory, world, _callback(proposal.approval_id, query="q-b"), port)
    assert len(world.ledger.named("decide")) == 1
    assert [r["request_id"] for r in world.receipts()] == [row.approval_id]
    assert len(world.ledger.rows("os_outbox")) == 1
    assert "לא ביצעתי שוב" in port.edited[-1] or "כבר" in port.edited[-1]


async def test_reject_callback_decides_with_the_rejection_uuid(factory, world):
    row, proposal = _card(factory, world)
    port = _CallbackPort()
    await _press(factory, world, _callback(proposal.approval_id, decision="no"), port)
    assert world.ledger.named("decide") == [("decide", "TELEGRAM_APPROVAL", row.rejection_id)]
    assert [r["status"] for r in world.receipts()] == ["rejected"]


async def test_crash_between_cas_and_ledger_commit_recovers_with_no_new_id(factory, world):
    row, proposal = _card(factory, world)
    port = _CallbackPort()

    def crash() -> None:
        raise RuntimeError("worker died before the ledger commit")

    world.ledger.before_decide = crash
    first = await _press(factory, world, _callback(proposal.approval_id, query="q-1"), port)
    assert first["processed"] == 1
    assert world.receipts() == []
    with factory() as check:
        assert provider_claims(LeadStore(check)) == []  # not a sticky unknown
    # Telegram redelivers (or the owner taps again): the same stored UUID is used.
    await _press(factory, world, _callback(proposal.approval_id, query="q-2"), port)
    assert {call[2] for call in world.ledger.named("decide")} == {row.approval_id}
    assert [(r["request_id"], r["status"]) for r in world.receipts()] == [
        (row.approval_id, "committed")
    ]


async def test_a_claimed_crash_window_is_recovered_by_receipt_not_left_unknown(factory, world):
    row, proposal = _card(factory, world)
    with factory() as session:
        store = LeadStore(session)
        approval = store.get_approval_by_approval_id(proposal.approval_id)
        approval.decision, approval.approver = "approved", OWNER
        session.commit()
        # A legacy claim left by a crash must not freeze a ledger card.
        assert store.claim_provider_write(
            scope="approval", key=f"{proposal.proposal_id}:execute"
        )
        store.mark_provider_write_pending_review(
            scope="approval", key=f"{proposal.proposal_id}:execute"
        )
    world.ledger.decide(world.ctx("TELEGRAM_APPROVAL"), row.proposal_id, row.approval_id, "approve")
    decides = len(world.ledger.named("decide"))
    outcome = await telegram_api._execute_assaf_os_owner_decision(
        settings=world.settings, actor_id=OWNER, proposal_id=proposal.proposal_id,
        decision="approve",
    )
    assert outcome.status == "executed"
    assert len(world.ledger.named("decide")) == decides  # read, not re-decided
    assert len(world.receipts()) == 1
