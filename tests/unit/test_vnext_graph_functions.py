from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.agents.client.graph import compile_client_graph
from app.agents.owner.graph import compile_owner_graph
from app.agents.shared.state import empty_client_state, empty_owner_state
from app.brain.store import BrainStore
from app.capabilities.types import GraphName
from app.channels.website import message_to_client_state
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, build_message_in_event
from app.domain.owner_brain import OwnerBrainResult, run_owner_turn
from app.integrations.sales_reply import FakeSalesReplyPort
from app.services.finalization import kind_for


def test_website_end_and_due_scan_invoke_client_graph() -> None:
    website = Path("app/api/website.py").read_text(encoding="utf-8")
    due_scan = Path("app/workers/due_scan.py").read_text(encoding="utf-8")
    inbound = Path("app/api/inbound.py").read_text(encoding="utf-8")
    assert 'turn_kind="session_end"' in website
    assert "qualify_and_finalize" not in website
    assert "finalize_inactive_website_conversations" in due_scan
    assert "scan_inactive_website_conversations" not in due_scan
    assert "apply_hot_handoff" in inbound
    assert "apply_hot_handoff" not in website


def test_client_retrieve_uses_client_policy_and_reaches_sales(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_execute(
        name,
        *,
        graph,
        args,
        handlers,
        kill_switch=False,
        preapproved=False,
    ):
        captured["name"] = name
        captured["graph"] = graph
        captured["query"] = args.get("query")
        del handlers, kill_switch, preapproved
        return {
            "hits": [
                {
                    "id": "k1",
                    "label": "site",
                    "text": "AssafWeb builds AI growth operators.",
                }
            ]
        }

    monkeypatch.setattr("app.agents.client.graph.execute_capability", fake_execute)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_know01abcdef"
        )
        db.commit()
        fake = FakeSalesReplyPort()
        graph = compile_client_graph(store, reply_port=fake, settings=Settings())
        out = graph.invoke(
            message_to_client_state(
                run_id="run_know",
                session_id="web_know01abcdef",
                lead_id=lead_id,
                text="מה אתם עושים?",
            )
        )
        assert captured["name"] == "knowledge.search"
        assert captured["graph"] is GraphName.CLIENT
        assert captured["query"] == "מה אתם עושים?"
        assert out["tools_used"] == ["knowledge.search"]
        assert fake.calls[0]["knowledge_hits"][0]["id"] == "k1"
        assert out["next_action"] == "understand_workflow"
        assert out["finalized"] is False
    finally:
        db.close()


def test_session_end_skips_sales_and_empty_open_is_not_finalized() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_endempty01abc"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        db.commit()
        out = compile_client_graph(store, settings=Settings()).invoke(
            empty_client_state(
                run_id="end_empty",
                conversation_id=session_id,
                visitor_id=session_id,
                lead_id=lead_id,
                turn_kind="session_end",
            )
        )
        assert out.get("next_action") in (None, "")
        assert not out.get("reply")
        assert out["finalized"] is False
        assert not store.has_owner_notification(kind=kind_for(), lead_id=lead_id)
    finally:
        db.close()


def test_inactivity_via_client_graph_finalizes_once() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_graphinact01ab"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        old = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        store.save_canonical_event(
            provider="website",
            event=build_message_in_event(
                provider="website",
                channel=Channel.WEBSITE,
                provider_event_id="in.graph.old",
                conversation_id=session_id,
                text="שלום",
                actor_role="prospect",
                lead_id=lead_id,
                occurred_at=old,
            ),
        )
        db.commit()
        settings = Settings(website_inactivity_minutes=30)
        now = old + timedelta(minutes=31)
        out = compile_client_graph(store, settings=settings, now=now).invoke(
            empty_client_state(
                run_id="inact_one",
                conversation_id=session_id,
                visitor_id=session_id,
                lead_id=lead_id,
                turn_kind="inactivity",
            )
        )
        assert out["finalized"] is True
        db.commit()
        again = compile_client_graph(store, settings=settings, now=now).invoke(
            empty_client_state(
                run_id="inact_two",
                conversation_id=session_id,
                visitor_id=session_id,
                lead_id=lead_id,
                turn_kind="inactivity",
            )
        )
        assert again["finalized"] is False
        assert store.has_owner_notification(kind=kind_for(), lead_id=lead_id)
    finally:
        db.close()


def test_owner_retrieve_uses_owner_policy(monkeypatch) -> None:
    names: list[tuple[str, GraphName]] = []

    def fake_execute(name, *, graph, args, handlers, kill_switch=False, preapproved=False):
        names.append((name, graph))
        del args, handlers, kill_switch, preapproved
        return {"hits": []}

    monkeypatch.setattr("app.domain.owner_brain.execute_capability", fake_execute)
    init_db()
    db = get_session_factory()()
    try:
        brain = BrainStore(db)
        result = run_owner_turn(
            owner_id="111",
            telegram_chat_id="111",
            run_id="run_own_ret",
            latest_message="מה קרה במייל?",
            kill_switch=False,
            brain=brain,
            settings=Settings(),
            produce=lambda: OwnerBrainResult("pong", True, ("search_memory",), 1, 2),
        )
        assert result.text == "pong"
        assert ("memory.search", GraphName.OWNER) in names
        assert ("knowledge.search", GraphName.OWNER) in names
        assert "memory.search" in result.tools_used
        assert "knowledge.search" in result.tools_used
        assert "search_memory" in result.tools_used
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
