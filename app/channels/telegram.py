"""Telegram channel: webhook transport only. Reasoning lives in OwnerGraph."""

from app.agents.shared.state import OwnerState, empty_owner_state


def message_to_owner_state(
    *,
    run_id: str,
    owner_id: str,
    chat_id: str,
    text: str,
    source: str = "text",
    kill_switch: bool = False,
) -> OwnerState:
    return empty_owner_state(
        run_id=run_id,
        owner_id=owner_id,
        telegram_chat_id=chat_id,
        thread_id=f"tg:{owner_id}",
        latest_message=text,
        source=source,
        kill_switch=kill_switch,
    )
