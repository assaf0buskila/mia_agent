"""Telegram is transport. OwnerGraph is where the owner turn actually happens.

The adapter tests below were always fine. What was missing is the one that costs
something: proof that the text Telegram sends came *out of the graph's state*. It is
written as a poison test — a node rewrites `reply` — because that is the only shape that
fails when the graph is bypassed. A plain "the reply is non-empty" assertion passes
whether OwnerGraph runs or is deleted.
"""

from __future__ import annotations

from typing import Any

from app.agents.owner.graph import compile_owner_graph
from app.api import telegram as telegram_api
from app.api.inbound import process_inbound_texts
from app.api.owner import process_owner_texts
from app.channels.telegram import message_to_owner_state
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain import owner_brain
from app.domain.events import Channel
from app.integrations.base import RecordingMessagePort

OWNER_ID = "550077"
MARK = "[through-the-graph] "


def test_telegram_adapter_builds_owner_state() -> None:
    state = message_to_owner_state(
        run_id="r",
        owner_id="42",
        chat_id="42",
        text="שלום",
        source="text",
    )
    assert state["thread_id"] == "tg:42"
    assert state["owner_id"] == "42"
    graph = compile_owner_graph()
    out = graph.invoke(state)
    assert out["reply"] == "שלום"


def test_owner_turn_uses_telegram_channel_adapter() -> None:
    assert owner_brain.message_to_owner_state is message_to_owner_state


def test_telegram_owner_entry_is_process_owner_texts() -> None:
    assert telegram_api.process_owner_texts is process_owner_texts
    assert process_owner_texts is not process_inbound_texts


class _MarkingGraph:
    """The real graph plus one node after `respond` that stamps `reply`."""

    def __init__(self) -> None:
        self.real = compile_owner_graph
        self.finals: list[dict[str, Any]] = []

    def __call__(self, *, respond=None, retrieve=None):
        compiled = self.real(respond=respond, retrieve=retrieve)
        owner = self

        class _Wrapped:
            def invoke(self, state):
                final = dict(compiled.invoke(state))
                final["reply"] = f"{MARK}{final.get('reply') or ''}"
                owner.finals.append(final)
                return final

        return _Wrapped()


async def test_the_text_telegram_sends_comes_out_of_the_graph_state(monkeypatch) -> None:
    """A node changes `reply`; Assaf must receive the changed text.

    Fails the moment the answer is taken from anywhere other than the graph's returned
    final state — a closure, a re-run of `produce`, anything.
    """
    graph = _MarkingGraph()
    monkeypatch.setattr(owner_brain, "compile_owner_graph", graph)
    init_db()
    session = get_session_factory()()
    port = RecordingMessagePort()
    try:
        result = await process_owner_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": "evt.owner.graphmark.1", "from": OWNER_ID, "text": "מה קרה היום?"}],
            store=LeadStore(session),
            port=port,
            kill_switch=False,
            owner_ids={OWNER_ID},
        )
        session.commit()
    finally:
        session.close()

    assert result["processed"] == 1
    assert len(graph.finals) == 1, "the owner turn never went through OwnerGraph"
    assert port.sent, "no reply was sent to Telegram"
    assert port.sent[0].text.startswith(MARK)
    assert port.sent[0].text == graph.finals[0]["reply"]
    assert result["reply"] == graph.finals[0]["reply"]


async def test_preclaimed_owner_event_requires_the_exact_received_webhook() -> None:
    """Caller-controlled item data cannot bypass the canonical webhook claim."""
    init_db()
    session = get_session_factory()()
    port = RecordingMessagePort()
    try:
        result = await process_owner_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": "evt.owner.unclaimed.1", "from": OWNER_ID, "text": "שלום"}],
            store=LeadStore(session),
            port=port,
            kill_switch=False,
            owner_ids={OWNER_ID},
            preclaimed_event_id="evt.owner.unclaimed.1",
            preclaimed_envelope_kind="audio",
        )
    finally:
        session.close()

    assert result["processed"] == 0
    assert result["duplicates"] == 1
    assert port.sent == []
