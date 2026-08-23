"""Google Search Console read port.

Production adapter: Composio ``GOOGLE_SEARCH_CONSOLE`` toolkit version ``20260806_00``,
pins ``SEARCH_ANALYTICS_QUERY``, ``INSPECT_URL``, ``LIST_SITES`` when Composio
credentials are set. Site URL is optional leftover env; otherwise ``LIST_SITES``
picks AssafWeb. Read-only — never add sitemap or site.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import AdapterHttpError, ToolOutcome

COMPOSIO_GSC_VERSION = "20260806_00"
COMPOSIO_LIST_SITES_TOOL = "GOOGLE_SEARCH_CONSOLE_LIST_SITES"
COMPOSIO_SEARCH_ANALYTICS_TOOL = "GOOGLE_SEARCH_CONSOLE_SEARCH_ANALYTICS_QUERY"
COMPOSIO_INSPECT_URL_TOOL = "GOOGLE_SEARCH_CONSOLE_INSPECT_URL"
_COMPOSIO_EXECUTE_BASE = "https://backend.composio.dev/api/v3.1/tools/execute"
MAX_ANALYTICS_ROWS = 10
PREFERRED_GSC_HOST = "assafweb.com"


class SearchAnalyticsRow(BaseModel):
    page: str = ""
    query: str = ""
    clicks: str | None = None
    impressions: str | None = None
    ctr: str | None = None
    position: str | None = None


class UrlInspectionResult(BaseModel):
    url: str
    indexing_status: str = ""
    coverage_state: str = ""


class SearchConsolePort(Protocol):
    def list_sites(self) -> list[str]: ...

    def query_search_analytics(
        self,
        *,
        start_date: str,
        end_date: str,
        dimensions: list[str],
    ) -> list[SearchAnalyticsRow]: ...

    def inspect_url(self, url: str) -> UrlInspectionResult | None: ...


class DisabledSearchConsolePort:
    def list_sites(self) -> list[str]:
        return []

    def query_search_analytics(
        self,
        *,
        start_date: str,
        end_date: str,
        dimensions: list[str],
    ) -> list[SearchAnalyticsRow]:
        del start_date, end_date, dimensions
        return []

    def inspect_url(self, url: str) -> UrlInspectionResult | None:
        del url
        return None


class FakeSearchConsolePort:
    def __init__(
        self,
        *,
        sites: list[str] | None = None,
        analytics_rows: list[SearchAnalyticsRow] | None = None,
        inspection: UrlInspectionResult | None = None,
    ) -> None:
        self._sites = list(sites or [])
        self._analytics_rows = list(analytics_rows or [])
        self._inspection = inspection
        self.last_analytics_args: dict[str, object] | None = None

    def list_sites(self) -> list[str]:
        return list(self._sites)

    def query_search_analytics(
        self,
        *,
        start_date: str,
        end_date: str,
        dimensions: list[str],
    ) -> list[SearchAnalyticsRow]:
        self.last_analytics_args = {
            "start_date": start_date,
            "end_date": end_date,
            "dimensions": list(dimensions),
        }
        return list(self._analytics_rows)

    def inspect_url(self, url: str) -> UrlInspectionResult | None:
        del url
        return self._inspection


class ComposioSearchConsolePort:
    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        site_url: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._site_url = site_url.strip()
        self._client = client

    def _resolved_site_url(self) -> str:
        if self._site_url:
            return self._site_url
        self._site_url = pick_gsc_site(self.list_sites())
        return self._site_url

    def list_sites(self) -> list[str]:
        body = self._execute(COMPOSIO_LIST_SITES_TOOL, {})
        return _map_site_list(body)

    def query_search_analytics(
        self,
        *,
        start_date: str,
        end_date: str,
        dimensions: list[str],
    ) -> list[SearchAnalyticsRow]:
        site_url = self._resolved_site_url()
        if not site_url:
            return []
        arguments: dict[str, Any] = {
            "siteUrl": site_url,
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions[:2],
            "rowLimit": MAX_ANALYTICS_ROWS,
        }
        body = self._execute(COMPOSIO_SEARCH_ANALYTICS_TOOL, arguments)
        return _map_analytics_rows(body, dimensions)

    def inspect_url(self, url: str) -> UrlInspectionResult | None:
        trimmed = url.strip()
        if not trimmed:
            return None
        site_url = self._resolved_site_url()
        if not site_url:
            return None
        arguments = {
            "siteUrl": site_url,
            "inspectionUrl": trimmed,
        }
        body = self._execute(COMPOSIO_INSPECT_URL_TOOL, arguments)
        return _map_inspection(trimmed, body)

    def _execute(self, tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_GSC_VERSION,
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
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            return None


def _metric_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _map_site_list(data: dict[str, Any] | None) -> list[str]:
    if data is None:
        return []
    entries = data.get("siteEntry") or data.get("sites") or data.get("items")
    if not isinstance(entries, list):
        return []
    sites: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            site = entry.strip()
        elif isinstance(entry, dict):
            raw = entry.get("siteUrl") or entry.get("url")
            site = raw.strip() if isinstance(raw, str) else ""
        else:
            site = ""
        if site:
            sites.append(site)
        if len(sites) >= MAX_ANALYTICS_ROWS:
            break
    return sites


def pick_gsc_site(sites: list[str], *, preferred: str = "") -> str:
    """Use leftover env if set; else AssafWeb; else the first listed site."""
    explicit = preferred.strip()
    if explicit:
        return explicit
    for site in sites:
        if PREFERRED_GSC_HOST in site.lower():
            return site
    return sites[0] if sites else ""


def _map_analytics_rows(
    data: dict[str, Any] | None,
    dimensions: list[str],
) -> list[SearchAnalyticsRow]:
    if data is None:
        return []
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    mapped: list[SearchAnalyticsRow] = []
    dim_keys = [dim.lower() for dim in dimensions]
    for row in rows:
        if not isinstance(row, dict):
            continue
        keys = row.get("keys")
        key_list = keys if isinstance(keys, list) else []
        page = ""
        query = ""
        for index, dim in enumerate(dim_keys):
            if index >= len(key_list):
                continue
            val = key_list[index]
            text = val.strip() if isinstance(val, str) else ""
            if dim in {"page", "landingpage"}:
                page = text
            elif dim in {"query", "searchquery"}:
                query = text
        mapped.append(
            SearchAnalyticsRow(
                page=page,
                query=query,
                clicks=_metric_str(row.get("clicks")),
                impressions=_metric_str(row.get("impressions")),
                ctr=_metric_str(row.get("ctr")),
                position=_metric_str(row.get("position")),
            )
        )
        if len(mapped) >= MAX_ANALYTICS_ROWS:
            break
    return mapped


def _map_inspection(url: str, data: dict[str, Any] | None) -> UrlInspectionResult | None:
    if data is None:
        return None
    inspection = data.get("inspectionResult")
    if not isinstance(inspection, dict):
        inspection = data
    index_status = inspection.get("indexStatusResult")
    status_text = ""
    coverage = ""
    if isinstance(index_status, dict):
        verdict = index_status.get("verdict") or index_status.get("indexingState")
        if isinstance(verdict, str):
            status_text = verdict.strip()
        coverage_raw = index_status.get("coverageState")
        if isinstance(coverage_raw, str):
            coverage = coverage_raw.strip()
    if not status_text and not coverage:
        return None
    return UrlInspectionResult(
        url=url,
        indexing_status=status_text,
        coverage_state=coverage,
    )


def _sanitize_page_label(text: str) -> str:
    cleaned = " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())
    return cleaned[:120]


def _gsc_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "gsc_search_metrics",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="gsc_search_analytics",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def build_search_console_port(settings: Settings) -> SearchConsolePort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioSearchConsolePort(
            api_key=api_key,
            user_id=user_id,
            site_url=settings.gsc_site_url.strip(),
        )
    return DisabledSearchConsolePort()


def format_gsc_rows_block(rows: list[SearchAnalyticsRow]) -> str:
    lines: list[str] = []
    for row in rows:
        label = _sanitize_page_label(row.page or row.query)
        if not label:
            continue
        parts: list[str] = [label]
        if row.impressions is not None:
            parts.append(f"הצגות {row.impressions}")
        if row.clicks is not None:
            parts.append(f"קליקים {row.clicks}")
        if row.ctr is not None:
            parts.append(f"CTR {row.ctr}")
        if len(parts) > 1:
            lines.append(" — ".join(parts))
        if len(lines) >= MAX_ANALYTICS_ROWS:
            break
    if not lines:
        return ""
    return "נתוני חיפוש (GSC):\n" + "\n".join(f"- {line}" for line in lines)


def _parse_ctr(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace("%", "").strip())
    except ValueError:
        return None


def find_weak_ctr_page(rows: list[SearchAnalyticsRow]) -> SearchAnalyticsRow | None:
    """Lowest CTR among rows with parseable impressions — compare returned rows only."""
    candidates: list[tuple[float, SearchAnalyticsRow]] = []
    for row in rows:
        impressions = _parse_ctr(row.impressions)
        ctr = _parse_ctr(row.ctr)
        if impressions is None or impressions <= 0 or ctr is None:
            continue
        candidates.append((ctr, row))
    if len(candidates) < 2:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]
