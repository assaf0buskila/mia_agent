"""Telegram private owner webhook. Allowlist numeric user ids before Mia."""

from __future__ import annotations

import json
from time import perf_counter

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_telegram_port, get_transcription_port
from app.api.inbound import process_inbound_texts
from app.core.config import get_settings
from app.core.webhooks import verify_telegram_secret
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms
from app.domain.events import Channel
from app.domain.owner_callbacks import resolve_owner_callback
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort
from app.integrations.telegram import (
    TelegramMediaError,
    TelegramSendError,
    parse_telegram_callback,
    parse_telegram_update,
)
from app.integrations.telegram_format import parse_callback_token
from app.integrations.transcribe import TranscriptionError, TranscriptionPort

router = APIRouter(prefix="/v1/telegram", tags=["telegram"])


async def _transcribe_telegram_voice(
    *,
    item: dict[str, str],
    media: object,
    transcribe_port: TranscriptionPort,
) -> dict[str, str]:
    if not item.get("file_id"):
        return item
    started = perf_counter()
    download = getattr(media, "download_voice", None)
    if not callable(download):
        item["text"] = ""
        item["source"] = "audio"
        return item
    try:
        audio_bytes, mime_type = await download(item["file_id"])
        try:
            result = await transcribe_port.transcribe(audio=audio_bytes, mime_type=mime_type)
        finally:
            del audio_bytes
    except (RuntimeError, TelegramMediaError, AdapterHttpError, TranscriptionError):
        item["text"] = ""
        item["source"] = "audio"
        return item
    item["text"] = result.text
    item["source"] = "audio"
    item["stt_provider"] = result.stt_provider
    item["stt_model"] = result.stt_model
    item["language"] = result.language
    item["duration_ms"] = str(result.duration_ms)
    item["confidence"] = result.confidence
    item["stt_latency_ms"] = str(elapsed_ms(started))
    return item


async def _handle_callback(
    *,
    callback: dict[str, str],
    port: MessagePort,
    owner_ids: set[str],
    db: Session,
) -> dict:
    """Resolve one inline-button press.

    `answerCallbackQuery` runs first and unconditionally: the client shows a spinner until
    it lands, so acknowledging before doing the work is what keeps the button feeling
    instant. The allowlist is still enforced — a callback carries a `from` like any other
    update and is not privileged.

    Callbacks can be replayed against a message whose buttons are already gone, so the
    decision path must stay idempotent; `decide_approval` is keyed on the approval id.
    """
    answer = getattr(port, "answer_callback_query", None)
    if callable(answer):
        try:
            await answer(callback["callback_query_id"])
        except (TelegramSendError, AdapterHttpError, RuntimeError):
            pass
    if callback["from"] not in owner_ids:
        return {"processed": 0, "ignored": True, "reason": "unauthorized"}
    decision, token = parse_callback_token(callback.get("data", ""))
    if not decision or not token:
        return {"processed": 0, "ignored": True, "reason": "unrecognized_callback"}
    store = LeadStore(db)
    resolved = resolve_owner_callback(store, decision=decision, token=token)
    edit = getattr(port, "edit_message_text", None)
    if callable(edit) and callback.get("message_id"):
        try:
            await edit(
                chat_id=callback["chat_id"],
                message_id=callback["message_id"],
                text=resolved,
                parse_mode="HTML",
            )
        except (TelegramSendError, AdapterHttpError, RuntimeError):
            pass
    return {"processed": 1, "decision": decision, "sent": True}


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
    port: MessagePort = Depends(get_telegram_port),
    transcribe_port: TranscriptionPort = Depends(get_transcription_port),
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict:
    settings = get_settings()
    body = await request.body()
    verify_telegram_secret(
        secret=settings.telegram_webhook_secret,
        header=x_telegram_bot_api_secret_token,
    )
    if settings.kill_switch:
        return {
            "processed": 0,
            "duplicates": 0,
            "sent": False,
            "killed": True,
        }
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"processed": 0, "ignored": True}
    if not isinstance(payload, dict):
        return {"processed": 0, "ignored": True}
    owner_ids = settings.telegram_owner_user_id_set()
    # An Update carries at most one optional field, so message and callback_query are a
    # clean either/or. Without this branch a button press produces a spinner that never
    # resolves and nothing in the logs.
    callback = parse_telegram_callback(payload)
    if callback is not None:
        return await _handle_callback(
            callback=callback, port=port, owner_ids=owner_ids, db=db
        )
    parsed = parse_telegram_update(payload)
    if parsed is None:
        return {"processed": 0, "ignored": True}
    if parsed["from"] not in owner_ids:
        return {"processed": 0, "ignored": True, "reason": "unauthorized"}
    store = LeadStore(db)
    item = dict(parsed)
    if item.get("file_id"):
        item = await _transcribe_telegram_voice(
            item=item, media=port, transcribe_port=transcribe_port
        )
    inbound = {
        "id": item["id"],
        "from": item["from"],
        "chat_id": item.get("chat_id") or item["from"],
        "message_id": item.get("message_id") or "",
        "text": item.get("text") or "",
        "source": item.get("source", ""),
        "stt_provider": item.get("stt_provider", ""),
        "stt_model": item.get("stt_model", ""),
        "language": item.get("language", ""),
        "duration_ms": item.get("duration_ms", "0"),
        "confidence": item.get("confidence", ""),
        "stt_latency_ms": item.get("stt_latency_ms", "0"),
    }
    return await process_inbound_texts(
        provider="telegram",
        channel=Channel.TELEGRAM,
        items=[inbound],
        store=store,
        port=port,
        kill_switch=settings.kill_switch,
        owner_ids=owner_ids,
    )
