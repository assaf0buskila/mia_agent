
"""A voice note and a typed message are the same owner turn.

The old test here asserted that the default echo node echoes — true whether or not
OwnerGraph carried a single real turn. What actually matters is that audio and text enter
the *same* graph, carrying the same thread, and that the answer comes back out of it. So
these drive `run_owner_turn` and the real Telegram owner entry point, and the graph is
spied on: if the turn ever stopped going through it, the spy would simply never fire.
"""

from __future__ import annotations

from typing import Any

from app.agents.owner.graph import compile_owner_graph
from app.api.owner import process_owner_texts
from app.capabilities.types import Principal
from app.channels.telegram import message_to_owner_state
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner import brain as owner_brain
from app.domain.owner.brain import OwnerBrainResult, run_owner_turn
from app.integrations.base import RecordingMessagePort

FALLBACK = "נרשם כמשימה. לא ביצעתי אותה."
OWNER_ID = "550042"


class _GraphSpy:
    """Wraps the real `compile_owner_graph`. Records every state in and every state out."""

    def __init__(self) -> None:
        self.real = compile_owner_graph
        self.states_in: list[dict[str, Any]] = []
        self.states_out: list[dict[str, Any]] = []

    def __call__(self, *, respond=None, retrieve=None):
        compiled = self.real(respond=respond, retrieve=retrieve)
        spy = self

        class _Wrapped:
            def invoke(self, state):
                spy.states_in.append(dict(state))
                final = compiled.invoke(state)
                spy.states_out.append(dict(final))
                return final

        return _Wrapped()


class _Producer:
    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    def __call__(self, state) -> OwnerBrainResult:
        self.states.append(dict(state))
        return OwnerBrainResult("תשובה אחת לשניהם", True, (), 1, 1)


# ------------------------------------------------------------------ graph shape


def test_the_owner_graph_has_the_same_nodes_for_both_sources() -> None:
    graph = compile_owner_graph()
    text_out = graph.invoke(
        message_to_owner_state(
            run_id="r1", owner_id="42", chat_id="42", text="תבדקי מיילים", source="text"
        )
    )
    voice_out = graph.invoke(
        message_to_owner_state(
            run_id="r2", owner_id="42", chat_id="42", text="תבדקי מיילים", source="audio"
        )
    )
    assert text_out["reply"] == voice_out["reply"]
    assert voice_out["source"] == "audio"
    assert text_out["thread_id"] == voice_out["thread_id"] == "tg:42"
    assert set(graph.nodes) >= {
        "load_owner_context",
        "retrieve_owner_knowledge",
        "respond",
    }


# --------------------------------------------------- both sources, one graph turn


def test_audio_and_text_reach_the_responder_through_the_same_graph() -> None:
    """The responder is handed state either way, and the source is the only difference."""
    replies = []
    produce = _Producer()
    for run_id, source in (("v_text", "text"), ("v_audio", "audio")):
        replies.append(
            run_owner_turn(
        principal=Principal.owner(source="test"),
                owner_id="42",
                telegram_chat_id="42",
                run_id=run_id,
                latest_message="תבדקי מיילים",
                kill_switch=False,
                produce=produce,
                fallback_text=FALLBACK,
                source=source,
            ).text
        )

    assert replies[0] == replies[1] == "תשובה אחת לשניהם"
    assert [state["source"] for state in produce.states] == ["text", "audio"]
    # Same thread, same latest message, same node path — only the source tag differs.
    assert {state["thread_id"] for state in produce.states} == {"tg:42"}
    assert {state["latest_message"] for state in produce.states} == {"תבדקי מיילים"}


# ------------------------------------------------------------ the real owner entry


async def _drive(item: dict[str, str], spy: _GraphSpy) -> RecordingMessagePort:
    init_db()
    session = get_session_factory()()
    port = RecordingMessagePort()
    try:
        await process_owner_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[item],
            store=LeadStore(session),
            port=port,
            kill_switch=False,
            owner_ids={item["from"]},
        )
        session.commit()
    finally:
        session.close()
    assert len(spy.states_in) == 1, "the owner turn did not go through OwnerGraph"
    return port


async def test_a_voice_note_enters_owner_graph_tagged_as_audio(monkeypatch) -> None:
    spy = _GraphSpy()
    monkeypatch.setattr(owner_brain, "compile_owner_graph", spy)
    port = await _drive(
        {
            "id": "evt.owner.voice.1",
            "from": OWNER_ID,
            "text": "מה קרה היום?",
            "source": "audio",
        },
        spy,
    )
    assert spy.states_in[0]["source"] == "audio"
    assert spy.states_in[0]["thread_id"] == f"tg:{OWNER_ID}"
    assert spy.states_in[0]["latest_message"] == "מה קרה היום?"
    # The reply Assaf gets is the one the graph returned.
    assert port.sent and port.sent[0].text == spy.states_out[0]["reply"]


async def test_a_typed_message_enters_the_same_graph_tagged_as_text(monkeypatch) -> None:
    spy = _GraphSpy()
    monkeypatch.setattr(owner_brain, "compile_owner_graph", spy)
    port = await _drive(
        {"id": "evt.owner.text.1", "from": OWNER_ID, "text": "מה קרה היום?"},
        spy,
    )
    assert spy.states_in[0]["source"] == "text"
    assert spy.states_in[0]["thread_id"] == f"tg:{OWNER_ID}"
    assert port.sent and port.sent[0].text == spy.states_out[0]["reply"]
