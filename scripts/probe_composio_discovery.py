"""Print what Composio actually returns for the resource-discovery actions.

    uv run python scripts/probe_composio_discovery.py

Composio publishes only the `{data, error, successful}` envelope for every action — the
inner provider payload is undocumented. The parsers in `app/integrations/composio_discovery.py`
are therefore written to be shape-tolerant, and this script is how that guess gets checked
against a real account so they can be tightened.

Reads credentials through `Settings` (i.e. from `.env` / Secrets Manager). It prints the
*shape* and the resolved ids — never the API key. Read-only: every action it calls is a
list action, and it writes nothing anywhere.
"""

from __future__ import annotations

import json
import sys

from app.core.config import get_settings
from app.integrations.composio_discovery import (
    GA4_LIST_ACCOUNT_SUMMARIES_TOOL,
    GSC_LIST_SITES_TOOL,
    ComposioDiscovery,
    extract_ga4_properties,
    extract_sites,
)
from app.integrations.ga4 import COMPOSIO_GA4_VERSION
from app.integrations.search_console import COMPOSIO_GSC_VERSION

MAX_PRINT_CHARS = 2000


def _outline(payload: object, depth: int = 0, path: str = "") -> list[str]:
    """Key outline of a payload, with scalar types instead of values.

    Values can carry account names and ids, so the outline is what gets shown by default;
    `--raw` prints the payload itself when you need the exact strings.
    """
    pad = "  " * depth
    if depth > 4:
        return [f"{pad}..."]
    lines: list[str] = []
    if isinstance(payload, dict):
        for key, value in list(payload.items())[:20]:
            if isinstance(value, dict | list):
                lines.append(f"{pad}{key}:")
                lines.extend(_outline(value, depth + 1, f"{path}.{key}"))
            else:
                lines.append(f"{pad}{key}: {type(value).__name__}")
    elif isinstance(payload, list):
        lines.append(f"{pad}[{len(payload)} items]")
        if payload:
            lines.extend(_outline(payload[0], depth + 1, f"{path}[0]"))
    else:
        lines.append(f"{pad}{type(payload).__name__}")
    return lines


def _probe(discovery, *, label: str, slug: str, version: str, extractor, raw: bool) -> bool:
    print(f"\n=== {label} ===")
    print(f"tool    : {slug}")
    print(f"version : {version}")
    try:
        payload = discovery._execute(slug, version)  # noqa: SLF001 - diagnostic script
    except Exception as exc:  # noqa: BLE001 - report and keep going
        print(f"result  : CALL FAILED ({type(exc).__name__}: {exc})")
        return False
    if payload is None:
        print("result  : no payload (successful != true, or unparseable)")
        return False
    print("shape   :")
    for line in _outline(payload, depth=2):
        print(line)
    if raw:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        print("raw     :")
        print(text[:MAX_PRINT_CHARS] + ("... [truncated]" if len(text) > MAX_PRINT_CHARS else ""))
    candidates = extractor(payload)
    print(f"parsed  : {len(candidates)} candidate(s) -> {list(candidates)}")
    if len(candidates) == 1:
        print("          RESOLVES cleanly, the env var can be removed")
    elif candidates:
        print("          AMBIGUOUS, Mia will refuse to guess and keep the port disabled")
    else:
        print("          PARSER FOUND NOTHING - the shape above needs a parser update")
    return bool(candidates)


def main() -> int:
    raw = "--raw" in sys.argv
    settings = get_settings()
    if not settings.composio_ready():
        print("Composio is not configured (MIA_COMPOSIO_API_KEY / MIA_COMPOSIO_USER_ID).")
        return 2
    # Probe constructs the client directly. `build_discovery` stays off unless
    # MIA_COMPOSIO_DISCOVERY=true — that default must not block this diagnostic.
    discovery = ComposioDiscovery(
        api_key=settings.composio_api_key.strip(),
        user_id=settings.composio_user_id.strip(),
        website_url=settings.website_url,
    )

    print("Composio resource discovery probe. Read-only; no key is printed.")
    active = discovery.connected_toolkits()
    print(f"\nactive connections ({len(active)}): {list(active)}")

    ok = 0
    ok += _probe(
        discovery,
        label="Search Console site  (replaces MIA_GSC_SITE_URL)",
        slug=GSC_LIST_SITES_TOOL,
        version=COMPOSIO_GSC_VERSION,
        extractor=extract_sites,
        raw=raw,
    )
    ok += _probe(
        discovery,
        label="GA4 property  (replaces MIA_GA4_PROPERTY_ID)",
        slug=GA4_LIST_ACCOUNT_SUMMARIES_TOOL,
        version=COMPOSIO_GA4_VERSION,
        extractor=extract_ga4_properties,
        raw=raw,
    )

    print(f"\n{ok}/2 discovery actions returned usable candidates.")
    print("Re-run with --raw to see the payloads if a parser found nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
