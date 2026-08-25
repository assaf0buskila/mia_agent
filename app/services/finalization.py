"""One finalization workflow. Idempotent on lead + summary version."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.domain.website_handoff_brief import KIND_WEBSITE_WHATSAPP
from app.services.notifications import render_conversation_summary, send_owner_telegram

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
        self, *, kind: str, lead_id: str, scheduled_at: str
    ) -> bool: ...


class WebsiteFinalizationStore(NotificationStore, Protocol):
    def has_website_prospect_message(self, lead_id: str) -> bool: ...

    def list_inactive_website_conversations(
        self,
        *,
        cutoff_iso: str,
        skip_kinds: tuple[str, ...],
        limit: int = 50,
    ) -> list[tuple[str, str]]: ...

    def get_sales(self, lead_id: str) -> Any: ...


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
    """Claim once, then notify. Retries after a successful claim do not send again."""
    kind = kind_for(version)
    lead_id = summary.lead_id
    if store.has_owner_notification(kind=kind, lead_id=lead_id):
        return FinalizeResult(claimed=False, sent=False, duplicate=True, kind=kind)
    scheduled = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
    inserted = store.try_insert_owner_notification(
        kind=kind, lead_id=lead_id, scheduled_at=scheduled
    )
    if not inserted:
        return FinalizeResult(claimed=False, sent=False, duplicate=True, kind=kind)
    if not send:
        return FinalizeResult(claimed=True, sent=False, kind=kind)
    payload = summary.model_dump()
    text = render_conversation_summary(
        {key: value if isinstance(value, str) else None for key, value in payload.items()}
    )
    sent = send_owner_telegram(text=text, settings=settings)
    return FinalizeResult(claimed=True, sent=sent, kind=kind)


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
    if require_visitor_message and not store.has_website_prospect_message(lead_id):
        return None
    sales = store.get_sales(lead_id)
    qualification = sales.fit.value if sales is not None else None
    return finalize_website_conversation(
        store,
        summary=ConversationSummary(
            conversation_id=session_id,
            lead_id=lead_id,
            qualification=qualification,
            recommended_next_step=next_step,
        ),
        settings=settings,
        now=now,
        send=not settings.kill_switch,
    )


def scan_inactive_website_conversations(
    store: WebsiteFinalizationStore,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> int:
    minutes = settings.website_inactivity_minutes
    if minutes <= 0:
        return 0
    clock = now or datetime.now(UTC)
    cutoff = (clock.astimezone(UTC) - timedelta(minutes=minutes)).isoformat()
    rows = store.list_inactive_website_conversations(
        cutoff_iso=cutoff,
        skip_kinds=(KIND, KIND_WEBSITE_WHATSAPP),
        limit=50,
    )
    finalized = 0
    for session_id, lead_id in rows:
        result = qualify_and_finalize(
            store,
            session_id=session_id,
            lead_id=lead_id,
            settings=settings,
            next_step="inactivity",
            require_visitor_message=True,
            now=clock,
        )
        if result is not None and result.claimed:
            finalized += 1
    return finalized
