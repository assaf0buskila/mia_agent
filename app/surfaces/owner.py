"""Owner Telegram surface. Simple loop. Talk like Dude. No stacked kill-switch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic, perf_counter

from app.api.inbound_common import event_conversation_id, outbound_reply
from app.api.owner import OwnerTurnResult, _is_authorized_owner
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.demo import demo_mode_active
from app.core.errors import MiaError
from app.core.logging import log_owner_agent
from app.core.owner_timing import owner_stage
from app.db.store import LeadStore
from app.domain.ai_runs import (
    OWNER_REPLY_ACTION,
    OWNER_REPLY_FAILED_ACTION,
    elapsed_ms,
    persist_ai_run,
)
from app.domain.events import (
    Channel,
    build_message_in_event,
    build_message_out_event,
    new_correlation_id,
    stamp_correlation,
)
from app.domain.owner.callbacks import approval_token
from app.domain.owner.proposal_cards import pending_approval_cards, render_owner_approval_card
from app.domain.owner.request_routing import (
    is_pending_approvals_request,
    is_tool_inventory_request,
    owner_tool_inventory_reply,
    requests_no_history,
)
from app.domain.owner.tasks import OwnerTaskType
from app.domain.tools import AdapterHttpError
from app.graph.owner_agent import OwnerUsage
from app.integrations.base import MessagePort, OutboundMessage
from app.integrations.telegram_format import approval_keyboard, split_message

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
    owner_ids: set[str],
    provider: str = "telegram",
    channel: Channel = Channel.TELEGRAM,
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

    # The prose reply never carries a keyboard: a button bound to a real, reviewable
    # card is the only thing an approve tap may execute. The explicit pending-approvals
    # command is the sole path allowed to surface an existing pending row; a failed
    # draft/calendar turn must not attach an old one.
    if channel is Channel.TELEGRAM and task_type is OwnerTaskType.PENDING_APPROVALS:
        outbound = _pending_approvals_messages(store, item=item, digest_text=reply)
    else:
        prose = outbound_reply(item, text=reply, channel=channel, reply_markup=None)
        outbound = [(prose, "")]
        if channel is Channel.TELEGRAM and turn_approval_ids:
            outbound.extend(
                _turn_approval_card_messages(store, item=item, approval_ids=turn_approval_ids)
            )

    sent = False
    # A label groups every chunk of one card (or is "" for the digest/prose, which
    # is never grouped). Once one chunk of a card has failed to send, every later
    # chunk sharing its label is skipped -- including the keyboard-bearing last
    # chunk -- so a partially-shown card can never still hand out a live approve
    # button. The card's approval row is untouched: still pending, unretried.
    failed_labels: set[str] = set()
    for index, (message, label) in enumerate(outbound):
        if label and label in failed_labels:
            continue
        try:
            with owner_stage("send", source_ref=item.get("id", ""), tool="telegram"):
                await port.send(message)
            if index == 0:
                sent = True
                if delivery_state is not None:
                    delivery_state["sent"] = True
        except (RuntimeError, MiaError, AdapterHttpError) as exc:
            # TelegramPort.send raises TelegramSendError (a MiaError) and
            # AdapterHttpError. `except RuntimeError` only caught the
            # not-configured DisabledMessagePort, so a Telegram 429 — likely on a
            # split 4096-char reply — threw away an answer the owner had already
            # waited and paid for, and left the webhook row `received`.
            if index == 0:
                sent = False
            else:
                # A card is a follow-up to the primary reply, not the reply
                # itself: its own send failure is logged (never the proposal's
                # content) and the underlying approval row is left exactly as
                # it was -- still pending, never retried or re-executed here.
                failed_labels.add(label)
                _log.warning(
                    "owner proposal card send failed reason=%s error=%s label=%s",
                    "card_send_failed",
                    type(exc).__name__,
                    label,
                )
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


_MAX_PENDING_CARDS = 5


def _card_chunks(
    item: dict[str, str], *, text: str, keyboard: dict, label: str
) -> list[tuple[OutboundMessage, str]]:
    """One proposal card as one or more `OutboundMessage`s, its keyboard on the last.

    A card can render longer than Telegram's message limit (a full Gmail body, a long
    CRM summary); `split_message` chunks it exactly like every other long owner reply.
    Every chunk here is already under that limit, so `TelegramPort.send`'s own
    splitting is a no-op per chunk and the keyboard placed on the last one lands on
    the last physical Telegram message, never a mid-card one.
    """
    chunks = split_message(text) or [text]
    last = len(chunks) - 1
    conversation_id = item.get("chat_id") or item["from"]
    return [
        (
            OutboundMessage(
                conversation_id=conversation_id,
                text=chunk,
                channel=Channel.TELEGRAM.value,
                idempotency_key=f"{item['id']}:{label}:{index}",
                parse_mode="HTML",
                reply_markup=keyboard if index == last else None,
            ),
            label,
        )
        for index, chunk in enumerate(chunks)
    ]


def _turn_approval_card_messages(
    store: LeadStore, *, item: dict[str, str], approval_ids: list[str]
) -> list[tuple[OutboundMessage, str]]:
    """One card per approval id created by this turn, its own keyboard, tool-call order."""
    ordered_ids = tuple(
        dict.fromkeys(value.strip() for value in approval_ids if value and value.strip())
    )
    messages: list[tuple[OutboundMessage, str]] = []
    for approval_id in ordered_ids:
        row = store.get_approval_by_approval_id(approval_id)
        card_text = render_owner_approval_card(row)
        keyboard = approval_keyboard(approval_token(approval_id))
        messages.extend(
            _card_chunks(item, text=card_text, keyboard=keyboard, label=f"turn:{approval_id}")
        )
    return messages


def _pending_approvals_messages(
    store: LeadStore, *, item: dict[str, str], digest_text: str
) -> list[tuple[OutboundMessage, str]]:
    """The explicit "what's pending?" view: the digest, then a card per proposal.

    `digest_text` (`format_pending_approvals_ack`'s output) is always sent as message
    index 0, without a keyboard -- the same text `run_owner_loop` persists as the
    canonical outbound event and returns as `last_reply`, so the audit trail and the
    API can never claim a message the owner did not actually receive.
    """
    digest = outbound_reply(item, text=digest_text, channel=Channel.TELEGRAM, reply_markup=None)
    cards, remaining = pending_approval_cards(store, limit=_MAX_PENDING_CARDS)
    if not cards:
        return [(digest, "")]
    messages: list[tuple[OutboundMessage, str]] = [(digest, "")]
    for approval_id, card_text in cards:
        keyboard = approval_keyboard(approval_token(approval_id))
        messages.extend(
            _card_chunks(
                item, text=card_text, keyboard=keyboard, label=f"pending:{approval_id}"
            )
        )
    if remaining > 0:
        word = "ממתין" if remaining == 1 else "ממתינים"
        messages.append(
            (
                OutboundMessage(
                    conversation_id=item.get("chat_id") or item["from"],
                    text=f"ועוד {remaining} {word} לאישור.",
                    channel=Channel.TELEGRAM.value,
                    idempotency_key=f"{item['id']}:pending:more",
                    parse_mode="HTML",
                ),
                "pending:more",
            )
        )
    return messages


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
    # Kept up to date by `run_owner_agent` as it runs, so the except clause below
    # can still record real provider spend for the turn even when the loop raises
    # before returning a normal `AgentOutcome` (see `OwnerUsage`).
    usage = OwnerUsage()
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
            usage=usage,
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
        # `completion` is only ever set by a real `run_owner_agent` call (success
        # as "answered", failure as "provider_error" / "budget_exhausted" /
        # "refused" / ... ); the early-return paths that never touch the agent at
        # all (kill switch, deterministic intent, no model configured) leave it
        # empty. So "the agent ran and did not complete" -- far more common than
        # an outright exception -- is exactly `not used_agent and completion`,
        # and only that case is a genuine failed turn worth marking as such.
        agent_ran_and_failed = not result.used_agent and bool(result.completion)
        persist_ai_run(
            store,
            run_id=correlation_id,
            lead_id=None,
            channel=Channel.TELEGRAM.value,
            next_action=(
                OWNER_REPLY_FAILED_ACTION if agent_ran_and_failed else OWNER_REPLY_ACTION
            ),
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
        if result.fallback_reason not in ("", "deterministic_intent") and (
            not result.text or result.text == fallback
        ):
            # The brain could not run — kill switch, no model, or the agent failed — and
            # handed back the generic greeting as its text. `result.text or fallback` then
            # answered a real question with "פה. מה צריך?" and the owner could not tell an
            # outage from a working assistant. A specific failure note the brain composed
            # for a read that just failed is more useful than this, and is kept.
            reply = OWNER_UNAVAILABLE
        else:
            reply = result.text or fallback
        if approval_ids_out is not None:
            approval_ids_out.extend(result.approval_ids)
        return reply, wrote
    except Exception as exc:
        # A brain outage here used to answer every real question with the greeting
        # "פה. מה צריך?". Logging was added first; the reply itself still lied until
        # this returned the honest unavailable message instead.
        _log.warning("owner agent turn failed error=%s", type(exc).__name__)
        # The turn used to vanish from ai_runs entirely here: any tokens the loop
        # had already spent before it broke were simply lost, so the audit table
        # looked like the turn never happened. Persist a row for it too, marked
        # failed via next_action, with whatever `usage` the loop reached before
        # raising. `usage.tokens_in/out` are real accumulated values once the
        # loop has taken at least one completed model turn; if it never got that
        # far they are still 0 -- the same "no usage" value `persist_ai_run`
        # already defaults to for every other caller. The schema has no separate
        # column to mark that zero as "unmeasured" rather than "measured"; that
        # is a known limitation of this fix, not a new one it introduces.
        try:
            persist_ai_run(
                store,
                run_id=correlation_id,
                lead_id=None,
                channel=Channel.TELEGRAM.value,
                next_action=OWNER_REPLY_FAILED_ACTION,
                kill_switch=settings.kill_switch,
                sales_model=settings.owner_agent_model,
                openai_api_key=settings.openai_api_key,
                sales_fallback_model=settings.owner_agent_fallback_model,
                gemini_api_key=settings.gemini_api_key,
                sales_gemini_model=settings.owner_agent_gemini_model,
                latency_ms=elapsed_ms(started),
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                automation_mode=settings.automation_mode.value,
            )
        except Exception as persist_exc:  # noqa: BLE001 - the audit write must never mask the original failure
            # The original exception can leave `store.session` in a failed state
            # (e.g. SQLAlchemy's PendingRollbackError once a prior statement in
            # this session errored), which makes this INSERT raise too. The owner
            # still gets the honest OWNER_UNAVAILABLE reply below either way, so
            # this is only logged by reason code -- never a payload -- and never
            # allowed to replace or hide the original failure's handling.
            _log.warning(
                "owner failed-turn ai_run persist failed error=%s",
                type(persist_exc).__name__,
            )
            try:
                # Best-effort recovery so the webhook-status and outbound-event
                # writes still below this in `run_owner_loop` are not also lost
                # to the same broken transaction.
                store.session.rollback()
            except Exception:  # noqa: BLE001 - recovery only, never worth surfacing
                pass
        return OWNER_UNAVAILABLE, wrote
