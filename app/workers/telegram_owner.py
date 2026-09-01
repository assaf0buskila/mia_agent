"""Deferred Telegram owner processing after webhook ack.

Telegram retries slow webhooks. Owner turns (voice STT + LangGraph + Composio) can run
for minutes and must not block the HTTP worker that also serves /health and the website
widget. The webhook claims the update, commits, returns 200, then runs here on a fresh
DB session.
"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

from app.api.inbound_common import event_conversation_id
from app.api.owner import process_owner_texts
from app.core.config import get_settings
from app.core.logging import log_comm
from app.db.session import get_session_factory
from app.db.store import LeadStore
from app.domain.events import (
    Channel,
    persist_tool_outcome,
    transcription_outcome,
)
from app.integrations.base import MessagePort
from app.integrations.transcribe import TranscriptionPort


async def process_telegram_owner_update(
    *,
    item: dict[str, str],
    envelope_kind: str,
    voice_file_id: str | None,
    port: MessagePort,
    transcribe_port: TranscriptionPort,
) -> None:
    from app.api.telegram import (
        _send_transcription_failure_reply,
        _transcribe_telegram_voice,
    )

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
                    kill_switch=settings.kill_switch,
                    automation_mode=settings.automation_mode,
                )
                store.mark_webhook(
                    provider="telegram",
                    provider_event_id=work["id"],
                    status="sent" if sent else "failed",
                )
                session.commit()
                return
        inbound = {
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
        await process_owner_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[inbound],
            store=store,
            port=port,
            kill_switch=settings.kill_switch,
            owner_ids=owner_ids,
            preclaimed_event_id=work["id"],
            preclaimed_envelope_kind=envelope_kind,
        )
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
        raise
    finally:
        session.close()
