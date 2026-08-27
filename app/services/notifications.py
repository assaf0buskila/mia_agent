"""Owner Telegram notifications. ClientGraph never sends these itself."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from app.core.config import Settings
from app.domain.owner_lead_card import format_owner_lead_card

_TELEGRAM_API = "https://api.telegram.org"

_EXTRA_LABELS = (
    ("name", "שם"),
    ("contact", "יצירת קשר"),
    ("business", "עסק"),
    ("need", "צורך"),
    ("pain", "בעיה"),
    ("relevant_service", "שירות"),
    ("timeline", "לוח זמנים"),
    ("budget", "תקציב"),
    ("qualification", "כישור"),
    ("meeting_status", "פגישה"),
    ("recommended_next_step", "סיום"),
    ("conversation_id", "שיחה"),
)


def render_conversation_summary(summary: dict[str, str | None]) -> str:
    """Format a website-final card. Missing fields are omitted, never invented.

    Structured labeled lines, not a prose blob. Hebrew owner-facing copy.
    """
    extras: list[tuple[str, str]] = []
    for key, label in _EXTRA_LABELS:
        value = (summary.get(key) or "").strip()
        if value:
            extras.append((label, value))
    return format_owner_lead_card(
        title="שיחה מהאתר הסתיימה",
        lead_id=(summary.get("lead_id") or "").strip(),
        stage=(summary.get("stage") or "").strip(),
        last_said=(summary.get("last_message_short") or "").strip(),
        next_action=(summary.get("next_action") or "").strip(),
        whatsapp_offered=(summary.get("whatsapp_offered") or "") == "כן",
        extra_pairs=extras,
    )


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
                    "parse_mode": "HTML",
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
