"""Website channel: HTTP transport only. Reasoning lives in ClientGraph."""

from app.agents.shared.state import ClientState, empty_client_state


def message_to_client_state(
    *,
    run_id: str,
    session_id: str,
    lead_id: str,
    text: str,
    kill_switch: bool = False,
    page_path: str = "",
    page_section: str = "",
    inbound_id: str = "",
    turn_kind: str = "message",
    meeting_first: bool = False,
) -> ClientState:
    return empty_client_state(
        run_id=run_id,
        conversation_id=session_id,
        visitor_id=session_id,
        lead_id=lead_id,
        latest_message=text,
        kill_switch=kill_switch,
        page_path=page_path,
        page_section=page_section,
        inbound_id=inbound_id,
        turn_kind=turn_kind,
        meeting_first=meeting_first,
    )
