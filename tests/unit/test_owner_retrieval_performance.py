"""Synthetic owner retrieval: call counts, fallback, budgets, and turn isolation."""

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from app.brain.context import assemble_owner_context
from app.brain.embeddings import DisabledEmbeddingPort, EmbeddingError, FakeEmbeddingPort
from app.brain.schemas import MemoryCategory, MemoryKind, MemorySource
from app.brain.vectors import encode_vector

NOW = datetime(2026, 10, 23, tzinfo=UTC)
QUERY = "automation projects"


class SyntheticBrain:
    """No DB/provider access. Include both semantic and keyword ranking candidates."""

    def __init__(self, *, memories=True, knowledge=True, vectors=True, profile=False):
        self.memories = [
            SimpleNamespace(
                memory_id="m1", text="automation projects in progress", importance=5,
                last_used_at="", created_at=NOW.isoformat(), kind=MemoryKind.SEMANTIC,
                category=MemoryCategory.IDENTITY if profile else MemoryCategory.PROJECT,
                source_ref="synthetic", source=MemorySource.TELEGRAM,
            )
        ] if memories else []
        self.chunks = [
            SimpleNamespace(
                chunk_id="k1", text="automation services for business", title="services",
                url="https://example.invalid", source_id="synthetic",
                category=SimpleNamespace(value="services"),
            )
        ] if knowledge else []
        self.vector = encode_vector(FakeEmbeddingPort().embed([QUERY])[0]) if vectors else ""
        self.touched = []

    def list_memories(self, *, categories=(), **kwargs):
        return [m for m in self.memories if not categories or m.category in categories]

    def list_knowledge_chunks(self):
        return self.chunks

    def memory_vectors(self, **kwargs):
        return [(m.memory_id, self.vector) for m in self.memories] if self.vector else []

    def knowledge_vectors(self):
        return [(k.chunk_id, self.vector) for k in self.chunks] if self.vector else []

    def list_open_gaps(self, **kwargs):
        return []

    def touch_memories(self, ids):
        self.touched.extend(ids)


def test_one_embedding_for_both_paths_and_new_call_on_next_turn():
    brain, port = SyntheticBrain(), FakeEmbeddingPort()
    first = assemble_owner_context(brain, query=QUERY, embedding_port=port, now=NOW)
    assert first.memories and first.knowledge
    assert first.memories[0].similarity > 0
    assert first.knowledge[0].similarity > 0
    assert port.calls == 1
    second = assemble_owner_context(brain, query=QUERY, embedding_port=port, now=NOW)
    assert second == first
    assert port.calls == 2


def test_failed_embedding_is_cached_and_keyword_fallback_matches_disabled():
    class Failing(FakeEmbeddingPort):
        def embed(self, texts):
            self.calls += 1
            raise EmbeddingError("synthetic failure")

    brain, port = SyntheticBrain(), Failing()
    result = assemble_owner_context(brain, query=QUERY, embedding_port=port, now=NOW)
    expected = assemble_owner_context(
        brain, query=QUERY, embedding_port=DisabledEmbeddingPort(), now=NOW
    )
    assert result.memories == expected.memories
    assert result.knowledge == expected.knowledge
    assert result.memories and result.knowledge
    assert port.calls == 1


@pytest.mark.parametrize("options", [
    {"memories": False, "knowledge": False},
    {"vectors": False},
    {"profile": True, "knowledge": False},
])
def test_no_eligible_vectors_does_not_embed(options):
    port = FakeEmbeddingPort()
    assemble_owner_context(SyntheticBrain(**options), query=QUERY, embedding_port=port)
    assert port.calls == 0


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_does_not_embed(query):
    port = FakeEmbeddingPort()
    assemble_owner_context(SyntheticBrain(), query=query, embedding_port=port)
    assert port.calls == 0


def test_zero_retrieval_limits_do_not_embed():
    port = FakeEmbeddingPort()
    assemble_owner_context(
        SyntheticBrain(), query=QUERY, embedding_port=port, memory_limit=0, knowledge_limit=0
    )
    assert port.calls == 0


def test_no_context_budget_does_not_embed():
    port = FakeEmbeddingPort()
    result = assemble_owner_context(SyntheticBrain(), query=QUERY, embedding_port=port, max_chars=0)
    assert result.used_chars == 0
    assert port.calls == 0


@pytest.mark.parametrize("remaining, expected", [(50, 30), (20, 9), (11.05, None), (-1, None)])
def test_embedding_timeout_preserves_synthesis_and_send(monkeypatch, remaining, expected):
    from app.brain.embeddings import OpenAIEmbeddingPort

    monkeypatch.setattr("app.core.deadlines.monotonic", lambda: 100.0)
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        port = OpenAIEmbeddingPort(api_key="synthetic", model="embedding", dim=2, client=http)
        result = assemble_owner_context(
            SyntheticBrain(), query=QUERY, embedding_port=port, now=NOW,
            deadline_at=100 + remaining, embedding_reserve_seconds=11,
        )
    assert result.memories and result.knowledge
    if expected is None:
        assert timeouts == []
    else:
        assert len(timeouts) == 1
        assert all(value == expected for value in timeouts[0].values())


def test_provider_timeout_falls_back_once(monkeypatch):
    from app.brain.embeddings import OpenAIEmbeddingPort

    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("synthetic", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        port = OpenAIEmbeddingPort(api_key="synthetic", model="embedding", dim=2, client=http)
        result = assemble_owner_context(SyntheticBrain(), query=QUERY, embedding_port=port, now=NOW)
    assert len(attempts) == 1
    assert result.memories and result.knowledge
    assert all(i.similarity == 0 for i in (*result.memories, *result.knowledge))


@pytest.mark.parametrize("message, expected_calls", [(QUERY, 1), ("answer without history", 0)])
def test_owner_wires_existing_deadline_and_reserves(monkeypatch, message, expected_calls):
    from app.brain.embeddings import OpenAIEmbeddingPort
    from app.capabilities.types import Principal
    from app.core.config import Settings
    from app.domain.owner import brain
    from app.domain.owner.tasks import OwnerTaskType
    from app.graph.owner_agent import AgentOutcome

    monkeypatch.setattr("app.core.deadlines.monotonic", lambda: 100.)
    timeouts = []
    contexts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1., 0.]}]})

    def loop(**kwargs):
        contexts.append(kwargs["context"])
        assert kwargs["deadline_at"] == 120.
        return AgentOutcome("ok", (), 0, 0, (), True)

    monkeypatch.setattr(brain, "run_owner_agent", loop)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = brain.answer_owner(
            principal=Principal.owner(source="test"), store=object(), brain=SyntheticBrain(),
            settings=Settings(_env_file=None, memory_enabled=True,
                owner_final_reserve_seconds=4., owner_min_model_seconds=8.),
            task_type=OwnerTaskType.NOTE, owner_text=message, history=(), fallback_text="fallback",
            kill_switch=False, demo_active=False, deadline_at=120.,
            embedding_port=OpenAIEmbeddingPort(api_key="synthetic", model="m", dim=2, client=http),
            client=SimpleNamespace(enabled=lambda: True),
        )
    assert result.used_agent
    assert timeouts == [8.] * expected_calls
    assert bool(contexts[0]) == bool(expected_calls)


def test_legacy_port_is_not_called_unbounded_inside_timed_turn():
    port = FakeEmbeddingPort()
    result = assemble_owner_context(
        SyntheticBrain(), query=QUERY, embedding_port=port, deadline_at=float("inf")
    )
    assert port.calls == 0
    assert result.memories and result.knowledge
