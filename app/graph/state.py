from typing import Any, NotRequired, TypedDict


class GraphState(TypedDict):
    """Serializable LangGraph state only. No SDK clients, secrets, or provider objects."""

    run_id: str
    thread_id: str
    actor_role: str
    channel: str
    conversation_id: str
    lead_id: NotRequired[str]
    latest_message: str
    language: str
    risk_level: str
    approval_required: bool
    kill_switch: bool
    next_action: NotRequired[str]
    reply: NotRequired[str]
    tokens_in: NotRequired[int]
    tokens_out: NotRequired[int]
    page_path: NotRequired[str]
    page_section: NotRequired[str]
    errors: list[str]
    cost: dict[str, Any]


def empty_state(
    *,
    run_id: str,
    thread_id: str,
    channel: str,
    lead_id: str | None = None,
    latest_message: str = "",
    kill_switch: bool = False,
    page_path: str = "",
    page_section: str = "",
) -> GraphState:
    state: GraphState = {
        "run_id": run_id,
        "thread_id": thread_id,
        "actor_role": "prospect",
        "channel": channel,
        "conversation_id": thread_id,
        "latest_message": latest_message,
        "language": "und",
        "risk_level": "low",
        "approval_required": False,
        "kill_switch": kill_switch,
        "errors": [],
        "cost": {},
        "page_path": page_path,
        "page_section": page_section,
    }
    if lead_id:
        state["lead_id"] = lead_id
    return state
