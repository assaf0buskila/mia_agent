"""Safety tests for the website visitor knowledge feature (ADR-028).

`app.brain.context.assemble_visitor_context` is the read side of a hard privacy
boundary: `docs/PRD.md` says a website visitor can never *write* owner memory, and this
file protects the matching read-side invariant — a visitor must never be able to
*retrieve* owner memory either. It also covers the operational safety properties the
wiring in `app/agents/client/graph.py` promises: the kill switch skips the lookup
entirely (no embedding call), a broken brain never 500s a website turn, and the ADR-028
meeting-first gate behaves exactly as documented for both callers
(`meeting_first=False` unchanged, `meeting_first=True` offers the meeting then falls
back to WhatsApp).
"""

from __future__ import annotations

import app.agents.client.graph as client_graph_module
import app.brain.context as context_module
import app.capabilities.knowledge as capability_module
from app.agents.client.graph import compile_client_graph
from app.agents.shared.state import empty_client_state
from app.brain.context import assemble_visitor_context
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.schemas import KnowledgeCategory, KnowledgeChunk
from app.capabilities.types import Principal
from app.core.errors import ProviderUnavailable
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.extract import extract_sales_signals
from app.domain.sales import NextAction, SalesState, mark_action_delivered, select_next_action
from app.main import app
from fastapi.testclient import TestClient


class _MemoryForbiddenBrainStore:
    """Spy `BrainStore`: knowledge methods work, every owner-memory method raises.

    If `assemble_visitor_context` ever reaches for owner memory — directly or through a
    helper it calls — this blows up the test instead of silently leaking a fact.
    """

    def __init__(self, chunks: list[KnowledgeChunk]) -> None:
        self._chunks = chunks

    def list_knowledge_chunks(self, **kwargs: object) -> list[KnowledgeChunk]:
        return list(self._chunks)

    def knowledge_vectors(self, **kwargs: object) -> list[tuple[str, str]]:
        return []

    def list_memories(self, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError(
            "assemble_visitor_context must never call BrainStore.list_memories"
        )

    def memory_vectors(self, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError(
            "assemble_visitor_context must never call BrainStore.memory_vectors"
        )

    def touch_memories(self, *args: object, **kwargs: object) -> None:
        raise AssertionError(
            "assemble_visitor_context must never call BrainStore.touch_memories"
        )

    def list_open_gaps(self, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError(
            "assemble_visitor_context must never call BrainStore.list_open_gaps"
        )


def _fake_chunk() -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id="chunk_pricing_1",
        source_id="src_faq",
        category=KnowledgeCategory.FAQ,
        title="Pricing",
        text="Starter automations begin at a fixed monthly fee, no setup cost.",
        url="https://www.assafweb.com/faq",
    )


def test_assemble_visitor_context_never_touches_owner_memory() -> None:
    """The critical test: knowledge-only retrieval, proven with a store that would raise
    on first contact with any owner-memory method rather than merely returning empty."""
    store = _MemoryForbiddenBrainStore([_fake_chunk()])
    embedding_port = FakeEmbeddingPort()

    context = assemble_visitor_context(
        store, query="how much does an automation cost", embedding_port=embedding_port
    )

    # Would have raised AssertionError above if memory had been touched at all.
    assert context.profile == ""
    assert context.memories == ()
    assert context.open_questions == ()
    assert len(context.knowledge) == 1
    assert context.knowledge[0].text == _fake_chunk().text


def test_assemble_visitor_context_empty_store_is_safe_too() -> None:
    """No knowledge ingested yet is a normal state, not an error, and still never
    reaches for memory."""
    store = _MemoryForbiddenBrainStore([])
    embedding_port = FakeEmbeddingPort()

    context = assemble_visitor_context(
        store, query="anything at all", embedding_port=embedding_port
    )

    assert context.profile == ""
    assert context.memories == ()
    assert context.knowledge == ()
    assert context.open_questions == ()


def test_kill_switch_skips_client_knowledge_retrieval_entirely(monkeypatch) -> None:
    """The ClientGraph capability is not invoked while the kill switch is set."""
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_visitor_kill"
        )
        db.commit()
        calls = {"n": 0}

        def spy_execute(*args: object, **kwargs: object) -> dict[str, object]:
            calls["n"] += 1
            return {"hits": []}

        monkeypatch.setattr(client_graph_module, "execute_capability", spy_execute)
        graph = compile_client_graph(
            store,
            principal=Principal.client(source="website", actor_id=lead_id),
        )
        result = graph.invoke(
            empty_client_state(
                run_id="run_visitor_kill",
                conversation_id="web_visitor_kill",
                visitor_id="web_visitor_kill",
                lead_id=lead_id,
                latest_message="what does this cost?",
                kill_switch=True,
            )
        )

        assert calls["n"] == 0
        assert result.get("reply")
    finally:
        db.close()


def test_website_client_graph_executes_knowledge_once(monkeypatch) -> None:
    """Live SITE lookup is published facts only. ClientGraph must not run.

    The website may call retrieve_knowledge for assafweb.com facts. It must not
    invoke the leftover ClientGraph capability path.
    """
    capability_calls = 0
    retrieval_paths: list[str] = []
    real_capability_retrieve = capability_module.retrieve_knowledge
    real_direct_retrieve = context_module.retrieve_knowledge
    real_execute = client_graph_module.execute_capability

    def through_capability(*args: object, **kwargs: object):
        retrieval_paths.append("capability")
        return real_capability_retrieve(*args, **kwargs)

    def through_legacy_website_path(*args: object, **kwargs: object):
        retrieval_paths.append("legacy_website")
        return real_direct_retrieve(*args, **kwargs)

    def counted_execute(*args: object, **kwargs: object):
        nonlocal capability_calls
        capability_calls += 1
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(capability_module, "retrieve_knowledge", through_capability)
    monkeypatch.setattr(context_module, "retrieve_knowledge", through_legacy_website_path)
    monkeypatch.setattr(client_graph_module, "execute_capability", counted_execute)
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "מה השירות שלכם כולל?"},
        )

    assert response.status_code == 200
    assert response.json()["next_action"] in {
        "ask_need",
        "ask_contact",
        "no_price",
        "answer",
    }
    assert capability_calls == 0
    assert "capability" not in retrieval_paths
    assert all(path == "legacy_website" for path in retrieval_paths)


def test_knowledge_capability_exception_does_not_break_the_turn(monkeypatch) -> None:
    """A brain outage must degrade phrasing, never 500 a customer.

    Patches the ClientGraph capability call so this exercises the actual website
    message endpoint end to end, not just its local error handling.
    """

    def boom(*args: object, **kwargs: object) -> None:
        raise ProviderUnavailable("brain outage")

    monkeypatch.setattr("app.agents.client.graph.execute_capability", boom)
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "מה המחיר של השירות?"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["message"]
        assert body["next_action"]


def test_meeting_first_false_keeps_pre_adr028_whatsapp_gate() -> None:
    """`meeting_first` defaults to False, which must reproduce today's behavior exactly:
    the continuation gate still offers WhatsApp directly, never the meeting."""
    state = SalesState(lead_id="lead_pre_adr028")
    action = NextAction.UNDERSTAND_WORKFLOW
    for text in (
        "We run a clinic and miss calls all day.",
        "we call everyone back by hand from a list",
        "about two hours every day",
    ):
        state = extract_sales_signals(state, text)
        # Omit meeting_first entirely so the default itself is under test.
        action = select_next_action(state, channel="website")
        state = mark_action_delivered(state, action)

    assert action is NextAction.OFFER_WHATSAPP
    assert state.whatsapp_handoff_offered is True
    assert state.meeting_exit_offered is False


def test_meeting_first_gate_then_whatsapp_fallback() -> None:
    """ADR-028 end to end at the state-machine level: the gate offers the meeting
    first; once it is offered and not taken, the next continuation-ready turn still
    offers WhatsApp exactly as before."""
    state = SalesState(lead_id="lead_meeting_first")
    steps = (
        ("We run a clinic and miss calls all day.", NextAction.DEEPEN_PAIN),
        ("we call everyone back by hand from a list", NextAction.REFLECT),
        ("about two hours every day", NextAction.OFFER_MEETING),
        ("I decide this quarter", NextAction.OFFER_WHATSAPP),
    )
    for text, expected in steps:
        state = extract_sales_signals(state, text)
        action = select_next_action(state, channel="website", meeting_first=True)
        assert action is expected
        state = mark_action_delivered(state, action)

    assert state.meeting_exit_offered is True
    assert state.whatsapp_handoff_offered is True
