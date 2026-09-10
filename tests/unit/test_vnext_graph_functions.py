from app.agents.owner.graph import compile_owner_graph
from app.agents.shared.state import empty_owner_state
from app.brain.store import BrainStore
from app.capabilities.types import GraphName, Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.domain.owner.brain import OwnerBrainResult, run_owner_turn


def test_owner_retrieve_uses_owner_policy(monkeypatch) -> None:
    names: list[tuple[str, GraphName]] = []

    def fake_execute(name, *, principal, args, handlers, kill_switch=False, preapproved=False):
        names.append((name, principal.graph))
        del args, handlers, kill_switch, preapproved
        return {"hits": []}

    monkeypatch.setattr("app.domain.owner.brain.execute_capability", fake_execute)
    init_db()
    db = get_session_factory()()
    try:
        result = run_owner_turn(
            principal=Principal.owner(source="test"),
            owner_id="111",
            telegram_chat_id="111",
            run_id="run_own_ret",
            latest_message="מה קרה במייל?",
            kill_switch=False,
            brain=BrainStore(db),
            settings=Settings(),
            produce=lambda state: OwnerBrainResult("pong", True, ("search_memory",), 1, 2),
        )
        assert result.text == "pong"
        assert ("memory.search", GraphName.OWNER) in names
        assert ("knowledge.search", GraphName.OWNER) in names
        assert "search_memory" in result.tools_used
        assert "knowledge.search" in result.tools_used
    finally:
        db.close()


def test_owner_graph_has_retrieve_node() -> None:
    graph = compile_owner_graph()
    out = graph.invoke(
        empty_owner_state(
            run_id="r",
            owner_id="1",
            telegram_chat_id="1",
            thread_id="tg:1",
            latest_message="שלום",
        )
    )
    assert out["reply"] == "שלום"
    assert "retrieve_owner_knowledge" in graph.nodes
