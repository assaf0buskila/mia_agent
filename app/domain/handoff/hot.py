"""Hot / close-ready handoff: stop selling, notify owner. Sync persist; Telegram best-effort."""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

from app.capabilities.leads import leads_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.core.config import Settings
from app.integrations.telegram_format import owner_text

_NO_HOT_LEADS_ACK = "אין לידים חמים שמחכים לתפיסה."


def _v1_hot_labels(store, ids: list[str]) -> list[str]:
    """Best-effort sales headline per v1 lead id; never the raw id (R3).

    v1 leads have no name field anywhere (`LeadRow`/`CustomerRow` are pure
    identity rows) -- `SalesState.headline` is the closest thing to a label,
    and it is not always present. One `get_sales` read per id (up to 12, capped
    by the caller) -- an N+1 not batched here; a future pass could add a
    bulk-lookup store method if this list ever needs to grow past that cap.
    Only `KeyError` (no `SalesStateRow` for that lead) and `SQLAlchemyError`
    (a transient store read failure) degrade to a positional placeholder --
    anything else is a real bug and must not be swallowed here.

    Assaf's explicit call for this surface (C9): hot leads are rendered as a
    name, never a raw internal id -- so a lead with no headline gets a
    position-based placeholder ("ליד 1", "ליד 2", ...) instead of falling back
    to `lead_id`. The position is stable within one call (`enumerate` over the
    same `ids` order the caller already capped to 12), so it still
    distinguishes several unnamed leads from each other without ever printing
    an id the owner could paste back into a tool that expects one internally.
    """
    labels: list[str] = []
    for position, lead_id in enumerate(ids, start=1):
        headline = ""
        try:
            headline = (store.get_sales(lead_id).headline or "").strip()
        except (KeyError, SQLAlchemyError):
            headline = ""
        labels.append(headline or f"ליד {position}")
    return labels


def format_hot_leads_ack(store, *, principal: Principal) -> str:
    """Union of two "hot" sources: v1's takeover-state leads and v2's unconfirmed pings.

    v1: `leads.get_recent`'s `hot_ids` -- `store.list_hot_lead_ids()`, i.e.
    `LeadRow.takeover_state == HUMAN_TAKEOVER_REQUIRED`, set by
    `store.set_takeover_state`. The auto-freeze *writer* (`apply_hot_handoff`)
    was removed 2026-09-16 per Assaf's decision that Mia must not freeze a
    conversation and hand it over automatically -- he wants hot leads listed so
    he can decide. The *read* stays: a row already in that state (production
    has one right now; see HANDOFF section 0) must keep surfacing here, and any
    future explicit `set_takeover_state` call is honoured too.

    v2: `store.list_undelivered_captured_website_leads` -- a website capture
    whose own Telegram ping never reached `confirmed`. v2 has no takeover-state
    equivalent; this is the real, already-tracked "Assaf was not reliably told"
    signal for that path instead.
    """
    result = execute_capability(
        "leads.get_recent",
        principal=principal,
        args={"limit": 12},
        handlers=leads_handlers(store),
    )
    v1_ids = [str(item) for item in (result.get("hot_ids") or []) if item]
    v2_leads = store.list_undelivered_captured_website_leads(limit=12)
    v1_labels = _v1_hot_labels(store, v1_ids)
    # Same "name, never a raw id" rule as v1 (R3): a website contact with no
    # captured name gets a position-based placeholder, distinct from v1's
    # ("פנייה" vs "ליד") so the two sources never collide in the dedupe below.
    v2_labels = [
        name or f"פנייה {position}"
        for position, (_contact_id, name) in enumerate(v2_leads, start=1)
    ]
    combined = list(dict.fromkeys([*v1_labels, *v2_labels]))
    if not combined:
        return _NO_HOT_LEADS_ACK
    listed = ", ".join(combined[:12])
    extra = "" if len(combined) <= 12 else f" (+{len(combined) - 12})"
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
        text=owner_text(brief, html=(parse_mode == "HTML")),
        settings=settings,
        parse_mode=parse_mode,
        recipient_ids=recipient_ids,
    )
