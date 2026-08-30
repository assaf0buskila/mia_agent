from pathlib import Path

import pytest
from app.agents.client.graph import compile_client_graph
from app.agents.shared.state import empty_client_state
from app.api import inbound
from app.api.inbound import process_inbound_texts
from app.capabilities.types import Principal
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.graph.orchestrator import build_graph
from app.integrations.base import RecordingMessagePort


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
    graph = compile_client_graph(principal=Principal.client(source="test"))
    assert "sales_turn" in graph.nodes
    assert "retrieve_knowledge" in graph.nodes
    assert "complete_turn" in graph.nodes


@pytest.mark.asyncio
async def test_prospect_inbound_never_constructs_owner_only_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(_settings):
        raise AssertionError("prospect inbound must not construct an owner-only adapter")

    for name in (
        "build_calendar_agenda_port",
        "build_gmail_port",
        "build_instagram_insights_port",
        "build_linkedin_port",
        "build_search_console_port",
        "build_ga4_port",
        "build_seo_audit_port",
        "build_owner_reply_port",
    ):
        monkeypatch.setattr(inbound, name, forbidden)
    init_db()
    session = get_session_factory()()
    try:
        result = await process_inbound_texts(
            provider="instagram",
            channel=Channel.INSTAGRAM,
            items=[{"id": "prospect.no-owner-adapters", "from": "visitor", "text": ""}],
            store=LeadStore(session),
            port=RecordingMessagePort(),
            kill_switch=False,
            owner_ids={"123"},
        )
        assert result["processed"] == 1
    finally:
        session.close()
