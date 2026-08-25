"""Serializable graph state. Owner and client never share a thread."""

from typing import Any, NotRequired, TypedDict


class OwnerState(TypedDict):
    run_id: str
    owner_id: str
    telegram_chat_id: str
    thread_id: str
    latest_message: str
    source: str
    kill_switch: bool
    reply: NotRequired[str]
    tools_used: NotRequired[list[str]]
    memory_hits: NotRequired[list[dict[str, str]]]
    knowledge_hits: NotRequired[list[dict[str, str]]]
    tokens_in: NotRequired[int]
    tokens_out: NotRequired[int]
    errors: list[str]


class ClientState(TypedDict):
    run_id: str
    conversation_id: str
    visitor_id: str
    lead_id: str
    latest_message: str
    channel: str
    kill_switch: bool
    turn_kind: NotRequired[str]
    inbound_id: NotRequired[str]
    page_path: NotRequired[str]
    page_section: NotRequired[str]
    # ADR-028: the booked meeting is the website's default exit, WhatsApp the fallback.
    meeting_first: NotRequired[bool]
    knowledge_hits: NotRequired[list[dict[str, str]]]
    tools_used: NotRequired[list[str]]
    next_action: NotRequired[str]
    reply: NotRequired[str]
    language: NotRequired[str]
    tokens_in: NotRequired[int]
    tokens_out: NotRequired[int]
    finalized: NotRequired[bool]
    errors: list[str]
    cost: dict[str, Any]


def empty_owner_state(
    *,
    run_id: str,
    owner_id: str,
    telegram_chat_id: str,
    thread_id: str,
    latest_message: str = "",
    source: str = "text",
    kill_switch: bool = False,
) -> OwnerState:
    return {
        "run_id": run_id,
        "owner_id": owner_id,
        "telegram_chat_id": telegram_chat_id,
        "thread_id": thread_id,
        "latest_message": latest_message,
        "source": source,
        "kill_switch": kill_switch,
        "tools_used": [],
        "memory_hits": [],
        "knowledge_hits": [],
        "errors": [],
    }


def empty_client_state(
    *,
    run_id: str,
    conversation_id: str,
    visitor_id: str,
    lead_id: str,
    latest_message: str = "",
    kill_switch: bool = False,
    page_path: str = "",
    page_section: str = "",
    channel: str = "website",
    turn_kind: str = "message",
    inbound_id: str = "",
    meeting_first: bool = False,
) -> ClientState:
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "visitor_id": visitor_id,
        "lead_id": lead_id,
        "latest_message": latest_message,
        "channel": channel,
        "kill_switch": kill_switch,
        "turn_kind": turn_kind,
        "inbound_id": inbound_id,
        "page_path": page_path,
        "page_section": page_section,
        "meeting_first": meeting_first,
        "knowledge_hits": [],
        "tools_used": [],
        "finalized": False,
        "errors": [],
        "cost": {},
    }
