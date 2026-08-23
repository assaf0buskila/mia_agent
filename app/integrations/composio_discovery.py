"""Resolve integration resource ids from Composio instead of hand-configured env vars.

Composio already holds an authenticated connection for each toolkit. The resource id —
which Search Console property, which GA4 property, which ad account — is the one thing
still pasted into `mia/prod` by hand, and it is the piece most likely to be wrong or stale.
Each toolkit publishes a zero-argument list action, so Mia can just ask.

Three deliberate limits:

- **Reads only.** `GOOGLESHEETS_SEARCH_SPREADSHEETS` exists and works, but the Sheets
  mirror is a *write* target. Auto-picking a spreadsheet to write into is not a
  convenience, it is a way to scribble on the wrong document, so it stays explicit.
- **Never guess between candidates.** One result is an answer. Several results without a
  preference rule is an ambiguity, and the port stays disabled rather than choosing.
- **Env var always wins.** Discovery only runs when the setting is blank, so an explicit
  override remains possible and nothing changes for an already-configured deployment.

The published tool docs specify only the `{data, error, successful}` envelope; the inner
provider passthrough is not documented. Parsing is therefore shape-tolerant — it accepts
the documented Google/Meta field names and falls back to a recursive scan for values that
match the id pattern. `scripts/probe_composio_discovery.py` prints what a live account
actually returns so the parsers can be tightened against real output.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.core.config import Settings
from app.domain.tools import AdapterHttpError
from app.integrations.ga4 import COMPOSIO_GA4_VERSION
from app.integrations.meta_ads import COMPOSIO_METAADS_VERSION
from app.integrations.search_console import COMPOSIO_GSC_VERSION

COMPOSIO_EXECUTE_BASE = "https://backend.composio.dev/api/v3.1/tools/execute"
COMPOSIO_CONNECTED_ACCOUNTS_URL = "https://backend.composio.dev/api/v3.1/connected_accounts"

# Zero-argument discovery actions, one per toolkit.
GSC_LIST_SITES_TOOL = "GOOGLE_SEARCH_CONSOLE_LIST_SITES"
GA4_LIST_ACCOUNT_SUMMARIES_TOOL = "GOOGLE_ANALYTICS_LIST_ACCOUNT_SUMMARIES"
METAADS_GET_AD_ACCOUNTS_TOOL = "METAADS_GET_AD_ACCOUNTS"

_TIMEOUT = 20.0
_MAX_SCAN_DEPTH = 6
_MAX_CANDIDATES = 25

# GA4 property resource names look like "properties/123456789".
_GA4_PROPERTY_RE = re.compile(r"^properties/(\d{4,})$")
_GA4_BARE_ID_RE = re.compile(r"^\d{6,}$")
# Meta returns both "act_123..." (id) and "123..." (account_id).
_META_ACT_RE = re.compile(r"^act_(\d{5,})$")
# Search Console accepts URL-prefix and domain properties.
_SITE_RE = re.compile(r"^(https?://\S+|sc-domain:\S+)$")


class DiscoveryResult:
    """One resolution attempt. `value` is empty unless exactly one candidate was found."""

    __slots__ = ("value", "candidates", "error")

    def __init__(
        self, *, value: str = "", candidates: tuple[str, ...] = (), error: str = ""
    ) -> None:
        self.value = value
        self.candidates = candidates
        self.error = error

    @property
    def resolved(self) -> bool:
        return bool(self.value)

    @property
    def ambiguous(self) -> bool:
        return not self.value and len(self.candidates) > 1

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"DiscoveryResult(value={self.value!r}, "
            f"candidates={self.candidates!r}, error={self.error!r})"
        )


def _unwrap(body: object) -> Any:
    """Return the provider payload from Composio's `{data, error, successful}` envelope.

    A 200 with `successful: false` is a provider-level failure, so the status code alone
    is not enough. `data` is sometimes delivered as a JSON string.
    """
    if not isinstance(body, dict):
        return None
    if body.get("successful") is not True:
        return None
    data = body.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    return data


def _scan_strings(payload: Any, *, depth: int = 0) -> list[str]:
    """Collect every string in a nested payload, breadth-limited.

    The fallback for undocumented response shapes: rather than guessing a key, look at
    every value and keep the ones that match the id pattern.
    """
    if depth > _MAX_SCAN_DEPTH:
        return []
    found: list[str] = []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        for value in payload.values():
            found.extend(_scan_strings(value, depth=depth + 1))
    elif isinstance(payload, list):
        for item in payload[:_MAX_CANDIDATES]:
            found.extend(_scan_strings(item, depth=depth + 1))
    return found


def _dedupe(values: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        cleaned = value.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return tuple(seen)[:_MAX_CANDIDATES]


def extract_sites(payload: Any) -> tuple[str, ...]:
    """Search Console properties. Documented shape is `siteEntry[].siteUrl`."""
    entries = None
    if isinstance(payload, dict):
        entries = payload.get("siteEntry") or payload.get("sites") or payload.get("items")
    values: list[str] = []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, str):
                values.append(entry)
            elif isinstance(entry, dict):
                raw = entry.get("siteUrl") or entry.get("url") or entry.get("site_url")
                if isinstance(raw, str):
                    values.append(raw)
    if not values:
        values = _scan_strings(payload)
    return _dedupe([value for value in values if _SITE_RE.match(value.strip())])


def extract_ga4_properties(payload: Any) -> tuple[str, ...]:
    """GA4 property ids, returned bare (no `properties/` prefix) to match the adapter."""
    values = _scan_strings(payload)
    properties: list[str] = []
    for value in values:
        cleaned = value.strip()
        match = _GA4_PROPERTY_RE.match(cleaned)
        if match:
            properties.append(match.group(1))
        elif _GA4_BARE_ID_RE.match(cleaned):
            properties.append(cleaned)
    return _dedupe(properties)


def extract_meta_ad_accounts(payload: Any) -> tuple[str, ...]:
    """Meta ad account ids, returned bare so the caller can format them as it needs."""
    values = _scan_strings(payload)
    accounts: list[str] = []
    for value in values:
        cleaned = value.strip()
        match = _META_ACT_RE.match(cleaned)
        if match:
            accounts.append(match.group(1))
        elif cleaned.isdigit() and len(cleaned) >= 8:
            accounts.append(cleaned)
    return _dedupe(accounts)


def choose_site(candidates: tuple[str, ...], *, website_url: str) -> str:
    """Prefer the property that matches the configured website before giving up.

    A Search Console account commonly holds several properties for one site (http, https,
    www, and a domain property). Matching on host makes that the normal case rather than
    an ambiguity, while a genuinely different second site still refuses to auto-resolve.
    """
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]
    host = ""
    parsed = website_url.strip()
    if parsed:
        host = re.sub(r"^https?://", "", parsed).strip("/").lower()
        host = host[4:] if host.startswith("www.") else host
    if not host:
        return ""
    matches = [
        candidate
        for candidate in candidates
        if host in candidate.lower().replace("www.", "")
    ]
    if len(matches) == 1:
        return matches[0]
    # Several properties for the same site: prefer the domain property, then https.
    domain_props = [item for item in matches if item.lower().startswith("sc-domain:")]
    if len(domain_props) == 1:
        return domain_props[0]
    https_props = [item for item in matches if item.lower().startswith("https://")]
    if len(https_props) == 1:
        return https_props[0]
    return ""


class ComposioDiscovery:
    """Executes the zero-argument list actions. Read-only; never writes anything."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        website_url: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._website_url = website_url
        self._client = client

    def enabled(self) -> bool:
        return bool(self._api_key.strip() and self._user_id.strip())

    def _execute(self, tool_slug: str, version: str) -> Any:
        payload = {
            "user_id": self._user_id,
            "version": version,
            "arguments": {},
        }
        headers = {"x-api-key": self._api_key, "Content-Type": "application/json"}
        url = f"{COMPOSIO_EXECUTE_BASE}/{tool_slug}"
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=_TIMEOUT) as client:
                    response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            return _unwrap(response.json())
        except ValueError:
            return None

    def _resolve(self, tool_slug: str, version: str, extractor) -> DiscoveryResult:
        try:
            payload = self._execute(tool_slug, version)
        except AdapterHttpError as exc:
            status = exc.status_code
            # 4xx here is almost always "No connected account found for this user and
            # toolkit" — a configuration state, not an outage. Either way: stay disabled.
            return DiscoveryResult(error=f"http_{status}" if status else "http_error")
        if payload is None:
            return DiscoveryResult(error="unsuccessful")
        candidates = extractor(payload)
        if len(candidates) == 1:
            return DiscoveryResult(value=candidates[0], candidates=candidates)
        return DiscoveryResult(candidates=candidates)

    def search_console_site(self) -> DiscoveryResult:
        result = self._resolve(GSC_LIST_SITES_TOOL, COMPOSIO_GSC_VERSION, extract_sites)
        if result.resolved or not result.candidates:
            return result
        chosen = choose_site(result.candidates, website_url=self._website_url)
        if chosen:
            return DiscoveryResult(value=chosen, candidates=result.candidates)
        return result

    def ga4_property(self) -> DiscoveryResult:
        return self._resolve(
            GA4_LIST_ACCOUNT_SUMMARIES_TOOL, COMPOSIO_GA4_VERSION, extract_ga4_properties
        )

    def meta_ad_account(self) -> DiscoveryResult:
        return self._resolve(
            METAADS_GET_AD_ACCOUNTS_TOOL,
            COMPOSIO_METAADS_VERSION,
            extract_meta_ad_accounts,
        )

    def connected_toolkits(self) -> tuple[str, ...]:
        """Toolkits with an ACTIVE connection, from the Connected Accounts API.

        Cheaper than a failed execute, and it distinguishes "never connected" from
        "connected but returned nothing". Never returns tokens or scopes.
        """
        headers = {"x-api-key": self._api_key}
        params = {"user_ids[]": self._user_id, "statuses[]": "ACTIVE", "limit": "100"}
        try:
            if self._client is not None:
                response = self._client.get(
                    COMPOSIO_CONNECTED_ACCOUNTS_URL, params=params, headers=headers
                )
            else:
                with httpx.Client(timeout=_TIMEOUT) as client:
                    response = client.get(
                        COMPOSIO_CONNECTED_ACCOUNTS_URL, params=params, headers=headers
                    )
        except httpx.HTTPError:
            return ()
        if response.status_code >= 400:
            return ()
        try:
            body = response.json()
        except ValueError:
            return ()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return ()
        slugs: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            toolkit = item.get("toolkit")
            slug = toolkit.get("slug") if isinstance(toolkit, dict) else None
            if isinstance(slug, str) and slug.strip():
                slugs.append(slug.strip().lower())
        return _dedupe(slugs)


def build_discovery(settings: Settings) -> ComposioDiscovery | None:
    """None unless discovery is explicitly enabled and Composio is configured.

    Off by default on purpose. Ports are constructed per request, so an enabled discovery
    costs one network call per process on first use — acceptable, but it should be a
    decision rather than a surprise. Run `scripts/probe_composio_discovery.py` first to
    confirm the account resolves cleanly, then set `MIA_COMPOSIO_DISCOVERY=true`.
    """
    if not settings.composio_discovery:
        return None
    if not settings.composio_ready():
        return None
    return ComposioDiscovery(
        api_key=settings.composio_api_key.strip(),
        user_id=settings.composio_user_id.strip(),
        website_url=settings.website_url,
    )


# Resolved ids are stable for the life of the process. Discovery is one HTTP call per
# toolkit per boot, never per request.
_CACHE: dict[str, str] = {}


def cached_resolve(key: str, resolver) -> str:
    """Resolve once per process. A failed or ambiguous lookup is cached as empty."""
    if key in _CACHE:
        return _CACHE[key]
    try:
        result = resolver()
    except Exception:  # noqa: BLE001 - discovery must never break port construction
        result = DiscoveryResult(error="exception")
    _CACHE[key] = result.value
    return result.value


def reset_cache() -> None:
    """Test hook. Also useful after a Composio connection changes."""
    _CACHE.clear()
