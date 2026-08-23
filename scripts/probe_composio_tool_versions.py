"""Diagnose why a Composio tool execute returns 404.

    uv run python scripts/probe_composio_tool_versions.py

The three resource-discovery tools all returned 404 in production while eight toolkits were
connected and other tools in the *same* toolkits worked. The version strings were copied
from those working tools, on the assumption that a version is per-toolkit. If it is
actually per-tool, a valid toolkit version 404s for a different tool in the same toolkit.

This separates the possible causes instead of guessing between them, by trying each tool
four ways and reporting which succeed:

  1. GET  /api/v3.1/tools/{slug}          -> does the slug exist at all, and what version
                                             does Composio itself report for it?
  2. POST /execute with the pinned version -> current production behaviour
  3. POST /execute with the reported version (when 1 gives one)
  4. POST /execute with NO version field   -> what the default actually resolves to

Read-only: every tool called is a list action. Never prints the API key.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from app.core.config import get_settings
from app.integrations.composio_discovery import (
    GA4_LIST_ACCOUNT_SUMMARIES_TOOL,
    GSC_LIST_SITES_TOOL,
    METAADS_GET_AD_ACCOUNTS_TOOL,
)
from app.integrations.ga4 import COMPOSIO_GA4_VERSION
from app.integrations.meta_ads import COMPOSIO_METAADS_VERSION
from app.integrations.search_console import COMPOSIO_GSC_VERSION

BASE = "https://backend.composio.dev/api/v3.1"
_TIMEOUT = 30.0
# Tools that already work in production, as controls: if a control also fails, the problem
# is the account or the request shape, not the discovery slugs.
CONTROLS = (
    ("google_search_console", "GOOGLE_SEARCH_CONSOLE_SEARCH_ANALYTICS_QUERY", COMPOSIO_GSC_VERSION),
)
TARGETS = (
    ("google_search_console", GSC_LIST_SITES_TOOL, COMPOSIO_GSC_VERSION),
    ("google_analytics", GA4_LIST_ACCOUNT_SUMMARIES_TOOL, COMPOSIO_GA4_VERSION),
    ("metaads", METAADS_GET_AD_ACCOUNTS_TOOL, COMPOSIO_METAADS_VERSION),
)


def _headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "Content-Type": "application/json"}


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"code={error.get('code')} message={error.get('message')}"[:250]
    if isinstance(error, str):
        return error[:250]
    return json.dumps(body)[:200]


def _find_versions(payload: Any, depth: int = 0) -> list[str]:
    """Pull anything that looks like a YYYYMMDD_NN version out of a tool record."""
    found: list[str] = []
    if depth > 5:
        return found
    if isinstance(payload, str):
        cleaned = payload.strip()
        if len(cleaned) == 11 and cleaned[:8].isdigit() and cleaned[8] == "_":
            found.append(cleaned)
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("version", "versions", "latest_version", "toolkit_version"):
                found.extend(_find_versions(value, depth + 1))
            else:
                found.extend(_find_versions(value, depth + 1))
    elif isinstance(payload, list):
        for item in payload[:20]:
            found.extend(_find_versions(item, depth + 1))
    return list(dict.fromkeys(found))


def _get_tool(client: httpx.Client, key: str, slug: str) -> str:
    """Ask Composio about the tool itself. Returns a reported version, or ''."""
    try:
        response = client.get(f"{BASE}/tools/{slug}", headers=_headers(key))
    except httpx.HTTPError as exc:
        print(f"    GET  /tools/{slug}: TRANSPORT FAILURE ({type(exc).__name__})")
        return ""
    if response.status_code != 200:
        print(f"    GET  slug exists? NO   http={response.status_code} {_error_text(response)}")
        return ""
    try:
        body = response.json()
    except ValueError:
        print("    GET  slug exists? yes, but the body was not JSON")
        return ""
    versions = _find_versions(body)
    print(f"    GET  slug exists? YES  versions_reported={versions or 'none found'}")
    return versions[0] if versions else ""


def _execute(
    client: httpx.Client, key: str, slug: str, user_id: str, version: str | None
) -> bool:
    payload: dict[str, Any] = {"user_id": user_id, "arguments": {}}
    label = "no version"
    if version:
        payload["version"] = version
        label = f"version={version}"
    try:
        response = client.post(
            f"{BASE}/tools/execute/{slug}", json=payload, headers=_headers(key)
        )
    except httpx.HTTPError as exc:
        print(f"    POST {label}: TRANSPORT FAILURE ({type(exc).__name__})")
        return False
    if response.status_code != 200:
        print(f"    POST {label}: http={response.status_code} {_error_text(response)}")
        return False
    try:
        body = response.json()
    except ValueError:
        print(f"    POST {label}: 200 but body was not JSON")
        return False
    successful = body.get("successful") if isinstance(body, dict) else None
    if successful is not True:
        print(f"    POST {label}: 200 successful=false {_error_text(response)}")
        return False
    keys = list(body.get("data", {}))[:8] if isinstance(body.get("data"), dict) else "non-dict"
    print(f"    POST {label}: OK  data keys={keys}")
    return True


def _probe(client: httpx.Client, key: str, user_id: str, toolkit: str, slug: str, pinned: str):
    print(f"\n[{toolkit}] {slug}")
    reported = _get_tool(client, key, slug)
    ok_pinned = _execute(client, key, slug, user_id, pinned)
    ok_reported = False
    if reported and reported != pinned:
        ok_reported = _execute(client, key, slug, user_id, reported)
    ok_none = _execute(client, key, slug, user_id, None)
    return ok_pinned, ok_reported, ok_none


def main() -> int:
    settings = get_settings()
    key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if not key or not user_id:
        print("Composio is not configured (MIA_COMPOSIO_API_KEY / MIA_COMPOSIO_USER_ID).")
        return 2

    print("Composio tool/version probe. Read-only; the API key is never printed.")
    results: dict[str, tuple[bool, bool, bool]] = {}
    with httpx.Client(timeout=_TIMEOUT) as client:
        print("\n=== CONTROLS (these already work in production) ===")
        for toolkit, slug, pinned in CONTROLS:
            results[slug] = _probe(client, key, user_id, toolkit, slug, pinned)
        print("\n=== DISCOVERY TOOLS (these 404 in production) ===")
        for toolkit, slug, pinned in TARGETS:
            results[slug] = _probe(client, key, user_id, toolkit, slug, pinned)

    print("\n=== VERDICT ===")
    for slug, (pinned_ok, reported_ok, none_ok) in results.items():
        if pinned_ok:
            print(f"  {slug}: works as pinned — nothing to change")
        elif reported_ok:
            print(f"  {slug}: FIX = use the version Composio reports for this tool")
        elif none_ok:
            print(f"  {slug}: FIX = omit `version` and let Composio resolve the default")
        else:
            print(f"  {slug}: still failing every way — slug or account scope, not version")
    print(
        "\nIf the controls pass and the discovery tools fail every way, the slugs or the\n"
        "connection's granted scopes are the problem, not version pinning."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
