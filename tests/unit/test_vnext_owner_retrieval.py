"""One owner message costs exactly one retrieval pass.

The defect this originally pinned: `retrieve_owner_knowledge` ran `memory.search` +
`knowledge.search` (two embeddings, two rankings), wrote the hits into graph state — and
then `answer_owner` called `assemble_owner_context`, which ran the identical
`retrieve_memories` / `retrieve_knowledge` a second time. The graph's copy was discarded.
Every owner turn paid for retrieval twice, forever, and no test noticed because none of
them counted.

The v2 cleanup deleted the graph, so that *particular* double path cannot come back. What
these tests still defend is the cost contract itself: one owner turn, one memory pass, one
knowledge pass, two query embeddings — regardless of how many model steps the agent loop
takes. They assert **exactly** one, never `>= 1`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.schemas import (
    KnowledgeCategory,
    KnowledgeChunk,
    MemoryCategory,
    MemoryKind,
    MemorySource,
)
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.memory import ConversationTurn
from app.domain.owner.brain import answer_owner
from app.domain.owner.tasks import OwnerTaskType
from app.integrations.llm_client import LlmClient

from tests.unit.test_owner_untrusted_frame import untrusted_body

MEMORY_TEXT = "Assaf runs the zorblat pipeline every Friday morning"
KNOWLEDGE_TEXT = "The zorblat service is billed as a fixed monthly fee"
QUESTION = "what is the zorblat status"
FALLBACK = "נרשם כמשימה. לא ביצעתי אותה."


# ------------------------------------------------------------------------ harness


def _assistant_text(text: str) -> dict:
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }


def _assistant_tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }


class _Script(httpx.BaseTransport):
    """Replays one response per model step and records every request payload."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode("utf-8")))
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return httpx.Response(200, json=self._responses[index])


def _client(*bodies: str | dict) -> tuple[LlmClient, _Script]:
    script = _Script(
        [_assistant_text(body) if isinstance(body, str) else body for body in bodies]
    )
    return (
        LlmClient(
            api_key="k",
            model="m",
            client=httpx.Client(transport=script, base_url="https://api.openai.com"),
        ),
        script,
    )


def _settings():
    settings = get_settings()
    settings.owner_agent_model = "test-model"
    settings.openai_api_key = "test-key"
    settings.memory_enabled = True
    return settings


def _seeded_brain() -> tuple[Any, BrainStore]:
    """A brain holding one memory and one knowledge chunk, both about `zorblat`."""
    init_db()
    session = get_session_factory()()
    brain = BrainStore(session)
    seeding_port = FakeEmbeddingPort()
    # The unit suite shares one in-memory database, so seed only what is not there yet:
    # re-inserting the chunk would collide on its primary key.
    if any(record.text == MEMORY_TEXT for record in brain.list_memories(subject='owner')):
        return session, brain
    brain.save_memory(
        text=MEMORY_TEXT,
        kind=MemoryKind.WORKING,
        category=MemoryCategory.PROJECT,
        importance=9,
        source=MemorySource.TELEGRAM,
        embedding=seeding_port.embed([MEMORY_TEXT])[0],
        embedding_model=seeding_port.model,
    )
    brain.replace_knowledge_chunks(
        source_id="src_zorblat",
        chunks=[
            (
                KnowledgeChunk(
                    chunk_id="chunk_zorblat_1",
                    source_id="src_zorblat",
                    category=KnowledgeCategory.SERVICE,
                    title="Zorblat",
                    text=KNOWLEDGE_TEXT,
                    url="https://www.assafweb.com/zorblat",
                ),
                seeding_port.embed([KNOWLEDGE_TEXT])[0],
            )
        ],
        embedding_model=seeding_port.model,
    )
    session.commit()
    return session, brain


def _count_retrievals(monkeypatch) -> dict[str, int]:
    """Count every path into the two retrieval functions, wherever it is imported from.

    `assemble_owner_context` calls them through `app.brain.context`; the capability
    handlers behind the `search_memory` / `search_knowledge` agent tools hold their own
    module-level references. All of them are counted, so the total is the real number of
    retrieval passes this turn, no matter which entry point ran it.
    """
    from app.brain import context as context_module
    from app.capabilities import knowledge as knowledge_module
    from app.capabilities import memory as memory_module

    counts = {"memory": 0, "knowledge": 0}
    real_memory = context_module.retrieve_memories
    real_knowledge = context_module.retrieve_knowledge

    def counting_memory(*args, **kwargs):
        counts["memory"] += 1
        return real_memory(*args, **kwargs)

    def counting_knowledge(*args, **kwargs):
        counts["knowledge"] += 1
        return real_knowledge(*args, **kwargs)

    monkeypatch.setattr(context_module, "retrieve_memories", counting_memory)
    monkeypatch.setattr(context_module, "retrieve_knowledge", counting_knowledge)
    monkeypatch.setattr(memory_module, "retrieve_memories", counting_memory)
    monkeypatch.setattr(knowledge_module, "retrieve_knowledge", counting_knowledge)
    return counts


def _answer(brain, session, port, client, *, settings, message: str = QUESTION):
    return answer_owner(
        principal=Principal.owner(source="test"),
        store=LeadStore(session),
        brain=brain,
        settings=settings,
        task_type=OwnerTaskType.NOTE,
        owner_text=message,
        history=(),
        fallback_text=FALLBACK,
        kill_switch=False,
        demo_active=False,
        embedding_port=port,
        client=client,
    )


# --------------------------------------------------------------------------- tests


@pytest.mark.parametrize("message", [
    "Read my current calendar. Don't use history.",
    "תבדקי את היומן עכשיו אל תשתמשי בהיסטוריה",
])
def test_explicit_no_history_skips_retrieval_and_prior_context(monkeypatch, message):
    from app.domain.owner import brain as owner_brain

    def forbidden(*args, **kwargs):
        raise AssertionError("history retrieval must not run")

    monkeypatch.setattr(owner_brain, "assemble_owner_context", forbidden)
    session, brain = _seeded_brain()
    embedding = FakeEmbeddingPort()
    client, script = _client("Current calendar result")
    try:
        result = answer_owner(
            principal=Principal.owner(source="test"), store=LeadStore(session), brain=brain,
            settings=_settings(), task_type=OwnerTaskType.NOTE, owner_text=message,
            history=(ConversationTurn(role="mia", text="STALE_HISTORY_SENTINEL"),),
            fallback_text=FALLBACK, kill_switch=False, demo_active=False,
            embedding_port=embedding, client=client,
        )
    finally:
        session.close()
    assert result.used_agent
    assert embedding.calls == 0
    payload = json.dumps(script.requests)
    assert "STALE_HISTORY_SENTINEL" not in payload
    # The seeded memory is in the brain; nothing retrieved it, so it is not in the prompt.
    assert MEMORY_TEXT not in payload


def test_one_owner_turn_retrieves_exactly_once(monkeypatch) -> None:
    """Retrieval is priced per owner turn, not per agent step.

    HONEST NOTE ON WHAT THIS STILL PROVES. Before the v2 cleanup this counter spanned two
    independent retrieval paths (the graph's `retrieve` node and `answer_owner`'s own
    `assemble_owner_context`) and proved they did not both fire. The graph is gone, so a
    mechanical port of the old test would only re-assert `1 == 1`.

    What is re-derived here is the surviving half of the cost contract: the agent loop runs
    **two** model steps below (a tool call, then prose), and context assembly must still
    happen exactly once — before the loop, not once per step. The counter also spans the
    `search_memory` / `search_knowledge` capability handlers, so a retrieval re-introduced
    anywhere else in the turn is caught too. Two model steps with one retrieval pass is a
    real assertion; it fails the moment assembly moves inside the loop or is repeated.
    """
    session, brain = _seeded_brain()
    counts = _count_retrievals(monkeypatch)
    port = FakeEmbeddingPort()
    client, script = _client(
        _assistant_tool_call("call_1", "definitely_not_a_registered_tool", {"q": "zorblat"}),
        "הכל בסדר עם zorblat.",
    )
    try:
        result = _answer(brain, session, port, client, settings=_settings())
    finally:
        session.close()

    assert result.used_agent is True
    # Guard the guard: if the loop stopped taking two steps this test would silently
    # degrade back into the vacuous single-step version.
    assert len(script.requests) == 2, "the agent loop did not take two model steps"
    # The whole point. Exactly one, not "at least one", across two model steps.
    assert counts["memory"] == 1
    assert counts["knowledge"] == 1
    # One query embedding per retrieval kind — four before the original fix.
    assert port.calls == 2


def test_the_answer_is_grounded_in_what_was_retrieved(monkeypatch) -> None:
    """Retrieving once is only correct if that one copy is the one the model sees.

    This is the only remaining test proving that retrieved memory and knowledge text
    actually reaches the model. H5b split where each one lands: owner-authored memory
    still goes in the system prompt, while ingested website knowledge is third-party text
    and now travels in its own framed user message instead of sitting in the system role
    under "Everything above is what you know." The behaviour under test is unchanged --
    exactly one retrieved copy, and the model sees it -- so the assertion follows the
    knowledge to its new message rather than being dropped.
    """
    session, brain = _seeded_brain()
    _count_retrievals(monkeypatch)
    port = FakeEmbeddingPort()
    client, script = _client("הכל בסדר עם zorblat.")
    try:
        _answer(brain, session, port, client, settings=_settings())
    finally:
        session.close()

    messages = script.requests[0]["messages"]
    system = messages[0]["content"]
    assert MEMORY_TEXT in system
    assert KNOWLEDGE_TEXT not in system
    carriers = [m for m in messages if KNOWLEDGE_TEXT in str(m.get("content", ""))]
    assert len(carriers) == 1
    assert carriers[0]["role"] == "user"
    assert KNOWLEDGE_TEXT in untrusted_body(carriers[0]["content"])
