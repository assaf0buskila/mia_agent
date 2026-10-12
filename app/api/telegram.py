"""Telegram private owner webhook. Allowlist numeric user ids before Mia."""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db, get_telegram_port, get_transcription_port
from app.api.inbound_common import outbound_reply
from app.capabilities.types import Principal
from app.core.config import AutomationMode, get_settings
from app.core.demo import demo_mode_active
from app.core.outbound import send_inbound_reply
from app.core.webhooks import verify_telegram_secret
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms
from app.domain.approvals import DECISION_APPROVED, DECISION_REJECTED
from app.domain.events import Channel, webhook_envelope_kind
from app.domain.gmail.drafts import execute_approved_gmail_send
from app.domain.owner.calendar_writes import execute_approved_calendar_change
from app.domain.owner.callbacks import resolve_owner_callback_result
from app.domain.owner.composio_writes import execute_approved_composio_write
from app.domain.owner.linkedin_writes import execute_approved_linkedin_write
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort, OutboundMessage
from app.integrations.calendar import build_calendar_port
from app.integrations.calendar_booking import build_calendar_booking_port
from app.integrations.gmail import build_gmail_port
from app.integrations.sheets import build_sheets_port
from app.integrations.telegram import (
    TelegramMediaError,
    TelegramSendError,
    is_valid_telegram_owner_context,
    parse_telegram_callback,
    parse_telegram_update,
    validate_telegram_voice_media,
)
from app.integrations.telegram_format import (
    approval_keyboard,
    owner_text,
    parse_callback_token,
    parse_lead_path_token,
)
from app.integrations.transcribe import TranscriptionError, TranscriptionPort
from app.workers.telegram_owner import process_telegram_owner_update

router = APIRouter(prefix="/v1/telegram", tags=["telegram"])
_log = logging.getLogger("mia.telegram")

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


def _is_reminder_callback(data: str) -> bool:
    return re.fullmatch(r"rm:(complete|snooze|contact):([a-f0-9]{32}):(\d{1,8})", data) is not None


async def _answer_owner_callback(port: MessagePort, callback_id: str, *, text: str = "") -> None:
    answer = getattr(port, "answer_callback_query", None)
    if callable(answer):
        try:
            if text:
                await answer(callback_id, text=text)
            else:
                await answer(callback_id)
        except (TelegramSendError, AdapterHttpError, RuntimeError):
            pass


async def _execute_assaf_os_owner_decision(
    *, settings, actor_id: str, proposal_id: str, decision: str
):
    """Run the blocking signed provider decision with a thread-owned DB session."""

    def execute():
        from app.db.session import get_session_factory
        from app.services.owner_actions import (
            OwnerActionExecution,
            execute_approved_owner_action_with_adapters,
            reject_assaf_os_owner_action,
        )

        session = get_session_factory()()
        try:
            thread_store = LeadStore(session)
            principal = Principal.owner(source="telegram", actor_id=actor_id)
            operation_key = f"{proposal_id}:{'execute' if decision == 'approve' else 'reject'}"
            if thread_store.get_provider_write_status(
                scope="approval", key=operation_key
            ) in {"provider_claimed", "pending_review"}:
                return OwnerActionExecution("unknown")
            if decision == "approve":
                outcome = execute_approved_owner_action_with_adapters(
                    thread_store,
                    settings=settings,
                    principal=principal,
                    proposal_id=proposal_id,
                )
            else:
                outcome = reject_assaf_os_owner_action(
                    thread_store,
                    settings=settings,
                    principal=principal,
                    proposal_id=proposal_id,
                )
            session.commit()
            return outcome
        except Exception:
            session.rollback()
            # A preflight transport failure happens before execute_owner_action can
            # create its effect claim. Make that uncertainty sticky as well: the
            # already-approved callback must not become a blind provider retry.
            thread_store = LeadStore(session)
            if thread_store.claim_provider_write(scope="approval", key=operation_key):
                thread_store.mark_provider_write_pending_review(
                    scope="approval", key=operation_key
                )
            session.commit()
            return OwnerActionExecution("unknown")
        finally:
            session.close()

    return await run_in_threadpool(execute)


async def _handle_callback(
    *,
    callback: dict[str, str],
    port: MessagePort,
    owner_ids: set[str],
    db: Session | None,
    settings,
) -> dict:
    """Resolve one inline-button press.

    Private owner context is validated before `answerCallbackQuery`; invalid callbacks
    receive no acknowledgement or other provider operation. Assaf OS decisions are
    durably committed locally before a truthful progress acknowledgement clears the
    Telegram spinner, and before slow remote verification or writing begins.

    Callbacks can be replayed against a message whose buttons are already gone, so the
    decision path must stay idempotent; `decide_approval` is keyed on the approval id.
    """
    if not is_valid_telegram_owner_context(callback, owner_ids):
        _log.info(
            "telegram_owner_context accepted=false kind=callback "
            "reason=invalid_private_owner_context"
        )
        return {"processed": 0, "ignored": True, "reason": "invalid_owner_context"}
    callback_decision, callback_token = parse_callback_token(callback.get("data", ""))
    deferred_assaf_os_ack = False
    if callback_decision and callback_token and db is not None:
        from app.services.owner_actions import read_owner_action

        pending = LeadStore(db).get_approval_by_approval_id(callback_token)
        envelope = read_owner_action(pending)
        deferred_assaf_os_ack = bool(
            envelope is not None and envelope.get("kind") == "assaf_os.approve"
        )
    if not deferred_assaf_os_ack:
        await _answer_owner_callback(port, callback["callback_query_id"])
    selected_path, preparation_token = parse_lead_path_token(callback.get("data", ""))
    if selected_path and preparation_token:
        if db is None:
            raise RuntimeError("recognized lead path callback requires a database session")
        from app.domain.owner.callbacks import approval_token
        from app.domain.owner.proposal_cards import render_owner_approval_card
        from app.services.assaf_os_leads import choose_lead_path

        store = LeadStore(db)
        choice = choose_lead_path(
            store,
            settings=settings,
            principal=Principal.owner(source="telegram", actor_id=callback["from"]),
            approval_id=preparation_token,
            path=selected_path,
        )
        reply_text = {
            "prepared": (
                "המסלול נבחר והוכנה הצעה מדויקת. עדיין לא נכתבה רשומה; "
                "יש לאשר את הכרטיס החדש."
            ),
            "needs_correction": (
                "חסר שם או שפרט הקשר דורש תיקון. שלח למיה את התיקון ואת המסלול; "
                "לא הוכנה הצעת כתיבה."
            ),
            "lead_already_exists": "כבר קיימת בגיליון רשומה עם אותו טלפון או אימייל.",
            "source_changed": "מקור ה-CRM השתנה. לא הוכנה הצעה; צריך לבדוק מחדש.",
            "unknown": (
                "תוצאת הכנת ההצעה אינה ודאית. לא ניסיתי שוב; צריך לבדוק קבלה לפי "
                "מזהה הבקשה המקורי."
            ),
            "expired": "בקשת ההכנה פגה. צריך להכין אותה מחדש.",
            "already_handled": "בקשת ההכנה כבר טופלה ולא ביצעתי אותה שוב.",
        }.get(choice.status, "לא הצלחתי להכין הצעת ליד. לא נכתבה רשומה.")
        if choice.status == "prepared":
            final_row = store.get_approval_by_approval_id(choice.approval_id)
            card = render_owner_approval_card(final_row)
            try:
                await port.send(
                    OutboundMessage(
                        conversation_id=callback["chat_id"],
                        text=owner_text(card, html=True),
                        channel=Channel.TELEGRAM.value,
                        idempotency_key=f"lead-final:{choice.approval_id}",
                        parse_mode="HTML",
                        reply_markup=approval_keyboard(approval_token(choice.approval_id)),
                    )
                )
            except (TelegramSendError, AdapterHttpError, RuntimeError):
                reply_text += " הכרטיס נשמר ברשימת האישורים הממתינים."
        edit = getattr(port, "edit_message_text", None)
        sent = False
        if callable(edit) and callback.get("message_id"):
            try:
                await edit(
                    chat_id=callback["chat_id"],
                    message_id=callback["message_id"],
                    text=owner_text(reply_text, html=True),
                    parse_mode="HTML",
                )
                sent = True
            except (TelegramSendError, AdapterHttpError, RuntimeError):
                sent = False
        return {
            "processed": 1,
            "decision": "lead_path",
            "status": choice.status,
            "sent": sent,
        }
    if _is_reminder_callback(callback.get("data", "")):
        from html import escape

        from app.tools.owner.reminders import handle_reminder_callback

        if db is None:
            raise RuntimeError("recognized reminder callback requires a database session")
        reply = handle_reminder_callback(callback=callback, store=LeadStore(db), settings=settings)
        # Keep the original reminder keyboard available: Open Contact must not destroy
        # Complete/Snooze. Mutations validate revision, so old buttons are harmless.
        try:
            await port.send(
                OutboundMessage(
                    conversation_id=callback["from"],
                    text=escape(reply),
                    channel="telegram",
                    parse_mode="HTML",
                    idempotency_key=f"reminder-callback:{callback['callback_query_id']}",
                )
            )
        except (TelegramSendError, AdapterHttpError, RuntimeError):
            return {"processed": 1, "sent": False}
        return {"processed": 1, "sent": True}
    decision, token = callback_decision, callback_token
    if not decision or not token:
        return {"processed": 0, "ignored": True, "reason": "unrecognized_callback"}
    if db is None:
        raise RuntimeError("recognized owner callback requires a database session")
    store = LeadStore(db)
    crm_pre_synced = False
    crm_sync_failed = False
    crm_sync_blocked = False
    if decision == "approve":
        from app.services.owner_actions import (
            read_owner_action,
            sync_owner_crm_sheet_in_session,
        )

        pending = store.get_approval_by_approval_id(token)
        envelope = read_owner_action(pending)
        if envelope is not None and envelope.get("kind") in {
            "crm.upsert",
            "crm.activity",
            "crm.resolve_conflict",
        }:
            if getattr(settings, "assaf_os_enabled", False):
                # Unified mode rejects this legacy action in owner_actions. Do not
                # pre-read or initialize the old CRM Sheet before that policy gate.
                pass
            elif settings.kill_switch:
                # The callback acknowledgement/edit is operational, but an approval
                # callback must not initialize or read Sheets while writes are stopped.
                # Leave the owner action pending so it can be retried after recovery.
                crm_sync_blocked = True
            else:
                try:
                    sync_owner_crm_sheet_in_session(
                        store,
                        sheets=build_sheets_port(settings),
                        timezone=settings.calendar_timezone,
                    )
                    crm_pre_synced = True
                except Exception:
                    db.rollback()
                    crm_sync_failed = True

    if crm_sync_blocked:
        resolved = None
        reply_text = "מתג העצירה פעיל. האישור נשאר ממתין ולא בוצע."
    elif crm_sync_failed:
        resolved = None
        reply_text = "לא הצלחתי לרענן את ה-CRM. האישור נשאר ממתין ולא בוצע."
    else:
        resolved = resolve_owner_callback_result(
            store,
            decision=decision,
            token=token,
            principal=Principal.owner(source="telegram", actor_id=callback["from"]),
        )
        reply_text = resolved.text
    if deferred_assaf_os_ack:
        desired = DECISION_APPROVED if decision == "approve" else DECISION_REJECTED
        decided = store.get_approval_by_approval_id(token)
        decision_claimed = decided is not None and decided.decision == desired
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            await _answer_owner_callback(
                port,
                callback["callback_query_id"],
                text="האישור לא נקלט. אפשר לנסות שוב.",
            )
            raise HTTPException(
                status_code=503,
                detail="owner approval temporarily unavailable",
            ) from None
        progress_text = (
            "האישור נקלט. בודק קבלה לפני עדכון סופי…"
            if decision_claimed and decision == "approve"
            else (
                "הדחייה נקלטה. בודק קבלה לפני עדכון סופי…"
                if decision_claimed
                else "האישור אינו תקף ולא יבוצע."
            )
        )
        await _answer_owner_callback(
            port, callback["callback_query_id"], text=progress_text
        )
        if decision_claimed and callback.get("message_id"):
            edit_progress = getattr(port, "edit_message_text", None)
            if callable(edit_progress):
                try:
                    await edit_progress(
                        chat_id=callback["chat_id"],
                        message_id=callback["message_id"],
                        text=owner_text(progress_text, html=True),
                        parse_mode="HTML",
                    )
                except (TelegramSendError, AdapterHttpError, RuntimeError):
                    pass
    if resolved is not None and resolved.gmail_draft_id_to_send is not None:
        reply_text = execute_approved_gmail_send(
            store=store,
            settings=settings,
            port=build_gmail_port(settings),
            draft_id=resolved.gmail_draft_id_to_send,
            kill_switch=settings.kill_switch,
            demo_active=demo_mode_active(settings),
        )
    if resolved is not None and resolved.calendar_resource_id_to_execute is not None:
        reply_text = execute_approved_calendar_change(
            store=store,
            settings=settings,
            calendar=build_calendar_port(settings),
            booking=build_calendar_booking_port(settings),
            resource_id=resolved.calendar_resource_id_to_execute,
            kill_switch=settings.kill_switch,
            demo_active=demo_mode_active(settings),
        )
    if resolved is not None and resolved.linkedin_resource_id_to_execute is not None:
        reply_text = execute_approved_linkedin_write(
            store=store,
            settings=settings,
            resource_id=resolved.linkedin_resource_id_to_execute,
            kill_switch=settings.kill_switch,
        )
    if resolved is not None and resolved.composio_resource_id_to_execute is not None:
        reply_text = execute_approved_composio_write(
            store=store,
            settings=settings,
            resource_id=resolved.composio_resource_id_to_execute,
            kill_switch=settings.kill_switch,
        )
    if resolved is not None and resolved.owner_action_resource_id_to_execute is not None:
        if deferred_assaf_os_ack:
            outcome = await _execute_assaf_os_owner_decision(
                settings=settings,
                actor_id=callback["from"],
                proposal_id=resolved.owner_action_resource_id_to_execute,
                decision="approve",
            )
        else:
            from app.services.owner_actions import execute_approved_owner_action_with_adapters

            outcome = execute_approved_owner_action_with_adapters(
                store,
                settings=settings,
                principal=Principal.owner(source="telegram", actor_id=callback["from"]),
                proposal_id=resolved.owner_action_resource_id_to_execute,
                crm_pre_synced=crm_pre_synced,
            )
        reply_text = outcome.text or {
            "target_changed": "היעד השתנה. צריך להכין בקשת אישור חדשה.",
            "already_handled": "הפעולה כבר טופלה או ממתינה לבדיקה. לא ביצעתי שוב.",
            "unknown": "תוצאת הפעולה אינה ודאית וממתינה לבדיקה. לא ביצעתי שוב.",
            "expired": "האישור פג. צריך להכין בקשת אישור חדשה.",
            "rejected": "ההצעה כבר נדחתה ולא בוצעה.",
            "policy_denied": "מתג העצירה פעיל. הפעולה לא בוצעה.",
        }.get(outcome.status, "הפעולה לא בוצעה.")
    if resolved is not None and decision == "reject":
        rejected_row = store.get_approval_by_approval_id(token)
        from app.services.owner_actions import read_owner_action, reject_assaf_os_owner_action

        rejected_envelope = read_owner_action(rejected_row)
        if rejected_envelope is not None and rejected_envelope.get("kind") == "assaf_os.approve":
            if deferred_assaf_os_ack:
                rejected = await _execute_assaf_os_owner_decision(
                    settings=settings,
                    actor_id=callback["from"],
                    proposal_id=rejected_row.resource_id,
                    decision="reject",
                )
            else:
                rejected = reject_assaf_os_owner_action(
                    store,
                    settings=settings,
                    principal=Principal.owner(source="telegram", actor_id=callback["from"]),
                    proposal_id=rejected_row.resource_id,
                )
            reply_text = rejected.text or {
                "already_handled": "הדחייה כבר טופלה.",
                "unknown": "תוצאת הדחייה אינה ודאית וממתינה לבדיקה. לא שלחתי שוב.",
                "committed": "ההצעה כבר אושרה ובוצעה, ולכן לא ניתן לדחות אותה.",
                "expired": "ההצעה פגה ולא בוצעה.",
                "target_changed": "היעד השתנה וההצעה לא בוצעה.",
                "policy_denied": "מתג העצירה פעיל. הדחייה לא נשלחה לגיליון.",
            }.get(rejected.status, reply_text)
    # Every branch above either interpolates only server-escaped HTML (bold()/code()
    # from resolve_owner_callback_result) or a fixed Hebrew literal, except
    # outcome.text, which can carry a provider-echoed value (see
    # app/services/owner_actions.py execute_owner_action) -- that value is escaped at
    # its source, not here, so this call is dash/bidi normalisation only, never a
    # second escaping pass over already-built HTML.
    reply_text = owner_text(reply_text, html=True)
    # `sent` reflects whether the edit itself landed -- never re-run the decided
    # action just because the owner's console failed to update. A failed edit still
    # leaves the result unreported, so one fallback sendMessage carries the same
    # `reply_text` as a new message instead of silently dropping it.
    edit = getattr(port, "edit_message_text", None)
    sent = False
    if callable(edit) and callback.get("message_id"):
        try:
            await edit(
                chat_id=callback["chat_id"],
                message_id=callback["message_id"],
                text=reply_text,
                parse_mode="HTML",
            )
            sent = True
        except (TelegramSendError, AdapterHttpError, RuntimeError):
            sent = False
    if not sent:
        send = getattr(port, "send", None)
        if callable(send):
            try:
                await send(
                    OutboundMessage(
                        conversation_id=callback["chat_id"],
                        text=reply_text,
                        channel=Channel.TELEGRAM.value,
                        idempotency_key=f"tg-cb-fallback:{callback.get('callback_query_id', '')}",
                        parse_mode="HTML",
                    )
                )
            except (TelegramSendError, AdapterHttpError, RuntimeError) as exc:
                _log.warning(
                    "owner callback fallback send failed reason=%s error=%s",
                    "callback_fallback_send_failed",
                    type(exc).__name__,
                )
    return {"processed": 1, "decision": decision, "sent": sent}


def _webhook_accepted(*, duplicate: bool, voice: bool = False) -> dict:
    if duplicate:
        return {"accepted": False, "duplicate": True, "voice": voice}
    return {"accepted": True, "duplicate": False}


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
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
        callback_is_private = is_valid_telegram_owner_context(callback, owner_ids)
        decision, token = parse_callback_token(callback.get("data", ""))
        lead_path, lead_token = parse_lead_path_token(callback.get("data", ""))
        recognized = (
            bool(decision and token)
            or bool(lead_path and lead_token)
            or _is_reminder_callback(callback.get("data", ""))
        )
        if not callback_is_private or not recognized:
            return await _handle_callback(
                callback=callback,
                port=port,
                owner_ids=owner_ids,
                db=None,
                settings=settings,
            )
        with contextmanager(get_db)() as db:
            return await _handle_callback(
                callback=callback,
                port=port,
                owner_ids=owner_ids,
                db=db,
                settings=settings,
            )
    if "callback_query" in payload:
        _log.info(
            "telegram_owner_context accepted=false kind=callback "
            "reason=invalid_private_owner_context"
        )
        return {"processed": 0, "ignored": True, "reason": "invalid_owner_context"}
    parsed = parse_telegram_update(payload)
    if parsed is None:
        if "message" in payload or "edited_message" in payload:
            _log.info(
                "telegram_owner_context accepted=false kind=message "
                "reason=invalid_private_owner_context"
            )
            return {"processed": 0, "ignored": True, "reason": "invalid_owner_context"}
        return {"processed": 0, "ignored": True}
    if not is_valid_telegram_owner_context(parsed, owner_ids):
        _log.info(
            "telegram_owner_context accepted=false kind=message "
            "reason=invalid_private_owner_context"
        )
        return {"processed": 0, "ignored": True, "reason": "invalid_owner_context"}
    item = dict(parsed)
    voice = bool(item.get("file_id"))
    photo = bool(item.get("photo_file_id"))
    if voice:
        envelope_kind = "audio"
    elif photo:
        envelope_kind = "image"
    else:
        envelope_kind = webhook_envelope_kind(item)
    with contextmanager(get_db)() as db:
        store = LeadStore(db)
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
