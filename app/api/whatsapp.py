import json
from time import perf_counter
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    get_transcription_port,
    get_whatsapp_media_port,
    get_whatsapp_port,
)
from app.api.inbound import process_inbound_texts
from app.core.config import get_settings
from app.core.errors import MiaError, WebhookRejected
from app.core.webhooks import verify_meta_signature
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms
from app.domain.conversation_scope import existing_whatsapp_scope, whatsapp_stt_allowed
from app.domain.events import Channel
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort
from app.integrations.transcribe import TranscriptionPort
from app.integrations.whatsapp import DisabledWhatsAppMediaPort, WhatsAppMediaPort

router = APIRouter(prefix="/v1/whatsapp", tags=["whatsapp"])


def parse_inbound_texts(payload: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for item in value.get("messages", []):
                if item.get("type") != "text":
                    continue
                body = item.get("text", {}).get("body", "")
                if not body:
                    continue
                messages.append(
                    {
                        "id": str(item.get("id", "")),
                        "from": str(item.get("from", "")),
                        "text": str(body),
                    }
                )
    return messages


def parse_inbound_audio(payload: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for item in value.get("messages", []):
                msg_type = item.get("type")
                media_id = ""
                mime_type = "audio/ogg"
                if msg_type == "audio":
                    audio = item.get("audio", {})
                    media_id = str(audio.get("id", "") or "")
                    mime_type = str(audio.get("mime_type", mime_type) or mime_type)
                elif msg_type == "voice":
                    voice = item.get("voice", {})
                    media_id = str(voice.get("id", "") or "")
                    mime_type = str(voice.get("mime_type", mime_type) or mime_type)
                else:
                    continue
                if not media_id:
                    continue
                messages.append(
                    {
                        "id": str(item.get("id", "")),
                        "from": str(item.get("from", "")),
                        "media_id": media_id,
                        "mime_type": mime_type,
                    }
                )
    return messages


async def transcribe_inbound_audio(
    *,
    provider: str,
    audio_items: list[dict[str, str]],
    store: LeadStore,
    media_port: WhatsAppMediaPort | DisabledWhatsAppMediaPort,
    transcribe_port: TranscriptionPort,
    owner_ids: set[str],
    require_business_scope: bool,
) -> list[dict[str, str]]:
    transcribed: list[dict[str, str]] = []
    for item in audio_items:
        if not item["id"] or not item["from"] or not item.get("media_id"):
            continue
        if store.is_webhook_duplicate(provider=provider, provider_event_id=item["id"]):
            transcribed.append(
                {
                    "id": item["id"],
                    "from": item["from"],
                    "text": "",
                    "source": "audio",
                }
            )
            continue
        scope = existing_whatsapp_scope(store, item["from"])
        if not whatsapp_stt_allowed(
            from_id=item["from"],
            owner_ids=owner_ids,
            scope=scope,
            require_business_scope=require_business_scope,
        ):
            transcribed.append(
                {
                    "id": item["id"],
                    "from": item["from"],
                    "text": "",
                    "source": "audio",
                }
            )
            continue
        try:
            audio_bytes, mime_type = await media_port.download(item["media_id"])
            try:
                started = perf_counter()
                result = await transcribe_port.transcribe(
                    audio=audio_bytes,
                    mime_type=mime_type,
                )
                stt_latency_ms = elapsed_ms(started)
            finally:
                del audio_bytes
        except (RuntimeError, MiaError, AdapterHttpError):
            # `transcribe` raises TranscriptionError (a MiaError) and media download
            # raises AdapterHttpError; neither is a RuntimeError. Unhandled, they left
            # the webhook as a 502, which Composio then retries — turning one failed
            # voice note into repeated delivery of every message in the batch.
            continue
        transcribed.append(
            {
                "id": item["id"],
                "from": item["from"],
                "text": result.text,
                "source": "audio",
                "stt_provider": result.stt_provider,
                "stt_model": result.stt_model,
                "language": result.language,
                "duration_ms": str(result.duration_ms),
                "confidence": result.confidence,
                "stt_latency_ms": str(stt_latency_ms),
            }
        )
    return transcribed


@router.get("/webhook")
def verify_subscription(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
) -> PlainTextResponse:
    settings = get_settings()
    if not settings.whatsapp_verify_token:
        raise WebhookRejected("whatsapp verify token is not configured")
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return PlainTextResponse(hub_challenge)
    raise WebhookRejected("whatsapp verify token mismatch")


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
    port: MessagePort = Depends(get_whatsapp_port),
    media_port: WhatsAppMediaPort | DisabledWhatsAppMediaPort = Depends(get_whatsapp_media_port),
    transcribe_port: TranscriptionPort = Depends(get_transcription_port),
) -> dict:
    settings = get_settings()
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    verify_meta_signature(secret=settings.whatsapp_app_secret, body=body, header=signature)

    if settings.kill_switch:
        return {
            "processed": 0,
            "duplicates": 0,
            "sent": False,
            "sent_count": 0,
            "killed": True,
        }

    payload = json.loads(body.decode() or "{}")
    store = LeadStore(db)
    text_items = parse_inbound_texts(payload)
    audio_items = parse_inbound_audio(payload)
    transcribed_items = await transcribe_inbound_audio(
        provider="whatsapp",
        audio_items=audio_items,
        store=store,
        media_port=media_port,
        transcribe_port=transcribe_port,
        owner_ids=settings.whatsapp_owner_phone_set(),
        require_business_scope=settings.whatsapp_require_business_scope,
    )
    return await process_inbound_texts(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        items=text_items + transcribed_items,
        store=store,
        port=port,
        kill_switch=settings.kill_switch,
        owner_ids=settings.whatsapp_owner_phone_set(),
    )
