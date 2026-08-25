from app.agents.owner.graph import compile_owner_graph
from app.api import telegram as telegram_api
from app.api.inbound import process_inbound_texts
from app.api.owner import process_owner_texts
from app.channels.telegram import message_to_owner_state


def test_telegram_adapter_builds_owner_state() -> None:
    state = message_to_owner_state(
        run_id="r",
        owner_id="42",
        chat_id="42",
        text="שלום",
        source="text",
    )
    assert state["thread_id"] == "tg:42"
    assert state["owner_id"] == "42"
    graph = compile_owner_graph()
    out = graph.invoke(state)
    assert out["reply"] == "שלום"


def test_telegram_owner_entry_is_process_owner_texts() -> None:
    assert telegram_api.process_owner_texts is process_owner_texts
    assert process_owner_texts is not process_inbound_texts
