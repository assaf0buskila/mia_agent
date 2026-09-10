"""Compatibility facade over the sole authenticated owner Telegram surface."""

from __future__ import annotations

from typing import NamedTuple

from app.core.config import Settings, get_settings
from app.db.store import LeadStore
from app.domain.events import (
    Channel,
    webhook_envelope_kind,
)
from app.integrations.base import MessagePort
from app.integrations.calendar import (
    CalendarAgendaPort,
    CalendarPort,
)
from app.integrations.calendar_booking import (
    CalendarBookingPort,
)
from app.integrations.ga4 import Ga4Port
from app.integrations.gmail import GmailPort
from app.integrations.instagram_insights import (
    InstagramInsightsPort,
)
from app.integrations.linkedin import LinkedInPort
from app.integrations.research import ResearchPort
from app.integrations.search_console import SearchConsolePort
from app.integrations.seo_audit import SeoAuditPort
from app.integrations.sheets import SheetsPort


class OwnerTurnResult(NamedTuple):
    processed: bool
    sent: bool
    last_reply: str | None


def _is_authorized_owner(*, actor_id: str, owner_ids: set[str]) -> bool:
    """Fail closed unless this request matches a configured numeric owner allowlist."""
    return bool(
        owner_ids
        and actor_id.isascii()
        and actor_id.isdigit()
        and all(owner_id.isascii() and owner_id.isdigit() for owner_id in owner_ids)
        and actor_id in owner_ids
    )


async def process_owner_item(
    *,
    item: dict[str, str],
    provider: str,
    channel: Channel,
    store: LeadStore,
    port: MessagePort,
    kill_switch: bool,
    owner_ids: set[str],
    settings: Settings,
    calendar_port: CalendarPort,
    calendar_booking_port: CalendarBookingPort | None = None,
    calendar_agenda_port: CalendarAgendaPort | None,
    gmail_port: GmailPort,
    sheets_port: SheetsPort,
    instagram_insights_port: InstagramInsightsPort,
    research_port: ResearchPort,
    linkedin_port: LinkedInPort,
    search_console_port: SearchConsolePort,
    ga4_port: Ga4Port,
    seo_audit_port: SeoAuditPort,
) -> OwnerTurnResult:
    """Thin compatibility facade over the sole owner Telegram reasoning loop."""
    if not _is_authorized_owner(actor_id=item["from"], owner_ids=owner_ids):
        return OwnerTurnResult(processed=False, sent=False, last_reply=None)
    from app.surfaces.owner import run_owner_loop

    del (
        calendar_port,
        calendar_booking_port,
        calendar_agenda_port,
        instagram_insights_port,
        research_port,
        linkedin_port,
        search_console_port,
        ga4_port,
        seo_audit_port,
        sheets_port,
        gmail_port,
    )
    effective_settings = settings.model_copy(update={"kill_switch": kill_switch})
    return await run_owner_loop(
        item=item,
        store=store,
        port=port,
        settings=effective_settings,
        crm=None,
        gmail_port=None,
        owner_ids=owner_ids,
        provider=provider,
        channel=channel,
    )


async def process_owner_texts(
    *,
    provider: str,
    channel: Channel,
    items: list[dict[str, str]],
    store: LeadStore,
    port: MessagePort,
    kill_switch: bool,
    owner_ids: set[str] | None = None,
    calendar: CalendarPort | None = None,
    calendar_booking: CalendarBookingPort | None = None,
    calendar_agenda: CalendarAgendaPort | None = None,
    sheets: SheetsPort | None = None,
    instagram_insights: InstagramInsightsPort | None = None,
    research: ResearchPort | None = None,
    linkedin: LinkedInPort | None = None,
    search_console: SearchConsolePort | None = None,
    ga4: Ga4Port | None = None,
    seo_audit: SeoAuditPort | None = None,
    gmail: GmailPort | None = None,
    preclaimed_event_id: str | None = None,
    preclaimed_envelope_kind: str | None = None,
) -> dict[str, int | bool | str | None]:
    """Process an authenticated owner batch through the shared owner surface."""
    processed = 0
    duplicates = 0
    sent_count = 0
    last_reply: str | None = None
    owner_ids = owner_ids or set()
    authorized_items = [
        item
        for item in items
        if item.get("id")
        and item.get("from")
        and _is_authorized_owner(actor_id=item["from"], owner_ids=owner_ids)
    ]
    if not authorized_items:
        return {
            "processed": processed,
            "duplicates": duplicates,
            "sent": False,
            "sent_count": sent_count,
            "reply": last_reply,
        }
    settings = get_settings()
    calendar_port = calendar
    calendar_booking_port = calendar_booking
    calendar_agenda_port = calendar_agenda
    gmail_port = gmail
    sheets_port = sheets
    instagram_insights_port = instagram_insights
    research_port = research
    linkedin_port = linkedin
    search_console_port = search_console
    ga4_port = ga4
    seo_audit_port = seo_audit
    for item in authorized_items:
        preclaimed = item["id"] == preclaimed_event_id
        if preclaimed:
            claimed = store.get_webhook(provider=provider, provider_event_id=item["id"])
            if not (
                claimed is not None
                and claimed.status == "received"
                and claimed.channel == channel.value
                and claimed.envelope_kind == preclaimed_envelope_kind
            ):
                duplicates += 1
                continue
        elif not store.claim_webhook(
            provider=provider,
            provider_event_id=item["id"],
            channel=channel.value,
            envelope_kind=webhook_envelope_kind(item),
        ):
            duplicates += 1
            continue
        turn = await process_owner_item(
            item=item,
            provider=provider,
            channel=channel,
            store=store,
            port=port,
            kill_switch=kill_switch,
            owner_ids=owner_ids,
            settings=settings,
            calendar_port=calendar_port,
            calendar_booking_port=calendar_booking_port,
            calendar_agenda_port=calendar_agenda_port,
            gmail_port=gmail_port,
            sheets_port=sheets_port,
            instagram_insights_port=instagram_insights_port,
            research_port=research_port,
            linkedin_port=linkedin_port,
            search_console_port=search_console_port,
            ga4_port=ga4_port,
            seo_audit_port=seo_audit_port,
        )
        if turn.processed:
            processed += 1
        if turn.sent:
            sent_count += 1
        if turn.last_reply is not None:
            last_reply = turn.last_reply
    return {
        "processed": processed,
        "duplicates": duplicates,
        "sent": sent_count > 0,
        "sent_count": sent_count,
        "reply": last_reply,
    }
