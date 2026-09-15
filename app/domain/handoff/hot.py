"""Hot / close-ready handoff: stop selling, notify owner. Sync persist; Telegram best-effort."""

from __future__ import annotations

from app.capabilities.types import Principal
from app.core.config import Settings

_NO_HOT_LEADS_ACK = "אין לידים חמים שמחכים לתפיסה."


def format_hot_leads_ack(store, *, principal: Principal) -> str:
    """v2-only: an unconfirmed Telegram ping on a website capture.

    v1's sales-workflow "hot" state (fit/pain/takeover, set only by the now-removed
    ``apply_hot_handoff``) is retired -- nothing in production ever set
    ``LeadRow.takeover_state`` outside that function, and it had no production
    caller (2026-09-16; see HANDOFF section 0 for the evidence). v2 has no
    equivalent workflow state either; an unconfirmed Telegram ping is a real signal
    already tracked on the outbox instead: it means Assaf has not reliably been
    told about this capture yet.
    """
    del principal  # kept for call-site compatibility; the v2 source needs no capability check
    leads = list(dict.fromkeys(store.list_undelivered_captured_website_leads(limit=12)))
    if not leads:
        return _NO_HOT_LEADS_ACK
    labels = [f"{name} ({contact_id})" if name else contact_id for contact_id, name in leads[:12]]
    listed = ", ".join(labels)
    extra = "" if len(leads) <= 12 else f" (+{len(leads) - 12})"
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
