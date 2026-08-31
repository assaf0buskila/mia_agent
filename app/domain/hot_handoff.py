"""Hot / close-ready handoff: stop selling, notify owner. Sync persist; Telegram best-effort."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

from app.capabilities.leads import leads_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.conversation_scope import TakeoverState
from app.domain.owner_notification_delivery import (
    KIND_HOT_LEAD_LEGACY,
    KIND_WEBSITE_HANDOFF_DELIVERY,
    WEBSITE_HANDOFF_DELIVERY_KINDS,
)
from app.domain.sales import SalesState

KIND_HOT_LEAD = KIND_HOT_LEAD_LEGACY

_BRIEF_MAX = 500


class OwnerNotifyAttempt(NamedTuple):
    """Result of one owner Telegram attempt.

    ``known_unreachable`` distinguishes missing Telegram configuration from a
    duplicate recipient claim, without consuming a later retry claim.
    """

    delivered: tuple[str, ...]
    attempted: bool
    known_unreachable: bool = False


def format_hot_brief(*, lead_id: str, sales: SalesState, want: str) -> str:
    lines = [
        "ליד חם — מיה עוצרת.",
        lead_id,
        f"fit={sales.fit.value}",
        f"pain={int(sales.pain_level)}",
        f"workflow={'yes' if sales.workflow_known else 'no'}",
        f"want={want[:120]}" if want else "want=handoff",
        "הבא: תפיסה אנושית.",
    ]
    return "\n".join(lines)[:_BRIEF_MAX]


def format_hot_leads_ack(store, *, principal: Principal) -> str:
    result = execute_capability(
        "leads.get_recent",
        principal=principal,
        args={"limit": 12},
        handlers=leads_handlers(store),
    )
    ids = [str(item) for item in (result.get("hot_ids") or []) if item]
    if not ids:
        return "אין לידים חמים שמחכים לתפיסה."
    listed = ", ".join(ids[:12])
    extra = "" if len(ids) <= 12 else f" (+{len(ids) - 12})"
    return f"לידים חמים: {listed}{extra}"


def notify_owners(
    *, brief: str, inbound_id: str, settings: Settings, parse_mode: str | None = "HTML"
) -> tuple[str, ...]:
    """Best-effort Telegram fan-out to every allowlisted owner id, not just the first.

    One recipient's send failing never stops the rest, and a failed send is never
    counted as delivered: the return value is exactly the chat ids that were actually
    sent to, in the same sorted order they were attempted. `inbound_id` is accepted for
    correlation parity with callers; the notify-once idempotency stays in the caller's
    `store.try_insert_owner_notification` claim, which this function does not touch — this
    fans out whatever it is handed, so the caller must not call it without a won claim.

    Telegram Bot API can return HTTP 200 with `ok: false` (or HTTP 400) for a parse_mode
    / chat-id problem. Those are not deliveries. Counting them as success is how a
    website handoff told the visitor the transfer happened while Assaf got nothing.
    """
    return _deliver_owners(
        brief=brief, inbound_id=inbound_id, settings=settings, parse_mode=parse_mode
    ).delivered


def _deliver_owners(
    *,
    brief: str,
    inbound_id: str,
    settings: Settings,
    parse_mode: str | None,
    recipient_ids: tuple[str, ...] | None = None,
):
    """Keep delivery certainty for workflow claim handling; public helper stays a tuple."""
    # Import lazily: services package exports finalization, which imports the website
    # handoff formatter and therefore this module during application startup.
    from app.services.notifications import deliver_owner_telegram

    _ = inbound_id
    return deliver_owner_telegram(
        text=brief,
        settings=settings,
        parse_mode=parse_mode,
        recipient_ids=recipient_ids,
    )


def apply_hot_handoff(
    store,
    *,
    lead_id: str,
    inbound_id: str,
    want: str,
    kill_switch: bool,
    settings: Settings,
    brief: str | None = None,
    parse_mode: str | None = None,
    notification_key: str = "",
) -> OwnerNotifyAttempt:
    """Mark HUMAN_TAKEOVER_REQUIRED, claim the notify, then Telegram. Never raise to inbound.

    Returns whether this call attempted a send and which chat ids Telegram accepted.
    Empty delivered means Assaf was not told — callers must not claim a transfer happened.

    The send is gated on a claiming insert that reports whether it actually won. The
    previous version persisted through an upsert that returns None and silently no-ops on a
    duplicate, then sent unconditionally — so every retry of the same inbound re-sent the
    brief and Assaf got the same hot lead over and over. One handoff, one message.
    """
    try:
        assert_allowed(
            RiskAction(name="hot_handoff_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return OwnerNotifyAttempt((), False)
    # Policy denial must leave the lead exactly as it was: no takeover state,
    # follow-up cancellation, inbox row, recipient claim, or transport attempt.
    store.set_takeover_state(lead_id, TakeoverState.HUMAN_TAKEOVER_REQUIRED.value)
    store.cancel_pending_follow_up(lead_id)
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
    sales = store.get_sales(lead_id)
    text = brief if brief is not None else format_hot_brief(
        lead_id=lead_id, sales=sales, want=want
    )
    mode = parse_mode if brief is not None else None
    # The owner inbox is durable handoff state, not a transport claim.  Keep it even
    # when Telegram cannot presently be attempted; recipient claims below remain
    # untouched so a later valid replay is eligible.
    store.upsert_owner_notification(
        kind=KIND_HOT_LEAD, lead_id=lead_id, scheduled_at=now_iso
    )
    if any(
        store.has_owner_notification_claim(
            kind=kind, lead_id=lead_id, conversation_id=notification_key
        )
        for kind in WEBSITE_HANDOFF_DELIVERY_KINDS
    ):
        return OwnerNotifyAttempt((), False)
    token = settings.telegram_bot_token.strip()
    recipients = tuple(sorted(settings.telegram_owner_user_id_set()))
    if not token or not recipients or not text.strip():
        accepted = store.confirmed_owner_notification_recipients(
            kind=KIND_WEBSITE_HANDOFF_DELIVERY,
            lead_id=lead_id,
            notification_key=notification_key,
        )
        if accepted:
            return OwnerNotifyAttempt(accepted, False)
        # Known no-attempt: no recipient claim is consumed, so a later valid replay
        # remains eligible.
        return OwnerNotifyAttempt((), False, True)
    claimed_recipients = tuple(
        recipient_id
        for recipient_id in recipients
        if store.try_claim_owner_notification_recipient_compatible(
            kind=KIND_WEBSITE_HANDOFF_DELIVERY,
            compatible_kinds=WEBSITE_HANDOFF_DELIVERY_KINDS,
            lead_id=lead_id,
            notification_key=notification_key,
            recipient_id=recipient_id,
            claimed_at=now_iso,
        )
    )
    if not claimed_recipients:
        return OwnerNotifyAttempt(
            store.confirmed_owner_notification_recipients(
                kind=KIND_WEBSITE_HANDOFF_DELIVERY,
                lead_id=lead_id,
                notification_key=notification_key,
            ),
            False,
        )
    # FastAPI normally commits only after the route returns. Telegram must never be
    # called while the takeover/inbox/recipient claims can still roll back, or an
    # accepted owner ping can be duplicated by the next retry.
    if not store.commit_owner_notification_delivery_state():
        return OwnerNotifyAttempt((), False, True)
    delivery = _deliver_owners(
        brief=text,
        inbound_id=inbound_id,
        settings=settings,
        parse_mode=mode,
        recipient_ids=claimed_recipients,
    )
    store.record_owner_notification_recipient_delivery_outcomes_durably(
        kind=KIND_WEBSITE_HANDOFF_DELIVERY,
        lead_id=lead_id,
        notification_key=notification_key,
        delivered_recipient_ids=delivery.delivered,
        rejected_recipient_ids=delivery.rejected,
    )
    accepted = store.confirmed_owner_notification_recipients(
        kind=KIND_WEBSITE_HANDOFF_DELIVERY,
        lead_id=lead_id,
        notification_key=notification_key,
    )
    return OwnerNotifyAttempt(accepted, True)
