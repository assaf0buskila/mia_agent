"""Owner Telegram notifications. ClientGraph never sends these itself."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.core.config import Settings

_TELEGRAM_API = "https://api.telegram.org"


@dataclass(frozen=True)
class OwnerTelegramDelivery:
    """Outcome of a bounded owner fan-out.

    ``confirmed_failure`` is deliberately narrow.  A Telegram response that rejects
    every recipient is a known non-delivery and can be retried.  A transport error can
    happen after Telegram accepted a request, so it is ambiguous and must keep the
    workflow claim to avoid duplicate owner pings.
    """

    delivered: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()
    no_attempt: bool = False

    @property
    def confirmed_failure(self) -> bool:
        return bool(self.rejected) and not self.delivered and not self.ambiguous


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


def deliver_owner_telegram(
    *,
    text: str,
    settings: Settings,
    transport: Callable[[str, str], None] | None = None,
    parse_mode: str | None = None,
    recipient_ids: tuple[str, ...] | None = None,
) -> OwnerTelegramDelivery:
    """Send a notification to every numeric owner, isolating recipient failures."""
    token = settings.telegram_bot_token.strip()
    owner_ids = settings.telegram_owner_user_id_set()
    requested_ids = owner_ids if recipient_ids is None else set(recipient_ids) & owner_ids
    recipients = tuple(sorted(requested_ids))
    if not token or not recipients or not text.strip():
        return OwnerTelegramDelivery(no_attempt=True)
    if transport is not None:
        delivered: list[str] = []
        ambiguous: list[str] = []
        for chat_id in recipients:
            try:
                transport(chat_id, text)
            except Exception:  # noqa: BLE001 - a callback cannot block another owner
                ambiguous.append(chat_id)
                continue
            delivered.append(chat_id)
        return OwnerTelegramDelivery(tuple(delivered), ambiguous=tuple(ambiguous))

    delivered: list[str] = []
    rejected: list[str] = []
    ambiguous: list[str] = []
    with httpx.Client(timeout=10.0) as client:
        for chat_id in recipients:
            try:
                response = client.post(
                    f"{_TELEGRAM_API}/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "link_preview_options": {"is_disabled": True},
                        **({"parse_mode": parse_mode} if parse_mode else {}),
                    },
                )
            except httpx.HTTPError:
                ambiguous.append(chat_id)
                continue
            status = _telegram_delivery_status(response)
            if status is True:
                delivered.append(chat_id)
            elif status is False:
                rejected.append(chat_id)
            elif status is None:
                ambiguous.append(chat_id)
    return OwnerTelegramDelivery(
        tuple(delivered), rejected=tuple(rejected), ambiguous=tuple(ambiguous)
    )


def send_owner_telegram(
    *,
    text: str,
    settings: Settings,
    transport: Callable[[str, str], None] | None = None,
) -> bool:
    """Compatibility bool wrapper for callers that do not own a workflow claim."""
    return bool(deliver_owner_telegram(text=text, settings=settings, transport=transport).delivered)


def _telegram_delivery_status(response: httpx.Response) -> bool | None:
    """Return accepted/rejected/ambiguous without guessing after a malformed response."""
    if response.status_code >= 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
        return None
    return body["ok"]
