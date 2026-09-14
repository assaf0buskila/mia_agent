"""Shared inbound helpers. Owner and prospect processors both use these."""

from __future__ import annotations

from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_CALENDAR_CREATE,
    ACTION_CALENDAR_RESCHEDULE,
    ACTION_GMAIL_SEND,
)
from app.domain.events import Channel
from app.domain.owner.callbacks import approval_token
from app.domain.owner.tasks import OwnerTaskType
from app.integrations.base import OutboundMessage
from app.integrations.telegram_format import approval_keyboard, render_owner_markdown

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
            text=render_owner_markdown(text),
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


def owner_telegram_reply_markup(
    store: LeadStore,
    *,
    channel: Channel,
    task_type: OwnerTaskType,
    turn_approval_id: str = "",
    turn_approval_ids: tuple[str, ...] = (),
) -> dict | None:
    """Buttons for every exact proposal created by this turn or an explicit pending read."""
    if channel is not Channel.TELEGRAM:
        return None
    exact_ids = tuple(
        dict.fromkeys(
            value.strip()
            for value in (*turn_approval_ids, turn_approval_id)
            if value.strip()
        )
    )
    if exact_ids:
        keyboard_rows: list[list[dict]] = []
        for approval_id in exact_ids:
            keyboard_rows.extend(approval_keyboard(approval_token(approval_id))["inline_keyboard"])
        return {"inline_keyboard": keyboard_rows}
    rows = store.list_all_pending_approvals()
    if task_type is OwnerTaskType.GMAIL_DRAFT:
        rows = [row for row in rows if row.action == ACTION_GMAIL_SEND]
    elif task_type is OwnerTaskType.CALENDAR_WRITE:
        rows = [
            row
            for row in rows
            if row.action in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE)
        ]
    elif task_type is not OwnerTaskType.PENDING_APPROVALS:
        return None
    if not rows:
        return None
    approval_id = (rows[0].approval_id or "").strip()
    if not approval_id:
        return None
    return approval_keyboard(approval_token(approval_id))
