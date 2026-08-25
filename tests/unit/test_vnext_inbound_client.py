from pathlib import Path

from app.agents.client.graph import compile_client_graph
from app.agents.shared.state import empty_client_state
from app.api.inbound import process_inbound_texts
from app.graph.orchestrator import build_graph


def test_inbound_prospect_uses_client_graph_not_inner_orchestrator() -> None:
    source = Path("app/api/inbound.py").read_text(encoding="utf-8")
    assert "compile_client_graph" in source
    assert "from app.graph.orchestrator import build_graph" not in source
    assert "empty_state(" not in source
    assert process_inbound_texts is not build_graph


def test_client_state_can_carry_non_website_channel() -> None:
    state = empty_client_state(
        run_id="run_wa",
        conversation_id="972501111111",
        visitor_id="972501111111",
        lead_id="lead_wa",
        latest_message="שלום",
        channel="whatsapp",
    )
    assert state["channel"] == "whatsapp"
    graph = compile_client_graph()
    assert "sales_turn" in graph.nodes
