"""Meta Ads campaign insights read port.

Production adapter: Composio ``METAADS`` toolkit version ``20260731_00``,
pin ``METAADS_GET_INSIGHTS`` only when ``MIA_COMPOSIO_API_KEY``,
``MIA_COMPOSIO_USER_ID``, and ``MIA_META_ADS_ACCOUNT_ID`` are set.
Assaf-owned Meta credentials; managed app **No**.
Never create/update/delete/pause campaigns this slice.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from time import perf_counter
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.ai_runs import elapsed_ms
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import AdapterHttpError, ToolOutcome

COMPOSIO_METAADS_VERSION = "20260731_00"
COMPOSIO_GET_INSIGHTS_TOOL = "METAADS_GET_INSIGHTS"
_COMPOSIO_EXECUTE_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_GET_INSIGHTS_TOOL}"
)
INSIGHT_FIELDS = ["spend", "impressions", "clicks", "ctr", "frequency", "campaign_name"]
_ALLOWED_DATE_PRESETS = frozenset({"last_7d", "last_30d", "this_month", "today"})
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CampaignInsights(BaseModel):
    campaign_name: str = ""
    spend: str | None = None
    impressions: str | None = None
    clicks: str | None = None
    ctr: str | None = None
    frequency: str | None = None
    date_preset: str = "last_7d"


class MetaAdsPort(Protocol):
    def get_insights(
        self,
        *,
        date_preset: str | None = "last_7d",
        time_range: dict[str, str] | None = None,
    ) -> CampaignInsights | None: ...


class DisabledMetaAdsPort:
    def get_insights(
        self,
        *,
        date_preset: str | None = "last_7d",
        time_range: dict[str, str] | None = None,
    ) -> CampaignInsights | None:
        del date_preset, time_range
        return None


class ComposioMetaAdsPort:
    """Live Composio execute adapter for METAADS_GET_INSIGHTS. Raises AdapterHttpError on HTTP."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        account_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._account_id = _normalize_account_id(account_id)
        self._client = client

    def get_insights(
        self,
        *,
        date_preset: str | None = "last_7d",
        time_range: dict[str, str] | None = None,
    ) -> CampaignInsights | None:
        if date_preset is not None and time_range is not None:
            return None
        if date_preset is None and time_range is None:
            return None
        if time_range is not None:
            if not _validate_time_range(time_range):
                return None
            arguments: dict[str, Any] = {
                "object_id": self._account_id,
                "level": "account",
                "time_range": {
                    "since": time_range["since"],
                    "until": time_range["until"],
                },
                "fields": INSIGHT_FIELDS,
                "limit": 1,
            }
            preset_label = ""
        else:
            if date_preset not in _ALLOWED_DATE_PRESETS:
                return None
            arguments = {
                "object_id": self._account_id,
                "level": "account",
                "date_preset": date_preset,
                "fields": INSIGHT_FIELDS,
                "limit": 1,
            }
            preset_label = date_preset
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_METAADS_VERSION,
            "arguments": arguments,
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _COMPOSIO_EXECUTE_URL,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        _COMPOSIO_EXECUTE_URL,
                        json=payload,
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("successful") is not True:
                return None
            data = body.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    return None
            row = _extract_insights_row(data)
            if row is None:
                return None
            return _map_row_to_insights(row, date_preset=preset_label)
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return None


class FakeMetaAdsPort:
    """Test double. Returns configured snapshot or None."""

    def __init__(
        self,
        snapshot: CampaignInsights | None = None,
        *,
        mtd_snapshot: CampaignInsights | None = None,
        previous_snapshot: CampaignInsights | None = None,
        snapshot_30d: CampaignInsights | None = None,
        previous_30d_snapshot: CampaignInsights | None = None,
        today_snapshot: CampaignInsights | None = None,
        time_range_snapshots: dict[tuple[str, str], CampaignInsights] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._mtd_snapshot = mtd_snapshot
        self._previous_snapshot = previous_snapshot
        self._snapshot_30d = snapshot_30d
        self._previous_30d_snapshot = previous_30d_snapshot
        self._today_snapshot = today_snapshot
        self._time_range_snapshots = time_range_snapshots or {}
        self.calls: list[dict[str, object]] = []

    def get_insights(
        self,
        *,
        date_preset: str | None = "last_7d",
        time_range: dict[str, str] | None = None,
    ) -> CampaignInsights | None:
        self.calls.append({"date_preset": date_preset, "time_range": time_range})
        if time_range is not None:
            since = time_range.get("since")
            until = time_range.get("until")
            if isinstance(since, str) and isinstance(until, str):
                explicit = self._time_range_snapshots.get((since, until))
                if explicit is not None:
                    return explicit
            span = _time_range_span(time_range)
            if span is not None and span <= 7:
                return self._previous_snapshot
            return self._previous_30d_snapshot
        if date_preset == "this_month":
            return self._mtd_snapshot
        if date_preset == "last_30d":
            return self._snapshot_30d
        if date_preset == "today":
            return self._today_snapshot
        if date_preset == "last_7d":
            return self._snapshot
        return None


def _time_range_span(time_range: dict[str, str]) -> int | None:
    since = time_range.get("since")
    until = time_range.get("until")
    if not isinstance(since, str) or not isinstance(until, str):
        return None
    if _DATE_PATTERN.fullmatch(since) is None or _DATE_PATTERN.fullmatch(until) is None:
        return None
    try:
        since_date = date.fromisoformat(since)
        until_date = date.fromisoformat(until)
    except ValueError:
        return None
    if since_date > until_date:
        return None
    return (until_date - since_date).days + 1


def _validate_time_range(time_range: dict[str, str]) -> bool:
    span = _time_range_span(time_range)
    return span is not None and 1 <= span <= 31


def _normalize_account_id(account_id: str) -> str:
    stripped = account_id.strip()
    if stripped.startswith("act_"):
        return stripped
    return f"act_{stripped}"


def _metric_value(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            return None
        return raw
    if isinstance(raw, (int, float)):
        return str(raw)
    return None


def _extract_insights_row(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, list) and nested:
            first = nested[0]
            if isinstance(first, dict):
                return first
        if any(
            key in data
            for key in ("spend", "impressions", "clicks", "ctr", "frequency", "campaign_name")
        ):
            return data
        return None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first
    return None


def _map_row_to_insights(row: dict[str, Any], *, date_preset: str) -> CampaignInsights:
    kwargs: dict[str, str] = {"date_preset": date_preset}
    for field in ("spend", "impressions", "clicks", "ctr", "frequency"):
        value = _metric_value(row.get(field))
        if value is not None:
            kwargs[field] = value
    campaign_name = _metric_value(row.get("campaign_name"))
    if campaign_name is not None:
        kwargs["campaign_name"] = campaign_name
    return CampaignInsights(**kwargs)


def _preset_label(date_preset: str) -> str:
    if date_preset.startswith("last_"):
        return date_preset[5:]
    return date_preset


def format_insights_line(insights: CampaignInsights) -> str:
    """One-line snapshot. Missing metrics are omitted, never zero-filled."""
    parts: list[str] = []
    if insights.spend is not None:
        parts.append(f"spend {insights.spend}")
    if insights.impressions is not None:
        parts.append(f"impr {insights.impressions}")
    if insights.clicks is not None:
        parts.append(f"clicks {insights.clicks}")
    if insights.ctr is not None:
        parts.append(f"CTR {insights.ctr}")
    if insights.frequency is not None:
        parts.append(f"freq {insights.frequency}")
    if not parts:
        return ""
    return f"{_preset_label(insights.date_preset)} אחרונים: {', '.join(parts)}."


def _compact_metric(value: float) -> str:
    formatted = f"{value:.2f}".rstrip("0").rstrip(".")
    return formatted if formatted else "0"


def format_today_baseline_line(
    today: CampaignInsights,
    baseline: CampaignInsights,
) -> str:
    """Read-only partial-day comparison. Missing pairs omitted; frequency excluded."""
    from app.domain.campaigns import _parse_metric

    pairs: list[str] = []
    today_spend = _parse_metric(today.spend)
    baseline_spend = _parse_metric(baseline.spend)
    if today_spend is not None and baseline_spend is not None:
        pairs.append(
            f"spend {_compact_metric(today_spend)} / {_compact_metric(baseline_spend / 7)}"
        )
    today_impr = _parse_metric(today.impressions)
    baseline_impr = _parse_metric(baseline.impressions)
    if today_impr is not None and baseline_impr is not None:
        pairs.append(
            f"impr {_compact_metric(today_impr)} / {_compact_metric(baseline_impr / 7)}"
        )
    today_clicks = _parse_metric(today.clicks)
    baseline_clicks = _parse_metric(baseline.clicks)
    if today_clicks is not None and baseline_clicks is not None:
        pairs.append(
            f"clicks {_compact_metric(today_clicks)} / {_compact_metric(baseline_clicks / 7)}"
        )
    today_ctr = _parse_metric(today.ctr)
    baseline_ctr = _parse_metric(baseline.ctr)
    if today_ctr is not None and baseline_ctr is not None:
        pairs.append(
            f"CTR {_compact_metric(today_ctr)} / {_compact_metric(baseline_ctr)}"
        )
    if not pairs:
        return ""
    return (
        "היום עד עכשיו מול ממוצע שבעה ימים מלאים: "
        + "; ".join(pairs)
        + "."
    )


def _try_insights(
    port: MetaAdsPort,
    *,
    date_preset: str | None = "last_7d",
    time_range: dict[str, str] | None = None,
) -> CampaignInsights | None:
    try:
        return port.get_insights(date_preset=date_preset, time_range=time_range)
    except AdapterHttpError:
        return None


def _campaign_metrics_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "campaign_metrics",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="meta_ads_insights",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def campaign_budget_outcome(
    *,
    present: bool,
    now: datetime,
    latency_ms: int = 0,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "campaign_budget_status",
        present=present,
        fetched_at=now,
        now=now,
    )
    base_status = "ok" if present else "empty"
    return ToolOutcome(
        tool="meta_ads_pacing",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=1 if present else 0,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def website_session_events_outcome(*, present: bool, now: datetime) -> ToolOutcome:
    base_status = "ok" if present else "empty"
    stamp = stamp_freshness(
        "website_session_events",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="website_session_events",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=1 if present else 0,
        freshness=stamp.status,
    )


def enrich_analytics_ack(
    ack: str,
    port: MetaAdsPort,
    kill_switch: bool,
    store=None,
    settings=None,
    sheets=None,
    extra_outcomes: list[ToolOutcome] | None = None,
    inbound_id: str = "",
) -> tuple[str, ToolOutcome]:
    """Append insights snapshot to owner analytics ack. Never raises; never writes ads."""
    try:
        assert_allowed(
            RiskAction(name="meta_ads_read", risk=RiskLevel.R0_READ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return ack, ToolOutcome(
            tool="meta_ads_insights",
            status="denied",
            result_count=0,
            freshness="",
        )

    insights_latency = 0
    try:
        from app.domain.campaigns import (
            ANOMALY_CPL_SPIKE,
            ANOMALY_CREATIVE_FATIGUE,
            ANOMALY_SPEND_UP_CLICKS_DOWN_30D,
            ANOMALY_SPEND_WITHOUT_LEADS,
            ANOMALY_WEBSITE_FUNNEL_DROP,
            KIND_WATCH,
            analyze_insights,
            apply_campaign_recommendation_policy,
            baseline_7d_time_range,
            format_recommendation_line,
            last_7d_event_bounds,
            previous_7d_event_bounds,
            previous_7d_time_range,
            previous_30d_time_range,
        )

        try:
            now = datetime.now(UTC)
            started = perf_counter()
            insights = port.get_insights()
            insights_latency = elapsed_ms(started)
        except AdapterHttpError as exc:
            return ack, _campaign_metrics_outcome(
                base_status=exc.tool_status(),
                present=False,
                result_count=0,
                latency_ms=elapsed_ms(started),
                now=now,
            )
        if insights is None:
            return ack, _campaign_metrics_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=insights_latency,
                now=now,
            )
        line = format_insights_line(insights)
        if not line:
            return ack, _campaign_metrics_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=insights_latency,
                now=now,
            )
        previous_spend: str | None = None
        previous_clicks: str | None = None
        previous_frequency: str | None = None
        previous_ctr: str | None = None
        if settings is not None:
            time_range = previous_7d_time_range(
                now=now,
                timezone=settings.calendar_timezone,
            )
            if time_range is not None:
                previous = _try_insights(
                    port, date_preset=None, time_range=time_range
                )
                if previous is not None:
                    previous_spend = previous.spend
                    previous_clicks = previous.clicks
                    previous_frequency = previous.frequency
                    previous_ctr = previous.ctr
        rec = analyze_insights(
            spend=insights.spend,
            impressions=insights.impressions,
            clicks=insights.clicks,
            ctr=insights.ctr,
            frequency=insights.frequency,
            previous_spend=previous_spend,
            previous_clicks=previous_clicks,
            previous_frequency=previous_frequency,
            previous_ctr=previous_ctr,
        )
        insight_kwargs = {
            "spend": insights.spend,
            "impressions": insights.impressions,
            "clicks": insights.clicks,
            "ctr": insights.ctr,
            "frequency": insights.frequency,
            "previous_spend": previous_spend,
            "previous_clicks": previous_clicks,
            "previous_frequency": previous_frequency,
            "previous_ctr": previous_ctr,
        }
        lead_count: int | None = None
        previous_lead_count: int | None = None
        behavior_events_counted = False
        if store is not None and settings is not None and (
            rec.kind == KIND_WATCH or rec.anomaly == ANOMALY_CREATIVE_FATIGUE
        ):
            bounds = last_7d_event_bounds(
                now=now,
                timezone=settings.calendar_timezone,
            )
            if bounds is not None:
                lead_count = store.count_canonical_events(
                    event_type="lead_created",
                    occurred_from=bounds[0],
                    occurred_to=bounds[1],
                )
                rec_leads = analyze_insights(
                    **insight_kwargs,
                    lead_count=lead_count,
                )
                if rec_leads.anomaly == ANOMALY_SPEND_WITHOUT_LEADS:
                    rec = rec_leads
                elif lead_count > 0:
                    prev_bounds = previous_7d_event_bounds(
                        now=now,
                        timezone=settings.calendar_timezone,
                    )
                    if prev_bounds is not None:
                        previous_lead_count = store.count_canonical_events(
                            event_type="lead_created",
                            occurred_from=prev_bounds[0],
                            occurred_to=prev_bounds[1],
                        )
                        rec_cpl = analyze_insights(
                            **insight_kwargs,
                            lead_count=lead_count,
                            previous_lead_count=previous_lead_count,
                        )
                        if rec_cpl.anomaly in (
                            ANOMALY_CPL_SPIKE,
                            ANOMALY_CREATIVE_FATIGUE,
                        ):
                            rec = rec_cpl
        if (
            rec.kind == KIND_WATCH
            and store is not None
            and settings is not None
        ):
            bounds = last_7d_event_bounds(
                now=now,
                timezone=settings.calendar_timezone,
            )
            if bounds is not None:
                opened = store.count_behavior_events(
                    kind="mia_opened",
                    occurred_from=bounds[0],
                    occurred_to=bounds[1],
                )
                behavior_events_counted = True
                started = store.count_behavior_events(
                    kind="conversation_started",
                    occurred_from=bounds[0],
                    occurred_to=bounds[1],
                )
                rec_funnel = analyze_insights(
                    **insight_kwargs,
                    lead_count=lead_count,
                    previous_lead_count=previous_lead_count,
                    opened_count=opened,
                    conversation_count=started,
                )
                if rec_funnel.anomaly == ANOMALY_WEBSITE_FUNNEL_DROP:
                    rec = rec_funnel
                elif rec.kind == KIND_WATCH:
                    prev_bounds = previous_7d_event_bounds(
                        now=now,
                        timezone=settings.calendar_timezone,
                    )
                    if prev_bounds is not None:
                        prev_opened = store.count_behavior_events(
                            kind="mia_opened",
                            occurred_from=prev_bounds[0],
                            occurred_to=prev_bounds[1],
                        )
                        prev_started = store.count_behavior_events(
                            kind="conversation_started",
                            occurred_from=prev_bounds[0],
                            occurred_to=prev_bounds[1],
                        )
                        rec_compare = analyze_insights(
                            **insight_kwargs,
                            lead_count=lead_count,
                            previous_lead_count=previous_lead_count,
                            opened_count=opened,
                            conversation_count=started,
                            previous_opened_count=prev_opened,
                            previous_conversation_count=prev_started,
                        )
                        if rec_compare.anomaly == ANOMALY_WEBSITE_FUNNEL_DROP:
                            rec = rec_compare
        if rec.kind == KIND_WATCH and settings is not None:
            insights_30d = _try_insights(port, date_preset="last_30d")
            if insights_30d is not None:
                previous_30d_spend: str | None = None
                previous_30d_clicks: str | None = None
                time_range_30d = previous_30d_time_range(
                    now=now,
                    timezone=settings.calendar_timezone,
                )
                if time_range_30d is not None:
                    previous_30d = _try_insights(
                        port,
                        date_preset=None,
                        time_range=time_range_30d,
                    )
                    if previous_30d is not None:
                        previous_30d_spend = previous_30d.spend
                        previous_30d_clicks = previous_30d.clicks
                rec_30d = analyze_insights(
                    spend=insights_30d.spend,
                    impressions=insights_30d.impressions,
                    clicks=insights_30d.clicks,
                    ctr=insights_30d.ctr,
                    previous_spend=previous_30d_spend,
                    previous_clicks=previous_30d_clicks,
                    compare_window="30d",
                )
                if rec_30d.anomaly == ANOMALY_SPEND_UP_CLICKS_DOWN_30D:
                    rec = rec_30d
        recommend_line = format_recommendation_line(rec)
        enriched = f"{ack}\n\n{line}\n{recommend_line}"
        if settings is not None:
            today_line = ""
            today_insights = _try_insights(port, date_preset="today")
            baseline_range = baseline_7d_time_range(
                now=now,
                timezone=settings.calendar_timezone,
            )
            baseline_insights = None
            if baseline_range is not None:
                baseline_insights = _try_insights(
                    port,
                    date_preset=None,
                    time_range=baseline_range,
                )
            if today_insights is not None and baseline_insights is not None:
                today_line = format_today_baseline_line(today_insights, baseline_insights)
            if today_line:
                enriched = f"{enriched}\n{today_line}"
        if store is not None:
            apply_campaign_recommendation_policy(
                store, rec=rec, kill_switch=kill_switch
            )
        if settings is not None and store is not None:
            from app.core.demo import demo_mode_active
            from app.domain.pacing import (
                apply_pacing_policy,
                apply_performance_policy,
                campaign_label,
                compute_pacing,
                compute_performance,
                format_pacing_line,
                parse_monthly_budget,
            )
            from app.integrations.sheets import maybe_mirror_campaign_control

            budget = parse_monthly_budget(settings.campaign_monthly_budget)
            if budget is not None:
                label = campaign_label(settings)
                mtd = _try_insights(port, date_preset="this_month")
                spend_mtd = None
                if mtd is not None and mtd.spend is not None:
                    from app.domain.campaigns import _parse_metric

                    spend_mtd = _parse_metric(mtd.spend)
                pacing = compute_pacing(
                    monthly_budget=budget,
                    spend_mtd=spend_mtd,
                    now=now,
                    timezone=settings.calendar_timezone,
                    campaign=label,
                )
                apply_pacing_policy(store, snapshot=pacing, kill_switch=kill_switch)
                performance = compute_performance(
                    store,
                    insights=mtd,
                    timezone=settings.calendar_timezone,
                    campaign=label,
                )
                apply_performance_policy(
                    store, snapshot=performance, kill_switch=kill_switch
                )
                if sheets is not None and not demo_mode_active(settings):
                    mirror_outcome = maybe_mirror_campaign_control(
                        store=store,
                        sheets=sheets,
                        settings=settings,
                        kill_switch=kill_switch,
                        inbound_id=inbound_id,
                    )
                    if extra_outcomes is not None and mirror_outcome is not None:
                        extra_outcomes.append(mirror_outcome)
                if extra_outcomes is not None:
                    extra_outcomes.append(
                        campaign_budget_outcome(
                            present=spend_mtd is not None,
                            now=now,
                        )
                    )
                enriched = f"{enriched}\n{format_pacing_line(pacing)}"
        if behavior_events_counted and extra_outcomes is not None:
            extra_outcomes.append(
                website_session_events_outcome(present=True, now=now)
            )
        return (
            enriched,
            _campaign_metrics_outcome(
                base_status="ok",
                present=True,
                result_count=1,
                latency_ms=insights_latency,
                now=now,
            ),
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        now = datetime.now(UTC)
        return ack, _campaign_metrics_outcome(
            base_status="error",
            present=False,
            result_count=0,
            latency_ms=insights_latency,
            now=now,
        )


def resolve_meta_ads_account_id(settings: Settings) -> str:
    """Configured ad account, or the one Composio reports. Env var always wins.

    Read-only: this account id only ever reaches `METAADS_GET_INSIGHTS`. Meta writes stay
    R4 approval-gated regardless of how the id was resolved.
    """
    configured = settings.meta_ads_account_id.strip()
    if configured:
        return configured
    from app.integrations.composio_discovery import build_discovery, cached_resolve

    discovery = build_discovery(settings)
    if discovery is None:
        return ""
    return cached_resolve("meta_ads_account_id", discovery.meta_ad_account)


def build_meta_ads_port(settings: Settings) -> MetaAdsPort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    account_id = resolve_meta_ads_account_id(settings)
    if api_key and user_id and account_id:
        return ComposioMetaAdsPort(
            api_key=api_key,
            user_id=user_id,
            account_id=account_id,
        )
    return DisabledMetaAdsPort()
