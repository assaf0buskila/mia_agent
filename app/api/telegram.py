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
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort
from app.integrations.telegram import TelegramMediaError, parse_telegram_update
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
    parsed = parse_telegram_update(payload)
    if parsed is None:
        return {"processed": 0, "ignored": True}
    owner_ids = settings.telegram_owner_user_id_set()
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
