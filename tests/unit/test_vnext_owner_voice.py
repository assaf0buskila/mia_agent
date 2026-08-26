from app.agents.owner.graph import compile_owner_graph
from app.channels.telegram import message_to_owner_state


def test_owner_voice_enters_same_owner_graph() -> None:
    graph = compile_owner_graph()
    text_out = graph.invoke(
        message_to_owner_state(
            run_id="r1",
            owner_id="42",
            chat_id="42",
            text="תבדקי מיילים",
            source="text",
        )
    )
    voice_out = graph.invoke(
        message_to_owner_state(
            run_id="r2",
            owner_id="42",
            chat_id="42",
            text="תבדקי מיילים",
            source="audio",
        )
    )
    assert text_out["reply"] == voice_out["reply"]
    assert voice_out["source"] == "audio"
    assert set(graph.nodes) >= {
        "load_owner_context",
        "retrieve_owner_knowledge",
        "respond",
    }
