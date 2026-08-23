"""LinkedIn personal member post analytics port (ADR-009 — direct official API).

Read-only: ``GET /rest/memberCreatorPostAnalytics`` with ``r_member_postAnalytics``.
Separate from Composio ``LinkedInPort`` profile read. Never post, comment, delete, DM, or upload.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from time import perf_counter
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.ai_runs import elapsed_ms
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import AdapterHttpError, ToolOutcome

LINKEDIN_API_VERSION = "202608"
_MEMBER_CREATOR_POST_ANALYTICS_URL = (
    "https://api.linkedin.com/rest/memberCreatorPostAnalytics"
)

_METRIC_QUERY_TYPES: tuple[tuple[str, str], ...] = (
    ("IMPRESSION", "impressions"),
    ("MEMBERS_REACHED", "members_reached"),
    ("REACTION", "reactions"),
    ("COMMENT", "comments"),
    ("RESHARE", "reshares"),
    ("LINK_CLICKS", "link_clicks"),
)


class LinkedInAnalyticsSnapshot(BaseModel):
    period_days: int = Field(default=30, ge=1, strict=True)
    impressions: int | None = Field(default=None, ge=0, strict=True)
    members_reached: int | None = Field(default=None, ge=0, strict=True)
    reactions: int | None = Field(default=None, ge=0, strict=True)
    comments: int | None = Field(default=None, ge=0, strict=True)
    reshares: int | None = Field(default=None, ge=0, strict=True)
    link_clicks: int | None = Field(default=None, ge=0, strict=True)


class LinkedInAnalyticsPort(Protocol):
    def get_member_analytics(
        self, *, start: date, end: date
    ) -> LinkedInAnalyticsSnapshot | None: ...


class DisabledLinkedInAnalyticsPort:
    def get_member_analytics(
        self, *, start: date, end: date
    ) -> LinkedInAnalyticsSnapshot | None:
        del start, end
        return None


class FakeLinkedInAnalyticsPort:
    """Test double. Returns configured snapshot and records calls."""

    def __init__(self, snapshot: LinkedInAnalyticsSnapshot | None = None) -> None:
        self._snapshot = snapshot
        self.calls: list[tuple[date, date]] = []

    def get_member_analytics(
        self, *, start: date, end: date
    ) -> LinkedInAnalyticsSnapshot | None:
        self.calls.append((start, end))
        return self._snapshot


class DirectLinkedInAnalyticsPort:
    """Live LinkedIn REST adapter. Raises AdapterHttpError on auth/transport/total HTTP failure."""

    def __init__(
        self,
        *,
        access_token: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._access_token = access_token
        self._client = client

    def get_member_analytics(
        self, *, start: date, end: date
    ) -> LinkedInAnalyticsSnapshot | None:
        period_days = (end - start).days
        if period_days <= 0:
            return None
        try:
            if self._client is not None:
                return self._collect_metrics(
                    client=self._client,
                    start=start,
                    end=end,
                    period_days=period_days,
                )
            with httpx.Client(timeout=20.0) as client:
                return self._collect_metrics(
                    client=client,
                    start=start,
                    end=end,
                    period_days=period_days,
                )
        except AdapterHttpError:
            raise
        except (
            ValueError,
            KeyError,
            TypeError,
            OSError,
        ):
            return None

    def _collect_metrics(
        self,
        *,
        client: httpx.Client,
        start: date,
        end: date,
        period_days: int,
    ) -> LinkedInAnalyticsSnapshot | None:
        values: dict[str, int | None] = {}
        last_http_failure: int | None = None
        had_transport_error = False
        for query_type, field_name in _METRIC_QUERY_TYPES:
            status_code, count = self._fetch_metric_count(
                client=client,
                query_type=query_type,
                start=start,
                end=end,
            )
            if status_code in (401, 403):
                raise AdapterHttpError(status_code)
            if status_code is None:
                had_transport_error = True
                values[field_name] = None
                continue
            if status_code >= 400:
                last_http_failure = status_code
                values[field_name] = None
                continue
            values[field_name] = count
        populated = {key: val for key, val in values.items() if val is not None}
        if populated:
            return LinkedInAnalyticsSnapshot(period_days=period_days, **populated)
        if last_http_failure is not None:
            raise AdapterHttpError(last_http_failure)
        if had_transport_error:
            raise AdapterHttpError(None)
        return None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Linkedin-Version": LINKEDIN_API_VERSION,
            "Content-Type": "application/json",
        }

    def _fetch_metric_count(
        self,
        *,
        client: httpx.Client,
        query_type: str,
        start: date,
        end: date,
    ) -> tuple[int | None, int | None]:
        params = {
            "q": "me",
            "queryType": query_type,
            "aggregation": "TOTAL",
            "dateRange": _format_linkedin_date_range(start, end),
        }
        try:
            response = client.get(
                _MEMBER_CREATOR_POST_ANALYTICS_URL,
                params=params,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            return response.status_code, None
        try:
            body = response.json()
            if not isinstance(body, dict):
                return response.status_code, None
            return response.status_code, _sum_element_counts(
                body.get("elements"),
                expected_metric=query_type,
            )
        except (
            ValueError,
            KeyError,
            TypeError,
            OSError,
        ):
            return response.status_code, None


def linkedin_analytics_date_range(
    *, now: datetime, timezone: str
) -> tuple[date, date] | None:
    """Previous 30 completed local-calendar days: start=D-30, end=D exclusive."""
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_date = now.astimezone(tz).date()
    start = local_date - timedelta(days=30)
    end = local_date
    return start, end


def _format_linkedin_date_range(start: date, end: date) -> str:
    return (
        f"(start:(day:{start.day},month:{start.month},year:{start.year}),"
        f"end:(day:{end.day},month:{end.month},year:{end.year}))"
    )


def _sum_element_counts(elements: object, *, expected_metric: str) -> int | None:
    if not isinstance(elements, list) or not elements:
        return None
    total = 0
    found = False
    for entry in elements:
        if not isinstance(entry, dict):
            continue
        if entry.get("metricType") != expected_metric:
            continue
        raw = entry.get("count")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            continue
        total += raw
        found = True
    return total if found else None


def _populated_metric_count(snapshot: LinkedInAnalyticsSnapshot) -> int:
    return sum(
        1
        for value in (
            snapshot.impressions,
            snapshot.members_reached,
            snapshot.reactions,
            snapshot.comments,
            snapshot.reshares,
            snapshot.link_clicks,
        )
        if value is not None
    )


def _linkedin_content_metrics_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "linkedin_content_metrics",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="linkedin_analytics",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def format_analytics_line(snapshot: LinkedInAnalyticsSnapshot) -> str:
    """Hebrew stats-only line. Missing metrics omitted; never invent values."""
    parts: list[str] = []
    if snapshot.impressions is not None:
        parts.append(f"חשיפות {snapshot.impressions}")
    if snapshot.members_reached is not None:
        parts.append(f"אנשים שנחשפו {snapshot.members_reached}")
    if snapshot.reactions is not None:
        parts.append(f"ריאקציות {snapshot.reactions}")
    if snapshot.comments is not None:
        parts.append(f"תגובות {snapshot.comments}")
    if snapshot.reshares is not None:
        parts.append(f"שיתופים {snapshot.reshares}")
    if snapshot.link_clicks is not None:
        parts.append(f"קליקים על קישורים {snapshot.link_clicks}")
    if not parts:
        return ""
    return (
        "ביצועי תוכן בשלושים הימים המלאים האחרונים: "
        + ", ".join(parts)
        + "."
    )


def enrich_linkedin_analytics_ack(
    ack: str,
    port: LinkedInAnalyticsPort,
    kill_switch: bool,
    *,
    now: datetime,
    timezone: str,
) -> tuple[str, ToolOutcome]:
    """Append personal analytics snapshot to owner linkedin ack. Never raises."""
    try:
        assert_allowed(
            RiskAction(name="linkedin_analytics_read", risk=RiskLevel.R0_READ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return ack, ToolOutcome(
            tool="linkedin_analytics",
            status="denied",
            result_count=0,
            freshness="",
        )

    date_range = linkedin_analytics_date_range(now=now, timezone=timezone)
    if date_range is None:
        return ack, _linkedin_content_metrics_outcome(
            base_status="empty",
            present=False,
            result_count=0,
            latency_ms=0,
            now=now,
        )

    start, end = date_range
    started = perf_counter()
    try:
        snapshot = port.get_member_analytics(start=start, end=end)
        latency = elapsed_ms(started)
        if snapshot is None:
            return ack, _linkedin_content_metrics_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        line = format_analytics_line(snapshot)
        if not line:
            return ack, _linkedin_content_metrics_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        metric_count = _populated_metric_count(snapshot)
        base_status = "ok" if metric_count == len(_METRIC_QUERY_TYPES) else "partial"
        return (
            f"{ack}\n\n{line}",
            _linkedin_content_metrics_outcome(
                base_status=base_status,
                present=True,
                result_count=metric_count,
                latency_ms=latency,
                now=now,
            ),
        )
    except AdapterHttpError as exc:
        return ack, _linkedin_content_metrics_outcome(
            base_status=exc.tool_status(),
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=now,
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        return ack, _linkedin_content_metrics_outcome(
            base_status="error",
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=now,
        )


def build_linkedin_analytics_port(settings: Settings) -> LinkedInAnalyticsPort:
    """Member post analytics is not a Composio tool. Leftover tokens are ignored.

    ``LINKEDIN_GET_SHARE_STATS`` is organization-page stats (ADR-027). Do not
    treat that as personal member analytics. Profile read stays on ``LinkedInPort``.
    """
    del settings
    return DisabledLinkedInAnalyticsPort()
