from app.agents.owner.graph import compile_owner_graph
from app.agents.shared.state import empty_owner_state
from app.domain.owner_brain import OwnerBrainResult, run_owner_turn


def test_owner_graph_returns_respond_text() -> None:
    graph = compile_owner_graph(respond=lambda state: {"reply": f"got:{state['latest_message']}"})
    out = graph.invoke(
        empty_owner_state(
            run_id="run_1",
            owner_id="111",
            telegram_chat_id="111",
            thread_id="tg:111",
            latest_message="מה קרה היום?",
        )
    )
    assert out["reply"] == "got:מה קרה היום?"
    assert "owner_id" in out
    assert "lead_id" not in out


def test_run_owner_turn_uses_owner_graph() -> None:
    result = run_owner_turn(
        owner_id="111",
        telegram_chat_id="111",
        run_id="run_2",
        latest_message="ping",
        kill_switch=False,
        produce=lambda: OwnerBrainResult("pong", True, ("search_memory",), 1, 2),
    )
    assert result.text == "pong"
    assert result.used_agent is True
    assert result.tools_used == ("search_memory",)
