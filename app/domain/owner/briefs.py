"""Owner daily operating brief (persist-only): counts from Postgres, no PII, no execute."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.brain.site_freshness import parse_http_date
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.engine_health import compute_engine_health, format_engine_health
from app.domain.followups import follow_up_due_on, local_day_bounds_utc_iso
from app.domain.funnel import compute_website_funnel, format_website_funnel
from app.domain.kpis import KPI_EVENT_TYPES, OWNER_BRIEF_EVENT_TYPES
from app.integrations.telegram_format import dotted_date

if TYPE_CHECKING:
    from app.brain.store import BrainStore
    from app.db.store import LeadStore


class DailyBriefSnapshot(BaseModel):
    brief_date: str
    leads: int = Field(ge=0)
    meetings_offered: int = Field(ge=0)
    handoffs: int = Field(ge=0)
    messages_in: int = Field(ge=0)
    follow_ups_due: int = Field(ge=0)
    meetings_booked: int = Field(ge=0)
    cancellation_requests: int = Field(ge=0)


def _count_leads_today(
    store: LeadStore, *, legacy_leads: int, occurred_from: str, occurred_to: str
) -> int:
    """Legacy ``lead_created`` events plus v2 CRM captures, never double counted.

    "Leads today" is distinct contacts/customers captured, not capture events: a
    returning visitor captured twice in the same window is one lead
    (``count_captured_website_leads`` already dedupes per contact). The two systems
    are disjoint in production today (the legacy website path has no live caller),
    but a lead that somehow exists in both -- same website session -- must still
    count once. Dedup keys on every conversation id that captured a v2 contact in
    the window (``CrmContactConversationRow``, never overwritten by a later
    session), not ``CrmContactRow.conversation_id``, which a returning visitor's
    later session does overwrite.
    """
    v2_count = store.count_captured_website_leads(
        occurred_from=occurred_from, occurred_to=occurred_to
    )
    v2_conversation_ids = store.list_captured_website_conversation_ids(
        occurred_from=occurred_from, occurred_to=occurred_to
    )
    legacy_website = store.list_legacy_website_lead_created(
        occurred_from=occurred_from, occurred_to=occurred_to
    )
    overlap = sum(
        1
        for _lead_id, conversation_id in legacy_website
        if conversation_id and conversation_id in v2_conversation_ids
    )
    return legacy_leads - overlap + v2_count


def compute_daily_brief(
    store: LeadStore,
    *,
    timezone: str,
    now: datetime | None = None,
) -> DailyBriefSnapshot | None:
    instant = now if now is not None else datetime.now(UTC)
    bounds = local_day_bounds_utc_iso(now=instant, timezone=timezone)
    if bounds is None:
        return None
    occurred_from, occurred_to = bounds
    try:
        brief_date = follow_up_due_on(now=instant, timezone=timezone, offset_days=0)
    except (ValueError, OSError, KeyError):
        return None
    counts = {
        key: store.count_canonical_events(
            event_type=key,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        for key in KPI_EVENT_TYPES
    }
    brief_counts = {
        key: store.count_canonical_events(
            event_type=key,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        for key in OWNER_BRIEF_EVENT_TYPES
    }
    leads = _count_leads_today(
        store,
        legacy_leads=counts["lead_created"],
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    return DailyBriefSnapshot(
        brief_date=brief_date,
        leads=leads,
        meetings_offered=counts["meeting_offered"],
        handoffs=counts["handoff"],
        messages_in=counts["message_in"],
        follow_ups_due=store.count_follow_ups_due_on(
            due_on=brief_date,
            status="pending",
        ),
        meetings_booked=brief_counts["meeting_booked"],
        cancellation_requests=brief_counts["meeting_cancellation_requested"],
    )


def format_daily_brief(snapshot: DailyBriefSnapshot) -> str:
    lines = [
        f"סיכום יומי {dotted_date(snapshot.brief_date)}",
        f"לידים: {snapshot.leads}",
        f"פגישות הוצעו: {snapshot.meetings_offered}",
        f"פגישות נקבעו: {snapshot.meetings_booked}",
        f"בקשות ביטול: {snapshot.cancellation_requests}",
        f"העברות: {snapshot.handoffs}",
        f"הודעות נכנסות: {snapshot.messages_in}",
        f"מעקבים לביצוע היום: {snapshot.follow_ups_due}",
    ]
    lines.append("לא ביצעתי משימות ולא שלחתי מעקבים.")
    return "\n".join(lines)


def _knowledge_staleness_line(
    brain: BrainStore, *, knowledge_sources: list[str]
) -> str | None:
    """One short Hebrew line iff the site has drifted ahead of a knowledge file.

    Silent whenever everything is fresh AND whenever the signal is merely
    "unknown" (a missing/unparsable `Last-Modified` header is not evidence of
    staleness) -- a line that shows up every day is one Assaf stops reading.
    `compare_staleness` only ever returns "stale" when both headers parsed, so
    `dotted_date` below always has a real date to render.
    """
    if not knowledge_sources:
        return None
    stale = [
        status
        for status in brain.list_knowledge_source_statuses(knowledge_sources)
        if status.site_stale == "stale"
    ]
    if not stale:
        return None
    names = ", ".join(status.source_id for status in stale)
    site_dt = parse_http_date(stale[0].site_last_modified)
    site_date = dotted_date(site_dt.date().isoformat()) if site_dt is not None else ""
    if not site_date:
        return f"קבצי הידע ({names}) ישנים מהאתר עצמו. כדאי להריץ עדכון ידע."
    return (
        f"קבצי הידע ({names}) ישנים מהאתר עצמו. "
        f"עדכון אחרון באתר: {site_date}. כדאי להריץ עדכון ידע."
    )


def apply_owner_brief_policy(
    store: LeadStore,
    *,
    snapshot: DailyBriefSnapshot,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    if demo_active or kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="owner_brief_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_owner_brief(
        brief_date=snapshot.brief_date,
        leads=snapshot.leads,
        meetings_offered=snapshot.meetings_offered,
        handoffs=snapshot.handoffs,
        messages_in=snapshot.messages_in,
        follow_ups_due=snapshot.follow_ups_due,
        meetings_booked=snapshot.meetings_booked,
        cancellation_requests=snapshot.cancellation_requests,
    )


def apply_owner_brief(
    store: LeadStore,
    *,
    timezone: str,
    kill_switch: bool,
    demo_active: bool,
    now: datetime | None = None,
    brain: BrainStore | None = None,
    knowledge_sources: list[str] | None = None,
) -> str | None:
    """Compute daily brief, optionally persist, return Hebrew scorecard or None.

    `brain`/`knowledge_sources` are optional so every existing caller (and test)
    keeps working unchanged; only a caller that has a `BrainStore` and configured
    sources on hand (the owner tool loop does, via `ToolContext`) gets the C12
    site-staleness line.
    """
    if demo_active:
        return None
    snapshot = compute_daily_brief(store, timezone=timezone, now=now)
    if snapshot is None:
        return None
    apply_owner_brief_policy(
        store,
        snapshot=snapshot,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )
    brief = format_daily_brief(snapshot)
    # "What happened today" should say what the website produced, not only event
    # counts. The funnel replaces the older one-line headline: the same lead-id-free
    # scorecard, but carrying the conversion rates the headline never had. Naming a
    # specific conversation stays the drill-down's job.
    lines = [brief]
    funnel = compute_website_funnel(store, timezone=timezone, now=now)
    if funnel is not None and funnel.has_signal():
        lines.append(format_website_funnel(funnel))
    # The engine line answers "is she actually thinking?". A canned count close to
    # the total means the real model is failing silently.
    engine = compute_engine_health(store, timezone=timezone, now=now)
    if engine is not None and engine.total_runs:
        lines.append(format_engine_health(engine))
    if brain is not None:
        staleness = _knowledge_staleness_line(
            brain, knowledge_sources=knowledge_sources or []
        )
        if staleness is not None:
            lines.append(staleness)
    return "\n".join(lines)
