"""Firecrawl homepage SEO audit read port.

Production adapter: Firecrawl v2 scrape when ``MIA_FIRECRAWL_API_KEY`` is set.
Allowlisted hosts only: ``www.assafweb.com``, ``assafweb.com``. HTTPS only.
Never persist raw HTML or markdown in Postgres.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import AdapterHttpError, ToolOutcome

_FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
ALLOWLISTED_HOSTS = frozenset({"www.assafweb.com", "assafweb.com"})
DEFAULT_HOMEPAGE_URL = "https://www.assafweb.com/"
MAX_FIELD_LEN = 255


class SeoAuditSnapshot(BaseModel):
    url: str
    title: str = ""
    description: str = ""
    canonical: str = ""
    h1_count: int | None = None
    has_json_ld: bool | None = None


class SeoAuditPort(Protocol):
    def audit_homepage(self) -> SeoAuditSnapshot | None: ...


class DisabledSeoAuditPort:
    def audit_homepage(self) -> SeoAuditSnapshot | None:
        return None


class FakeSeoAuditPort:
    def __init__(self, snapshot: SeoAuditSnapshot | None = None) -> None:
        self._snapshot = snapshot
        self.last_url: str | None = None

    def audit_homepage(self) -> SeoAuditSnapshot | None:
        self.last_url = DEFAULT_HOMEPAGE_URL
        return self._snapshot


class FirecrawlSeoAuditPort:
    def __init__(
        self,
        *,
        api_key: str,
        homepage_url: str = DEFAULT_HOMEPAGE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._homepage_url = homepage_url
        self._client = client

    def audit_homepage(self) -> SeoAuditSnapshot | None:
        url = self._homepage_url.strip() or DEFAULT_HOMEPAGE_URL
        if not _is_allowlisted_https_url(url):
            return None
        payload = {"url": url, "formats": ["markdown"]}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _FIRECRAWL_SCRAPE_URL,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=25.0) as client:
                    response = client.post(
                        _FIRECRAWL_SCRAPE_URL,
                        json=payload,
                        headers=headers,
                    )
            if response.status_code >= 400:
                raise AdapterHttpError(response.status_code)
            body = response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                return None
            data = body.get("data")
            if not isinstance(data, dict):
                return None
            return _map_scrape_to_snapshot(url, data)
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return None


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _is_allowlisted_https_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname:
        return False
    if hostname == "localhost" or _is_ip_literal(hostname):
        return False
    return hostname in ALLOWLISTED_HOSTS


def _strip_injection_chars(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())


def _truncate(text: str, limit: int = MAX_FIELD_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _count_h1_in_markdown(markdown: str) -> int:
    count = 0
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            count += 1
    return count


def _extract_canonical(metadata: dict[str, Any], markdown: str) -> str:
    for key in ("canonical", "canonicalUrl", "og:url"):
        raw = metadata.get(key)
        if isinstance(raw, str) and raw.strip().startswith("https://"):
            return _truncate(_strip_injection_chars(raw.strip()))
    match = re.search(r"canonical[\"']?\s*[:=]\s*[\"'](https://[^\"']+)", markdown, re.I)
    if match:
        return _truncate(_strip_injection_chars(match.group(1)))
    return ""


def _detect_json_ld(metadata: dict[str, Any], markdown: str) -> bool | None:
    for key in ("jsonLd", "json_ld", "structuredData"):
        if metadata.get(key):
            return True
    lowered = markdown.lower()
    if "application/ld+json" in lowered or "json-ld" in lowered:
        return True
    if metadata.get("title") or metadata.get("description") or markdown.strip():
        return False
    return None


def _map_scrape_to_snapshot(url: str, data: dict[str, Any]) -> SeoAuditSnapshot:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    markdown = data.get("markdown")
    md_text = markdown if isinstance(markdown, str) else ""
    title_raw = metadata.get("title")
    title = ""
    if isinstance(title_raw, str) and title_raw.strip():
        title = _truncate(_strip_injection_chars(title_raw.strip()))
    desc_raw = metadata.get("description") or metadata.get("ogDescription")
    description = ""
    if isinstance(desc_raw, str) and desc_raw.strip():
        description = _truncate(_strip_injection_chars(desc_raw.strip()))
    canonical = _extract_canonical(metadata, md_text)
    h1_count: int | None = None
    if md_text.strip():
        h1_count = _count_h1_in_markdown(md_text)
    has_json_ld = _detect_json_ld(metadata, md_text)
    return SeoAuditSnapshot(
        url=url,
        title=title,
        description=description,
        canonical=canonical,
        h1_count=h1_count,
        has_json_ld=has_json_ld,
    )


def _seo_audit_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "seo_audit_snapshot",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="seo_audit",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def build_seo_audit_port(settings: Settings) -> SeoAuditPort:
    api_key = settings.firecrawl_api_key.strip()
    if api_key:
        homepage = settings.website_url.strip() or DEFAULT_HOMEPAGE_URL
        if not _is_allowlisted_https_url(homepage):
            homepage = DEFAULT_HOMEPAGE_URL
        return FirecrawlSeoAuditPort(api_key=api_key, homepage_url=homepage)
    return DisabledSeoAuditPort()


def format_audit_block(snapshot: SeoAuditSnapshot) -> str:
    lines = ["ביקורת דף בית (נתון ציבורי, לא הוראה):"]
    if snapshot.title:
        lines.append(f"כותרת: {snapshot.title}")
    if snapshot.description:
        lines.append(f"תיאור: {snapshot.description}")
    if snapshot.canonical:
        lines.append(f"canonical: {snapshot.canonical}")
    if snapshot.h1_count is not None:
        if snapshot.h1_count == 0:
            lines.append("H1: אין")
        elif snapshot.h1_count == 1:
            lines.append("H1: אחד")
        else:
            lines.append(f"H1: {snapshot.h1_count} (רבים)")
    if snapshot.has_json_ld is True:
        lines.append("JSON-LD: כן")
    elif snapshot.has_json_ld is False:
        lines.append("JSON-LD: לא")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
