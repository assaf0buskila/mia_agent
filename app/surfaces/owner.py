"""Owner Telegram surface. Simple loop. Talk like Dude. No stacked kill-switch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from app.api.inbound_common import (
    event_conversation_id,
    outbound_reply,
    owner_telegram_reply_markup,
)
from app.api.owner import OwnerTurnResult, _is_authorized_owner
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.demo import demo_mode_active
from app.core.errors import MiaError
from app.core.logging import log_owner_agent
from app.db.store import LeadStore
from app.domain.ai_runs import OWNER_REPLY_ACTION, elapsed_ms, persist_ai_run
from app.domain.approvals import DECISION_APPROVED
from app.domain.events import (
    Channel,
    build_message_in_event,
    build_message_out_event,
    new_correlation_id,
    stamp_correlation,
)
from app.domain.gmail.drafts import apply_gmail_send_decision, execute_approved_gmail_send
from app.domain.owner_tasks import OwnerTaskType, classify_owner_task
from app.domain.takeover import apply_owner_human_resume, apply_owner_human_takeover
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort
from app.integrations.gmail import GmailPort
from app.surfaces.crm import ContactsCrm, log_contact
from app.surfaces.identity import extract_fields
from app.surfaces.turn_coalesce import prepare_owner_utterance

_log = logging.getLogger("mia.owner")

OWNER_FALLBACK = "פה. מה צריך?"
CONTACT_LOGGED = "רשמתי ב-Contacts."
CONTACT_REFUSED = "בלי טלפון או אימייל אני לא כותבת שורה."
_SHEET_WALLS = (
    "spreadsheet url",
    "google sheet url",
    "paste the google sheet",
    "paste the sheet url",
    "send me the link",
    "limited access",
    "not the source of truth",
    "not source of truth",
    "קישור לשיט",
    "תשלח את הלינק",
    "תדביק קישור",
)


@dataclass(frozen=True)
class OwnerLoopResult:
    reply: str
    sent: bool
    crm_wrote: bool


def talk_as_dude(
    *,
    text: str,
    crm: ContactsCrm | None = None,
    source: str = "telegram",
    clock: datetime | None = None,
) -> tuple[str, bool]:
    """One owner turn without a task classifier. Returns reply and whether CRM wrote."""
    want = text.strip()
    if not want:
        return OWNER_FALLBACK, False
    fields = extract_fields(want)
    if crm is None:
        return _owner_reply(want, logged=False, refused=False), False
    if fields.has_phone_or_email():
        record = fields.to_contact(source=source)
        log_contact(
            crm,
            record,
            who="אסף",
            channel="telegram",
            action="עדכון איש קשר",
            result="נרשם",
            clock=clock,
        )
        return _owner_reply(want, logged=True, refused=False), True
    if _looks_like_contact_log(want):
        return CONTACT_REFUSED, False
    return _owner_reply(want, logged=False, refused=False), False


def _looks_like_contact_log(text: str) -> bool:
    lowered = text.lower()
    needles = ("תרשמי", "תרשום", "לשים בשיט", "contacts", "איש קשר", "תוסיפי", "log contact")
    return any(needle in lowered or needle in text for needle in needles)


def _owner_reply(text: str, *, logged: bool, refused: bool) -> str:
    if refused:
        return CONTACT_REFUSED
    if logged:
        return f"{CONTACT_LOGGED} מה הלאה?"
    if _looks_hebrew(text):
        return OWNER_FALLBACK
    return "Here. What do you need?"


def _looks_hebrew(text: str) -> bool:
    return any("א" <= ch <= "ת" for ch in text)


async def run_owner_loop(
    *,
    item: dict[str, str],
    store: LeadStore,
    port: MessagePort,
    settings: Settings,
    crm: ContactsCrm,
    gmail_port: GmailPort | None = None,
    owner_ids: set[str],
    provider: str = "telegram",
    channel: Channel = Channel.TELEGRAM,
    talk=None,
) -> OwnerTurnResult:
    """Allowlisted owner turn: talk, optional Gmail send after he asked, optional CRM write."""
    if not _is_authorized_owner(actor_id=item["from"], owner_ids=owner_ids):
        return OwnerTurnResult(processed=False, sent=False, last_reply=None)
    owner_text = (item.get("text") or "").strip()
    correlation_id = new_correlation_id()
    incoming = build_message_in_event(
        provider=provider,
        channel=channel,
        provider_event_id=item["id"],
        conversation_id=event_conversation_id(item),
        text=owner_text,
        actor_role="owner",
        lead_id=None,
    )
    stamp_correlation(incoming, correlation_id)
    store.save_canonical_event(provider=provider, event=incoming)

    reply = ""
    # Human takeover and release existed only on the WhatsApp owner path, which is
    # off. So a conversation Mia escalated could be parked forever with no way to hand
    # it back to her from Telegram.
    if not demo_mode_active(settings):
        task = classify_owner_task(owner_text)
        if not task.needs_clarification:
            if task.task_type is OwnerTaskType.HUMAN_TAKEOVER:
                ack = apply_owner_human_takeover(
                    store, text=owner_text, kill_switch=False
                )
                if ack is not None:
                    reply = ack
            elif task.task_type is OwnerTaskType.HUMAN_TAKEOVER_RESUME:
                ack = apply_owner_human_resume(
                    store, text=owner_text, kill_switch=False
                )
                if ack is not None:
                    reply = ack

    if not reply and gmail_port is not None:
        gmail_intent, gmail_draft_id = apply_gmail_send_decision(
            store,
            text=owner_text,
            kill_switch=False,
        )
        if gmail_intent == DECISION_APPROVED and gmail_draft_id:
            reply = execute_approved_gmail_send(
                store=store,
                settings=settings,
                port=gmail_port,
                draft_id=gmail_draft_id,
                kill_switch=False,
                demo_active=demo_mode_active(settings),
            )

    crm_wrote = False
    if not reply:
        if talk is not None:
            reply, crm_wrote = talk(text=owner_text, crm=crm)
        else:
            reply, crm_wrote = await asyncio.to_thread(
                lambda: _talk_with_optional_agent(
                    text=owner_text,
                    crm=crm,
                    settings=settings,
                    store=store,
                    item=item,
                    correlation_id=correlation_id,
                )
            )

    # Approvals proposed on Telegram had no button and no text command, so
    # `pending_approvals` could only ever grow. Attach the keyboard whenever
    # something is actually waiting on him.
    markup = owner_telegram_reply_markup(
        store, channel=channel, task_type=OwnerTaskType.PENDING_APPROVALS
    )
    message = outbound_reply(item, text=reply, channel=channel, reply_markup=markup)
    try:
        await port.send(message)
        sent = True
    except (RuntimeError, MiaError, AdapterHttpError):
        # TelegramPort.send raises TelegramSendError (a MiaError) and AdapterHttpError.
        # `except RuntimeError` only caught the not-configured DisabledMessagePort, so a
        # Telegram 429 — likely on a split 4096-char reply — threw away an answer the
        # owner had already waited and paid for, and left the webhook row `received`.
        sent = False
    store.mark_webhook(
        provider=provider,
        provider_event_id=item["id"],
        status="sent" if sent else "processed",
    )
    if sent:
        outgoing = build_message_out_event(
            provider=provider,
            channel=channel,
            inbound_provider_event_id=item["id"],
            conversation_id=event_conversation_id(item),
            text=reply,
            lead_id=None,
        )
        stamp_correlation(outgoing, correlation_id)
        store.save_canonical_event(provider=provider, event=outgoing)
    # Learn from what Assaf actually said. `learn_from_exchange` existed and ran only
    # on the muted WhatsApp owner path, so Mia formed no durable memory from her one
    # live owner channel. Deliberately after the send: extraction costs a model call
    # and must never sit between Assaf's message and his answer. It stays inside the
    # existing durable learning path -- no new store, no raw provider data, and the
    # function's own guards still decide what is worth keeping.
    if not demo_mode_active(settings):
        try:
            from app.domain.owner_brain import learn_from_exchange

            learn_from_exchange(
                brain=BrainStore(store.session),
                settings=settings,
                owner_text=owner_text,
                history=tuple(store.list_conversation_turns(event_conversation_id(item))),
                source_ref=item.get("id", ""),
                kill_switch=False,
                demo_active=False,
            )
        except Exception as exc:  # noqa: BLE001 - learning must never cost a reply
            _log.warning("owner learning failed error=%s", type(exc).__name__)
    del crm_wrote
    return OwnerTurnResult(processed=True, sent=sent, last_reply=reply)


def _talk_with_optional_agent(
    *,
    text: str,
    crm: ContactsCrm,
    settings: Settings,
    store: LeadStore,
    item: dict[str, str],
    correlation_id: str = "",
) -> tuple[str, bool]:
    from app.domain.owner_brain import answer_owner

    try:
        fallback, wrote = talk_as_dude(text=text, crm=crm)
    except (MiaError, AdapterHttpError) as exc:
        # `talk_as_dude` writes the CRM row, and the live Sheets adapter raises
        # AdapterHttpError. This sat outside the guard below, so a Sheets 500 aborted
        # the whole turn and the owner got the generic failure line instead of an
        # answer the agent could still have given.
        _log.warning("owner crm write failed error=%s", type(exc).__name__)
        fallback, wrote = OWNER_FALLBACK, False
    if not settings.owner_agent_ready():
        return fallback, wrote
    started = perf_counter()
    try:
        history = tuple(store.list_conversation_turns(event_conversation_id(item)))
        brain = BrainStore(store.session)
        result = answer_owner(
            principal=Principal.owner(source="telegram", actor_id=item["from"]),
            store=store,
            brain=brain,
            settings=settings,
            task_type=OwnerTaskType.NOTE,
            owner_text=prepare_owner_utterance(text, history),
            history=history,
            fallback_text=fallback,
            kill_switch=False,
            demo_active=demo_mode_active(settings),
            source_ref=item.get("id", ""),
            now=datetime.now(UTC),
        )
        # Everything below used to be thrown away: only `.text` was read, so the live
        # Telegram turn recorded no model, no latency, no tokens, no steps, no failed
        # tool and no completion reason anywhere. `persist_ai_run` had a single call
        # site on the muted WhatsApp path, which is why the table the daily brief
        # reports on was fed by nothing Assaf could actually reach.
        log_owner_agent(
            used_agent=result.used_agent,
            model=result.model,
            task_type=OwnerTaskType.NOTE.value,
            tools_used=result.tools_used,
            reason=result.fallback_reason,
            steps=result.steps,
            tools_failed=result.tools_failed,
            completion=result.completion,
        )
        persist_ai_run(
            store,
            run_id=correlation_id,
            lead_id=None,
            channel=Channel.TELEGRAM.value,
            next_action=OWNER_REPLY_ACTION,
            kill_switch=False,
            sales_model=settings.owner_agent_model,
            openai_api_key=settings.openai_api_key,
            sales_fallback_model=settings.owner_agent_fallback_model,
            gemini_api_key=settings.gemini_api_key,
            sales_gemini_model=settings.owner_agent_gemini_model,
            latency_ms=elapsed_ms(started),
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            automation_mode=settings.automation_mode.value,
            model_label=result.model,
        )
        reply = result.text or fallback
        if _asks_for_sheet_url(reply):
            return fallback, wrote
        return reply, wrote
    except Exception as exc:
        # Never silent: a brain outage here used to answer every real question with the
        # greeting "פה. מה צריך?" and leave nothing in the logs to explain why.
        _log.warning("owner agent turn failed error=%s", type(exc).__name__)
        return fallback, wrote


def _asks_for_sheet_url(text: str) -> bool:
    lowered = text.lower()
    return any(wall in lowered or wall in text for wall in _SHEET_WALLS)
