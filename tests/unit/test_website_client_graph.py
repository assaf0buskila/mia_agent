from app.agents.client.graph import compile_client_graph
from app.agents.owner.graph import compile_owner_graph


def test_owner_and_client_graphs_are_distinct() -> None:
    owner = compile_owner_graph()
    client = compile_client_graph()
    assert owner is not client
    assert set(owner.nodes) >= {
        "load_owner_context",
        "retrieve_owner_knowledge",
        "respond",
    }
    assert set(client.nodes) >= {
        "load_conversation",
        "retrieve_knowledge",
        "sales_turn",
        "complete_turn",
    }
    assert "sales_turn" not in owner.nodes
    assert "respond" not in client.nodes
    assert "retrieve_knowledge" not in owner.nodes
    assert "retrieve_owner_knowledge" not in client.nodes
