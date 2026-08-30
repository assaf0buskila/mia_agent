"""LinkedIn own-profile read port.

Production adapter: Composio ``LINKEDIN`` toolkit version ``20260724_00``,
pin ``LINKEDIN_GET_MY_INFO`` only when ``MIA_COMPOSIO_API_KEY`` and
``MIA_COMPOSIO_USER_ID`` are set. Managed OAuth **Yes**.
Never post, comment, delete, DM, or upload this slice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.errors import PermissionDenied, PolicyDenied
from app.domain.ai_runs import elapsed_ms
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import (
    AdapterHttpError,
    AdapterResponseError,
    AdapterSchemaError,
    ToolOutcome,
)

COMPOSIO_LINKEDIN_VERSION = "20260724_00"
COMPOSIO_GET_MY_INFO_TOOL = "LINKEDIN_GET_MY_INFO"
_COMPOSIO_EXECUTE_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_GET_MY_INFO_TOOL}"
)


class LinkedInProfile(BaseModel):
    name: str = ""
    headline: str = ""


class LinkedInPort(Protocol):
    def get_my_profile(self) -> LinkedInProfile | None: ...


class DisabledLinkedInPort:
    def get_my_profile(self) -> LinkedInProfile | None:
        return None


class ComposioLinkedInPort:
    """Live Composio execute adapter for LINKEDIN_GET_MY_INFO. Raises AdapterHttpError on HTTP."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._client = client

    def get_my_profile(self) -> LinkedInProfile | None:
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_LINKEDIN_VERSION,
            "arguments": {},
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _COMPOSIO_EXECUTE_URL,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        _COMPOSIO_EXECUTE_URL,
                        json=payload,
                        headers=headers,
                    )
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
            if not isinstance(data, dict):
                raise AdapterSchemaError()
            return _map_data_to_profile(data)
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            raise AdapterSchemaError() from None


class FakeLinkedInPort:
    """Test double. Returns configured snapshot or None."""

    def __init__(self, snapshot: LinkedInProfile | None = None) -> None:
        self._snapshot = snapshot

    def get_my_profile(self) -> LinkedInProfile | None:
        return self._snapshot


def _non_empty_str(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _localized_text(value: object) -> str | None:
    """Accept a plain string or LinkedIn `{localized: {locale: text}}` objects."""
    direct = _non_empty_str(value)
    if direct:
        return direct
    if not isinstance(value, dict):
        return None
    localized = value.get("localized")
    if isinstance(localized, dict):
        for item in localized.values():
            found = _non_empty_str(item)
            if found:
                return found
    return None


def _join_name_parts(*parts: object) -> str:
    strings: list[str] = []
    for part in parts:
        found = _localized_text(part)
        if found:
            strings.append(found)
    return " ".join(strings)


def _map_data_to_profile(data: dict[str, Any]) -> LinkedInProfile | None:
    name = _localized_text(data.get("name"))
    if not name:
        name = _join_name_parts(
            data.get("localizedFirstName"),
            data.get("localizedLastName"),
        )
    if not name:
        name = _join_name_parts(data.get("firstName"), data.get("lastName"))
    headline = _localized_text(data.get("headline"))
    if not headline:
        headline = _localized_text(data.get("localizedHeadline"))
    if not name and not headline:
        return None
    return LinkedInProfile(name=name or "", headline=headline or "")


def format_profile_line(profile: LinkedInProfile) -> str:
    """One-line snapshot. Missing fields are omitted, never invented."""
    if profile.name and profile.headline:
        return f"פרופיל: {profile.name} — {profile.headline}."
    if profile.headline:
        return f"פרופיל: {profile.headline}."
    if profile.name:
        return f"פרופיל: {profile.name}."
    return ""


def _linkedin_profile_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "linkedin_profile",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="linkedin_profile",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def enrich_linkedin_ack(
    ack: str,
    port: LinkedInPort,
    kill_switch: bool,
    *,
    principal: Principal,
) -> tuple[str, ToolOutcome]:
    """Append own-profile snapshot to owner linkedin ack. Never raises; never posts."""
    from app.capabilities.linkedin import linkedin_handlers

    now = datetime.now(UTC)
    started = perf_counter()
    try:
        payload = execute_capability(
            "linkedin.get_profile",
            principal=principal,
            args={},
            handlers=linkedin_handlers(port),
            kill_switch=kill_switch,
        )
        latency = elapsed_ms(started)
        if not payload.get("found"):
            return ack, _linkedin_profile_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        profile = LinkedInProfile(
            name=str(payload.get("name") or ""),
            headline=str(payload.get("headline") or ""),
        )
        line = format_profile_line(profile)
        if not line:
            return ack, _linkedin_profile_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        return (
            f"{ack}\n\n{line}",
            _linkedin_profile_outcome(
                base_status="ok",
                present=True,
                result_count=1,
                latency_ms=latency,
                now=now,
            ),
        )
    except PermissionDenied:
        return ack, ToolOutcome(
            tool="linkedin_profile",
            status="denied",
            result_count=0,
            freshness="",
        )
    except AdapterHttpError as exc:
        return ack, _linkedin_profile_outcome(
            base_status=exc.tool_status(),
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=now,
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        return ack, _linkedin_profile_outcome(
            base_status="error",
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=now,
        )


def build_linkedin_port(settings: Settings) -> LinkedInPort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioLinkedInPort(api_key=api_key, user_id=user_id)
    return DisabledLinkedInPort()
