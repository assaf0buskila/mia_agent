"""Owner Telegram surface. Simple loop. Talk like Dude. No stacked kill-switch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.api.inbound_common import event_conversation_id, outbound_reply
from app.api.owner import OwnerTurnResult, _is_authorized_owner
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.demo import demo_mode_active
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED
from app.domain.events import (
    Channel,
    build_message_in_event,
    build_message_out_event,
    new_correlation_id,
    stamp_correlation,
)
from app.domain.gmail_drafts import apply_gmail_send_decision, execute_approved_gmail_send
from app.domain.owner_tasks import OwnerTaskType
from app.integrations.base import MessagePort
from app.integrations.gmail import GmailPort
from app.surfaces.crm import ContactsCrm, log_contact
from app.surfaces.identity import extract_fields

OWNER_FALLBACK = "פה. מה צריך?"
CONTACT_LOGGED = "רשמתי ב-Contacts."
CONTACT_REFUSED = "בלי טלפון או אימייל אני לא כותבת שורה."


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
    if gmail_port is not None:
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
            reply, crm_wrote = _talk_with_optional_agent(
                text=owner_text,
                crm=crm,
                settings=settings,
                store=store,
                item=item,
            )

    message = outbound_reply(item, text=reply, channel=channel)
    try:
        await port.send(message)
        sent = True
    except RuntimeError:
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
    del crm_wrote
    return OwnerTurnResult(processed=True, sent=sent, last_reply=reply)


def _talk_with_optional_agent(
    *,
    text: str,
    crm: ContactsCrm,
    settings: Settings,
    store: LeadStore,
    item: dict[str, str],
) -> tuple[str, bool]:
    from app.domain.owner_brain import answer_owner

    fallback, wrote = talk_as_dude(text=text, crm=crm)
    if not settings.owner_agent_ready():
        return fallback, wrote
    try:
        brain = BrainStore(store.session)
        result = answer_owner(
            principal=Principal.owner(source="telegram", actor_id=item["from"]),
            store=store,
            brain=brain,
            settings=settings,
            task_type=OwnerTaskType.NOTE,
            owner_text=text,
            history=tuple(store.list_conversation_turns(event_conversation_id(item))),
            fallback_text=fallback,
            kill_switch=False,
            demo_active=demo_mode_active(settings),
            source_ref=item.get("id", ""),
            now=datetime.now(UTC),
        )
        return (result.text or fallback), wrote
    except Exception:
        return fallback, wrote
