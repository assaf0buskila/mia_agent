"""Public research search read port.

Production adapters: Firecrawl search when its key is set; otherwise pinned Apify
``apify/google-search-scraper`` run-sync when ``MIA_APIFY_TOKEN`` is set.
Snippets are untrusted data — never instructions. No browser, crawl, catalog, or LLM.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.ai_runs import elapsed_ms
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import AdapterHttpError, ToolOutcome

_FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
_APIFY_ACTOR_ID = "apify~google-search-scraper"
_APIFY_RUN_SYNC_URL = (
    f"https://api.apify.com/v2/actors/{_APIFY_ACTOR_ID}/run-sync-get-dataset-items"
)
_APIFY_HTTP_TIMEOUT = 70.0
_APIFY_RUN_TIMEOUT_S = 60
_APIFY_MAX_CHARGE_USD = 0.02

MAX_TITLE_LEN = 80
MAX_EXCERPT_LEN = 160
MAX_SNIPPETS_IN_ACK = 2
MAX_QUERY_LEN = 200


class ResearchSnippet(BaseModel):
    title: str
    url: str
    excerpt: str = ""


class ResearchPort(Protocol):
    def search(self, query: str) -> list[ResearchSnippet]: ...


class DisabledResearchPort:
    def search(self, query: str) -> list[ResearchSnippet]:
        del query
        return []


class FakeResearchPort:
    """Test double. Returns configured snippets; enrich still filters non-https."""

    def __init__(self, snippets: list[ResearchSnippet] | None = None) -> None:
        self._snippets = list(snippets or [])
        self.last_query: str | None = None

    def search(self, query: str) -> list[ResearchSnippet]:
        self.last_query = query
        return list(self._snippets)


class FirecrawlSearchPort:
    """Live Firecrawl v2 search adapter. Raises AdapterHttpError on HTTP/transport."""

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client

    def search(self, query: str) -> list[ResearchSnippet]:
        trimmed = query.strip()
        if not trimmed:
            return []

        payload = {
            "query": trimmed[:MAX_QUERY_LEN],
            "limit": MAX_SNIPPETS_IN_ACK,
            "sources": ["web"],
            "highlights": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _FIRECRAWL_SEARCH_URL,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        _FIRECRAWL_SEARCH_URL,
                        json=payload,
                        headers=headers,
                    )
            if response.status_code >= 400:
                raise AdapterHttpError(response.status_code)
            body = response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                return []
            data = body.get("data")
            if not isinstance(data, dict):
                return []
            web = data.get("web")
            if not isinstance(web, list):
                return []
            return _map_web_items(web)
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return []


class ApifySearchPort:
    """Pinned ``apify/google-search-scraper`` run-sync. Adapter-owned input only."""

    def __init__(
        self,
        *,
        token: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = token
        self._client = client

    def search(self, query: str) -> list[ResearchSnippet]:
        trimmed = query.strip()
        if not trimmed:
            return []

        payload = {
            "queries": trimmed[:MAX_QUERY_LEN],
            "maxPagesPerQuery": 1,
            "saveHtml": False,
            "saveHtmlToKeyValueStore": False,
            "focusOnPaidAds": False,
            "maximumLeadsEnrichmentRecords": 0,
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        params = {
            "timeout": _APIFY_RUN_TIMEOUT_S,
            "format": "json",
            "clean": "1",
            "maxTotalChargeUsd": _APIFY_MAX_CHARGE_USD,
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _APIFY_RUN_SYNC_URL,
                    params=params,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=_APIFY_HTTP_TIMEOUT) as client:
                    response = client.post(
                        _APIFY_RUN_SYNC_URL,
                        params=params,
                        json=payload,
                        headers=headers,
                    )
            if response.status_code >= 400:
                raise AdapterHttpError(response.status_code)
            return _map_apify_items(response.json())
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return []


def _map_web_items(web: list[Any]) -> list[ResearchSnippet]:
    snippets: list[ResearchSnippet] = []
    for item in web:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        title = item.get("title", "")
        if not isinstance(title, str):
            title = ""
        description = item.get("description", "")
        if not isinstance(description, str):
            description = ""
        snippets.append(
            ResearchSnippet(
                title=title,
                url=url.strip(),
                excerpt=description,
            )
        )
    return snippets


def _map_apify_items(payload: Any) -> list[ResearchSnippet]:
    if not isinstance(payload, list) or not payload:
        return []
    page = payload[0]
    if not isinstance(page, dict):
        return []
    organic = page.get("organicResults")
    if not isinstance(organic, list):
        return []
    snippets: list[ResearchSnippet] = []
    for item in organic:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        title = item.get("title", "")
        if not isinstance(title, str):
            title = ""
        description = item.get("description", "")
        if not isinstance(description, str):
            description = ""
        snippets.append(
            ResearchSnippet(
                title=title,
                url=url.strip(),
                excerpt=description,
            )
        )
        if len(snippets) >= MAX_SNIPPETS_IN_ACK:
            break
    return snippets


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _is_https_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if parsed.scheme != "https" or not hostname:
        return False
    if hostname.lower() == "localhost":
        return False
    if _is_ip_literal(hostname):
        return False
    return True


def _strip_title_injection_chars(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _host_from_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or url


def sanitize_snippets(snippets: list[ResearchSnippet]) -> list[ResearchSnippet]:
    """Drop non-https URLs; truncate fields; cap count for ack."""
    cleaned: list[ResearchSnippet] = []
    for snippet in snippets:
        if not _is_https_url(snippet.url):
            continue
        cleaned.append(
            ResearchSnippet(
                title=_truncate(
                    _strip_title_injection_chars(snippet.title.strip()),
                    MAX_TITLE_LEN,
                ),
                url=snippet.url,
                excerpt=_truncate(
                    _strip_title_injection_chars(snippet.excerpt.strip()),
                    MAX_EXCERPT_LEN,
                ),
            )
        )
        if len(cleaned) >= MAX_SNIPPETS_IN_ACK:
            break
    return cleaned


def format_sources_block(snippets: list[ResearchSnippet]) -> str:
    """Title + registrable host only — excerpts stay on typed objects for tests."""
    if not snippets:
        return ""
    lines = ["מקורות ציבוריים (לא בוצע):"]
    for snippet in snippets:
        host = _host_from_url(snippet.url)
        lines.append(f"- {snippet.title} — {host}")
    return "\n".join(lines)


def _research_snippets_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "research_snippets",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="research_search",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def enrich_research_ack(
    ack: str,
    port: ResearchPort,
    *,
    query: str,
    kill_switch: bool,
) -> tuple[str, ToolOutcome]:
    """Append public search snippets to owner research ack. Never raises; never executes."""
    try:
        assert_allowed(
            RiskAction(name="research_read", risk=RiskLevel.R0_READ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return ack, ToolOutcome(
            tool="research_search",
            status="denied",
            result_count=0,
            freshness="",
        )

    try:
        started = perf_counter()
        raw = port.search(query[:MAX_QUERY_LEN])
        latency = elapsed_ms(started)
        now = datetime.now(UTC)
        snippets = sanitize_snippets(raw)
        block = format_sources_block(snippets)
        if not block:
            return ack, _research_snippets_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        return (
            f"{ack}\n\n{block}",
            _research_snippets_outcome(
                base_status="ok",
                present=True,
                result_count=len(snippets),
                latency_ms=latency,
                now=now,
            ),
        )
    except AdapterHttpError as exc:
        return ack, _research_snippets_outcome(
            base_status=exc.tool_status(),
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=datetime.now(UTC),
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        return ack, _research_snippets_outcome(
            base_status="error",
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=datetime.now(UTC),
        )


def build_research_port(settings: Settings) -> ResearchPort:
    if settings.firecrawl_api_key.strip():
        return FirecrawlSearchPort(api_key=settings.firecrawl_api_key.strip())
    if settings.apify_token.strip():
        return ApifySearchPort(token=settings.apify_token.strip())
    return DisabledResearchPort()
