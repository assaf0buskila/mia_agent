"""Owner Telegram surface. Simple loop. Talk like Dude. No stacked kill-switch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic, perf_counter

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
from app.core.owner_timing import owner_stage
from app.db.store import LeadStore
from app.domain.ai_runs import OWNER_REPLY_ACTION, elapsed_ms, persist_ai_run
from app.domain.events import (
    Channel,
    build_message_in_event,
    build_message_out_event,
    new_correlation_id,
    stamp_correlation,
)
from app.domain.owner.request_routing import (
    is_pending_approvals_request,
    is_tool_inventory_request,
    owner_tool_inventory_reply,
    requests_no_history,
)
from app.domain.owner.tasks import OwnerTaskType
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort
from app.integrations.gmail import GmailPort
from app.surfaces.crm import ContactsCrm

_log = logging.getLogger("mia.owner")

OWNER_FALLBACK = "פה. מה צריך?"
OWNER_UNAVAILABLE = "מנוע השיחה של מיה לא זמין כרגע."


@dataclass(frozen=True)
class OwnerLoopResult:
    reply: str
    sent: bool
    crm_wrote: bool


def _owner_reply(text: str) -> str:
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
    deadline_at: float | None = None,
    delivery_state: dict[str, bool] | None = None,
) -> OwnerTurnResult:
    """Allowlisted owner turn: talk, optional Gmail send after he asked, optional CRM write."""
    if not _is_authorized_owner(actor_id=item["from"], owner_ids=owner_ids):
        return OwnerTurnResult(processed=False, sent=False, last_reply=None)
    if settings.kill_switch:
        try:
            store.mark_webhook(
                provider=provider,
                provider_event_id=item["id"],
                status="processed",
            )
        except KeyError:
            pass
        return OwnerTurnResult(processed=True, sent=False, last_reply=None)
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
    task_type = OwnerTaskType.NOTE
    crm_wrote = False
    turn_approval_ids: list[str] = []
    inventory_request = not reply and is_tool_inventory_request(owner_text)
    pending_request = not reply and is_pending_approvals_request(owner_text)
    if pending_request:
        from app.domain.owner.reads import format_pending_approvals_ack

        task_type = OwnerTaskType.PENDING_APPROVALS
        reply = format_pending_approvals_ack(store)
    elif inventory_request:
        reply = owner_tool_inventory_reply()
    elif not reply:
        reply, crm_wrote = await asyncio.to_thread(
            lambda: _talk_with_optional_agent(
                text=owner_text,
                authorization_text=(
                    item["owner_request_text"]
                    if "owner_request_text" in item
                    else owner_text
                ).strip(),
                settings=settings,
                store=store,
                item=item,
                correlation_id=correlation_id,
                deadline_at=deadline_at,
                approval_ids_out=turn_approval_ids,
            )
        )

    # The worker sends the one timeout notice after it has drained this coroutine.
    # Do not send a late model answer and race that notice.
    if deadline_at is not None and monotonic() >= deadline_at:
        return OwnerTurnResult(processed=True, sent=False, last_reply=None)

    # Bind a proposal button only to approval metadata created by this turn. The
    # explicit pending-approvals command is the sole path allowed to select an
    # existing pending row; a failed draft/calendar turn must not attach an old one.
    markup_task_type = (
        OwnerTaskType.PENDING_APPROVALS
        if task_type is OwnerTaskType.PENDING_APPROVALS
        else OwnerTaskType.NOTE
    )
    markup = owner_telegram_reply_markup(
        store,
        channel=channel,
        task_type=markup_task_type,
        turn_approval_ids=tuple(turn_approval_ids),
    )
    message = outbound_reply(item, text=reply, channel=channel, reply_markup=markup)
    try:
        with owner_stage("send", source_ref=item.get("id", ""), tool="telegram"):
            await port.send(message)
        sent = True
        if delivery_state is not None:
            delivery_state["sent"] = True
    except (RuntimeError, MiaError, AdapterHttpError):
        # TelegramPort.send raises TelegramSendError (a MiaError) and AdapterHttpError.
        # `except RuntimeError` only caught the not-configured DisabledMessagePort, so a
        # Telegram 429 — likely on a split 4096-char reply — threw away an answer the
        # owner had already waited and paid for, and left the webhook row `received`.
        sent = False
    try:
        store.mark_webhook(
            provider=provider,
            provider_event_id=item["id"],
            status="sent" if sent else "processed",
        )
    except Exception as exc:  # noqa: BLE001 - accepted delivery must not be re-noticed
        if not sent:
            raise
        _log.warning(
            "owner webhook status update failed after delivery error=%s",
            type(exc).__name__,
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
        try:
            store.save_canonical_event(provider=provider, event=outgoing)
        except Exception as exc:  # noqa: BLE001 - reply already reached Telegram
            _log.warning(
                "owner outbound event save failed after delivery error=%s",
                type(exc).__name__,
            )
    del crm_wrote
    return OwnerTurnResult(processed=True, sent=sent, last_reply=reply)


def _talk_with_optional_agent(
    *,
    text: str,
    authorization_text: str | None = None,
    crm: object | None = None,
    settings: Settings,
    store: LeadStore,
    item: dict[str, str],
    correlation_id: str = "",
    deadline_at: float | None = None,
    approval_ids_out: list[str] | None = None,
) -> tuple[str, bool]:
    del crm  # compatibility only; all mutations flow through registered v2 tools
    from app.domain.owner.brain import answer_owner

    try:
        fallback, wrote = _owner_reply(text), False
    except (MiaError, AdapterHttpError) as exc:
        _log.warning("owner fallback failed error=%s", type(exc).__name__)
        fallback, wrote = OWNER_FALLBACK, False
    if not settings.owner_agent_ready():
        return OWNER_UNAVAILABLE, False
    started = perf_counter()
    try:
        no_history = requests_no_history(text)
        history = (
            () if no_history else tuple(store.list_conversation_turns(event_conversation_id(item)))
        )
        brain = BrainStore(store.session)
        result = answer_owner(
            principal=Principal.owner(source="telegram", actor_id=item["from"]),
            store=store,
            brain=brain,
            settings=settings,
            task_type=OwnerTaskType.NOTE,
            # Keep the exact current owner message as the authorization source. Any
            # contextual hint belongs in graph context, never in write-intent text.
            owner_text=text.strip(),
            raw_owner_request=(
                authorization_text if authorization_text is not None else text
            ).strip(),
            history=history,
            fallback_text=fallback,
            kill_switch=settings.kill_switch,
            demo_active=demo_mode_active(settings),
            source_ref=item.get("id", ""),
            now=datetime.now(UTC),
            deadline_at=deadline_at,
            input_source=item.get("source") or "text",
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
            kill_switch=settings.kill_switch,
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
        if approval_ids_out is not None:
            approval_ids_out.extend(result.approval_ids)
        return reply, wrote
    except Exception as exc:
        # Never silent: a brain outage here used to answer every real question with the
        # greeting "פה. מה צריך?" and leave nothing in the logs to explain why.
        _log.warning("owner agent turn failed error=%s", type(exc).__name__)
        return fallback, wrote
