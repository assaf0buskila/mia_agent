"""Shared inbound helpers. Owner and prospect processors both use these."""

from __future__ import annotations

from app.domain.events import Channel
from app.integrations.base import OutboundMessage
from app.integrations.telegram_format import owner_text, render_owner_markdown

_MAX_STT_DURATION_MS = 86_400_000


def clamp_ms_field(item: dict[str, str], key: str) -> int:
    raw = item.get(key, "0")
    if not raw:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    if value > _MAX_STT_DURATION_MS:
        return _MAX_STT_DURATION_MS
    return value


def stt_latency_ms(item: dict[str, str]) -> int:
    return clamp_ms_field(item, "stt_latency_ms")


def transcript_duration_ms(item: dict[str, str]) -> int:
    return clamp_ms_field(item, "duration_ms")


def event_conversation_id(item: dict[str, str]) -> str:
    return item.get("thread_id") or item.get("chat_id") or item["from"]


def outbound_reply(
    item: dict[str, str],
    *,
    text: str,
    channel: Channel,
    reply_markup: dict | None = None,
) -> OutboundMessage:
    if channel is Channel.TELEGRAM:
        return OutboundMessage(
            conversation_id=item.get("chat_id") or item["from"],
            text=render_owner_markdown(owner_text(text)),
            channel=channel.value,
            idempotency_key=item["id"],
            reply_to_id=item.get("message_id") or "",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    return OutboundMessage(
        conversation_id=item.get("chat_id") or item["from"],
        text=text,
        channel=channel.value,
        idempotency_key=item["id"],
        reply_to_id=item["id"],
    )
