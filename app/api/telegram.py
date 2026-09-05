"""Telegram private owner webhook. Allowlist numeric user ids before Mia."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_telegram_port, get_transcription_port
from app.api.inbound_common import outbound_reply
from app.core.config import AutomationMode, get_settings
from app.core.demo import demo_mode_active
from app.core.outbound import send_inbound_reply
from app.core.webhooks import verify_telegram_secret
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms
from app.domain.events import Channel, webhook_envelope_kind
from app.domain.gmail.drafts import execute_approved_gmail_send
from app.domain.owner.calendar_writes import execute_approved_calendar_change
from app.domain.owner.callbacks import resolve_owner_callback_result
from app.domain.owner.composio_writes import execute_approved_composio_write
from app.domain.owner.linkedin_writes import execute_approved_linkedin_write
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort
from app.integrations.calendar import build_calendar_port
from app.integrations.calendar_booking import build_calendar_booking_port
from app.integrations.gmail import build_gmail_port
from app.integrations.telegram import (
    TelegramMediaError,
    TelegramSendError,
    parse_telegram_callback,
    parse_telegram_update,
    validate_telegram_voice_media,
)
from app.integrations.telegram_format import parse_callback_token
from app.integrations.transcribe import TranscriptionError, TranscriptionPort
from app.workers.telegram_owner import process_telegram_owner_update

router = APIRouter(prefix="/v1/telegram", tags=["telegram"])

# This is deliberately fixed text: a transcription provider failure must be visible to
# the owner, but no provider response, audio bytes, transcript, or configuration detail
# may be reflected back into Telegram.
_VOICE_TRANSCRIPTION_FAILURE_REPLY = (
    "לא הצלחתי לתמלל את ההודעה הקולית. אפשר לנסות שוב או לשלוח טקסט."
)

VoiceFailureStage = Literal[
    "",
    "download_unavailable",
    "download_failed",
    "media_rejected",
    "stt_failed",
]


def _voice_failure(
    item: dict[str, str], *, stage: VoiceFailureStage, started: float
) -> tuple[dict[str, str], VoiceFailureStage, int]:
    """Return only a fixed operational class; never retain provider or audio detail."""
    return item, stage, elapsed_ms(started)


async def _transcribe_telegram_voice(
    *,
    item: dict[str, str],
    media: object,
    transcribe_port: TranscriptionPort,
) -> tuple[dict[str, str], VoiceFailureStage, int]:
    if not item.get("file_id"):
        return item, "", 0
    started = perf_counter()
    download = getattr(media, "download_voice", None)
    if not callable(download):
        return _voice_failure(item, stage="download_unavailable", started=started)
    try:
        downloaded = await download(
            item["file_id"],
            declared_mime_type=item.get("mime_type", ""),
            declared_filename=item.get("file_name", ""),
        )
    except (RuntimeError, TelegramMediaError, AdapterHttpError, TypeError, ValueError):
        return _voice_failure(item, stage="download_failed", started=started)
    try:
        audio_bytes, mime_type, filename = downloaded
    except (TypeError, ValueError):
        return _voice_failure(item, stage="download_failed", started=started)
    try:
        audio_bytes, mime_type = validate_telegram_voice_media(audio_bytes, mime_type)
    except TelegramMediaError:
        return _voice_failure(item, stage="media_rejected", started=started)
    try:
        result = await transcribe_port.transcribe(
            audio=audio_bytes, mime_type=mime_type, filename=filename
        )
    except (RuntimeError, AdapterHttpError, TranscriptionError):
        return _voice_failure(item, stage="stt_failed", started=started)
    finally:
        del audio_bytes
    if not (result.text or "").strip():
        return _voice_failure(item, stage="stt_failed", started=started)
    item["text"] = result.text
    item["source"] = "audio"
    item["stt_provider"] = result.stt_provider
    item["stt_model"] = result.stt_model
    item["language"] = result.language
    item["duration_ms"] = str(result.duration_ms)
    item["confidence"] = result.confidence
    latency_ms = elapsed_ms(started)
    item["stt_latency_ms"] = str(latency_ms)
    return item, "", latency_ms


async def _send_transcription_failure_reply(
    *,
    item: dict[str, str],
    port: MessagePort,
    kill_switch: bool,
    automation_mode: AutomationMode,
) -> bool:
    """Reply safely when voice media/STT is unavailable, without invoking OwnerGraph.

    There is no owner request text to reason over on this path.  Treating an STT failure
    as an empty request used to route it through OwnerGraph and turn it into an unrelated
    clarification, hiding the actual operational failure from Assaf.
    """
    return await send_inbound_reply(
        port=port,
        message=outbound_reply(
            item,
            text=_VOICE_TRANSCRIPTION_FAILURE_REPLY,
            channel=Channel.TELEGRAM,
        ),
        kill_switch=False,
        automation_mode=automation_mode,
        actor_role="owner",
    )


async def _handle_callback(
    *,
    callback: dict[str, str],
    port: MessagePort,
    owner_ids: set[str],
    db: Session,
    settings,
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
    resolved = resolve_owner_callback_result(store, decision=decision, token=token)
    reply_text = resolved.text
    if resolved.gmail_draft_id_to_send is not None:
        reply_text = execute_approved_gmail_send(
            store=store,
            settings=settings,
            port=build_gmail_port(settings),
            draft_id=resolved.gmail_draft_id_to_send,
            kill_switch=settings.kill_switch,
            demo_active=demo_mode_active(settings),
        )
    if resolved.calendar_resource_id_to_execute is not None:
        reply_text = execute_approved_calendar_change(
            store=store,
            settings=settings,
            calendar=build_calendar_port(settings),
            booking=build_calendar_booking_port(settings),
            resource_id=resolved.calendar_resource_id_to_execute,
            kill_switch=settings.kill_switch,
            demo_active=demo_mode_active(settings),
        )
    if resolved.linkedin_resource_id_to_execute is not None:
        reply_text = execute_approved_linkedin_write(
            store=store,
            settings=settings,
            resource_id=resolved.linkedin_resource_id_to_execute,
            kill_switch=settings.kill_switch,
        )
    if resolved.composio_resource_id_to_execute is not None:
        reply_text = execute_approved_composio_write(
            store=store,
            settings=settings,
            resource_id=resolved.composio_resource_id_to_execute,
            kill_switch=settings.kill_switch,
        )
    edit = getattr(port, "edit_message_text", None)
    if callable(edit) and callback.get("message_id"):
        try:
            await edit(
                chat_id=callback["chat_id"],
                message_id=callback["message_id"],
                text=reply_text,
                parse_mode="HTML",
            )
        except (TelegramSendError, AdapterHttpError, RuntimeError):
            pass
    return {"processed": 1, "decision": decision, "sent": True}


def _webhook_accepted(*, duplicate: bool, voice: bool = False) -> dict:
    if duplicate:
        return {"accepted": False, "duplicate": True, "voice": voice}
    return {"accepted": True, "duplicate": False}


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
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
            callback=callback,
            port=port,
            owner_ids=owner_ids,
            db=db,
            settings=settings,
        )
    parsed = parse_telegram_update(payload)
    if parsed is None:
        return {"processed": 0, "ignored": True}
    if parsed["from"] not in owner_ids:
        return {"processed": 0, "ignored": True, "reason": "unauthorized"}
    store = LeadStore(db)
    item = dict(parsed)
    voice = bool(item.get("file_id"))
    photo = bool(item.get("photo_file_id"))
    if voice:
        envelope_kind = "audio"
    elif photo:
        envelope_kind = "image"
    else:
        envelope_kind = webhook_envelope_kind(item)
    if not store.claim_webhook(
        provider="telegram",
        provider_event_id=item["id"],
        channel=Channel.TELEGRAM.value,
        envelope_kind=envelope_kind,
    ):
        return _webhook_accepted(duplicate=True, voice=voice)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="telegram processing temporarily unavailable",
        ) from None
    background_tasks.add_task(
        process_telegram_owner_update,
        item=item,
        envelope_kind=envelope_kind,
        voice_file_id=item.get("file_id") or None,
        photo_file_id=item.get("photo_file_id") or None,
        port=port,
        transcribe_port=transcribe_port,
    )
    return _webhook_accepted(duplicate=False)
