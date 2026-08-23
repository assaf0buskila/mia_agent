"""Campaign insights analysis and persist-only recommendations (§20.2). Never writes Meta."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.events import build_campaign_recommendation_event

KIND_WATCH = "watch"
KIND_INVESTIGATE = "investigate"
KIND_UNCERTAIN = "uncertain"
ANOMALY_NONE = "none"
ANOMALY_SPEND_WITHOUT_CLICKS = "spend_without_clicks"
ANOMALY_INCOMPLETE = "incomplete_metrics"
ANOMALY_SPEND_UP_CLICKS_DOWN = "spend_up_clicks_down"
ANOMALY_SPEND_UP_CLICKS_DOWN_30D = "spend_up_clicks_down_30d"
ANOMALY_SPEND_WITHOUT_LEADS = "spend_without_leads"
ANOMALY_CPL_SPIKE = "cpl_spike"
ANOMALY_CREATIVE_FATIGUE = "creative_fatigue"
ANOMALY_WEBSITE_FUNNEL_DROP = "website_funnel_drop"

_VALID_KINDS = frozenset({KIND_WATCH, KIND_INVESTIGATE, KIND_UNCERTAIN})
_VALID_ANOMALIES = frozenset({
    ANOMALY_NONE,
    ANOMALY_SPEND_WITHOUT_CLICKS,
    ANOMALY_INCOMPLETE,
    ANOMALY_SPEND_UP_CLICKS_DOWN,
    ANOMALY_SPEND_UP_CLICKS_DOWN_30D,
    ANOMALY_SPEND_WITHOUT_LEADS,
    ANOMALY_CPL_SPIKE,
    ANOMALY_CREATIVE_FATIGUE,
    ANOMALY_WEBSITE_FUNNEL_DROP,
})
_VALID_COMPARE_WINDOWS = frozenset({"7d", "30d"})

RECOMMENDATION_LINES = {
    "investigate": (
        "יש הוצאה בלי קליקים בדוח — ממליצה לבדוק מה קורה, בלי שינוי תקציב עד שנבין."
    ),
    "uncertain": "חסרים מדדים בדוח — אי אפשר להמליץ בביטחון; נמשיך לעקוב.",
    "watch": "הנתונים נראים תקינים — ממליצה להמשיך לעקוב, בלי שינוי תקציב.",
}


class CampaignRecommendation(BaseModel):
    kind: str
    anomaly: str

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in _VALID_KINDS:
            raise ValueError(f"unknown kind: {value}")
        return value

    @field_validator("anomaly")
    @classmethod
    def _validate_anomaly(cls, value: str) -> str:
        if value not in _VALID_ANOMALIES:
            raise ValueError(f"unknown anomaly: {value}")
        return value


def last_7d_event_bounds(*, now: datetime, timezone: str) -> tuple[str, str] | None:
    """Seven inclusive local-calendar days including today as UTC ISO bounds (exclusive end)."""
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return None
    if now.tzinfo is None:
        local = now.replace(tzinfo=UTC).astimezone(tz)
    else:
        local = now.astimezone(tz)
    first_today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = first_today - timedelta(days=6)
    end_local = first_today + timedelta(days=1)
    return (
        start_local.astimezone(UTC).isoformat(),
        end_local.astimezone(UTC).isoformat(),
    )


def previous_7d_event_bounds(*, now: datetime, timezone: str) -> tuple[str, str] | None:
    """Previous 7 local days before last-7 window (D-14 midnight → D-7 midnight, exclusive end)."""
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return None
    if now.tzinfo is None:
        local = now.replace(tzinfo=UTC).astimezone(tz)
    else:
        local = now.astimezone(tz)
    first_today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = first_today - timedelta(days=14)
    end_local = first_today - timedelta(days=7)
    return (
        start_local.astimezone(UTC).isoformat(),
        end_local.astimezone(UTC).isoformat(),
    )


def previous_7d_time_range(*, now: datetime, timezone: str) -> dict[str, str] | None:
    """Previous 7 inclusive local-calendar days before the last-7 window (since=D-14, until=D-8)."""
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_date = now.astimezone(tz).date()
    since = local_date - timedelta(days=14)
    until = local_date - timedelta(days=8)
    return {"since": since.isoformat(), "until": until.isoformat()}


def baseline_7d_time_range(*, now: datetime, timezone: str) -> dict[str, str] | None:
    """Previous 7 completed local-calendar days (since=D-7, until=D-1 inclusive Meta dates)."""
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_date = now.astimezone(tz).date()
    since = local_date - timedelta(days=7)
    until = local_date - timedelta(days=1)
    return {"since": since.isoformat(), "until": until.isoformat()}


def previous_30d_time_range(*, now: datetime, timezone: str) -> dict[str, str] | None:
    """Previous 30 local days before last-30 window (since=D-60, until=D-31 inclusive)."""
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_date = now.astimezone(tz).date()
    since = local_date - timedelta(days=60)
    until = local_date - timedelta(days=31)
    return {"since": since.isoformat(), "until": until.isoformat()}


def _parse_metric(value: str | None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", stripped.replace(",", ""))
    if not cleaned or cleaned in (".", "-", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_lead_count(lead_count: int | None) -> int | None:
    if lead_count is None or lead_count < 0:
        return None
    return lead_count


def analyze_insights(
    *,
    spend: str | None = None,
    impressions: str | None = None,
    clicks: str | None = None,
    ctr: str | None = None,
    frequency: str | None = None,
    previous_spend: str | None = None,
    previous_clicks: str | None = None,
    previous_frequency: str | None = None,
    previous_ctr: str | None = None,
    lead_count: int | None = None,
    previous_lead_count: int | None = None,
    opened_count: int | None = None,
    conversation_count: int | None = None,
    previous_opened_count: int | None = None,
    previous_conversation_count: int | None = None,
    compare_window: str = "7d",
) -> CampaignRecommendation:
    if compare_window not in _VALID_COMPARE_WINDOWS:
        raise ValueError(f"unknown compare_window: {compare_window}")
    parsed_spend = _parse_metric(spend)
    parsed_impressions = _parse_metric(impressions)
    parsed_clicks = _parse_metric(clicks)
    parsed_ctr = _parse_metric(ctr)

    if (
        parsed_spend is None
        and parsed_impressions is None
        and parsed_clicks is None
        and parsed_ctr is None
    ):
        return CampaignRecommendation(kind=KIND_UNCERTAIN, anomaly=ANOMALY_INCOMPLETE)

    if parsed_spend is not None and parsed_spend > 0 and (
        parsed_clicks is None or parsed_clicks == 0
    ):
        return CampaignRecommendation(
            kind=KIND_INVESTIGATE,
            anomaly=ANOMALY_SPEND_WITHOUT_CLICKS,
        )

    parsed_previous_spend = _parse_metric(previous_spend)
    parsed_previous_clicks = _parse_metric(previous_clicks)
    if (
        parsed_spend is not None
        and parsed_clicks is not None
        and parsed_previous_spend is not None
        and parsed_previous_clicks is not None
        and parsed_spend > parsed_previous_spend
        and parsed_clicks < parsed_previous_clicks
    ):
        anomaly = (
            ANOMALY_SPEND_UP_CLICKS_DOWN_30D
            if compare_window == "30d"
            else ANOMALY_SPEND_UP_CLICKS_DOWN
        )
        return CampaignRecommendation(kind=KIND_INVESTIGATE, anomaly=anomaly)

    normalized_lead_count = _normalize_lead_count(lead_count)
    if (
        parsed_spend is not None
        and parsed_spend > 0
        and normalized_lead_count == 0
    ):
        return CampaignRecommendation(
            kind=KIND_INVESTIGATE,
            anomaly=ANOMALY_SPEND_WITHOUT_LEADS,
        )

    normalized_previous_lead_count = _normalize_lead_count(previous_lead_count)
    if (
        parsed_spend is not None
        and parsed_spend > 0
        and normalized_lead_count is not None
        and normalized_lead_count > 0
        and parsed_previous_spend is not None
        and parsed_previous_spend > 0
        and normalized_previous_lead_count is not None
        and normalized_previous_lead_count > 0
        and (parsed_spend / normalized_lead_count)
        > (parsed_previous_spend / normalized_previous_lead_count)
    ):
        return CampaignRecommendation(
            kind=KIND_INVESTIGATE,
            anomaly=ANOMALY_CPL_SPIKE,
        )

    parsed_frequency = _parse_metric(frequency)
    parsed_previous_frequency = _parse_metric(previous_frequency)
    parsed_previous_ctr = _parse_metric(previous_ctr)
    if (
        compare_window == "7d"
        and parsed_frequency is not None
        and parsed_previous_frequency is not None
        and parsed_ctr is not None
        and parsed_previous_ctr is not None
        and parsed_frequency > parsed_previous_frequency
        and parsed_ctr < parsed_previous_ctr
    ):
        return CampaignRecommendation(
            kind=KIND_INVESTIGATE,
            anomaly=ANOMALY_CREATIVE_FATIGUE,
        )

    normalized_opened = _normalize_lead_count(opened_count)
    normalized_conversation = _normalize_lead_count(conversation_count)
    normalized_previous_opened = _normalize_lead_count(previous_opened_count)
    normalized_previous_conversation = _normalize_lead_count(previous_conversation_count)
    if compare_window == "7d":
        if (
            normalized_opened is not None
            and normalized_opened > 0
            and normalized_conversation is not None
            and normalized_conversation == 0
        ):
            return CampaignRecommendation(
                kind=KIND_INVESTIGATE,
                anomaly=ANOMALY_WEBSITE_FUNNEL_DROP,
            )
        if (
            normalized_opened is not None
            and normalized_previous_opened is not None
            and normalized_conversation is not None
            and normalized_previous_conversation is not None
            and normalized_opened > normalized_previous_opened
            and normalized_conversation < normalized_previous_conversation
        ):
            return CampaignRecommendation(
                kind=KIND_INVESTIGATE,
                anomaly=ANOMALY_WEBSITE_FUNNEL_DROP,
            )

    return CampaignRecommendation(kind=KIND_WATCH, anomaly=ANOMALY_NONE)


def format_recommendation_line(rec: CampaignRecommendation) -> str:
    if rec.anomaly == ANOMALY_SPEND_WITHOUT_LEADS:
        return (
            "יש הוצאה בלי לידים בדוח — ממליצה לבדוק את המסלול, בלי שינוי תקציב."
        )
    if rec.anomaly == ANOMALY_CPL_SPIKE:
        return (
            "עלות ליד עלתה מול שבעת הימים הקודמים — ממליצה לבדוק, בלי שינוי תקציב."
        )
    if rec.anomaly == ANOMALY_CREATIVE_FATIGUE:
        return (
            "תדירות עלתה ו-CTR ירד מול שבעת הימים הקודמים — "
            "ממליצה לבדוק קריאייטיב, בלי שינוי תקציב."
        )
    if rec.anomaly == ANOMALY_WEBSITE_FUNNEL_DROP:
        return (
            "זוהתה ירידה במשפך האתר — "
            "ממליצה לבדוק את המסלול, בלי שינוי תקציב."
        )
    if rec.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN_30D:
        return (
            "הוצאה עלתה וקליקים ירדו מול שלושים הימים הקודמים — ממליצה לבדוק, בלי שינוי תקציב."
        )
    if rec.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN:
        return (
            "הוצאה עלתה וקליקים ירדו מול שבעת הימים הקודמים — ממליצה לבדוק, בלי שינוי תקציב."
        )
    return RECOMMENDATION_LINES[rec.kind]


def apply_campaign_recommendation_policy(
    store,
    *,
    rec: CampaignRecommendation,
    kill_switch: bool,
) -> None:
    if kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="campaign_recommendation_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    event = build_campaign_recommendation_event(kind=rec.kind, anomaly=rec.anomaly)
    store.upsert_campaign_recommendation(
        scope="account",
        kind=rec.kind,
        anomaly=rec.anomaly,
        payload_json=json.dumps(event.payload),
    )
    store.save_canonical_event(
        provider="meta",
        event=event,
    )
