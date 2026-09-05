
"""One owner message costs exactly one retrieval pass.

The defect this pins: `retrieve_owner_knowledge` ran `memory.search` + `knowledge.search`
(two embeddings, two rankings), wrote the hits into graph state — and then `answer_owner`
called `assemble_owner_context`, which ran the identical `retrieve_memories` /
`retrieve_knowledge` a second time. The graph's copy was discarded. Every owner turn paid
for retrieval twice, forever, and no test noticed because none of them counted.

So these tests count, and they assert **exactly** one, never `>= 1`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
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
from app.domain.owner.brain import answer_owner, run_owner_turn
from app.domain.owner.tasks import OwnerTaskType
from app.integrations.llm_client import LlmClient

MEMORY_TEXT = "Assaf runs the zorblat pipeline every Friday morning"
KNOWLEDGE_TEXT = "The zorblat service is billed as a fixed monthly fee"
QUESTION = "what is the zorblat status"
FALLBACK = "נרשם כמשימה. לא ביצעתי אותה."


# ------------------------------------------------------------------------ harness


class _Script(httpx.BaseTransport):
    def __init__(self, body: str) -> None:
        self._body = body
        self.requests: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": self._body},
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            },
        )


def _client(body: str) -> tuple[LlmClient, _Script]:
    script = _Script(body)
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
    handlers hold their own module-level references. Both are counted, so the total is the
    real number of retrieval passes this turn — 2 each before the fix, 1 each after.
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


def _turn(brain, session, port, client, *, settings):
    return run_owner_turn(
        principal=Principal.owner(source="test"),
        owner_id="111",
        telegram_chat_id="111",
        run_id="run_retrieval",
        latest_message=QUESTION,
        kill_switch=False,
        fallback_text=FALLBACK,
        brain=brain,
        settings=settings,
        embedding_port=port,
        produce=lambda state: answer_owner(
        principal=Principal.owner(source="test"),
            store=LeadStore(session),
            brain=brain,
            settings=settings,
            task_type=OwnerTaskType.NOTE,
            owner_text=QUESTION,
            history=(),
            fallback_text=FALLBACK,
            kill_switch=False,
            demo_active=False,
            embedding_port=port,
            client=client,
            graph_state=state,
        ),
    )


# --------------------------------------------------------------------------- tests


def test_one_owner_turn_retrieves_exactly_once(monkeypatch) -> None:
    session, brain = _seeded_brain()
    counts = _count_retrievals(monkeypatch)
    port = FakeEmbeddingPort()
    client, script = _client("הכל בסדר עם zorblat.")
    try:
        result = _turn(brain, session, port, client, settings=_settings())
    finally:
        session.close()

    assert result.used_agent is True
    # The whole point. Exactly one, not "at least one".
    assert counts["memory"] == 1
    assert counts["knowledge"] == 1
    # One query embedding per retrieval kind — four before the fix.
    assert port.calls == 2


def test_the_answer_is_grounded_in_what_the_graph_retrieved(monkeypatch) -> None:
    """Retrieving once is only correct if the surviving copy is the one the model sees."""
    session, brain = _seeded_brain()
    _count_retrievals(monkeypatch)
    port = FakeEmbeddingPort()
    client, script = _client("הכל בסדר עם zorblat.")
    try:
        _turn(brain, session, port, client, settings=_settings())
    finally:
        session.close()

    system = script.requests[0]["messages"][0]["content"]
    assert MEMORY_TEXT in system
    assert KNOWLEDGE_TEXT in system


def test_the_graph_hits_are_what_reach_the_prompt(monkeypatch) -> None:
    """Poison the retrieve node's output: if it is really consumed, the prompt changes.

    This is the test that cannot pass while `answer_owner` assembles its own context —
    the planted line lives only in graph state, so a second retrieval can never find it.
    """
    from app.domain.owner import brain as owner_brain

    planted = "PLANTED-BY-THE-RETRIEVE-NODE"
    real_retrieve = owner_brain.retrieve_owner_context

    def planting_retrieve(state, **kwargs):
        update = real_retrieve(state, **kwargs)
        update["memory_hits"] = [{"id": "planted", "label": "working", "text": planted}]
        return update

    monkeypatch.setattr(owner_brain, "retrieve_owner_context", planting_retrieve)
    session, brain = _seeded_brain()
    port = FakeEmbeddingPort()
    client, script = _client("ok")
    try:
        _turn(brain, session, port, client, settings=_settings())
    finally:
        session.close()

    system = script.requests[0]["messages"][0]["content"]
    assert planted in system
    # The node replaced the memory hits, so the real memory is no longer in the prompt.
    assert MEMORY_TEXT not in system


def test_without_a_wired_brain_the_responder_still_retrieves_once(monkeypatch) -> None:
    """No graph retrieval available is not a licence to retrieve twice — or zero times."""
    session, brain = _seeded_brain()
    counts = _count_retrievals(monkeypatch)
    port = FakeEmbeddingPort()
    client, script = _client("ok")
    settings = _settings()
    try:
        run_owner_turn(
        principal=Principal.owner(source="test"),
            owner_id="111",
            telegram_chat_id="111",
            run_id="run_no_brain",
            latest_message=QUESTION,
            kill_switch=False,
            fallback_text=FALLBACK,
            produce=lambda state: answer_owner(
        principal=Principal.owner(source="test"),
                store=LeadStore(session),
                brain=brain,
                settings=settings,
                task_type=OwnerTaskType.NOTE,
                owner_text=QUESTION,
                history=(),
                fallback_text=FALLBACK,
                kill_switch=False,
                demo_active=False,
                embedding_port=port,
                client=client,
                graph_state=state,
            ),
        )
    finally:
        session.close()

    assert counts["memory"] == 1
    assert counts["knowledge"] == 1
