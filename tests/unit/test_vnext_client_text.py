from app.agents.client.graph import compile_client_graph
from app.channels.website import message_to_client_state
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel


def test_website_text_reaches_client_graph() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_vnext"
        )
        db.commit()
        graph = compile_client_graph(store)
        out = graph.invoke(
            message_to_client_state(
                run_id="run_1",
                session_id="web_vnext",
                lead_id=lead_id,
                text="שלום",
            )
        )
        assert out["next_action"] == "understand_workflow"
        assert "יום רגיל בעסק" in out["reply"]
        assert out["conversation_id"] == "web_vnext"
    finally:
        db.close()
