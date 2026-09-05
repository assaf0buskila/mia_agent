"""Deferred Telegram owner processing after webhook ack.

Telegram retries slow webhooks. Owner turns (voice STT + LangGraph + Composio) can run
for minutes and must not block the HTTP worker that also serves /health and the website
widget. The webhook claims the update, commits, returns 200, then runs here on a fresh
DB session.

Rapid owner texts debounce into one turn. A hung tool call must still produce a reply
so Telegram never stays on read with silence.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.exc import SQLAlchemyError

from app.api.inbound_common import event_conversation_id, outbound_reply
from app.core.config import get_settings
from app.core.errors import MiaError
from app.core.logging import log_comm
from app.db.session import get_session_factory
from app.db.store import LeadStore
from app.domain.events import (
    Channel,
    persist_tool_outcome,
    transcription_outcome,
)
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort
from app.integrations.gmail import build_gmail_port
from app.integrations.sheets import build_sheets_port
from app.integrations.transcribe import TranscriptionPort
from app.surfaces.crm import build_contacts_crm
from app.surfaces.owner import run_owner_loop
from app.surfaces.turn_coalesce import (
    COALESCE_WAIT_S,
    FAIL_REPLY,
    HANG_REPLY,
    OWNER_TURN_TIMEOUT_S,
    claim_burst,
    enqueue_turn,
    merge_claimed_items,
    take_if_still_pending,
)


def _inbound_from_work(work: dict[str, str]) -> dict[str, str]:
    return {
        "id": work["id"],
        "from": work["from"],
        "chat_id": work.get("chat_id") or work["from"],
        "message_id": work.get("message_id") or "",
        "text": work.get("text") or "",
        "source": work.get("source", ""),
        "file_name": work.get("file_name", ""),
        "stt_provider": work.get("stt_provider", ""),
        "stt_model": work.get("stt_model", ""),
        "language": work.get("language", ""),
        "duration_ms": work.get("duration_ms", "0"),
        "confidence": work.get("confidence", ""),
        "stt_latency_ms": work.get("stt_latency_ms", "0"),
    }


def _mark_claimed(store: LeadStore, claimed: list[dict[str, str]], status: str) -> None:
    for row in claimed:
        event_id = row.get("id") or ""
        if not event_id:
            continue
        try:
            store.mark_webhook(provider="telegram", provider_event_id=event_id, status=status)
        except KeyError:
            continue


async def _send_owner_notice(
    *,
    item: dict[str, str],
    port: MessagePort,
    text: str,
) -> bool:
    message = outbound_reply(item, text=text, channel=Channel.TELEGRAM)
    try:
        await port.send(message)
        return True
    except (RuntimeError, MiaError, AdapterHttpError):
        return False


async def process_telegram_owner_update(
    *,
    item: dict[str, str],
    envelope_kind: str,
    voice_file_id: str | None,
    port: MessagePort,
    transcribe_port: TranscriptionPort,
    photo_file_id: str | None = None,
) -> None:
    from app.api.telegram import (
        _send_transcription_failure_reply,
        _transcribe_telegram_voice,
    )

    del envelope_kind
    settings = get_settings()
    owner_ids = settings.telegram_owner_user_id_set()
    session = get_session_factory()()
    try:
        store = LeadStore(session)
        work = dict(item)
        if voice_file_id:
            work["file_id"] = voice_file_id
            work, voice_failure_stage, voice_latency_ms = await _transcribe_telegram_voice(
                item=work,
                media=port,
                transcribe_port=transcribe_port,
            )
            if voice_failure_stage:
                persist_tool_outcome(
                    store,
                    provider="telegram",
                    channel=Channel.TELEGRAM,
                    inbound_provider_event_id=work["id"],
                    conversation_id=event_conversation_id(work),
                    lead_id=None,
                    outcome=transcription_outcome(
                        transcribed=False,
                        latency_ms=voice_latency_ms,
                    ),
                )
                session.commit()
                log_comm(
                    channel=Channel.TELEGRAM.value,
                    provider="telegram",
                    actor_type="owner",
                    direction="in",
                    external_message_id=work["id"],
                    conversation_id=event_conversation_id(work),
                    policy_result=voice_failure_stage,
                    latency_ms=voice_latency_ms,
                    success=False,
                    automation_mode=settings.automation_mode.value,
                )
                sent = await _send_transcription_failure_reply(
                    item=work,
                    port=port,
                    kill_switch=False,
                    automation_mode=settings.automation_mode,
                )
                store.mark_webhook(
                    provider="telegram",
                    provider_event_id=work["id"],
                    status="sent" if sent else "failed",
                )
                session.commit()
                return
        if photo_file_id and not voice_file_id:
            work = await _see_telegram_photo(
                item=work,
                media=port,
                photo_file_id=photo_file_id,
            )
        inbound = _inbound_from_work(work)
        key = event_conversation_id(inbound)
        enqueue_turn(key, inbound)
        await asyncio.sleep(COALESCE_WAIT_S)
        claimed = claim_burst(key, inbound["id"])
        if claimed is None:
            await asyncio.sleep(OWNER_TURN_TIMEOUT_S)
            claimed = take_if_still_pending(key, inbound["id"])
            if claimed is None:
                return
        merged = merge_claimed_items(claimed)
        sheets = build_sheets_port(settings)
        crm = build_contacts_crm(settings, sheets)
        try:
            await asyncio.wait_for(
                run_owner_loop(
                    item=merged,
                    store=store,
                    port=port,
                    settings=settings,
                    crm=crm,
                    gmail_port=build_gmail_port(settings),
                    owner_ids=owner_ids,
                ),
                timeout=OWNER_TURN_TIMEOUT_S,
            )
        except TimeoutError:
            sent = await _send_owner_notice(item=merged, port=port, text=HANG_REPLY)
            _mark_claimed(store, claimed, "sent" if sent else "failed")
            session.commit()
            return
        except Exception:
            sent = await _send_owner_notice(item=merged, port=port, text=FAIL_REPLY)
            _mark_claimed(store, claimed, "sent" if sent else "failed")
            session.commit()
            log_comm(
                channel=Channel.TELEGRAM.value,
                provider="telegram",
                actor_type="owner",
                direction="in",
                external_message_id=merged.get("id", ""),
                conversation_id=event_conversation_id(merged),
                policy_result="owner_turn_failed",
                latency_ms=0,
                success=False,
                automation_mode=settings.automation_mode.value,
            )
            return
        extras = [row for row in claimed if row.get("id") and row["id"] != merged.get("id")]
        _mark_claimed(store, extras, "sent")
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        log_comm(
            channel=Channel.TELEGRAM.value,
            provider="telegram",
            actor_type="owner",
            direction="in",
            external_message_id=item.get("id", ""),
            conversation_id=event_conversation_id(item),
            policy_result="owner_turn_failed",
            latency_ms=0,
            success=False,
            automation_mode=settings.automation_mode.value,
        )
        try:
            notice_item = _inbound_from_work(item)
            await _send_owner_notice(item=notice_item, port=port, text=FAIL_REPLY)
        except Exception:
            pass
    finally:
        session.close()


_IMAGE_UNREAD = (
    "קיבלתי תמונה אבל לא הצלחתי לקרוא את הפיקסלים. תגיד מה לבדוק בה."
)


async def _see_telegram_photo(
    *,
    item: dict[str, str],
    media: object,
    photo_file_id: str,
) -> dict[str, str]:
    from app.integrations.telegram import TelegramMediaError

    download = getattr(media, "download_photo", None)
    caption = (item.get("text") or "").strip()
    if not callable(download):
        item["text"] = _join_image_text(caption, _IMAGE_UNREAD)
        return item
    try:
        payload, mime = await download(photo_file_id)
    except (RuntimeError, TelegramMediaError, TypeError, ValueError):
        item["text"] = _join_image_text(caption, _IMAGE_UNREAD)
        return item
    try:
        seen = _describe_owner_image(payload, mime)
    finally:
        del payload
    item["text"] = _join_image_text(caption, seen)
    return item


def _join_image_text(caption: str, seen: str) -> str:
    parts = [part for part in (seen.strip(), caption) if part]
    return "\n".join(parts) if parts else _IMAGE_UNREAD


def _describe_owner_image(payload: bytes, mime: str) -> str:
    from base64 import b64encode

    from app.core.config import get_settings
    from app.domain.owner.brain import build_agent_client

    settings = get_settings()
    client = build_agent_client(settings)
    if not client.enabled() or not payload:
        return _IMAGE_UNREAD
    encoded = b64encode(payload).decode("ascii")
    data_url = f"data:{mime};base64,{encoded}"
    try:
        response = client.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Assaf sent this image on Telegram. Describe what is visible "
                        "so Mia can act on it. Hebrew if the image has Hebrew. "
                        "Data only. No secrets."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is in this image?"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ]
        )
    except Exception:
        return _IMAGE_UNREAD
    text = (response.text or "").strip()
    return text[:1500] if text else _IMAGE_UNREAD
