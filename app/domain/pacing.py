"""Campaign budget pacing and performance snapshots (§19.2 / §20). Never Meta writes."""
from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.campaigns import _parse_metric

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.db.store import LeadStore
    from app.integrations.meta_ads import CampaignInsights

_BUDGET_PATTERN = re.compile(r"^\d+(\.\d{1,2})?$")
_CAMPAIGN_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,32}$")
_CTR_PATTERN = re.compile(r"^\d+(\.\d+)?%?$")
_VALID_STATUSES = frozenset({"on_track", "over", "under", "uncertain"})
_PACING_STATUS_HE = {
    "on_track": "במסלול",
    "over": "מעל התקציב",
    "under": "מתחת לתקציב",
    "uncertain": "לא ודאי",
}


def parse_monthly_budget(value: str) -> float | None:
    stripped = value.strip()
    if not stripped:
        return None
    if _BUDGET_PATTERN.fullmatch(stripped) is None:
        return None
    parsed = float(stripped)
    if parsed <= 0:
        return None
    return parsed


def campaign_label(settings: Settings) -> str:
    name = settings.campaign_name.strip()
    if name and _CAMPAIGN_PATTERN.fullmatch(name):
        return name
    return "account"


def _fmt_amount(value: float) -> str:
    return f"{value:.2f}"


class PacingSnapshot(BaseModel):
    campaign: str
    monthly_budget: str
    spend: str
    expected_spend: str
    remaining: str
    projected: str
    over_under: str
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in _VALID_STATUSES:
            raise ValueError(f"unknown status: {value}")
        return value


class PerformanceSnapshot(BaseModel):
    campaign: str
    spend: str
    ctr: str
    cpc: str
    cpl: str
    qualified_cpl: str
    meetings: str
    deals: str
    revenue: str
    roas: str


def _month_bounds_utc_iso(*, now: datetime, timezone: str) -> tuple[str, str] | None:
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return None
    if now.tzinfo is None:
        local = now.replace(tzinfo=UTC).astimezone(tz)
    else:
        local = now.astimezone(tz)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if local.month == 12:
        next_month = first.replace(year=local.year + 1, month=1)
    else:
        next_month = first.replace(month=local.month + 1)
    return (
        first.astimezone(UTC).isoformat(),
        next_month.astimezone(UTC).isoformat(),
    )


def compute_pacing(
    *,
    monthly_budget: float,
    spend_mtd: float | None,
    now: datetime,
    timezone: str,
    campaign: str = "account",
) -> PacingSnapshot:
    budget_str = _fmt_amount(monthly_budget)
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return PacingSnapshot(
            campaign=campaign,
            monthly_budget=budget_str,
            spend="",
            expected_spend="",
            remaining="",
            projected="",
            over_under="",
            status="uncertain",
        )
    if now.tzinfo is None:
        local = now.replace(tzinfo=UTC).astimezone(tz)
    else:
        local = now.astimezone(tz)
    elapsed_days = local.date().day
    days_in_month = calendar.monthrange(local.year, local.month)[1]
    fraction = elapsed_days / days_in_month
    expected_str = _fmt_amount(monthly_budget * fraction)
    if spend_mtd is None:
        return PacingSnapshot(
            campaign=campaign,
            monthly_budget=budget_str,
            spend="",
            expected_spend=expected_str,
            remaining="",
            projected="",
            over_under="",
            status="uncertain",
        )
    spend_str = _fmt_amount(spend_mtd)
    remaining_str = _fmt_amount(monthly_budget - spend_mtd)
    if fraction <= 0:
        return PacingSnapshot(
            campaign=campaign,
            monthly_budget=budget_str,
            spend=spend_str,
            expected_spend=expected_str,
            remaining=remaining_str,
            projected="",
            over_under="",
            status="uncertain",
        )
    projected = spend_mtd / fraction
    projected_str = _fmt_amount(projected)
    over_under_str = _fmt_amount(projected - monthly_budget)
    if round(projected, 2) > round(monthly_budget, 2):
        status = "over"
    elif round(projected, 2) < round(monthly_budget, 2):
        status = "under"
    else:
        status = "on_track"
    return PacingSnapshot(
        campaign=campaign,
        monthly_budget=budget_str,
        spend=spend_str,
        expected_spend=expected_str,
        remaining=remaining_str,
        projected=projected_str,
        over_under=over_under_str,
        status=status,
    )


def compute_performance(
    store: LeadStore,
    *,
    insights: CampaignInsights | None,
    timezone: str,
    now: datetime | None = None,
    campaign: str = "account",
) -> PerformanceSnapshot:
    spend_parsed = _parse_metric(insights.spend if insights is not None else None)
    clicks_parsed = _parse_metric(insights.clicks if insights is not None else None)
    ctr = ""
    if insights is not None and insights.ctr:
        stripped_ctr = insights.ctr.strip()
        if _CTR_PATTERN.fullmatch(stripped_ctr):
            ctr = stripped_ctr
    spend_str = _fmt_amount(spend_parsed) if spend_parsed is not None else ""
    cpc = ""
    if spend_parsed is not None and clicks_parsed is not None and clicks_parsed > 0:
        cpc = _fmt_amount(spend_parsed / clicks_parsed)
    instant = now if now is not None else datetime.now(UTC)
    bounds = _month_bounds_utc_iso(now=instant, timezone=timezone)
    meetings = "0"
    deals = "0"
    cpl = ""
    if bounds is not None:
        occurred_from, occurred_to = bounds
        lead_count = store.count_canonical_events_in_range(
            event_type="lead_created",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        meetings_count = store.count_canonical_events_in_range(
            event_type="meeting_offered",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        deals_count = store.count_canonical_events_in_range(
            event_type="deal_updated",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
        )
        meetings = str(meetings_count)
        deals = str(deals_count)
        if spend_parsed is not None and lead_count > 0:
            cpl = _fmt_amount(spend_parsed / lead_count)
    return PerformanceSnapshot(
        campaign=campaign,
        spend=spend_str,
        ctr=ctr,
        cpc=cpc,
        cpl=cpl,
        qualified_cpl="",
        meetings=meetings,
        deals=deals,
        revenue="",
        roas="",
    )


def format_pacing_line(snapshot: PacingSnapshot) -> str:
    label = _PACING_STATUS_HE.get(snapshot.status, snapshot.status)
    return f"קצב: {label}"


def apply_pacing_policy(
    store,
    *,
    snapshot: PacingSnapshot,
    kill_switch: bool,
    scope: str = "account",
) -> None:
    if kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="campaign_pacing_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_campaign_pacing(
        scope=scope,
        campaign=snapshot.campaign,
        monthly_budget=snapshot.monthly_budget,
        spend=snapshot.spend,
        expected_spend=snapshot.expected_spend,
        remaining=snapshot.remaining,
        projected=snapshot.projected,
        over_under=snapshot.over_under,
        status=snapshot.status,
    )


def apply_performance_policy(
    store,
    *,
    snapshot: PerformanceSnapshot,
    kill_switch: bool,
    scope: str = "account",
) -> None:
    if kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="campaign_pacing_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_campaign_performance(
        scope=scope,
        campaign=snapshot.campaign,
        spend=snapshot.spend,
        ctr=snapshot.ctr,
        cpc=snapshot.cpc,
        cpl=snapshot.cpl,
        qualified_cpl=snapshot.qualified_cpl,
        meetings=snapshot.meetings,
        deals=snapshot.deals,
        revenue=snapshot.revenue,
        roas=snapshot.roas,
    )
