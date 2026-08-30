"""Owner daily operating brief (persist-only): counts from Postgres, no PII, no execute."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.engine_health import compute_engine_health, format_engine_health
from app.domain.followups import follow_up_due_on, local_day_bounds_utc_iso
from app.domain.funnel import compute_website_funnel, format_website_funnel
from app.domain.kpis import KPI_EVENT_TYPES, OWNER_BRIEF_EVENT_TYPES

if TYPE_CHECKING:
    from app.db.store import LeadStore

_DATE_DISPLAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class DailyBriefSnapshot(BaseModel):
    brief_date: str
    leads: int = Field(ge=0)
    meetings_offered: int = Field(ge=0)
    handoffs: int = Field(ge=0)
    messages_in: int = Field(ge=0)
    follow_ups_due: int = Field(ge=0)
    meetings_booked: int = Field(ge=0)
    cancellation_requests: int = Field(ge=0)


def _format_brief_date(brief_date: str) -> str:
    match = _DATE_DISPLAY.fullmatch(brief_date)
    if match is None:
        return brief_date
    year, month, day = match.groups()
    return f"{day}.{month}.{year}"


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
    return DailyBriefSnapshot(
        brief_date=brief_date,
        leads=counts["lead_created"],
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
        f"סיכום יומי {_format_brief_date(snapshot.brief_date)}",
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
) -> str | None:
    """Compute daily brief, optionally persist, return Hebrew scorecard or None."""
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
    return "\n".join(lines)
