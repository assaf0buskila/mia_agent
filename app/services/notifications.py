"""Owner Telegram notifications. ClientGraph never sends these itself."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from app.core.config import Settings

_TELEGRAM_API = "https://api.telegram.org"


def render_conversation_summary(summary: dict[str, str | None]) -> str:
    """Format a website-final card. Missing fields are omitted, never invented."""
    lines = ["New website conversation", ""]
    labels = (
        ("name", "Name"),
        # Extracted only when the visitor typed an address or number themselves. Without
        # it the owner reads a card about someone he has no way to reach.
        ("contact", "Contact"),
        ("business", "Business"),
        ("need", "What they need"),
        ("pain", "Main problem"),
        ("relevant_service", "Service they appear interested in"),
        ("timeline", "Timeline"),
        ("budget", "Budget"),
        ("qualification", "Qualification"),
        ("meeting_status", "Meeting"),
        ("recommended_next_step", "Recommended next step"),
        ("conversation_id", "Conversation ID"),
    )
    for key, label in labels:
        value = summary.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines).strip()


def send_owner_telegram(
    *,
    text: str,
    settings: Settings,
    transport: Callable[[str, str], None] | None = None,
) -> bool:
    token = settings.telegram_bot_token.strip()
    owner_ids = settings.telegram_owner_user_id_set()
    if not token or not owner_ids or not text.strip():
        return False
    chat_id = sorted(owner_ids)[0]
    if transport is not None:
        transport(chat_id, text)
        return True
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                f"{_TELEGRAM_API}/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "link_preview_options": {"is_disabled": True},
                },
            )
    except httpx.HTTPError:
        return False
    if response.status_code >= 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("ok") is True
