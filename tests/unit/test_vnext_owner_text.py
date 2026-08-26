"""OwnerGraph owns the owner turn — or the turn fails honestly.

Every test here is written to fail if `run_owner_turn` stopped going through
`compile_owner_graph`. The previous versions of these tests could not: they injected the
`respond` lambda they then asserted, and `run_owner_turn` ended in `return produce()`, so
deleting OwnerGraph outright would have left them green while silently billing a second
model turn on every graph failure.

Three properties are pinned:

* the answer is read off the graph's **returned final state**, so a node that changes
  `reply` changes what Assaf gets;
* `produce` is handed that state, so retrieval done inside the graph can reach the answer;
* a graph that breaks degrades to the deterministic ack and never calls `produce` twice.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.agents.owner.graph import compile_owner_graph
from app.agents.shared.state import empty_owner_state
from app.brain.store import BrainStore
from app.capabilities.types import GraphName, Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.domain import owner_brain
from app.domain.owner_brain import (
    GRAPH_FAILURE_REASON,
    GRAPH_NO_RESULT_REASON,
    OwnerBrainResult,
    run_owner_turn,
)

FALLBACK = "נרשם כמשימה. לא ביצעתי אותה."


# ------------------------------------------------------------------ stub graphs
#
# Each stands in for `compile_owner_graph(...)` and models one way the graph can behave.
# They are the only way to prove the caller reads the graph's output instead of its own
# closure: a real graph faithfully echoes back whatever `respond` returned, so it cannot
# tell "read from state" apart from "read from a closure".


class _RewritingGraph:
    """Runs `respond`, then rewrites `reply` — as a post-respond node would."""

    def __init__(self, respond) -> None:
        self._respond = respond
        self.invocations = 0

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self.invocations += 1
        final = {**state, **self._respond(state)}
        final["reply"] = f"[graph]{final['reply']}"
        return final


class _ShortCircuitGraph:
    """Returns without ever reaching `respond` — a routing bug, or an early END."""

    def __init__(self, respond) -> None:
        del respond
        self.invocations = 0

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self.invocations += 1
        return dict(state)


class _ExplodingGraph:
    """`respond` runs, then the graph raises — a node failing after the model was paid."""

    def __init__(self, respond) -> None:
        self._respond = respond

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        self._respond(state)
        raise RuntimeError("node blew up")


def _stub_graph(monkeypatch, factory) -> list[Any]:
    built: list[Any] = []

    def fake_compile(*, respond=None, retrieve=None):
        del retrieve
        graph = factory(respond)
        built.append(graph)
        return graph

    monkeypatch.setattr(owner_brain, "compile_owner_graph", fake_compile)
    return built


class _Producer:
    """Stands in for `answer_owner`. Counts calls — a second call is a second paid turn."""

    def __init__(self, text: str = "pong") -> None:
        self.calls = 0
        self.states: list[dict[str, Any]] = []
        self._text = text

    def __call__(self, state) -> OwnerBrainResult:
        self.calls += 1
        self.states.append(dict(state))
        return OwnerBrainResult(self._text, True, ("search_memory",), 1, 2)


# ------------------------------------------------------------------- graph unit


def test_owner_graph_returns_respond_text() -> None:
    graph = compile_owner_graph(respond=lambda state: {"reply": f"got:{state['latest_message']}"})
    out = graph.invoke(
        empty_owner_state(
            run_id="run_1",
            owner_id="111",
            telegram_chat_id="111",
            thread_id="tg:111",
            latest_message="מה קרה היום?",
        )
    )
    assert out["reply"] == "got:מה קרה היום?"
    assert "owner_id" in out
    assert "lead_id" not in out


# ------------------------------------------- the answer comes out of graph state


def test_the_answer_is_read_from_the_returned_graph_state(monkeypatch) -> None:
    """A node that rewrites `reply` must change what Assaf gets.

    With the answer smuggled out through a `captured` closure this returns "pong" and the
    graph's own output is discarded — which is exactly what made OwnerGraph decorative.
    """
    built = _stub_graph(monkeypatch, _RewritingGraph)
    produce = _Producer()

    result = run_owner_turn(
        principal=Principal.owner(source="test"),
        owner_id="111",
        telegram_chat_id="111",
        run_id="run_state",
        latest_message="ping",
        kill_switch=False,
        produce=produce,
        fallback_text=FALLBACK,
    )

    assert built[0].invocations == 1
    assert result.text == "[graph]pong"
    assert result.used_agent is True
    assert result.tools_used == ("search_memory",)
    assert produce.calls == 1


def test_produce_receives_the_graph_state_it_must_answer_from(monkeypatch) -> None:
    """`produce` takes the state, so what the graph retrieved can reach the answer.

    By signature alone the old `Callable[[], OwnerBrainResult]` made this impossible: the
    retrieve node's work could not influence a single token of the reply.
    """
    hits = [{"id": "m1", "label": "working", "text": "Assaf ships Mia on Fridays"}]

    def fake_execute(
        name, *, principal, args, handlers, kill_switch=False, preapproved=False
    ):
        del handlers, kill_switch, preapproved
        assert principal.graph is GraphName.OWNER
        assert args["query"] == "מה קרה במייל?"
        return {"hits": hits if name == "memory.search" else []}

    monkeypatch.setattr(owner_brain, "execute_capability", fake_execute)
    init_db()
    db = get_session_factory()()
    produce = _Producer()
    try:
        run_owner_turn(
        principal=Principal.owner(source="test"),
            owner_id="111",
            telegram_chat_id="111",
            run_id="run_state_in",
            latest_message="מה קרה במייל?",
            kill_switch=False,
            produce=produce,
            fallback_text=FALLBACK,
            brain=BrainStore(db),
            settings=Settings(),
        )
    finally:
        db.close()

    assert produce.calls == 1
    seen = produce.states[0]
    assert seen["latest_message"] == "מה קרה במייל?"
    assert seen["thread_id"] == "tg:111"
    assert seen["source"] == "text"
    # The retrieve node ran, and what it found is in the state the responder answers from.
    assert seen["retrieval_done"] is True
    assert seen["memory_hits"] == hits
    assert "memory.search" in seen["tools_used"]


# --------------------------------------------------- a broken graph fails honestly


def test_a_graph_that_never_responds_does_not_run_a_second_produce(monkeypatch) -> None:
    """The `return produce()` fallback: a graph short-circuit bought a second paid turn.

    Nothing about the failure was visible — the reply looked completely normal — so the
    only symptom was the bill.
    """
    built = _stub_graph(monkeypatch, _ShortCircuitGraph)
    produce = _Producer()

    result = run_owner_turn(
        principal=Principal.owner(source="test"),
        owner_id="111",
        telegram_chat_id="111",
        run_id="run_short",
        latest_message="ping",
        kill_switch=False,
        produce=produce,
        fallback_text=FALLBACK,
    )

    assert built[0].invocations == 1
    assert produce.calls == 0
    assert result.text == FALLBACK
    assert result.used_agent is False
    assert result.fallback_reason == GRAPH_NO_RESULT_REASON


def test_a_graph_that_raises_degrades_to_the_deterministic_ack(monkeypatch) -> None:
    """A raising graph must reach Assaf as the canned ack, not as a traceback."""
    _stub_graph(monkeypatch, _ExplodingGraph)
    produce = _Producer()

    result = run_owner_turn(
        principal=Principal.owner(source="test"),
        owner_id="111",
        telegram_chat_id="111",
        run_id="run_boom",
        latest_message="ping",
        kill_switch=False,
        produce=produce,
        fallback_text=FALLBACK,
        source="audio",
    )

    # Exactly one — the graph got its turn, the failure did not buy another.
    assert produce.calls == 1
    assert result.text == FALLBACK
    assert result.used_agent is False
    assert result.tools_used == ()
    assert result.fallback_reason.startswith(GRAPH_FAILURE_REASON)
    assert "RuntimeError" in result.fallback_reason


def test_a_graph_failure_is_logged_not_swallowed(monkeypatch, caplog) -> None:
    """Silent degradation is the defect this whole module keeps re-learning."""
    _stub_graph(monkeypatch, _ExplodingGraph)
    with caplog.at_level("WARNING", logger="mia.agent"):
        run_owner_turn(
        principal=Principal.owner(source="test"),
            owner_id="111",
            telegram_chat_id="111",
            run_id="run_logged",
            latest_message="ping",
            kill_switch=False,
            produce=_Producer(),
            fallback_text=FALLBACK,
        )
    assert any("owner_graph_failed" in record.getMessage() for record in caplog.records)


# ----------------------------------------------------------- end-to-end, real graph


def test_the_real_graph_carries_the_turn_end_to_end() -> None:
    """No stubs: the compiled OwnerGraph is what produces the answer."""
    produce = _Producer("pong")
    result = run_owner_turn(
        principal=Principal.owner(source="test"),
        owner_id="111",
        telegram_chat_id="111",
        run_id="run_real",
        latest_message="ping",
        kill_switch=False,
        produce=produce,
        fallback_text=FALLBACK,
    )
    assert result.text == "pong"
    assert result.used_agent is True
    assert result.tools_used == ("search_memory",)
    assert produce.calls == 1
    # No brain/settings wired in, so the retrieve node cannot run and says so honestly.
    assert produce.states[0]["retrieval_done"] is False


@pytest.mark.parametrize("kill_switch", [True, False])
def test_the_kill_switch_reaches_the_responder_through_state(kill_switch: bool) -> None:
    produce = _Producer()
    run_owner_turn(
        principal=Principal.owner(source="test"),
        owner_id="111",
        telegram_chat_id="111",
        run_id="run_kill",
        latest_message="ping",
        kill_switch=kill_switch,
        produce=produce,
        fallback_text=FALLBACK,
    )
    assert produce.states[0]["kill_switch"] is kill_switch
