"""Owner weekly operating brief (persist-only): counts from Postgres, no PII, no execute."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.kpis import compute_weekly_kpi, week_bounds_utc_iso

if TYPE_CHECKING:
    from app.db.store import LeadStore

_VALID_PACING = frozenset({"on_track", "over", "under", "uncertain", ""})
_VALID_PRELAUNCH = frozenset({"", "ready", "not_ready"})
_PACING_STATUS_HE = {
    "on_track": "במסלול",
    "over": "מעל התקציב",
    "under": "מתחת לתקציב",
    "uncertain": "לא ודאי",
}
_DATE_DISPLAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class WeeklyBriefSnapshot(BaseModel):
    week_start: str
    leads: int = Field(ge=0)
    meetings_offered: int = Field(ge=0)
    handoffs: int = Field(ge=0)
    messages_in: int = Field(ge=0)
    follow_ups_pending: int = Field(ge=0)
    meetings_booked: int = Field(ge=0)
    cancellation_requests: int = Field(ge=0)
    pacing_status: str = ""
    prelaunch_ready: str = ""

    @field_validator("pacing_status")
    @classmethod
    def _validate_pacing(cls, value: str) -> str:
        if value not in _VALID_PACING:
            raise ValueError(f"invalid pacing_status: {value}")
        return value

    @field_validator("prelaunch_ready")
    @classmethod
    def _validate_prelaunch(cls, value: str) -> str:
        if value not in _VALID_PRELAUNCH:
            raise ValueError(f"invalid prelaunch_ready: {value}")
        return value


def _format_week_start(week_start: str) -> str:
    match = _DATE_DISPLAY.fullmatch(week_start)
    if match is None:
        return week_start
    year, month, day = match.groups()
    return f"{day}.{month}.{year}"


def compute_weekly_brief(
    store: LeadStore,
    *,
    timezone: str,
    now: datetime | None = None,
) -> WeeklyBriefSnapshot | None:
    kpi = compute_weekly_kpi(store, timezone=timezone, now=now)
    if kpi is None:
        return None
    pacing_status = ""
    pacing_row = store.get_campaign_pacing()
    if pacing_row is not None and pacing_row.status in _VALID_PACING - {""}:
        pacing_status = pacing_row.status
    prelaunch_ready = ""
    prelaunch_row = store.get_campaign_prelaunch()
    if prelaunch_row is not None:
        prelaunch_ready = "ready" if prelaunch_row.ready else "not_ready"
    bounds = week_bounds_utc_iso(week_start=kpi.week_start, timezone=timezone)
    meetings_booked = 0
    cancellation_requests = 0
    if bounds is not None:
        occurred_from, occurred_to = bounds
        meetings_booked = store.count_canonical_events(
            event_type="meeting_booked",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        cancellation_requests = store.count_canonical_events(
            event_type="meeting_cancellation_requested",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
    return WeeklyBriefSnapshot(
        week_start=kpi.week_start,
        leads=kpi.leads,
        meetings_offered=kpi.meetings_offered,
        handoffs=kpi.handoffs,
        messages_in=kpi.messages_in,
        follow_ups_pending=kpi.follow_ups_pending,
        meetings_booked=meetings_booked,
        cancellation_requests=cancellation_requests,
        pacing_status=pacing_status,
        prelaunch_ready=prelaunch_ready,
    )


def format_weekly_brief(snapshot: WeeklyBriefSnapshot) -> str:
    lines = [
        f"סיכום שבועי {_format_week_start(snapshot.week_start)}",
        f"לידים: {snapshot.leads}",
        f"פגישות הוצעו: {snapshot.meetings_offered}",
        f"פגישות נקבעו: {snapshot.meetings_booked}",
        f"בקשות ביטול: {snapshot.cancellation_requests}",
        f"העברות: {snapshot.handoffs}",
        f"הודעות נכנסות: {snapshot.messages_in}",
        f"מעקבים פתוחים: {snapshot.follow_ups_pending}",
    ]
    if snapshot.pacing_status:
        label = _PACING_STATUS_HE.get(snapshot.pacing_status, snapshot.pacing_status)
        lines.append(f"קצב: {label}")
    if snapshot.prelaunch_ready == "ready":
        lines.append("שער טרום-השקה: מוכן")
    elif snapshot.prelaunch_ready == "not_ready":
        lines.append("שער טרום-השקה: לא מוכן")
    lines.append("לא ביצעתי משימות ולא שלחתי מעקבים.")
    return "\n".join(lines)


def apply_owner_weekly_policy(
    store: LeadStore,
    *,
    snapshot: WeeklyBriefSnapshot,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    if demo_active or kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="owner_weekly_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_owner_weekly(
        week_start=snapshot.week_start,
        leads=snapshot.leads,
        meetings_offered=snapshot.meetings_offered,
        handoffs=snapshot.handoffs,
        messages_in=snapshot.messages_in,
        follow_ups_pending=snapshot.follow_ups_pending,
        meetings_booked=snapshot.meetings_booked,
        cancellation_requests=snapshot.cancellation_requests,
        pacing_status=snapshot.pacing_status,
        prelaunch_ready=snapshot.prelaunch_ready,
    )


def apply_owner_weekly(
    store: LeadStore,
    *,
    timezone: str,
    kill_switch: bool,
    demo_active: bool,
    now: datetime | None = None,
) -> str | None:
    """Compute weekly brief, optionally persist, return Hebrew scorecard or None."""
    if demo_active:
        return None
    snapshot = compute_weekly_brief(store, timezone=timezone, now=now)
    if snapshot is None:
        return None
    apply_owner_weekly_policy(
        store,
        snapshot=snapshot,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )
    return format_weekly_brief(snapshot)
