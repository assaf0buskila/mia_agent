"""One finalization workflow. Idempotent on conversation + summary version."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.domain.memory import ConversationTurn
from app.domain.sales import SalesState
from app.domain.website_handoff_brief import KIND_WEBSITE_WHATSAPP
from app.services.conversation_facts import (
    describe_business,
    describe_meeting,
    describe_name,
    describe_pain,
    describe_qualification,
    extract_budget,
    extract_contact,
    extract_need,
    extract_timeline,
    relevant_service,
)
from app.services.notifications import deliver_owner_telegram, render_conversation_summary

KIND_PREFIX = "web_final_"
SUMMARY_VERSION = "v1"
KIND = f"{KIND_PREFIX}{SUMMARY_VERSION}"


class ConversationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    lead_id: str
    name: str | None = None
    contact: str | None = None
    business: str | None = None
    need: str | None = None
    pain: str | None = None
    relevant_service: str | None = None
    timeline: str | None = None
    budget: str | None = None
    qualification: str | None = None
    meeting_status: str | None = None
    recommended_next_step: str | None = None


class FinalizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claimed: bool
    sent: bool = False
    duplicate: bool = False
    kind: str = KIND


class NotificationStore(Protocol):
    def has_owner_notification(self, *, kind: str, lead_id: str) -> bool: ...

    def try_insert_owner_notification(
        self,
        *,
        kind: str,
        lead_id: str,
        scheduled_at: str,
        conversation_id: str = "",
    ) -> bool: ...

    def release_owner_notification_claim(
        self, *, kind: str, lead_id: str, conversation_id: str = ""
    ) -> None: ...

    def owner_notification_claimed_at(
        self, *, kind: str, lead_id: str, conversation_id: str = ""
    ) -> str | None: ...

    def upsert_owner_notification(
        self, *, kind: str, lead_id: str, scheduled_at: str
    ) -> None: ...

    def try_claim_owner_notification_recipient(
        self,
        *,
        kind: str,
        lead_id: str,
        notification_key: str,
        recipient_id: str,
        claimed_at: str,
    ) -> bool: ...

    def release_owner_notification_recipient_claim(
        self,
        *,
        kind: str,
        lead_id: str,
        notification_key: str,
        recipient_id: str,
    ) -> None: ...


class WebsiteFinalizationStore(NotificationStore, Protocol):
    def has_website_prospect_message(self, lead_id: str, conversation_id: str) -> bool: ...

    def list_inactive_website_conversations(
        self,
        *,
        cutoff_iso: str,
        skip_kinds: tuple[str, ...] = (),
        skip_conversation_kinds: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[tuple[str, str]]: ...

    def get_sales(self, lead_id: str) -> Any: ...

    def list_conversation_turns(self, conversation_id: str) -> list[ConversationTurn]: ...

    def get_meeting(self, lead_id: str) -> Any: ...


def kind_for(version: str = SUMMARY_VERSION) -> str:
    return f"{KIND_PREFIX}{version}"[:32]


def finalize_website_conversation(
    store: NotificationStore,
    *,
    summary: ConversationSummary,
    settings: Settings,
    now: datetime | None = None,
    send: bool = True,
    version: str = SUMMARY_VERSION,
) -> FinalizeResult:
    """Claim once per conversation, then notify. Retries after a claim do not send again.

    The claim is keyed on the CONVERSATION, not the lead. Keying it on the lead meant a
    returning visitor's second conversation was reported as a duplicate and Assaf was never
    told about it — a silently lost lead. There is also no read-before-write here any more:
    the claiming insert is the whole decision, so two concurrent finalizations of the same
    conversation cannot both pass, and the loser gets False rather than an exception.
    """
    kind = kind_for(version)
    lead_id = summary.lead_id
    if not send:
        # A policy-suppressed send is not a delivery attempt.  Do not consume the
        # conversation claim, or turning the kill switch off later would silently lose
        # this owner's card as a duplicate.
        return FinalizeResult(claimed=False, sent=False, kind=kind)
    scheduled = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
    payload = summary.model_dump()
    text = render_conversation_summary(
        {key: value if isinstance(value, str) else None for key, value in payload.items()}
    )
    # The inbox records the local business event; it is not a transport claim.
    inbox_already_present = store.has_owner_notification(kind=kind, lead_id=lead_id)
    store.upsert_owner_notification(kind=kind, lead_id=lead_id, scheduled_at=scheduled)
    token = settings.telegram_bot_token.strip()
    recipients = tuple(sorted(settings.telegram_owner_user_id_set()))
    if not token or not recipients or not text.strip():
        # No delivery claim exists without a valid transport attempt.  The inbox row
        # still makes a repeated no-config scan quiet; a later configured replay can
        # proceed because recipient claims are still absent.
        return FinalizeResult(
            claimed=not inbox_already_present,
            sent=False,
            duplicate=inbox_already_present,
            kind=kind,
        )
    # The old ledger has no recipient or outcome data.  Never fabricate a recipient
    # backfill from it: for this exact historical conversation, its durable presence
    # is conservative evidence of accepted or ambiguous delivery and protects every
    # configured recipient from a post-migration resend.
    if store.owner_notification_claimed_at(
        kind=kind, lead_id=lead_id, conversation_id=summary.conversation_id
    ) is not None:
        return FinalizeResult(claimed=False, sent=False, duplicate=True, kind=kind)
    claimed_recipients = tuple(
        recipient_id
        for recipient_id in recipients
        if store.try_claim_owner_notification_recipient(
            kind=kind,
            lead_id=lead_id,
            notification_key=summary.conversation_id,
            recipient_id=recipient_id,
            claimed_at=scheduled,
        )
    )
    if not claimed_recipients:
        return FinalizeResult(claimed=False, sent=False, duplicate=True, kind=kind)
    delivery = deliver_owner_telegram(
        text=text, settings=settings, recipient_ids=claimed_recipients
    )
    for recipient_id in delivery.rejected:
        store.release_owner_notification_recipient_claim(
            kind=kind,
            lead_id=lead_id,
            notification_key=summary.conversation_id,
            recipient_id=recipient_id,
        )
    return FinalizeResult(claimed=True, sent=bool(delivery.delivered), kind=kind)


def _or_none(value: str) -> str | None:
    """A fact we do not have is None, so the renderer omits the line entirely."""
    cleaned = (value or "").strip()
    return cleaned or None


def _read_sales(store: WebsiteFinalizationStore, lead_id: str) -> SalesState | None:
    try:
        return store.get_sales(lead_id)
    except KeyError:
        # No ladder row yet. The owner still gets the card, just with fewer lines on it.
        return None


def build_conversation_summary(
    store: WebsiteFinalizationStore,
    *,
    session_id: str,
    lead_id: str,
    next_step: str,
) -> ConversationSummary:
    """Fill the owner's card from state we already hold. No LLM call, nothing invented.

    Every field is either a flag the sales ladder recorded, a sanitised fragment of the
    visitor's own words, or a row from the meetings table. Anything that was never
    established stays None and never reaches the message.
    """
    sales = _read_sales(store, lead_id)
    turns: list[ConversationTurn] = store.list_conversation_turns(session_id)
    meeting = store.get_meeting(lead_id)
    if sales is None:
        return ConversationSummary(
            conversation_id=session_id,
            lead_id=lead_id,
            contact=_or_none(extract_contact(turns)),
            need=_or_none(extract_need(turns)),
            relevant_service=_or_none(relevant_service(turns)),
            recommended_next_step=next_step,
        )
    return ConversationSummary(
        conversation_id=session_id,
        lead_id=lead_id,
        name=_or_none(describe_name(turns, sales)),
        contact=_or_none(extract_contact(turns)),
        business=_or_none(describe_business(turns, sales)),
        need=_or_none(extract_need(turns)),
        pain=_or_none(describe_pain(sales)),
        relevant_service=_or_none(relevant_service(turns)),
        timeline=_or_none(extract_timeline(turns, sales)),
        budget=_or_none(extract_budget(turns, sales)),
        qualification=_or_none(describe_qualification(sales)),
        meeting_status=_or_none(describe_meeting(meeting, sales)),
        recommended_next_step=next_step,
    )


def qualify_and_finalize(
    store: WebsiteFinalizationStore,
    *,
    session_id: str,
    lead_id: str,
    settings: Settings,
    next_step: str,
    require_visitor_message: bool = False,
    now: datetime | None = None,
) -> FinalizeResult | None:
    """One finalization service. Skip empty sessions and WhatsApp-briefing duplicates."""
    if store.has_owner_notification(kind=KIND_WEBSITE_WHATSAPP, lead_id=lead_id):
        return None
    if require_visitor_message and not store.has_website_prospect_message(
        lead_id, session_id
    ):
        return None
    return finalize_website_conversation(
        store,
        summary=build_conversation_summary(
            store,
            session_id=session_id,
            lead_id=lead_id,
            next_step=next_step,
        ),
        settings=settings,
        now=now,
        send=not settings.kill_switch,
    )
# Inactivity traversal is intentionally owned by ClientGraph / ``mia-due-scan``.
