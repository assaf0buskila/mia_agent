"""OwnerGraph — Telegram. Never executes client sales NBA."""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from app.agents.shared.state import OwnerState

OwnerRespond = Callable[[OwnerState], dict]
OwnerRetrieve = Callable[[OwnerState], dict]


def compile_owner_graph(
    *,
    respond: OwnerRespond | None = None,
    retrieve: OwnerRetrieve | None = None,
):
    """Compile the owner LangGraph.

    `respond` and `retrieve` are injected so production can close over ports without
    putting SDK objects in graph state. Tests pass fakes or omit them.
    """

    def load_owner_context(state: OwnerState) -> dict:
        errors = list(state.get("errors", []))
        if not state.get("owner_id"):
            errors.append("missing owner_id")
        if not state.get("thread_id"):
            errors.append("missing thread_id")
        return {"errors": errors}

    def retrieve_owner_knowledge(state: OwnerState) -> dict:
        if retrieve is not None:
            return retrieve(state)
        return {
            "tools_used": list(state.get("tools_used") or []),
            "memory_hits": list(state.get("memory_hits") or []),
            "knowledge_hits": list(state.get("knowledge_hits") or []),
        }

    def respond_node(state: OwnerState) -> dict:
        if respond is not None:
            return respond(state)
        text = (state.get("latest_message") or "").strip()
        return {"reply": text}

    graph = StateGraph(OwnerState)
    graph.add_node("load_owner_context", load_owner_context)
    graph.add_node("retrieve_owner_knowledge", retrieve_owner_knowledge)
    graph.add_node("respond", respond_node)
    graph.add_edge(START, "load_owner_context")
    graph.add_edge("load_owner_context", "retrieve_owner_knowledge")
    graph.add_edge("retrieve_owner_knowledge", "respond")
    graph.add_edge("respond", END)
    return graph.compile()
