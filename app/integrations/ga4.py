"""Google Analytics 4 read port.

Production adapter: Composio ``GOOGLE_ANALYTICS`` toolkit version ``20260721_00``,
pins ``RUN_PIVOT_REPORT``, ``LIST_CONVERSION_EVENTS``, and ``LIST_ACCOUNT_SUMMARIES``.
Property id comes from ``MIA_GA4_PROPERTY_ID`` or opt-in ``composio_discovery``
(cached, not per request). Read-only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import (
    AdapterHttpError,
    AdapterResponseError,
    AdapterSchemaError,
    ToolOutcome,
)

COMPOSIO_GA4_VERSION = "20260721_00"
COMPOSIO_PIVOT_REPORT_TOOL = "GOOGLE_ANALYTICS_RUN_PIVOT_REPORT"
COMPOSIO_LIST_CONVERSION_EVENTS_TOOL = "GOOGLE_ANALYTICS_LIST_CONVERSION_EVENTS"
COMPOSIO_LIST_ACCOUNT_SUMMARIES_TOOL = "GOOGLE_ANALYTICS_LIST_ACCOUNT_SUMMARIES"
_COMPOSIO_EXECUTE_BASE = "https://backend.composio.dev/api/v3.1/tools/execute"
MAX_PIVOT_ROWS = 10
PREFERRED_GA4_NAME = "assafweb"
_PROPERTY_RE = re.compile(r"^properties/\d+$")
_GA4_METRIC_TYPES = frozenset(
    {
        "METRIC_TYPE_UNSPECIFIED",
        "TYPE_INTEGER",
        "TYPE_FLOAT",
        "TYPE_SECONDS",
        "TYPE_MILLISECONDS",
        "TYPE_MINUTES",
        "TYPE_HOURS",
        "TYPE_STANDARD",
        "TYPE_CURRENCY",
        "TYPE_FEET",
        "TYPE_MILES",
        "TYPE_METERS",
        "TYPE_KILOMETERS",
    }
)
_PIVOT_DIMENSIONS = ("landingPage", "sessionSource")
_PIVOT_METRICS = ("activeUsers", "sessions", "conversions", "engagedSessions")
_HISTORICAL_PIVOT_METRICS = ("sessions", "engagedSessions")


class Ga4PivotRow(BaseModel):
    landing_page: str = ""
    session_source: str = ""
    sessions: str | None = None
    engaged_sessions: str | None = None
    users: str | None = None
    conversions: str | None = None


class Ga4Port(Protocol):
    def run_pivot_report(
        self,
        *,
        start_date: str,
        end_date: str,
    ) -> list[Ga4PivotRow]: ...

    def list_conversion_events(self) -> list[str]: ...


class DisabledGa4Port:
    def run_pivot_report(
        self,
        *,
        start_date: str,
        end_date: str,
    ) -> list[Ga4PivotRow]:
        del start_date, end_date
        return []

    def list_conversion_events(self) -> list[str]:
        return []


class FakeGa4Port:
    def __init__(
        self,
        *,
        pivot_rows: list[Ga4PivotRow] | None = None,
        conversion_events: list[str] | None = None,
    ) -> None:
        self._pivot_rows = list(pivot_rows or [])
        self._conversion_events = list(conversion_events or [])
        self.last_pivot_args: dict[str, str] | None = None

    def run_pivot_report(
        self,
        *,
        start_date: str,
        end_date: str,
    ) -> list[Ga4PivotRow]:
        self.last_pivot_args = {"start_date": start_date, "end_date": end_date}
        return list(self._pivot_rows)

    def list_conversion_events(self) -> list[str]:
        return list(self._conversion_events)


class ComposioGa4Port:
    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        property_id: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._property_id = property_id.strip()
        self._client = client

    def run_pivot_report(
        self,
        *,
        start_date: str,
        end_date: str,
    ) -> list[Ga4PivotRow]:
        property_id = self._property_id
        if not property_id:
            return []
        arguments: dict[str, Any] = {
            "property": property_id,
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": "landingPage"}, {"name": "sessionSource"}],
            "metrics": [
                {"name": "activeUsers"},
                {"name": "sessions"},
                {"name": "conversions"},
                {"name": "engagedSessions"},
            ],
            "pivots": [
                {"fieldNames": ["landingPage"], "limit": MAX_PIVOT_ROWS},
                {"fieldNames": ["sessionSource"], "limit": MAX_PIVOT_ROWS},
            ],
        }
        body = self._execute(COMPOSIO_PIVOT_REPORT_TOOL, arguments)
        if body is not None and not _has_pivot_rows_schema(body):
            raise AdapterSchemaError()
        return _map_pivot_rows(body)

    def list_conversion_events(self) -> list[str]:
        property_id = self._property_id
        if not property_id:
            return []
        arguments = {"parent": property_id}
        body = self._execute(COMPOSIO_LIST_CONVERSION_EVENTS_TOOL, arguments)
        if body is not None and not _has_conversion_events_schema(body):
            raise AdapterSchemaError()
        return _map_conversion_events(body)

    def _execute(self, tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_GA4_VERSION,
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
            if not isinstance(body, dict) or not isinstance(body.get("successful"), bool):
                raise AdapterSchemaError()
            if body["successful"] is False:
                raise AdapterResponseError()
            data = body.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    raise AdapterSchemaError() from None
            if isinstance(data, dict):
                return data
            raise AdapterSchemaError()
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            raise AdapterSchemaError() from None


def normalize_ga4_property_id(raw: str) -> str | None:
    trimmed = raw.strip()
    if not trimmed:
        return None
    if trimmed.startswith("properties/"):
        trimmed = trimmed
    elif trimmed.isdigit():
        trimmed = f"properties/{trimmed}"
    if _PROPERTY_RE.fullmatch(trimmed):
        return trimmed
    return None


def pick_ga4_property(
    summaries: list[tuple[str, str]], *, preferred: str = ""
) -> str | None:
    explicit = normalize_ga4_property_id(preferred)
    if explicit:
        return explicit
    for property_id, display_name in summaries:
        blob = f"{property_id} {display_name}".lower()
        if PREFERRED_GA4_NAME in blob:
            return normalize_ga4_property_id(property_id)
    if len(summaries) == 1:
        return normalize_ga4_property_id(summaries[0][0])
    return None


def _map_ga4_property_summaries(
    data: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    if data is None:
        return []
    accounts = data.get("accountSummaries") or data.get("accounts") or []
    if not isinstance(accounts, list):
        return []
    mapped: list[tuple[str, str]] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        properties = (
            account.get("propertySummaries")
            or account.get("properties")
            or []
        )
        if not isinstance(properties, list):
            continue
        for item in properties:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("property") or item.get("name") or item.get("id")
            raw_name = item.get("displayName") or item.get("name") or ""
            property_id = raw_id.strip() if isinstance(raw_id, str) else ""
            display_name = raw_name.strip() if isinstance(raw_name, str) else ""
            if property_id:
                mapped.append((property_id, display_name))
    return mapped


def _metric_str(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _map_pivot_rows(data: dict[str, Any] | None) -> list[Ga4PivotRow]:
    if data is None:
        return []
    rows = data.get("rows")
    if not isinstance(rows, list):
        pivot = data.get("pivotReport") or data.get("report")
        if isinstance(pivot, dict):
            rows = pivot.get("rows")
    if not isinstance(rows, list):
        return []
    mapped: list[Ga4PivotRow] = []
    for row in rows:
        if not _has_pivot_row_schema(row):
            raise AdapterSchemaError()
        assert isinstance(row, dict)
        dimension_values = (
            row["dimensionValues"]
            if "dimensionValues" in row
            else row.get("dimensions")
        )
        metric_values = (
            row["metricValues"] if "metricValues" in row else row.get("metrics")
        )
        if not isinstance(dimension_values, list) or not isinstance(metric_values, list):
            raise AdapterSchemaError()
        landing = ""
        source = ""
        sessions: str | None = None
        engaged: str | None = None
        users: str | None = None
        conversions: str | None = None
        landing = _dim_value(dimension_values[0])
        source = _dim_value(dimension_values[1])
        if len(metric_values) == 2:  # historical fixture/provider response shape
            sessions = _metric_str(_metric_value(metric_values[0]))
            engaged = _metric_str(_metric_value(metric_values[1]))
        else:
            values = [_metric_str(_metric_value(value)) for value in metric_values]
            users, sessions, conversions, engaged = values
        mapped.append(
            Ga4PivotRow(
                landing_page=landing,
                session_source=source,
                sessions=sessions,
                engaged_sessions=engaged,
                users=users,
                conversions=conversions,
            )
        )
        if len(mapped) >= MAX_PIVOT_ROWS:
            break
    return mapped


def _has_pivot_rows_schema(data: dict[str, Any]) -> bool:
    if "rows" in data:
        return _has_pivot_report_schema(data)
    for key in ("pivotReport", "report"):
        if key in data:
            nested = data[key]
            return isinstance(nested, dict) and _has_pivot_report_schema(nested)
    return _has_pivot_report_schema(data)


def _has_pivot_report_schema(data: dict[str, Any]) -> bool:
    """Accept GA4's typed no-data report, while malformed present rows stay fatal."""
    if "rows" in data:
        rows = data["rows"]
        if not isinstance(rows, list) or not all(
            _has_pivot_row_schema(row) for row in rows
        ):
            return False
        return _has_report_header_schema(
            data,
            require_all=False,
            require_empty_pivots=not rows,
        ) and _has_expected_pivot_headers(data, rows=rows)
    return _has_report_header_schema(
        data,
        require_all=True,
        require_empty_pivots=True,
    ) and _has_expected_pivot_headers(data, rows=[])


def _has_expected_pivot_headers(
    data: dict[str, Any], *, rows: list[object]
) -> bool:
    """Bind positional values to the exact semantic request contract.

    GA4 values are positional. Merely validating that header names are strings lets a
    provider reorder or add columns while Mia confidently labels the wrong KPI. A
    rowless response may legitimately return only a prefix of the requested headers,
    but populated responses must name the exact columns for their row shape whenever
    headers are present. Headerless populated responses remain accepted for the
    historical two-metric provider shape.
    """

    dimension_names = _header_names(data.get("dimensionHeaders"))
    metric_names = _header_names(data.get("metricHeaders"))
    metric_count: int | None = None
    if rows:
        metric_counts = {
            len(row.get("metricValues", row.get("metrics", [])))
            for row in rows
            if isinstance(row, dict)
        }
        if len(metric_counts) != 1:
            return False
        metric_count = next(iter(metric_counts))
        # The only accepted headerless compatibility shape is the historical
        # two-metric report. Current four-metric data must carry both exact header
        # sets or positional values could be confidently assigned to the wrong KPI.
        both_missing = dimension_names is None and metric_names is None
        if metric_count == 2 and both_missing:
            return True
        if dimension_names is None or metric_names is None:
            return False
    if dimension_names is not None:
        expected_dimensions = (
            _PIVOT_DIMENSIONS
            if rows
            else _PIVOT_DIMENSIONS[: len(dimension_names)]
        )
        if dimension_names != expected_dimensions:
            return False
    if metric_names is None:
        return True
    if not rows:
        return metric_names == _PIVOT_METRICS[: len(metric_names)]
    assert metric_count is not None
    expected_metrics = (
        _HISTORICAL_PIVOT_METRICS if metric_count == 2 else _PIVOT_METRICS
    )
    return metric_names == expected_metrics


def _header_names(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for header in value:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str):
            return ()
        names.append(header["name"].strip())
    return tuple(names)


def _has_report_header_schema(
    data: dict[str, Any],
    *,
    require_all: bool,
    require_empty_pivots: bool,
) -> bool:
    validators = {
        "dimensionHeaders": _has_dimension_header_schema,
        "metricHeaders": _has_metric_header_schema,
        "pivotHeaders": _has_pivot_header_schema,
    }
    for key, validator in validators.items():
        if key not in data:
            if require_all:
                return False
            continue
        values = data[key]
        if not isinstance(values, list) or not all(validator(value) for value in values):
            return False
        if key == "pivotHeaders" and require_empty_pivots and values:
            return False
    if "metadata" not in data:
        return not require_all
    return isinstance(data["metadata"], dict)


def _has_dimension_header_schema(header: object) -> bool:
    return (
        isinstance(header, dict)
        and isinstance(header.get("name"), str)
        and bool(header["name"].strip())
    )


def _has_metric_header_schema(header: object) -> bool:
    if not isinstance(header, dict):
        return False
    metric_type = header.get("type")
    return (
        isinstance(header.get("name"), str)
        and bool(header["name"].strip())
        and isinstance(metric_type, str)
        and metric_type in _GA4_METRIC_TYPES
    )


def _has_pivot_header_schema(header: object) -> bool:
    if not isinstance(header, dict):
        return False
    pivot_dimension_headers = header.get("pivotDimensionHeaders")
    row_count = header.get("rowCount")
    if (
        isinstance(pivot_dimension_headers, list)
        and all(
            _has_pivot_dimension_header_schema(item)
            for item in pivot_dimension_headers
        )
        and isinstance(row_count, int)
        and not isinstance(row_count, bool)
        and row_count >= 0
    ):
        return (
            (row_count == 0 and not pivot_dimension_headers)
            or (
                row_count > 0
                and bool(pivot_dimension_headers)
                and len(pivot_dimension_headers) <= row_count
            )
        )
    return False


def _has_pivot_dimension_header_schema(header: object) -> bool:
    if not isinstance(header, dict):
        return False
    dimension_values = header.get("dimensionValues")
    return isinstance(dimension_values, list) and bool(dimension_values) and all(
        isinstance(value, dict)
        and isinstance(value.get("value"), str)
        for value in dimension_values
    )


def _has_pivot_row_schema(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    dimension_values = (
        row["dimensionValues"]
        if "dimensionValues" in row
        else row.get("dimensions")
    )
    metric_values = (
        row["metricValues"] if "metricValues" in row else row.get("metrics")
    )
    if (
        not isinstance(dimension_values, list)
        or len(dimension_values) != 2
        or any(not _has_dimension_value_schema(value) for value in dimension_values)
        or not isinstance(metric_values, list)
        or len(metric_values) not in {2, 4}
        or any(_metric_str(_metric_value(value)) is None for value in metric_values)
    ):
        return False
    return True


def _has_dimension_value_schema(value: object) -> bool:
    if isinstance(value, str):
        return True
    return (
        isinstance(value, dict)
        and "value" in value
        and isinstance(value["value"], str)
    )


def _dim_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        raw = value.get("value")
        return raw.strip() if isinstance(raw, str) else ""
    return ""


def _metric_value(value: object) -> object:
    if isinstance(value, dict):
        return value.get("value")
    return value


def _map_conversion_events(data: dict[str, Any] | None) -> list[str]:
    if data is None:
        return []
    events = data.get("conversionEvents") or data.get("events") or data.get("items")
    if not isinstance(events, list):
        return []
    names: list[str] = []
    for event in events:
        if isinstance(event, str):
            name = event.strip()
        elif isinstance(event, dict):
            raw = event.get("eventName") or event.get("name")
            name = raw.strip() if isinstance(raw, str) else ""
        else:
            name = ""
        if name:
            names.append(name)
        if len(names) >= MAX_PIVOT_ROWS:
            break
    return names


def _has_conversion_events_schema(data: dict[str, Any]) -> bool:
    return any(isinstance(data.get(key), list) for key in ("conversionEvents", "events", "items"))


def _ga4_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "ga4_traffic_metrics",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="ga4_pivot_report",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def build_ga4_port(settings: Settings) -> Ga4Port:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if not (api_key and user_id):
        return DisabledGa4Port()
    explicit = settings.ga4_property_id.strip()
    if explicit and normalize_ga4_property_id(explicit) is None:
        return DisabledGa4Port()
    property_id = normalize_ga4_property_id(explicit) or ""
    if not property_id:
        # No property configured: ask Composio which one the connected account owns.
        # Lazy import — composio_discovery imports this module for the pinned version.
        from app.integrations.composio_discovery import build_discovery, cached_resolve

        discovery = build_discovery(settings)
        if discovery is not None:
            discovered = cached_resolve("ga4_property_id", discovery.ga4_property)
            property_id = normalize_ga4_property_id(discovered) or ""
    return ComposioGa4Port(
        api_key=api_key,
        user_id=user_id,
        property_id=property_id,
    )


def format_ga4_rows_block(rows: list[Ga4PivotRow]) -> str:
    lines: list[str] = []
    for row in rows:
        label = (row.landing_page or row.session_source).strip()
        if not label:
            continue
        parts: list[str] = [label[:120]]
        if row.sessions is not None:
            parts.append(f"סשנים {row.sessions}")
        if row.users is not None:
            parts.append(f"משתמשים {row.users}")
        if row.conversions is not None:
            parts.append(f"המרות {row.conversions}")
        if row.engaged_sessions is not None:
            parts.append(f"מעורבים {row.engaged_sessions}")
        if len(parts) > 1:
            lines.append(" — ".join(parts))
        if len(lines) >= MAX_PIVOT_ROWS:
            break
    if not lines:
        return ""
    return "תנועה (GA4):\n" + "\n".join(f"- {line}" for line in lines)


def format_conversion_events_block(events: list[str]) -> str:
    if not events:
        return ""
    shown = events[:MAX_PIVOT_ROWS]
    return "אירועי המרה (GA4):\n" + "\n".join(f"- {name[:80]}" for name in shown)
