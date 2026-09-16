"""Does the live site outrun the knowledge files Mia reads?

Two independent staleness gaps exist. Gap 1 -- Mia lags the *files* -- is fixed by
ingesting more often (`app/workers/ingest_knowledge.py`'s schedule) and is visible
in `/health` (`app/brain/store.py::BrainStore.list_knowledge_source_statuses`). Gap
2 is this module's job: the maintained files themselves can silently stop tracking
the live site, and `app/brain/knowledge.py` has no way to notice -- it only ever
reads the files, never the site that produced them.

This runs during the scheduled ingest, never per visitor request: one cheap `HEAD`
against the site root, one per configured knowledge source, comparing
`Last-Modified` headers. That header is an honest-but-weak signal on a static
host -- it can be absent, or a host/CDN can leave it unchanged across a real edit --
so a missing or unparsable header, or a failed request, always resolves to
"unknown", never a false "fresh". Nothing here crawls page content or logs a
fetched body; only the header value itself is kept, and only as stored data (never
written to a log line).
"""

from __future__ import annotations

from datetime import timedelta
from email.utils import parsedate_to_datetime
from typing import Literal, NamedTuple, Protocol

import httpx

StaleVerdict = Literal["fresh", "stale", "unknown"]

_TIMEOUT = 10.0
# The scheduled ingest itself now runs hourly, so ordinary clock/CDN skew between
# the site and a source's own header is minutes, not hours. Anything beyond this
# reflects a real gap between the site and the files, not measurement noise.
MATERIAL_STALENESS = timedelta(hours=6)


class SiteFreshnessPort(Protocol):
    def last_modified(self, url: str) -> str | None: ...


class HttpSiteFreshnessChecker:
    """A plain HTTP HEAD. Never raises -- any failure degrades to `None` (unknown)."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client

    def last_modified(self, url: str) -> str | None:
        try:
            if self._client is not None:
                response = self._client.head(url)
            else:
                with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                    response = client.head(url)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        value = response.headers.get("last-modified", "").strip()
        return value or None


class FakeSiteFreshnessChecker:
    """Test double mapping url -> a raw `Last-Modified` value, or `None` for missing."""

    def __init__(self, headers: dict[str, str | None]) -> None:
        self._headers = dict(headers)
        self.requested: list[str] = []

    def last_modified(self, url: str) -> str | None:
        self.requested.append(url)
        return self._headers.get(url)


def parse_http_date(value: str | None):
    """RFC 1123 `Last-Modified` -> an aware `datetime`, or `None` if unusable.

    A naive result (no timezone in the string) is treated as unusable too: an
    unanchored instant cannot be safely compared against another timezone's clock.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def compare_staleness(
    *, site_last_modified: str | None, source_last_modified: str | None
) -> StaleVerdict:
    """"stale" only when both headers parse and the site is materially newer.

    Either side missing or unparsable is "unknown" -- never a false "fresh": a
    silent drift is exactly the bug this module exists to catch.
    """
    site_dt = parse_http_date(site_last_modified)
    source_dt = parse_http_date(source_last_modified)
    if site_dt is None or source_dt is None:
        return "unknown"
    if site_dt - source_dt > MATERIAL_STALENESS:
        return "stale"
    return "fresh"


class SourceFreshness(NamedTuple):
    source_id: str
    site_last_modified: str
    source_last_modified: str
    stale: StaleVerdict


def check_site_freshness(
    *,
    website_url: str,
    sources: list[tuple[str, str]],
    checker: SiteFreshnessPort,
) -> list[SourceFreshness]:
    """One `HEAD` against the site root, one per configured source. Never raises.

    `sources` is `(source_id, url)` pairs, e.g. `app.brain.knowledge.source_urls`'s
    output, so the same source list drives ingestion and this check.
    """
    root = website_url.strip().rstrip("/") or website_url.strip()
    site_header = checker.last_modified(root) if root else None
    results: list[SourceFreshness] = []
    for source_id, url in sources:
        source_header = checker.last_modified(url)
        results.append(
            SourceFreshness(
                source_id=source_id,
                site_last_modified=site_header or "",
                source_last_modified=source_header or "",
                stale=compare_staleness(
                    site_last_modified=site_header,
                    source_last_modified=source_header,
                ),
            )
        )
    return results
