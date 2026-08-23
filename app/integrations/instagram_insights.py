"""Instagram organic content insights read port (ADR-015).

Read-only: recent media list + per-media insights. Composio when sender=composio
or when Graph tokens are empty. Graph remains the default-direct path.
No publish, comments write, captions, or media URLs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.demo import demo_mode_active
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.ai_runs import elapsed_ms
from app.domain.content_insights import (
    ALLOWLISTED_MEDIA_TYPES,
    ContentInsight,
    apply_content_insight_policy,
    is_allowlisted_media_id,
)
from app.domain.ownership_freshness import VALID_INSTAGRAM_SENDERS
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import AdapterHttpError, ToolOutcome
from app.integrations.instagram import (
    COMPOSIO_GET_MEDIA_INSIGHTS_TOOL,
    COMPOSIO_GET_USER_MEDIA_TOOL,
    COMPOSIO_INSTAGRAM_VERSION,
)
from app.integrations.sheets import SheetsPort, maybe_mirror_content_insights

_COMPOSIO_EXECUTE_BASE = "https://backend.composio.dev/api/v3.1/tools/execute"
_MEDIA_LIST_FIELDS = "id,media_type"

_INSIGHT_METRICS = ("views", "reach", "likes", "comments", "saved")
_ALLOWED_GRAPH_HOSTS = frozenset({"graph.instagram.com", "graph.facebook.com"})


class InstagramInsightsPort(Protocol):
    def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]: ...


class DisabledInstagramInsightsPort:
    def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]:
        del limit
        return []


class GraphInstagramInsightsPort:
    """Direct Instagram Graph adapter. Raises AdapterHttpError on media-list HTTP/transport."""

    def __init__(
        self,
        *,
        access_token: str,
        account_id: str,
        graph_version: str,
        graph_host: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._access_token = access_token
        self._account_id = account_id
        self._graph_version = graph_version.strip().removeprefix("/")
        self._graph_host = _normalized_graph_host(graph_host)
        self._client = client

    def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]:
        capped = max(1, min(limit, 25))
        try:
            media_items = self._fetch_media_list(limit=capped)
            results: list[ContentInsight] = []
            for media_id, media_type in media_items:
                metrics = self._fetch_insights(media_id)
                if metrics is None:
                    continue
                results.append(
                    ContentInsight(
                        media_id=media_id,
                        media_type=media_type,
                        **metrics,
                    )
                )
            return results
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            OSError,
        ):
            return []

    def _base_url(self, path: str) -> str:
        return f"https://{self._graph_host}/{self._graph_version}/{path}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get_json(
        self, url: str, params: dict[str, str], *, classify_http: bool = False
    ) -> dict | None:
        headers = self._headers()
        try:
            if self._client is not None:
                response = self._client.get(url, params=params, headers=headers)
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            if classify_http:
                raise AdapterHttpError(None) from exc
            raise
        if response.status_code >= 400:
            if classify_http:
                raise AdapterHttpError(response.status_code)
            return None
        body = response.json()
        if not isinstance(body, dict):
            return None
        return body

    def _fetch_media_list(self, *, limit: int) -> list[tuple[str, str]]:
        url = self._base_url(f"{self._account_id}/media")
        params = {"fields": "id,media_type", "limit": str(limit)}
        body = self._get_json(url, params, classify_http=True)
        if body is None:
            return []
        data = body.get("data")
        if not isinstance(data, list):
            return []
        items: list[tuple[str, str]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("id")
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                raw_id = str(raw_id)
            raw_type = entry.get("media_type")
            if not isinstance(raw_id, str) or not isinstance(raw_type, str):
                continue
            media_id = raw_id.strip()
            media_type = raw_type.strip().upper()
            if not is_allowlisted_media_id(media_id):
                continue
            if media_type not in ALLOWLISTED_MEDIA_TYPES:
                continue
            items.append((media_id, media_type))
        return items

    def _fetch_insights(self, media_id: str) -> dict[str, str | None] | None:
        url = self._base_url(f"{media_id}/insights")
        params = {"metric": ",".join(_INSIGHT_METRICS)}
        body = self._get_json(url, params)
        if body is None:
            return None
        data = body.get("data")
        if not isinstance(data, list):
            return None
        metrics: dict[str, str | None] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or name not in _INSIGHT_METRICS:
                continue
            value = _parse_insight_metric_value(entry)
            if value is not None:
                metrics[name] = value
        return metrics


class ComposioInstagramInsightsPort:
    """Composio INSTAGRAM media list + insights. Captions and URLs are never requested."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        account_id: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._account_id = account_id.strip() or "me"
        self._client = client

    def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]:
        capped = max(1, min(limit, 25))
        media_items = self._fetch_media_list(limit=capped)
        results: list[ContentInsight] = []
        for media_id, media_type in media_items:
            metrics = self._fetch_insights(media_id)
            if metrics is None:
                continue
            results.append(
                ContentInsight(
                    media_id=media_id,
                    media_type=media_type,
                    **metrics,
                )
            )
        return results

    def _execute(self, tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_INSTAGRAM_VERSION,
            "arguments": arguments,
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        url = f"{_COMPOSIO_EXECUTE_BASE}/{tool_slug}"
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(url, json=payload, headers=headers)
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
            if isinstance(data, dict):
                return data
            return None
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return None

    def _fetch_media_list(self, *, limit: int) -> list[tuple[str, str]]:
        body = self._execute(
            COMPOSIO_GET_USER_MEDIA_TOOL,
            {
                "ig_user_id": self._account_id,
                "limit": limit,
                "fields": _MEDIA_LIST_FIELDS,
            },
        )
        if body is None:
            return []
        data = body.get("data")
        if not isinstance(data, list):
            return []
        items: list[tuple[str, str]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get("id")
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                raw_id = str(raw_id)
            raw_type = entry.get("media_type")
            if not isinstance(raw_id, str) or not isinstance(raw_type, str):
                continue
            media_id = raw_id.strip()
            media_type = raw_type.strip().upper()
            if not is_allowlisted_media_id(media_id):
                continue
            if media_type not in ALLOWLISTED_MEDIA_TYPES:
                continue
            items.append((media_id, media_type))
        return items

    def _fetch_insights(self, media_id: str) -> dict[str, str | None] | None:
        body = self._execute(
            COMPOSIO_GET_MEDIA_INSIGHTS_TOOL,
            {
                "ig_media_id": media_id,
                "metric": list(_INSIGHT_METRICS),
            },
        )
        if body is None:
            return None
        data = body.get("data")
        if not isinstance(data, list):
            return None
        metrics: dict[str, str | None] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or name not in _INSIGHT_METRICS:
                continue
            value = _parse_insight_metric_value(entry)
            if value is not None:
                metrics[name] = value
        return metrics


class FakeInstagramInsightsPort:
    def __init__(self, items: list[ContentInsight] | None = None) -> None:
        self._items = list(items or [])

    def list_recent_insights(self, *, limit: int = 5) -> list[ContentInsight]:
        return self._items[:limit]


def _normalized_graph_host(host: str) -> str:
    cleaned = host.strip().removeprefix("https://").removeprefix("http://").split("/")[0]
    if cleaned not in _ALLOWED_GRAPH_HOSTS:
        return "graph.instagram.com"
    return cleaned


def _parse_insight_metric_value(entry: dict) -> str | None:
    values = entry.get("values")
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            raw = first.get("value")
            parsed = _metric_value(raw)
            if parsed is not None:
                return parsed
    total_value = entry.get("total_value")
    if isinstance(total_value, dict):
        raw = total_value.get("value")
        return _metric_value(raw)
    return None


def _metric_value(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        stripped = raw.strip()
        return stripped if stripped else None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return str(int(raw)) if isinstance(raw, float) and raw.is_integer() else str(raw)
    return None


def format_content_insights_line(
    items: list[ContentInsight], *, total_signals: int = 0
) -> str:
    n = len(items)
    if n == 0:
        return ""
    return f"תוכן: {n} פוסטים, לידים מתוכן {total_signals}."


def _instagram_content_metrics_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "instagram_content_metrics",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="instagram_insights",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def enrich_content_insights_ack(
    ack: str,
    port: InstagramInsightsPort,
    store,
    kill_switch: bool,
    sheets: SheetsPort | None = None,
    settings: Settings | None = None,
    extra_outcomes: list[ToolOutcome] | None = None,
    inbound_id: str = "",
) -> tuple[str, ToolOutcome]:
    try:
        assert_allowed(
            RiskAction(name="instagram_insights_read", risk=RiskLevel.R0_READ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return ack, ToolOutcome(
            tool="instagram_insights",
            status="denied",
            result_count=0,
            freshness="",
        )
    started = perf_counter()
    now = datetime.now(UTC)
    try:
        items = port.list_recent_insights(limit=5)
        latency = elapsed_ms(started)
        if not items:
            return ack, _instagram_content_metrics_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        apply_content_insight_policy(store, items=items, kill_switch=kill_switch)
        if sheets is not None and settings is not None and not demo_mode_active(settings):
            mirror_outcome = maybe_mirror_content_insights(
                store=store,
                sheets=sheets,
                settings=settings,
                kill_switch=kill_switch,
                inbound_id=inbound_id,
            )
            if extra_outcomes is not None and mirror_outcome is not None:
                extra_outcomes.append(mirror_outcome)
        wanted = {item.media_id for item in items}
        total_signals = sum(
            record.lead_signals
            for record in store.list_content_insights()
            if record.media_id in wanted
        )
        line = format_content_insights_line(items, total_signals=total_signals)
        if not line:
            return ack, _instagram_content_metrics_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        enriched = f"{ack}\n\n{line}" if ack else line
        return (
            enriched,
            _instagram_content_metrics_outcome(
                base_status="ok",
                present=True,
                result_count=len(items),
                latency_ms=latency,
                now=now,
            ),
        )
    except AdapterHttpError as exc:
        return ack, _instagram_content_metrics_outcome(
            base_status=exc.tool_status(),
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=now,
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        return ack, _instagram_content_metrics_outcome(
            base_status="error",
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=now,
        )


def build_instagram_insights_port(settings: Settings) -> InstagramInsightsPort:
    sender = (
        settings.instagram_sender
        if settings.instagram_sender in VALID_INSTAGRAM_SENDERS
        else "direct"
    )
    token = settings.instagram_access_token.strip()
    account_id = settings.instagram_account_id.strip()
    if sender == "composio":
        if settings.composio_ready():
            return ComposioInstagramInsightsPort(
                api_key=settings.composio_api_key,
                user_id=settings.composio_user_id,
                account_id=account_id,
            )
        return DisabledInstagramInsightsPort()
    if token and account_id:
        return GraphInstagramInsightsPort(
            access_token=token,
            account_id=account_id,
            graph_version=settings.instagram_graph_version,
            graph_host=settings.instagram_graph_host,
        )
    if settings.composio_ready():
        return ComposioInstagramInsightsPort(
            api_key=settings.composio_api_key,
            user_id=settings.composio_user_id,
            account_id=account_id,
        )
    return DisabledInstagramInsightsPort()
